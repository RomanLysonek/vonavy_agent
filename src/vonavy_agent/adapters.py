from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from pydantic import Field, model_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vonavy_agent.domain import ExperimentSpec, StrictModel
from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_json
from vonavy_agent.identity import LOCAL_OWNER_ID
from vonavy_agent.managed_files import publish_bytes
from vonavy_agent.persistence import AdapterSnapshot, session_scope
from vonavy_agent.settings import Settings

SHELL_FRAGMENT = re.compile(r"[;&|`]|[$][(]|[\r\n\x00]")


class CapabilityManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_kind: Literal["capability"] = "capability"
    adapter_kind: Literal["anomaly", "chronos"]
    adapter_version: str
    available: bool
    supported_frequencies: tuple[str, ...] = ()
    supports_probability_calibration: bool = False
    requires_confirmation: bool = True
    estimated_wall_seconds: int | None = Field(default=None, ge=0)
    estimated_memory_mb: int | None = Field(default=None, ge=0)
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def availability_reason(self) -> CapabilityManifest:
        if not self.available and not self.unavailable_reason:
            raise ValueError("unavailable capabilities require an unavailable_reason")
        return self


class ExternalResultManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_kind: Literal["result"] = "result"
    adapter_kind: Literal["anomaly", "chronos"]
    adapter_version: str
    source_revision: str | None = None
    dataset_hash: str
    spec_hash: str
    metrics: dict[str, float | None]
    unsupported: dict[str, str] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class ResourceEstimate(StrictModel):
    wall_seconds: int
    memory_mb: int
    supported: bool
    reason: str | None = None


class PreparedInvocation(StrictModel):
    executable: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int = Field(ge=1, le=86_400)
    environment: dict[str, str] = Field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()


class Adapter(Protocol):
    def capabilities(self) -> CapabilityManifest: ...

    def estimate(self, spec: ExperimentSpec) -> ResourceEstimate: ...

    def prepare(self, spec: ExperimentSpec, managed_dir: Path) -> PreparedInvocation: ...

    def run(self, prepared: PreparedInvocation) -> object: ...

    def collect(self, result: object) -> ExternalResultManifest: ...


def validate_invocation(
    invocation: PreparedInvocation,
    *,
    allowed_executables: set[Path],
    allowed_root: Path,
) -> PreparedInvocation:
    executable = Path(invocation.executable)
    if not executable.is_absolute() or executable.resolve() not in {
        path.resolve() for path in allowed_executables
    }:
        raise AgentError("adapter_executable_denied", "Adapter executable is not allow-listed")
    cwd = Path(invocation.cwd).resolve()
    root = allowed_root.resolve()
    if cwd != root and root not in cwd.parents:
        raise AgentError("adapter_cwd_denied", "Adapter working directory is outside its root")
    if any(SHELL_FRAGMENT.search(argument) for argument in invocation.argv):
        raise AgentError("adapter_shell_fragment", "Adapter argv contains a shell fragment")
    for output in invocation.expected_outputs:
        output_path = (cwd / output).resolve()
        if output_path == cwd or cwd not in output_path.parents:
            raise AgentError(
                "adapter_output_denied", "Expected output escapes the working directory"
            )
    return invocation


def dry_run_invocation(
    invocation: PreparedInvocation,
    *,
    allowed_executables: set[Path],
    allowed_root: Path,
) -> dict[str, object]:
    validated = validate_invocation(
        invocation, allowed_executables=allowed_executables, allowed_root=allowed_root
    )
    return {
        "executed": False,
        "argv": [validated.executable, *validated.argv],
        "cwd": validated.cwd,
        "timeout_seconds": validated.timeout_seconds,
        "expected_outputs": list(validated.expected_outputs),
    }


def import_adapter_snapshot(
    engine: Engine,
    settings: Settings,
    stream: BinaryIO,
    original_name: str,
    owner_id: str = LOCAL_OWNER_ID,
) -> AdapterSnapshot:
    if Path(original_name).name != original_name or Path(original_name).suffix.lower() != ".json":
        raise AgentError("invalid_snapshot_name", "Adapter snapshots must be plain JSON basenames")
    content = bytearray()
    while chunk := stream.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > min(settings.max_upload_bytes, 10 * 1024 * 1024):
            raise AgentError("snapshot_too_large", "Adapter snapshot exceeds 10 MB")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AgentError("invalid_snapshot_json", f"Invalid adapter snapshot: {exc}") from exc
    kind = payload.get("manifest_kind")
    parsed: CapabilityManifest | ExternalResultManifest
    if kind == "capability":
        parsed = CapabilityManifest.model_validate(payload)
    elif kind == "result":
        parsed = ExternalResultManifest.model_validate(payload)
    else:
        raise AgentError("unknown_manifest_kind", "Unknown adapter manifest kind")
    source_hash = sha256(content).hexdigest()
    try:
        publish_bytes(
            settings,
            Path("imports"),
            f"{source_hash}.json",
            bytes(content),
            source_hash,
        )
    except OSError as exc:
        raise AgentError(
            "adapter_snapshot_integrity",
            "Adapter snapshot destination is not a safe managed file",
            status_code=500,
        ) from exc
    with session_scope(engine) as session:
        row = AdapterSnapshot(
            owner_id=owner_id,
            adapter_kind=parsed.adapter_kind,
            manifest_kind=parsed.manifest_kind,
            schema_version=parsed.schema_version,
            source_sha256=source_hash,
            canonical_json=canonical_json(parsed.model_dump(mode="json")),
        )
        session.add(row)
        session.flush()
        row_id = row.id
    with Session(engine) as session:
        return session.get_one(AdapterSnapshot, row_id)


def adapter_capabilities(
    engine: Engine,
    owner_id: str = LOCAL_OWNER_ID,
) -> list[dict[str, object]]:
    defaults: dict[str, dict[str, object]] = {
        "anomaly": CapabilityManifest(
            adapter_kind="anomaly",
            adapter_version="unconfigured",
            available=False,
            unavailable_reason="No validated anomaly capability snapshot has been imported",
        ).model_dump(mode="json"),
        "chronos": CapabilityManifest(
            adapter_kind="chronos",
            adapter_version="unconfigured",
            available=False,
            unavailable_reason="No validated Chronos capability snapshot has been imported",
        ).model_dump(mode="json"),
    }
    with Session(engine) as session:
        rows = (
            session.query(AdapterSnapshot)
            .filter_by(owner_id=owner_id, manifest_kind="capability")
            .all()
        )
    for row in sorted(rows, key=lambda item: item.created_at):
        defaults[row.adapter_kind] = json.loads(row.canonical_json)
    return [defaults[name] for name in sorted(defaults)]
