from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FeatureRole(StrEnum):
    PAST_ONLY = "past_only"
    KNOWN_FUTURE = "known_future"
    STATIC = "static"
    EXCLUDED = "excluded"


class AvailabilityKind(StrEnum):
    COLUMN = "column"
    EVENT_TIME = "available_at_event_time"
    ORIGIN = "known_at_origin"
    ALWAYS = "always"


class AvailabilityPolicy(StrictModel):
    kind: AvailabilityKind
    column: str | None = None

    @model_validator(mode="after")
    def column_matches_kind(self) -> AvailabilityPolicy:
        if self.kind == AvailabilityKind.COLUMN and not self.column:
            raise ValueError("column is required for column availability")
        if self.kind != AvailabilityKind.COLUMN and self.column is not None:
            raise ValueError("column is only valid for column availability")
        return self


class FeatureMapping(StrictModel):
    name: str = Field(min_length=1)
    role: FeatureRole
    availability: AvailabilityPolicy

    @model_validator(mode="after")
    def role_matches_availability(self) -> FeatureMapping:
        allowed = {
            FeatureRole.PAST_ONLY: {AvailabilityKind.COLUMN, AvailabilityKind.EVENT_TIME},
            FeatureRole.KNOWN_FUTURE: {AvailabilityKind.COLUMN, AvailabilityKind.ORIGIN},
            FeatureRole.STATIC: {AvailabilityKind.COLUMN, AvailabilityKind.ALWAYS},
            FeatureRole.EXCLUDED: set(AvailabilityKind),
        }
        if self.availability.kind not in allowed[self.role]:
            raise ValueError(f"{self.availability.kind} is incompatible with {self.role}")
        return self


class DatasetMappingSpec(StrictModel):
    timestamp_column: str
    entity_column: str | None = None
    target_column: str
    frequency: Literal["D"] = "D"
    target_availability: AvailabilityPolicy
    observation_availability_column: str | None = None
    features: tuple[FeatureMapping, ...] = ()

    @model_validator(mode="after")
    def unique_columns(self) -> DatasetMappingSpec:
        if self.target_availability.kind not in {
            AvailabilityKind.COLUMN,
            AvailabilityKind.EVENT_TIME,
        }:
            raise ValueError(
                "target availability must be event-time or an explicit known-at column"
            )
        feature_names = [feature.name for feature in self.features]
        if len(feature_names) != len(set(feature_names)):
            raise ValueError("feature names must be unique")
        reserved = {self.timestamp_column, self.target_column}
        if self.entity_column:
            reserved.add(self.entity_column)
        if self.observation_availability_column:
            reserved.add(self.observation_availability_column)
        if reserved.intersection(feature_names):
            raise ValueError("timestamp, entity, and target columns cannot also be features")
        return self


class DateRange(StrictModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> DateRange:
        if self.start > self.end:
            raise ValueError("date range start must not be after end")
        return self


class OriginSpec(StrictModel):
    date: date
    role: Literal["calibration", "test"]


class SeasonalNaiveConfig(StrictModel):
    kind: Literal["seasonal_naive"] = "seasonal_naive"
    period_days: Annotated[int, Field(ge=1, le=366)] = 7


class MovingAverageConfig(StrictModel):
    kind: Literal["moving_average"] = "moving_average"
    window_days: Annotated[int, Field(ge=2, le=366)] = 28


class RidgeDirectConfig(StrictModel):
    kind: Literal["ridge_direct"] = "ridge_direct"
    alpha: Annotated[float, Field(gt=0, le=1_000_000)] = 1.0
    lag_days: tuple[Annotated[int, Field(ge=1, le=366)], ...] = Field(
        default=(1, 7, 14, 28), min_length=1
    )
    rolling_days: tuple[Annotated[int, Field(ge=2, le=366)], ...] = Field(
        default=(7, 28), min_length=1
    )


ModelConfig = SeasonalNaiveConfig | MovingAverageConfig | RidgeDirectConfig


class ResourceLimits(StrictModel):
    max_rows: Annotated[int, Field(ge=1, le=5_000_000)] = 500_000
    max_entities: Annotated[int, Field(ge=1, le=100_000)] = 5_000
    max_origins: Annotated[int, Field(ge=1, le=500)] = 50
    max_models: Annotated[int, Field(ge=1, le=10)] = 3
    wall_seconds: Annotated[int, Field(ge=5, le=86_400)] = 900
    memory_mb: Annotated[int, Field(ge=128, le=65_536)] = 4_096
    threads: Literal[1] = 1


class ExperimentSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_version_id: str
    mapping_id: str
    profile_id: str
    frequency: Literal["D"] = "D"
    train: DateRange
    calibration: DateRange
    test: DateRange
    origins: tuple[OriginSpec, ...]
    horizon_days: Annotated[int, Field(ge=1, le=90)]
    training_window_days: Annotated[int, Field(ge=14, le=3_650)]
    entity_column: str | None = None
    target_column: str
    features: tuple[FeatureMapping, ...] = ()
    feature_allow_list: tuple[str, ...] | None = None
    scoring_availability_policy: Literal[
        "assume_available", "available_only", "require_available"
    ] = "assume_available"
    models: tuple[ModelConfig, ...]
    seeds: tuple[Annotated[int, Field(ge=0, le=2**31 - 1)], ...] = (42,)
    metrics: tuple[Literal["wape", "mae", "rmse", "bias", "coverage", "runtime"], ...] = (
        "wape",
        "mae",
        "rmse",
        "bias",
        "coverage",
        "runtime",
    )
    evaluation_as_of: datetime
    minimum_coverage: Annotated[float, Field(ge=0, le=1)] = 0.8
    resources: ResourceLimits = ResourceLimits()

    @field_validator("evaluation_as_of")
    @classmethod
    def aware_evaluation_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation_as_of must include an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_boundaries(self) -> ExperimentSpec:
        if not (self.train.end < self.calibration.start <= self.calibration.end < self.test.start):
            raise ValueError(
                "train, calibration, and test must be chronological and non-overlapping"
            )
        if not self.origins:
            raise ValueError("at least one forecast origin is required")
        origin_keys = [(origin.date, origin.role) for origin in self.origins]
        if len(origin_keys) != len(set(origin_keys)):
            raise ValueError("forecast origins must be unique")
        for origin in self.origins:
            window = self.calibration if origin.role == "calibration" else self.test
            if not window.start <= origin.date <= window.end:
                raise ValueError(f"{origin.role} origin is outside its split")
            if (
                origin.date.fromordinal(origin.date.toordinal() + self.horizon_days - 1)
                > window.end
            ):
                raise ValueError(f"{origin.role} horizon crosses its split boundary")
        if len(self.origins) > self.resources.max_origins:
            raise ValueError("origin count exceeds resource limit")
        if len(self.models) > self.resources.max_models:
            raise ValueError("model count exceeds resource limit")
        if len({model.kind for model in self.models}) != len(self.models):
            raise ValueError("model kinds must be unique")
        if not self.models:
            raise ValueError("at least one model is required")
        if len(self.seeds) != len(set(self.seeds)):
            raise ValueError("seeds must be unique")
        if not self.seeds:
            raise ValueError("at least one seed is required")
        mapped = {feature.name: feature for feature in self.features}
        if self.feature_allow_list is not None:
            if len(self.feature_allow_list) != len(set(self.feature_allow_list)):
                raise ValueError("feature allow-list entries must be unique")
            unknown = set(self.feature_allow_list) - set(mapped)
            if unknown:
                raise ValueError(f"feature allow-list contains unknown features: {sorted(unknown)}")
            excluded = [
                name
                for name in self.feature_allow_list
                if mapped[name].role == FeatureRole.EXCLUDED
            ]
            if excluded:
                raise ValueError(f"excluded features cannot be active: {excluded}")
        return self

    def selected_features(self) -> tuple[FeatureMapping, ...]:
        allowed = (
            set(self.feature_allow_list)
            if self.feature_allow_list is not None
            else {feature.name for feature in self.features if feature.role != FeatureRole.EXCLUDED}
        )
        return tuple(feature for feature in self.features if feature.name in allowed)


class GateReason(StrictModel):
    code: str
    message: str
    count: int = 0
    examples: tuple[str, ...] = ()
    remediation: str | None = None


class GateReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["passed", "blocked"]
    reasons: tuple[GateReason, ...] = ()
    warnings: tuple[GateReason, ...] = ()
    evidence: dict[str, object] = Field(default_factory=dict)
    confirmation_token: str | None = None


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    PUBLISHING = "publishing"
    FINALIZING = "finalizing"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_JOB_STATES = {JobState.CANCELLED, JobState.SUCCEEDED, JobState.FAILED}
