from pathlib import Path


def test_agentic_routes_and_session_guards_are_materialized() -> None:
    root = Path(__file__).parents[1]
    handler = (root / "lambda/forecast_control_plane/handler.py").read_text()
    stack = (root / "vonavy_infra/control_plane_stack.py").read_text()
    web = (root / "web/app.js").read_text()
    index = (root / "web/index.html").read_text()
    styles = (root / "web/styles.css").read_text()

    assert "run_agent_turn" in handler
    assert "AGENT_SESSION_MAX_TURNS" in handler
    assert "AGENT_SESSION#" in handler
    assert "attribute_not_exists(pk) AND attribute_not_exists(sk)" in handler
    assert "agent_session_limit" in handler
    assert "agent_session_conflict" in handler
    assert "turn_count=:expected_turns" in handler
    assert "POST /api/datasets/{dataset_id}/forecast-agent/sessions" in handler
    assert "POST /api/forecast-agent/sessions/{session_id}/messages" in handler
    assert "GET /api/forecast-agent/sessions/{session_id}" in handler

    assert "/api/datasets/{dataset_id}/forecast-agent/sessions" in stack
    assert "/api/forecast-agent/sessions/{session_id}/messages" in stack
    assert "/api/forecast-agent/sessions/{session_id}" in stack
    assert '"forecastAgentConversationEnabled": True' in stack

    assert "agenticForecastDataset" in web
    assert "immutable plan" in web
    assert "explicit confirmation" in web
    assert "sendAgentMessage" in web
    assert "confirmAgentPlan" in web
    assert 'id="agent-dialog"' in index
    assert 'id="agent-messages"' in index
    assert ".agent-dialog" in styles
    assert "No raw rows, code execution, or automatic training" in index
