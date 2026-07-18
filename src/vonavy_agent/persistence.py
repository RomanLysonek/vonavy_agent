from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from vonavy_agent.identity import LOCAL_OWNER_ID


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


class Dataset(Base):
    __tablename__ = "datasets"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Blob(Base):
    __tablename__ = "blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(30))
    byte_size: Mapped[int] = mapped_column(Integer)
    relative_path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    ingest_mode: Mapped[str] = mapped_column(String(20))
    original_name: Mapped[str] = mapped_column(String(255))
    source_blob_sha256: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"))
    materialized_blob_sha256: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"))
    row_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class DatasetMapping(Base):
    __tablename__ = "dataset_mappings"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    mapping_hash: Mapped[str] = mapped_column(String(64), index=True)
    canonical_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class DataProfile(Base):
    __tablename__ = "data_profiles"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    mapping_id: Mapped[str] = mapped_column(ForeignKey("dataset_mappings.id"))
    profile_hash: Mapped[str] = mapped_column(String(64), index=True)
    canonical_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ExperimentSpecRow(Base):
    __tablename__ = "experiment_specs"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    spec_hash: Mapped[str] = mapped_column(String(64), index=True)
    canonical_json: Mapped[str] = mapped_column(Text)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id"))
    mapping_id: Mapped[str] = mapped_column(ForeignKey("dataset_mappings.id"))
    profile_id: Mapped[str] = mapped_column(ForeignKey("data_profiles.id"))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class GateResultRow(Base):
    __tablename__ = "gate_results"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    spec_id: Mapped[str] = mapped_column(ForeignKey("experiment_specs.id"))
    spec_hash: Mapped[str] = mapped_column(String(64))
    profile_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    canonical_json: Mapped[str] = mapped_column(Text)
    confirmation_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_state: Mapped[str] = mapped_column(String(20))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Run(Base):
    __tablename__ = "runs"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    spec_id: Mapped[str] = mapped_column(ForeignKey("experiment_specs.id"))
    gate_result_id: Mapped[str] = mapped_column(ForeignKey("gate_results.id"))
    artifact_relative_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class RunMetric(Base):
    __tablename__ = "run_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(50))
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin: Mapped[str | None] = mapped_column(String(10), nullable=True)
    horizon: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metric: Mapped[str] = mapped_column(String(30))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer)
    coverage: Mapped[float] = mapped_column(Float)
    unsupported_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class PlannerProposal(Base):
    __tablename__ = "planner_proposals"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    input_hash: Mapped[str] = mapped_column(String(64))
    canonical_json: Mapped[str] = mapped_column(Text)
    confirmed_spec_id: Mapped[str | None] = mapped_column(
        ForeignKey("experiment_specs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class AdapterSnapshot(Base):
    __tablename__ = "adapter_snapshots"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    adapter_kind: Mapped[str] = mapped_column(String(30))
    manifest_kind: Mapped[str] = mapped_column(String(30))
    schema_version: Mapped[str] = mapped_column(String(20))
    source_sha256: Mapped[str] = mapped_column(String(64))
    canonical_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class Export(Base):
    __tablename__ = "exports"
    owner_id: Mapped[str] = mapped_column(String(128), default=LOCAL_OWNER_ID, index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    run_ids_json: Mapped[str] = mapped_column(Text)
    relative_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


def create_db_engine(database_path: Path) -> Engine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    return engine


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session, session.begin():
        yield session
