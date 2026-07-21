from __future__ import annotations

import importlib.metadata
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import torch
from torch import nn

from vonavy_agent.forecasting.contracts import (
    NEURALNET_ADAPTER_ID,
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
from vonavy_agent.forecasting.model import (
    ForecastRunOutput,
    _metrics,
    _write_forecast,
    sha256_file,
)
from vonavy_agent.forecasting.panel import (
    PanelFrames,
    PreparedPanel,
    build_panel_frames,
    prepare_daily_panel,
)

NEURALNET_PARAMETERS: dict[str, int | float | str | bool] = {
    "architecture": "embedding-mlp-residual",
    "hidden_dims": "256,128,64",
    "dropout": "0.20,0.15,0.10",
    "entity_embedding_dim": 12,
    "categorical_embedding_dim": 4,
    "horizon_embedding_dim": 4,
    "optimizer": "AdamW",
    "learning_rate": 0.001,
    "weight_decay": 0.0001,
    "scheduler": "cosine",
    "loss": "mse",
    "target": "log1p_residual",
    "epochs": 30,
    "batch_size": 512,
    "final_seeds": "42,123,777",
    "holdout_seeds": "42",
    "device": "cpu",
    "threads": 1,
    "numeric_preprocessing": "median_impute+missing_indicator+standardize",
}
MAX_NEURAL_TRAIN_ROWS = 300_000
FINAL_SEEDS = (42, 123, 777)
HOLDOUT_SEEDS = (42,)


@dataclass(slots=True)
class NumericState:
    columns: tuple[str, ...]
    median: np.ndarray
    mean: np.ndarray
    scale: np.ndarray


@dataclass(slots=True)
class TensorFrame:
    numeric: torch.Tensor
    categoricals: tuple[torch.Tensor, ...]
    horizon: torch.Tensor


class DirectQuantityNet(nn.Module):
    def __init__(
        self,
        *,
        numeric_width: int,
        category_sizes: tuple[int, ...],
        category_dims: tuple[int, ...],
    ) -> None:
        super().__init__()
        self.category_embeddings = nn.ModuleList(
            nn.Embedding(size, dim) for size, dim in zip(category_sizes, category_dims, strict=True)
        )
        self.horizon_embedding = nn.Embedding(7, 4)
        input_width = numeric_width + sum(category_dims) + 4
        layers: list[nn.Module] = []
        previous = input_width
        for hidden, dropout in zip((256, 128, 64), (0.20, 0.15, 0.10), strict=True):
            layers.extend(
                [
                    nn.Linear(previous, hidden),
                    nn.BatchNorm1d(hidden),
                    nn.GELU(),
                    nn.Dropout(dropout),
                ]
            )
            previous = hidden
        self.trunk = nn.Sequential(*layers)
        self.output = nn.Linear(previous, 1)

    def forward(
        self,
        numeric: torch.Tensor,
        categoricals: tuple[torch.Tensor, ...],
        horizon: torch.Tensor,
    ) -> torch.Tensor:
        embedded = [
            embedding(values)
            for embedding, values in zip(self.category_embeddings, categoricals, strict=True)
        ]
        embedded.append(self.horizon_embedding(horizon))
        representation = torch.cat([numeric, *embedded], dim=1)
        return cast(torch.Tensor, self.output(self.trunk(representation)).squeeze(-1))


def _numeric_columns(frames: PanelFrames) -> tuple[str, ...]:
    categorical = set(frames.categorical_columns)
    return tuple(
        column
        for column in frames.feature_columns
        if column not in categorical and column != "horizon"
    )


def _fit_numeric(frame: pd.DataFrame, columns: tuple[str, ...]) -> tuple[NumericState, np.ndarray]:
    values = frame.loc[:, columns].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float32)
    values[~np.isfinite(values)] = np.nan
    missing = ~np.isfinite(values)
    with np.errstate(all="ignore"):
        median = np.nanmedian(values, axis=0)
    median = np.where(np.isfinite(median), median, 0.0).astype(np.float32)
    imputed = np.where(missing, median, values).astype(np.float32)
    mean = imputed.mean(axis=0, dtype=np.float64).astype(np.float32)
    scale = imputed.std(axis=0, dtype=np.float64).astype(np.float32)
    scale = np.where(np.isfinite(scale) & (scale > 1e-6), scale, 1.0).astype(np.float32)
    transformed = np.concatenate(((imputed - mean) / scale, missing.astype(np.float32)), axis=1)
    if not np.isfinite(transformed).all():
        raise ValueError("Numeric preprocessing produced non-finite NeuralNet inputs")
    return NumericState(columns, median, mean, scale), transformed.astype(np.float32)


def _transform_numeric(frame: pd.DataFrame, state: NumericState) -> np.ndarray:
    values = (
        frame.loc[:, state.columns].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float32)
    )
    values[~np.isfinite(values)] = np.nan
    missing = ~np.isfinite(values)
    imputed = np.where(missing, state.median, values).astype(np.float32)
    transformed = np.concatenate(
        ((imputed - state.mean) / state.scale, missing.astype(np.float32)),
        axis=1,
    )
    if not np.isfinite(transformed).all():
        raise ValueError("Numeric preprocessing produced non-finite NeuralNet inputs")
    return transformed.astype(np.float32)


def _category_maps(frames: PanelFrames) -> tuple[dict[str, dict[str, int]], ...]:
    maps: list[dict[str, dict[str, int]]] = []
    for column in frames.categorical_columns:
        levels = tuple(frames.categorical_levels[column])
        level_map = {value: index for index, value in enumerate(levels)}
        if "__missing__" not in level_map:
            level_map["__missing__"] = len(level_map)
        maps.append({column: level_map})
    return tuple(maps)


def _encode_categories(
    frame: pd.DataFrame,
    category_maps: tuple[dict[str, dict[str, int]], ...],
) -> tuple[torch.Tensor, ...]:
    encoded: list[torch.Tensor] = []
    for item in category_maps:
        column, level_map = next(iter(item.items()))
        missing_index = level_map["__missing__"]
        values = frame[column].astype("string").fillna("__missing__")
        codes = values.map(level_map).fillna(missing_index).to_numpy(dtype=np.int64)
        encoded.append(torch.as_tensor(codes, dtype=torch.long))
    return tuple(encoded)


def _tensor_frame(
    frame: pd.DataFrame,
    numeric: np.ndarray,
    category_maps: tuple[dict[str, dict[str, int]], ...],
) -> TensorFrame:
    horizon = pd.to_numeric(frame["horizon"], errors="raise").to_numpy(dtype=np.int64) - 1
    if (horizon < 0).any() or (horizon > 6).any():
        raise ValueError("NeuralNet horizon values must be between one and seven")
    return TensorFrame(
        numeric=torch.as_tensor(numeric, dtype=torch.float32),
        categoricals=_encode_categories(frame, category_maps),
        horizon=torch.as_tensor(horizon, dtype=torch.long),
    )


def _category_dimensions(
    category_maps: tuple[dict[str, dict[str, int]], ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    sizes: list[int] = []
    dims: list[int] = []
    for item in category_maps:
        column, mapping = next(iter(item.items()))
        sizes.append(len(mapping))
        dims.append(12 if column == "entity" else 4)
    return tuple(sizes), tuple(dims)


def _batch_ranges(n_rows: int, batch_size: int) -> tuple[tuple[int, int], ...]:
    if n_rows < 2:
        raise ValueError("NeuralNet training requires at least two rows")
    starts = list(range(0, n_rows, batch_size))
    if len(starts) > 1 and n_rows - starts[-1] == 1:
        starts[-1] -= 1
    return tuple(
        (start, starts[index + 1] if index + 1 < len(starts) else n_rows)
        for index, start in enumerate(starts)
    )


def _train_seed(
    tensors: TensorFrame,
    target: np.ndarray,
    *,
    category_sizes: tuple[int, ...],
    category_dims: tuple[int, ...],
    seed: int,
) -> DirectQuantityNet:
    torch.manual_seed(seed)
    np.random.seed(seed)
    torch.set_num_threads(1)
    model = DirectQuantityNet(
        numeric_width=int(tensors.numeric.shape[1]),
        category_sizes=category_sizes,
        category_dims=category_dims,
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=0.0001)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer,
        T_max=30,
        eta_min=0.00001,
    )
    y = torch.as_tensor(target, dtype=torch.float32)
    n_rows = len(y)
    ranges = _batch_ranges(n_rows, 512)
    model.train()
    for _ in range(30):
        permutation = torch.randperm(n_rows)
        for start, end in ranges:
            index = permutation[start:end]
            prediction = model(
                tensors.numeric[index],
                tuple(values[index] for values in tensors.categoricals),
                tensors.horizon[index],
            )
            loss = torch.mean((prediction - y[index]) ** 2)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()  # type: ignore[no-untyped-call]
            optimizer.step()
        scheduler.step()
    model.eval()
    return model


def _fit_models(
    frames: PanelFrames,
    seeds: tuple[int, ...],
) -> tuple[
    list[DirectQuantityNet],
    NumericState,
    tuple[dict[str, dict[str, int]], ...],
    TensorFrame,
]:
    if len(frames.train) > MAX_NEURAL_TRAIN_ROWS:
        raise ValueError(f"NeuralNet v1 supports at most {MAX_NEURAL_TRAIN_ROWS} direct-panel rows")
    numeric_state, numeric = _fit_numeric(frames.train, _numeric_columns(frames))
    category_maps = _category_maps(frames)
    train_tensors = _tensor_frame(frames.train, numeric, category_maps)
    target = frames.train["target"].to_numpy(dtype=np.float32)
    baseline = frames.train["target_baseline"].to_numpy(dtype=np.float32)
    residual = np.log1p(target) - np.log1p(np.clip(baseline, 0.0, None))
    if not np.isfinite(residual).all():
        raise ValueError("NeuralNet target preprocessing produced non-finite residuals")
    category_sizes, category_dims = _category_dimensions(category_maps)
    models = [
        _train_seed(
            train_tensors,
            residual,
            category_sizes=category_sizes,
            category_dims=category_dims,
            seed=seed,
        )
        for seed in seeds
    ]
    predict_numeric = _transform_numeric(frames.predict, numeric_state)
    predict_tensors = _tensor_frame(frames.predict, predict_numeric, category_maps)
    return models, numeric_state, category_maps, predict_tensors


def _predict_models(
    models: list[DirectQuantityNet],
    tensors: TensorFrame,
    baseline: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    raw_predictions: list[np.ndarray] = []
    with torch.inference_mode():
        for model in models:
            raw_predictions.append(
                model(tensors.numeric, tensors.categoricals, tensors.horizon)
                .detach()
                .cpu()
                .numpy()
                .astype(float)
            )
    raw = np.nanmean(np.vstack(raw_predictions), axis=0)
    prediction = np.expm1(raw + np.log1p(np.clip(baseline, 0.0, None)))
    fallback = ~np.isfinite(prediction)
    prediction = np.where(fallback, baseline, prediction)
    prediction = np.nan_to_num(prediction, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(prediction, 0.0, None), fallback


def _artifact_payload(
    models: list[DirectQuantityNet],
    numeric_state: NumericState,
    category_maps: tuple[dict[str, dict[str, int]], ...],
    frames: PanelFrames,
) -> dict[str, Any]:
    category_sizes, category_dims = _category_dimensions(category_maps)
    return {
        "schema_version": "neuralnet-direct-artifact/v1",
        "adapter_id": NEURALNET_ADAPTER_ID,
        "parameters": NEURALNET_PARAMETERS,
        "feature_order": frames.feature_columns,
        "categorical_columns": frames.categorical_columns,
        "category_maps": category_maps,
        "numeric_columns": numeric_state.columns,
        "numeric_median": numeric_state.median,
        "numeric_mean": numeric_state.mean,
        "numeric_scale": numeric_state.scale,
        "numeric_width": len(numeric_state.columns) * 2,
        "category_sizes": category_sizes,
        "category_dims": category_dims,
        "model_state_dicts": [model.state_dict() for model in models],
    }


def _package_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ("numpy", "pandas", "torch", "pyarrow", "pydantic"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "unavailable"
    return result


def run_neuralnet_forecast(
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
            models, _, _, predict_tensors = _fit_models(holdout_frames, HOLDOUT_SEEDS)
            holdout_prediction, _ = _predict_models(
                models,
                predict_tensors,
                holdout_frames.predict["target_baseline"].to_numpy(dtype=float),
            )
            lookup = prepared.frame.set_index(["entity", "timestamp"])["observed_target"]
            actual = np.asarray(
                [
                    lookup.get((row.entity, row.target_date), np.nan)
                    for row in holdout_frames.predict.itertuples()
                ],
                dtype=float,
            )
            holdout = _metrics(
                actual,
                holdout_prediction,
                len(holdout_frames.predict),
                holdout_origin,
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
    models, numeric_state, category_maps, predict_tensors = _fit_models(
        final_frames,
        FINAL_SEEDS,
    )
    fit_seconds = time.monotonic() - fit_started

    forecast_started = time.monotonic()
    prediction, fallback = _predict_models(
        models,
        predict_tensors,
        final_frames.predict["target_baseline"].to_numpy(dtype=float),
    )
    forecast = final_frames.predict[["entity", "target_date", "horizon"]].copy()
    forecast = forecast.rename(columns={"target_date": "timestamp"})
    forecast["prediction"] = prediction
    forecast["fallback_used"] = fallback
    context_columns = (
        mapping.known_future_numeric
        + mapping.known_future_categorical
        + mapping.static_numeric
        + mapping.static_categorical
    )
    for column in context_columns:
        if column in final_frames.predict:
            forecast[column] = final_frames.predict[column].to_numpy()

    output_directory.mkdir(parents=True, exist_ok=True)
    forecast_path = output_directory / "forecast.parquet"
    model_path = output_directory / "model.pt"
    manifest_path = output_directory / "model-manifest.json"
    _write_forecast(forecast, forecast_path)
    torch.save(
        _artifact_payload(models, numeric_state, category_maps, final_frames),
        model_path,
    )
    forecast_seconds = time.monotonic() - forecast_started

    forecast_sha256 = sha256_file(forecast_path)
    model_sha256 = sha256_file(model_path)
    adapter = AdapterIdentity(id=NEURALNET_ADAPTER_ID)
    manifest = ModelArtifactManifest(
        adapter=adapter,
        vonavy_agent_source_revision=source_revision,
        owner_id=owner_id,
        dataset_id=dataset_id,
        run_id=run_id,
        input=input_identity,
        mapping=mapping,
        training_end=prepared.training_end.date(),
        parameters=NEURALNET_PARAMETERS,
        feature_order=final_frames.feature_columns,
        categorical_levels=final_frames.categorical_levels,
        holdout=holdout,
        package_versions=_package_versions(),
        model_sha256=model_sha256,
        forecast_sha256=forecast_sha256,
    )
    manifest_path.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    manifest_sha256 = sha256_file(manifest_path)
    finished_at = datetime.now(UTC)
    result = ForecastResult(
        status=ForecastStatus.SUCCEEDED,
        owner_id=owner_id,
        dataset_id=dataset_id,
        run_id=run_id,
        adapter=adapter,
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
        artifacts=ForecastArtifacts(
            forecast=ArtifactReference(
                key="forecast.parquet",
                sha256=forecast_sha256,
                byte_size=forecast_path.stat().st_size,
            ),
            model=ArtifactReference(
                key="model.pt",
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
