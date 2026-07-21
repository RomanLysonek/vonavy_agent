from __future__ import annotations

import importlib.util
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
            self, code: str, message: str, status: int, detail: dict[str, Any] | None = None
        ):
            self.code = code
            self.message = message
            self.status = status
            self.detail = detail or {}

    def column_profiles(result: dict[str, Any]) -> list[dict[str, Any]]:
        return result["profiles"]

    def validate_mapping(raw: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any]:
        allowed = {item["name"] for item in profiles}
        columns = [raw["timestampColumn"], raw["targetColumn"]]
        columns.extend(raw.get("knownFutureNumeric", []))
        columns.extend(raw.get("knownFutureCategorical", []))
        columns.extend(raw.get("staticNumeric", []))
        columns.extend(raw.get("staticCategorical", []))
        columns.extend(
            value for value in (raw.get("entityColumn"), raw.get("availabilityColumn")) if value
        )
        if any(value not in allowed for value in columns):
            raise AgentPlanError("agent_provider_invalid", "unknown column", 502)
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
    spec = importlib.util.spec_from_file_location("orchestrator", MODULE_ROOT / "orchestrator.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator"] = module
    spec.loader.exec_module(module)
    return module


class FakeBedrock:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _profiles() -> list[dict[str, Any]]:
    return [
        {"name": "DateKey", "logicalType": "date", "temporal": {"maximum": "2026-01-18"}},
        {"name": "ProductId", "logicalType": "numeric"},
        {"name": "Quantity", "logicalType": "numeric", "nullRatio": 0.1},
        {"name": "ProductAvailable", "logicalType": "boolean"},
        {"name": "Discount", "logicalType": "numeric"},
        {"name": "Campaign", "logicalType": "string"},
        {"name": "Category", "logicalType": "string"},
        {"name": "FuturePrediction", "logicalType": "numeric"},
    ]


def _tool_use(name: str, value: dict[str, Any], tool_id: str) -> dict[str, Any]:
    return {"toolUse": {"name": name, "input": value, "toolUseId": tool_id}}


def test_agent_uses_bounded_tools_and_returns_confirmable_plan() -> None:
    module = _load()
    mapping = {
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
    client = FakeBedrock(
        [
            {"output": {"message": {"content": [_tool_use("inspect_dataset", {}, "1")]}}},
            {"output": {"message": {"content": [_tool_use("compare_models", {}, "2")]}}},
            {
                "output": {
                    "message": {
                        "content": [
                            _tool_use(
                                "draft_forecast_plan",
                                {
                                    "adapterId": "neuralnet-direct-v1",
                                    "mapping": mapping,
                                    "summary": "Use the shared panel neural network.",
                                    "warnings": [],
                                },
                                "3",
                            )
                        ]
                    }
                }
            },
            {
                "output": {
                    "message": {
                        "content": [{"text": "The NeuralNet plan is ready for confirmation."}]
                    }
                }
            },
        ]
    )
    turn = module.run_agent_turn(
        dataset_id="dataset",
        dataset_version_id="version",
        validation_result={"profiles": _profiles()},
        message="Choose the best trained model and prepare a safe plan.",
        bedrock_client=client,
    )
    assert turn.draft_plan is not None
    assert turn.draft_plan["adapterId"] == "neuralnet-direct-v1"
    assert turn.draft_plan["requiresConfirmation"] is True
    assert turn.draft_plan["executesAutomatically"] is False
    assert turn.draft_plan["mapping"]["excluded"] == ["FuturePrediction"]
    assert [item["name"] for item in turn.tool_audit] == [
        "inspect_dataset",
        "compare_models",
        "draft_forecast_plan",
    ]
    assert all(request["inferenceConfig"]["temperature"] == 0.0 for request in client.requests)
    assert all(len(request["toolConfig"]["tools"]) == 3 for request in client.requests)


def test_unknown_column_in_plan_is_rejected() -> None:
    module = _load()
    mapping = {
        "timestampColumn": "DateKey",
        "entityColumn": "ProductId",
        "targetColumn": "UnknownTarget",
        "availabilityColumn": None,
        "knownFutureNumeric": [],
        "knownFutureCategorical": [],
        "staticNumeric": [],
        "staticCategorical": [],
        "excluded": [],
    }
    client = FakeBedrock(
        [
            {
                "output": {
                    "message": {
                        "content": [
                            _tool_use(
                                "draft_forecast_plan",
                                {
                                    "adapterId": "xgboost-direct-v1",
                                    "mapping": mapping,
                                    "summary": "Unsafe",
                                    "warnings": [],
                                },
                                "1",
                            )
                        ]
                    }
                }
            }
        ]
    )
    with pytest.raises(module.OrchestratorError, match="unknown column"):
        module.run_agent_turn(
            dataset_id="dataset",
            dataset_version_id="version",
            validation_result={"profiles": _profiles()},
            message="Run it.",
            bedrock_client=client,
        )


def test_history_and_message_limits_are_enforced() -> None:
    module = _load()
    with pytest.raises(module.OrchestratorError, match="exceeds"):
        module.run_agent_turn(
            dataset_id="dataset",
            dataset_version_id="version",
            validation_result={"profiles": _profiles()},
            message="x" * 2001,
            bedrock_client=FakeBedrock([]),
        )
