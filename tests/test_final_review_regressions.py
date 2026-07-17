from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pytest
from conftest import make_spec, synthetic_frame
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import vonavy_agent.jobs as jobs_module
from vonavy_agent.adapters import import_adapter_snapshot
from vonavy_agent.datasets import (
    build_profile,
    compute_profile,
    publish_profile,
)
from vonavy_agent.domain import (
    AvailabilityKind,
    AvailabilityPolicy,
    DatasetMappingSpec,
    JobState,
    MovingAverageConfig,
    SeasonalNaiveConfig,
)
from vonavy_agent.errors import AgentError
from vonavy_agent.executor import ExecutionContext, LeaseLost
from vonavy_agent.experiments import create_experiment_spec, run_gate
from vonavy_agent.exporting import create_static_export, safe_embedded_json
from vonavy_agent.jobs import (
    OUTPUT_LIMIT_BYTES,
    Worker,
    enqueue_export,
    enqueue_job,
    enqueue_run,
)
from vonavy_agent.managed_files import verified_managed_file
from vonavy_agent.persistence import (
    AdapterSnapshot,
    DataProfile,
    Export,
    Job,
    Run,
    RunMetric,
    new_id,
    session_scope,
)
from vonavy_agent.planner import propose_experiments


def test_stale_executor_cannot_publish_after_lease_recovery(evidence) -> None:
    settings, engine, registry, version, mapping, _ = evidence
    job = enqueue_job(
        engine,
        "profile",
        {"dataset_version_id": version.id, "mapping_id": mapping.id},
    )
    worker = Worker(settings, engine)
    claim = worker._claim()
    assert claim is not None
    context = ExecutionContext(
        settings,
        claim.job_id,
        worker.worker_id,
        claim.lease_token,
        os.getppid(),
    )
    computation = compute_profile(registry, version.id, mapping.id, 10)
    context.begin_publish()
    stage = settings.managed_root / "jobs" / "tmp" / job.id / claim.lease_token
    stage.mkdir(parents=True)
    (stage / "partial").write_text("not visible")
    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(DataProfile))
    with session_scope(engine) as session:
        session.get_one(Job, job.id).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    worker.recover()
    with pytest.raises(LeaseLost):
        context.finalize(
            {"profile_id": "stale-profile"},
            lambda session: publish_profile(session, computation, "stale-profile"),
        )
    with Session(engine) as session:
        assert session.get_one(Job, job.id).state == JobState.FAILED
        assert session.scalar(select(func.count()).select_from(DataProfile)) == before
    assert not stage.exists()


def test_final_ownership_transaction_rolls_back_evidence_together(evidence) -> None:
    settings, engine, registry, version, mapping, _ = evidence
    job = enqueue_job(
        engine,
        "profile",
        {"dataset_version_id": version.id, "mapping_id": mapping.id},
    )
    worker = Worker(settings, engine)
    claim = worker._claim()
    assert claim is not None
    context = ExecutionContext(
        settings,
        claim.job_id,
        worker.worker_id,
        claim.lease_token,
        os.getppid(),
    )
    computation = compute_profile(registry, version.id, mapping.id, 10)
    context.begin_publish()

    def fail_after_insert(session) -> object:
        publish_profile(session, computation, "rolled-back-profile")
        raise RuntimeError("force transaction rollback")

    with pytest.raises(RuntimeError, match="force transaction rollback"):
        context.finalize({"profile_id": "rolled-back-profile"}, fail_after_insert)
    with Session(engine) as session:
        persisted = session.get_one(Job, job.id)
        assert persisted.state == JobState.PUBLISHING
        assert persisted.result_json is None
        assert session.get(DataProfile, "rolled-back-profile") is None


def test_target_information_availability_rejects_future_label_shortcuts(runtime) -> None:
    _, engine, registry = runtime
    with pytest.raises(ValidationError, match="target availability"):
        DatasetMappingSpec(
            timestamp_column="date",
            entity_column="store",
            target_column="demand",
            target_availability=AvailabilityPolicy(kind=AvailabilityKind.ALWAYS),
        )
    with pytest.raises(ValidationError, match="target availability"):
        DatasetMappingSpec(
            timestamp_column="date",
            entity_column="store",
            target_column="demand",
            target_availability=AvailabilityPolicy(kind=AvailabilityKind.ORIGIN),
        )

    frame = synthetic_frame()
    frame["target_known_at"] = frame["date"]
    version = registry.ingest_stream(
        io.BytesIO(frame.to_csv(index=False).encode()),
        "premature-target.csv",
        "Premature target",
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
        make_spec(version, mapping, profile, models=(SeasonalNaiveConfig(),)),
    )
    report = json.loads(run_gate(engine, registry, spec.id).canonical_json)
    assert "target_availability_precedes_event" in {reason["code"] for reason in report["reasons"]}


def test_product_availability_is_distinct_and_controls_denominators(runtime) -> None:
    settings, engine, registry = runtime
    frame = synthetic_frame()
    frame["product_available"] = True
    unavailable_keys = {
        ("store-a", "2025-03-11"),
        ("store-a", "2025-03-26"),
    }
    for entity, day in unavailable_keys:
        mask = (frame["store"] == entity) & (frame["date"] == day)
        frame.loc[mask, "product_available"] = False
        frame.loc[mask, "demand"] = 0.0
    version = registry.ingest_stream(
        io.BytesIO(frame.to_csv(index=False).encode()),
        "product-availability.csv",
        "Product availability",
    )
    mapping = registry.create_mapping(
        version.id,
        DatasetMappingSpec(
            timestamp_column="date",
            entity_column="store",
            target_column="demand",
            target_availability=AvailabilityPolicy(kind=AvailabilityKind.EVENT_TIME),
            observation_availability_column="product_available",
        ),
    )
    profile = build_profile(registry, version.id, mapping.id, 10)
    profile_json = json.loads(profile.canonical_json)
    assert profile_json["observation_availability"]["unavailable_rows"] == 2
    base = make_spec(
        version,
        mapping,
        profile,
        models=(SeasonalNaiveConfig(),),
    )
    available_only = base.model_copy(update={"scoring_availability_policy": "available_only"})
    spec = create_experiment_spec(engine, available_only)
    gate = run_gate(engine, registry, spec.id)
    gate_json = json.loads(gate.canonical_json)
    assert gate.status == "passed"
    assert gate_json["evidence"]["expected_score_rows"] == 6
    run, _ = enqueue_run(engine, spec.id, gate.id, gate.confirmation_token or "")
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        persisted = session.get_one(Run, run.id)
        summary = json.loads(persisted.summary_json or "{}")
    assert summary["expected_rows"] == {"calibration": 3, "test": 3}
    predictions = pd.read_parquet(settings.managed_root / "runs" / run.id / "predictions.parquet")
    assert not any((row.entity, row.date) in unavailable_keys for row in predictions.itertuples())

    required = create_experiment_spec(
        engine,
        base.model_copy(update={"scoring_availability_policy": "require_available"}),
    )
    required_report = json.loads(run_gate(engine, registry, required.id).canonical_json)
    assert "required_observation_unavailable" in {
        reason["code"] for reason in required_report["reasons"]
    }
    mismatched = create_experiment_spec(engine, base)
    mismatch_report = json.loads(run_gate(engine, registry, mismatched.id).canonical_json)
    assert "observation_availability_policy_mismatch" in {
        reason["code"] for reason in mismatch_report["reasons"]
    }


def test_adapter_snapshot_publication_rejects_symlink_and_corrupt_winner(
    runtime,
) -> None:
    settings, engine, _ = runtime
    payload = canonical_adapter_snapshot()
    content = json.dumps(payload).encode()
    source_hash = sha256(content).hexdigest()
    destination = settings.managed_root / "imports" / f"{source_hash}.json"
    outside = settings.managed_root.parent / "outside-adapter.json"
    outside.write_text("outside")
    destination.symlink_to(outside)
    with pytest.raises(AgentError, match="safe managed file"):
        import_adapter_snapshot(engine, settings, io.BytesIO(content), "capability.json")
    assert outside.read_text() == "outside"
    destination.unlink()
    destination.write_bytes(b"corrupt")
    with pytest.raises(AgentError, match=r"artifact .* is invalid"):
        import_adapter_snapshot(engine, settings, io.BytesIO(content), "capability.json")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AdapterSnapshot)) == 0


def canonical_adapter_snapshot() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "manifest_kind": "capability",
        "adapter_kind": "chronos",
        "adapter_version": "test",
        "available": False,
        "unavailable_reason": "test fixture",
    }


def test_embedded_report_json_escapes_mixed_case_script_terminators() -> None:
    embedded = safe_embedded_json(json.dumps({"value": "</SCRIPT><ScRiPt>alert(1)</sCrIpT>"}))
    assert "<" not in embedded
    assert "\\u003c/SCRIPT>" in embedded


def test_export_verifies_run_manifest_and_zip_hash(evidence, spec_row) -> None:
    settings, engine, registry, _, _, _ = evidence
    gate = run_gate(engine, registry, spec_row.id)
    run, _ = enqueue_run(engine, spec_row.id, gate.id, gate.confirmation_token or "")
    assert Worker(settings, engine).run_once()
    manifest = settings.managed_root / "runs" / run.id / "manifest.json"
    manifest.write_text("{}")
    with pytest.raises(AgentError, match="hash is invalid"):
        create_static_export(engine, settings, "corrupt-run", [run.id])


def test_export_enqueue_is_atomic_and_download_hash_is_verified(evidence, spec_row) -> None:
    settings, engine, registry, _, _, _ = evidence
    gate = run_gate(engine, registry, spec_row.id)
    run, _ = enqueue_run(engine, spec_row.id, gate.id, gate.confirmation_token or "")
    assert Worker(settings, engine).run_once()
    export_id = new_id()
    export, job = enqueue_export(engine, export_id, [run.id])
    with Session(engine) as session:
        assert session.get_one(Job, job.id)
        assert session.get_one(Export, export.id)
    with pytest.raises(IntegrityError):
        enqueue_export(engine, export_id, [run.id])
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(Job).where(Job.kind == "export")) == 1
        )
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        published = session.get_one(Export, export_id)
        assert published.relative_path and published.manifest_hash
        relative = Path(published.relative_path)
        expected_hash = published.manifest_hash
    path = settings.managed_root / relative
    path.write_bytes(b"corrupt zip")
    with (
        pytest.raises(AgentError, match="hash is invalid"),
        verified_managed_file(settings, relative, expected_hash),
    ):
        pass


def test_fast_oversized_child_output_cannot_succeed(runtime, monkeypatch) -> None:
    settings, engine, _ = runtime
    job = enqueue_job(engine, "unsupported", {})
    worker = Worker(settings, engine)
    claim = worker._claim()
    assert claim is not None

    class FastProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"x" * (OUTPUT_LIMIT_BYTES + 1))
            self.stderr = io.BytesIO()
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self, timeout=None) -> int:
            return 0

    monkeypatch.setattr(jobs_module.subprocess, "Popen", lambda *args, **kwargs: FastProcess())
    worker._execute(claim)
    with Session(engine) as session:
        persisted = session.get_one(Job, job.id)
        assert persisted.state == JobState.FAILED
        assert json.loads(persisted.error_json or "{}")["code"] == "output_limit"


def test_missing_process_group_still_waits(runtime, monkeypatch) -> None:
    class RaceProcess:
        pid = 12345

        def __init__(self) -> None:
            self.waits = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            return 0

    process = RaceProcess()

    def missing_group(pid, signal_number) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(jobs_module.os, "killpg", missing_group)
    Worker._terminate_and_reap(process)
    assert process.waits >= 1


def test_requested_metrics_are_the_only_public_metrics(evidence) -> None:
    settings, engine, registry, version, mapping, profile = evidence
    spec = make_spec(
        version,
        mapping,
        profile,
        models=(SeasonalNaiveConfig(),),
    ).model_copy(update={"metrics": ("mae",)})
    spec_row = create_experiment_spec(engine, spec)
    gate = run_gate(engine, registry, spec_row.id)
    run, _ = enqueue_run(engine, spec_row.id, gate.id, gate.confirmation_token or "")
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        persisted = session.get_one(Run, run.id)
        metrics = session.scalars(select(RunMetric).where(RunMetric.run_id == run.id)).all()
    assert {metric.metric for metric in metrics} == {"mae"}
    summary = json.loads(persisted.summary_json or "{}")
    assert {metric["metric"] for metric in summary["metrics"]} == {"mae"}
    assert summary["diagnostics"]["common_row_coverage"]


def test_planner_skips_ablation_without_feature_capable_model(
    evidence,
) -> None:
    _, engine, _, version, mapping, profile = evidence
    spec = create_experiment_spec(
        engine,
        make_spec(
            version,
            mapping,
            profile,
            models=(SeasonalNaiveConfig(), MovingAverageConfig()),
        ),
    )
    proposal = json.loads(propose_experiments(engine, spec.id).canonical_json)
    assert "feature_ablation" not in {item["kind"] for item in proposal["proposals"]}
