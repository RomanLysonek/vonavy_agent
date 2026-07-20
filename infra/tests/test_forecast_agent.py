from __future__ import annotations

import importlib.util
import json
from io import BytesIO
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

AGENT_PATH = Path(__file__).parents[1] / "lambda/forecast_control_plane/agent.py"
SPEC = importlib.util.spec_from_file_location("forecast_control_plane_agent", AGENT_PATH)
assert SPEC is not None and SPEC.loader is not None
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class MissingParameterSsm:
    def get_parameter(self, **_: Any) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "missing"}},
            "GetParameter",
        )


class KeySsm:
    def get_parameter(self, **_: Any) -> dict[str, Any]:
        return {"Parameter": {"Value": "test-key"}}


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)


def _validation_result() -> dict[str, Any]:
    return {
        "status": "succeeded",
        "dataset_id": "00000000-0000-0000-0000-000000000001",
        "input_identity": {"version_id": "version-1"},
        "row_count": 154,
        "column_count": 8,
        "columns": [
            {
                "name": "DateKey",
                "logical_type": "date",
                "null_ratio": 0.0,
                "non_null_count": 154,
                "temporal": {"minimum": "2025-01-01", "maximum": "2025-03-24"},
            },
            {
                "name": "ProductId",
                "logical_type": "numeric",
                "null_ratio": 0.0,
                "non_null_count": 154,
                "numeric": {"minimum": 1, "maximum": 2},
            },
            {
                "name": "Quantity",
                "logical_type": "numeric",
                "null_ratio": 14 / 154,
                "non_null_count": 140,
                "numeric": {"minimum": 2, "maximum": 30, "mean": 12},
            },
            {
                "name": "ProductAvailable",
                "logical_type": "boolean",
                "null_ratio": 0.0,
                "non_null_count": 154,
                "boolean": {"true_count": 148, "false_count": 6},
            },
            {
                "name": "Discount",
                "logical_type": "numeric",
                "null_ratio": 0.0,
                "non_null_count": 154,
                "numeric": {"minimum": 0, "maximum": 20},
            },
            {
                "name": "Campaign",
                "logical_type": "string",
                "null_ratio": 0.0,
                "non_null_count": 154,
                "string": {
                    "distinct_count": 3,
                    "top_values": [{"value": "SECRET_RAW_VALUE", "count": 100}],
                },
            },
            {
                "name": "Category",
                "logical_type": "string",
                "null_ratio": 0.0,
                "non_null_count": 154,
                "string": {"distinct_count": 2},
            },
            {
                "name": "FuturePrediction",
                "logical_type": "numeric",
                "null_ratio": 0.0,
                "non_null_count": 154,
            },
        ],
    }


def _openai_mapping() -> dict[str, Any]:
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
        "confidence": 0.94,
        "summary": "Daily product demand with known future promotion context.",
        "preprocessingSteps": [
            "Parse DateKey as a daily calendar.",
            "Keep promotion values at their forecast dates.",
        ],
        "warnings": ["Confirm that future campaign values are truly known."],
    }


def _opener_for(mapping: dict[str, Any], captured: list[dict[str, Any]] | None = None):
    def opener(request: Any, timeout: int) -> FakeResponse:
        assert timeout == agent.OPENAI_TIMEOUT_SECONDS
        body = json.loads(request.data)
        if captured is not None:
            captured.append(body)
        return FakeResponse(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(mapping)}],
                    }
                ],
            }
        )

    return opener


def _reset_key_cache() -> None:
    agent._API_KEY = None
    agent._SSM = None


def test_provider_payload_never_contains_raw_string_values() -> None:
    profiles = agent._column_profiles(_validation_result())
    payload = agent._provider_payload(profiles, "forecast demand")
    serialized = json.dumps(payload)
    assert "SECRET_RAW_VALUE" not in serialized
    assert "top_values" not in serialized
    assert payload["store"] is False
    assert payload["text"]["format"]["type"] == "json_schema"
    assert "tools" not in payload


def test_deterministic_fallback_produces_confirmable_xgboost_plan() -> None:
    _reset_key_cache()
    plan = agent.build_forecast_agent_plan(
        dataset_id="00000000-0000-0000-0000-000000000001",
        dataset_version_id="version-1",
        validation_result=_validation_result(),
        objective="Predict the next week.",
        ssm_client=MissingParameterSsm(),
    )
    assert plan["agentMode"] == "deterministic-fallback"
    assert plan["requiresConfirmation"] is True
    assert plan["mapping"] == {
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
    assert plan["trainingEnd"] == "2025-03-17"
    assert plan["forecastStart"] == "2025-03-18"
    assert plan["forecastEnd"] == "2025-03-24"
    assert plan["execution"]["adapterId"] == "xgboost-direct-v1"
    assert plan["privacy"]["rawRowsSentToProvider"] is False


def test_openai_plan_is_structured_validated_and_version_bound() -> None:
    _reset_key_cache()
    captured: list[dict[str, Any]] = []
    plan = agent.build_forecast_agent_plan(
        dataset_id="00000000-0000-0000-0000-000000000001",
        dataset_version_id="version-1",
        validation_result=_validation_result(),
        objective="Focus on promotion-aware demand.",
        ssm_client=KeySsm(),
        opener=_opener_for(_openai_mapping(), captured),
    )
    assert plan["agentMode"] == "openai"
    assert plan["model"] == "gpt-5-mini-2025-08-07"
    assert plan["mapping"]["knownFutureNumeric"] == ["Discount"]
    assert plan["mapping"]["excluded"] == ["FuturePrediction"]
    assert len(plan["planId"]) == 64
    assert captured[0]["model"] == "gpt-5-mini-2025-08-07"
    assert captured[0]["store"] is False


def test_provider_cannot_reference_unknown_or_leakage_column() -> None:
    raw = _openai_mapping()
    raw["knownFutureNumeric"] = ["NotAColumn"]
    try:
        agent._validate_provider_mapping(raw, agent._column_profiles(_validation_result()))
    except agent.AgentPlanError as exc:
        assert exc.code == "agent_provider_invalid"
        assert exc.status == 502
    else:
        raise AssertionError("unknown provider column was accepted")

    raw = _openai_mapping()
    raw["knownFutureNumeric"] = ["FuturePrediction"]
    raw["excluded"] = ["Discount"]
    try:
        agent._validate_provider_mapping(raw, agent._column_profiles(_validation_result()))
    except agent.AgentPlanError as exc:
        assert exc.code == "agent_provider_invalid"
    else:
        raise AssertionError("outcome-derived feature was accepted")


def test_validation_result_is_bound_to_exact_dataset_version() -> None:
    _reset_key_cache()
    try:
        agent.build_forecast_agent_plan(
            dataset_id="00000000-0000-0000-0000-000000000001",
            dataset_version_id="different-version",
            validation_result=_validation_result(),
            ssm_client=MissingParameterSsm(),
        )
    except agent.AgentPlanError as exc:
        assert exc.code == "validation_result_mismatch"
        assert exc.status == 409
    else:
        raise AssertionError("mismatched immutable dataset version was accepted")


def test_plan_id_changes_with_objective_or_mapping() -> None:
    mapping = _openai_mapping()
    base = agent._plan_id("dataset", "version", mapping, "2025-03-17", "one", "openai")
    assert base != agent._plan_id("dataset", "version", mapping, "2025-03-17", "two", "openai")
    changed = dict(mapping)
    changed["targetColumn"] = "Other"
    assert base != agent._plan_id("dataset", "version", changed, "2025-03-17", "one", "openai")
