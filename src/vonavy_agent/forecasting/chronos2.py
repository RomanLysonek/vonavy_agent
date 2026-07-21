"""Pinned Chronos-2 zero-shot adapter for the seven-day forecast contract."""

from __future__ import annotations

import importlib.metadata
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from vonavy_agent.forecasting.contracts import (
    CHRONOS2_ADAPTER_ID,
    CHRONOS2_SOURCE_REPOSITORY,
    CHRONOS2_SOURCE_REVISION,
    AdapterIdentity,
    ArtifactReference,
    ForecastArtifacts,
    ForecastMapping,
    ForecastProfile,
    ForecastResult,
    ForecastStatus,
    ForecastTiming,
    HoldoutMetrics,
    InputIdentity,
    ModelArtifactManifest,
)
from vonavy_agent.forecasting.model import ForecastRunOutput, sha256_file
from vonavy_agent.forecasting.panel import PreparedPanel, build_panel_frames, prepare_daily_panel

CHRONOS2_MODEL_ID = "amazon/chronos-2"
CHRONOS2_MODEL_REVISION = "29ec3766d36d6f73f0696f85560a422f50e8498c"
CHRONOS2_LICENSE = "Apache-2.0"
CHRONOS2_LOCAL_PATH = "/opt/models/chronos-2"
CHRONOS2_QUANTILES = (0.1, 0.5, 0.9)
CHRONOS2_CONTEXT_LIMIT = 8192
CHRONOS2_BATCH_SIZE = 32
CHRONOS2_MAX_ROWS = 500_000
CHRONOS2_MAX_ENTITIES = 100


class ChronosPipeline(Protocol):
    def predict_df(self, **kwargs: object) -> pd.DataFrame: ...


@dataclass(slots=True)
class ChronosFrames:
    context: pd.DataFrame
    future: pd.DataFrame
    eligible_entities: tuple[str, ...]
    categorical_levels: dict[str, tuple[str, ...]]
    feature_columns: tuple[str, ...]


def _package_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in (
        "chronos-forecasting",
        "huggingface-hub",
        "numpy",
        "pandas",
        "pyarrow",
        "pydantic",
        "torch",
    ):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "unavailable"
    return result


@lru_cache(maxsize=1)
def load_chronos2_pipeline(model_path: str | None = None) -> ChronosPipeline:
    """Load the immutable image-baked Chronos-2 weights once per worker."""
    try:
        import torch
        from chronos import BaseChronosPipeline
    except ImportError as exc:  # pragma: no cover - deployment packaging guard
        raise RuntimeError("Chronos-2 runtime dependencies are not installed") from exc

    local_path = (
        model_path or os.environ.get("CHRONOS2_MODEL_PATH") or CHRONOS2_LOCAL_PATH
    ).strip()
    if not Path(local_path).is_dir():
        raise RuntimeError("Pinned Chronos-2 model directory is missing from the worker image")
    pipeline = BaseChronosPipeline.from_pretrained(
        local_path,
        device_map="cpu",
        torch_dtype=torch.float32,
        local_files_only=True,
    )
    return pipeline  # type: ignore[no-any-return]


def _encode_categories(
    context: pd.DataFrame,
    future: pd.DataFrame,
    columns: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    levels: dict[str, tuple[str, ...]] = {}
    for column in columns:
        combined = pd.concat([context[column], future[column]], ignore_index=True)
        values = combined.astype("string").fillna("__missing__")
        column_levels = tuple(sorted(values.unique().tolist()))
        mapping = {value: index for index, value in enumerate(column_levels)}
        context[column] = (
            context[column].astype("string").fillna("__missing__").map(mapping).astype(float)
        )
        future[column] = (
            future[column].astype("string").fillna("__missing__").map(mapping).astype(float)
        )
        levels[column] = column_levels
    return levels


def _chronos_frames(prepared: PreparedPanel, origin: pd.Timestamp) -> ChronosFrames:
    context_rows: list[pd.DataFrame] = []
    future_rows: list[pd.DataFrame] = []
    eligible: list[str] = []
    mapping = prepared.mapping
    known_numeric = mapping.known_future_numeric + mapping.static_numeric
    known_categorical = mapping.known_future_categorical + mapping.static_categorical
    covariates = known_numeric + known_categorical

    for entity in prepared.entities:
        group = prepared.frame.loc[prepared.frame["entity"].eq(entity)].sort_values("timestamp")
        historical = group.loc[group["timestamp"].le(origin)].copy()
        observed = historical["observed_target"]
        if len(historical) < 3 or not observed.notna().any():
            continue
        historical = historical.tail(CHRONOS2_CONTEXT_LIMIT)
        context = pd.DataFrame(
            {
                "item_id": entity,
                "timestamp": historical["timestamp"].to_numpy(),
                "target": historical["observed_target"].to_numpy(dtype=float),
                "was_observed": historical["observed_target"].notna().astype(float).to_numpy(),
                "was_available": historical["available"].astype(float).to_numpy(),
            }
        )
        target_dates = pd.date_range(origin + pd.Timedelta(days=1), periods=7, freq="D")
        future_source = group.loc[group["timestamp"].isin(target_dates)].copy()
        if len(future_source) != 7:
            continue
        future = pd.DataFrame(
            {
                "item_id": entity,
                "timestamp": future_source["timestamp"].to_numpy(),
            }
        )
        for column in covariates:
            context[column] = historical[column].to_numpy()
            future[column] = future_source[column].to_numpy()
        for column in known_numeric:
            context[column] = pd.to_numeric(context[column], errors="coerce")
            future[column] = pd.to_numeric(future[column], errors="coerce")
        context_rows.append(context)
        future_rows.append(future)
        eligible.append(entity)

    base_context_columns = ("item_id", "timestamp", "target", "was_observed", "was_available")
    base_future_columns = ("item_id", "timestamp")
    if not context_rows:
        return ChronosFrames(
            context=pd.DataFrame(columns=base_context_columns + covariates),
            future=pd.DataFrame(columns=base_future_columns + covariates),
            eligible_entities=(),
            categorical_levels={},
            feature_columns=("was_observed", "was_available", *covariates),
        )
    context_frame = pd.concat(context_rows, ignore_index=True)
    future_frame = pd.concat(future_rows, ignore_index=True)
    levels = _encode_categories(context_frame, future_frame, known_categorical)
    return ChronosFrames(
        context=context_frame,
        future=future_frame,
        eligible_entities=tuple(eligible),
        categorical_levels=levels,
        feature_columns=("was_observed", "was_available", *covariates),
    )


def _quantile_column(frame: pd.DataFrame, level: float) -> str | None:
    candidates = (
        str(level),
        f"{level:.1f}",
        f"quantile_{level}",
        f"quantile_{level:.1f}",
    )
    return next((candidate for candidate in candidates if candidate in frame.columns), None)


def _predict_origin(
    prepared: PreparedPanel,
    origin: pd.Timestamp,
    pipeline: ChronosPipeline,
) -> tuple[pd.DataFrame, ChronosFrames]:
    baseline_frames = build_panel_frames(prepared, origin, origin)
    output = baseline_frames.predict[["entity", "target_date", "horizon", "target_baseline"]].copy()
    output = output.rename(columns={"target_date": "timestamp"})
    output["prediction"] = output["target_baseline"].to_numpy(dtype=float)
    output["quantile_0.1"] = output["prediction"]
    output["quantile_0.5"] = output["prediction"]
    output["quantile_0.9"] = output["prediction"]
    output["fallback_used"] = True
    output["no_context"] = True

    frames = _chronos_frames(prepared, origin)
    if not frames.eligible_entities:
        output["prediction"] = np.clip(
            np.nan_to_num(output["prediction"], nan=0.0, posinf=0.0, neginf=0.0),
            0.0,
            None,
        )
        return output, frames

    predicted = pipeline.predict_df(
        df=frames.context,
        future_df=frames.future,
        prediction_length=7,
        quantile_levels=list(CHRONOS2_QUANTILES),
        id_column="item_id",
        timestamp_column="timestamp",
        target="target",
        batch_size=CHRONOS2_BATCH_SIZE,
        cross_learning=True,
        validate_inputs=True,
        freq="D",
    )
    if not isinstance(predicted, pd.DataFrame):
        raise ValueError("Chronos-2 returned a non-tabular prediction")
    required = {"item_id", "timestamp"}
    missing = sorted(required - set(predicted.columns))
    if missing:
        raise ValueError("Chronos-2 output is missing columns: " + ", ".join(missing))
    predicted = predicted.copy()
    if "target_name" in predicted.columns:
        predicted = predicted.loc[predicted["target_name"].astype(str).eq("target")]
    predicted["entity"] = predicted["item_id"].astype(str)
    predicted["timestamp"] = pd.to_datetime(predicted["timestamp"], errors="coerce").dt.normalize()
    if predicted[["entity", "timestamp"]].duplicated().any():
        raise ValueError("Chronos-2 returned duplicate entity/timestamp keys")
    point_column = (
        "predictions" if "predictions" in predicted.columns else _quantile_column(predicted, 0.5)
    )
    if point_column is None:
        raise ValueError("Chronos-2 output contains no point or median forecast")
    selected = predicted[["entity", "timestamp", point_column]].rename(
        columns={point_column: "model_prediction"}
    )
    for level in CHRONOS2_QUANTILES:
        source = _quantile_column(predicted, level)
        name = f"model_quantile_{level:.1f}"
        selected[name] = (
            predicted[source].to_numpy() if source else predicted[point_column].to_numpy()
        )

    merged = output.merge(selected, on=["entity", "timestamp"], how="left", validate="one_to_one")
    point = pd.to_numeric(merged["model_prediction"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(point)
    baseline = merged["target_baseline"].to_numpy(dtype=float)
    point = np.where(valid, point, baseline)
    point = np.clip(np.nan_to_num(point, nan=0.0, posinf=0.0, neginf=0.0), 0.0, None)
    quantiles = []
    for level in CHRONOS2_QUANTILES:
        values = pd.to_numeric(merged[f"model_quantile_{level:.1f}"], errors="coerce").to_numpy(
            dtype=float
        )
        quantiles.append(np.where(valid & np.isfinite(values), values, point))
    quantile_matrix = np.sort(np.clip(np.column_stack(quantiles), 0.0, None), axis=1)
    merged["prediction"] = point
    merged["quantile_0.1"] = quantile_matrix[:, 0]
    merged["quantile_0.5"] = quantile_matrix[:, 1]
    merged["quantile_0.9"] = quantile_matrix[:, 2]
    merged["fallback_used"] = ~valid
    merged["no_context"] = ~merged["entity"].isin(frames.eligible_entities)
    return merged[output.columns], frames


def _holdout_metrics(
    prepared: PreparedPanel,
    prediction: pd.DataFrame,
    origin: pd.Timestamp,
) -> HoldoutMetrics:
    actual_lookup = prepared.frame.set_index(["entity", "timestamp"])["observed_target"]
    actual = np.asarray(
        [actual_lookup.get((row.entity, row.timestamp), np.nan) for row in prediction.itertuples()],
        dtype=float,
    )
    predicted = prediction["prediction"].to_numpy(dtype=float)
    valid = np.isfinite(actual) & np.isfinite(predicted)
    if not valid.any():
        return HoldoutMetrics(
            supported=False,
            origin=origin.date(),
            rows=0,
            coverage=0.0,
            unsupported_reason="No common observed holdout rows",
        )
    actual_valid = actual[valid]
    predicted_valid = predicted[valid]
    denominator = float(np.abs(actual_valid).sum())
    wape = (
        float(np.abs(actual_valid - predicted_valid).sum() / denominator)
        if denominator > 0
        else None
    )
    return HoldoutMetrics(
        supported=wape is not None,
        origin=origin.date(),
        rows=int(valid.sum()),
        wape=wape,
        mae=float(np.mean(np.abs(actual_valid - predicted_valid))),
        bias=float(np.mean(predicted_valid - actual_valid)),
        coverage=float(valid.sum() / max(1, len(prediction))),
        unsupported_reason=None if wape is not None else "Holdout actual denominator is zero",
    )


def run_chronos2_forecast(
    *,
    raw: pd.DataFrame,
    mapping: ForecastMapping,
    training_end: pd.Timestamp,
    output_directory: Path,
    owner_id: str,
    dataset_id: str,
    run_id: str,
    input_identity: InputIdentity,
    source_revision: str,
    max_rows: int,
    max_entities: int,
    max_history_days: int,
    pipeline: ChronosPipeline | None = None,
) -> ForecastRunOutput:
    started_at = datetime.now(UTC)
    total_started = time.monotonic()
    prepare_started = time.monotonic()
    prepared = prepare_daily_panel(
        raw,
        mapping,
        training_end.date(),
        max_rows=min(max_rows, CHRONOS2_MAX_ROWS),
        max_entities=min(max_entities, CHRONOS2_MAX_ENTITIES),
        max_history_days=max_history_days,
    )
    prepare_seconds = time.monotonic() - prepare_started
    runtime = pipeline or load_chronos2_pipeline()

    holdout_started = time.monotonic()
    holdout_origin = prepared.training_end - pd.Timedelta(days=7)
    if holdout_origin - prepared.frame["timestamp"].min() < pd.Timedelta(days=35):
        holdout = HoldoutMetrics(
            supported=False,
            origin=holdout_origin.date(),
            rows=0,
            coverage=0.0,
            unsupported_reason="At least 42 history days are required for the recent holdout",
        )
    else:
        try:
            holdout_prediction, _ = _predict_origin(prepared, holdout_origin, runtime)
            holdout = _holdout_metrics(prepared, holdout_prediction, holdout_origin)
        except ValueError as exc:
            holdout = HoldoutMetrics(
                supported=False,
                origin=holdout_origin.date(),
                rows=0,
                coverage=0.0,
                unsupported_reason=str(exc),
            )
    holdout_seconds = time.monotonic() - holdout_started

    forecast_started = time.monotonic()
    forecast, final_frames = _predict_origin(prepared, prepared.training_end, runtime)
    for column in (
        mapping.known_future_numeric
        + mapping.known_future_categorical
        + mapping.static_numeric
        + mapping.static_categorical
    ):
        lookup = prepared.frame.set_index(["entity", "timestamp"])[column]
        forecast[column] = [
            lookup.get((row.entity, row.timestamp), np.nan) for row in forecast.itertuples()
        ]
    output_directory.mkdir(parents=True, exist_ok=True)
    forecast_path = output_directory / "forecast.parquet"
    model_path = output_directory / "chronos-model.json"
    manifest_path = output_directory / "model-manifest.json"
    forecast.to_parquet(forecast_path, index=False)
    model_descriptor = {
        "adapter_id": CHRONOS2_ADAPTER_ID,
        "source_repository": CHRONOS2_SOURCE_REPOSITORY,
        "source_revision": CHRONOS2_SOURCE_REVISION,
        "model_id": CHRONOS2_MODEL_ID,
        "model_revision": CHRONOS2_MODEL_REVISION,
        "license": CHRONOS2_LICENSE,
        "model_path": CHRONOS2_LOCAL_PATH,
        "weights_baked_into_immutable_image": True,
        "runtime_downloads": False,
        "fine_tuned": False,
        "quantiles": list(CHRONOS2_QUANTILES),
    }
    model_path.write_text(
        json.dumps(model_descriptor, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    forecast_seconds = time.monotonic() - forecast_started

    forecast_sha256 = sha256_file(forecast_path)
    model_sha256 = sha256_file(model_path)
    identity = AdapterIdentity(id=CHRONOS2_ADAPTER_ID)
    manifest = ModelArtifactManifest(
        adapter=identity,
        vonavy_agent_source_revision=source_revision,
        owner_id=owner_id,
        dataset_id=dataset_id,
        run_id=run_id,
        input=input_identity,
        mapping=mapping,
        training_end=prepared.training_end.date(),
        seed=42,
        parameters={
            "model_id": CHRONOS2_MODEL_ID,
            "model_revision": CHRONOS2_MODEL_REVISION,
            "license": CHRONOS2_LICENSE,
            "prediction_length": 7,
            "context_limit": CHRONOS2_CONTEXT_LIMIT,
            "batch_size": CHRONOS2_BATCH_SIZE,
            "max_rows": CHRONOS2_MAX_ROWS,
            "max_entities": CHRONOS2_MAX_ENTITIES,
            "quantiles": "0.1,0.5,0.9",
            "cross_learning": True,
            "device": "cpu",
            "fine_tuned": False,
        },
        feature_order=final_frames.feature_columns,
        categorical_levels=final_frames.categorical_levels,
        holdout=holdout,
        package_versions=_package_versions(),
        model_sha256=model_sha256,
        forecast_sha256=forecast_sha256,
    )
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    manifest_sha256 = sha256_file(manifest_path)
    finished_at = datetime.now(UTC)
    result = ForecastResult(
        status=ForecastStatus.SUCCEEDED,
        owner_id=owner_id,
        dataset_id=dataset_id,
        run_id=run_id,
        adapter=identity,
        input=input_identity,
        profile=ForecastProfile(
            rows=len(raw),
            entities=len(prepared.entities),
            history_start=prepared.frame["timestamp"].min().date(),
            training_end=prepared.training_end.date(),
            forecast_start=prepared.forecast_dates[0].date(),
            forecast_end=prepared.forecast_dates[-1].date(),
            trainable_rows=0,
            fallback_rows=int(forecast["fallback_used"].sum()),
        ),
        holdout=holdout,
        artifacts=ForecastArtifacts(
            forecast=ArtifactReference(
                key="forecast.parquet",
                sha256=forecast_sha256,
                byte_size=forecast_path.stat().st_size,
            ),
            model=ArtifactReference(
                key="chronos-model.json",
                sha256=model_sha256,
                byte_size=model_path.stat().st_size,
            ),
            manifest=ArtifactReference(
                key="model-manifest.json",
                sha256=manifest_sha256,
                byte_size=manifest_path.stat().st_size,
            ),
        ),
        warnings=prepared.warnings,
        timing=ForecastTiming(
            prepare_seconds=prepare_seconds,
            holdout_seconds=holdout_seconds,
            fit_seconds=0.0,
            forecast_seconds=forecast_seconds,
            total_seconds=time.monotonic() - total_started,
        ),
        started_at=started_at,
        finished_at=finished_at,
    )
    return ForecastRunOutput(
        result=result,
        manifest=manifest,
        forecast_path=forecast_path,
        model_path=model_path,
        manifest_path=manifest_path,
    )
