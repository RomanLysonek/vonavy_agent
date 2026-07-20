from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import numpy as np
import pandas as pd

from vonavy_agent.forecasting.contracts import ForecastIssue, ForecastMapping

BASELINE_LAGS = (7, 14, 21, 28)
BASELINE_WEIGHTS = np.asarray((4.0, 3.0, 2.0, 1.0), dtype=float)
POINT_LAGS = (0, 1, 7, 14, 28)
ROLLING_WINDOWS = (7, 14, 28)


@dataclass(slots=True)
class PreparedPanel:
    frame: pd.DataFrame
    mapping: ForecastMapping
    training_end: pd.Timestamp
    forecast_dates: pd.DatetimeIndex
    entities: tuple[str, ...]
    global_median: float
    warnings: tuple[ForecastIssue, ...]


@dataclass(slots=True)
class PanelFrames:
    train: pd.DataFrame
    predict: pd.DataFrame
    feature_columns: tuple[str, ...]
    categorical_columns: tuple[str, ...]
    categorical_levels: dict[str, tuple[str, ...]]


def _availability(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False).astype(bool)
    normalised = series.astype("string").str.casefold()
    mapped = normalised.map({"1": True, "0": False, "true": True, "false": False})
    if mapped.isna().any():
        raise ValueError("availability column must contain only boolean-like values")
    return mapped.astype(bool)


def _ensure_static(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    for column in columns:
        counts = frame.groupby("entity", sort=False)[column].nunique(dropna=True)
        bad = counts[counts > 1]
        if not bad.empty:
            sample = ", ".join(str(value) for value in bad.index[:5])
            raise ValueError(f"static column {column!r} varies within entities: {sample}")


def prepare_daily_panel(
    raw: pd.DataFrame,
    mapping: ForecastMapping,
    training_end: date,
    *,
    max_rows: int,
    max_entities: int,
    max_history_days: int,
) -> PreparedPanel:
    if len(raw) > max_rows:
        raise ValueError(f"dataset has {len(raw)} rows; policy allows {max_rows}")
    missing = sorted(set(mapping.required_columns) - set(raw.columns.astype(str)))
    if missing:
        raise ValueError("mapped columns are missing: " + ", ".join(missing))

    frame = pd.DataFrame(index=raw.index)
    frame["timestamp"] = pd.to_datetime(
        raw[mapping.timestamp_column], errors="coerce"
    ).dt.normalize()
    if frame["timestamp"].isna().any():
        raise ValueError("timestamp column contains unparseable values")
    frame["entity"] = (
        raw[mapping.entity_column].astype("string").fillna("__missing_entity__")
        if mapping.entity_column
        else "__single_series__"
    )
    if frame["entity"].nunique() > max_entities:
        raise ValueError(f"dataset has too many entities; policy allows {max_entities}")
    frame["target"] = pd.to_numeric(raw[mapping.target_column], errors="coerce")
    if (frame["target"].dropna() < 0).any():
        raise ValueError("target values must be nonnegative")
    frame["available"] = (
        _availability(raw[mapping.availability_column]) if mapping.availability_column else True
    )
    frame["calendar_row"] = True
    frame["source_row"] = np.arange(len(raw), dtype=np.int64)

    passthrough = (
        mapping.known_future_numeric
        + mapping.known_future_categorical
        + mapping.static_numeric
        + mapping.static_categorical
    )
    for column in passthrough:
        frame[column] = raw[column]
    for column in mapping.known_future_numeric + mapping.static_numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    duplicates = frame.duplicated(["entity", "timestamp"], keep=False)
    if duplicates.any():
        example = frame.loc[duplicates, ["entity", "timestamp"]].head(5).to_dict("records")
        raise ValueError(f"duplicate entity/timestamp keys are not allowed: {example}")

    training_end_ts = pd.Timestamp(training_end).normalize()
    forecast_dates = pd.date_range(training_end_ts + pd.Timedelta(days=1), periods=7, freq="D")
    required_end = forecast_dates[-1]
    if (frame.loc[frame["timestamp"] > training_end_ts, "target"].notna()).any():
        raise ValueError("target must be null after training_end")
    if not ((frame["timestamp"] <= training_end_ts) & frame["target"].notna()).any():
        raise ValueError("no observed target exists on or before training_end")

    entities = tuple(sorted(frame["entity"].astype(str).unique()))
    reindexed: list[pd.DataFrame] = []
    warnings: list[ForecastIssue] = []
    static_columns = mapping.static_numeric + mapping.static_categorical
    _ensure_static(frame, static_columns)

    for entity in entities:
        entity_frame = frame.loc[frame["entity"] == entity].copy().sort_values("timestamp")
        historical = entity_frame.loc[entity_frame["timestamp"] <= training_end_ts]
        if historical.empty:
            raise ValueError(f"entity {entity!r} has no history on or before training_end")
        first = historical["timestamp"].min()
        if (training_end_ts - first).days + 1 > max_history_days:
            first = training_end_ts - pd.Timedelta(days=max_history_days - 1)
            entity_frame = entity_frame.loc[entity_frame["timestamp"] >= first]
            warnings.append(
                ForecastIssue(
                    code="history_truncated",
                    message="History was truncated by server policy",
                    entity=entity,
                )
            )
        index = pd.date_range(first, required_end, freq="D")
        entity_frame = entity_frame.set_index("timestamp").reindex(index)
        entity_frame.index.name = "timestamp"
        entity_frame["entity"] = entity
        entity_frame["calendar_row"] = (
            entity_frame["calendar_row"].astype("boolean").fillna(False).astype(bool)
        )
        entity_frame["available"] = (
            entity_frame["available"].astype("boolean").fillna(False).astype(bool)
        )
        for column in static_columns:
            non_null = historical[column].dropna()
            entity_frame[column] = non_null.iloc[-1] if not non_null.empty else np.nan
        reindexed.append(entity_frame.reset_index())

    full = pd.concat(reindexed, ignore_index=True)
    historical_mask = full["timestamp"] <= training_end_ts
    full["observed_target"] = np.where(
        historical_mask & full["available"] & full["target"].notna(),
        full["target"].astype(float),
        np.nan,
    )

    future_mask = full["timestamp"].isin(forecast_dates)
    missing_future = ~full.loc[future_mask, "calendar_row"]
    if missing_future.any() and (mapping.known_future_numeric or mapping.known_future_categorical):
        raise ValueError(
            "all seven future rows are required for every entity when known-future features are selected"
        )
    for column in mapping.known_future_numeric + mapping.known_future_categorical:
        if full.loc[future_mask, column].isna().any():
            raise ValueError(f"known-future column {column!r} is missing on forecast dates")

    return PreparedPanel(
        frame=full.sort_values(["entity", "timestamp"]).reset_index(drop=True),
        mapping=mapping,
        training_end=training_end_ts,
        forecast_dates=forecast_dates,
        entities=entities,
        global_median=(
            float(full.loc[historical_mask, "observed_target"].median())
            if full.loc[historical_mask, "observed_target"].notna().any()
            else 0.0
        ),
        warnings=tuple(warnings),
    )


def _weighted_baseline(values: np.ndarray) -> np.ndarray:
    valid = np.isfinite(values)
    weighted = np.where(valid, values * BASELINE_WEIGHTS, 0.0).sum(axis=1)
    denominator = np.where(valid, BASELINE_WEIGHTS, 0.0).sum(axis=1)
    return cast(
        "np.ndarray[Any, Any]",
        np.divide(weighted, denominator, out=np.full(len(values), np.nan), where=denominator > 0),
    )


def _calendar_features(target: pd.Series) -> pd.DataFrame:
    day_of_week = target.dt.dayofweek.to_numpy(dtype=float)
    month = target.dt.month.to_numpy(dtype=float)
    day_of_year = target.dt.dayofyear.to_numpy(dtype=float)
    return pd.DataFrame(
        {
            "day_of_week_sin": np.sin(2 * np.pi * day_of_week / 7),
            "day_of_week_cos": np.cos(2 * np.pi * day_of_week / 7),
            "month_sin": np.sin(2 * np.pi * (month - 1) / 12),
            "month_cos": np.cos(2 * np.pi * (month - 1) / 12),
            "day_of_year_sin": np.sin(2 * np.pi * (day_of_year - 1) / 365.25),
            "day_of_year_cos": np.cos(2 * np.pi * (day_of_year - 1) / 365.25),
            "is_weekend": (day_of_week >= 5).astype(float),
        },
        index=target.index,
    )


def _entity_lookup(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        str(entity): group.set_index("timestamp").sort_index()
        for entity, group in frame.groupby("entity", sort=False)
    }


def _value_at(group: pd.DataFrame, timestamp: pd.Timestamp, column: str) -> object:
    if timestamp not in group.index:
        return np.nan
    value = group.at[timestamp, column]
    if isinstance(value, pd.Series):
        return value.iloc[-1]
    return value


def _make_rows(
    prepared: PreparedPanel,
    origins_targets: Iterable[tuple[pd.Timestamp, pd.Timestamp]],
    *,
    include_target: bool,
) -> pd.DataFrame:
    lookup = _entity_lookup(prepared.frame)
    records: list[dict[str, object]] = []
    for entity in prepared.entities:
        group = lookup[entity]
        observed = group["observed_target"]
        entity_median = float(observed.median()) if observed.notna().any() else np.nan
        for origin, target_date in origins_targets:
            horizon = int((target_date - origin).days)
            if horizon < 1 or horizon > 7:
                continue
            row: dict[str, object] = {
                "entity": entity,
                "origin": origin,
                "target_date": target_date,
                "horizon": horizon,
            }
            for lag in POINT_LAGS:
                row[f"lag_{lag}"] = _value_at(
                    group, origin - pd.Timedelta(days=lag), "observed_target"
                )
            for window in ROLLING_WINDOWS:
                start = origin - pd.Timedelta(days=window - 1)
                values = observed.loc[start:origin]
                row[f"roll_mean_{window}"] = values.mean()
                row[f"roll_std_{window}"] = values.std(ddof=0)
                row[f"roll_min_{window}"] = values.min()
                row[f"roll_max_{window}"] = values.max()
                row[f"roll_count_{window}"] = float(values.count())
                availability = group.loc[start:origin, "available"]
                row[f"unavailable_rate_{window}"] = (
                    float((~availability).mean()) if len(availability) else np.nan
                )
            seasonal = np.asarray(
                [
                    _value_at(group, target_date - pd.Timedelta(days=lag), "observed_target")
                    for lag in BASELINE_LAGS
                ],
                dtype=float,
            )
            for lag, value in zip(BASELINE_LAGS, seasonal, strict=True):
                row[f"seasonal_lag_{lag}"] = value
                row[f"seasonal_lag_{lag}_missing"] = float(not np.isfinite(value))
            baseline = _weighted_baseline(seasonal.reshape(1, -1))[0]
            if not np.isfinite(baseline):
                fallback_candidates = (
                    row["roll_mean_7"],
                    row["lag_0"],
                    entity_median,
                    prepared.global_median,
                )
                baseline = 0.0
                for value in fallback_candidates:
                    if not isinstance(value, int | float | np.integer | np.floating):
                        continue
                    candidate = float(cast(Any, value))
                    if np.isfinite(candidate):
                        baseline = candidate
                        break
            row["target_baseline"] = max(0.0, float(baseline))
            row["target_baseline_missing"] = float(
                not np.isfinite(_weighted_baseline(seasonal.reshape(1, -1))[0])
            )
            for column in (
                prepared.mapping.known_future_numeric + prepared.mapping.known_future_categorical
            ):
                row[column] = _value_at(group, target_date, column)
            for column in prepared.mapping.static_numeric + prepared.mapping.static_categorical:
                row[column] = _value_at(group, origin, column)
            if include_target:
                row["target"] = _value_at(group, target_date, "observed_target")
            records.append(row)
    panel = pd.DataFrame.from_records(records)
    if panel.empty:
        return panel
    calendar = _calendar_features(pd.to_datetime(panel["target_date"]))
    for column in map(str, calendar.columns):
        panel[column] = calendar[column].to_numpy()
    return panel


def build_panel_frames(
    prepared: PreparedPanel, fit_cutoff: pd.Timestamp, predict_origin: pd.Timestamp
) -> PanelFrames:
    historical_dates = pd.date_range(prepared.frame["timestamp"].min(), fit_cutoff, freq="D")
    training_pairs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for target_date in historical_dates:
        for horizon in range(1, 8):
            origin = target_date - pd.Timedelta(days=horizon)
            if origin <= fit_cutoff:
                training_pairs.append((origin, target_date))
    train = _make_rows(prepared, training_pairs, include_target=True)
    if not train.empty:
        train = train.loc[train["target"].notna()].reset_index(drop=True)
    predict_pairs = [
        (predict_origin, predict_origin + pd.Timedelta(days=horizon)) for horizon in range(1, 8)
    ]
    predict = _make_rows(prepared, predict_pairs, include_target=False)

    categorical = (
        "entity",
        *prepared.mapping.known_future_categorical,
        *prepared.mapping.static_categorical,
    )
    excluded = {"origin", "target_date", "target"}
    feature_columns = tuple(column for column in train.columns if column not in excluded)
    if not feature_columns:
        raise ValueError("no trainable feature rows were produced")
    levels: dict[str, tuple[str, ...]] = {}
    combined = pd.concat(
        [train[list(feature_columns)], predict[list(feature_columns)]], ignore_index=True
    )
    for column in categorical:
        values = combined[column].astype("string").fillna("__missing__")
        levels[column] = tuple(sorted(values.unique().tolist()))
    return PanelFrames(
        train=train,
        predict=predict,
        feature_columns=feature_columns,
        categorical_columns=tuple(column for column in categorical if column in feature_columns),
        categorical_levels=levels,
    )


def tree_frame(
    frame: pd.DataFrame,
    feature_columns: tuple[str, ...],
    categorical_columns: tuple[str, ...],
    categorical_levels: dict[str, tuple[str, ...]],
) -> pd.DataFrame:
    result = frame.loc[:, feature_columns].copy()
    for column in feature_columns:
        if column in categorical_columns:
            values = result[column].astype("string").fillna("__missing__")
            result[column] = pd.Categorical(values, categories=categorical_levels[column])
        else:
            result[column] = pd.to_numeric(result[column], errors="coerce").replace(
                [np.inf, -np.inf], np.nan
            )
    return result
