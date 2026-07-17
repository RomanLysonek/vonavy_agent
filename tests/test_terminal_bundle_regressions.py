from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from vonavy_agent.domain import JobState
from vonavy_agent.experiments import run_gate
from vonavy_agent.hashing import canonical_json
from vonavy_agent.jobs import Worker, enqueue_export, enqueue_run
from vonavy_agent.persistence import (
    Export,
    Job,
    Run,
    RunMetric,
    new_id,
    session_scope,
)


def successful_run(evidence, spec_row):
    settings, engine, registry, _, _, _ = evidence
    gate = run_gate(engine, registry, spec_row.id)
    run, job = enqueue_run(
        engine,
        spec_row.id,
        gate.id,
        gate.confirmation_token or "",
    )
    assert Worker(settings, engine).run_once()
    return settings, engine, run, job


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_succeeded_repair_rejects_incomplete_listed_run_files(
    evidence,
    spec_row,
    damage: str,
) -> None:
    settings, engine, run, job = successful_run(evidence, spec_row)
    with session_scope(engine) as session:
        persisted_job = session.get_one(Job, job.id)
        persisted_run = session.get_one(Run, run.id)
        persisted_job.result_json = canonical_json(
            {
                "run_id": run.id,
                "summary": json.loads(persisted_run.summary_json or "{}"),
            }
        )
    listed_file = settings.managed_root / "runs" / run.id / "metrics.json"
    if damage == "missing":
        listed_file.unlink()
    else:
        listed_file.write_text("corrupt")
    Worker(settings, engine).recover()
    with Session(engine) as session:
        persisted_job = session.get_one(Job, job.id)
        persisted_run = session.get_one(Run, run.id)
        assert persisted_job.state == JobState.FAILED
        assert persisted_run.summary_json is None
        assert session.query(RunMetric).filter(RunMetric.run_id == run.id).count() == 0
    manifest = json.loads((settings.managed_root / "runs" / run.id / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["errors"][0]["code"] == "artifact_repair_failed"


def test_valid_legacy_succeeded_run_without_staging_metadata_is_verified(
    evidence,
    spec_row,
) -> None:
    settings, engine, run, job = successful_run(evidence, spec_row)
    with session_scope(engine) as session:
        persisted_job = session.get_one(Job, job.id)
        persisted_run = session.get_one(Run, run.id)
        persisted_job.result_json = canonical_json(
            {
                "run_id": run.id,
                "summary": json.loads(persisted_run.summary_json or "{}"),
            }
        )
    Worker(settings, engine).recover()
    with Session(engine) as session:
        assert session.get_one(Job, job.id).state == JobState.SUCCEEDED


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_legacy_succeeded_export_final_artifact_is_verified(
    evidence,
    spec_row,
    damage: str,
) -> None:
    settings, engine, run, _ = successful_run(evidence, spec_row)
    export_id = new_id()
    export, job = enqueue_export(engine, export_id, [run.id])
    assert Worker(settings, engine).run_once()
    with session_scope(engine) as session:
        persisted_job = session.get_one(Job, job.id)
        persisted_export = session.get_one(Export, export.id)
        persisted_job.result_json = canonical_json(
            {
                "export_id": export.id,
                "relative_path": persisted_export.relative_path,
                "sha256": persisted_export.manifest_hash,
            }
        )
        final_path = settings.managed_root / (persisted_export.relative_path or "")
    if damage == "missing":
        final_path.unlink()
    else:
        final_path.write_bytes(b"corrupt")
    Worker(settings, engine).recover()
    with Session(engine) as session:
        persisted_job = session.get_one(Job, job.id)
        persisted_export = session.get_one(Export, export.id)
        assert persisted_job.state == JobState.FAILED
        assert persisted_export.relative_path is None
        assert persisted_export.manifest_hash is None
