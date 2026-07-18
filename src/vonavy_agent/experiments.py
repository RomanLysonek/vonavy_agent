from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vonavy_agent.datasets import DatasetRegistry, observation_availability
from vonavy_agent.domain import (
    AvailabilityKind,
    DatasetMappingSpec,
    ExperimentSpec,
    FeatureRole,
    GateReason,
    GateReport,
)
from vonavy_agent.eligibility import expected_grid
from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_hash, canonical_json
from vonavy_agent.identity import LOCAL_OWNER_ID
from vonavy_agent.persistence import (
    DataProfile,
    DatasetMapping,
    DatasetVersion,
    ExperimentSpecRow,
    GateResultRow,
    session_scope,
)
from vonavy_agent.policy import ResourcePolicy


def create_experiment_spec(
    engine: Engine,
    spec: ExperimentSpec,
    owner_id: str = LOCAL_OWNER_ID,
    resource_policy: ResourcePolicy | None = None,
) -> ExperimentSpecRow:
    if resource_policy is not None:
        resource_policy.validate(spec.resources)
    payload = spec.model_dump(mode="json")
    with session_scope(engine) as session:
        profile = session.get(DataProfile, spec.profile_id)
        mapping = session.get(DatasetMapping, spec.mapping_id)
        version = session.get(DatasetVersion, spec.dataset_version_id)
        if (
            profile is None
            or mapping is None
            or version is None
            or profile.owner_id != owner_id
            or mapping.owner_id != owner_id
            or version.owner_id != owner_id
        ):
            raise AgentError(
                "evidence_not_found", "Mapping or profile does not exist", status_code=404
            )
        if (
            profile.dataset_version_id != spec.dataset_version_id
            or mapping.dataset_version_id != spec.dataset_version_id
            or profile.mapping_id != spec.mapping_id
        ):
            raise AgentError("evidence_mismatch", "Dataset, mapping, and profile do not match")
        row = ExperimentSpecRow(
            owner_id=owner_id,
            spec_hash=canonical_hash(payload),
            canonical_json=canonical_json(payload),
            dataset_version_id=spec.dataset_version_id,
            mapping_id=spec.mapping_id,
            profile_id=spec.profile_id,
        )
        session.add(row)
        session.flush()
        row_id = row.id
    with Session(engine) as session:
        return session.get_one(ExperimentSpecRow, row_id)


def _available_at(
    frame: pd.DataFrame,
    dates: pd.Series,
    kind: AvailabilityKind,
    column: str | None,
) -> pd.Series:
    if kind == AvailabilityKind.COLUMN:
        assert column is not None
        return pd.to_datetime(frame[column], errors="coerce", utc=True, format="mixed")
    if kind == AvailabilityKind.EVENT_TIME:
        return pd.to_datetime(dates, utc=True) + pd.Timedelta(days=1)
    if kind in {AvailabilityKind.ORIGIN, AvailabilityKind.ALWAYS}:
        return pd.Series(pd.Timestamp.min.tz_localize("UTC"), index=frame.index)
    raise AssertionError(f"Unhandled availability kind: {kind}")


@dataclass(frozen=True)
class GateComputation:
    owner_id: str
    spec_id: str
    spec_hash: str
    profile_hash: str
    status: str
    canonical_json: str
    confirmation_token: str | None


def compute_gate(
    engine: Engine,
    registry: DatasetRegistry,
    spec_row_id: str,
    owner_id: str = LOCAL_OWNER_ID,
) -> GateComputation:
    with Session(engine) as session:
        spec_row = session.get(ExperimentSpecRow, spec_row_id)
        if spec_row is None or spec_row.owner_id != owner_id:
            raise AgentError("spec_not_found", "Experiment spec does not exist", status_code=404)
        spec = ExperimentSpec.model_validate_json(spec_row.canonical_json)
        profile_row = session.get_one(DataProfile, spec.profile_id)
        mapping_row = session.get_one(DatasetMapping, spec.mapping_id)
        if profile_row.owner_id != owner_id or mapping_row.owner_id != owner_id:
            raise AgentError(
                "evidence_not_found",
                "Mapping or profile does not exist",
                status_code=404,
            )
        mapping = DatasetMappingSpec.model_validate_json(mapping_row.canonical_json)
        profile = json.loads(profile_row.canonical_json)
        frame = registry.read_materialized_frame(
            session,
            spec.dataset_version_id,
            owner_id,
        ).reset_index(drop=True)
    reasons: list[GateReason] = []
    warnings: list[GateReason] = []

    def block(
        code: str,
        message: str,
        count: int = 0,
        examples: list[str] | None = None,
        remediation: str | None = None,
    ) -> None:
        reasons.append(
            GateReason(
                code=code,
                message=message,
                count=count,
                examples=tuple((examples or [])[:10]),
                remediation=remediation,
            )
        )

    if mapping.target_column != spec.target_column or mapping.entity_column != spec.entity_column:
        block("mapping_spec_mismatch", "Target or entity mapping differs from the experiment spec")
    if (
        mapping.observation_availability_column is None
        and spec.scoring_availability_policy != "assume_available"
    ) or (
        mapping.observation_availability_column is not None
        and spec.scoring_availability_policy == "assume_available"
    ):
        block(
            "observation_availability_policy_mismatch",
            "Scoring availability policy must explicitly match the product-availability mapping",
        )
    mapped_features = {feature.name: feature for feature in mapping.features}
    for feature in spec.features:
        if feature.name not in mapped_features or mapped_features[feature.name] != feature:
            block("feature_mapping_mismatch", f"Feature mapping differs for {feature.name}")
    if profile["rows"] > spec.resources.max_rows:
        block("row_limit", "Dataset row count exceeds the resource limit", profile["rows"])
    if profile["entities"] > spec.resources.max_entities:
        block(
            "entity_limit", "Dataset entity count exceeds the resource limit", profile["entities"]
        )
    if profile["invalid_timestamps"]:
        block(
            "invalid_timestamps",
            "Timestamp column contains invalid values",
            profile["invalid_timestamps"],
        )
    if profile["duplicate_key_rows"]:
        block(
            "duplicate_entity_dates",
            "Duplicate entity/date keys are not allowed",
            profile["duplicate_key_rows"],
            remediation="Create a new dataset version with unique entity/date rows.",
        )
    if profile["gap_days"]:
        block(
            "daily_gaps",
            "Regular daily data has missing dates; no silent filling is allowed",
            profile["gap_days"],
            remediation="Create a complete daily dataset version or shorten the explicit split.",
        )
    timestamps = pd.to_datetime(frame[mapping.timestamp_column], errors="coerce", utc=True)
    dates = timestamps.dt.tz_convert(None).dt.normalize()
    entities = (
        frame[mapping.entity_column].astype("string")
        if mapping.entity_column
        else pd.Series(["__single__"] * len(frame), index=frame.index, dtype="string")
    )
    target = pd.to_numeric(frame[mapping.target_column], errors="coerce")
    relevant_start = pd.Timestamp(spec.train.start)
    relevant_end = pd.Timestamp(spec.test.end)
    relevant = dates.between(relevant_start, relevant_end)
    if mapping.entity_column:
        null_entities = relevant & frame[mapping.entity_column].isna()
        if null_entities.any():
            block(
                "null_entity",
                "Entity identifiers must be present on every required row",
                int(null_entities.sum()),
            )
    target_bad = relevant & (target.isna() | ~np.isfinite(target))
    if target_bad.any():
        block(
            "missing_or_nonfinite_target",
            "Target is missing or non-finite on required rows",
            int(target_bad.sum()),
        )
    selected_features = list(spec.selected_features())
    feature_availability: dict[str, pd.Series] = {}
    for feature in selected_features:
        missing = relevant & frame[feature.name].isna()
        if missing.any():
            block(
                "missing_feature",
                f"Selected feature {feature.name} is missing on required rows",
                int(missing.sum()),
            )
        available = _available_at(
            frame,
            dates,
            feature.availability.kind,
            feature.availability.column,
        )
        feature_availability[feature.name] = available
        invalid_feature_availability = relevant & available.isna()
        if invalid_feature_availability.any():
            block(
                "invalid_feature_availability",
                f"Feature availability contains invalid values for {feature.name}",
                int(invalid_feature_availability.sum()),
            )
        if feature.role == FeatureRole.STATIC:
            varying = (
                pd.DataFrame({"entity": entities, "value": frame[feature.name]})
                .groupby("entity", dropna=False)["value"]
                .nunique(dropna=False)
            )
            changing = varying[varying > 1]
            if not changing.empty:
                block(
                    "changing_static_feature",
                    f"Static feature {feature.name} changes within entities",
                    int(changing.size),
                    [str(value) for value in changing.index[:10]],
                )
    target_available = _available_at(
        frame,
        dates,
        mapping.target_availability.kind,
        mapping.target_availability.column,
    )
    invalid_availability = relevant & target_available.isna()
    if invalid_availability.any():
        block(
            "invalid_target_availability",
            "Target availability contains invalid values",
            int(invalid_availability.sum()),
        )
    premature_target_availability = (
        relevant & target_available.notna() & (target_available < timestamps + pd.Timedelta(days=1))
    )
    if premature_target_availability.any():
        block(
            "target_availability_precedes_event",
            "Target known-at timestamps cannot precede their event timestamps",
            int(premature_target_availability.sum()),
        )
    observed = observation_availability(frame, mapping.observation_availability_column)
    invalid_observation_availability = relevant & observed.isna()
    if invalid_observation_availability.any():
        block(
            "invalid_observation_availability",
            "Product/observation availability must be an explicit boolean value",
            int(invalid_observation_availability.sum()),
        )
    grid = expected_grid(
        entities,
        dates,
        observed,
        spec.origins,
        spec.horizon_days,
        spec.scoring_availability_policy,
    )
    grid_by_key = {(cell.role, cell.origin, cell.horizon, cell.entity): cell for cell in grid}
    expected_rows = 0
    score_rows = 0
    feature_origin_failures: dict[str, int] = {}
    required_unavailable = 0
    required_unavailable_examples: list[str] = []
    entity_values = sorted(str(value) for value in entities.dropna().unique())
    for origin in spec.origins:
        origin_ts = pd.Timestamp(origin.date, tz="UTC")
        for entity in entity_values:
            entity_mask = entities == entity
            for feature in selected_features:
                if feature.role == FeatureRole.KNOWN_FUTURE:
                    continue
                required_date = pd.Timestamp.fromordinal(
                    origin.date.toordinal() - (1 if feature.role == FeatureRole.PAST_ONLY else 0)
                )
                required = entity_mask & (dates == required_date)
                available = feature_availability[feature.name]
                if int(required.sum()) != 1 or not (available[required] <= origin_ts).all():
                    feature_origin_failures[feature.name] = (
                        feature_origin_failures.get(feature.name, 0) + 1
                    )
            for horizon in range(1, spec.horizon_days + 1):
                forecast_date = pd.Timestamp.fromordinal(origin.date.toordinal() + horizon - 1)
                row_mask = entity_mask & (dates == forecast_date)
                cell = grid_by_key[(origin.role, origin.date, horizon, entity)]
                if not cell.included:
                    continue
                row_observed = cell.observation_available is True
                expected_rows += 1
                if spec.scoring_availability_policy == "require_available" and not row_observed:
                    required_unavailable += 1
                    required_unavailable_examples.append(
                        f"{entity}@{forecast_date.date().isoformat()}"
                    )
                if (
                    int(row_mask.sum()) == 1
                    and row_observed
                    and target[row_mask].notna().all()
                    and (target_available[row_mask] <= spec.evaluation_as_of).all()
                ):
                    score_rows += 1
                for feature in selected_features:
                    if feature.role != FeatureRole.KNOWN_FUTURE:
                        continue
                    available = feature_availability[feature.name]
                    if int(row_mask.sum()) != 1 or not (available[row_mask] <= origin_ts).all():
                        feature_origin_failures[feature.name] = (
                            feature_origin_failures.get(feature.name, 0) + 1
                        )
    if required_unavailable:
        block(
            "required_observation_unavailable",
            "One or more required scoring rows are product-unavailable",
            required_unavailable,
            required_unavailable_examples,
        )
    for feature_name, count in sorted(feature_origin_failures.items()):
        block(
            "feature_unavailable_at_origin",
            f"Selected feature {feature_name} is not available when required at origin",
            count,
        )
    from vonavy_agent.backtest import _prepare_data, model_feasibility

    model_failures: list[str] = []
    entity_count = len(entity_values)
    if not reasons:
        prepared = _prepare_data(engine, registry, spec, mapping)
        for origin in spec.origins:
            for model in spec.models:
                for horizon in range(1, spec.horizon_days + 1):
                    training_rows, prediction_entities = model_feasibility(
                        prepared, origin.date, horizon, model
                    )
                    if prediction_entities != entity_count or (
                        model.kind == "ridge_direct" and training_rows < 2
                    ):
                        model_failures.append(
                            f"{model.kind}@{origin.date.isoformat()}+{horizon}:"
                            f"train={training_rows},predict={prediction_entities}/{entity_count}"
                        )
    if model_failures:
        block(
            "model_infeasible",
            "Configured models lack eligible training or prediction rows",
            len(model_failures),
            model_failures,
            remediation="Increase eligible history or revise model windows/features.",
        )
    coverage = score_rows / expected_rows if expected_rows else 0.0
    if coverage < spec.minimum_coverage:
        block(
            "coverage_below_minimum",
            "Availability-aware score coverage is below the explicit minimum",
            expected_rows - score_rows,
            remediation="Adjust evaluation_as_of, coverage threshold, origins, or the dataset version.",
        )
    estimated_cells = profile["rows"] * max(len(spec.models), 1) * max(spec.horizon_days, 1)
    estimated_memory_mb = estimated_cells * 8 * 4 / (1024 * 1024)
    if estimated_memory_mb > spec.resources.memory_mb:
        block(
            "memory_estimate",
            "Conservative experiment memory estimate exceeds the limit",
            int(estimated_memory_mb),
        )
    if mapping.target_availability.kind == AvailabilityKind.EVENT_TIME:
        warnings.append(
            GateReason(
                code="event_time_availability",
                message="Daily target availability is explicitly assumed at the next-day origin",
            )
        )
    evidence = {
        "dataset_version_id": spec.dataset_version_id,
        "mapping_hash": mapping_row.mapping_hash,
        "profile_hash": profile_row.profile_hash,
        "spec_hash": spec_row.spec_hash,
        "rows": profile["rows"],
        "entities": profile["entities"],
        "expected_score_rows": expected_rows,
        "eligible_score_rows": score_rows,
        "coverage": coverage,
        "estimated_memory_mb": round(estimated_memory_mb, 2),
    }
    status = "blocked" if reasons else "passed"
    unsigned = GateReport(
        status=status,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        evidence=evidence,
    )
    token = (
        canonical_hash(
            {
                "spec_hash": spec_row.spec_hash,
                "profile_hash": profile_row.profile_hash,
                "gate": unsigned.model_dump(mode="json"),
            }
        )
        if status == "passed"
        else None
    )
    report = unsigned.model_copy(update={"confirmation_token": token})
    return GateComputation(
        owner_id=owner_id,
        spec_id=spec_row.id,
        spec_hash=spec_row.spec_hash,
        profile_hash=profile_row.profile_hash,
        status=status,
        canonical_json=canonical_json(report.model_dump(mode="json")),
        confirmation_token=token,
    )


def publish_gate(
    session: Session,
    computation: GateComputation,
    row_id: str | None = None,
) -> GateResultRow:
    row = GateResultRow(
        owner_id=computation.owner_id,
        spec_id=computation.spec_id,
        spec_hash=computation.spec_hash,
        profile_hash=computation.profile_hash,
        status=computation.status,
        canonical_json=computation.canonical_json,
        confirmation_token=computation.confirmation_token,
    )
    if row_id is not None:
        row.id = row_id
    session.add(row)
    session.flush()
    return row


def run_gate(
    engine: Engine,
    registry: DatasetRegistry,
    spec_row_id: str,
    before_publish: Callable[[], None] | None = None,
    owner_id: str = LOCAL_OWNER_ID,
) -> GateResultRow:
    computation = compute_gate(engine, registry, spec_row_id, owner_id)
    if before_publish is not None:
        before_publish()
    with session_scope(engine) as session:
        row = publish_gate(session, computation)
        row_id = row.id
    with Session(engine) as session:
        return session.get_one(GateResultRow, row_id)
