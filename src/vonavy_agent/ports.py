from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from vonavy_agent.domain import EvaluationSpec, ForecastSpec, InferenceSpec
from vonavy_agent.identity import IdentityContext


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    byte_size: int
    media_type: str


class ObjectStore(Protocol):
    """Durable immutable object storage boundary.

    Local mode maps this to managed files; AWS mode will map it to private S3.
    """

    def put_file(self, source: Path, *, key: str, media_type: str) -> StoredObject: ...

    def open_verified(self, stored: StoredObject) -> AbstractContextManager[BinaryIO]: ...

    def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SubmittedJob:
    job_id: str
    state: str


class JobBackend(Protocol):
    """Execution boundary for local subprocesses or AWS Batch."""

    def submit(
        self,
        identity: IdentityContext,
        *,
        kind: str,
        payload: Mapping[str, Any],
    ) -> SubmittedJob: ...

    def cancel(self, identity: IdentityContext, job_id: str) -> SubmittedJob: ...


class ExperimentRepository(Protocol):
    """Owner-scoped metadata persistence boundary.

    The initial implementation remains SQLAlchemy/SQLite. DynamoDB can implement
    this contract without leaking cloud details into domain or API code.
    """

    def owner_exists(self, identity: IdentityContext, aggregate_id: str) -> bool: ...

    def iter_owner_runs(self, identity: IdentityContext) -> Iterator[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    adapter_kind: str
    compute_class: str
    minimum_vram_gb: int | None
    supports_evaluation: bool
    supports_forecast: bool
    supports_saved_model_inference: bool


class ModelAdapter(Protocol):
    """Allow-listed model implementation boundary used by disposable runners."""

    def capabilities(self) -> ModelCapabilities: ...

    def evaluate(self, spec: EvaluationSpec, workdir: Path) -> Mapping[str, Any]: ...

    def forecast(self, spec: ForecastSpec, workdir: Path) -> Mapping[str, Any]: ...

    def infer(self, spec: InferenceSpec, workdir: Path) -> Mapping[str, Any]: ...
