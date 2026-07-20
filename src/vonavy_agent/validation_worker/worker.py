from __future__ import annotations

import hashlib
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from vonavy_agent import __version__
from vonavy_agent.validation_contracts import (
    SUPPORTED_VALIDATION_MEDIA_TYPES,
    ColumnProfile,
    InputArtifact,
    InputIdentity,
    LocalInputArtifact,
    LocalInputIdentity,
    S3InputIdentity,
    ValidationIssue,
    ValidationRequest,
    ValidationResourceUsage,
    ValidationResult,
    ValidationStatus,
)
from vonavy_agent.validation_worker.artifacts import (
    ArtifactReader,
    ArtifactTooLargeError,
    UnsafeArtifactPathError,
)
from vonavy_agent.validation_worker.profiling import (
    Deadline,
    ScanProblem,
    build_profiles,
    scan_csv,
    scan_parquet,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024**2 if sys.platform == "darwin" else 1024
    return max(float(usage) / divisor, 0.0)


def _usage(start_cpu: float, profiled_rows: int, sampled: bool) -> ValidationResourceUsage:
    return ValidationResourceUsage(
        peak_rss_mb=_peak_rss_mb(),
        cpu_seconds=max(time.process_time() - start_cpu, 0.0),
        profiled_rows=profiled_rows,
        profiling_sampled=sampled,
    )


def _issue(code: str, message: str, *, column: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, column=column)


def _input_identity(
    artifact: InputArtifact,
    *,
    size_bytes: int,
    sha256: str,
) -> InputIdentity:
    if isinstance(artifact, LocalInputArtifact):
        return LocalInputIdentity(
            path=artifact.path,
            media_type=artifact.media_type,
            size_bytes=size_bytes,
            sha256=sha256,
        )
    return S3InputIdentity(
        bucket=artifact.bucket,
        key=artifact.key,
        version_id=artifact.version_id,
        media_type=artifact.media_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )


def _hash_file(path: Path, deadline: Deadline) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                deadline.check()
                digest.update(chunk)
    except OSError as exc:
        raise ScanProblem(
            "file_not_found",
            "Input artifact could not be read",
            invalid=False,
        ) from exc
    return digest.hexdigest()


def _result(
    request: ValidationRequest,
    *,
    status: ValidationStatus,
    started_at: datetime,
    started_monotonic: float,
    start_cpu: float,
    errors: tuple[ValidationIssue, ...],
    input_identity: InputIdentity | None = None,
    data_format: Literal["csv", "parquet"] | None = None,
    row_count: int | None = None,
    column_count: int | None = None,
    columns: tuple[ColumnProfile, ...] = (),
    warnings: tuple[ValidationIssue, ...] = (),
    profiled_rows: int = 0,
    profiling_sampled: bool = False,
) -> ValidationResult:
    finished = utc_now()
    return ValidationResult(
        job_id=request.job_id,
        dataset_id=request.dataset_id,
        status=status,
        started_at=started_at,
        finished_at=finished,
        duration_ms=max(int((time.monotonic() - started_monotonic) * 1000), 0),
        input_identity=input_identity,
        format=data_format,
        row_count=row_count,
        column_count=column_count,
        columns=columns,
        warnings=warnings,
        validation_errors=errors,
        resource_usage=_usage(start_cpu, profiled_rows, profiling_sampled),
        worker_version=__version__,
    )


def validate_request(
    request: ValidationRequest,
    reader: ArtifactReader,
) -> ValidationResult:
    started_at = utc_now()
    started_monotonic = time.monotonic()
    start_cpu = time.process_time()
    deadline = Deadline(request.limits.max_execution_seconds)
    input_identity: InputIdentity | None = None
    data_format: Literal["csv", "parquet"] | None = None
    if (
        request.input.expected_size_bytes is not None
        and request.input.expected_size_bytes > request.limits.max_input_bytes
    ):
        return _result(
            request,
            status=ValidationStatus.INVALID,
            started_at=started_at,
            started_monotonic=started_monotonic,
            start_cpu=start_cpu,
            errors=(
                _issue(
                    "input_too_large",
                    "Input exceeds the configured byte limit",
                ),
            ),
        )
    if request.input.media_type not in SUPPORTED_VALIDATION_MEDIA_TYPES:
        return _result(
            request,
            status=ValidationStatus.INVALID,
            started_at=started_at,
            started_monotonic=started_monotonic,
            start_cpu=start_cpu,
            errors=(
                _issue(
                    "unsupported_media_type",
                    "Input media type is not supported by the validation worker",
                ),
            ),
        )
    try:
        with reader.materialize(request.input) as path:
            deadline.check()
            size = path.stat().st_size
            if size > request.limits.max_input_bytes:
                raise ScanProblem(
                    "input_too_large",
                    "Input exceeds the configured byte limit",
                    invalid=True,
                )
            if request.input.expected_size_bytes is not None and (
                size != request.input.expected_size_bytes
            ):
                raise ScanProblem(
                    "size_mismatch",
                    "Input size does not match the immutable artifact reference",
                    invalid=True,
                )
            checksum = _hash_file(path, deadline)
            if request.input.expected_sha256 is not None and (
                checksum != request.input.expected_sha256
            ):
                raise ScanProblem(
                    "checksum_mismatch",
                    "Input checksum does not match the immutable artifact reference",
                    invalid=True,
                )
            input_identity = _input_identity(
                request.input,
                size_bytes=size,
                sha256=checksum,
            )
            if request.input.media_type == "text/csv":
                summary = scan_csv(path, request.limits, checksum, deadline)
                data_format = "csv"
            else:
                summary = scan_parquet(path, request.limits, checksum, deadline)
                data_format = "parquet"
            post_scan_checksum = _hash_file(path, deadline)
            if post_scan_checksum != checksum:
                raise ScanProblem(
                    "input_changed",
                    "Input artifact changed while validation was running",
                    invalid=False,
                )
            columns, warnings = build_profiles(summary, request.limits, deadline)
            return _result(
                request,
                status=ValidationStatus.SUCCEEDED,
                started_at=started_at,
                started_monotonic=started_monotonic,
                start_cpu=start_cpu,
                errors=(),
                input_identity=input_identity,
                data_format=data_format,
                row_count=summary.row_count,
                column_count=len(summary.columns),
                columns=columns,
                warnings=warnings,
                profiled_rows=len(summary.sample),
                profiling_sampled=summary.row_count > len(summary.sample),
            )
    except ScanProblem as exc:
        return _result(
            request,
            status=ValidationStatus.INVALID if exc.invalid else ValidationStatus.FAILED,
            started_at=started_at,
            started_monotonic=started_monotonic,
            start_cpu=start_cpu,
            errors=(_issue(exc.code, exc.message, column=exc.column),),
            input_identity=input_identity,
            data_format=data_format,
        )
    except ArtifactTooLargeError:
        return _result(
            request,
            status=ValidationStatus.INVALID,
            started_at=started_at,
            started_monotonic=started_monotonic,
            start_cpu=start_cpu,
            errors=(_issue("input_too_large", "Input exceeds the configured byte limit"),),
            input_identity=input_identity,
            data_format=data_format,
        )
    except (UnsafeArtifactPathError, FileNotFoundError, PermissionError, OSError):
        return _result(
            request,
            status=ValidationStatus.FAILED,
            started_at=started_at,
            started_monotonic=started_monotonic,
            start_cpu=start_cpu,
            errors=(_issue("file_not_found", "Input artifact is unavailable or unsafe"),),
            input_identity=input_identity,
            data_format=data_format,
        )
    except (ValidationError, ValueError, TypeError):
        return _result(
            request,
            status=ValidationStatus.FAILED,
            started_at=started_at,
            started_monotonic=started_monotonic,
            start_cpu=start_cpu,
            errors=(_issue("parser_failure", "Input parser failed unexpectedly"),),
            input_identity=input_identity,
            data_format=data_format,
        )
    except Exception:
        return _result(
            request,
            status=ValidationStatus.FAILED,
            started_at=started_at,
            started_monotonic=started_monotonic,
            start_cpu=start_cpu,
            errors=(_issue("internal_error", "Validation worker failed unexpectedly"),),
            input_identity=input_identity,
            data_format=data_format,
        )
