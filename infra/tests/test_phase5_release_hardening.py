from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
PROJECT = ROOT.parent


def test_api_responses_publish_bounded_provenance() -> None:
    control = (ROOT / "lambda/control_plane/handler.py").read_text(encoding="utf-8")
    forecast = (ROOT / "lambda/forecast_control_plane/handler.py").read_text(encoding="utf-8")

    for source in (control, forecast):
        assert '"x-vonavy-request-id": response_id' in source
        assert '"x-vonavy-source-revision": SOURCE_REVISION' in source
        assert "def _new_response_id() -> str:" in source
        assert "return str(uuid.UUID(bytes=os.urandom(16), version=4))" in source
        assert "response_id = _new_response_id()" in source
        assert "response_id = str(uuid.uuid4())" not in source
        assert '"source_revision": SOURCE_REVISION' in source
        assert '"status_code":' in source

    assert 'SOURCE_REVISION = os.environ.get("SOURCE_REVISION", "unknown")' in control


def test_cdk_exposes_diagnostics_without_new_authority() -> None:
    stack = (ROOT / "vonavy_infra/control_plane_stack.py").read_text(encoding="utf-8")

    assert stack.count('"SOURCE_REVISION": config.source_revision') == 2
    assert "expose_headers=[" in stack
    assert '"x-vonavy-request-id"' in stack
    assert '"x-vonavy-source-revision"' in stack
    assert stack.count("http_api.add_routes(") == 2


def test_browser_retries_only_bounded_read_requests() -> None:
    web = (ROOT / "web/app.js").read_text(encoding="utf-8")

    assert "const API_REQUEST_TIMEOUT_MS = 15_000;" in web
    assert "const API_RETRY_DELAYS_MS = [250, 750];" in web
    assert "new Set([429, 502, 503, 504])" in web
    assert 'const attempts = method === "GET" ? 3 : 1;' in web
    assert "new AbortController()" in web
    assert 'response.headers.get("x-vonavy-request-id")' in web
    assert 'response.headers.get("x-vonavy-source-revision")' in web
    assert "class ApiRequestError extends Error" in web
    assert "innerHTML" not in web


def test_release_hardening_contract_is_checked_in() -> None:
    document = (PROJECT / "docs/release-hardening.md").read_text(encoding="utf-8")

    assert "`POST` requests are attempted exactly once" in document
    assert "x-vonavy-request-id" in document
    assert "x-vonavy-source-revision" in document
    assert "adds no" in document
