from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORECAST_REQUEST_SCHEMA: Literal["forecast-request/v1"] = "forecast-request/v1"
FORECAST_RESULT_SCHEMA: Literal["forecast-result/v1"] = "forecast-result/v1"
MODEL_MANIFEST_SCHEMA: Literal["model-artifact-manifest/v1"] = "model-artifact-manifest/v1"
AdapterId: TypeAlias = Literal[
    "xgboost-direct-v1",
    "neuralnet-direct-v1",
    "chronos2-zero-shot-v1",
]
ADAPTER_ID: Literal["xgboost-direct-v1"] = "xgboost-direct-v1"
NEURALNET_ADAPTER_ID: Literal["neuralnet-direct-v1"] = "neuralnet-direct-v1"
CHRONOS2_ADAPTER_ID: Literal["chronos2-zero-shot-v1"] = "chronos2-zero-shot-v1"
MODEL_SOURCE_REPOSITORY = "RomanLysonek/vonava_predikce"
MODEL_SOURCE_REVISION = "8fb0b4634a9e67554b548af47afc18f68f9b0dd7"
CHRONOS2_SOURCE_REPOSITORY = "RomanLysonek/vonavy_chronos"
CHRONOS2_SOURCE_REVISION = "ecda712f64883b348fe612446475264da1a66ce9"

_ADAPTER_SOURCES: dict[str, tuple[str, str]] = {
    ADAPTER_ID: (MODEL_SOURCE_REPOSITORY, MODEL_SOURCE_REVISION),
    NEURALNET_ADAPTER_ID: (MODEL_SOURCE_REPOSITORY, MODEL_SOURCE_REVISION),
    CHRONOS2_ADAPTER_ID: (CHRONOS2_SOURCE_REPOSITORY, CHRONOS2_SOURCE_REVISION),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ForecastStatus(StrEnum):
    SUCCEEDED = "succeeded"
    INVALID = "invalid"
    FAILED = "failed"


class ForecastMapping(StrictModel):
    timestamp_column: str = Field(min_length=1, max_length=128)
    target_column: str = Field(min_length=1, max_length=128)
    entity_column: str | None = Field(default=None, min_length=1, max_length=128)
    availability_column: str | None = Field(default=None, min_length=1, max_length=128)
    known_future_numeric: tuple[str, ...] = ()
    known_future_categorical: tuple[str, ...] = ()
    static_numeric: tuple[str, ...] = ()
    static_categorical: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    @field_validator(
        "known_future_numeric",
        "known_future_categorical",
        "static_numeric",
        "static_categorical",
        "excluded",
    )
    @classmethod
    def _unique_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        clean = tuple(value.strip() for value in values)
        if any(not value or len(value) > 128 for value in clean):
            raise ValueError("column names must contain 1-128 characters")
        if len(set(clean)) != len(clean):
            raise ValueError("column lists must not contain duplicates")
        return clean

    @model_validator(mode="after")
    def _disjoint_roles(self) -> ForecastMapping:
        primary = {
            self.timestamp_column,
            self.target_column,
            self.entity_column,
            self.availability_column,
        } - {None}
        groups = (
            self.known_future_numeric,
            self.known_future_categorical,
            self.static_numeric,
            self.static_categorical,
            self.excluded,
        )
        flattened = [value for group in groups for value in group]
        if len(flattened) != len(set(flattened)):
            raise ValueError("a column may have only one semantic role")
        if primary.intersection(flattened):
            raise ValueError("primary columns cannot also be feature or excluded columns")
        return self

    @property
    def required_columns(self) -> tuple[str, ...]:
        values: list[str] = [self.timestamp_column, self.target_column]
        values.extend(value for value in (self.entity_column, self.availability_column) if value)
        values.extend(self.known_future_numeric)
        values.extend(self.known_future_categorical)
        values.extend(self.static_numeric)
        values.extend(self.static_categorical)
        return tuple(values)


class ForecastLimits(StrictModel):
    max_bytes: int = Field(ge=1, le=2_000_000_000)
    max_rows: int = Field(ge=1, le=10_000_000)
    max_entities: int = Field(ge=1, le=100_000)
    max_history_days: int = Field(ge=35, le=10_000)
    threads: Literal[1] = 1


class S3InputArtifact(StrictModel):
    bucket: str = Field(min_length=3, max_length=63)
    key: str = Field(min_length=1, max_length=1024)
    version_id: str = Field(min_length=1, max_length=1024)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["text/csv", "application/vnd.apache.parquet"]
    byte_size: int = Field(ge=1)


class S3OutputArtifact(StrictModel):
    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = Field(min_length=1, max_length=900)


class ForecastRequest(StrictModel):
    schema_version: Literal["forecast-request/v1"] = FORECAST_REQUEST_SCHEMA
    owner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
    dataset_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    input: S3InputArtifact
    output: S3OutputArtifact
    mapping: ForecastMapping
    training_end: date
    horizon_days: Literal[7] = 7
    adapter_id: AdapterId = ADAPTER_ID
    seed: Literal[42] = 42
    limits: ForecastLimits
    source_revision: str = Field(pattern=r"^(unknown|[0-9a-f]{40})$")
    requested_at: datetime


class LocalForecastRequest(StrictModel):
    schema_version: Literal["forecast-request/v1"] = FORECAST_REQUEST_SCHEMA
    owner_id: str = "local"
    dataset_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    run_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    input_path: str = Field(min_length=1, max_length=512)
    output_directory: str = Field(min_length=1, max_length=512)
    media_type: Literal["text/csv", "application/vnd.apache.parquet"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mapping: ForecastMapping
    training_end: date
    horizon_days: Literal[7] = 7
    adapter_id: AdapterId = ADAPTER_ID
    seed: Literal[42] = 42
    limits: ForecastLimits
    source_revision: str = Field(pattern=r"^(unknown|[0-9a-f]{40})$")
    requested_at: datetime


class ForecastIssue(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    column: str | None = None
    entity: str | None = None
    count: int | None = Field(default=None, ge=0)


class HoldoutMetrics(StrictModel):
    supported: bool
    origin: date | None = None
    rows: int = Field(ge=0)
    wape: float | None = Field(default=None, ge=0)
    mae: float | None = Field(default=None, ge=0)
    bias: float | None = None
    coverage: float = Field(ge=0, le=1)
    unsupported_reason: str | None = None


class ForecastProfile(StrictModel):
    rows: int = Field(ge=0)
    entities: int = Field(ge=0)
    history_start: date | None
    training_end: date
    forecast_start: date
    forecast_end: date
    trainable_rows: int = Field(ge=0)
    fallback_rows: int = Field(ge=0)


class ForecastTiming(StrictModel):
    prepare_seconds: float = Field(ge=0)
    holdout_seconds: float = Field(ge=0)
    fit_seconds: float = Field(ge=0)
    forecast_seconds: float = Field(ge=0)
    total_seconds: float = Field(ge=0)


class ArtifactReference(StrictModel):
    key: str = Field(min_length=1)
    version_id: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)


class ForecastArtifacts(StrictModel):
    forecast: ArtifactReference
    model: ArtifactReference
    manifest: ArtifactReference
    result: ArtifactReference | None = None


class AdapterIdentity(StrictModel):
    id: AdapterId = ADAPTER_ID
    source_repository: str = MODEL_SOURCE_REPOSITORY
    source_revision: str = MODEL_SOURCE_REVISION

    @model_validator(mode="before")
    @classmethod
    def _bind_source(cls, data: object) -> object:
        if data is None:
            return data
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            return data
        values = dict(data)
        adapter_id = str(values.get("id", ADAPTER_ID))
        expected = _ADAPTER_SOURCES.get(adapter_id)
        if expected is None:
            return values
        repository, revision = expected
        supplied_repository = values.get("source_repository")
        supplied_revision = values.get("source_revision")
        if supplied_repository not in (None, repository):
            raise ValueError("adapter source_repository does not match adapter id")
        if supplied_revision not in (None, revision):
            raise ValueError("adapter source_revision does not match adapter id")
        values["source_repository"] = repository
        values["source_revision"] = revision
        return values


class InputIdentity(StrictModel):
    bucket: str | None = None
    key: str | None = None
    version_id: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForecastResult(StrictModel):
    schema_version: Literal["forecast-result/v1"] = FORECAST_RESULT_SCHEMA
    status: ForecastStatus
    owner_id: str
    dataset_id: str
    run_id: str
    adapter: AdapterIdentity = Field(default_factory=AdapterIdentity)
    input: InputIdentity
    profile: ForecastProfile
    holdout: HoldoutMetrics
    artifacts: ForecastArtifacts | None
    warnings: tuple[ForecastIssue, ...] = ()
    failure: ForecastIssue | None = None
    timing: ForecastTiming
    started_at: datetime
    finished_at: datetime


class ModelArtifactManifest(StrictModel):
    schema_version: Literal["model-artifact-manifest/v1"] = MODEL_MANIFEST_SCHEMA
    adapter: AdapterIdentity = Field(default_factory=AdapterIdentity)
    vonavy_agent_source_revision: str
    owner_id: str
    dataset_id: str
    run_id: str
    input: InputIdentity
    mapping: ForecastMapping
    training_end: date
    horizon_days: Literal[7] = 7
    seed: Literal[42] = 42
    parameters: dict[str, int | float | str | bool]
    feature_order: tuple[str, ...]
    categorical_levels: dict[str, tuple[str, ...]]
    holdout: HoldoutMetrics
    package_versions: dict[str, str]
    model_sha256: str | None = None
    forecast_sha256: str | None = None
