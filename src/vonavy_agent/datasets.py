from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256 as new_sha256
from itertools import pairwise
from pathlib import Path
from typing import BinaryIO

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vonavy_agent.domain import (
    AvailabilityKind,
    DatasetMappingSpec,
)
from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_hash, canonical_json, file_hash
from vonavy_agent.identity import LOCAL_OWNER_ID
from vonavy_agent.persistence import (
    Blob,
    DataProfile,
    Dataset,
    DatasetMapping,
    DatasetVersion,
    session_scope,
)
from vonavy_agent.settings import Settings

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,254}$")
SUPPORTED_SUFFIXES = {".csv": "csv", ".parquet": "parquet"}


def _safe_original_name(name: str) -> str:
    candidate = Path(name).name
    if candidate != name or not SAFE_NAME.fullmatch(candidate):
        raise AgentError("unsafe_filename", "Filename must be a plain safe basename")
    if Path(candidate).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise AgentError("unsupported_type", "Only CSV and Parquet files are supported")
    return candidate


def _read_frame(path: Path, media_type: str) -> pd.DataFrame:
    if media_type == "parquet":
        with path.open("rb") as handle:
            if handle.read(4) != b"PAR1":
                raise AgentError("invalid_parquet", "Parquet file has an invalid magic header")
        try:
            return pd.read_parquet(path)
        except (OSError, ValueError, pa.ArrowException) as exc:
            raise AgentError("invalid_parquet", f"Could not read Parquet: {exc}") from exc
    try:
        return pd.read_csv(path, encoding="utf-8")
    except (OSError, UnicodeError, pd.errors.ParserError) as exc:
        raise AgentError("invalid_csv", f"Could not read UTF-8 CSV: {exc}") from exc


def observation_availability(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None:
        return pd.Series(True, index=frame.index, dtype="boolean")
    values = frame[column]
    if pd.api.types.is_bool_dtype(values):
        return values.astype("boolean")
    normalised = values.astype("string").str.strip().str.lower()
    return normalised.map({"true": True, "false": False, "1": True, "0": False}).astype("boolean")


class DatasetRegistry:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        settings.ensure_directories()

    def list_inbox(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        directory_fd = self.settings.open_managed_dir_fd(Path("inbox"))
        try:
            with os.scandir(directory_fd) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    entry_stat = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(entry_stat.st_mode):
                        continue
                    if Path(entry.name).suffix.lower() not in SUPPORTED_SUFFIXES:
                        continue
                    results.append({"name": entry.name, "bytes": entry_stat.st_size})
        finally:
            os.close(directory_fd)
        return results

    def import_inbox(
        self,
        inbox_name: str,
        dataset_name: str,
        mode: str = "snapshot",
        dataset_id: str | None = None,
        parent_version_id: str | None = None,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> DatasetVersion:
        safe_name = _safe_original_name(inbox_name)
        source_fd = self.settings.open_managed_file(Path("inbox") / safe_name)
        temp_path: Path | None = None
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise AgentError("unsafe_inbox_path", "Inbox selection is not a regular file")
            if before.st_size > self.settings.max_upload_bytes:
                raise AgentError("file_too_large", "Inbox file exceeds the configured size limit")
            with os.fdopen(source_fd, "rb", closefd=False) as handle:
                temp_path, byte_count = self._stage_stream(handle, Path(safe_name).suffix.lower())
            after = os.fstat(source_fd)
            if (
                before.st_ino,
                before.st_dev,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_ino,
                after.st_dev,
                after.st_size,
                after.st_mtime_ns,
            ) or byte_count != before.st_size:
                raise AgentError("source_changed", "Inbox file changed while it was being copied")
            return self._ingest_staged(
                temp_path,
                safe_name,
                dataset_name,
                mode=mode,
                dataset_id=dataset_id,
                parent_version_id=parent_version_id,
                owner_id=owner_id,
            )
        finally:
            os.close(source_fd)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def ingest_stream(
        self,
        stream: BinaryIO,
        original_name: str,
        dataset_name: str,
        *,
        mode: str = "snapshot",
        dataset_id: str | None = None,
        parent_version_id: str | None = None,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> DatasetVersion:
        safe_name = _safe_original_name(original_name)
        temp_path, _ = self._stage_stream(stream, Path(safe_name).suffix.lower())
        try:
            return self._ingest_staged(
                temp_path,
                safe_name,
                dataset_name,
                mode=mode,
                dataset_id=dataset_id,
                parent_version_id=parent_version_id,
                owner_id=owner_id,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def _stage_stream(self, stream: BinaryIO, suffix: str) -> tuple[Path, int]:
        temp_dir = self.settings.managed_root / "jobs" / "tmp"
        fd, temp_name = tempfile.mkstemp(prefix="ingest-", suffix=suffix, dir=temp_dir)
        temp_path = Path(temp_name)
        byte_count = 0
        completed = False
        try:
            with os.fdopen(fd, "wb") as output:
                while chunk := stream.read(1024 * 1024):
                    byte_count += len(chunk)
                    if byte_count > self.settings.max_upload_bytes:
                        raise AgentError(
                            "file_too_large", "Upload exceeds the configured size limit"
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if byte_count == 0:
                raise AgentError("empty_file", "Uploaded file is empty")
            completed = True
            return temp_path, byte_count
        finally:
            if not completed:
                temp_path.unlink(missing_ok=True)

    def _ingest_staged(
        self,
        temp_path: Path,
        safe_name: str,
        dataset_name: str,
        *,
        mode: str,
        dataset_id: str | None,
        parent_version_id: str | None,
        owner_id: str,
    ) -> DatasetVersion:
        if not dataset_name.strip() or len(dataset_name) > 200:
            raise AgentError("invalid_dataset_name", "Dataset name must contain 1-200 characters")
        if mode not in {"snapshot", "append"}:
            raise AgentError("invalid_ingest_mode", "Ingest mode must be snapshot or append")
        if mode == "append" and (not dataset_id or not parent_version_id):
            raise AgentError("missing_parent", "Append requires dataset_id and parent_version_id")
        suffix = Path(safe_name).suffix.lower()
        media_type = SUPPORTED_SUFFIXES[suffix]
        frame = _read_frame(temp_path, media_type)
        if not len(frame.columns) or frame.columns.duplicated().any():
            raise AgentError("invalid_schema", "Dataset columns must be present and unique")
        source_blob = self._publish_blob(temp_path, media_type)
        materialized_blob, materialized_rows = self._materialize(
            frame, mode, dataset_id, parent_version_id, owner_id
        )
        with session_scope(self.engine) as session:
            dataset = self._resolve_dataset(session, dataset_name, dataset_id, mode, owner_id)
            version_number = (
                session.scalar(
                    select(func.coalesce(func.max(DatasetVersion.version_number), 0)).where(
                        DatasetVersion.dataset_id == dataset.id
                    )
                )
                or 0
            ) + 1
            version = DatasetVersion(
                owner_id=owner_id,
                dataset_id=dataset.id,
                version_number=version_number,
                parent_id=parent_version_id,
                ingest_mode=mode,
                original_name=safe_name,
                source_blob_sha256=source_blob.sha256,
                materialized_blob_sha256=materialized_blob.sha256,
                row_count=materialized_rows,
            )
            session.add(version)
            session.flush()
            version_id = version.id
        with Session(self.engine) as session:
            return session.get_one(DatasetVersion, version_id)

    def _resolve_dataset(
        self,
        session: Session,
        dataset_name: str,
        dataset_id: str | None,
        mode: str,
        owner_id: str,
    ) -> Dataset:
        if dataset_id:
            dataset = session.get(Dataset, dataset_id)
            if dataset is None or dataset.owner_id != owner_id:
                raise AgentError("dataset_not_found", "Dataset does not exist", status_code=404)
            return dataset
        if mode == "append":
            raise AgentError("missing_dataset", "Append requires an existing dataset")
        dataset = Dataset(owner_id=owner_id, name=dataset_name.strip())
        session.add(dataset)
        session.flush()
        return dataset

    def _materialize(
        self,
        incoming: pd.DataFrame,
        mode: str,
        dataset_id: str | None,
        parent_version_id: str | None,
        owner_id: str,
    ) -> tuple[Blob, int]:
        frame = incoming
        if mode == "append":
            with Session(self.engine) as session:
                parent = session.get(DatasetVersion, parent_version_id)
                if parent is None or parent.dataset_id != dataset_id or parent.owner_id != owner_id:
                    raise AgentError(
                        "invalid_parent", "Append parent does not belong to the dataset"
                    )
                parent_blob = session.get_one(Blob, parent.materialized_blob_sha256)
                with self._open_verified_blob(parent_blob) as handle:
                    parent_frame = pd.read_parquet(handle)
            if list(parent_frame.columns) != list(incoming.columns):
                raise AgentError(
                    "append_schema_mismatch", "Append columns must exactly match the parent"
                )
            mismatched = [
                name
                for name in incoming.columns
                if str(parent_frame[name].dtype) != str(incoming[name].dtype)
            ]
            if mismatched:
                raise AgentError(
                    "append_type_mismatch",
                    "Append column types must match the parent",
                    detail={"columns": mismatched[:20]},
                )
            frame = pd.concat([parent_frame, incoming], ignore_index=True)
        temp_dir = self.settings.managed_root / "jobs" / "tmp"
        fd, temp_name = tempfile.mkstemp(prefix="materialized-", suffix=".parquet", dir=temp_dir)
        os.close(fd)
        path = Path(temp_name)
        try:
            frame.to_parquet(path, index=False)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            return self._publish_blob(path, "parquet"), len(frame)
        finally:
            path.unlink(missing_ok=True)

    def _publish_blob(self, source: Path, media_type: str) -> Blob:
        sha256 = file_hash(source)
        source_size = source.stat().st_size
        relative = Path("blobs") / "sha256" / sha256[:2] / f"{sha256}.{media_type}"
        parent_fd = self.settings.open_managed_dir_fd(relative.parent, create=True)
        temp_name = f".{relative.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        temp_fd: int | None = None
        try:
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            with source.open("rb") as input_handle, os.fdopen(temp_fd, "wb") as output:
                temp_fd = None
                while chunk := input_handle.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            with suppress(FileExistsError):
                os.link(
                    temp_name,
                    relative.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            winner_fd = os.open(
                relative.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                self._verify_blob_fd(winner_fd, sha256, source_size)
            finally:
                os.close(winner_fd)
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            with suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=parent_fd)
            os.close(parent_fd)
        with session_scope(self.engine) as session:
            session.execute(
                sqlite_insert(Blob)
                .values(
                    sha256=sha256,
                    media_type=media_type,
                    byte_size=source_size,
                    relative_path=str(relative),
                )
                .on_conflict_do_nothing(index_elements=[Blob.sha256])
            )
        with Session(self.engine) as session:
            blob = session.get_one(Blob, sha256)
            if (
                blob.byte_size != source_size
                or blob.media_type != media_type
                or blob.relative_path != str(relative)
            ):
                raise AgentError("blob_metadata_mismatch", "Stored blob metadata is inconsistent")
            return blob

    @staticmethod
    def _verify_blob_fd(fd: int, expected_hash: str, expected_size: int) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            raise AgentError("blob_integrity_failure", "Managed blob size is invalid")
        digest = new_sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise AgentError("blob_integrity_failure", "Managed blob hash is invalid")
        os.lseek(fd, 0, os.SEEK_SET)

    @contextmanager
    def _open_verified_blob(self, blob: Blob) -> Iterator[BinaryIO]:
        try:
            relative = Path(blob.relative_path)
            fd = self.settings.open_managed_file(relative)
        except (OSError, ValueError) as exc:
            raise AgentError(
                "managed_blob_missing", "Managed dataset blob is missing", status_code=500
            ) from exc
        try:
            self._verify_blob_fd(fd, blob.sha256, blob.byte_size)
        except (AgentError, OSError):
            os.close(fd)
            raise
        with os.fdopen(fd, "rb") as handle:
            yield handle

    def materialized_path(
        self,
        session: Session,
        version_id: str,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> Path:
        version = session.get(DatasetVersion, version_id)
        if version is None or version.owner_id != owner_id:
            raise AgentError(
                "dataset_version_not_found", "Dataset version does not exist", status_code=404
            )
        blob = session.get_one(Blob, version.materialized_blob_sha256)
        with self._open_verified_blob(blob):
            pass
        return self.settings.managed_root / blob.relative_path

    def read_materialized_frame(
        self,
        session: Session,
        version_id: str,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> pd.DataFrame:
        version = session.get(DatasetVersion, version_id)
        if version is None or version.owner_id != owner_id:
            raise AgentError(
                "dataset_version_not_found", "Dataset version does not exist", status_code=404
            )
        blob = session.get_one(Blob, version.materialized_blob_sha256)
        with self._open_verified_blob(blob) as handle:
            return pd.read_parquet(handle)

    def create_mapping(
        self,
        version_id: str,
        mapping: DatasetMappingSpec,
        owner_id: str = LOCAL_OWNER_ID,
    ) -> DatasetMapping:
        with session_scope(self.engine) as session:
            version = session.get(DatasetVersion, version_id)
            if version is None or version.owner_id != owner_id:
                raise AgentError(
                    "dataset_version_not_found",
                    "Dataset version does not exist",
                    status_code=404,
                )
            blob = session.get_one(Blob, version.materialized_blob_sha256)
            with self._open_verified_blob(blob) as handle:
                columns = set(pq.read_schema(handle).names)
            required = {mapping.timestamp_column, mapping.target_column}
            if mapping.entity_column:
                required.add(mapping.entity_column)
            if mapping.observation_availability_column:
                required.add(mapping.observation_availability_column)
            required.update(feature.name for feature in mapping.features)
            for availability in [
                mapping.target_availability,
                *(feature.availability for feature in mapping.features),
            ]:
                if availability.kind == AvailabilityKind.COLUMN and availability.column:
                    required.add(availability.column)
            missing = sorted(required - columns)
            if missing:
                raise AgentError(
                    "mapping_columns_missing",
                    "Mapping references columns that are not present",
                    detail={"columns": missing},
                )
            payload = mapping.model_dump(mode="json")
            row = DatasetMapping(
                owner_id=owner_id,
                dataset_version_id=version_id,
                mapping_hash=canonical_hash(payload),
                canonical_json=canonical_json(payload),
            )
            session.add(row)
            session.flush()
            row_id = row.id
        with Session(self.engine) as session:
            return session.get_one(DatasetMapping, row_id)


@dataclass(frozen=True)
class ProfileComputation:
    owner_id: str
    dataset_version_id: str
    mapping_id: str
    profile_hash: str
    canonical_json: str


def compute_profile(
    registry: DatasetRegistry,
    version_id: str,
    mapping_id: str,
    max_categories: int,
    owner_id: str = LOCAL_OWNER_ID,
) -> ProfileComputation:
    with Session(registry.engine) as session:
        mapping_row = session.get(DatasetMapping, mapping_id)
        if (
            mapping_row is None
            or mapping_row.dataset_version_id != version_id
            or mapping_row.owner_id != owner_id
        ):
            raise AgentError("mapping_not_found", "Mapping does not belong to the dataset version")
        mapping = DatasetMappingSpec.model_validate_json(mapping_row.canonical_json)
        frame = registry.read_materialized_frame(session, version_id, owner_id)
    timestamps = pd.to_datetime(frame[mapping.timestamp_column], errors="coerce", utc=True)
    invalid_timestamps = int(timestamps.isna().sum())
    dates = timestamps.dt.tz_convert(None).dt.normalize()
    entities = (
        frame[mapping.entity_column].astype("string")
        if mapping.entity_column
        else pd.Series(["__single__"] * len(frame), dtype="string")
    )
    keys = pd.DataFrame({"entity": entities, "date": dates})
    valid_keys = keys.dropna()
    duplicate_count = int(valid_keys.duplicated(["entity", "date"], keep=False).sum())
    gap_count = 0
    cadence_days: list[int] = []
    entity_coverage: list[dict[str, object]] = []
    for entity, group in valid_keys.groupby("entity", sort=True):
        unique_dates = sorted(pd.Timestamp(value) for value in group["date"].dropna().unique())
        if not unique_dates:
            continue
        cadence_days.extend(
            int((current - previous).days) for previous, current in pairwise(unique_dates)
        )
        expected = int((unique_dates[-1] - unique_dates[0]).days) + 1
        gaps = expected - len(unique_dates)
        gap_count += max(gaps, 0)
        entity_coverage.append(
            {
                "entity": str(entity),
                "start": unique_dates[0].date().isoformat(),
                "end": unique_dates[-1].date().isoformat(),
                "observed_days": len(unique_dates),
                "expected_days": expected,
                "gaps": max(gaps, 0),
            }
        )
    numeric: dict[str, object] = {}
    categorical: dict[str, object] = {}
    missingness = {name: int(frame[name].isna().sum()) for name in frame.columns}
    for name in frame.columns:
        series = frame[name]
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            numeric_series = pd.to_numeric(series, errors="coerce")
            nonfinite = numeric_series.notna() & ~np.isfinite(numeric_series)
            finite = numeric_series[np.isfinite(numeric_series)].dropna()
            numeric[name] = {
                "count": int(finite.count()),
                "nonfinite_count": int(nonfinite.sum()),
                "min": float(finite.min()) if not finite.empty else None,
                "max": float(finite.max()) if not finite.empty else None,
                "mean": float(finite.mean()) if not finite.empty else None,
                "std": float(finite.std()) if len(finite) > 1 else None,
                "quantiles": {
                    str(key): float(value)
                    for key, value in finite.quantile([0.05, 0.5, 0.95]).items()
                }
                if not finite.empty
                else {},
            }
        elif name not in {
            mapping.timestamp_column,
            *(
                availability.column
                for availability in [
                    mapping.target_availability,
                    *(feature.availability for feature in mapping.features),
                ]
                if availability.column
            ),
        }:
            counts = series.astype("string").value_counts(dropna=True)
            categorical[name] = {
                "cardinality": int(counts.size),
                "top_values": [
                    {"value": str(key), "count": int(value)}
                    for key, value in counts.head(max_categories).items()
                ],
                "truncated": counts.size > max_categories,
            }
    availability_lags: dict[str, object] = {}
    policies = {"target": mapping.target_availability}
    policies.update({feature.name: feature.availability for feature in mapping.features})
    for name, policy in policies.items():
        if policy.kind == AvailabilityKind.COLUMN and policy.column:
            available = pd.to_datetime(
                frame[policy.column],
                errors="coerce",
                utc=True,
                format="mixed",
            )
            lag = (available.dt.tz_convert(None).dt.normalize() - dates).dt.days.dropna()
            availability_lags[name] = {
                "invalid": int(available.isna().sum()),
                "min_days": int(lag.min()) if not lag.empty else None,
                "median_days": float(lag.median()) if not lag.empty else None,
                "max_days": int(lag.max()) if not lag.empty else None,
            }
        else:
            availability_lags[name] = {"policy": policy.kind.value}
    observed = observation_availability(frame, mapping.observation_availability_column)
    observation_profile = {
        "column": mapping.observation_availability_column,
        "assumption": (
            "explicit_column"
            if mapping.observation_availability_column
            else "all_rows_product_available"
        ),
        "available_rows": int(observed.eq(True).sum()),
        "unavailable_rows": int(observed.eq(False).sum()),
        "invalid_rows": int(observed.isna().sum()),
    }
    profile = {
        "schema_version": "1.0",
        "dataset_version_id": version_id,
        "mapping_id": mapping_id,
        "rows": len(frame),
        "columns": [{"name": name, "dtype": str(frame[name].dtype)} for name in frame.columns],
        "entities": int(entities.nunique(dropna=True)),
        "date_start": dates.min().date().isoformat() if dates.notna().any() else None,
        "date_end": dates.max().date().isoformat() if dates.notna().any() else None,
        "invalid_timestamps": invalid_timestamps,
        "duplicate_key_rows": duplicate_count,
        "gap_days": gap_count,
        "cadence_days": {
            "min": min(cadence_days) if cadence_days else None,
            "median": float(pd.Series(cadence_days).median()) if cadence_days else None,
            "max": max(cadence_days) if cadence_days else None,
        },
        "entity_coverage": entity_coverage[:100],
        "entity_coverage_truncated": len(entity_coverage) > 100,
        "missingness": missingness,
        "numeric": numeric,
        "categorical": categorical,
        "availability_lags": availability_lags,
        "observation_availability": observation_profile,
    }
    profile_hash = canonical_hash(profile)
    return ProfileComputation(
        owner_id=owner_id,
        dataset_version_id=version_id,
        mapping_id=mapping_id,
        profile_hash=profile_hash,
        canonical_json=canonical_json(profile),
    )


def publish_profile(
    session: Session,
    computation: ProfileComputation,
    row_id: str | None = None,
) -> DataProfile:
    row = DataProfile(
        owner_id=computation.owner_id,
        dataset_version_id=computation.dataset_version_id,
        mapping_id=computation.mapping_id,
        profile_hash=computation.profile_hash,
        canonical_json=computation.canonical_json,
    )
    if row_id is not None:
        row.id = row_id
    session.add(row)
    session.flush()
    return row


def build_profile(
    registry: DatasetRegistry,
    version_id: str,
    mapping_id: str,
    max_categories: int,
    before_publish: Callable[[], None] | None = None,
    owner_id: str = LOCAL_OWNER_ID,
) -> DataProfile:
    computation = compute_profile(
        registry,
        version_id,
        mapping_id,
        max_categories,
        owner_id,
    )
    if before_publish is not None:
        before_publish()
    with session_scope(registry.engine) as session:
        row = publish_profile(session, computation)
        row_id = row.id
    with Session(registry.engine) as session:
        return session.get_one(DataProfile, row_id)
