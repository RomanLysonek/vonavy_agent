from __future__ import annotations

import hashlib
import importlib.metadata
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from vonavy_agent.forecasting.contracts import (
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
from vonavy_agent.forecasting.evaluation import build_forecast_evaluation
from vonavy_agent.forecasting.panel import (
    PanelFrames,
    PreparedPanel,
    build_panel_frames,
    prepare_daily_panel,
    tree_frame,
)

XGBOOST_PARAMETERS: dict[str, int | float | str | bool] = {
    "n_estimators": 400,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 5,
    "tree_method": "hist",
    "enable_categorical": True,
    "objective": "reg:squarederror",
    "random_state": 42,
    "n_jobs": 1,
    "verbosity": 0,
}


@dataclass(slots=True)
class ForecastRunOutput:
    result: ForecastResult
    manifest: ModelArtifactManifest
    forecast_path: Path
    model_path: Path
    manifest_path: Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fit(frames: PanelFrames) -> Any:
    from xgboost import XGBRegressor

    X = tree_frame(
        frames.train,
        frames.feature_columns,
        frames.categorical_columns,
        frames.categorical_levels,
    )
    target = frames.train["target"].to_numpy(dtype=float)
    baseline = frames.train["target_baseline"].to_numpy(dtype=float)
    y = np.log1p(target) - np.log1p(baseline)
    estimator = XGBRegressor(**XGBOOST_PARAMETERS)
    estimator.fit(X, y)
    return estimator


def _predict(estimator: Any, frames: PanelFrames) -> tuple[np.ndarray, np.ndarray]:
    X = tree_frame(
        frames.predict,
        frames.feature_columns,
        frames.categorical_columns,
        frames.categorical_levels,
    )
    raw = np.asarray(estimator.predict(X), dtype=float)
    baseline = frames.predict["target_baseline"].to_numpy(dtype=float)
    prediction = np.expm1(raw + np.log1p(np.clip(baseline, 0.0, None)))
    fallback = ~np.isfinite(prediction)
    prediction = np.where(fallback, baseline, prediction)
    prediction = np.nan_to_num(prediction, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(prediction, 0.0, None), fallback


def _metrics(
    actual: np.ndarray, prediction: np.ndarray, expected_rows: int, origin: pd.Timestamp
) -> HoldoutMetrics:
    valid = np.isfinite(actual) & np.isfinite(prediction)
    if not valid.any():
        return HoldoutMetrics(
            supported=False,
            origin=origin.date(),
            rows=0,
            coverage=0.0,
            unsupported_reason="No common observed holdout rows",
        )
    a = actual[valid]
    p = prediction[valid]
    denominator = float(np.abs(a).sum())
    wape = float(np.abs(a - p).sum() / denominator) if denominator > 0 else None
    return HoldoutMetrics(
        supported=wape is not None,
        origin=origin.date(),
        rows=int(valid.sum()),
        wape=wape,
        mae=float(np.mean(np.abs(a - p))),
        bias=float(np.mean(p - a)),
        coverage=float(valid.sum() / max(1, expected_rows)),
        unsupported_reason=None if wape is not None else "Holdout actual denominator is zero",
    )


def _package_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ("numpy", "pandas", "xgboost", "pyarrow", "pydantic"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "unavailable"
    return result


def _write_forecast(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(destination, index=False)


def run_xgboost_forecast(
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
) -> ForecastRunOutput:
    started_at = datetime.now(UTC)
    total_started = time.monotonic()
    prepare_started = time.monotonic()
    prepared: PreparedPanel = prepare_daily_panel(
        raw,
        mapping,
        training_end.date(),
        max_rows=max_rows,
        max_entities=max_entities,
        max_history_days=max_history_days,
    )
    prepare_seconds = time.monotonic() - prepare_started

    holdout_started = time.monotonic()
    holdout_origin = prepared.training_end - pd.Timedelta(days=7)
    holdout: HoldoutMetrics
    holdout_actual = np.asarray([], dtype=float)
    holdout_prediction_evidence = np.asarray([], dtype=float)
    holdout_baseline = np.asarray([], dtype=float)
    holdout_entities = np.asarray([], dtype=object)
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
            holdout_frames = build_panel_frames(prepared, holdout_origin, holdout_origin)
            holdout_model = _fit(holdout_frames)
            holdout_prediction, _ = _predict(holdout_model, holdout_frames)
            lookup = prepared.frame.set_index(["entity", "timestamp"])["observed_target"]
            actual = np.asarray(
                [
                    lookup.get((row.entity, row.target_date), np.nan)
                    for row in holdout_frames.predict.itertuples()
                ],
                dtype=float,
            )
            holdout_actual = actual
            holdout_prediction_evidence = holdout_prediction
            holdout_baseline = holdout_frames.predict["target_baseline"].to_numpy(dtype=float)
            holdout_entities = holdout_frames.predict["entity"].to_numpy(dtype=object)
            holdout = _metrics(
                actual, holdout_prediction, len(holdout_frames.predict), holdout_origin
            )
        except ValueError as exc:
            holdout = HoldoutMetrics(
                supported=False,
                origin=holdout_origin.date(),
                rows=0,
                coverage=0.0,
                unsupported_reason=str(exc),
            )
    holdout_seconds = time.monotonic() - holdout_started

    fit_started = time.monotonic()
    final_frames = build_panel_frames(prepared, prepared.training_end, prepared.training_end)
    if len(final_frames.train) < 10:
        raise ValueError("The mapped dataset produced fewer than 10 trainable direct-panel rows")
    estimator = _fit(final_frames)
    fit_seconds = time.monotonic() - fit_started

    forecast_started = time.monotonic()
    prediction, fallback = _predict(estimator, final_frames)
    evaluation = build_forecast_evaluation(
        holdout_origin=holdout.origin,
        actual=holdout_actual,
        prediction=holdout_prediction_evidence,
        baseline=holdout_baseline,
        entities=holdout_entities,
        train_features=final_frames.train,
        fresh_features=final_frames.predict,
        feature_columns=final_frames.feature_columns,
        categorical_columns=final_frames.categorical_columns,
    )
    forecast = final_frames.predict[["entity", "target_date", "horizon"]].copy()
    forecast = forecast.rename(columns={"target_date": "timestamp"})
    forecast["prediction"] = prediction
    forecast["fallback_used"] = fallback
    for column in (
        mapping.known_future_numeric
        + mapping.known_future_categorical
        + mapping.static_numeric
        + mapping.static_categorical
    ):
        if column in final_frames.predict:
            forecast[column] = final_frames.predict[column].to_numpy()

    output_directory.mkdir(parents=True, exist_ok=True)
    forecast_path = output_directory / "forecast.parquet"
    model_path = output_directory / "model.ubj"
    manifest_path = output_directory / "model-manifest.json"
    _write_forecast(forecast, forecast_path)
    estimator.save_model(model_path)
    forecast_seconds = time.monotonic() - forecast_started

    forecast_sha256 = sha256_file(forecast_path)
    model_sha256 = sha256_file(model_path)
    manifest = ModelArtifactManifest(
        vonavy_agent_source_revision=source_revision,
        owner_id=owner_id,
        dataset_id=dataset_id,
        run_id=run_id,
        input=input_identity,
        mapping=mapping,
        training_end=prepared.training_end.date(),
        parameters=XGBOOST_PARAMETERS,
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
        adapter=AdapterIdentity(),
        input=input_identity,
        profile=ForecastProfile(
            rows=len(raw),
            entities=len(prepared.entities),
            history_start=prepared.frame["timestamp"].min().date(),
            training_end=prepared.training_end.date(),
            forecast_start=prepared.forecast_dates[0].date(),
            forecast_end=prepared.forecast_dates[-1].date(),
            trainable_rows=len(final_frames.train),
            fallback_rows=int(fallback.sum()),
        ),
        holdout=holdout,
        evaluation=evaluation,
        artifacts=ForecastArtifacts(
            forecast=ArtifactReference(
                key="forecast.parquet",
                sha256=forecast_sha256,
                byte_size=forecast_path.stat().st_size,
            ),
            model=ArtifactReference(
                key="model.ubj",
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
            fit_seconds=fit_seconds,
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
