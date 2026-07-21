from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from vonavy_agent.forecasting.contracts import ForecastEvaluationEvidence
from vonavy_agent.forecasting.evaluation import build_forecast_evaluation


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    train = pd.DataFrame(
        {
            "entity": ["A", "A", "B", "B"],
            "numeric": [0.0, 1.0, 2.0, 3.0],
            "category": ["old", "old", "stable", "stable"],
            "horizon": [1, 2, 1, 2],
        }
    )
    fresh = pd.DataFrame(
        {
            "entity": ["A", "C"],
            "numeric": [4.0, 7.0],
            "category": ["old", "new"],
            "horizon": [1, 2],
        }
    )
    return train, fresh


def test_evaluation_is_deterministic_and_contains_no_raw_entity_values() -> None:
    train, fresh = _frames()
    arguments = {
        "holdout_origin": pd.Timestamp("2026-01-04").date(),
        "actual": [10.0, 20.0, 5.0, 10.0],
        "prediction": [11.0, 18.0, 5.0, 9.0],
        "baseline": [15.0, 25.0, 8.0, 12.0],
        "entities": ["A", "A", "B", "B"],
        "train_features": train,
        "fresh_features": fresh,
        "feature_columns": ("entity", "numeric", "category", "horizon"),
        "categorical_columns": ("entity", "category"),
    }
    first = build_forecast_evaluation(**arguments)
    second = build_forecast_evaluation(**arguments)
    assert first == second
    assert first.schema_version == "forecast-evaluation/v1"
    assert first.baseline_skill.verdict == "better"
    assert first.baseline_skill.relative_improvement is not None
    assert first.baseline_skill.relative_improvement > 0
    serialized = json.dumps(first.model_dump(mode="json"), sort_keys=True)
    assert '"A"' not in serialized
    assert '"B"' not in serialized
    assert all(item.entity_key.startswith("entity-") for item in first.worst_entities)
    assert first.safety.raw_entity_values_exported is False
    assert "multi_origin_oof" in first.unavailable


def test_evaluation_measures_cold_start_extrapolation_and_shift() -> None:
    train, fresh = _frames()
    result = build_forecast_evaluation(
        holdout_origin=pd.Timestamp("2026-01-04").date(),
        actual=[10.0, 20.0],
        prediction=[12.0, 18.0],
        baseline=[11.0, 19.0],
        entities=["A", "B"],
        train_features=train,
        fresh_features=fresh,
        feature_columns=("entity", "numeric", "category", "horizon"),
        categorical_columns=("entity", "category"),
    )
    assert result.evaluated_entity_count == 2
    assert result.cold_start_entity_count == 1
    assert result.cold_start_rate == 0.5
    assert result.feature_extrapolation_rate > 0
    shifts = {item.feature: item for item in result.feature_shifts}
    assert shifts["numeric"].extrapolated_count == 2
    assert shifts["category"].statistic == "unseen_rate"
    assert shifts["category"].value == 0.5


def test_zero_denominator_discloses_unavailable_skill() -> None:
    train, fresh = _frames()
    result = build_forecast_evaluation(
        holdout_origin=None,
        actual=np.zeros(2),
        prediction=np.zeros(2),
        baseline=np.zeros(2),
        entities=["A", "B"],
        train_features=train,
        fresh_features=fresh,
        feature_columns=(),
        categorical_columns=(),
    )
    assert result.baseline_skill.supported is False
    assert result.baseline_skill.verdict == "unavailable"
    assert "model_vs_baseline_skill" in result.unavailable


def test_contract_rejects_nonfinite_evidence() -> None:
    train, fresh = _frames()
    result = build_forecast_evaluation(
        holdout_origin=None,
        actual=[1.0],
        prediction=[1.0],
        baseline=[2.0],
        entities=["A"],
        train_features=train,
        fresh_features=fresh,
        feature_columns=(),
        categorical_columns=(),
    )
    payload = result.model_dump(mode="python")
    payload["feature_extrapolation_rate"] = float("nan")
    with pytest.raises(ValidationError):
        ForecastEvaluationEvidence.model_validate(payload)
