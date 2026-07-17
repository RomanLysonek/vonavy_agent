from __future__ import annotations

import json
import math
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from hashlib import sha256 as new_sha256
from importlib import metadata
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vonavy_agent.datasets import DatasetRegistry, observation_availability
from vonavy_agent.domain import (
    AvailabilityKind,
    DatasetMappingSpec,
    ExperimentSpec,
    FeatureMapping,
    FeatureRole,
    MovingAverageConfig,
    RidgeDirectConfig,
    SeasonalNaiveConfig,
)
from vonavy_agent.eligibility import expected_grid
from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_hash, canonical_json, file_hash
from vonavy_agent.managed_files import fsync_tree
from vonavy_agent.persistence import (
    DataProfile,
    DatasetMapping,
    ExperimentSpecRow,
    GateResultRow,
    Job,
    Run,
    RunMetric,
)
from vonavy_agent.settings import Settings


class RunCancelled(Exception):
    pass


@dataclass(frozen=True)
class PreparedData:
    frame: pd.DataFrame
    mapping: DatasetMappingSpec
    spec: ExperimentSpec


@dataclass(frozen=True)
class StagedRun:
    summary: dict[str, Any]
    staging_dir: Path
    final_dir: Path
    manifest_hash: str
    already_visible: bool = False


def _prepare_data(
    engine: Engine,
    registry: DatasetRegistry,
    spec: ExperimentSpec,
    mapping: DatasetMappingSpec,
) -> PreparedData:
    with Session(engine) as session:
        frame = registry.read_materialized_frame(session, spec.dataset_version_id).copy()
    frame["_date"] = (
        pd.to_datetime(frame[mapping.timestamp_column], errors="raise", utc=True)
        .dt.tz_convert(None)
        .dt.normalize()
    )
    frame["_entity"] = (
        frame[mapping.entity_column].astype("string") if mapping.entity_column else "__single__"
    )
    frame["_target"] = pd.to_numeric(frame[mapping.target_column], errors="raise").astype(float)
    frame["_target_available"] = _availability_series(
        frame, mapping.target_availability.kind, mapping.target_availability.column
    )
    frame["_observation_available"] = observation_availability(
        frame, mapping.observation_availability_column
    )
    frame = frame.sort_values(["_entity", "_date"], kind="stable").reset_index(drop=True)
    return PreparedData(frame=frame, mapping=mapping, spec=spec)


def _availability_series(
    frame: pd.DataFrame,
    kind: AvailabilityKind,
    column: str | None,
) -> pd.Series:
    if kind == AvailabilityKind.COLUMN:
        assert column is not None
        return pd.to_datetime(
            frame[column], errors="raise", utc=True, format="mixed"
        ).dt.tz_convert(None)
    if kind == AvailabilityKind.EVENT_TIME:
        if "_date" in frame:
            return pd.to_datetime(frame["_date"]) + pd.Timedelta(days=1)
        raise AgentError("availability_error", "Event-time availability requires parsed dates")
    return pd.Series(pd.Timestamp.min, index=frame.index)


def _lookup(frame: pd.DataFrame) -> dict[tuple[str, date], pd.Series]:
    return {(str(row["_entity"]), row["_date"].date()): row for _, row in frame.iterrows()}


def _eligible_target(
    row: pd.Series | None,
    cutoff: pd.Timestamp,
) -> float | None:
    if (
        row is None
        or pd.isna(row["_target"])
        or row["_target_available"] > cutoff
        or pd.isna(row["_observation_available"])
        or not bool(row["_observation_available"])
    ):
        return None
    return float(row["_target"])


def _feature_value(
    row: pd.Series | None,
    feature: FeatureMapping,
    cutoff: pd.Timestamp,
    frame: pd.DataFrame,
) -> object | None:
    if row is None or pd.isna(row[feature.name]):
        return None
    available = _availability_series(
        frame.loc[[row.name]], feature.availability.kind, feature.availability.column
    ).iloc[0]
    if available > cutoff:
        return None
    return cast(object, row[feature.name])


def _base_features(
    lookup: dict[tuple[str, date], pd.Series],
    frame: pd.DataFrame,
    entity: str,
    sample_origin: date,
    label_date: date,
    cutoff: pd.Timestamp,
    config: RidgeDirectConfig,
    features: tuple[FeatureMapping, ...],
) -> dict[str, object] | None:
    values: dict[str, object] = {"entity": entity}
    for lag in config.lag_days:
        row = lookup.get((entity, date.fromordinal(sample_origin.toordinal() - lag)))
        target = _eligible_target(row, cutoff)
        if target is None:
            return None
        values[f"target_lag_{lag}"] = target
    for window in config.rolling_days:
        observed: list[float] = []
        for lag in range(1, window + 1):
            row = lookup.get((entity, date.fromordinal(sample_origin.toordinal() - lag)))
            target = _eligible_target(row, cutoff)
            if target is None:
                return None
            observed.append(target)
        values[f"target_mean_{window}"] = float(np.mean(observed))
    for feature in features:
        if feature.role == FeatureRole.EXCLUDED:
            continue
        if feature.role == FeatureRole.PAST_ONLY:
            row = lookup.get((entity, date.fromordinal(sample_origin.toordinal() - 1)))
        elif feature.role == FeatureRole.KNOWN_FUTURE:
            row = lookup.get((entity, label_date))
        else:
            row = lookup.get((entity, sample_origin))
            if row is None:
                candidates = frame[frame["_entity"] == entity]
                row = candidates.iloc[0] if not candidates.empty else None
        value = _feature_value(row, feature, cutoff, frame)
        if value is None:
            return None
        values[feature.name] = value
    return values


def _ridge_predictions(
    data: PreparedData,
    origin: date,
    horizon: int,
    config: RidgeDirectConfig,
    seed: int,
) -> dict[str, float]:
    train, labels, predict, predict_entities = _ridge_design(data, origin, horizon, config)
    if train.empty:
        raise AgentError("ridge_no_training_rows", "Direct Ridge has no eligible training rows")
    if predict.empty:
        return {}
    numeric = [name for name in train.columns if pd.api.types.is_numeric_dtype(train[name])]
    categorical = [name for name in train.columns if name not in numeric]
    transformers: list[tuple[str, object, list[str]]] = []
    if numeric:
        transformers.append(("numeric", StandardScaler(), numeric))
    if categorical:
        transformers.append(
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                categorical,
            )
        )
    pipeline = Pipeline(
        [
            ("preprocess", ColumnTransformer(transformers=transformers)),
            ("model", Ridge(alpha=config.alpha, random_state=seed)),
        ]
    )
    pipeline.fit(train, labels)
    output = pipeline.predict(predict)
    return {entity: float(value) for entity, value in zip(predict_entities, output, strict=True)}


def _ridge_design(
    data: PreparedData,
    origin: date,
    horizon: int,
    config: RidgeDirectConfig,
) -> tuple[pd.DataFrame, np.ndarray, pd.DataFrame, list[str]]:
    frame, spec = data.frame, data.spec
    lookup = _lookup(frame)
    entities = sorted(str(value) for value in frame["_entity"].unique())
    actual_origin = pd.Timestamp(origin)
    max_lag = max((*config.lag_days, *config.rolling_days))
    fit_start = date.fromordinal(origin.toordinal() - spec.training_window_days + max_lag)
    latest_sample = date.fromordinal(origin.toordinal() - horizon)
    rows: list[dict[str, object]] = []
    labels: list[float] = []
    sample_origin = fit_start
    while sample_origin <= latest_sample:
        label_date = date.fromordinal(sample_origin.toordinal() + horizon - 1)
        for entity in entities:
            label_row = lookup.get((entity, label_date))
            label = _eligible_target(label_row, actual_origin)
            if label is None:
                continue
            values = _base_features(
                lookup,
                frame,
                entity,
                sample_origin,
                label_date,
                pd.Timestamp(sample_origin),
                config,
                spec.selected_features(),
            )
            if values is not None:
                rows.append(values)
                labels.append(label)
        sample_origin = date.fromordinal(sample_origin.toordinal() + 1)
    train = pd.DataFrame(rows)
    forecast_date = date.fromordinal(origin.toordinal() + horizon - 1)
    predict_rows: list[dict[str, object]] = []
    predict_entities: list[str] = []
    for entity in entities:
        values = _base_features(
            lookup,
            frame,
            entity,
            origin,
            forecast_date,
            actual_origin,
            config,
            spec.selected_features(),
        )
        if values is not None:
            predict_rows.append(values)
            predict_entities.append(entity)
    return train, np.asarray(labels), pd.DataFrame(predict_rows), predict_entities


def model_feasibility(
    data: PreparedData,
    origin: date,
    horizon: int,
    config: SeasonalNaiveConfig | MovingAverageConfig | RidgeDirectConfig,
) -> tuple[int, int]:
    if isinstance(config, RidgeDirectConfig):
        train, _, predict, _ = _ridge_design(data, origin, horizon, config)
        return len(train), len(predict)
    predictions = _baseline_predictions(data, origin, horizon, config)
    return len(predictions), len(predictions)


def _baseline_predictions(
    data: PreparedData,
    origin: date,
    horizon: int,
    config: SeasonalNaiveConfig | MovingAverageConfig,
) -> dict[str, float]:
    frame = data.frame
    lookup = _lookup(frame)
    entities = sorted(str(value) for value in frame["_entity"].unique())
    cutoff = pd.Timestamp(origin)
    forecast_date = date.fromordinal(origin.toordinal() + horizon - 1)
    output: dict[str, float] = {}
    for entity in entities:
        if isinstance(config, SeasonalNaiveConfig):
            reference = date.fromordinal(forecast_date.toordinal() - config.period_days)
            while reference >= origin:
                reference = date.fromordinal(reference.toordinal() - config.period_days)
            row = lookup.get((entity, reference))
            value = _eligible_target(row, cutoff)
        else:
            values = [
                _eligible_target(
                    lookup.get((entity, date.fromordinal(origin.toordinal() - lag))), cutoff
                )
                for lag in range(1, config.window_days + 1)
            ]
            observed = [float(item) for item in values if item is not None]
            value = float(np.mean(observed)) if len(observed) == config.window_days else None
        if value is not None:
            output[entity] = value
    return output


def _metric_records(
    predictions: pd.DataFrame,
    expected_rows: dict[str, int],
    expected_origins: dict[tuple[str, str], int],
    expected_horizons: dict[tuple[str, int], int],
    configured_models: tuple[str, ...],
    seeds: tuple[int, ...],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    key_columns = ["role", "seed", "origin", "horizon", "entity", "date"]

    def add_metrics(
        group: pd.DataFrame,
        role: str,
        model: str,
        seed: int,
        expected: int,
        **dims: Any,
    ) -> None:
        coverage = len(group) / expected if expected else 0.0
        if group.empty:
            values: dict[str, tuple[float | None, str | None]] = {
                metric: (None, "no_common_prediction_rows")
                for metric in ("wape", "mae", "rmse", "bias")
            }
        else:
            error = group["prediction"] - group["actual"]
            denominator = float(group["actual"].abs().sum())
            values = {
                "wape": (
                    float(error.abs().sum() / denominator) if denominator else None,
                    None if denominator else "zero_actual_denominator",
                ),
                "mae": (float(error.abs().mean()), None),
                "rmse": (float(math.sqrt(float((error**2).mean()))), None),
                "bias": (float(error.mean()), None),
            }
        for metric, (value, unsupported) in values.items():
            records.append(
                {
                    "role": role,
                    "model": model,
                    "seed": seed,
                    "origin": dims.get("origin"),
                    "horizon": dims.get("horizon"),
                    "metric": metric,
                    "value": value,
                    "row_count": len(group),
                    "coverage": coverage,
                    "unsupported_reason": unsupported,
                }
            )
        records.append(
            {
                "role": role,
                "model": model,
                "seed": seed,
                "origin": dims.get("origin"),
                "horizon": dims.get("horizon"),
                "metric": "coverage",
                "value": coverage,
                "row_count": len(group),
                "coverage": coverage,
                "unsupported_reason": None,
            }
        )

    for role in sorted(expected_rows):
        for seed in seeds:
            scoped = predictions[(predictions["role"] == role) & (predictions["seed"] == seed)]
            common_keys: pd.DataFrame | None = None
            for model in configured_models:
                keys = scoped[scoped["model"] == model][key_columns].drop_duplicates()
                common_keys = (
                    keys if common_keys is None else common_keys.merge(keys, on=key_columns)
                )
            assert common_keys is not None
            common = scoped.merge(common_keys, on=key_columns)
            for model in configured_models:
                model_rows = common[common["model"] == model]
                add_metrics(model_rows, role, model, seed, expected_rows[role])
                for (origin_role, origin), expected in sorted(expected_origins.items()):
                    if origin_role == role:
                        add_metrics(
                            model_rows[model_rows["origin"] == origin],
                            role,
                            model,
                            seed,
                            expected,
                            origin=origin,
                        )
                for (horizon_role, horizon), expected in sorted(expected_horizons.items()):
                    if horizon_role == role:
                        add_metrics(
                            model_rows[model_rows["horizon"] == horizon],
                            role,
                            model,
                            seed,
                            expected,
                            horizon=horizon,
                        )
    return records


def run_backtest(
    engine: Engine,
    settings: Settings,
    job_id: str,
    run_id: str,
    stage_key: str,
    ownership_check: Callable[[], None] | None = None,
    before_publish: Callable[[], None] | None = None,
) -> StagedRun:
    registry = DatasetRegistry(settings, engine)
    with Session(engine) as session:
        run = session.get_one(Run, run_id)
        spec_row = session.get_one(ExperimentSpecRow, run.spec_id)
        gate_row = session.get_one(GateResultRow, run.gate_result_id)
        profile_row = session.get_one(DataProfile, spec_row.profile_id)
        mapping_row = session.get_one(DatasetMapping, spec_row.mapping_id)
        spec = ExperimentSpec.model_validate_json(spec_row.canonical_json)
        mapping = DatasetMappingSpec.model_validate_json(mapping_row.canonical_json)
    final_dir = settings.managed_root / "runs" / run_id
    if (final_dir / "manifest.json").is_file():
        result = json.loads((final_dir / "summary.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise AgentError("invalid_summary", "Published run summary must be a JSON object")
        if before_publish is not None:
            before_publish()
        return StagedRun(
            summary=cast(dict[str, Any], result),
            staging_dir=final_dir,
            final_dir=final_dir,
            manifest_hash=file_hash(final_dir / "manifest.json"),
            already_visible=True,
        )
    temp_dir = settings.managed_root / "jobs" / "tmp" / job_id / stage_key / "run"
    shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True)
    started = time.monotonic()
    prepared = _prepare_data(engine, registry, spec, mapping)
    lookup = _lookup(prepared.frame)
    predictions: list[dict[str, Any]] = []
    expected_rows = {"calibration": 0, "test": 0}
    expected_origins: dict[tuple[str, str], int] = {}
    expected_horizons: dict[tuple[str, int], int] = {}
    grid = expected_grid(
        prepared.frame["_entity"],
        prepared.frame["_date"],
        prepared.frame["_observation_available"],
        spec.origins,
        spec.horizon_days,
        spec.scoring_availability_policy,
    )
    for cell in grid:
        if not cell.included:
            continue
        expected_rows[cell.role] += 1
        origin_key = (cell.role, cell.origin.isoformat())
        expected_origins[origin_key] = expected_origins.get(origin_key, 0) + 1
        horizon_key = (cell.role, cell.horizon)
        expected_horizons[horizon_key] = expected_horizons.get(horizon_key, 0) + 1
    for origin_spec in spec.origins:
        if ownership_check is not None:
            ownership_check()
        else:
            with Session(engine) as session:
                if session.get_one(Job, job_id).cancel_requested:
                    raise RunCancelled
        for seed in spec.seeds:
            for model in spec.models:
                for horizon in range(1, spec.horizon_days + 1):
                    if isinstance(model, RidgeDirectConfig):
                        model_predictions = _ridge_predictions(
                            prepared, origin_spec.date, horizon, model, seed
                        )
                    else:
                        model_predictions = _baseline_predictions(
                            prepared, origin_spec.date, horizon, model
                        )
                    forecast_date = date.fromordinal(origin_spec.date.toordinal() + horizon - 1)
                    for entity, prediction in model_predictions.items():
                        actual_row = lookup.get((entity, forecast_date))
                        if actual_row is None:
                            continue
                        actual = _eligible_target(
                            actual_row,
                            pd.Timestamp(spec.evaluation_as_of).tz_localize(None),
                        )
                        if actual is None:
                            continue
                        predictions.append(
                            {
                                "role": origin_spec.role,
                                "seed": seed,
                                "origin": origin_spec.date.isoformat(),
                                "horizon": horizon,
                                "entity": entity,
                                "date": forecast_date.isoformat(),
                                "model": model.kind,
                                "prediction": prediction,
                                "actual": actual,
                            }
                        )
    prediction_frame = pd.DataFrame(
        predictions,
        columns=[
            "role",
            "seed",
            "origin",
            "horizon",
            "entity",
            "date",
            "model",
            "prediction",
            "actual",
        ],
    )
    configured_models = tuple(sorted(model.kind for model in spec.models))
    internal_metric_records = _metric_records(
        prediction_frame,
        expected_rows,
        expected_origins,
        expected_horizons,
        configured_models,
        spec.seeds,
    )
    runtime = time.monotonic() - started
    for model_name in sorted({configured.kind for configured in spec.models}):
        internal_metric_records.append(
            {
                "role": "all",
                "model": model_name,
                "seed": None,
                "origin": None,
                "horizon": None,
                "metric": "runtime",
                "value": runtime,
                "row_count": 0,
                "coverage": 0.0,
                "unsupported_reason": None,
            }
        )
    public_metric_records = [
        record for record in internal_metric_records if record["metric"] in spec.metrics
    ]
    diagnostic_coverage = [
        {
            "role": record["role"],
            "model": record["model"],
            "seed": record["seed"],
            "rows": record["row_count"],
            "coverage": record["coverage"],
        }
        for record in internal_metric_records
        if record["metric"] == "coverage" and record["origin"] is None and record["horizon"] is None
    ]
    summary = {
        "schema_version": "1.0",
        "run_id": run_id,
        "expected_rows": expected_rows,
        "prediction_rows": len(prediction_frame),
        "configured_models": list(configured_models),
        "metrics": public_metric_records,
        "diagnostics": {"common_row_coverage": diagnostic_coverage},
        "runtime_seconds": runtime,
    }
    (temp_dir / "spec.json").write_text(spec_row.canonical_json, encoding="utf-8")
    (temp_dir / "gate.json").write_text(gate_row.canonical_json, encoding="utf-8")
    (temp_dir / "profile.json").write_text(profile_row.canonical_json, encoding="utf-8")
    prediction_frame.to_parquet(temp_dir / "predictions.parquet", index=False)
    (temp_dir / "metrics.json").write_text(canonical_json(public_metric_records), encoding="utf-8")
    (temp_dir / "summary.json").write_text(canonical_json(summary), encoding="utf-8")
    environment = _environment(settings)
    (temp_dir / "environment.json").write_text(canonical_json(environment), encoding="utf-8")
    (temp_dir / "stdout.log").write_text("", encoding="utf-8")
    (temp_dir / "stderr.log").write_text("", encoding="utf-8")
    outputs = {
        path.name: {"sha256": file_hash(path), "bytes": path.stat().st_size}
        for path in sorted(temp_dir.iterdir())
        if path.is_file()
    }
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "source": _source_revision(),
        "dataset_hash": _dataset_hash(engine, spec.dataset_version_id),
        "mapping_hash": mapping_row.mapping_hash,
        "profile_hash": profile_row.profile_hash,
        "spec_hash": spec_row.spec_hash,
        "dependency_hash": file_hash(Path("uv.lock")) if Path("uv.lock").is_file() else None,
        "environment": environment,
        "seeds": list(spec.seeds),
        "command": [sys.executable, "-m", "vonavy_agent.executor", "--job-id", job_id],
        "adapter": {"kind": "builtin", "version": "1.0"},
        "resource_limits": spec.resources.model_dump(mode="json"),
        "runtime_seconds": runtime,
        "outputs": outputs,
        "warnings": json.loads(gate_row.canonical_json)["warnings"],
        "errors": [],
    }
    (temp_dir / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    fsync_tree(temp_dir)
    if before_publish is not None:
        before_publish()
    return StagedRun(
        summary=summary,
        staging_dir=temp_dir,
        final_dir=final_dir,
        manifest_hash=file_hash(temp_dir / "manifest.json"),
    )


def _dataset_hash(engine: Engine, version_id: str) -> str:
    from vonavy_agent.persistence import DatasetVersion

    with Session(engine) as session:
        return session.get_one(DatasetVersion, version_id).materialized_blob_sha256


def _environment(settings: Settings) -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in ("vonavy-agent", "pandas", "numpy", "pyarrow", "scikit-learn"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "packages": packages,
        "memory_enforcement": "preflight_estimate_only",
        "hard_memory_limit_supported": False,
        "managed_root": str(settings.managed_root.resolve()),
    }


def _git(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip()


def _source_revision() -> dict[str, Any]:
    status = _git(["status", "--porcelain"])
    tree = _git(["rev-parse", "HEAD^{tree}"])
    diff = _git(["diff", "--binary", "HEAD"])
    tracked = _git_paths(["ls-files", "-z"])
    untracked = _git_paths(["ls-files", "--others", "--exclude-standard", "-z"])
    source_paths = sorted(set(tracked + untracked))
    return {
        "commit": _git(["rev-parse", "HEAD"]),
        "branch": _git(["branch", "--show-current"]),
        "tree": tree,
        "dirty": bool(status) if status is not None else None,
        "tracked_dirty": (
            any(not line.startswith("??") for line in status.splitlines())
            if status is not None
            else None
        ),
        "tracked_file_count": len(tracked),
        "untracked_file_count": len(untracked),
        "source_file_count": len(source_paths),
        "source_tree_hash": _source_tree_hash(source_paths),
        "diff_hash": canonical_hash(diff) if diff is not None else None,
    }


def _git_paths(args: list[str]) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            timeout=5,
            shell=False,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return [
        value.decode("utf-8", errors="surrogateescape")
        for value in completed.stdout.split(b"\0")
        if value
    ]


def _source_tree_hash(paths: list[str]) -> str | None:
    if not paths:
        return None
    digest = new_sha256()
    root = Path.cwd().resolve()
    for relative in paths:
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"file\0")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def persist_run(
    session: Session,
    run_id: str,
    manifest_hash: str,
    summary: dict[str, Any],
) -> None:
    run = session.get_one(Run, run_id)
    run.artifact_relative_path = str(Path("runs") / run_id)
    run.manifest_hash = manifest_hash
    run.summary_json = canonical_json(summary)
    session.execute(delete(RunMetric).where(RunMetric.run_id == run_id))
    for record in summary["metrics"]:
        session.add(RunMetric(run_id=run_id, **record))
