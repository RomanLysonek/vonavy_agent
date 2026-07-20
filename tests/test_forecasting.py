from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from vonavy_agent.forecasting.contracts import (
    ForecastLimits,
    ForecastMapping,
    InputIdentity,
    LocalForecastRequest,
)
from vonavy_agent.forecasting.mapping import (
    build_forecast_plan,
    confirmation_token_for_forecast_plan,
    suggest_forecast_mapping,
)
from vonavy_agent.forecasting.model import XGBOOST_PARAMETERS, run_xgboost_forecast
from vonavy_agent.forecasting.panel import build_panel_frames, prepare_daily_panel
from vonavy_agent.forecasting.worker import run_local


def _panel(*, future: bool = True) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    start = pd.Timestamp("2025-01-01")
    for entity, base, category in (("A", 10.0, "skin"), ("B", 20.0, "fragrance")):
        for offset in range(77 if future else 70):
            timestamp = start + pd.Timedelta(days=offset)
            is_future = offset >= 70
            rows.append(
                {
                    "DateKey": timestamp,
                    "ProductId": entity,
                    "Quantity": None
                    if is_future
                    else base + 2.0 * (offset % 7) + (5.0 if offset % 10 == 0 else 0.0),
                    "ProductAvailable": offset not in {16, 44},
                    "Discount": float(offset % 3),
                    "Campaign": f"campaign-{offset % 2}",
                    "Category": category,
                }
            )
    return pd.DataFrame(rows)


def _mapping() -> ForecastMapping:
    return ForecastMapping(
        timestamp_column="DateKey",
        entity_column="ProductId",
        target_column="Quantity",
        availability_column="ProductAvailable",
        known_future_numeric=("Discount",),
        known_future_categorical=("Campaign",),
        static_categorical=("Category",),
    )


def test_mapping_proposal_is_conservative_and_requires_confirmation() -> None:
    proposal = suggest_forecast_mapping(_panel())
    assert proposal["suggested"]["timestamp_column"] == "DateKey"
    assert proposal["suggested"]["entity_column"] == "ProductId"
    assert proposal["suggested"]["target_column"] == "Quantity"
    assert proposal["suggested"]["availability_column"] == "ProductAvailable"
    assert proposal["requires_confirmation"] is True
    assert proposal["timestamp"][0]["confidence"] > 0.9


def test_confirmation_token_binds_mapping_and_training_end() -> None:
    limits = {"max_rows": 2_000_000, "max_entities": 20_000, "max_history_days": 3_000}
    first = confirmation_token_for_forecast_plan(
        owner_id="owner",
        dataset_version_id="version-1",
        dataset_sha256="1" * 64,
        mapping=_mapping(),
        training_end=date(2025, 3, 11),
        source_revision="2" * 40,
        limits=limits,
    )
    second = confirmation_token_for_forecast_plan(
        owner_id="owner",
        dataset_version_id="version-1",
        dataset_sha256="1" * 64,
        mapping=_mapping(),
        training_end=date(2025, 3, 10),
        source_revision="2" * 40,
        limits=limits,
    )
    assert first != second


def test_direct_panel_is_seven_day_and_never_exposes_target_as_feature() -> None:
    prepared = prepare_daily_panel(
        _panel(),
        _mapping(),
        date(2025, 3, 11),
        max_rows=1_000,
        max_entities=10,
        max_history_days=1_000,
    )
    frames = build_panel_frames(prepared, prepared.training_end, prepared.training_end)
    assert len(frames.predict) == 14
    assert set(frames.predict["horizon"]) == set(range(1, 8))
    assert "target" not in frames.feature_columns
    assert "target_date" not in frames.feature_columns
    assert "origin" not in frames.feature_columns
    assert frames.train["target"].notna().all()


def test_known_future_features_require_uploaded_future_rows() -> None:
    with pytest.raises(ValueError, match="future rows"):
        prepare_daily_panel(
            _panel(future=False),
            _mapping(),
            date(2025, 3, 11),
            max_rows=1_000,
            max_entities=10,
            max_history_days=1_000,
        )


def test_unavailable_target_is_not_observed_zero() -> None:
    prepared = prepare_daily_panel(
        _panel(),
        _mapping(),
        date(2025, 3, 11),
        max_rows=1_000,
        max_entities=10,
        max_history_days=1_000,
    )
    unavailable = prepared.frame.loc[
        (prepared.frame["entity"] == "A")
        & (prepared.frame["timestamp"] == pd.Timestamp("2025-01-17"))
    ].iloc[0]
    assert unavailable["available"] is False or not bool(unavailable["available"])
    assert np.isnan(float(unavailable["observed_target"]))


def test_fixed_xgboost_configuration_is_single_threaded() -> None:
    assert XGBOOST_PARAMETERS == {
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


def test_xgboost_model_produces_nonnegative_forecast_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write_csv_bytes(frame: pd.DataFrame, destination: Path) -> None:
        destination.write_bytes(frame.to_csv(index=False).encode())

    monkeypatch.setattr("vonavy_agent.forecasting.model._write_forecast", write_csv_bytes)
    output = run_xgboost_forecast(
        raw=_panel(),
        mapping=_mapping(),
        training_end=pd.Timestamp("2025-03-11"),
        output_directory=tmp_path,
        owner_id="owner",
        dataset_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        input_identity=InputIdentity(sha256="0" * 64),
        source_revision="unknown",
        max_rows=1_000,
        max_entities=10,
        max_history_days=1_000,
    )
    assert output.result.status == "succeeded"
    assert output.result.profile.entities == 2
    assert output.result.profile.fallback_rows == 0
    assert output.result.holdout.supported is True
    assert output.result.holdout.wape is not None
    forecast = pd.read_csv(output.forecast_path)
    assert len(forecast) == 14
    assert np.isfinite(forecast["prediction"]).all()
    assert (forecast["prediction"] >= 0).all()
    assert output.manifest.adapter.source_revision
    assert output.model_path.stat().st_size > 0


def test_local_worker_rejects_path_escape(tmp_path: Path) -> None:
    request = LocalForecastRequest(
        dataset_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        input_path="../outside.csv",
        output_directory="output",
        media_type="text/csv",
        input_sha256=hashlib.sha256(b"unused").hexdigest(),
        mapping=_mapping(),
        training_end=date(2025, 3, 11),
        limits=ForecastLimits(
            max_bytes=1_000_000,
            max_rows=1_000,
            max_entities=10,
            max_history_days=1_000,
        ),
        source_revision="unknown",
        requested_at=datetime.now(UTC),
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    assert run_local(request_path, "result.json", tmp_path) == 2
    payload = json.loads((tmp_path / "result.json").read_text())
    assert payload["status"] == "invalid"


def test_plan_reports_exact_seven_day_window() -> None:
    plan = build_forecast_plan(_panel(), _mapping(), date(2025, 3, 11))
    assert plan["forecast_start"] == "2025-03-12"
    assert plan["forecast_end"] == "2025-03-18"
    assert plan["horizon_days"] == 7
    assert plan["adapter_id"] == "xgboost-direct-v1"
