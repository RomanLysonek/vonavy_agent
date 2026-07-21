from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any

os.environ.setdefault("DATA_BUCKET", "data-bucket")
os.environ.setdefault("METADATA_TABLE", "metadata-table")
os.environ.setdefault("FORECAST_JOB_QUEUE", "queue-arn")
os.environ.setdefault("FORECAST_JOB_DEFINITION", "job-definition-arn:1")
os.environ.setdefault("SOURCE_REVISION", "unknown")

agent = types.ModuleType("agent")
agent.AgentPlanError = RuntimeError
agent.build_forecast_agent_plan = lambda **kwargs: {}
sys.modules["agent"] = agent
orchestrator = types.ModuleType("orchestrator")
orchestrator.OrchestratorError = RuntimeError
orchestrator.run_agent_turn = lambda **kwargs: None
sys.modules["orchestrator"] = orchestrator

PATH = Path(__file__).parents[1] / "lambda/forecast_control_plane/handler.py"
SPEC = importlib.util.spec_from_file_location("phase4d_handler", PATH)
assert SPEC and SPEC.loader
handler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handler)


def _evaluation(*, verdict: str = "better", improvement: float = 0.2) -> dict[str, Any]:
    return {
        "schema_version": "forecast-evaluation/v1",
        "evidence_basis": "worker-holdout-and-aggregate-features",
        "holdout_origin": "2026-01-04",
        "baseline_skill": {
            "supported": True,
            "metric": "wape",
            "common_rows": 14,
            "model_value": 0.2,
            "baseline_value": 0.25,
            "relative_improvement": improvement,
            "verdict": verdict,
            "reason": None,
        },
        "worst_entities": [
            {
                "entity_key": "entity-0123456789abcdef",
                "rows": 7,
                "model_wape": 0.3,
                "baseline_wape": 0.4,
                "relative_improvement": 0.25,
                "model_mae": 2.0,
                "bias": -0.5,
            }
        ],
        "evaluated_entity_count": 2,
        "cold_start_entity_count": 0,
        "cold_start_rate": 0.0,
        "evaluated_feature_count": 2,
        "extrapolated_value_count": 0,
        "evaluated_value_count": 28,
        "feature_extrapolation_rate": 0.0,
        "feature_shifts": [],
        "unavailable": [],
        "safety": {
            "deterministic": True,
            "worker_computed": True,
            "raw_rows_exported": False,
            "raw_entity_values_exported": False,
            "automatic_experiment_execution": False,
        },
    }


def test_ready_review_uses_only_worker_evidence() -> None:
    payload = {"evaluation": _evaluation()}
    review = handler._forecast_result_review(payload)
    assert review == handler._forecast_result_review(payload)
    assert review["policyVersion"] == "forecast-result-review/v1"
    assert review["status"] == "ready"
    assert review["findings"][0]["findingId"] == "skill.model_vs_baseline"
    assert review["safety"] == {
        "deterministic": True,
        "workerEvidenceOnly": True,
        "rawRowsRead": False,
        "rawEntityValuesRead": False,
        "bedrockInvoked": False,
        "automaticRerun": False,
    }
    assert all(item["executesAutomatically"] is False for item in review["recommendations"])


def test_negative_skill_and_shift_produce_measured_recommendations() -> None:
    evidence = _evaluation(verdict="worse", improvement=-0.2)
    evidence["cold_start_entity_count"] = 1
    evidence["cold_start_rate"] = 0.5
    evidence["feature_extrapolation_rate"] = 0.25
    evidence["extrapolated_value_count"] = 7
    evidence["feature_shifts"] = [
        {
            "feature": "Discount",
            "kind": "numeric",
            "statistic": "standardized_mean_shift",
            "value": 2.5,
            "reference_count": 100,
            "fresh_count": 14,
            "extrapolated_count": 7,
            "severity": "warning",
        }
    ]
    review = handler._forecast_result_review({"evaluation": evidence})
    assert review["status"] == "needs_attention"
    ids = {item["recommendationId"] for item in review["recommendations"]}
    assert "experiment.compare_adapter" in ids
    assert "experiment.cold_start_strategy" in ids
    assert "experiment.extrapolation_backtest" in ids
    assert "experiment.recent_window" in ids


def test_raw_entity_identifier_is_rejected() -> None:
    evidence = _evaluation()
    evidence["worst_entities"][0]["entity_key"] = "Product-123"
    try:
        handler._forecast_result_review({"evaluation": evidence})
    except handler.ApiError as exc:
        assert exc.code == "forecast_result_invalid"
        assert exc.status == 502
    else:
        raise AssertionError("raw entity identifier was accepted")


def test_successful_terminal_result_requires_evaluation() -> None:
    try:
        handler._validated_result_evaluation({}, required=True)
    except handler.ApiError as exc:
        assert exc.code == "forecast_result_invalid"
        assert exc.status == 502
    else:
        raise AssertionError("successful result without evaluation evidence was accepted")


def test_inconsistent_or_nonfinite_evidence_is_rejected() -> None:
    evidence = _evaluation()
    evidence["feature_extrapolation_rate"] = float("nan")
    try:
        handler._forecast_result_review({"evaluation": evidence})
    except handler.ApiError as exc:
        assert exc.code == "forecast_result_invalid"
    else:
        raise AssertionError("non-finite evaluation evidence was accepted")


def test_historical_result_gets_bounded_insufficient_evidence_review() -> None:
    review = handler._forecast_result_review({})
    assert review["status"] == "insufficient_evidence"
    assert review["recommendations"][0]["executesAutomatically"] is False
