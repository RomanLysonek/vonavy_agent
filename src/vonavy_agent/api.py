from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from vonavy_agent.adapters import (
    PreparedInvocation,
    adapter_capabilities,
    dry_run_invocation,
    import_adapter_snapshot,
)
from vonavy_agent.datasets import DatasetRegistry
from vonavy_agent.domain import DatasetMappingSpec, ExperimentSpec, JobState
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import create_experiment_spec
from vonavy_agent.jobs import (
    enqueue_export,
    enqueue_job,
    enqueue_run,
    request_cancellation,
)
from vonavy_agent.managed_files import verified_managed_file
from vonavy_agent.persistence import (
    DataProfile,
    Dataset,
    DatasetVersion,
    ExperimentSpecRow,
    Export,
    GateResultRow,
    Job,
    Run,
    RunMetric,
    create_db_engine,
    new_id,
)
from vonavy_agent.planner import confirm_proposal, propose_experiments
from vonavy_agent.settings import Settings


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InboxImportRequest(RequestModel):
    name: str
    dataset_name: str
    mode: str = "snapshot"
    dataset_id: str | None = None
    parent_version_id: str | None = None


class ProfileRequest(RequestModel):
    dataset_version_id: str
    mapping_id: str


class RunRequest(RequestModel):
    spec_id: str
    gate_result_id: str
    confirmation_token: str


class ComparisonRequest(RequestModel):
    run_ids: list[str] = Field(min_length=1)


class ProposalConfirmRequest(RequestModel):
    rank: int = Field(ge=1)


class ExportRequest(RequestModel):
    run_ids: list[str] = Field(min_length=1)


def migrate(settings: Settings) -> None:
    settings.ensure_directories()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.database_path.resolve()}")
    command.upgrade(config, "head")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_directories()
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    registry = DatasetRegistry(settings, engine)
    worker_process: subprocess.Popen[str] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal worker_process
        if settings.supervise_worker:
            worker_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "vonavy_agent.cli",
                    "worker",
                    "--managed-root",
                    str(settings.managed_root),
                ],
                shell=False,
                text=True,
            )
        try:
            yield
        finally:
            if worker_process is not None and worker_process.poll() is None:
                worker_process.terminate()
                try:
                    worker_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker_process.kill()

    app = FastAPI(title="Experiment Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine

    @app.exception_handler(AgentError)
    async def agent_error_handler(_: Request, exc: AgentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                }
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "version": "0.1.0", "worker_supervised": settings.supervise_worker}

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, object]:
        return {
            "frequency": ["D"],
            "models": ["seasonal_naive", "moving_average", "ridge_direct"],
            "metrics": ["wape", "mae", "rmse", "bias", "coverage", "runtime"],
            "adapters": adapter_capabilities(engine),
        }

    @app.get("/api/inbox")
    def inbox() -> dict[str, object]:
        return {"files": registry.list_inbox()}

    @app.post("/api/inbox/import")
    def import_inbox(request: InboxImportRequest) -> dict[str, object]:
        version = registry.import_inbox(
            request.name,
            request.dataset_name,
            request.mode,
            request.dataset_id,
            request.parent_version_id,
        )
        return _version_json(version)

    @app.post("/api/datasets/upload")
    def upload_dataset(
        file: Annotated[UploadFile, File()],
        dataset_name: Annotated[str, Form()],
        mode: Annotated[str, Form()] = "snapshot",
        dataset_id: Annotated[str | None, Form()] = None,
        parent_version_id: Annotated[str | None, Form()] = None,
    ) -> dict[str, object]:
        version = registry.ingest_stream(
            file.file,
            file.filename or "",
            dataset_name,
            mode=mode,
            dataset_id=dataset_id,
            parent_version_id=parent_version_id,
        )
        return _version_json(version)

    @app.get("/api/datasets")
    def list_datasets() -> dict[str, object]:
        with Session(engine) as session:
            datasets = session.scalars(select(Dataset).order_by(Dataset.created_at)).all()
            versions = session.scalars(
                select(DatasetVersion).order_by(
                    DatasetVersion.dataset_id, DatasetVersion.version_number
                )
            ).all()
        by_dataset: dict[str, list[dict[str, object]]] = {}
        for version in versions:
            by_dataset.setdefault(version.dataset_id, []).append(_version_json(version))
        return {
            "datasets": [
                {
                    "id": dataset.id,
                    "name": dataset.name,
                    "created_at": dataset.created_at.isoformat(),
                    "versions": by_dataset.get(dataset.id, []),
                }
                for dataset in datasets
            ]
        }

    @app.get("/api/dataset-versions/{version_id}")
    def get_version(version_id: str) -> dict[str, object]:
        with Session(engine) as session:
            version = session.get(DatasetVersion, version_id)
            if version is None:
                raise AgentError(
                    "dataset_version_not_found", "Dataset version does not exist", status_code=404
                )
            return _version_json(version)

    @app.post("/api/dataset-versions/{version_id}/mappings")
    def create_mapping(version_id: str, mapping: DatasetMappingSpec) -> dict[str, object]:
        row = registry.create_mapping(version_id, mapping)
        return {
            "id": row.id,
            "dataset_version_id": row.dataset_version_id,
            "mapping_hash": row.mapping_hash,
            "mapping": json.loads(row.canonical_json),
        }

    @app.post("/api/profiles")
    def enqueue_profile(request: ProfileRequest) -> dict[str, object]:
        job = enqueue_job(engine, "profile", request.model_dump())
        return _job_json(job)

    @app.get("/api/profiles/{profile_id}")
    def get_profile(profile_id: str) -> dict[str, object]:
        with Session(engine) as session:
            row = session.get(DataProfile, profile_id)
            if row is None:
                raise AgentError("profile_not_found", "Profile does not exist", status_code=404)
            return {
                "id": row.id,
                "profile_hash": row.profile_hash,
                "profile": json.loads(row.canonical_json),
            }

    @app.post("/api/specs")
    def create_spec(spec: ExperimentSpec) -> dict[str, object]:
        row = create_experiment_spec(engine, spec)
        return _spec_json(row)

    @app.get("/api/specs/{spec_id}")
    def get_spec(spec_id: str) -> dict[str, object]:
        with Session(engine) as session:
            row = session.get(ExperimentSpecRow, spec_id)
            if row is None:
                raise AgentError(
                    "spec_not_found", "Experiment spec does not exist", status_code=404
                )
            return _spec_json(row)

    @app.post("/api/specs/{spec_id}/gate")
    def enqueue_gate(spec_id: str) -> dict[str, object]:
        with Session(engine) as session:
            if session.get(ExperimentSpecRow, spec_id) is None:
                raise AgentError(
                    "spec_not_found", "Experiment spec does not exist", status_code=404
                )
        return _job_json(enqueue_job(engine, "gate", {"spec_id": spec_id}))

    @app.get("/api/gates/{gate_id}")
    def get_gate(gate_id: str) -> dict[str, object]:
        with Session(engine) as session:
            row = session.get(GateResultRow, gate_id)
            if row is None:
                raise AgentError("gate_not_found", "Gate result does not exist", status_code=404)
            return {"id": row.id, "spec_id": row.spec_id, "report": json.loads(row.canonical_json)}

    @app.post("/api/runs")
    def create_run(request: RunRequest) -> dict[str, object]:
        run, job = enqueue_run(
            engine,
            request.spec_id,
            request.gate_result_id,
            request.confirmation_token,
        )
        return {"run_id": run.id, "job": _job_json(job)}

    @app.get("/api/runs")
    def list_runs() -> dict[str, object]:
        with Session(engine) as session:
            runs = session.scalars(select(Run).order_by(Run.created_at.desc())).all()
            jobs = {job.id: job for job in session.scalars(select(Job)).all()}
        return {"runs": [_run_json(run, jobs[run.job_id]) for run in runs]}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, object]:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise AgentError("run_not_found", "Run does not exist", status_code=404)
            return _run_json(run, session.get_one(Job, run.job_id))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise AgentError("job_not_found", "Job does not exist", status_code=404)
            return _job_json(job)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, object]:
        return _job_json(request_cancellation(engine, job_id, settings))

    @app.post("/api/comparisons")
    def comparison(request: ComparisonRequest) -> dict[str, object]:
        with Session(engine) as session:
            runs = [session.get(Run, run_id) for run_id in request.run_ids]
            jobs = {
                job.id: job
                for job in session.scalars(
                    select(Job).where(Job.id.in_([run.job_id for run in runs if run is not None]))
                ).all()
            }
            if any(
                run is None
                or run.summary_json is None
                or jobs[run.job_id].state != JobState.SUCCEEDED.value
                for run in runs
            ):
                raise AgentError("runs_not_comparable", "All compared runs must be successful")
            metrics = session.scalars(
                select(RunMetric).where(
                    RunMetric.run_id.in_(request.run_ids),
                    RunMetric.role.in_(["calibration", "test"]),
                    RunMetric.origin.is_(None),
                    RunMetric.horizon.is_(None),
                )
            ).all()
        return {
            "run_ids": sorted(set(request.run_ids)),
            "metrics": [
                {
                    "run_id": metric.run_id,
                    "role": metric.role,
                    "model": metric.model,
                    "seed": metric.seed,
                    "metric": metric.metric,
                    "value": metric.value,
                    "unsupported_reason": metric.unsupported_reason,
                    "row_count": metric.row_count,
                    "coverage": metric.coverage,
                }
                for metric in metrics
            ],
        }

    @app.post("/api/planner/proposals/{spec_id}")
    def planner_proposals(spec_id: str) -> dict[str, object]:
        row = propose_experiments(engine, spec_id)
        return {"id": row.id, "proposal": json.loads(row.canonical_json)}

    @app.post("/api/planner/proposals/{proposal_id}/confirm")
    def planner_confirm(proposal_id: str, request: ProposalConfirmRequest) -> dict[str, object]:
        return _spec_json(confirm_proposal(engine, proposal_id, request.rank))

    @app.get("/api/adapters")
    def adapters() -> dict[str, object]:
        return {"adapters": adapter_capabilities(engine)}

    @app.post("/api/adapters/import")
    def adapter_import(file: Annotated[UploadFile, File()]) -> dict[str, object]:
        row = import_adapter_snapshot(engine, settings, file.file, file.filename or "")
        return {
            "id": row.id,
            "adapter_kind": row.adapter_kind,
            "manifest_kind": row.manifest_kind,
            "sha256": row.source_sha256,
            "manifest": json.loads(row.canonical_json),
        }

    @app.post("/api/adapters/{adapter_kind}/dry-run")
    def adapter_dry_run(adapter_kind: str) -> dict[str, object]:
        if adapter_kind not in {"anomaly", "chronos"}:
            raise AgentError("adapter_unknown", "Unknown adapter kind")
        cwd = (settings.managed_root / "imports").resolve()
        invocation = PreparedInvocation(
            executable=sys.executable,
            argv=("-m", f"vonavy_agent.adapters_{adapter_kind}", "--capabilities"),
            cwd=str(cwd),
            timeout_seconds=30,
            expected_outputs=(),
        )
        return dry_run_invocation(
            invocation,
            allowed_executables={Path(sys.executable)},
            allowed_root=settings.managed_root,
        )

    @app.post("/api/exports")
    def create_export_job(request: ExportRequest) -> dict[str, object]:
        export_id = new_id()
        _, job = enqueue_export(engine, export_id, request.run_ids)
        return {"export_id": export_id, "job": _job_json(job)}

    @app.get("/api/exports/{export_id}")
    def get_export(export_id: str) -> dict[str, object]:
        with Session(engine) as session:
            row = session.get(Export, export_id)
            if row is None:
                raise AgentError("export_not_found", "Export does not exist", status_code=404)
            job = session.get_one(Job, row.job_id)
            return {
                "id": row.id,
                "job": _job_json(job),
                "download_ready": bool(
                    row.relative_path
                    and row.manifest_hash
                    and job.state == JobState.SUCCEEDED.value
                ),
                "sha256": row.manifest_hash,
            }

    @app.get("/api/exports/{export_id}/download")
    def download_export(export_id: str) -> StreamingResponse:
        with Session(engine) as session:
            row = session.get(Export, export_id)
            if row is None or not row.relative_path or not row.manifest_hash:
                raise AgentError("export_not_ready", "Export is not ready", status_code=409)
            job = session.get_one(Job, row.job_id)
            if job.state != JobState.SUCCEEDED.value:
                raise AgentError("export_not_ready", "Export is not ready", status_code=409)
            relative = Path(row.relative_path)
            expected_hash = row.manifest_hash
        verified = verified_managed_file(settings, relative, expected_hash)
        handle = verified.__enter__()

        def content() -> Iterator[bytes]:
            try:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
            finally:
                verified.__exit__(None, None, None)

        return StreamingResponse(
            content(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{relative.name}"'},
        )

    web_root = Path(__file__).resolve().parent / "web"
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    return app


def _version_json(version: DatasetVersion) -> dict[str, object]:
    return {
        "id": version.id,
        "dataset_id": version.dataset_id,
        "version_number": version.version_number,
        "parent_id": version.parent_id,
        "ingest_mode": version.ingest_mode,
        "original_name": version.original_name,
        "source_sha256": version.source_blob_sha256,
        "materialized_sha256": version.materialized_blob_sha256,
        "row_count": version.row_count,
        "created_at": version.created_at.isoformat(),
    }


def _job_json(job: Job) -> dict[str, object]:
    result_visible = job.state == JobState.SUCCEEDED.value
    return {
        "id": job.id,
        "kind": job.kind,
        "state": job.state,
        "attempt": job.attempt,
        "cancel_requested": job.cancel_requested,
        "result": (json.loads(job.result_json) if result_visible and job.result_json else None),
        "error": json.loads(job.error_json) if job.error_json else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def _spec_json(row: ExperimentSpecRow) -> dict[str, object]:
    return {"id": row.id, "spec_hash": row.spec_hash, "spec": json.loads(row.canonical_json)}


def _run_json(run: Run, job: Job) -> dict[str, object]:
    evidence_visible = job.state == JobState.SUCCEEDED.value
    return {
        "id": run.id,
        "spec_id": run.spec_id,
        "gate_result_id": run.gate_result_id,
        "job": _job_json(job),
        "summary": (
            json.loads(run.summary_json) if evidence_visible and run.summary_json else None
        ),
        "manifest_hash": run.manifest_hash if evidence_visible else None,
        "created_at": run.created_at.isoformat(),
    }
