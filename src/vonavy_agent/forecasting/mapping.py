from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from vonavy_agent.forecasting.contracts import ADAPTER_ID, MODEL_SOURCE_REVISION, ForecastMapping

_NAME_NORMALIZER = re.compile(r"[^a-z0-9]+")

_TIMESTAMP_NAMES = ("date", "datekey", "timestamp", "datetime", "day", "ds")
_ENTITY_NAMES = ("productid", "product", "sku", "itemid", "item", "entity", "store", "series")
_TARGET_NAMES = ("quantity", "qty", "demand", "sales", "units", "target", "y")
_AVAILABILITY_NAMES = ("productavailable", "available", "availability", "instock", "in_stock")
_KNOWN_FUTURE_TOKENS = (
    "campaign",
    "promo",
    "promotion",
    "discount",
    "price",
    "holiday",
    "event",
    "coupon",
    "sale",
)
_STATIC_TOKENS = ("brand", "category", "subcategory", "region", "storetype", "producttype")
_LEAKAGE_TOKENS = ("futuretarget", "prediction", "forecast", "label", "outcome")


@dataclass(frozen=True, slots=True)
class MappingCandidate:
    column: str
    confidence: float
    reason: str


def _normalise(value: str) -> str:
    return _NAME_NORMALIZER.sub("", value.casefold())


def _rank(columns: Iterable[str], preferred: tuple[str, ...]) -> list[MappingCandidate]:
    candidates: list[MappingCandidate] = []
    for column in columns:
        normalised = _normalise(column)
        if normalised in preferred:
            rank = preferred.index(normalised)
            candidates.append(
                MappingCandidate(
                    column, max(0.7, 0.99 - rank * 0.04), f"name matches {preferred[rank]}"
                )
            )
        else:
            partial = next(
                (token for token in preferred if token in normalised or normalised in token), None
            )
            if partial:
                candidates.append(MappingCandidate(column, 0.55, f"name resembles {partial}"))
    return sorted(candidates, key=lambda item: (-item.confidence, item.column.casefold()))


def _candidate_payload(candidate: MappingCandidate) -> dict[str, object]:
    return {
        "column": candidate.column,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
    }


def suggest_forecast_mapping(frame: pd.DataFrame) -> dict[str, Any]:
    columns = [str(column) for column in frame.columns]
    timestamp = _rank(columns, _TIMESTAMP_NAMES)
    entity = _rank(columns, _ENTITY_NAMES)
    target = _rank(columns, _TARGET_NAMES)
    availability = _rank(columns, _AVAILABILITY_NAMES)

    used = {
        timestamp[0].column if timestamp else None,
        entity[0].column if entity else None,
        target[0].column if target else None,
        availability[0].column if availability else None,
    }
    known_numeric: list[str] = []
    known_categorical: list[str] = []
    static_numeric: list[str] = []
    static_categorical: list[str] = []
    excluded: list[str] = []
    warnings: list[str] = []

    for column in columns:
        if column in used:
            continue
        name = _normalise(column)
        if any(token in name for token in _LEAKAGE_TOKENS):
            excluded.append(column)
            warnings.append(f"{column!r} looks outcome-derived and was excluded")
            continue
        if any(token in name for token in _KNOWN_FUTURE_TOKENS):
            if pd.api.types.is_numeric_dtype(frame[column]) or pd.api.types.is_bool_dtype(
                frame[column]
            ):
                known_numeric.append(column)
            else:
                known_categorical.append(column)
            continue
        if any(token in name for token in _STATIC_TOKENS):
            if pd.api.types.is_numeric_dtype(frame[column]):
                static_numeric.append(column)
            else:
                static_categorical.append(column)

    ambiguous = []
    for role, candidates in (
        ("timestamp", timestamp),
        ("entity", entity),
        ("target", target),
        ("availability", availability),
    ):
        if len(candidates) > 1 and candidates[0].confidence - candidates[1].confidence < 0.15:
            ambiguous.append(role)
    if ambiguous:
        warnings.append("Ambiguous roles require confirmation: " + ", ".join(ambiguous))

    return {
        "timestamp": [_candidate_payload(candidate) for candidate in timestamp],
        "entity": [_candidate_payload(candidate) for candidate in entity],
        "target": [_candidate_payload(candidate) for candidate in target],
        "availability": [_candidate_payload(candidate) for candidate in availability],
        "suggested": {
            "timestamp_column": timestamp[0].column if timestamp else None,
            "entity_column": entity[0].column if entity else None,
            "target_column": target[0].column if target else None,
            "availability_column": availability[0].column if availability else None,
            "known_future_numeric": known_numeric,
            "known_future_categorical": known_categorical,
            "static_numeric": static_numeric,
            "static_categorical": static_categorical,
            "excluded": excluded,
        },
        "warnings": warnings,
        "requires_confirmation": True,
    }


def validate_forecast_mapping(frame: pd.DataFrame, mapping: ForecastMapping) -> tuple[str, ...]:
    columns = set(str(column) for column in frame.columns)
    missing = sorted(set(mapping.required_columns) - columns)
    if missing:
        raise ValueError("Mapped columns are missing: " + ", ".join(missing))
    warnings: list[str] = []
    timestamp = pd.to_datetime(frame[mapping.timestamp_column], errors="coerce")
    if timestamp.isna().any():
        raise ValueError("timestamp column contains unparseable values")
    target = pd.to_numeric(frame[mapping.target_column], errors="coerce")
    historical = target.notna()
    if not historical.any():
        raise ValueError("target column contains no observed values")
    if (target[historical] < 0).any():
        raise ValueError("target values must be nonnegative")
    if mapping.availability_column:
        values = frame[mapping.availability_column].dropna()
        accepted = {True, False, "0", "1", "true", "false", "True", "False"}
        if not set(values.unique()).issubset(accepted):
            raise ValueError("availability column must be boolean-like")
    if mapping.entity_column is None:
        warnings.append("No entity column selected; the dataset will be treated as one series")
    return tuple(warnings)


def build_forecast_plan(
    frame: pd.DataFrame,
    mapping: ForecastMapping,
    training_end: date,
) -> dict[str, Any]:
    warnings = list(validate_forecast_mapping(frame, mapping))
    timestamps = pd.to_datetime(frame[mapping.timestamp_column], errors="raise").dt.normalize()
    target = pd.to_numeric(frame[mapping.target_column], errors="coerce")
    training_end_ts = pd.Timestamp(training_end)
    if not ((timestamps <= training_end_ts) & target.notna()).any():
        raise ValueError("no observed target exists on or before training_end")
    future_dates = pd.date_range(training_end_ts + pd.Timedelta(days=1), periods=7, freq="D")
    uploaded_future = set(timestamps[timestamps > training_end_ts].unique())
    missing_future = [
        value.date().isoformat()
        for value in future_dates
        if value.to_datetime64() not in uploaded_future
    ]
    if missing_future and (mapping.known_future_numeric or mapping.known_future_categorical):
        warnings.append("Known-future features are selected but some forecast dates are absent")
    entities = (
        1
        if mapping.entity_column is None
        else int(frame[mapping.entity_column].astype("string").nunique(dropna=False))
    )
    observed = frame.loc[(timestamps <= training_end_ts) & target.notna()]
    return {
        "adapter_id": ADAPTER_ID,
        "adapter_source_revision": MODEL_SOURCE_REVISION,
        "mapping": mapping.model_dump(mode="json"),
        "training_end": training_end.isoformat(),
        "forecast_start": future_dates[0].date().isoformat(),
        "forecast_end": future_dates[-1].date().isoformat(),
        "horizon_days": 7,
        "rows": len(frame),
        "observed_rows": len(observed),
        "entities": entities,
        "uploaded_future_dates": 7 - len(missing_future),
        "missing_future_dates": missing_future,
        "warnings": warnings,
        "resource_class": "cpu-small",
        "maximum_runtime_seconds": 3600,
    }


def estimate_forecast_run(plan: dict[str, Any]) -> dict[str, Any]:
    rows = int(plan["rows"])
    entities = int(plan["entities"])
    relative = min(1.0, rows / 500_000 + entities / 20_000)
    return {
        "resource_class": "cpu-small",
        "vcpus": 1,
        "memory_mib": 4096,
        "maximum_runtime_seconds": 3600,
        "relative_size": round(relative, 4),
        "gpu": False,
    }


def confirmation_token_for_forecast_plan(
    *,
    owner_id: str,
    dataset_version_id: str,
    dataset_sha256: str,
    mapping: ForecastMapping,
    training_end: date,
    source_revision: str,
    limits: dict[str, int],
) -> str:
    document = {
        "owner_id": owner_id,
        "dataset_version_id": dataset_version_id,
        "dataset_sha256": dataset_sha256,
        "mapping": mapping.model_dump(mode="json"),
        "training_end": training_end.isoformat(),
        "forecast_start": (training_end + timedelta(days=1)).isoformat(),
        "forecast_end": (training_end + timedelta(days=7)).isoformat(),
        "adapter_id": ADAPTER_ID,
        "adapter_source_revision": MODEL_SOURCE_REVISION,
        "leakage_policy_version": "daily-direct-v1",
        "source_revision": source_revision,
        "limits": limits,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
