from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

MODULE_ROOT = Path(__file__).parents[1] / "lambda" / "forecast_control_plane"


def _load() -> Any:
    agent = types.ModuleType("agent")

    class AgentPlanError(Exception):
        def __init__(
            self,
            code: str,
            message: str,
            status: int,
            detail: dict[str, Any] | None = None,
        ) -> None:
            self.code = code
            self.message = message
            self.status = status
            self.detail = detail or {}

    def column_profiles(result: dict[str, Any]) -> list[dict[str, Any]]:
        return result["profiles"]

    def validate_mapping(raw: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any]:
        del profiles
        return raw

    def training_end(
        mapping: dict[str, Any], profiles: list[dict[str, Any]]
    ) -> tuple[str, list[str]]:
        del mapping, profiles
        return "2026-01-11", []

    agent.AgentPlanError = AgentPlanError
    agent.BEDROCK_MODEL_ID = "eu.anthropic.claude-opus-4-6-v1"
    agent.BEDROCK_REGION = "eu-central-1"
    agent._column_profiles = column_profiles
    agent._validate_provider_mapping = validate_mapping
    agent._training_end = training_end
    sys.modules["agent"] = agent

    module_name = "orchestrator_phase4c"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_ROOT / "orchestrator.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _profiles() -> list[dict[str, Any]]:
    rows = 154
    return [
        {
            "name": "DateKey",
            "logicalType": "date",
            "nonNullCount": rows,
            "nullRatio": 0.0,
            "temporal": {"minimum": "2025-11-03", "maximum": "2026-01-18"},
        },
        {
            "name": "ProductId",
            "logicalType": "numeric",
            "nonNullCount": rows,
            "nullRatio": 0.0,
        },
        {
            "name": "Quantity",
            "logicalType": "numeric",
            "nonNullCount": rows - 14,
            "nullRatio": 14 / rows,
            "numeric": {
                "zero_count": 56,
                "negative_count": 0,
                "non_finite_count": 0,
            },
        },
        {
            "name": "ProductAvailable",
            "logicalType": "boolean",
            "nonNullCount": rows,
            "nullRatio": 0.0,
        },
        {
            "name": "Discount",
            "logicalType": "numeric",
            "nonNullCount": rows - 2,
            "nullRatio": 2 / rows,
            "sampleValues": ["SECRET_DISCOUNT_VALUE"],
        },
        {
            "name": "Campaign",
            "logicalType": "string",
            "nonNullCount": rows,
            "nullRatio": 0.0,
            "sampleValues": ["SECRET_CAMPAIGN_VALUE"],
        },
        {
            "name": "Category",
            "logicalType": "string",
            "nonNullCount": rows,
            "nullRatio": 0.0,
        },
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


def test_compiler_returns_fixed_deterministic_catalogue() -> None:
    module = _load()
    first = module._compile_preprocessing_plan(_profiles(), _mapping(), "xgboost-direct-v1")
    second = module._compile_preprocessing_plan(_profiles(), _mapping(), "xgboost-direct-v1")

    assert first == second
    assert first["schemaVersion"] == "forecast-preprocessing-plan/v1"
    assert first["catalogVersion"] == "forecast-preprocessing-catalog/v1"
    assert first["digest"]["algorithm"] == "sha256"
    assert len(first["digest"]["value"]) == 64
    assert len(first["operations"]) == 11
    assert [item["order"] for item in first["operations"]] == list(range(1, 12))
    assert first["safety"] == {
        "deterministic": True,
        "fixedOperationCatalogue": True,
        "generatedCode": False,
        "arbitraryTransforms": False,
        "rawRowsRequiredByAgent": False,
        "rawStringValuesRequiredByAgent": False,
        "mappingValidatedServerSide": True,
        "workerRemainsAuthoritative": True,
    }
    assert first["requiresConfirmation"] is True
    assert first["executesAutomatically"] is False
    assert first["review"]["policyVersion"] == "forecast-preprocessing-review/v1"
    assert first["findings"]


def test_plan_excludes_profile_samples_and_preserves_leakage_guard() -> None:
    module = _load()
    plan = module._compile_preprocessing_plan(_profiles(), _mapping(), "neuralnet-direct-v1")
    encoded = json.dumps(plan, sort_keys=True)

    assert "SECRET_DISCOUNT_VALUE" not in encoded
    assert "SECRET_CAMPAIGN_VALUE" not in encoded
    leakage = next(
        item for item in plan["operations"] if item["operationId"] == "leakage.exclusion_guard"
    )
    assert leakage["columns"] == ["FuturePrediction"]
    assert leakage["evidence"]["targetExcludedFromFeatures"] is True
    assert plan["safety"]["generatedCode"] is False


def test_structured_findings_and_review_are_evidence_backed() -> None:
    module = _load()
    plan = module._compile_preprocessing_plan(_profiles(), _mapping(), "xgboost-direct-v1")

    diagnostics = plan["diagnostics"]
    assert diagnostics["evidenceBasis"] == "validated-aggregate-metadata"
    assert diagnostics["estimatedRows"] == 154
    assert diagnostics["historyDays"] == 77
    assert diagnostics["target"]["zeroCount"] == 56
    assert diagnostics["target"]["zeroFraction"] == 0.4
    assert "distribution drift" in diagnostics["notAvailableFromAggregateProfile"]

    findings = {item["findingId"]: item for item in plan["findings"]}
    assert findings["target.intermittency"]["severity"] == "warning"
    assert findings["target.intermittency"]["confidence"] == "measured"
    assert findings["leakage.exclusion_policy"]["confidence"] == "policy"
    assert findings["adapter.preparation_boundary"]["evidence"]["adapterId"] == (
        "xgboost-direct-v1"
    )

    review = plan["review"]
    assert review["policyVersion"] == "forecast-preprocessing-review/v1"
    assert review["status"] == "needs_attention"
    assert review["maxSeverity"] == "warning"
    assert review["confidence"] == "mixed"
    assert "target.intermittency" in review["attentionFindingIds"]


def test_non_finite_target_is_rejected_before_plan_creation() -> None:
    module = _load()
    profiles = _profiles()
    profiles[2]["numeric"]["non_finite_count"] = 2

    with pytest.raises(module.PreprocessingPlanError) as error:
        module._compile_preprocessing_plan(profiles, _mapping(), "xgboost-direct-v1")

    assert error.value.code == "preprocessing_target_non_finite"
    assert error.value.detail["nonFiniteCount"] == 2


def test_adapter_boundary_is_explicit_for_every_supported_adapter() -> None:
    module = _load()
    expected_fit = {
        "xgboost-direct-v1": True,
        "neuralnet-direct-v1": True,
        "chronos2-zero-shot-v1": False,
    }

    for adapter_id, task_specific_fit in expected_fit.items():
        plan = module._compile_preprocessing_plan(_profiles(), _mapping(), adapter_id)
        boundary = next(
            item
            for item in plan["operations"]
            if item["operationId"] == "adapter.encoding_boundary"
        )
        assert boundary["evidence"]["taskSpecificFit"] is task_specific_fit
        assert "existing" in boundary["action"]
        assert plan["adapterId"] == adapter_id


def test_target_future_nulls_are_not_generically_imputed() -> None:
    module = _load()
    plan = module._compile_preprocessing_plan(_profiles(), _mapping(), "chronos2-zero-shot-v1")
    target = next(
        item for item in plan["operations"] if item["operationId"] == "target.observation_boundary"
    )

    assert target["evidence"]["nullRatio"] > 0
    assert "No target imputation" in target["policy"]
    assert not plan["blockers"]


def test_missing_selected_feature_is_warning_without_authorized_fill() -> None:
    module = _load()
    plan = module._compile_preprocessing_plan(_profiles(), _mapping(), "xgboost-direct-v1")
    guard = next(
        item for item in plan["operations"] if item["operationId"] == "features.missingness_guard"
    )

    assert guard["status"] == "warning"
    assert guard["columns"] == ["Discount"]
    assert any("no generic" in warning.casefold() for warning in plan["warnings"])
    assert "forward-fill" in guard["policy"]


def test_role_collision_is_rejected() -> None:
    module = _load()
    mapping = _mapping()
    mapping["knownFutureNumeric"] = ["Quantity"]

    with pytest.raises(module.PreprocessingPlanError) as error:
        module._compile_preprocessing_plan(_profiles(), mapping, "xgboost-direct-v1")

    assert error.value.code == "preprocessing_role_collision"


def test_required_timestamp_missingness_is_rejected() -> None:
    module = _load()
    profiles = _profiles()
    profiles[0]["nullRatio"] = 0.1

    with pytest.raises(module.PreprocessingPlanError) as error:
        module._compile_preprocessing_plan(profiles, _mapping(), "xgboost-direct-v1")

    assert error.value.code == "preprocessing_required_values_missing"


def test_draft_binds_server_compiled_preprocessing_plan() -> None:
    module = _load()
    plan = module._draft_plan(
        {
            "adapterId": "xgboost-direct-v1",
            "mapping": _mapping(),
            "summary": "Use a safe deterministic preprocessing plan.",
            "warnings": [],
        },
        profiles=_profiles(),
        dataset_id="dataset",
        dataset_version_id="version",
        selection_objective="quick and cheap",
    )

    preprocessing = plan["preprocessingPlan"]
    assert preprocessing["adapterId"] == plan["adapterId"]
    assert preprocessing["mapping"] == plan["mapping"]
    assert preprocessing["digest"]["value"]
    assert preprocessing["requiresConfirmation"] is True
    assert plan["requiresConfirmation"] is True
    assert plan["executesAutomatically"] is False


def test_compile_tool_is_bounded_and_does_not_create_a_draft() -> None:
    module = _load()
    result, draft = module._execute_tool(
        "compile_preprocessing_plan",
        {"adapterId": "xgboost-direct-v1", "mapping": _mapping()},
        profiles=_profiles(),
        dataset_id="dataset",
        dataset_version_id="version",
    )

    assert draft is None
    assert result["preprocessingPlan"]["catalogVersion"] == ("forecast-preprocessing-catalog/v1")
    assert result["preprocessingPlan"]["executesAutomatically"] is False


def test_tool_schema_and_round_limit_cover_preprocessing_before_draft() -> None:
    module = _load()
    names = [item["toolSpec"]["name"] for item in module._tool_schema()["tools"]]

    assert names == [
        "inspect_dataset",
        "compare_models",
        "compile_preprocessing_plan",
        "draft_forecast_plan",
    ]
    assert module.MAX_TOOL_ROUNDS >= 5
    assert module.PREPROCESSING_CATALOG_VERSION == "forecast-preprocessing-catalog/v1"
