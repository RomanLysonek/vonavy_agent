from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from vonavy_agent.backtest import RunCancelled, persist_run, run_backtest
from vonavy_agent.datasets import (
    DatasetRegistry,
    compute_profile,
    publish_profile,
)
from vonavy_agent.domain import JobState
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import compute_gate, publish_gate
from vonavy_agent.exporting import stage_static_export
from vonavy_agent.hashing import canonical_json
from vonavy_agent.persistence import (
    Export,
    Job,
    JobEvent,
    Run,
    create_db_engine,
    new_id,
    session_scope,
)
from vonavy_agent.settings import Settings


class LeaseLost(Exception):
    pass


class ExecutionContext:
    def __init__(
        self,
        settings: Settings,
        job_id: str,
        worker_id: str,
        lease_token: str,
        parent_pid: int,
    ) -> None:
        self.settings = settings
        self.engine = create_db_engine(settings.database_path)
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_token = lease_token
        self.parent_pid = parent_pid

    def assert_running(self) -> None:
        self._assert_parent()
        with Session(self.engine) as session:
            job = session.get_one(Job, self.job_id)
            if job.cancel_requested or job.state == JobState.CANCELLING.value:
                raise RunCancelled
            if (
                job.state != JobState.RUNNING.value
                or job.worker_id != self.worker_id
                or job.lease_token != self.lease_token
            ):
                raise LeaseLost

    def begin_publish(self) -> None:
        self._assert_parent()
        now = datetime.utcnow()
        with session_scope(self.engine) as session:
            changed = cast(
                CursorResult[Any],
                session.execute(
                    update(Job)
                    .where(
                        Job.id == self.job_id,
                        Job.state == JobState.RUNNING.value,
                        Job.worker_id == self.worker_id,
                        Job.lease_token == self.lease_token,
                        Job.lease_expires_at >= now,
                        Job.cancel_requested.is_(False),
                    )
                    .values(state=JobState.PUBLISHING.value, updated_at=now)
                ),
            )
            if changed.rowcount != 1:
                job = session.get_one(Job, self.job_id)
                if job.cancel_requested or job.state == JobState.CANCELLING.value:
                    raise RunCancelled
                raise LeaseLost
            session.add(
                JobEvent(
                    job_id=self.job_id,
                    from_state=JobState.RUNNING.value,
                    to_state=JobState.PUBLISHING.value,
                    detail_json=canonical_json({"lease_token": self.lease_token}),
                )
            )

    def finalize(
        self,
        result: dict[str, Any],
        publish_evidence: Callable[[Session], object],
    ) -> None:
        self._assert_parent()
        now = datetime.utcnow()
        with session_scope(self.engine) as session:
            changed = cast(
                CursorResult[Any],
                session.execute(
                    update(Job)
                    .where(
                        Job.id == self.job_id,
                        Job.state == JobState.PUBLISHING.value,
                        Job.worker_id == self.worker_id,
                        Job.lease_token == self.lease_token,
                        Job.lease_expires_at >= now,
                    )
                    .values(
                        state=JobState.FINALIZING.value,
                        result_json=canonical_json(result),
                        updated_at=now,
                    )
                ),
            )
            if changed.rowcount != 1:
                raise LeaseLost
            publish_evidence(session)
            session.add(
                JobEvent(
                    job_id=self.job_id,
                    from_state=JobState.PUBLISHING.value,
                    to_state=JobState.FINALIZING.value,
                    detail_json=canonical_json({"lease_token": self.lease_token}),
                )
            )

    def _assert_parent(self) -> None:
        if os.getppid() != self.parent_pid:
            raise LeaseLost


def execute(context: ExecutionContext) -> dict[str, Any]:
    context.assert_running()
    with Session(context.engine) as session:
        job = session.get_one(Job, context.job_id)
        payload = json.loads(job.payload_json)
        kind = job.kind
        owner_id = job.owner_id
    result: dict[str, Any]
    if kind == "profile":
        profile_computation = compute_profile(
            DatasetRegistry(context.settings, context.engine),
            payload["dataset_version_id"],
            payload["mapping_id"],
            context.settings.max_profile_categories,
            owner_id,
        )
        profile_id = new_id()
        context.begin_publish()
        result = {"profile_id": profile_id}
        context.finalize(
            result,
            lambda session: publish_profile(session, profile_computation, profile_id),
        )
    elif kind == "gate":
        gate_computation = compute_gate(
            context.engine,
            DatasetRegistry(context.settings, context.engine),
            payload["spec_id"],
            owner_id,
        )
        gate_id = new_id()
        context.begin_publish()
        result = {"gate_result_id": gate_id}
        context.finalize(
            result,
            lambda session: publish_gate(session, gate_computation, gate_id),
        )
    elif kind == "export":
        staged_export = stage_static_export(
            context.engine,
            context.settings,
            payload["export_id"],
            payload["run_ids"],
            context.settings.managed_root
            / "jobs"
            / "tmp"
            / context.job_id
            / context.lease_token
            / "export",
            owner_id,
        )
        result = staged_export.result
        context.begin_publish()

        def publish_export(session: Session) -> None:
            export = session.get_one(Export, payload["export_id"])
            export.relative_path = result["relative_path"]
            export.manifest_hash = result["sha256"]

        context.finalize(result, publish_export)
        staged_export.final_path.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staged_export.staging_path, staged_export.final_path)
        shutil.rmtree(staged_export.staging_path.parent, ignore_errors=True)
    elif kind == "experiment":
        with Session(context.engine) as session:
            run = session.get_one(Run, payload["run_id"])
        staged_run = run_backtest(
            context.engine,
            context.settings,
            context.job_id,
            run.id,
            context.lease_token,
            ownership_check=context.assert_running,
            before_publish=context.begin_publish,
            owner_id=owner_id,
        )
        result = {
            "run_id": run.id,
            "summary": staged_run.summary,
            "staging_relative_path": str(
                staged_run.staging_dir.relative_to(context.settings.managed_root)
            ),
        }
        context.finalize(
            result,
            lambda session: persist_run(
                session,
                run.id,
                staged_run.manifest_hash,
                staged_run.summary,
            ),
        )
        if not staged_run.already_visible:
            staged_run.final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staged_run.staging_dir, staged_run.final_dir)
    else:
        raise AgentError("unsupported_job_kind", f"Unsupported job kind: {kind}")
    return result


def exit_with(code: int) -> NoReturn:
    raise SystemExit(code)


def watch_parent(expected_parent_pid: int) -> None:
    def monitor() -> None:
        while os.getppid() == expected_parent_pid:
            time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=monitor, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--managed-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--lease-token", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    settings = Settings(managed_root=Path(args.managed_root))
    if settings.database_path.resolve() != Path(args.database).resolve():
        raise SystemExit("Database path does not match the managed root")
    context = ExecutionContext(
        settings,
        args.job_id,
        args.worker_id,
        args.lease_token,
        args.parent_pid,
    )
    watch_parent(args.parent_pid)
    try:
        result = execute(context)
    except RunCancelled:
        exit_with(130)
    except LeaseLost:
        exit_with(75)
    except AgentError as exc:
        print(
            canonical_json({"error": {"code": exc.code, "message": exc.message}}),
            file=sys.stderr,
        )
        exit_with(2)
    print(canonical_json(result))


if __name__ == "__main__":
    main()
