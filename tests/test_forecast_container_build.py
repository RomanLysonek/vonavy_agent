from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.forecast-batch"


def test_chronos_snapshot_layer_is_resilient_and_source_independent() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")

    dependency_sync = "uv sync --frozen --no-dev --no-install-project"
    snapshot_download = "snapshot_download(repo_id='amazon/chronos-2'"
    source_copy = "COPY src ./src"
    project_sync = "uv sync --frozen --no-dev --no-editable"

    assert dependency_sync in source
    assert snapshot_download in source
    assert source_copy in source
    assert project_sync in source
    assert source.index(dependency_sync) < source.index(snapshot_download)
    assert source.index(snapshot_download) < source.index(source_copy)
    assert source.index(source_copy) < source.index(project_sync)

    assert "while true; do" in source
    assert 'if [ "$attempt" -ge 6 ]; then' in source
    assert "delay=$((attempt * 10))" in source
    assert "retrying in ${delay}s" in source

    assert "revision='29ec3766d36d6f73f0696f85560a422f50e8498c'" in source
    assert "HF_HUB_OFFLINE=1" in source
    assert "TRANSFORMERS_OFFLINE=1" in source
