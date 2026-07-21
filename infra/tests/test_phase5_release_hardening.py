from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

ROOT = Path(__file__).parents[1]
PROJECT = ROOT.parent


def _load_response_log_helper(source: str) -> Callable[[str, int], str]:
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_response_log_message"
    )
    namespace: dict[str, object] = {
        "json": json,
        "SOURCE_REVISION": "0123456789abcdef0123456789abcdef01234567",
    }
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]),
            "<response-log-helper>",
            "exec",
        ),
        namespace,
    )
    helper = namespace["_response_log_message"]
    assert callable(helper)
    return cast(Callable[[str, int], str], helper)


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
        assert "def _response_log_message(" in source
        assert '"event": "api_response"' in source
        assert '"requestId": response_id' in source
        assert '"sourceRevision": SOURCE_REVISION' in source
        assert '"statusCode": status_code' in source
        assert "LOGGER.info(_response_log_message(" in source

    assert 'SOURCE_REVISION = os.environ.get("SOURCE_REVISION", "unknown")' in control


def test_response_log_message_is_correlatable_and_bounded() -> None:
    request_id = "00000000-0000-4000-8000-000000000123"
    revision = "0123456789abcdef0123456789abcdef01234567"

    for path in (
        ROOT / "lambda/control_plane/handler.py",
        ROOT / "lambda/forecast_control_plane/handler.py",
    ):
        helper = _load_response_log_helper(path.read_text(encoding="utf-8"))
        encoded = helper(request_id, 503)
        event = json.loads(encoded)

        assert encoded == (
            '{"event":"api_response","requestId":"'
            + request_id
            + '","sourceRevision":"'
            + revision
            + '","statusCode":503}'
        )
        assert event == {
            "event": "api_response",
            "requestId": request_id,
            "sourceRevision": revision,
            "statusCode": 503,
        }
        assert set(event) == {"event", "requestId", "sourceRevision", "statusCode"}


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
    assert "compact JSON `api_response` log" in document
    assert "adds no" in document
