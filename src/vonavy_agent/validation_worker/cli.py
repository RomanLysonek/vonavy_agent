from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vonavy_agent import __version__
from vonavy_agent.hashing import canonical_json
from vonavy_agent.validation_contracts import (
    LocalInputArtifact,
    LocalOutputArtifact,
    ValidationIssue,
    ValidationRequest,
    ValidationResourceUsage,
    ValidationResult,
    ValidationStatus,
)
from vonavy_agent.validation_worker.artifacts import (
    LocalFileArtifactReader,
    LocalFileArtifactWriter,
    LocalWorkspace,
    UnsafeArtifactPathError,
)
from vonavy_agent.validation_worker.worker import validate_request


def _fallback_result(raw: object, code: str, message: str) -> ValidationResult:
    now = datetime.now(UTC)
    job_id: str | None = None
    dataset_id: str | None = None
    if isinstance(raw, dict):
        candidate_job = raw.get("job_id")
        candidate_dataset = raw.get("dataset_id")
        job_id = candidate_job if isinstance(candidate_job, str) else None
        dataset_id = candidate_dataset if isinstance(candidate_dataset, str) else None
    return ValidationResult(
        job_id=job_id,
        dataset_id=dataset_id,
        status=ValidationStatus.FAILED,
        started_at=now,
        finished_at=now,
        duration_ms=0,
        validation_errors=(ValidationIssue(code=code, message=message),),
        resource_usage=ValidationResourceUsage(
            peak_rss_mb=0,
            cpu_seconds=0,
            profiled_rows=0,
            profiling_sampled=False,
        ),
        worker_version=__version__,
    )


def run_cli(request_path: Path, result_path: str, workspace_path: Path | None = None) -> int:
    workspace_root = workspace_path or request_path.parent
    raw: Any = None
    try:
        workspace = LocalWorkspace(workspace_root)
        writer = LocalFileArtifactWriter(workspace)
    except (OSError, UnsafeArtifactPathError):
        print('{"status":"failed","code":"unsafe_workspace"}')
        return 1
    try:
        raw = json.loads(request_path.read_text(encoding="utf-8"))
        request = ValidationRequest.model_validate(raw)
        if not isinstance(request.input, LocalInputArtifact) or not isinstance(
            request.output, LocalOutputArtifact
        ):
            result = _fallback_result(
                raw,
                "unsupported_storage",
                "The local CLI only supports local input and output artifacts",
            )
        elif request.output.path != result_path:
            result = _fallback_result(
                raw,
                "output_path_mismatch",
                "CLI result path must match the request output artifact path",
            )
        else:
            result = validate_request(request, LocalFileArtifactReader(workspace))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        result = _fallback_result(
            raw,
            "invalid_request",
            "Validation request is missing, malformed, or incompatible",
        )
    try:
        payload = canonical_json(result.model_dump(mode="json")) + "\n"
        writer.write_bytes(result_path, payload.encode("utf-8"))
    except (OSError, UnsafeArtifactPathError):
        print('{"status":"failed","code":"output_write_failure"}')
        return 1
    print(canonical_json({"status": result.status.value, "result": result_path}))
    if result.status == ValidationStatus.SUCCEEDED:
        return 0
    if result.status == ValidationStatus.INVALID:
        return 2
    return 1
