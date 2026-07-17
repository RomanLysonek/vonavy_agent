from __future__ import annotations

import io
import json

import pytest
from conftest import make_spec, synthetic_frame

from vonavy_agent.datasets import build_profile
from vonavy_agent.domain import (
    AvailabilityKind,
    AvailabilityPolicy,
    DatasetMappingSpec,
)
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import create_experiment_spec, run_gate
from vonavy_agent.hashing import file_hash
from vonavy_agent.persistence import Blob


def test_ingestion_hashes_versions_and_never_changes_source(runtime) -> None:
    settings, engine, registry = runtime
    source = synthetic_frame(days=5).to_csv(index=False).encode()
    first = registry.ingest_stream(io.BytesIO(source), "safe.csv", "Demand")
    with engine.connect() as connection:
        blob_path = (
            settings.managed_root
            / connection.execute(
                Blob.__table__.select().where(Blob.sha256 == first.source_blob_sha256)
            )
            .mappings()
            .one()["relative_path"]
        )
    assert blob_path.read_bytes() == source
    assert file_hash(blob_path) == first.source_blob_sha256

    delta = synthetic_frame(days=6).tail(2).to_csv(index=False).encode()
    second = registry.ingest_stream(
        io.BytesIO(delta),
        "delta.csv",
        "Demand",
        mode="append",
        dataset_id=first.dataset_id,
        parent_version_id=first.id,
    )
    assert second.version_number == 2
    assert second.parent_id == first.id
    assert second.row_count == first.row_count + 2
    assert blob_path.read_bytes() == source


def test_ingestion_rejects_traversal(runtime) -> None:
    _, _, registry = runtime
    with pytest.raises(AgentError, match="plain safe basename"):
        registry.ingest_stream(io.BytesIO(b"a\n1\n"), "../escape.csv", "Unsafe")


def test_profile_and_gate_block_duplicate_keys(runtime) -> None:
    _, engine, registry = runtime
    version = registry.ingest_stream(
        io.BytesIO(synthetic_frame(duplicate=True).to_csv(index=False).encode()),
        "duplicate.csv",
        "Duplicate",
    )
    mapping = registry.create_mapping(
        version.id,
        DatasetMappingSpec(
            timestamp_column="date",
            entity_column="store",
            target_column="demand",
            target_availability=AvailabilityPolicy(kind=AvailabilityKind.EVENT_TIME),
        ),
    )
    profile = build_profile(registry, version.id, mapping.id, 10)
    spec = make_spec(version, mapping, profile, models=make_spec_models())
    spec_row = create_experiment_spec(engine, spec)
    gate = run_gate(engine, registry, spec_row.id)
    report = json.loads(gate.canonical_json)
    assert gate.status == "blocked"
    assert "duplicate_entity_dates" in {reason["code"] for reason in report["reasons"]}


def make_spec_models():
    from vonavy_agent.domain import MovingAverageConfig, SeasonalNaiveConfig

    return (SeasonalNaiveConfig(), MovingAverageConfig())


def test_profile_reports_daily_coverage(evidence) -> None:
    _, _, _, _, _, profile = evidence
    payload = json.loads(profile.canonical_json)
    assert payload["rows"] == 240
    assert payload["entities"] == 2
    assert payload["gap_days"] == 0
    assert payload["duplicate_key_rows"] == 0
