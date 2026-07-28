from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

INFRA_ROOT = Path(__file__).parents[1]
CONTROL_PLANE = INFRA_ROOT / "lambda" / "forecast_control_plane"
HANDLER = CONTROL_PLANE / "handler.py"
ORCHESTRATOR = CONTROL_PLANE / "orchestrator.py"
STACK = INFRA_ROOT / "vonavy_infra" / "control_plane_stack.py"
WEB = INFRA_ROOT / "web" / "app.js"


def _load_orchestrator() -> ModuleType:
    agent = ModuleType("agent")

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

    agent.AgentPlanError = AgentPlanError
    agent.BEDROCK_MODEL_ID = "eu.anthropic.claude-opus-4-6-v1"
    agent.BEDROCK_REGION = "eu-central-1"
    agent._column_profiles = lambda value: value["profiles"]
    agent._training_end = lambda mapping, profiles: ("2026-01-11", [])
    agent._validate_provider_mapping = lambda value, profiles: value

    previous = sys.modules.get("agent")
    sys.modules["agent"] = agent
    try:
        spec = importlib.util.spec_from_file_location(
            "forecast_result_orchestrator_test",
            ORCHESTRATOR,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("agent", None)
        else:
            sys.modules["agent"] = previous


class FakeBedrock:
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

    def converse(self, **request: Any) -> dict[str, Any]:
        self.request = request
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": (
                                "Measured WAPE is available. The deterministic review should be "
                                "checked before operational reuse."
                            )
                        }
                    ]
                }
            }
        }


def test_twenty_turn_phases_and_result_route_are_wired() -> None:
    handler = HANDLER.read_text(encoding="utf-8")
    orchestrator = ORCHESTRATOR.read_text(encoding="utf-8")
    stack = STACK.read_text(encoding="utf-8")
    web = WEB.read_text(encoding="utf-8")

    assert (
        'AGENT_SESSION_MAX_TURNS = int(os.environ.get("AGENT_SESSION_MAX_TURNS", "20"))' in handler
    )
    assert 'AGENT_DAILY_LIMIT = int(os.environ.get("AGENT_DAILY_LIMIT", "50"))' in handler
    assert '"AGENT_SESSION_MAX_TURNS": "20"' in stack
    assert '"AGENT_DAILY_LIMIT": "50"' in stack
    assert '"forecastAgentMaximumTurns": 20' in stack
    assert '"/api/forecasts/{run_id}/agent/sessions"' in stack
    assert "POST /api/forecasts/{run_id}/agent/sessions" in handler
    assert "MAX_HISTORY_MESSAGES = 40" in orchestrator
    assert "Discuss result with AI" in web
    assert "reviewForecastResult(run, output, discuss)" in web


def test_result_session_is_bound_to_server_owned_completed_result() -> None:
    source = HANDLER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    create = functions["_result_agent_session_create"]
    worker = functions["_agent_turn_worker"]
    message = functions["_agent_session_message"]
    assert "payload = _result(event, run_id)" in create
    assert "_agent_result_context(payload)" in create
    assert "_dataset(" not in create
    assert 'phase == "result"' in worker
    assert "_run_result_agent_session_turn" in worker
    assert 'phase == "planning"' in message
    assert 'phase != "result"' in message


def test_result_conversation_is_tool_free_and_summary_only() -> None:
    orchestrator = _load_orchestrator()
    bedrock = FakeBedrock()
    turn = orchestrator.run_result_agent_turn(
        run_id="11111111-1111-4111-8111-111111111111",
        result_context={
            "schema_version": "forecast-result/v1",
            "status": "succeeded",
            "run_id": "11111111-1111-4111-8111-111111111111",
            "dataset_id": "22222222-2222-4222-8222-222222222222",
            "adapter": {"id": "xgboost-direct-v1"},
            "evaluation": {"metrics": {"wape": 0.18}},
            "review": {"status": "ready", "findings": []},
            "artifacts": {"predictions": {"key": "private/predictions.csv"}},
            "downloads": {"predictions": "https://example.invalid/private"},
        },
        message="What does the measured WAPE mean?",
        history=[],
        bedrock_client=bedrock,
    )

    assert bedrock.request is not None
    assert "toolConfig" not in bedrock.request
    assert bedrock.request["requestMetadata"]["operation"] == "forecast-result-conversation"
    system = bedrock.request["system"][0]["text"]
    assert '"wape":0.18' in system
    assert "private/predictions.csv" not in system
    assert "example.invalid" not in system

    payload = turn.as_dict()
    assert payload["conversationPhase"] == "result"
    assert payload["draftPlan"] is None
    assert payload["requiresConfirmation"] is False
    assert payload["privacy"]["resultSummaryOnly"] is True
    assert payload["privacy"]["rawRowsSentToProvider"] is False


def test_history_holds_twenty_complete_turns() -> None:
    orchestrator = _load_orchestrator()
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "text": f"message {index}"}
        for index in range(40)
    ]

    cleaned = orchestrator._clean_history(history)

    assert len(cleaned) == 40
    assert cleaned[0]["text"] == "message 0"
    assert cleaned[-1]["text"] == "message 39"
