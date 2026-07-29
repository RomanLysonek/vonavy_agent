from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKER = ROOT / "src/vonavy_agent/forecasting/aws_batch.py"


def _functions(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    return {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def test_comparison_worker_runs_all_models_before_publishing_aggregate() -> None:
    source = WORKER.read_text(encoding="utf-8")
    functions = _functions(source)

    validate = functions["_validate_comparison_request"]
    execute = functions["_comparison_main"]
    aggregate = functions["_comparison_payload"]
    basis = functions["_comparison_basis"]
    dispatch = functions["main"]

    assert '"forecast-comparison-request/v1"' in validate
    assert "2 <= len(adapter_ids)" in validate
    assert "len(set(adapter_ids)) != len(adapter_ids)" in validate
    assert execute.count("_download(") == 1
    assert "for child in children" in execute
    assert "raw=frame.copy(deep=True)" in execute
    assert execute.count("completed = {result.adapter.id for result in results}") == 2
    assert execute.index("for child in children") < execute.index("_comparison_payload(")
    assert execute.index("_comparison_payload(") < execute.index("_publish_payload(")
    assert '"forecast-comparison-result/v1"' in aggregate
    assert '"leaderboard"' in aggregate
    assert '"leaderboard_comparable"' in aggregate
    assert '"best_adapter_id"' in aggregate
    assert "len(set(signatures)) != 1" in basis
    assert '"partial"' in functions["_comparison_status"]
    assert "_comparison_main(parsed)" in dispatch


def test_single_forecast_path_remains_available() -> None:
    source = WORKER.read_text(encoding="utf-8")
    functions = _functions(source)

    assert "ForecastRequest.model_validate_json" in functions["_single_main"]
    assert "_publish_result(" in functions["_single_main"]
    assert "_single_main(raw_request)" in functions["main"]
