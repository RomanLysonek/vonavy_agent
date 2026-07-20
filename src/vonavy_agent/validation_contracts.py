from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from vonavy_agent.domain import StrictModel

VALIDATION_REQUEST_SCHEMA: Literal["validation-request/v1"] = "validation-request/v1"
VALIDATION_RESULT_SCHEMA: Literal["validation-result/v1"] = "validation-result/v1"
ColumnLogicalType: TypeAlias = Literal["numeric", "string", "boolean", "date", "timestamp", "other"]
QuantileName: TypeAlias = Literal["p01", "p05", "p25", "p50", "p75", "p95", "p99"]

SUPPORTED_VALIDATION_MEDIA_TYPES = {
    "text/csv",
    "application/vnd.apache.parquet",
    "application/x-parquet",
}


def _safe_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not path.parts:
        raise ValueError("artifact paths must be non-empty relative paths")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact paths must not contain empty, dot, or parent components")
    return path.as_posix()


def _validated_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("expected_sha256 must be a 64-character hexadecimal digest")
    return normalized


class ValidationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    INVALID = "invalid"
    FAILED = "failed"


class LocalInputArtifact(StrictModel):
    storage: Literal["local"] = "local"
    path: str
    media_type: str = Field(min_length=1, max_length=200)
    expected_size_bytes: Annotated[int, Field(ge=0)] | None = None
    expected_sha256: str | None = None

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("expected_sha256")
    @classmethod
    def valid_sha256(cls, value: str | None) -> str | None:
        return _validated_sha256(value)


class LocalOutputArtifact(StrictModel):
    storage: Literal["local"] = "local"
    path: str

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class S3InputArtifact(StrictModel):
    storage: Literal["s3"] = "s3"
    bucket: str = Field(min_length=3, max_length=63)
    key: str = Field(min_length=1, max_length=1_024)
    version_id: str = Field(min_length=1, max_length=1_024)
    media_type: str = Field(min_length=1, max_length=200)
    expected_size_bytes: Annotated[int, Field(ge=0)] | None = None
    expected_sha256: str | None = None

    @field_validator("expected_sha256")
    @classmethod
    def valid_sha256(cls, value: str | None) -> str | None:
        return _validated_sha256(value)


class S3OutputArtifact(StrictModel):
    storage: Literal["s3"] = "s3"
    bucket: str = Field(min_length=3, max_length=63)
    key: str = Field(min_length=1, max_length=1_024)


InputArtifact: TypeAlias = Annotated[
    LocalInputArtifact | S3InputArtifact,
    Field(discriminator="storage"),
]
OutputArtifact: TypeAlias = Annotated[
    LocalOutputArtifact | S3OutputArtifact,
    Field(discriminator="storage"),
]


class ValidationLimits(StrictModel):
    max_input_bytes: Annotated[int, Field(ge=1, le=10 * 1024**3)] = 250 * 1024**2
    max_rows: Annotated[int, Field(ge=1, le=10_000_000)] = 500_000
    max_columns: Annotated[int, Field(ge=1, le=10_000)] = 250
    max_string_sample_length: Annotated[int, Field(ge=16, le=65_536)] = 512
    max_distinct_values: Annotated[int, Field(ge=1, le=1_000)] = 20
    max_profile_rows: Annotated[int, Field(ge=1, le=100_000)] = 5_000
    max_execution_seconds: Annotated[int, Field(ge=1, le=86_400)] = 900


class ValidationRequest(StrictModel):
    schema_version: Literal["validation-request/v1"] = VALIDATION_REQUEST_SCHEMA
    job_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=200)
    input: InputArtifact
    output: OutputArtifact
    limits: ValidationLimits = ValidationLimits()
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def aware_requested_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must include an explicit timezone")
        return value.astimezone(UTC)


class ValidationIssue(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    column: str | None = None
    count: Annotated[int, Field(ge=0)] | None = None
    detail: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TopValue(StrictModel):
    value: str
    count: Annotated[int, Field(ge=1)]
    truncated: bool = False


class NumericStatistics(StrictModel):
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    standard_deviation: float | None = None
    quantiles: dict[QuantileName, float] = Field(default_factory=dict)
    zero_count: Annotated[int, Field(ge=0)] = 0
    negative_count: Annotated[int, Field(ge=0)] = 0
    positive_count: Annotated[int, Field(ge=0)] = 0
    non_finite_count: Annotated[int, Field(ge=0)] = 0


class StringStatistics(StrictModel):
    minimum_length: Annotated[int, Field(ge=0)] | None = None
    maximum_length: Annotated[int, Field(ge=0)] | None = None
    mean_length: float | None = None
    empty_string_count: Annotated[int, Field(ge=0)] = 0
    distinct_count: Annotated[int, Field(ge=0)] = 0
    distinct_is_approximate: bool = False
    values_exceeding_sample_limit: Annotated[int, Field(ge=0)] = 0
    top_values: tuple[TopValue, ...] = ()


class BooleanStatistics(StrictModel):
    true_count: Annotated[int, Field(ge=0)] = 0
    false_count: Annotated[int, Field(ge=0)] = 0


class TemporalStatistics(StrictModel):
    minimum: str | None = None
    maximum: str | None = None
    invalid_count: Annotated[int, Field(ge=0)] = 0
    timezone_aware: bool = False


class ColumnProfile(StrictModel):
    name: str
    logical_type: ColumnLogicalType
    physical_type: str
    row_count: Annotated[int, Field(ge=0)]
    null_count: Annotated[int, Field(ge=0)]
    null_ratio: Annotated[float, Field(ge=0, le=1)]
    non_null_count: Annotated[int, Field(ge=0)]
    statistics_basis: Literal["exact", "sampled"]
    sample_size: Annotated[int, Field(ge=0)]
    numeric: NumericStatistics | None = None
    string: StringStatistics | None = None
    boolean: BooleanStatistics | None = None
    temporal: TemporalStatistics | None = None


class LocalInputIdentity(StrictModel):
    storage: Literal["local"] = "local"
    path: str
    media_type: str
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: str


class S3InputIdentity(StrictModel):
    storage: Literal["s3"] = "s3"
    bucket: str
    key: str
    version_id: str
    media_type: str
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: str


InputIdentity: TypeAlias = Annotated[
    LocalInputIdentity | S3InputIdentity,
    Field(discriminator="storage"),
]


class ValidationResourceUsage(StrictModel):
    peak_rss_mb: Annotated[float, Field(ge=0)]
    cpu_seconds: Annotated[float, Field(ge=0)]
    profiled_rows: Annotated[int, Field(ge=0)]
    profiling_sampled: bool


class ValidationResult(StrictModel):
    schema_version: Literal["validation-result/v1"] = VALIDATION_RESULT_SCHEMA
    job_id: str | None = None
    dataset_id: str | None = None
    status: ValidationStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: Annotated[int, Field(ge=0)]
    input_identity: InputIdentity | None = None
    format: Literal["csv", "parquet"] | None = None
    row_count: Annotated[int, Field(ge=0)] | None = None
    column_count: Annotated[int, Field(ge=0)] | None = None
    columns: tuple[ColumnProfile, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    validation_errors: tuple[ValidationIssue, ...] = ()
    resource_usage: ValidationResourceUsage
    worker_version: str

    @field_validator("started_at", "finished_at")
    @classmethod
    def aware_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("result timestamps must include an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def terminal_contract(self) -> ValidationResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.status == ValidationStatus.SUCCEEDED and self.validation_errors:
            raise ValueError("succeeded results cannot contain validation errors")
        if self.status != ValidationStatus.SUCCEEDED and not self.validation_errors:
            raise ValueError("non-succeeded results require at least one validation error")
        return self
