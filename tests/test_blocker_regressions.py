from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from conftest import make_spec, synthetic_frame
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vonavy_agent.backtest import _metric_records, _source_revision
from vonavy_agent.datasets import build_profile
from vonavy_agent.domain import (
    AvailabilityKind,
    AvailabilityPolicy,
    DatasetMappingSpec,
    ExperimentSpec,
    FeatureMapping,
    FeatureRole,
    JobState,
    MovingAverageConfig,
    RidgeDirectConfig,
    SeasonalNaiveConfig,
)
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import create_experiment_spec, run_gate
from vonavy_agent.jobs import (
    StreamCollector,
    Worker,
    enqueue_job,
    enqueue_run,
    request_cancellation,
)
from vonavy_agent.persistence import (
    Blob,
    DatasetVersion,
    Job,
    Run,
    session_scope,
)
from vonavy_agent.planner import confirm_proposal, propose_experiments
from vonavy_agent.settings import Settings


def test_managed_directories_reject_symlink_children(tmp_path: Path) -> None:
    root = tmp_path / "state"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inbox").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        Settings(managed_root=root).ensure_directories()
    assert not list(outside.iterdir())
    database_root = tmp_path / "database-state"
    database_root.mkdir()
    (database_root / "agent.sqlite3").symlink_to(tmp_path / "outside.sqlite3")
    with pytest.raises(OSError):
        Settings(managed_root=database_root).ensure_directories()


def test_blob_integrity_is_verified_on_every_consumption(evidence) -> None:
    settings, engine, registry, version, _, _ = evidence
    with Session(engine) as session:
        version_row = session.get_one(DatasetVersion, version.id)
        blob = session.get_one(Blob, version_row.materialized_blob_sha256)
        path = settings.managed_root / blob.relative_path
        path.write_bytes(b"x" * blob.byte_size)
        with pytest.raises(AgentError, match="hash is invalid"):
            registry.read_materialized_frame(session, version.id)


def test_inbox_change_is_rejected_before_version_commit(runtime, monkeypatch) -> None:
    settings, engine, registry = runtime
    source = settings.inbox_path / "changing.csv"
    source.write_bytes(synthetic_frame(20).to_csv(index=False).encode())
    original_stage = registry._stage_stream

    def changing_stage(stream, suffix):
        result = original_stage(stream, suffix)
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(registry, "_stage_stream", changing_stage)
    with pytest.raises(AgentError, match="changed while"):
        registry.import_inbox("changing.csv", "Changing")
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(DatasetVersion)) == 0


def test_concurrent_identical_blob_publication_has_one_verified_winner(runtime) -> None:
    settings, engine, registry = runtime
    source = settings.managed_root / "jobs" / "tmp" / "same.csv"
    source.write_bytes(synthetic_frame(10).to_csv(index=False).encode())
    with ThreadPoolExecutor(max_workers=4) as pool:
        blobs = list(pool.map(lambda _: registry._publish_blob(source, "csv"), range(8)))
    assert len({blob.sha256 for blob in blobs}) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Blob)) == 1


def test_profile_encodes_nonfinite_values_and_gate_blocks_them(runtime) -> None:
    _, engine, registry = runtime
    frame = synthetic_frame()
    frame.loc[0, "demand"] = np.inf
    version = registry.ingest_stream(
        io.BytesIO(frame.to_csv(index=False).encode()), "nonfinite.csv", "Nonfinite"
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
    assert "Infinity" not in profile.canonical_json
    assert json.loads(profile.canonical_json)["numeric"]["demand"]["nonfinite_count"] == 1
    spec = make_spec(version, mapping, profile, models=(SeasonalNaiveConfig(),))
    spec_row = create_experiment_spec(engine, spec)
    report = json.loads(run_gate(engine, registry, spec_row.id).canonical_json)
    assert "missing_or_nonfinite_target" in {reason["code"] for reason in report["reasons"]}


def test_claim_cycle_recovers_expired_lease_and_fences_old_worker(runtime) -> None:
    settings, engine, _ = runtime
    stale = enqueue_job(engine, "unsupported", {})
    with session_scope(engine) as session:
        row = session.get_one(Job, stale.id)
        row.state = JobState.RUNNING
        row.worker_id = "old-worker"
        row.lease_token = "old-token"
        row.attempt = 1
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    replacement = Worker(settings, engine)
    claim = replacement._claim()
    assert claim is not None
    assert claim.job_id == stale.id
    assert claim.lease_token != "old-token"
    with session_scope(engine) as session:
        session.get_one(Job, stale.id).lease_token = "newer-token"
    assert not replacement._heartbeat(claim)
    replacement._finish_failed(claim, "stale", "must not win")
    with Session(engine) as session:
        assert session.get_one(Job, stale.id).state == JobState.RUNNING


def test_claim_and_cancel_race_never_runs_a_cancelled_queue(runtime) -> None:
    settings, engine, _ = runtime
    for _ in range(10):
        job = enqueue_job(engine, "unsupported", {})
        worker = Worker(settings, engine)
        barrier = threading.Barrier(2)
        claimed = []
        cancellation = []

        def claim(
            current_barrier=barrier,
            current_worker=worker,
            current_claimed=claimed,
        ) -> None:
            current_barrier.wait()
            current_claimed.append(current_worker._claim())

        def cancel(
            current_barrier=barrier,
            current_job=job,
            current_cancellation=cancellation,
        ) -> None:
            current_barrier.wait()
            current_cancellation.append(request_cancellation(engine, current_job.id, settings))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda function: function(), (claim, cancel)))
        with Session(engine) as session:
            current = session.get_one(Job, job.id)
            if claimed[0] is None:
                assert current.state == JobState.CANCELLED
            else:
                assert current.state == JobState.CANCELLING
                worker._finish_cancelled(claimed[0], "cancelled_by_user")


def test_all_job_kinds_use_fenced_subprocess_path(evidence, spec_row) -> None:
    settings, engine, _, version, mapping, _ = evidence
    profile_job = enqueue_job(
        engine,
        "profile",
        {"dataset_version_id": version.id, "mapping_id": mapping.id},
    )
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        finished_profile = session.get_one(Job, profile_job.id)
        assert finished_profile.state == JobState.SUCCEEDED
        assert finished_profile.lease_token is None
    gate_job = enqueue_job(engine, "gate", {"spec_id": spec_row.id})
    assert Worker(settings, engine).run_once()
    with Session(engine) as session:
        finished_gate = session.get_one(Job, gate_job.id)
        assert finished_gate.state == JobState.SUCCEEDED
        assert finished_gate.lease_token is None


def test_stream_collector_drains_beyond_limit_without_blocking() -> None:
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    collector = StreamCollector(reader, limit=1024)
    collector.start()

    def write() -> None:
        with os.fdopen(write_fd, "wb") as output:
            output.write(b"x" * (2 * 1024 * 1024))

    thread = threading.Thread(target=write)
    thread.start()
    thread.join(timeout=5)
    collector.join()
    reader.close()
    assert not thread.is_alive()
    assert collector.overflow
    assert len(collector.data) == 1024


def test_process_group_termination_always_reaps() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=os.name == "posix",
    )
    Worker._terminate_and_reap(process)
    assert process.poll() is not None


def test_recovery_terminalises_with_run_evidence(evidence, spec_row) -> None:
    settings, engine, registry, _, _, _ = evidence
    gate = run_gate(engine, registry, spec_row.id)
    run, job = enqueue_run(engine, spec_row.id, gate.id, gate.confirmation_token or "")
    with session_scope(engine) as session:
        row = session.get_one(Job, job.id)
        row.state = JobState.RUNNING
        row.worker_id = "dead"
        row.lease_token = "dead-token"
        row.attempt = settings.worker_max_attempts
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    Worker(settings, engine).recover()
    with Session(engine) as session:
        assert session.get_one(Job, job.id).state == JobState.FAILED
        assert session.get_one(Run, run.id).manifest_hash
    manifest = json.loads((settings.managed_root / "runs" / run.id / "manifest.json").read_text())
    assert manifest["errors"][0]["code"] == "worker_interrupted"


def test_metrics_keep_missing_models_and_use_group_denominators() -> None:
    rows = []
    for model in ("a", "b"):
        for horizon in (1, 2):
            for entity in ("x", "y"):
                rows.append(
                    {
                        "role": "calibration",
                        "seed": 42,
                        "origin": "2025-01-01",
                        "horizon": horizon,
                        "entity": entity,
                        "date": f"2025-01-0{horizon}",
                        "model": model,
                        "prediction": 10.0,
                        "actual": 10.0,
                    }
                )
    predictions = pd.DataFrame(rows)
    metrics = _metric_records(
        predictions,
        {"calibration": 4},
        {("calibration", "2025-01-01"): 4},
        {("calibration", 1): 2, ("calibration", 2): 2},
        ("a", "b"),
        (42,),
    )
    grouped_coverage = [
        item["value"]
        for item in metrics
        if item["metric"] == "coverage"
        and (item["origin"] is not None or item["horizon"] is not None)
    ]
    assert grouped_coverage and set(grouped_coverage) == {1.0}
    unsupported = _metric_records(
        predictions[predictions["model"] == "a"],
        {"calibration": 4},
        {("calibration", "2025-01-01"): 4},
        {("calibration", 1): 2, ("calibration", 2): 2},
        ("a", "missing"),
        (42,),
    )
    missing_wape = next(
        item
        for item in unsupported
        if item["model"] == "missing"
        and item["metric"] == "wape"
        and item["origin"] is None
        and item["horizon"] is None
    )
    assert missing_wape["value"] is None
    assert missing_wape["unsupported_reason"] == "no_common_prediction_rows"


def test_source_revision_hashes_complete_executed_tree() -> None:
    source = _source_revision()
    assert source["source_tree_hash"]
    assert source["source_file_count"] == (
        source["tracked_file_count"] + source["untracked_file_count"]
    )


def test_planner_ablation_preserves_mapping_and_passes_gate(evidence, spec_row) -> None:
    _, engine, registry, _, _, _ = evidence
    proposal = propose_experiments(engine, spec_row.id)
    payload = json.loads(proposal.canonical_json)
    rank = next(item["rank"] for item in payload["proposals"] if item["kind"] == "feature_ablation")
    ablated = confirm_proposal(engine, proposal.id, rank)
    spec = ExperimentSpec.model_validate_json(ablated.canonical_json)
    assert spec.feature_allow_list == ()
    assert spec.features
    assert run_gate(engine, registry, ablated.id).status == "passed"


def test_gate_blocks_all_feature_availability_failures(runtime) -> None:
    _, engine, registry = runtime
    frame = synthetic_frame()
    frame["past_available"] = frame["date"]
    frame.loc[0, "past_available"] = "invalid"
    frame["static_available"] = "2030-01-01"
    version = registry.ingest_stream(
        io.BytesIO(frame.to_csv(index=False).encode()), "availability.csv", "Availability"
    )
    mapping = registry.create_mapping(
        version.id,
        DatasetMappingSpec(
            timestamp_column="date",
            entity_column="store",
            target_column="demand",
            target_availability=AvailabilityPolicy(kind=AvailabilityKind.EVENT_TIME),
            features=(
                FeatureMapping(
                    name="promotion",
                    role=FeatureRole.PAST_ONLY,
                    availability=AvailabilityPolicy(
                        kind=AvailabilityKind.COLUMN,
                        column="past_available",
                    ),
                ),
                FeatureMapping(
                    name="region",
                    role=FeatureRole.STATIC,
                    availability=AvailabilityPolicy(
                        kind=AvailabilityKind.COLUMN,
                        column="static_available",
                    ),
                ),
            ),
        ),
    )
    profile = build_profile(registry, version.id, mapping.id, 10)
    spec_row = create_experiment_spec(
        engine,
        make_spec(version, mapping, profile, models=(SeasonalNaiveConfig(),)),
    )
    report = json.loads(run_gate(engine, registry, spec_row.id).canonical_json)
    codes = {reason["code"] for reason in report["reasons"]}
    assert "invalid_feature_availability" in codes
    assert "feature_unavailable_at_origin" in codes


def test_gate_blocks_model_infeasibility_and_null_entities(runtime) -> None:
    _, engine, registry = runtime
    frame = synthetic_frame()
    frame.loc[0, "store"] = None
    version = registry.ingest_stream(
        io.BytesIO(frame.to_csv(index=False).encode()), "null-entity.csv", "Null entity"
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
    null_spec = create_experiment_spec(
        engine,
        make_spec(version, mapping, profile, models=(SeasonalNaiveConfig(),)),
    )
    null_report = json.loads(run_gate(engine, registry, null_spec.id).canonical_json)
    assert "null_entity" in {reason["code"] for reason in null_report["reasons"]}

    clean_version = registry.ingest_stream(
        io.BytesIO(synthetic_frame().to_csv(index=False).encode()),
        "short-history.csv",
        "Short history",
    )
    clean_mapping = registry.create_mapping(
        clean_version.id,
        DatasetMappingSpec(
            timestamp_column="date",
            entity_column="store",
            target_column="demand",
            target_availability=AvailabilityPolicy(kind=AvailabilityKind.EVENT_TIME),
        ),
    )
    clean_profile = build_profile(registry, clean_version.id, clean_mapping.id, 10)
    infeasible = create_experiment_spec(
        engine,
        make_spec(
            clean_version,
            clean_mapping,
            clean_profile,
            models=(MovingAverageConfig(window_days=100),),
        ),
    )
    report = json.loads(run_gate(engine, registry, infeasible.id).canonical_json)
    assert "model_infeasible" in {reason["code"] for reason in report["reasons"]}


def test_spec_rejects_naive_cutoff_and_empty_ridge_windows(evidence) -> None:
    _, _, _, version, mapping, profile = evidence
    valid = make_spec(version, mapping, profile)
    with pytest.raises(ValidationError, match="explicit timezone"):
        ExperimentSpec.model_validate(
            {**valid.model_dump(mode="python"), "evaluation_as_of": datetime(2025, 5, 2)}
        )
    with pytest.raises(ValidationError):
        RidgeDirectConfig(lag_days=(), rolling_days=())
    with pytest.raises(ValidationError):
        RidgeDirectConfig(alpha=float("inf"))
