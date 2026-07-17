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

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session

from vonavy_agent.domain import TERMINAL_JOB_STATES, JobState
from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_json, file_hash
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
            if state == JobState.PUBLISHING:
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
        with session_scope(self.engine) as session:
            stale = session.scalars(
                select(Job).where(
                    Job.state.in_(
                        [
                            JobState.RUNNING.value,
                            JobState.CANCELLING.value,
                            JobState.PUBLISHING.value,
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
                values: dict[str, Any]
                detail: dict[str, Any]
                recovered_result = (
                    self._publishing_result(session, job) if source == JobState.PUBLISHING else None
                )
                if recovered_result is not None:
                    target = JobState.SUCCEEDED
                    values = {
                        "result_json": canonical_json(recovered_result),
                        "error_json": None,
                    }
                    detail = {"reason": "recovered_published_result"}
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
        for job_id, status, error in evidence:
            self._record_terminal_run(job_id, status, error)
        self._repair_terminal_evidence()

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
        try:
            while process.poll() is None:
                time.sleep(0.1)
                if stdout.overflow or stderr.overflow:
                    outcome = ("failed", "output_limit", "Executor output exceeded 1 MB")
                    break
                if time.monotonic() - started > wall_seconds:
                    outcome = ("failed", "wall_timeout", f"Exceeded {wall_seconds} seconds")
                    break
                if not self._heartbeat(claim):
                    outcome = ("lost", "lease_lost", "Worker no longer owns the job lease")
                    break
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
        with session_scope(self.engine) as session:
            job = session.get_one(Job, claim.job_id)
            if not job.result_json:
                raise AgentError("missing_job_result", "Publishing job did not persist a result")
            self._owned_terminal(
                session,
                claim,
                {JobState.PUBLISHING},
                JobState.SUCCEEDED,
                {},
                {},
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
        with session_scope(self.engine) as session:
            transitioned = self._owned_terminal(
                session,
                claim,
                {JobState.RUNNING, JobState.CANCELLING, JobState.PUBLISHING},
                JobState.FAILED,
                {"error_json": canonical_json(error)},
                error,
            )
        if transitioned:
            self._record_terminal_run(claim.job_id, "failed", error)

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
        if process.poll() is not None:
            process.wait()
            return
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()

    def _publishing_result(self, session: Session, job: Job) -> dict[str, Any] | None:
        payload = json.loads(job.payload_json)
        if job.result_json:
            return cast(dict[str, Any], json.loads(job.result_json))
        if job.kind == "experiment":
            run = session.get_one(Run, payload["run_id"])
            if run.summary_json and run.manifest_hash:
                return {"run_id": run.id, "summary": json.loads(run.summary_json)}
        elif job.kind == "export":
            export = session.get_one(Export, payload["export_id"])
            if export.relative_path and export.manifest_hash:
                path = self.settings.managed_root / export.relative_path
                if path.is_file() and file_hash(path) == export.manifest_hash:
                    return {
                        "export_id": export.id,
                        "relative_path": export.relative_path,
                        "sha256": export.manifest_hash,
                        "bytes": path.stat().st_size,
                    }
        elif job.kind == "profile":
            row = session.scalar(
                select(DataProfile)
                .where(
                    DataProfile.dataset_version_id == payload["dataset_version_id"],
                    DataProfile.mapping_id == payload["mapping_id"],
                    DataProfile.created_at >= job.created_at,
                )
                .order_by(DataProfile.created_at.desc())
                .limit(1)
            )
            if row is not None:
                return {"profile_id": row.id}
        elif job.kind == "gate":
            gate_row = session.scalar(
                select(GateResultRow)
                .where(
                    GateResultRow.spec_id == payload["spec_id"],
                    GateResultRow.created_at >= job.created_at,
                )
                .order_by(GateResultRow.created_at.desc())
                .limit(1)
            )
            if gate_row is not None:
                return {"gate_result_id": gate_row.id}
        return None

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
