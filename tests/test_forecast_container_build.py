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
    seed_stage = "FROM ${CHRONOS_SEED_IMAGE} AS chronos-seed"
    seed_copy = "COPY --from=chronos-seed /chronos-seed /opt/models/chronos-2"
    stack_source = (ROOT / "infra/vonavy_infra/control_plane_stack.py").read_text(encoding="utf-8")
    app_source = (ROOT / "infra/app.py").read_text(encoding="utf-8")

    assert "ARG CHRONOS_SEED_IMAGE=python:3.12.3-slim-bookworm" in source
    assert "mkdir -p /chronos-seed" in source
    assert "cp -a /opt/models/chronos-2/. /chronos-seed/" in source
    assert seed_stage in source
    assert seed_copy in source
    assert source.index(seed_stage) < source.index(dependency_sync)
    assert source.index(dependency_sync) < source.index(seed_copy)
    assert source.index(seed_copy) < source.index(snapshot_download)
    assert "-s /opt/models/chronos-2/config.json" in source
    assert "-name '*.safetensors'" in source
    assert "Using Chronos-2 snapshot from the internal seed image" in source
    assert "forecast_seed_image: str | None = None" in stack_source
    assert 'forecast_build_args = {"VCS_REF": config.source_revision}' in stack_source
    assert 'forecast_build_args["CHRONOS_SEED_IMAGE"]' in stack_source
    assert "build_args=forecast_build_args" in stack_source
    assert 'forecast_seed_image=_optional("VONAVY_FORECAST_SEED_IMAGE")' in app_source

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
