from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

AGENT_PATH = Path(__file__).parents[1] / "lambda/forecast_control_plane/agent.py"
SPEC = importlib.util.spec_from_file_location("forecast_control_plane_agent", AGENT_PATH)
assert SPEC is not None and SPEC.loader is not None
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class FakeBedrock:
    def __init__(
        self,
        mapping: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.mapping = mapping
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.mapping is not None
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "forecast-mapping-1",
                                "name": agent.FORECAST_MAPPING_TOOL_NAME,
                                "input": self.mapping,
                            }
                        }
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 300, "outputTokens": 180},
        }


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


def _bedrock_mapping() -> dict[str, Any]:
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


def test_provider_request_uses_forced_schema_bound_tool_without_raw_values() -> None:
    profiles = agent._column_profiles(_validation_result())
    request = agent._provider_request(profiles, "forecast demand")
    serialized = json.dumps(request)

    assert "SECRET_RAW_VALUE" not in serialized
    assert "top_values" not in serialized
    assert request["modelId"] == "eu.anthropic.claude-opus-4-6-v1"
    assert request["inferenceConfig"] == {"maxTokens": 1200, "temperature": 0.0}
    assert "outputConfig" not in request
    assert request["requestMetadata"]["application"] == "vonavy-agent"

    tool_config = request["toolConfig"]
    assert tool_config["toolChoice"] == {"tool": {"name": agent.FORECAST_MAPPING_TOOL_NAME}}
    assert len(tool_config["tools"]) == 1
    tool_spec = tool_config["tools"][0]["toolSpec"]
    assert tool_spec["name"] == agent.FORECAST_MAPPING_TOOL_NAME
    assert tool_spec["inputSchema"] == {"json": agent._output_schema()}
    assert tool_spec["strict"] is True


def test_bedrock_plan_is_validated_confirmable_and_version_bound() -> None:
    client = FakeBedrock(_bedrock_mapping())
    plan = agent.build_forecast_agent_plan(
        dataset_id="00000000-0000-0000-0000-000000000001",
        dataset_version_id="version-1",
        validation_result=_validation_result(),
        objective="Focus on promotion-aware demand.",
        bedrock_client=client,
    )

    assert plan["agentMode"] == "bedrock"
    assert plan["provider"] == "amazon-bedrock"
    assert plan["model"] == "eu.anthropic.claude-opus-4-6-v1"
    assert plan["requiresConfirmation"] is True
    assert plan["mapping"]["knownFutureNumeric"] == ["Discount"]
    assert plan["mapping"]["excluded"] == ["FuturePrediction"]
    assert plan["trainingEnd"] == "2025-03-17"
    assert plan["forecastStart"] == "2025-03-18"
    assert plan["forecastEnd"] == "2025-03-24"
    assert plan["execution"]["adapterId"] == "xgboost-direct-v1"
    assert plan["execution"]["availableAdapterIds"] == [
        "xgboost-direct-v1",
        "neuralnet-direct-v1",
        "chronos2-zero-shot-v1",
    ]
    assert plan["privacy"]["rawRowsSentToProvider"] is False
    assert plan["privacy"]["awsIamAuthentication"] is True
    assert len(plan["planId"]) == 64
    assert len(client.calls) == 1
    assert client.calls[0]["toolConfig"]["toolChoice"] == {
        "tool": {"name": agent.FORECAST_MAPPING_TOOL_NAME}
    }


def test_response_mapping_accepts_one_required_tool_input() -> None:
    mapping = _bedrock_mapping()
    response = {
        "output": {
            "message": {
                "content": [
                    {"text": "Submitting the mapping."},
                    {
                        "toolUse": {
                            "toolUseId": "mapping-1",
                            "name": agent.FORECAST_MAPPING_TOOL_NAME,
                            "input": mapping,
                        }
                    },
                ]
            }
        }
    }

    assert agent._response_mapping(response) == mapping


def test_response_mapping_rejects_text_only_json() -> None:
    response = {"output": {"message": {"content": [{"text": json.dumps(_bedrock_mapping())}]}}}

    try:
        agent._response_mapping(response)
    except agent.AgentPlanError as exc:
        assert exc.code == "agent_provider_invalid"
        assert exc.status == 502
        assert "tool call" in exc.message
    else:
        raise AssertionError("free-form JSON text was accepted")


def test_response_mapping_rejects_ambiguous_or_unexpected_tool_calls() -> None:
    mapping = _bedrock_mapping()
    duplicate = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "mapping-1",
                            "name": agent.FORECAST_MAPPING_TOOL_NAME,
                            "input": mapping,
                        }
                    },
                    {
                        "toolUse": {
                            "toolUseId": "mapping-2",
                            "name": agent.FORECAST_MAPPING_TOOL_NAME,
                            "input": mapping,
                        }
                    },
                ]
            }
        }
    }
    unexpected = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "other-1",
                            "name": "run_forecast",
                            "input": mapping,
                        }
                    }
                ]
            }
        }
    }

    for response in (duplicate, unexpected):
        try:
            agent._response_mapping(response)
        except agent.AgentPlanError as exc:
            assert exc.code == "agent_provider_invalid"
            assert exc.status == 502
        else:
            raise AssertionError("ambiguous or unexpected tool call was accepted")


def test_provider_cannot_reference_unknown_or_leakage_column() -> None:
    raw = _bedrock_mapping()
    raw["knownFutureNumeric"] = ["NotAColumn"]
    try:
        agent._validate_provider_mapping(raw, agent._column_profiles(_validation_result()))
    except agent.AgentPlanError as exc:
        assert exc.code == "agent_provider_invalid"
        assert exc.status == 502
    else:
        raise AssertionError("unknown provider column was accepted")

    raw = _bedrock_mapping()
    raw["knownFutureNumeric"] = ["FuturePrediction"]
    raw["excluded"] = ["Discount"]
    try:
        agent._validate_provider_mapping(raw, agent._column_profiles(_validation_result()))
    except agent.AgentPlanError as exc:
        assert exc.code == "agent_provider_invalid"
    else:
        raise AssertionError("outcome-derived feature was accepted")


def test_validation_result_is_bound_to_exact_dataset_version() -> None:
    try:
        agent.build_forecast_agent_plan(
            dataset_id="00000000-0000-0000-0000-000000000001",
            dataset_version_id="different-version",
            validation_result=_validation_result(),
            bedrock_client=FakeBedrock(_bedrock_mapping()),
        )
    except agent.AgentPlanError as exc:
        assert exc.code == "validation_result_mismatch"
        assert exc.status == 409
    else:
        raise AssertionError("mismatched immutable dataset version was accepted")


def test_plan_id_changes_with_objective_or_mapping() -> None:
    mapping = _bedrock_mapping()
    base = agent._plan_id("dataset", "version", mapping, "2025-03-17", "one", "bedrock")
    assert base != agent._plan_id("dataset", "version", mapping, "2025-03-17", "two", "bedrock")
    changed = dict(mapping)
    changed["targetColumn"] = "Other"
    assert base != agent._plan_id("dataset", "version", changed, "2025-03-17", "one", "bedrock")


def test_bedrock_access_denied_is_configuration_error() -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "Converse",
    )
    try:
        agent.build_forecast_agent_plan(
            dataset_id="00000000-0000-0000-0000-000000000001",
            dataset_version_id="version-1",
            validation_result=_validation_result(),
            bedrock_client=FakeBedrock(error=error),
        )
    except agent.AgentPlanError as exc:
        assert exc.code == "agent_configuration_error"
        assert exc.status == 503
        assert "Bedrock" in exc.message
    else:
        raise AssertionError("Bedrock access denial was accepted")


def test_bedrock_throttling_is_retryable_unavailable_error() -> None:
    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "Converse",
    )
    try:
        agent.build_forecast_agent_plan(
            dataset_id="00000000-0000-0000-0000-000000000001",
            dataset_version_id="version-1",
            validation_result=_validation_result(),
            bedrock_client=FakeBedrock(error=error),
        )
    except agent.AgentPlanError as exc:
        assert exc.code == "agent_provider_unavailable"
        assert exc.status == 503
    else:
        raise AssertionError("Bedrock throttling was accepted")
