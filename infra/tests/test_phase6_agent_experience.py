from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
MODULE = ROOT / "lambda/forecast_control_plane/agent_async.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("agent_async_phase6", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_async_ids_are_idempotent_and_payload_contains_no_message() -> None:
    module = _load()
    token = "00000000-0000-4000-8000-000000000001"
    session = module.session_id_for("owner", "dataset", token)
    turn = module.turn_id_for(session, token)
    assert session == module.session_id_for("owner", "dataset", token)
    assert turn == module.turn_id_for(session, token)
    payload = json.loads(module.invocation_payload("owner", session, turn))
    assert payload == {
        "owner": "owner",
        "schemaVersion": "forecast-agent-async-turn/v1",
        "sessionId": session,
        "source": "vonavy.agent-turn",
        "turnId": turn,
    }
    assert "message" not in payload


def test_message_and_request_token_are_bounded() -> None:
    module = _load()
    assert module.clean_agent_message("  compare   models ") == "compare models"
    with pytest.raises(module.AgentAsyncValueError):
        module.clean_agent_message("x" * 2001)
    with pytest.raises(module.AgentAsyncValueError):
        module.canonical_request_token("00000000-0000-5000-8000-000000000001")


def test_turn_view_exposes_bounded_failure_state() -> None:
    module = _load()
    view = module.turn_view(
        {
            "turn_count": 2,
            "turn_id": "turn",
            "turn_status": "failed",
            "turn_error_code": "agent_provider_error",
            "turn_error_message": "Bedrock failed",
        }
    )
    assert view["pending"] is False
    assert view["error"] == {
        "code": "agent_provider_error",
        "message": "Bedrock failed",
    }


def test_handler_moves_bedrock_work_out_of_api_post_paths() -> None:
    handler = (ROOT / "lambda/forecast_control_plane/handler.py").read_text()
    tree = ast.parse(handler)
    functions = {
        node.name: ast.get_source_segment(handler, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "_run_agent_session_turn" not in functions["_agent_session_create"]
    assert "_run_agent_session_turn" not in functions["_agent_session_message"]
    assert "_run_agent_session_turn" in functions["_agent_turn_worker"]
    assert 'InvocationType="Event"' in functions["_enqueue_agent_turn"]
    assert "turn_status" in handler
    assert "AGENT_ASYNC_SOURCE" in functions["lambda_handler"]


def test_stack_grants_self_invoke_and_extends_only_worker_timeout() -> None:
    stack = (ROOT / "vonavy_infra/control_plane_stack.py").read_text()
    assert "timeout=Duration.seconds(300)" in stack
    assert 'actions=["lambda:InvokeFunction"]' in stack
    assert "Tags.of(forecast_control_plane_function).add" in stack
    assert '"vonavy-agent:async-self-invoke"' in stack
    assert '"aws:ResourceTag/vonavy-agent:async-self-invoke": "true"' in stack
    assert "arn_format=ArnFormat.COLON_RESOURCE_NAME" in stack
    assert "forecast_control_plane_function.function_arn" not in stack


def test_frontend_uses_safe_markdown_polling_and_explicit_confirmation_state() -> None:
    web = (ROOT / "web/app.js").read_text()
    styles = (ROOT / "web/styles.css").read_text()
    assert "renderSafeMarkdown" in web
    assert "agent-markdown-table" in web
    assert "waitForAgentTurn" in web
    assert "requestToken: crypto.randomUUID()" in web
    assert "session.links.self" in web or "current.links.self" in web
    assert "Ask the agent to prepare a confirmable plan first." in web
    assert "innerHTML" not in web
    assert "insertAdjacentHTML" not in web
    assert "DOMParser" not in web
    assert ".agent-markdown-table" in styles
