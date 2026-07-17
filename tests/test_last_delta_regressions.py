from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta

import pytest
from conftest import make_spec, synthetic_frame
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from vonavy_agent.api import create_app
from vonavy_agent.datasets import build_profile
from vonavy_agent.domain import (
    AvailabilityKind,
    AvailabilityPolicy,
    DatasetMappingSpec,
    JobState,
    SeasonalNaiveConfig,
)
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import create_experiment_spec, run_gate
from vonavy_agent.exporting import create_static_export
from vonavy_agent.hashing import canonical_json
from vonavy_agent.jobs import (
    Worker,
    enqueue_export,
    enqueue_run,
)
from vonavy_agent.persistence import (
    GateResultRow,
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


def expire_as(
    engine,
    job_id: str,
    state: JobState,
    token: str,
) -> None:
    with session_scope(engine) as session:
        job = session.get_one(Job, job_id)
        job.state = state.value
        job.worker_id = "dead-worker"
        job.lease_token = token
        job.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)


def test_finalizing_recovery_promotes_verified_stage_before_success(evidence, spec_row) -> None:
    settings, engine, run, job = successful_run(evidence, spec_row)
    with Session(engine) as session:
        result = json.loads(session.get_one(Job, job.id).result_json or "{}")
    final = settings.managed_root / "runs" / run.id
    staging = settings.managed_root / result["staging_relative_path"]
    staging.parent.mkdir(parents=True, exist_ok=True)
    os.rename(final, staging)
    expire_as(engine, job.id, JobState.FINALIZING, "expired-finalizer")
    Worker(settings, engine).recover()
    with Session(engine) as session:
        assert session.get_one(Job, job.id).state == JobState.SUCCEEDED
    assert (final / "manifest.json").is_file()
    assert not staging.exists()


def test_finalizing_recovery_fails_and_invalidates_corrupt_evidence(evidence, spec_row) -> None:
    settings, engine, run, job = successful_run(evidence, spec_row)
    (settings.managed_root / "runs" / run.id / "manifest.json").write_text("corrupt")
    expire_as(engine, job.id, JobState.FINALIZING, "corrupt-finalizer")
    Worker(settings, engine).recover()
    with Session(engine) as session:
        persisted_job = session.get_one(Job, job.id)
        persisted_run = session.get_one(Run, run.id)
        assert persisted_job.state == JobState.FAILED
        assert persisted_run.summary_json is None
        assert session.scalars(select(RunMetric).where(RunMetric.run_id == run.id)).all() == []
    terminal_manifest = json.loads(
        (settings.managed_root / "runs" / run.id / "manifest.json").read_text()
    )
    assert terminal_manifest["status"] == "failed"
    assert terminal_manifest["errors"][0]["code"] == "artifact_promotion_failed"


def test_finalizing_run_and_export_evidence_is_not_public(evidence, spec_row) -> None:
    settings, engine, run, job = successful_run(evidence, spec_row)
    export_id = new_id()
    export, export_job = enqueue_export(engine, export_id, [run.id])
    assert Worker(settings, engine).run_once()
    with session_scope(engine) as session:
        session.get_one(Job, job.id).state = JobState.FINALIZING.value
        session.get_one(Job, export_job.id).state = JobState.FINALIZING.value
    with TestClient(create_app(settings)) as client:
        run_response = client.get(f"/api/runs/{run.id}")
        assert run_response.status_code == 200
        assert run_response.json()["summary"] is None
        assert run_response.json()["manifest_hash"] is None
        job_response = client.get(f"/api/jobs/{job.id}")
        assert job_response.json()["result"] is None
        comparison = client.post("/api/comparisons", json={"run_ids": [run.id]})
        assert comparison.status_code == 400
        export_response = client.get(f"/api/exports/{export.id}")
        assert export_response.json()["download_ready"] is False
        download = client.get(f"/api/exports/{export.id}/download")
        assert download.status_code == 409
    with pytest.raises(AgentError, match="not a successful"):
        create_static_export(engine, settings, "hidden-run", [run.id])


def test_legacy_publishing_recovers_only_complete_verified_evidence(evidence, spec_row) -> None:
    settings, engine, _, job = successful_run(evidence, spec_row)
    expire_as(engine, job.id, JobState.PUBLISHING, "legacy-complete")
    Worker(settings, engine).recover()
    with Session(engine) as session:
        assert session.get_one(Job, job.id).state == JobState.SUCCEEDED

    second_gate = run_gate(engine, evidence[2], spec_row.id)
    incomplete_run, incomplete_job = enqueue_run(
        engine,
        spec_row.id,
        second_gate.id,
        second_gate.confirmation_token or "",
    )
    with session_scope(engine) as session:
        persisted = session.get_one(Job, incomplete_job.id)
        persisted.state = JobState.PUBLISHING.value
        persisted.worker_id = "legacy-worker"
        persisted.lease_token = "legacy-incomplete"
        persisted.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        persisted.result_json = canonical_json(
            {"run_id": incomplete_run.id, "summary": {"fake": True}}
        )
    Worker(settings, engine).recover()
    with Session(engine) as session:
        assert session.get_one(Job, incomplete_job.id).state == JobState.FAILED
        assert session.get_one(Run, incomplete_run.id).summary_json is None


def test_legacy_passing_gate_cannot_bypass_current_target_policy(runtime) -> None:
    _, engine, registry = runtime
    frame = synthetic_frame()
    frame["target_known_at"] = frame["date"]
    version = registry.ingest_stream(
        io.BytesIO(frame.to_csv(index=False).encode()),
        "legacy-gate.csv",
        "Legacy gate",
    )
    mapping = registry.create_mapping(
        version.id,
        DatasetMappingSpec(
            timestamp_column="date",
            entity_column="store",
            target_column="demand",
            target_availability=AvailabilityPolicy(
                kind=AvailabilityKind.COLUMN,
                column="target_known_at",
            ),
        ),
    )
    profile = build_profile(registry, version.id, mapping.id, 10)
    spec = create_experiment_spec(
        engine,
        make_spec(
            version,
            mapping,
            profile,
            models=(SeasonalNaiveConfig(),),
        ),
    )
    with session_scope(engine) as session:
        legacy = GateResultRow(
            spec_id=spec.id,
            spec_hash=spec.spec_hash,
            profile_hash=profile.profile_hash,
            status="passed",
            canonical_json=canonical_json(
                {
                    "schema_version": "1.0",
                    "status": "passed",
                    "reasons": [],
                    "warnings": [],
                    "evidence": {},
                    "confirmation_token": "legacy-token",
                }
            ),
            confirmation_token="legacy-token",
        )
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id
    with pytest.raises(AgentError, match="recompute"):
        enqueue_run(engine, spec.id, legacy_id, "legacy-token")


def test_absent_forecast_row_remains_in_gate_and_run_denominator(
    runtime,
) -> None:
    settings, engine, registry = runtime
    frame = synthetic_frame()
    missing = (frame["store"] == "store-b") & (frame["date"] == "2025-03-11")
    frame = frame.loc[~missing].reset_index(drop=True)
    version = registry.ingest_stream(
        io.BytesIO(frame.to_csv(index=False).encode()),
        "missing-forecast.csv",
        "Missing forecast",
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
    spec = create_experiment_spec(
        engine,
        make_spec(
            version,
            mapping,
            profile,
            models=(SeasonalNaiveConfig(),),
        ).model_copy(update={"minimum_coverage": 0.5}),
    )
    blocked_gate = run_gate(engine, registry, spec.id)
    gate_report = json.loads(blocked_gate.canonical_json)
    assert gate_report["evidence"]["expected_score_rows"] == 8
    assert gate_report["evidence"]["eligible_score_rows"] == 7

    token = "current-policy-test-token"
    with session_scope(engine) as session:
        forced = GateResultRow(
            spec_id=spec.id,
            spec_hash=spec.spec_hash,
            profile_hash=profile.profile_hash,
            status="passed",
            canonical_json=canonical_json(
                {
                    "schema_version": "1.0",
                    "policy_version": "2",
                    "status": "passed",
                    "reasons": [],
                    "warnings": [],
                    "evidence": {},
                    "confirmation_token": token,
                }
            ),
            confirmation_token=token,
        )
        session.add(forced)
        session.flush()
        forced_id = forced.id
    run, _ = enqueue_run(engine, spec.id, forced_id, token)
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        summary = json.loads(session.get_one(Run, run.id).summary_json or "{}")
    assert summary["expected_rows"]["calibration"] == 4
    calibration_coverage = next(
        metric
        for metric in summary["metrics"]
        if metric["role"] == "calibration"
        and metric["metric"] == "coverage"
        and metric["origin"] is None
        and metric["horizon"] is None
    )
    assert calibration_coverage["coverage"] == pytest.approx(0.75)
