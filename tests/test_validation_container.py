from __future__ import annotations

from pathlib import Path


def test_validation_worker_container_is_non_root_and_has_no_network_service() -> None:
    dockerfile = Path("Dockerfile.validation-worker").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["vonavy-agent", "validate-dataset"]' in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "openssh" not in dockerfile.lower()
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in dockerfile
    assert "COPY --from=build --chown=10001:10001 /opt/venv /opt/venv" in dockerfile
