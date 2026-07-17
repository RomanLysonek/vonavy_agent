from __future__ import annotations

import json
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vonavy_agent.adapters import PreparedInvocation, dry_run_invocation
from vonavy_agent.backtest import PreparedData, _ridge_predictions
from vonavy_agent.domain import DatasetMappingSpec, JobState, RidgeDirectConfig
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import run_gate
from vonavy_agent.exporting import create_static_export
from vonavy_agent.jobs import Worker, enqueue_job, enqueue_run, request_cancellation
from vonavy_agent.persistence import Job, Run, RunMetric, session_scope
from vonavy_agent.planner import propose_experiments


def test_gate_run_metrics_manifest_and_static_export(evidence, spec_row) -> None:
    settings, engine, registry, _, _, _ = evidence
    gate = run_gate(engine, registry, spec_row.id)
    assert gate.status == "passed"
    run, job = enqueue_run(engine, spec_row.id, gate.id, gate.confirmation_token or "")
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        finished = session.get_one(Job, job.id)
        persisted_run = session.get_one(Run, run.id)
        metrics = session.scalars(select(RunMetric).where(RunMetric.run_id == run.id)).all()
    assert finished.state == JobState.SUCCEEDED
    assert persisted_run.manifest_hash
    assert {metric.metric for metric in metrics} >= {"wape", "mae", "rmse", "bias", "coverage"}
    assert {metric.role for metric in metrics} >= {"calibration", "test"}
    manifest = json.loads((settings.managed_root / "runs" / run.id / "manifest.json").read_text())
    assert manifest["spec_hash"] == spec_row.spec_hash
    assert manifest["command"][1:3] == ["-m", "vonavy_agent.executor"]
    exported = create_static_export(engine, settings, "export-test", [run.id])
    with zipfile.ZipFile(settings.managed_root / exported["relative_path"]) as archive:
        assert set(archive.namelist()) == {"index.html", "report.json", "manifest.json"}
        index = archive.read("index.html").decode()
        assert "NOTINO / Interview Assignment" in index
        assert "https://" not in index

    failed_run, failed_job = enqueue_run(
        engine, spec_row.id, gate.id, gate.confirmation_token or ""
    )
    with session_scope(engine) as session:
        session.get_one(Job, failed_job.id).kind = "unsupported"
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        assert session.get_one(Job, failed_job.id).state == JobState.FAILED
    failed_manifest = json.loads(
        (settings.managed_root / "runs" / failed_run.id / "manifest.json").read_text()
    )
    assert failed_manifest["status"] == "failed"
    assert failed_manifest["errors"][0]["code"] == "unsupported_job_kind"

    cancelled_run, cancelled_job = enqueue_run(
        engine, spec_row.id, gate.id, gate.confirmation_token or ""
    )
    request_cancellation(engine, cancelled_job.id, settings)
    cancelled_manifest = json.loads(
        (settings.managed_root / "runs" / cancelled_run.id / "manifest.json").read_text()
    )
    assert cancelled_manifest["status"] == "cancelled"


def test_ridge_predictions_do_not_use_future_targets(evidence) -> None:
    _, _, registry, version, mapping_row, profile = evidence
    mapping = DatasetMappingSpec.model_validate_json(mapping_row.canonical_json)
    with Session(registry.engine) as session:
        path = registry.materialized_path(session, version.id)
    frame = pd.read_parquet(path)
    frame["_date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["_entity"] = frame["store"].astype("string")
    frame["_target"] = frame["demand"].astype(float)
    frame["_target_available"] = frame["_date"] + pd.Timedelta(days=1)
    frame["_observation_available"] = True
    from conftest import make_spec

    spec = make_spec(version, mapping_row, profile, models=(RidgeDirectConfig(),))
    origin = spec.origins[0].date
    prepared = PreparedData(frame.copy(), mapping, spec)
    original = _ridge_predictions(prepared, origin, 1, RidgeDirectConfig(), 42)
    changed = frame.copy()
    changed.loc[changed["_date"] > pd.Timestamp(origin), "_target"] += 100_000
    perturbed = _ridge_predictions(
        PreparedData(changed, mapping, spec), origin, 1, RidgeDirectConfig(), 42
    )
    assert perturbed == pytest.approx(original)


def test_worker_recovery_cancellation_and_adapter_safety(runtime) -> None:
    settings, engine, _ = runtime
    queued = enqueue_job(engine, "unsupported", {})
    cancelled = request_cancellation(engine, queued.id)
    assert cancelled.state == JobState.CANCELLED
    stale = enqueue_job(engine, "unsupported", {})
    with session_scope(engine) as session:
        row = session.get_one(Job, stale.id)
        row.state = JobState.RUNNING
        row.attempt = 1
        row.worker_id = "dead-worker"
        row.lease_token = "dead-token"
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    Worker(settings, engine).recover()
    with Session(engine) as session:
        assert session.get_one(Job, stale.id).state == JobState.QUEUED

    root = settings.managed_root.resolve()
    safe = PreparedInvocation(
        executable=sys.executable,
        argv=("-m", "vonavy_agent.adapters_chronos", "--capabilities"),
        cwd=str(root),
        timeout_seconds=10,
    )
    result = dry_run_invocation(safe, allowed_executables={Path(sys.executable)}, allowed_root=root)
    assert result["executed"] is False
    with pytest.raises(AgentError, match="shell fragment"):
        dry_run_invocation(
            safe.model_copy(update={"argv": ("--ok;rm",)}),
            allowed_executables={Path(sys.executable)},
            allowed_root=root,
        )


def test_planner_is_deterministic_and_does_not_enqueue(evidence, spec_row) -> None:
    _, engine, _, _, _, _ = evidence
    first = propose_experiments(engine, spec_row.id)
    second = propose_experiments(engine, spec_row.id)
    assert first.canonical_json == second.canonical_json
    payload = json.loads(first.canonical_json)
    assert payload["proposals"][0]["kind"] == "baseline_first"
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(Job).where(Job.kind == "experiment"))
            == 0
        )
