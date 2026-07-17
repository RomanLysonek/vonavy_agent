from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from vonavy_agent.backtest import RunCancelled, run_backtest
from vonavy_agent.datasets import DatasetRegistry, build_profile
from vonavy_agent.domain import JobState
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import run_gate
from vonavy_agent.exporting import create_static_export
from vonavy_agent.hashing import canonical_json
from vonavy_agent.persistence import (
    Export,
    Job,
    JobEvent,
    Run,
    create_db_engine,
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

    def store_result(self, result: dict[str, Any]) -> None:
        self._assert_parent()
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
                    )
                    .values(result_json=canonical_json(result), updated_at=datetime.utcnow())
                ),
            )
            if changed.rowcount != 1:
                raise LeaseLost

    def _assert_parent(self) -> None:
        if os.getppid() != self.parent_pid:
            raise LeaseLost


def execute(context: ExecutionContext) -> dict[str, Any]:
    context.assert_running()
    with Session(context.engine) as session:
        job = session.get_one(Job, context.job_id)
        payload = json.loads(job.payload_json)
        kind = job.kind
    result: dict[str, Any]
    if kind == "profile":
        profile_row = build_profile(
            DatasetRegistry(context.settings, context.engine),
            payload["dataset_version_id"],
            payload["mapping_id"],
            context.settings.max_profile_categories,
            before_publish=context.begin_publish,
        )
        result = {"profile_id": profile_row.id}
    elif kind == "gate":
        gate_row = run_gate(
            context.engine,
            DatasetRegistry(context.settings, context.engine),
            payload["spec_id"],
            before_publish=context.begin_publish,
        )
        result = {"gate_result_id": gate_row.id}
    elif kind == "export":
        result = create_static_export(
            context.engine,
            context.settings,
            payload["export_id"],
            payload["run_ids"],
            before_publish=context.begin_publish,
        )
        with session_scope(context.engine) as session:
            export = session.get_one(Export, payload["export_id"])
            export.relative_path = result["relative_path"]
            export.manifest_hash = result["sha256"]
    elif kind == "experiment":
        with Session(context.engine) as session:
            run = session.get_one(Run, payload["run_id"])
        summary = run_backtest(
            context.engine,
            context.settings,
            context.job_id,
            run.id,
            ownership_check=context.assert_running,
            before_publish=context.begin_publish,
        )
        result = {"run_id": run.id, "summary": summary}
    else:
        raise AgentError("unsupported_job_kind", f"Unsupported job kind: {kind}")
    context.store_result(result)
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
