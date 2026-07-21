from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from vonavy_agent.forecasting.contracts import (
    BaselineSkillEvidence,
    EntityErrorEvidence,
    EvaluationSafetyEvidence,
    FeatureShiftEvidence,
    ForecastEvaluationEvidence,
)

MAX_ENTITY_DIAGNOSTICS = 10
MAX_FEATURE_SHIFTS = 10
_TIE_TOLERANCE = 0.01


def _array(values: Iterable[object]) -> np.ndarray:
    return np.asarray(list(values), dtype=float)


def _wape(actual: np.ndarray, prediction: np.ndarray) -> float | None:
    valid = np.isfinite(actual) & np.isfinite(prediction)
    if not valid.any():
        return None
    denominator = float(np.abs(actual[valid]).sum())
    if denominator <= 0:
        return None
    return float(np.abs(actual[valid] - prediction[valid]).sum() / denominator)


def _relative_improvement(model_value: float | None, baseline_value: float | None) -> float | None:
    if model_value is None or baseline_value is None or baseline_value <= 0:
        return None
    return float((baseline_value - model_value) / baseline_value)


def _baseline_skill(
    actual: np.ndarray,
    prediction: np.ndarray,
    baseline: np.ndarray,
) -> BaselineSkillEvidence:
    valid = np.isfinite(actual) & np.isfinite(prediction) & np.isfinite(baseline)
    common_rows = int(valid.sum())
    if not common_rows:
        return BaselineSkillEvidence(
            supported=False,
            common_rows=0,
            verdict="unavailable",
            reason="No common finite holdout rows for the model and seasonal baseline",
        )
    model_value = _wape(actual[valid], prediction[valid])
    baseline_value = _wape(actual[valid], baseline[valid])
    improvement = _relative_improvement(model_value, baseline_value)
    if model_value is None or baseline_value is None or improvement is None:
        return BaselineSkillEvidence(
            supported=False,
            common_rows=common_rows,
            model_value=model_value,
            baseline_value=baseline_value,
            verdict="unavailable",
            reason="The common holdout target has a zero absolute-demand denominator",
        )
    verdict: Literal["better", "tied", "worse"]
    if improvement > _TIE_TOLERANCE:
        verdict = "better"
    elif improvement < -_TIE_TOLERANCE:
        verdict = "worse"
    else:
        verdict = "tied"
    return BaselineSkillEvidence(
        supported=True,
        common_rows=common_rows,
        model_value=model_value,
        baseline_value=baseline_value,
        relative_improvement=improvement,
        verdict=verdict,
    )


def _entity_key(value: object) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()
    return f"entity-{digest[:16]}"


def _entity_diagnostics(
    actual: np.ndarray,
    prediction: np.ndarray,
    baseline: np.ndarray,
    entities: np.ndarray,
) -> tuple[EntityErrorEvidence, ...]:
    if not (len(actual) == len(prediction) == len(baseline) == len(entities)):
        return ()
    records: list[EntityErrorEvidence] = []
    for entity in sorted({str(value) for value in entities}):
        selected = np.asarray([str(value) == entity for value in entities], dtype=bool)
        valid = selected & np.isfinite(actual) & np.isfinite(prediction)
        if not valid.any():
            continue
        entity_actual = actual[valid]
        entity_prediction = prediction[valid]
        entity_baseline = baseline[valid]
        model_wape = _wape(entity_actual, entity_prediction)
        baseline_wape = _wape(entity_actual, entity_baseline)
        records.append(
            EntityErrorEvidence(
                entity_key=_entity_key(entity),
                rows=int(valid.sum()),
                model_wape=model_wape,
                baseline_wape=baseline_wape,
                relative_improvement=_relative_improvement(model_wape, baseline_wape),
                model_mae=float(np.mean(np.abs(entity_actual - entity_prediction))),
                bias=float(np.mean(entity_prediction - entity_actual)),
            )
        )
    records.sort(
        key=lambda item: (
            -(item.model_wape if item.model_wape is not None else -1.0),
            -item.model_mae,
            item.entity_key,
        )
    )
    return tuple(records[:MAX_ENTITY_DIAGNOSTICS])


def _severity(kind: str, value: float) -> Literal["info", "notice", "warning"]:
    if kind == "categorical":
        if value >= 0.10:
            return "warning"
        if value > 0:
            return "notice"
        return "info"
    if value >= 2.0:
        return "warning"
    if value >= 1.0:
        return "notice"
    return "info"


def _feature_evidence(
    train: pd.DataFrame,
    fresh: pd.DataFrame,
    feature_columns: Iterable[str],
    categorical_columns: set[str],
    entity_column: str,
) -> tuple[tuple[FeatureShiftEvidence, ...], int, int, int, list[str]]:
    shifts: list[FeatureShiftEvidence] = []
    evaluated_features = 0
    extrapolated_values = 0
    evaluated_values = 0
    unavailable: list[str] = []
    for column in sorted(set(feature_columns)):
        if column == entity_column or column not in train.columns or column not in fresh.columns:
            continue
        if column in categorical_columns:
            reference = train[column].astype("string").dropna()
            current = fresh[column].astype("string").dropna()
            if reference.empty or current.empty:
                unavailable.append(f"feature_shift:{column}")
                continue
            reference_levels = set(reference.tolist())
            unseen = ~current.isin(reference_levels)
            count = int(unseen.sum())
            value = float(unseen.mean())
            shifts.append(
                FeatureShiftEvidence(
                    feature=column,
                    kind="categorical",
                    statistic="unseen_rate",
                    value=value,
                    reference_count=len(reference),
                    fresh_count=len(current),
                    extrapolated_count=count,
                    severity=_severity("categorical", value),
                )
            )
            evaluated_features += 1
            extrapolated_values += count
            evaluated_values += len(current)
            continue

        reference_values = pd.to_numeric(train[column], errors="coerce").to_numpy(dtype=float)
        current_values = pd.to_numeric(fresh[column], errors="coerce").to_numpy(dtype=float)
        reference_values = reference_values[np.isfinite(reference_values)]
        current_values = current_values[np.isfinite(current_values)]
        if not len(reference_values) or not len(current_values):
            unavailable.append(f"feature_shift:{column}")
            continue
        mean = float(reference_values.mean())
        scale = float(reference_values.std(ddof=0))
        if not np.isfinite(scale) or scale < 1e-9:
            scale = max(abs(mean) * 0.01, 1.0)
        value = float(abs(float(current_values.mean()) - mean) / scale)
        minimum = float(reference_values.min())
        maximum = float(reference_values.max())
        outside = (current_values < minimum) | (current_values > maximum)
        count = int(outside.sum())
        shifts.append(
            FeatureShiftEvidence(
                feature=column,
                kind="numeric",
                statistic="standardized_mean_shift",
                value=value,
                reference_count=len(reference_values),
                fresh_count=len(current_values),
                extrapolated_count=count,
                severity=_severity("numeric", value),
            )
        )
        evaluated_features += 1
        extrapolated_values += count
        evaluated_values += len(current_values)

    severity_rank = {"warning": 2, "notice": 1, "info": 0}
    shifts.sort(key=lambda item: (-severity_rank[item.severity], -item.value, item.feature))
    return (
        tuple(shifts[:MAX_FEATURE_SHIFTS]),
        evaluated_features,
        extrapolated_values,
        evaluated_values,
        unavailable,
    )


def build_forecast_evaluation(
    *,
    holdout_origin: date | None,
    actual: Iterable[object],
    prediction: Iterable[object],
    baseline: Iterable[object],
    entities: Iterable[object],
    train_features: pd.DataFrame,
    fresh_features: pd.DataFrame,
    feature_columns: Iterable[str],
    categorical_columns: Iterable[str],
    entity_column: str = "entity",
    evaluated_entity_count_override: int | None = None,
    cold_start_entity_count_override: int | None = None,
) -> ForecastEvaluationEvidence:
    actual_array = _array(actual)
    prediction_array = _array(prediction)
    baseline_array = _array(baseline)
    entity_array = np.asarray(list(entities), dtype=object)
    unavailable: list[str] = ["multi_origin_oof"]
    if not (len(actual_array) == len(prediction_array) == len(baseline_array)):
        actual_array = np.asarray([], dtype=float)
        prediction_array = np.asarray([], dtype=float)
        baseline_array = np.asarray([], dtype=float)
        entity_array = np.asarray([], dtype=object)
        unavailable.append("holdout_array_alignment")

    skill = _baseline_skill(actual_array, prediction_array, baseline_array)
    if not skill.supported:
        unavailable.append("model_vs_baseline_skill")
    worst = _entity_diagnostics(
        actual_array,
        prediction_array,
        baseline_array,
        entity_array,
    )
    if not worst:
        unavailable.append("per_entity_holdout_diagnostics")

    train_entities = (
        {str(value) for value in train_features[entity_column].dropna().tolist()}
        if entity_column in train_features.columns
        else set()
    )
    fresh_entities = (
        {str(value) for value in fresh_features[entity_column].dropna().tolist()}
        if entity_column in fresh_features.columns
        else set()
    )
    evaluated_entity_count = (
        evaluated_entity_count_override
        if evaluated_entity_count_override is not None
        else len(fresh_entities)
    )
    cold_start_entity_count = (
        cold_start_entity_count_override
        if cold_start_entity_count_override is not None
        else len(fresh_entities - train_entities)
    )
    cold_start_entity_count = min(max(cold_start_entity_count, 0), max(evaluated_entity_count, 0))
    cold_start_rate = (
        float(cold_start_entity_count / evaluated_entity_count) if evaluated_entity_count else 0.0
    )

    shifts, feature_count, extrapolated, evaluated, feature_unavailable = _feature_evidence(
        train_features,
        fresh_features,
        feature_columns,
        set(categorical_columns),
        entity_column,
    )
    unavailable.extend(feature_unavailable[: max(0, 12 - len(unavailable))])
    if not feature_count:
        unavailable.append("train_fresh_feature_evidence")
    extrapolation_rate = float(extrapolated / evaluated) if evaluated else 0.0

    return ForecastEvaluationEvidence(
        holdout_origin=holdout_origin,
        baseline_skill=skill,
        worst_entities=worst,
        evaluated_entity_count=evaluated_entity_count,
        cold_start_entity_count=cold_start_entity_count,
        cold_start_rate=cold_start_rate,
        evaluated_feature_count=feature_count,
        extrapolated_value_count=extrapolated,
        evaluated_value_count=evaluated,
        feature_extrapolation_rate=extrapolation_rate,
        feature_shifts=shifts,
        unavailable=tuple(dict.fromkeys(unavailable))[:12],
        safety=EvaluationSafetyEvidence(),
    )
