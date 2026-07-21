from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

MODULE_ROOT = Path(__file__).parents[1] / "lambda" / "forecast_control_plane"


def _load() -> Any:
    agent = types.ModuleType("agent")

    class AgentPlanError(Exception):
        def __init__(
            self, code: str, message: str, status: int, detail: dict[str, Any] | None = None
        ) -> None:
            self.code = code
            self.message = message
            self.status = status
            self.detail = detail or {}

    def column_profiles(result: dict[str, Any]) -> list[dict[str, Any]]:
        return result["profiles"]

    def validate_mapping(raw: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any]:
        return raw

    def training_end(
        mapping: dict[str, Any], profiles: list[dict[str, Any]]
    ) -> tuple[str, list[str]]:
        return "2026-01-11", []

    agent.AgentPlanError = AgentPlanError
    agent.BEDROCK_MODEL_ID = "eu.anthropic.claude-opus-4-6-v1"
    agent.BEDROCK_REGION = "eu-central-1"
    agent._column_profiles = column_profiles
    agent._validate_provider_mapping = validate_mapping
    agent._training_end = training_end
    sys.modules["agent"] = agent
    spec = importlib.util.spec_from_file_location(
        "orchestrator_phase4b", MODULE_ROOT / "orchestrator.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_phase4b"] = module
    spec.loader.exec_module(module)
    return module


def _profiles(rows: int = 154, history_days: int = 77) -> list[dict[str, Any]]:
    return [
        {
            "name": "DateKey",
            "logicalType": "date",
            "nonNullCount": rows,
            "nullRatio": 0.0,
            "temporal": {"minimum": "2025-11-03", "maximum": "2026-01-18"}
            if history_days < 365
            else {"minimum": "2024-01-01", "maximum": "2026-01-18"},
        },
        {"name": "ProductId", "logicalType": "numeric", "nonNullCount": rows, "nullRatio": 0.0},
        {
            "name": "Quantity",
            "logicalType": "numeric",
            "nonNullCount": rows - 14,
            "nullRatio": 14 / rows,
        },
        {
            "name": "ProductAvailable",
            "logicalType": "boolean",
            "nonNullCount": rows,
            "nullRatio": 0.0,
        },
        {"name": "Discount", "logicalType": "numeric", "nonNullCount": rows, "nullRatio": 0.0},
        {"name": "Campaign", "logicalType": "string", "nonNullCount": rows, "nullRatio": 0.0},
        {"name": "Category", "logicalType": "string", "nonNullCount": rows, "nullRatio": 0.0},
        {
            "name": "FuturePrediction",
            "logicalType": "numeric",
            "nonNullCount": rows,
            "nullRatio": 0.0,
        },
    ]


def _mapping() -> dict[str, Any]:
    return {
        "timestampColumn": "DateKey",
        "entityColumn": "ProductId",
        "targetColumn": "Quantity",
        "availabilityColumn": "ProductAvailable",
        "knownFutureNumeric": ["Discount"],
        "knownFutureCategorical": ["Campaign"],
        "staticNumeric": [],
        "staticCategorical": ["Category"],
        "excluded": ["FuturePrediction"],
    }


def test_fast_small_panel_recommends_xgboost() -> None:
    module = _load()
    result = module._model_recommendations(_profiles(), "Give me the quickest low cost retraining")
    assert result["policyVersion"] == "forecast-model-selection/v1"
    assert result["recommendedAdapterId"] == "xgboost-direct-v1"
    assert result["ranking"][0]["recommended"] is True
    assert result["ranking"][0]["runtimeEstimate"]["minimumMinutes"] >= 1
    assert result["ranking"][0]["costEstimate"]["currencyAmount"] is None
    assert result["requiresUserConfirmation"] is True


def test_uncertainty_objective_recommends_chronos() -> None:
    module = _load()
    result = module._model_recommendations(
        _profiles(), "I need uncertainty quantiles and intervals"
    )
    assert result["recommendedAdapterId"] == "chronos2-zero-shot-v1"
    assert "native forecast quantiles" in " ".join(result["ranking"][0]["reasons"])


def test_large_rich_panel_accuracy_objective_recommends_neuralnet() -> None:
    module = _load()
    result = module._model_recommendations(
        _profiles(rows=20_000, history_days=700),
        "Prioritize the best nonlinear fitted accuracy and retrain on this history",
    )
    assert result["recommendedAdapterId"] == "neuralnet-direct-v1"
    assert result["signals"]["estimatedRows"] >= 20_000
    assert result["signals"]["historyDays"] >= 365


def test_draft_binds_selection_evidence_and_marks_override() -> None:
    module = _load()
    plan = module._draft_plan(
        {
            "adapterId": "xgboost-direct-v1",
            "mapping": _mapping(),
            "summary": "Use the fast trained model.",
            "warnings": [],
        },
        profiles=_profiles(),
        dataset_id="dataset",
        dataset_version_id="version",
        selection_objective="I need uncertainty quantiles",
    )
    selection = plan["modelSelection"]
    assert selection["recommendedAdapterId"] == "chronos2-zero-shot-v1"
    assert selection["selectedAdapterId"] == "xgboost-direct-v1"
    assert selection["selectedRank"] > 1
    assert any(
        "differs from the deterministic top recommendation" in item for item in plan["warnings"]
    )
    assert plan["requiresConfirmation"] is True
    assert plan["executesAutomatically"] is False


def test_compare_models_returns_deterministic_selection() -> None:
    module = _load()
    result, plan = module._execute_tool(
        "compare_models",
        {"objective": "quick and cheap"},
        profiles=_profiles(),
        dataset_id="dataset",
        dataset_version_id="version",
    )
    assert plan is None
    assert set(result["adapters"]) == {
        "xgboost-direct-v1",
        "neuralnet-direct-v1",
        "chronos2-zero-shot-v1",
    }
    assert result["selection"]["recommendedAdapterId"] == "xgboost-direct-v1"
