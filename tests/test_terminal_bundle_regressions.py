from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from vonavy_agent.domain import JobState
from vonavy_agent.experiments import run_gate
from vonavy_agent.jobs import Worker, enqueue_run
from vonavy_agent.persistence import Job, Run, RunMetric


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
