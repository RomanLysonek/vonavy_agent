from __future__ import annotations

from pathlib import Path

INFRA = Path(__file__).parents[1]
ORCHESTRATOR = INFRA / "lambda" / "forecast_control_plane" / "orchestrator.py"
APP = INFRA / "web" / "app.js"
STACK = INFRA / "vonavy_infra" / "control_plane_stack.py"


def test_phase4c_source_boundaries() -> None:
    source = ORCHESTRATOR.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")

    assert 'PREPROCESSING_CATALOG_VERSION = "forecast-preprocessing-catalog/v1"' in source
    assert 'PREPROCESSING_REVIEW_POLICY_VERSION = "forecast-preprocessing-review/v1"' in source
    assert '"name": "compile_preprocessing_plan"' in source
    assert '"preprocessingPlan": preprocessing_plan' in source
    assert '"generatedCode": False' in source
    assert '"arbitraryTransforms": False' in source
    assert '"rawRowsRequiredByAgent": False' in source
    assert '"rawStringValuesRequiredByAgent": False' in source
    assert '"evidenceBasis": "validated-aggregate-metadata"' in source
    assert '"confidence": confidence' in source
    assert '"notAvailableFromAggregateProfile"' in source
    assert "exec(" not in source
    assert "eval(" not in source

    assert "Preprocessing: ${operations.length} fixed operations" in app
    assert "plan digest: ${digest.slice(0, 12)}" in app
    assert "preprocessingDigest.slice(0, 12)" in app
    assert 'Preprocessing review: ${review.status || "unavailable"}' in app
    assert "attention finding" in app
    assert "innerHTML" not in app


def test_phase4c_does_not_add_routes_or_iam() -> None:
    stack = STACK.read_text(encoding="utf-8")
    route_count = stack.count("http_api.add_routes(")

    assert route_count == 2
    assert "compile_preprocessing_plan" not in stack
    assert "forecast-preprocessing-catalog" not in stack
