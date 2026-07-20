from __future__ import annotations

import csv
import math
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from vonavy_agent.validation_contracts import (
    BooleanStatistics,
    ColumnLogicalType,
    ColumnProfile,
    NumericStatistics,
    QuantileName,
    StringStatistics,
    TemporalStatistics,
    TopValue,
    ValidationIssue,
    ValidationLimits,
)


class ScanProblem(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        invalid: bool,
        column: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.invalid = invalid
        self.column = column


class Deadline:
    def __init__(self, seconds: int) -> None:
        self._deadline = time.monotonic() + seconds

    def check(self) -> None:
        if time.monotonic() > self._deadline:
            raise ScanProblem(
                "execution_timeout",
                "Validation exceeded the configured execution time limit",
                invalid=False,
            )


@dataclass(slots=True)
class ReservoirSampler:
    capacity: int
    seed: int
    string_limit: int
    seen: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    values_exceeding_limit: dict[str, int] = field(default_factory=dict)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def add_frame(self, frame: pd.DataFrame) -> None:
        columns = [str(column) for column in frame.columns]
        for values in frame.itertuples(index=False, name=None):
            normalized: dict[str, Any] = {}
            for column, value in zip(columns, values, strict=True):
                if isinstance(value, str) and len(value) > self.string_limit:
                    self.values_exceeding_limit[column] = (
                        self.values_exceeding_limit.get(column, 0) + 1
                    )
                    normalized[column] = value[: self.string_limit]
                elif isinstance(value, np.generic):
                    normalized[column] = value.item()
                else:
                    normalized[column] = value
            self.seen += 1
            if len(self.rows) < self.capacity:
                self.rows.append(normalized)
            else:
                replacement = self._rng.randrange(self.seen)
                if replacement < self.capacity:
                    self.rows[replacement] = normalized


@dataclass(frozen=True, slots=True)
class ScanSummary:
    columns: tuple[str, ...]
    physical_types: dict[str, tuple[str, ...]]
    row_count: int
    null_counts: dict[str, int]
    sample: pd.DataFrame
    values_exceeding_limit: dict[str, int]


def _validate_columns(columns: list[str], limits: ValidationLimits) -> None:
    if not columns:
        raise ScanProblem("empty_dataset", "Dataset contains no columns", invalid=True)
    if len(columns) > limits.max_columns:
        raise ScanProblem(
            "too_many_columns",
            "Dataset exceeds the configured column limit",
            invalid=True,
        )
    if any(not str(column).strip() for column in columns):
        raise ScanProblem("empty_column_name", "Column names must not be empty", invalid=True)
    duplicates = sorted(name for name, count in Counter(columns).items() if count > 1)
    if duplicates:
        raise ScanProblem(
            "duplicate_columns",
            "Dataset column names must be unique",
            invalid=True,
        )


def _csv_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            for row in reader:
                if row and any(value.strip() for value in row):
                    return [str(value) for value in row]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ScanProblem("malformed_csv", "CSV header could not be read", invalid=False) from exc
    return []


def _sample_seed(checksum: str) -> int:
    return int(checksum[:16], 16)


def scan_csv(
    path: Path,
    limits: ValidationLimits,
    checksum: str,
    deadline: Deadline,
) -> ScanSummary:
    columns = _csv_header(path)
    _validate_columns(columns, limits)
    sampler = ReservoirSampler(
        limits.max_profile_rows,
        _sample_seed(checksum),
        limits.max_string_sample_length,
    )
    null_counts = dict.fromkeys(columns, 0)
    physical: dict[str, set[str]] = {column: set() for column in columns}
    row_count = 0
    try:
        chunks = pd.read_csv(
            path,
            encoding="utf-8-sig",
            chunksize=10_000,
            on_bad_lines="error",
            low_memory=False,
        )
        for chunk in chunks:
            deadline.check()
            actual = [str(column) for column in chunk.columns]
            if actual != columns:
                raise ScanProblem(
                    "malformed_csv",
                    "CSV parser produced a schema inconsistent with its header",
                    invalid=False,
                )
            row_count += len(chunk)
            if row_count > limits.max_rows:
                raise ScanProblem(
                    "too_many_rows",
                    "Dataset exceeds the configured row limit",
                    invalid=True,
                )
            for column in columns:
                null_counts[column] += int(chunk[column].isna().sum())
                physical[column].add(str(chunk[column].dtype))
            sampler.add_frame(chunk)
    except ScanProblem:
        raise
    except pd.errors.EmptyDataError as exc:
        raise ScanProblem(
            "empty_dataset",
            "Dataset contains no tabular data",
            invalid=True,
        ) from exc
    except (OSError, UnicodeError, pd.errors.ParserError, ValueError) as exc:
        raise ScanProblem(
            "malformed_csv",
            "CSV content could not be parsed",
            invalid=False,
        ) from exc
    if row_count == 0:
        raise ScanProblem("empty_dataset", "Dataset contains a header but no rows", invalid=True)
    return ScanSummary(
        columns=tuple(columns),
        physical_types={name: tuple(sorted(values)) for name, values in physical.items()},
        row_count=row_count,
        null_counts=null_counts,
        sample=pd.DataFrame(sampler.rows, columns=columns),
        values_exceeding_limit=sampler.values_exceeding_limit,
    )


def _load_pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ModuleNotFoundError as exc:
        raise ScanProblem(
            "parser_failure",
            "Parquet support is unavailable in this worker environment",
            invalid=False,
        ) from exc
    return pa, pq


def _primitive_arrow_type(data_type: Any, pa: Any) -> bool:
    return not any(
        predicate(data_type)
        for predicate in (
            pa.types.is_list,
            pa.types.is_large_list,
            pa.types.is_fixed_size_list,
            pa.types.is_struct,
            pa.types.is_map,
            pa.types.is_union,
            pa.types.is_dictionary,
        )
    )


def scan_parquet(
    path: Path, limits: ValidationLimits, checksum: str, deadline: Deadline
) -> ScanSummary:
    pa, pq = _load_pyarrow()
    try:
        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise ScanProblem(
            "malformed_parquet",
            "Parquet metadata could not be read",
            invalid=False,
        ) from exc
    columns = [str(name) for name in schema.names]
    _validate_columns(columns, limits)
    unsupported = [field.name for field in schema if not _primitive_arrow_type(field.type, pa)]
    if unsupported:
        raise ScanProblem(
            "unsupported_column_type",
            "Parquet contains nested or unsupported column types",
            invalid=True,
            column=unsupported[0],
        )
    metadata_rows = int(parquet.metadata.num_rows)
    if metadata_rows > limits.max_rows:
        raise ScanProblem("too_many_rows", "Dataset exceeds the configured row limit", invalid=True)
    if metadata_rows == 0:
        raise ScanProblem("empty_dataset", "Dataset contains no rows", invalid=True)
    sampler = ReservoirSampler(
        limits.max_profile_rows,
        _sample_seed(checksum),
        limits.max_string_sample_length,
    )
    null_counts = dict.fromkeys(columns, 0)
    row_count = 0
    try:
        for batch in parquet.iter_batches(batch_size=10_000, use_threads=False):
            deadline.check()
            frame = batch.to_pandas()
            row_count += len(frame)
            for column in columns:
                null_counts[column] += int(frame[column].isna().sum())
            sampler.add_frame(frame)
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise ScanProblem(
            "malformed_parquet",
            "Parquet row groups could not be read",
            invalid=False,
        ) from exc
    if row_count != metadata_rows:
        raise ScanProblem(
            "malformed_parquet",
            "Parquet row count did not match its metadata",
            invalid=False,
        )
    return ScanSummary(
        columns=tuple(columns),
        physical_types={field.name: (str(field.type),) for field in schema},
        row_count=row_count,
        null_counts=null_counts,
        sample=pd.DataFrame(sampler.rows, columns=columns),
        values_exceeding_limit=sampler.values_exceeding_limit,
    )


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _logical_type(series: pd.Series, physical_types: tuple[str, ...]) -> ColumnLogicalType:
    non_null = series.dropna()
    physical = " ".join(physical_types).lower()
    if pd.api.types.is_bool_dtype(series) or "bool" in physical:
        return "boolean"
    if pd.api.types.is_numeric_dtype(series) or any(
        token in physical for token in ("int", "float", "double", "decimal", "uint")
    ):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series) or "timestamp" in physical:
        return "timestamp"
    if "date" in physical:
        return "date"
    if non_null.empty:
        return "string" if any(token in physical for token in ("string", "object")) else "other"
    if all(isinstance(value, str) for value in non_null):
        strings = non_null.astype("string")
        date_like = strings.str.contains(r"[-/:T]", regex=True, na=False)
        if bool(date_like.all()):
            parsed = pd.to_datetime(strings, errors="coerce", utc=True, format="mixed")
            valid_ratio = float(parsed.notna().mean())
            if valid_ratio >= 0.95:
                has_time = bool(strings.str.contains(r"[T :]", regex=True).any())
                return "timestamp" if has_time else "date"
        return "string"
    return "other"


def _numeric_statistics(series: pd.Series) -> NumericStatistics:
    numeric = pd.to_numeric(series, errors="coerce").dropna().astype(float)
    if numeric.empty:
        return NumericStatistics()
    finite_mask = np.isfinite(numeric.to_numpy())
    finite = numeric.iloc[np.flatnonzero(finite_mask)]
    quantile_values: dict[QuantileName, float] = {}
    if not finite.empty:
        requested: tuple[tuple[QuantileName, float], ...] = (
            ("p01", 0.01),
            ("p05", 0.05),
            ("p25", 0.25),
            ("p50", 0.50),
            ("p75", 0.75),
            ("p95", 0.95),
            ("p99", 0.99),
        )
        for name, quantile in requested:
            value = _finite_float(finite.quantile(quantile))
            if value is not None:
                quantile_values[name] = value
    return NumericStatistics(
        minimum=_finite_float(finite.min()) if not finite.empty else None,
        maximum=_finite_float(finite.max()) if not finite.empty else None,
        mean=_finite_float(finite.mean()) if not finite.empty else None,
        standard_deviation=_finite_float(finite.std()) if len(finite) > 1 else None,
        quantiles=quantile_values,
        zero_count=int((finite == 0).sum()),
        negative_count=int((finite < 0).sum()),
        positive_count=int((finite > 0).sum()),
        non_finite_count=int((~finite_mask).sum()),
    )


def _string_statistics(
    series: pd.Series,
    limits: ValidationLimits,
    values_exceeding_limit: int,
    sampled: bool,
) -> StringStatistics:
    values = series.dropna().astype("string")
    if values.empty:
        return StringStatistics(
            distinct_is_approximate=sampled,
            values_exceeding_sample_limit=values_exceeding_limit,
        )
    lengths = values.str.len()
    counts = values.value_counts(dropna=True)
    top_values = tuple(
        TopValue(
            value=str(value)[: limits.max_string_sample_length],
            count=int(count),
            truncated=len(str(value)) > limits.max_string_sample_length,
        )
        for value, count in counts.head(limits.max_distinct_values).items()
    )
    return StringStatistics(
        minimum_length=int(lengths.min()),
        maximum_length=int(lengths.max()),
        mean_length=float(lengths.mean()),
        empty_string_count=int((values == "").sum()),
        distinct_count=int(counts.size),
        distinct_is_approximate=sampled,
        values_exceeding_sample_limit=values_exceeding_limit,
        top_values=top_values,
    )


def _boolean_statistics(series: pd.Series) -> BooleanStatistics:
    values = series.dropna().astype(bool)
    return BooleanStatistics(
        true_count=int(values.sum()),
        false_count=int((~values).sum()),
    )


def _temporal_statistics(
    series: pd.Series,
    logical_type: Literal["date", "timestamp"],
) -> TemporalStatistics:
    values = series.dropna()
    if values.empty:
        return TemporalStatistics()
    timezone_aware = isinstance(series.dtype, pd.DatetimeTZDtype)
    if not timezone_aware and logical_type == "timestamp":
        strings = values.astype("string")
        timezone_aware = bool(
            strings.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False).any()
        )
    parsed = pd.to_datetime(values, errors="coerce", utc=True, format="mixed")
    valid = parsed.dropna()
    return TemporalStatistics(
        minimum=valid.min().isoformat() if not valid.empty else None,
        maximum=valid.max().isoformat() if not valid.empty else None,
        invalid_count=int(parsed.isna().sum()),
        timezone_aware=timezone_aware if logical_type == "timestamp" else False,
    )


def _profile_column(
    summary: ScanSummary,
    limits: ValidationLimits,
    column: str,
    sampled: bool,
) -> tuple[ColumnProfile, tuple[ValidationIssue, ...]]:
    series = summary.sample[column]
    logical = _logical_type(series, summary.physical_types[column])
    null_count = summary.null_counts[column]
    non_null_count = summary.row_count - null_count
    null_ratio = null_count / summary.row_count if summary.row_count else 0.0
    sample_size = int(series.notna().sum())
    physical_type = "|".join(summary.physical_types[column])
    statistics_basis: Literal["sampled", "exact"] = "sampled" if sampled else "exact"
    warnings: list[ValidationIssue] = []
    if logical == "numeric":
        numeric = _numeric_statistics(series)
        profile = ColumnProfile(
            name=column,
            logical_type=logical,
            physical_type=physical_type,
            row_count=summary.row_count,
            null_count=null_count,
            null_ratio=null_ratio,
            non_null_count=non_null_count,
            statistics_basis=statistics_basis,
            sample_size=sample_size,
            numeric=numeric,
        )
        if numeric.non_finite_count:
            warnings.append(
                ValidationIssue(
                    code="non_finite_values",
                    message="Numeric column contains NaN or infinite values",
                    column=column,
                    count=numeric.non_finite_count,
                )
            )
    elif logical == "string":
        profile = ColumnProfile(
            name=column,
            logical_type=logical,
            physical_type=physical_type,
            row_count=summary.row_count,
            null_count=null_count,
            null_ratio=null_ratio,
            non_null_count=non_null_count,
            statistics_basis=statistics_basis,
            sample_size=sample_size,
            string=_string_statistics(
                series,
                limits,
                summary.values_exceeding_limit.get(column, 0),
                sampled,
            ),
        )
        if summary.values_exceeding_limit.get(column, 0):
            warnings.append(
                ValidationIssue(
                    code="string_values_truncated",
                    message="Long strings were truncated in the profiling sample",
                    column=column,
                    count=summary.values_exceeding_limit[column],
                )
            )
    elif logical == "boolean":
        profile = ColumnProfile(
            name=column,
            logical_type=logical,
            physical_type=physical_type,
            row_count=summary.row_count,
            null_count=null_count,
            null_ratio=null_ratio,
            non_null_count=non_null_count,
            statistics_basis=statistics_basis,
            sample_size=sample_size,
            boolean=_boolean_statistics(series),
        )
    elif logical == "date" or logical == "timestamp":
        profile = ColumnProfile(
            name=column,
            logical_type=logical,
            physical_type=physical_type,
            row_count=summary.row_count,
            null_count=null_count,
            null_ratio=null_ratio,
            non_null_count=non_null_count,
            statistics_basis=statistics_basis,
            sample_size=sample_size,
            temporal=_temporal_statistics(series, logical),
        )
    else:
        profile = ColumnProfile(
            name=column,
            logical_type=logical,
            physical_type=physical_type,
            row_count=summary.row_count,
            null_count=null_count,
            null_ratio=null_ratio,
            non_null_count=non_null_count,
            statistics_basis=statistics_basis,
            sample_size=sample_size,
        )
    return profile, tuple(warnings)


def build_profiles(
    summary: ScanSummary,
    limits: ValidationLimits,
    deadline: Deadline | None = None,
) -> tuple[tuple[ColumnProfile, ...], tuple[ValidationIssue, ...]]:
    sampled = summary.row_count > len(summary.sample)
    profiles: list[ColumnProfile] = []
    warnings: list[ValidationIssue] = []
    if sampled:
        warnings.append(
            ValidationIssue(
                code="profile_sampled",
                message="Expensive column statistics were computed on a deterministic sample",
                count=len(summary.sample),
                detail={"total_rows": summary.row_count},
            )
        )
    for column in summary.columns:
        if deadline is not None:
            deadline.check()
        profile, column_warnings = _profile_column(summary, limits, column, sampled)
        profiles.append(profile)
        warnings.extend(column_warnings)
    warnings.sort(key=lambda issue: (issue.code, issue.column or "", issue.message))
    return tuple(profiles), tuple(warnings)
