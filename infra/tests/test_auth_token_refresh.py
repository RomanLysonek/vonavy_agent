from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_browser_refreshes_expired_tokens_and_replays_one_401() -> None:
    completed = subprocess.run(
        ["node", str(ROOT / "tests/auth_token_refresh_smoke.mjs")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["status"] == "passed"


def test_refresh_contract_is_bounded_and_uses_session_storage() -> None:
    web = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert 'grant_type: "refresh_token"' in web
    assert "tokenRefreshPromise" in web
    assert "response.status === 401 && authReplayAvailable" in web
    assert "authReplayAvailable = false" in web
    assert "saveTokens(tokenResponse)" in web
    assert 'sessionStorage.setItem("vonavy_tokens"' in web
    assert 'localStorage.setItem("vonavy_tokens"' not in web
    assert 'const attempts = method === "GET" ? 3 : 1;' in web
