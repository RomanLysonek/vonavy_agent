from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session

from vonavy_agent.domain import (
    CURRENT_GATE_POLICY_VERSION,
    TERMINAL_JOB_STATES,
    JobState,
)
from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_json, file_hash
from vonavy_agent.managed_files import verified_managed_file, verify_run_bundle
from vonavy_agent.persistence import (
    DataProfile,
    DatasetMapping,
    DatasetVersion,
    ExperimentSpecRow,
    Export,
    GateResultRow,
    Job,
    JobEvent,
    Run,
    RunMetric,
    session_scope,
)
from vonavy_agent.settings import Settings

OUTPUT_LIMIT_BYTES = 1_000_000


@dataclass(frozen=True)
class Claim:
    job_id: str
    lease_token: str


class StreamCollector:
    def __init__(self, stream: IO[bytes], limit: int = OUTPUT_LIMIT_BYTES) -> None:
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.overflow = False
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self) -> None:
        self.thread.join(timeout=5)

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")

    def _drain(self) -> None:
        while chunk := self.stream.read(64 * 1024):
            remaining = self.limit - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.overflow = True


def enqueue_job(engine: Engine, kind: str, payload: dict[str, Any]) -> Job:
    with session_scope(engine) as session:
        job = Job(kind=kind, payload_json=canonical_json(payload))
        session.add(job)
        session.flush()
        session.add(JobEvent(job_id=job.id, from_state=None, to_state=JobState.QUEUED.value))
        job_id = job.id
    with Session(engine) as session:
        return session.get_one(Job, job_id)


def enqueue_run(
    engine: Engine,
    spec_id: str,
    gate_result_id: str,
    confirmation_token: str,
) -> tuple[Run, Job]:
    with session_scope(engine) as session:
        gate = session.get(GateResultRow, gate_result_id)
        if (
            gate is None
            or gate.spec_id != spec_id
            or gate.status != "passed"
            or gate.confirmation_token != confirmation_token
        ):
            raise AgentError(
                "gate_confirmation_invalid",
                "A current passing gate and its exact confirmation token are required",
            )
        gate_payload = json.loads(gate.canonical_json)
        if gate_payload.get("policy_version") != CURRENT_GATE_POLICY_VERSION:
            raise AgentError(
                "gate_policy_legacy",
                "Gate evidence predates current leakage and availability policy; recompute it",
            )
        job = Job(kind="experiment", payload_json="{}")
        session.add(job)
        session.flush()
        run = Run(job_id=job.id, spec_id=spec_id, gate_result_id=gate_result_id)
        session.add(run)
        session.flush()
        job.payload_json = canonical_json({"run_id": run.id})
        session.add(JobEvent(job_id=job.id, from_state=None, to_state=JobState.QUEUED.value))
        run_id, job_id = run.id, job.id
    with Session(engine) as session:
        return session.get_one(Run, run_id), session.get_one(Job, job_id)


def enqueue_export(
    engine: Engine,
    export_id: str,
    run_ids: list[str],
) -> tuple[Export, Job]:
    unique_run_ids = sorted(set(run_ids))
    with session_scope(engine) as session:
        job = Job(kind="export", payload_json="{}")
        session.add(job)
        session.flush()
        export = Export(
            id=export_id,
            job_id=job.id,
            run_ids_json=canonical_json(unique_run_ids),
        )
        session.add(export)
        job.payload_json = canonical_json({"export_id": export_id, "run_ids": unique_run_ids})
        session.add(
            JobEvent(
                job_id=job.id,
                from_state=None,
                to_state=JobState.QUEUED.value,
            )
        )
        job_id = job.id
    with Session(engine) as session:
        return session.get_one(Export, export_id), session.get_one(Job, job_id)


def request_cancellation(engine: Engine, job_id: str, settings: Settings | None = None) -> Job:
    cancelled_while_queued = False
    for _ in range(3):
        with session_scope(engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise AgentError("job_not_found", "Job does not exist", status_code=404)
            state = JobState(job.state)
            if state in TERMINAL_JOB_STATES:
                raise AgentError("job_terminal", "Terminal jobs cannot be cancelled")
            if state in {JobState.PUBLISHING, JobState.FINALIZING}:
                raise AgentError(
                    "job_publishing",
                    "Job evidence is already being atomically published",
                    status_code=409,
                )
            target = JobState.CANCELLED if state == JobState.QUEUED else JobState.CANCELLING
            cancellation_values: dict[str, Any] = {
                "state": target.value,
                "cancel_requested": True,
                "updated_at": datetime.utcnow(),
            }
            if target == JobState.CANCELLED:
                cancellation_values.update(
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                )
            changed = cast(
                CursorResult[Any],
                session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.state == state.value)
                    .values(**cancellation_values)
                ),
            )
            if changed.rowcount != 1:
                continue
            session.add(
                JobEvent(
                    job_id=job_id,
                    from_state=state.value,
                    to_state=target.value,
                    detail_json=canonical_json({"reason": "cancel_requested"}),
                )
            )
            cancelled_while_queued = target == JobState.CANCELLED
            break
    else:
        raise AgentError(
            "job_state_race", "Job state changed repeatedly; retry cancellation", status_code=409
        )
    if cancelled_while_queued and settings is not None:
        Worker(settings, engine)._record_terminal_run(
            job_id, "cancelled", {"code": "cancelled_by_user"}
        )
    with Session(engine) as session:
        return session.get_one(Job, job_id)


class Worker:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.worker_id = f"{uuid.uuid4()}:{os.getpid()}"

    def recover(self) -> None:
        now = datetime.utcnow()
        evidence: list[tuple[str, str, dict[str, Any]]] = []
        stale_paths: list[Path] = []
        finalizing_claims: list[Claim] = []
        with session_scope(self.engine) as session:
            stale = session.scalars(
                select(Job).where(
                    Job.state.in_(
                        [
                            JobState.RUNNING.value,
                            JobState.CANCELLING.value,
                            JobState.PUBLISHING.value,
                            JobState.FINALIZING.value,
                        ]
                    ),
                    Job.lease_expires_at < now,
                )
            ).all()
            for job in stale:
                source = JobState(job.state)
                token = job.lease_token
                if token is None:
                    continue
                if source == JobState.FINALIZING:
                    replacement_token = str(uuid.uuid4())
                    changed = cast(
                        CursorResult[Any],
                        session.execute(
                            update(Job)
                            .where(
                                Job.id == job.id,
                                Job.state == JobState.FINALIZING.value,
                                Job.lease_token == token,
                                Job.lease_expires_at < now,
                            )
                            .values(
                                worker_id=self.worker_id,
                                lease_token=replacement_token,
                                lease_expires_at=now
                                + timedelta(seconds=self.settings.worker_lease_seconds),
                                updated_at=now,
                            )
                        ),
                    )
                    if changed.rowcount == 1:
                        finalizing_claims.append(Claim(job.id, replacement_token))
                    continue
                values: dict[str, Any]
                detail: dict[str, Any]
                legacy_complete = (
                    self._legacy_publishing_complete(session, job)
                    if source == JobState.PUBLISHING
                    else False
                )
                if legacy_complete:
                    target = JobState.SUCCEEDED
                    values = {}
                    detail = {"reason": "recovered_legacy_publishing"}
                elif source == JobState.CANCELLING or job.cancel_requested:
                    target = JobState.CANCELLED
                    values = {}
                    detail = {"reason": "worker_interrupted"}
                elif (
                    job.attempt >= self.settings.worker_max_attempts
                    or source == JobState.PUBLISHING
                ):
                    target = JobState.FAILED
                    error = {"code": "worker_interrupted", "attempts": job.attempt}
                    values = {"error_json": canonical_json(error)}
                    detail = error
                else:
                    target = JobState.QUEUED
                    values = {"cancel_requested": False}
                    detail = {"reason": "lease_expired"}
                changed = cast(
                    CursorResult[Any],
                    session.execute(
                        update(Job)
                        .where(
                            Job.id == job.id,
                            Job.state == source.value,
                            Job.lease_token == token,
                            Job.lease_expires_at < now,
                        )
                        .values(
                            state=target.value,
                            worker_id=None,
                            lease_token=None,
                            lease_expires_at=None,
                            updated_at=now,
                            **values,
                        )
                    ),
                )
                if changed.rowcount != 1:
                    continue
                if target != JobState.SUCCEEDED:
                    stale_paths.append(self.settings.managed_root / "jobs" / "tmp" / job.id / token)
                if source == JobState.PUBLISHING and target == JobState.FAILED:
                    stale_paths.extend(self._invalidate_visible_evidence(session, job))
                session.add(
                    JobEvent(
                        job_id=job.id,
                        from_state=source.value,
                        to_state=target.value,
                        detail_json=canonical_json(detail),
                    )
                )
                if target == JobState.CANCELLED:
                    evidence.append((job.id, "cancelled", {"code": "worker_interrupted"}))
                elif target == JobState.FAILED:
                    evidence.append((job.id, "failed", detail))
        for claim in finalizing_claims:
            self._finish_success(claim)
        self._remove_evidence_paths(stale_paths)
        for job_id, status, error in evidence:
            self._record_terminal_run(job_id, status, error)
        self._repair_terminal_evidence()
        self._repair_published_artifacts()

    def run_once(self) -> bool:
        claim = self._claim()
        if claim is None:
            return False
        self._execute(claim)
        return True

    def run_forever(self) -> None:
        while True:
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)

    def _claim(self) -> Claim | None:
        self.recover()
        now = datetime.utcnow()
        token = str(uuid.uuid4())
        with session_scope(self.engine) as session:
            job_id = session.scalar(
                select(Job.id)
                .where(Job.state == JobState.QUEUED.value, Job.cancel_requested.is_(False))
                .order_by(Job.created_at, Job.id)
                .limit(1)
            )
            if job_id is None:
                return None
            claimed = cast(
                CursorResult[Any],
                session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.state == JobState.QUEUED.value,
                        Job.cancel_requested.is_(False),
                    )
                    .values(
                        state=JobState.RUNNING.value,
                        worker_id=self.worker_id,
                        lease_token=token,
                        attempt=Job.attempt + 1,
                        lease_expires_at=now
                        + timedelta(seconds=self.settings.worker_lease_seconds),
                        updated_at=now,
                    )
                ),
            )
            if claimed.rowcount != 1:
                return None
            session.add(
                JobEvent(
                    job_id=job_id,
                    from_state=JobState.QUEUED.value,
                    to_state=JobState.RUNNING.value,
                    detail_json=canonical_json({"lease_token": token}),
                )
            )
            return Claim(job_id=job_id, lease_token=token)

    def _execute(self, claim: Claim) -> None:
        wall_seconds = self._wall_seconds(claim.job_id)
        command = [
            sys.executable,
            "-m",
            "vonavy_agent.executor",
            "--database",
            str(self.settings.database_path),
            "--managed-root",
            str(self.settings.managed_root),
            "--job-id",
            claim.job_id,
            "--worker-id",
            self.worker_id,
            "--lease-token",
            claim.lease_token,
            "--parent-pid",
            str(os.getpid()),
        ]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONUNBUFFERED": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name == "posix",
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = StreamCollector(process.stdout)
        stderr = StreamCollector(process.stderr)
        stdout.start()
        stderr.start()
        started = time.monotonic()
        outcome: tuple[str, str, str] | None = None
        finalised = False
        try:
            while process.poll() is None:
                time.sleep(0.1)
                if stdout.overflow or stderr.overflow:
                    outcome = ("failed", "output_limit", "Executor output exceeded 1 MB")
                    break
                if time.monotonic() - started > wall_seconds:
                    outcome = ("failed", "wall_timeout", f"Exceeded {wall_seconds} seconds")
                    break
                if not finalised and not self._heartbeat(claim):
                    with Session(self.engine) as session:
                        state = session.get_one(Job, claim.job_id).state
                    if state == JobState.SUCCEEDED.value:
                        finalised = True
                        continue
                    outcome = ("lost", "lease_lost", "Worker no longer owns the job lease")
                    break
                if finalised:
                    continue
                with Session(self.engine) as session:
                    job = session.get_one(Job, claim.job_id)
                    if job.lease_token != claim.lease_token or job.worker_id != self.worker_id:
                        outcome = ("lost", "lease_lost", "Worker no longer owns the job lease")
                        break
                    if job.cancel_requested or job.state == JobState.CANCELLING.value:
                        outcome = ("cancelled", "cancelled_by_user", "Cancellation requested")
                        break
            if outcome is not None and process.poll() is None:
                self._terminate_and_reap(process)
            else:
                process.wait()
        finally:
            if process.poll() is None:
                self._terminate_and_reap(process)
            stdout.join()
            stderr.join()
            process.stdout.close()
            process.stderr.close()
        if stdout.overflow or stderr.overflow:
            self._finish_failed(claim, "output_limit", "Executor output exceeded 1 MB")
            return
        if outcome is not None:
            kind, code, message = outcome
            if kind == "cancelled":
                self._finish_cancelled(claim, code)
            elif kind == "failed":
                self._finish_failed(claim, code, message)
            return
        if process.returncode == 0:
            self._finish_success(claim)
        elif process.returncode == 130:
            self._finish_cancelled(claim, "cancelled_by_user")
        elif process.returncode == 75:
            with Session(self.engine) as session:
                job = session.get_one(Job, claim.job_id)
                if job.lease_token == claim.lease_token and job.state == JobState.CANCELLING.value:
                    self._finish_cancelled(claim, "cancelled_by_user")
        else:
            error = self._executor_error(stderr.text())
            self._finish_failed(claim, error["code"], error["message"])

    def _wall_seconds(self, job_id: str) -> int:
        with Session(self.engine) as session:
            job = session.get_one(Job, job_id)
            if job.kind != "experiment":
                return 900
            run = session.get_one(Run, json.loads(job.payload_json)["run_id"])
            spec = session.get_one(ExperimentSpecRow, run.spec_id)
            return int(json.loads(spec.canonical_json)["resources"]["wall_seconds"])

    @staticmethod
    def _executor_error(stderr: str) -> dict[str, str]:
        for line in reversed(stderr.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                error = parsed["error"]
                if isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
                    return {"code": error["code"], "message": error["message"]}
        return {"code": "executor_failed", "message": stderr[-4000:]}

    def _heartbeat(self, claim: Claim) -> bool:
        with session_scope(self.engine) as session:
            changed = cast(
                CursorResult[Any],
                session.execute(
                    update(Job)
                    .where(
                        Job.id == claim.job_id,
                        Job.worker_id == self.worker_id,
                        Job.lease_token == claim.lease_token,
                        Job.state.in_(
                            [
                                JobState.RUNNING.value,
                                JobState.CANCELLING.value,
                                JobState.PUBLISHING.value,
                                JobState.FINALIZING.value,
                            ]
                        ),
                    )
                    .values(
                        lease_expires_at=datetime.utcnow()
                        + timedelta(seconds=self.settings.worker_lease_seconds),
                        updated_at=datetime.utcnow(),
                    )
                ),
            )
            return changed.rowcount == 1

    def _finish_success(self, claim: Claim) -> None:
        try:
            self._promote_claim_artifacts(claim)
        except (AgentError, OSError) as exc:
            self._finish_failed(
                claim,
                "artifact_promotion_failed",
                str(exc),
            )
            return
        with session_scope(self.engine) as session:
            job = session.get_one(Job, claim.job_id)
            if not job.result_json:
                raise AgentError("missing_job_result", "Publishing job did not persist a result")
            transitioned = self._owned_terminal(
                session,
                claim,
                {JobState.FINALIZING},
                JobState.SUCCEEDED,
                {},
                {},
            )
        if not transitioned:
            raise AgentError(
                "final_success_cas_failed",
                "Finalizing job lost ownership before terminal success",
            )

    def _finish_cancelled(self, claim: Claim, code: str) -> None:
        transitioned = False
        with session_scope(self.engine) as session:
            transitioned = self._owned_terminal(
                session,
                claim,
                {JobState.RUNNING, JobState.CANCELLING},
                JobState.CANCELLED,
                {},
                {"code": code},
            )
        if transitioned:
            self._record_terminal_run(claim.job_id, "cancelled", {"code": code})

    def _finish_failed(self, claim: Claim, code: str, message: str) -> None:
        error = {"code": code, "message": message}
        transitioned = False
        was_finalizing = False
        paths: list[Path] = []
        with session_scope(self.engine) as session:
            job = session.get_one(Job, claim.job_id)
            was_finalizing = job.state == JobState.FINALIZING.value
            transitioned = self._owned_terminal(
                session,
                claim,
                {
                    JobState.RUNNING,
                    JobState.CANCELLING,
                    JobState.PUBLISHING,
                    JobState.FINALIZING,
                },
                JobState.FAILED,
                {"error_json": canonical_json(error)},
                error,
            )
            if transitioned and was_finalizing:
                paths = self._invalidate_visible_evidence(session, job)
        if transitioned:
            self._remove_evidence_paths(paths)
            self._record_terminal_run(claim.job_id, "failed", error)

    def _cleanup_visible_evidence(self, job_id: str) -> None:
        with session_scope(self.engine) as session:
            job = session.get_one(Job, job_id)
            paths = self._invalidate_visible_evidence(session, job)
        self._remove_evidence_paths(paths)

    def _invalidate_visible_evidence(self, session: Session, job: Job) -> list[Path]:
        paths: list[Path] = []
        result = json.loads(job.result_json) if job.result_json else {}
        if staging := result.get("staging_relative_path"):
            paths.append(self.settings.managed_root / staging)
        if job.kind == "profile" and result.get("profile_id"):
            session.execute(delete(DataProfile).where(DataProfile.id == result["profile_id"]))
        elif job.kind == "gate" and result.get("gate_result_id"):
            session.execute(
                delete(GateResultRow).where(GateResultRow.id == result["gate_result_id"])
            )
        elif job.kind == "experiment":
            run = session.get_one(Run, json.loads(job.payload_json)["run_id"])
            session.execute(delete(RunMetric).where(RunMetric.run_id == run.id))
            if run.artifact_relative_path:
                paths.append(self.settings.managed_root / run.artifact_relative_path)
            run.artifact_relative_path = None
            run.manifest_hash = None
            run.summary_json = None
        elif job.kind == "export":
            export = session.get_one(Export, json.loads(job.payload_json)["export_id"])
            if export.relative_path:
                paths.append(self.settings.managed_root / export.relative_path)
            export.relative_path = None
            export.manifest_hash = None
        return paths

    @staticmethod
    def _remove_evidence_paths(paths: list[Path]) -> None:
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    def _owned_terminal(
        self,
        session: Session,
        claim: Claim,
        sources: set[JobState],
        target: JobState,
        values: dict[str, Any],
        detail: dict[str, Any],
    ) -> bool:
        current = session.get_one(Job, claim.job_id)
        source = JobState(current.state)
        if source not in sources:
            return False
        changed = cast(
            CursorResult[Any],
            session.execute(
                update(Job)
                .where(
                    Job.id == claim.job_id,
                    Job.state == source.value,
                    Job.worker_id == self.worker_id,
                    Job.lease_token == claim.lease_token,
                )
                .values(
                    state=target.value,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=datetime.utcnow(),
                    **values,
                )
            ),
        )
        if changed.rowcount != 1:
            return False
        session.add(
            JobEvent(
                job_id=claim.job_id,
                from_state=source.value,
                to_state=target.value,
                detail_json=canonical_json(detail),
            )
        )
        return True

    @staticmethod
    def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
        try:
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
        finally:
            process.wait()

    def _legacy_publishing_complete(self, session: Session, job: Job) -> bool:
        if not job.result_json:
            return False
        payload = json.loads(job.payload_json)
        result = json.loads(job.result_json)
        try:
            if job.kind == "experiment":
                run = session.get_one(Run, payload["run_id"])
                if not run.summary_json or not run.manifest_hash or not run.artifact_relative_path:
                    return False
                verify_run_bundle(
                    self.settings,
                    Path(run.artifact_relative_path),
                    run.manifest_hash,
                )
                return True
            if job.kind == "export":
                export = session.get_one(Export, payload["export_id"])
                if not export.relative_path or not export.manifest_hash:
                    return False
                with verified_managed_file(
                    self.settings,
                    Path(export.relative_path),
                    export.manifest_hash,
                    result.get("bytes"),
                ):
                    pass
                return True
            if job.kind == "profile":
                profile_id = result.get("profile_id")
                return (
                    isinstance(profile_id, str) and session.get(DataProfile, profile_id) is not None
                )
            if job.kind == "gate":
                gate_id = result.get("gate_result_id")
                return isinstance(gate_id, str) and session.get(GateResultRow, gate_id) is not None
        except (AgentError, OSError, ValueError):
            return False
        return False

    def _promote_claim_artifacts(self, claim: Claim) -> None:
        with Session(self.engine) as session:
            job = session.get_one(Job, claim.job_id)
            if (
                job.state != JobState.FINALIZING.value
                or job.worker_id != self.worker_id
                or job.lease_token != claim.lease_token
                or not job.result_json
            ):
                raise AgentError(
                    "finalization_lease_lost",
                    "Finalizing artifact lease is no longer owned",
                )
            result = json.loads(job.result_json)
            payload = json.loads(job.payload_json)
            if job.kind == "profile":
                if session.get(DataProfile, result.get("profile_id")) is None:
                    raise AgentError(
                        "profile_evidence_missing",
                        "Finalizing profile evidence is missing",
                    )
                return
            if job.kind == "gate":
                if session.get(GateResultRow, result.get("gate_result_id")) is None:
                    raise AgentError(
                        "gate_evidence_missing",
                        "Finalizing gate evidence is missing",
                    )
                return
            staging_value = result.get("staging_relative_path")
            if not isinstance(staging_value, str):
                raise AgentError(
                    "staging_evidence_missing",
                    "Finalizing artifact staging path is missing",
                )
            staging_relative = Path(staging_value)
            if job.kind == "experiment":
                run = session.get_one(Run, payload["run_id"])
                if not run.artifact_relative_path or not run.manifest_hash:
                    raise AgentError(
                        "run_evidence_missing",
                        "Finalizing run evidence is incomplete",
                    )
                final_relative = Path(run.artifact_relative_path)
                self._promote_run_bundle(staging_relative, final_relative, run.manifest_hash)
                return
            if job.kind == "export":
                export = session.get_one(Export, payload["export_id"])
                if not export.relative_path or not export.manifest_hash:
                    raise AgentError(
                        "export_evidence_missing",
                        "Finalizing export evidence is incomplete",
                    )
                self._promote_export(
                    staging_relative,
                    Path(export.relative_path),
                    export.manifest_hash,
                    result.get("bytes"),
                )
                return
            raise AgentError("unsupported_job_kind", f"Unsupported job kind: {job.kind}")

    def _promote_run_bundle(
        self,
        staging_relative: Path,
        final_relative: Path,
        expected_manifest_hash: str,
    ) -> None:
        staging = self.settings.managed_root / staging_relative
        final = self.settings.managed_root / final_relative
        if staging.exists():
            verify_run_bundle(self.settings, staging_relative, expected_manifest_hash)
            if not final.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.rename(staging, final)
                self._fsync_directory(final.parent)
        verify_run_bundle(self.settings, final_relative, expected_manifest_hash)
        self._fsync_directory(final.parent)

    def _promote_export(
        self,
        staging_relative: Path,
        final_relative: Path,
        expected_hash: str,
        expected_size: object,
    ) -> None:
        size = expected_size if isinstance(expected_size, int) else None
        staging = self.settings.managed_root / staging_relative
        final = self.settings.managed_root / final_relative
        if staging.exists():
            with verified_managed_file(self.settings, staging_relative, expected_hash, size):
                pass
            if not final.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.rename(staging, final)
                self._fsync_directory(final.parent)
        with verified_managed_file(self.settings, final_relative, expected_hash, size):
            pass
        self._fsync_directory(final.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _repair_published_artifacts(self) -> None:
        with Session(self.engine) as session:
            jobs = session.scalars(
                select(Job).where(
                    Job.state == JobState.SUCCEEDED.value,
                    Job.result_json.is_not(None),
                )
            ).all()
            repairs: list[tuple[Path, Path, str]] = []
            for job in jobs:
                result = json.loads(job.result_json or "{}")
                staging = result.get("staging_relative_path")
                if not isinstance(staging, str):
                    continue
                staging_path = self.settings.managed_root / staging
                if job.kind == "experiment":
                    run = session.get_one(Run, json.loads(job.payload_json)["run_id"])
                    if run.artifact_relative_path and run.manifest_hash:
                        repairs.append(
                            (
                                staging_path,
                                self.settings.managed_root / run.artifact_relative_path,
                                run.manifest_hash,
                            )
                        )
                elif job.kind == "export":
                    export = session.get_one(Export, json.loads(job.payload_json)["export_id"])
                    if export.relative_path and export.manifest_hash:
                        repairs.append(
                            (
                                staging_path,
                                self.settings.managed_root / export.relative_path,
                                export.manifest_hash,
                            )
                        )
        for staging, final, expected_hash in repairs:
            if not final.exists() and staging.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.rename(staging, final)
            if final.is_dir():
                relative = final.relative_to(self.settings.managed_root)
                with verified_managed_file(
                    self.settings,
                    relative / "manifest.json",
                    expected_hash,
                ):
                    pass
            else:
                relative = final.relative_to(self.settings.managed_root)
                with verified_managed_file(self.settings, relative, expected_hash):
                    pass
            if staging.exists():
                if staging.is_dir():
                    shutil.rmtree(staging, ignore_errors=True)
                else:
                    staging.unlink(missing_ok=True)

    def _repair_terminal_evidence(self) -> None:
        with Session(self.engine) as session:
            jobs = session.scalars(
                select(Job)
                .join(Run, Run.job_id == Job.id)
                .where(
                    Job.state.in_([JobState.FAILED.value, JobState.CANCELLED.value]),
                    Run.manifest_hash.is_(None),
                )
            ).all()
        for job in jobs:
            status = "cancelled" if job.state == JobState.CANCELLED.value else "failed"
            error = json.loads(job.error_json) if job.error_json else {"code": "worker_interrupted"}
            self._record_terminal_run(job.id, status, error)

    def _record_terminal_run(self, job_id: str, status: str, error: dict[str, Any]) -> None:
        from vonavy_agent.backtest import _environment, _source_revision

        with Session(self.engine) as session:
            job = session.get_one(Job, job_id)
            if job.state != status:
                return
            run = session.scalar(select(Run).where(Run.job_id == job_id))
            if run is None:
                return
            spec = session.get_one(ExperimentSpecRow, run.spec_id)
            gate = session.get_one(GateResultRow, run.gate_result_id)
            profile = session.get_one(DataProfile, spec.profile_id)
            mapping = session.get_one(DatasetMapping, spec.mapping_id)
            dataset = session.get_one(DatasetVersion, spec.dataset_version_id)
        final_dir = self.settings.managed_root / "runs" / run.id
        if final_dir.exists() and not (final_dir / "manifest.json").is_file():
            shutil.rmtree(final_dir)
        if not (final_dir / "manifest.json").is_file():
            temp_dir = (
                self.settings.managed_root / "jobs" / "tmp" / f"terminal-{job_id}-{uuid.uuid4()}"
            )
            temp_dir.mkdir(parents=True)
            (temp_dir / "spec.json").write_text(spec.canonical_json, encoding="utf-8")
            (temp_dir / "gate.json").write_text(gate.canonical_json, encoding="utf-8")
            (temp_dir / "error.json").write_text(canonical_json(error), encoding="utf-8")
            outputs = {
                path.name: {"sha256": file_hash(path), "bytes": path.stat().st_size}
                for path in sorted(temp_dir.iterdir())
            }
            dependency = Path("uv.lock")
            manifest = {
                "schema_version": "1.0",
                "run_id": run.id,
                "status": status,
                "source": _source_revision(),
                "dataset_hash": dataset.materialized_blob_sha256,
                "mapping_hash": mapping.mapping_hash,
                "profile_hash": profile.profile_hash,
                "spec_hash": spec.spec_hash,
                "dependency_hash": file_hash(dependency) if dependency.is_file() else None,
                "environment": _environment(self.settings),
                "seeds": json.loads(spec.canonical_json)["seeds"],
                "command": [sys.executable, "-m", "vonavy_agent.executor", "--job-id", job_id],
                "adapter": {"kind": "builtin", "version": "1.0"},
                "runtime_seconds": None,
                "outputs": outputs,
                "warnings": json.loads(gate.canonical_json)["warnings"],
                "errors": [error],
            }
            (temp_dir / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(temp_dir, final_dir)
            except OSError:
                if not (final_dir / "manifest.json").is_file():
                    raise
                shutil.rmtree(temp_dir, ignore_errors=True)
        manifest_path = final_dir / "manifest.json"
        if manifest_path.is_file():
            with session_scope(self.engine) as session:
                persisted = session.get_one(Run, run.id)
                persisted.artifact_relative_path = str(Path("runs") / run.id)
                persisted.manifest_hash = file_hash(manifest_path)
