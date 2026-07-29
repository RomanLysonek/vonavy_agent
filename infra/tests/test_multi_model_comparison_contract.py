from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

INFRA = Path(__file__).parents[1]
PROJECT = INFRA.parent
ORCHESTRATOR = INFRA / "lambda/forecast_control_plane/orchestrator.py"
HANDLER = INFRA / "lambda/forecast_control_plane/handler.py"
APP = INFRA / "web/app.js"
INDEX = INFRA / "web/index.html"
STYLES = INFRA / "web/styles.css"
WORKER = PROJECT / "src/vonavy_agent/forecasting/aws_batch.py"


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
        spec = importlib.util.spec_from_file_location("multi_model_orchestrator_test", ORCHESTRATOR)
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


def _profiles() -> list[dict[str, Any]]:
    return [
        {"name": "DateKey", "logicalType": "date", "temporal": {"maximum": "2026-01-18"}},
        {"name": "ProductId", "logicalType": "numeric"},
        {"name": "Quantity", "logicalType": "numeric", "nullRatio": 0.0},
        {"name": "ProductAvailable", "logicalType": "boolean"},
        {"name": "Discount", "logicalType": "numeric"},
        {"name": "Campaign", "logicalType": "string"},
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
        "staticCategorical": [],
        "excluded": [],
    }


def test_multi_model_draft_is_explicit_and_non_singular() -> None:
    module = _load_orchestrator()
    adapter_ids = [
        "xgboost-direct-v1",
        "neuralnet-direct-v1",
        "chronos2-zero-shot-v1",
    ]
    plan = module._draft_plan(
        {
            "adapterIds": adapter_ids,
            "mapping": _mapping(),
            "summary": "Run all three models on one immutable comparison.",
            "warnings": [],
        },
        profiles=_profiles(),
        dataset_id="dataset",
        dataset_version_id="version",
        selection_objective="compare all three",
    )

    assert plan["schemaVersion"] == "forecast-agent-draft/v2"
    assert plan["executionMode"] == "comparison"
    assert plan["adapterIds"] == adapter_ids
    assert [item["adapterId"] for item in plan["preprocessingPlans"]] == adapter_ids
    assert "adapterId" not in plan
    assert plan["requiresConfirmation"] is True
    assert plan["executesAutomatically"] is False


def test_backend_contract_uses_one_parent_job_and_all_terminal_result() -> None:
    handler = HANDLER.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")

    assert 'payload.get("adapterIds")' in handler
    assert '"forecast-comparison-request/v1"' in handler
    assert '"forecast-comparison-result/v1"' in handler
    assert 'TERMINAL = {"succeeded", "partial", "invalid", "failed"}' in handler
    assert '"run_mode": "comparison"' in handler
    assert handler.count("batch.submit_job(") == 1
    assert '"models"' in handler
    assert '"leaderboard"' in handler
    assert '"leaderboard_comparable"' in handler
    assert 'result.get("child_runs") != expected_child_runs' in handler
    assert "def _comparison_agent_model_context(" in handler
    assert 'for field in ("worst_entities", "feature_shifts")' in handler
    assert '"forecast-comparison-result/v1"' in worker
    assert "for child in children" in worker


def test_compact_dialog_and_comparison_persistence_are_materialized() -> None:
    app = APP.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert "BEDROCK FORECAST COPILOT" not in index
    assert "Validated metadata only" not in index
    assert 'id="agent-plan-toggle"' in index
    assert index.index('id="agent-send"') < index.index('id="agent-plan-toggle"')
    assert index.index('id="agent-plan-toggle"') < index.index('id="agent-confirm"')
    assert index.index('id="agent-confirm"') < index.index('id="agent-close"')
    assert "height: calc(100dvh - 24px)" in styles
    assert "width: min(calc(100vw - 24px), 1800px)" in styles
    assert (
        'FORECAST_TERMINAL_STATUSES = new Set(["succeeded", "partial", "invalid", "failed"])' in app
    )
    assert "request.adapterIds = adapterIds" in app
    assert "Common-evidence model leaderboard" in app
    assert 'showAgentPane(agentContext.planVisible ? "chat" : "plan")' in app
    assert "childRuns: Array.isArray(run.childRuns)" in app
    assert '$("agent-plan-toggle").addEventListener("click", toggleAgentPlan)' in app
