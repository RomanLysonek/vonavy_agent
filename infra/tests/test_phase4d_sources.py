from pathlib import Path


def test_phase4d_worker_evidence_and_deterministic_critic_are_materialized() -> None:
    root = Path(__file__).parents[1]
    project = root.parent
    contracts = (project / "src/vonavy_agent/forecasting/contracts.py").read_text()
    evaluation = (project / "src/vonavy_agent/forecasting/evaluation.py").read_text()
    model = (project / "src/vonavy_agent/forecasting/model.py").read_text()
    neural = (project / "src/vonavy_agent/forecasting/neural_net.py").read_text()
    chronos = (project / "src/vonavy_agent/forecasting/chronos2.py").read_text()
    handler = (root / "lambda/forecast_control_plane/handler.py").read_text()
    web = (root / "web/app.js").read_text()

    assert "forecast-evaluation/v1" in contracts
    assert "ForecastEvaluationEvidence" in contracts
    assert "build_forecast_evaluation" in evaluation
    assert "raw_entity_values_exported" in contracts
    assert "entity-" in evaluation
    assert "build_forecast_evaluation" in model
    assert "build_forecast_evaluation" in neural
    assert "build_forecast_evaluation" in chronos

    assert "forecast-result-review/v1" in handler
    assert "_forecast_result_review" in handler
    assert '"bedrockInvoked": False' in handler
    assert '"automaticRerun": False' in handler
    assert 'payload["review"]' in handler
    assert "appendResultReview" in web
    assert "Measured next experiments" in web


def test_phase4d_adds_no_route_or_stack_source() -> None:
    root = Path(__file__).parents[1]
    stack = (root / "vonavy_infra/control_plane_stack.py").read_text()
    assert "forecast-result-review" not in stack
    assert "forecast-evaluation" not in stack
