from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from vonavy_agent.forecasting.chronos2 import (
    CHRONOS2_MODEL_ID,
    CHRONOS2_MODEL_REVISION,
    run_chronos2_forecast,
)
from vonavy_agent.forecasting.contracts import (
    CHRONOS2_SOURCE_REPOSITORY,
    CHRONOS2_SOURCE_REVISION,
    AdapterIdentity,
    ForecastLimits,
    ForecastMapping,
    InputIdentity,
    LocalForecastRequest,
)


class FakeChronosPipeline:
    def __init__(self, *, nonfinite_first: bool = False) -> None:
        self.nonfinite_first = nonfinite_first
        self.calls: list[dict[str, object]] = []

    def predict_df(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append(kwargs)
        future = kwargs["future_df"]
        assert isinstance(future, pd.DataFrame)
        result = future[["item_id", "timestamp"]].copy()
        first = result["timestamp"].min()
        day = (result["timestamp"] - first).dt.days.to_numpy(dtype=float)
        entity = result["item_id"].map({"A": 10.0, "B": 20.0}).to_numpy(dtype=float)
        point = entity + day
        result["target_name"] = "target"
        result["predictions"] = point
        result["0.1"] = point - 1.0
        result["0.5"] = point
        result["0.9"] = point + 1.0
        if self.nonfinite_first:
            result.loc[result.index[0], ["predictions", "0.1", "0.5", "0.9"]] = np.nan
        return result.iloc[::-1].reset_index(drop=True)


def _panel() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    start = pd.Timestamp("2025-01-01")
    for entity, base, category in (("A", 10.0, "skin"), ("B", 20.0, "fragrance")):
        for offset in range(77):
            future = offset >= 70
            rows.append(
                {
                    "DateKey": start + pd.Timedelta(days=offset),
                    "ProductId": entity,
                    "Quantity": None if future else base + 2.0 * (offset % 7),
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


def test_chronos_contract_binds_zero_shot_source_identity() -> None:
    request = LocalForecastRequest(
        dataset_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        input_path="input.csv",
        output_directory="output",
        media_type="text/csv",
        input_sha256="0" * 64,
        mapping=_mapping(),
        training_end=date(2025, 3, 11),
        adapter_id="chronos2-zero-shot-v1",
        limits=ForecastLimits(
            max_bytes=1_000_000,
            max_rows=1_000,
            max_entities=10,
            max_history_days=1_000,
        ),
        source_revision="unknown",
        requested_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert request.adapter_id == "chronos2-zero-shot-v1"
    identity = AdapterIdentity(id=request.adapter_id)
    assert identity.source_repository == CHRONOS2_SOURCE_REPOSITORY
    assert identity.source_revision == CHRONOS2_SOURCE_REVISION


def test_chronos_zero_shot_forecast_realigns_quantiles_and_writes_descriptor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def write_csv(frame: pd.DataFrame, destination: Path, index: bool = False) -> None:
        del index
        destination.write_text(frame.to_csv(index=False), encoding="utf-8")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", write_csv)
    fake = FakeChronosPipeline()
    output = run_chronos2_forecast(
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
        pipeline=fake,
    )

    forecast = pd.read_csv(output.forecast_path)
    assert len(forecast) == 14
    assert (
        np.isfinite(forecast[["prediction", "quantile_0.1", "quantile_0.5", "quantile_0.9"]])
        .all()
        .all()
    )
    assert (forecast["prediction"] >= 0).all()
    assert (forecast["quantile_0.1"] <= forecast["quantile_0.5"]).all()
    assert (forecast["quantile_0.5"] <= forecast["quantile_0.9"]).all()
    assert output.result.adapter.id == "chronos2-zero-shot-v1"
    assert output.manifest.adapter.id == "chronos2-zero-shot-v1"
    assert output.result.timing.fit_seconds == 0.0
    assert output.model_path.name == "chronos-model.json"
    descriptor = json.loads(output.model_path.read_text())
    assert descriptor["model_id"] == CHRONOS2_MODEL_ID
    assert descriptor["model_revision"] == CHRONOS2_MODEL_REVISION
    assert descriptor["license"] == "Apache-2.0"
    assert descriptor["weights_baked_into_immutable_image"] is True
    assert descriptor["runtime_downloads"] is False
    assert descriptor["fine_tuned"] is False
    assert fake.calls
    call = fake.calls[-1]
    assert call["prediction_length"] == 7
    assert call["freq"] == "D"
    assert call["cross_learning"] is True
    future = call["future_df"]
    assert isinstance(future, pd.DataFrame)
    assert "ProductAvailable" not in future.columns
    assert "was_available" not in future.columns


def test_chronos_nonfinite_output_uses_safe_baseline_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        pd.DataFrame,
        "to_parquet",
        lambda self, destination, index=False: Path(destination).write_text(
            self.to_csv(index=index), encoding="utf-8"
        ),
    )
    output = run_chronos2_forecast(
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
        pipeline=FakeChronosPipeline(nonfinite_first=True),
    )
    forecast = pd.read_csv(output.forecast_path)
    assert forecast["fallback_used"].any()
    assert np.isfinite(forecast["prediction"]).all()
    assert (forecast["prediction"] >= 0).all()


def test_local_worker_dispatches_chronos_without_runtime_download(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import hashlib

    import vonavy_agent.forecasting.worker as worker_module

    input_path = tmp_path / "input.csv"
    input_path.write_text(_panel().to_csv(index=False), encoding="utf-8")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    request = LocalForecastRequest(
        dataset_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        input_path="input.csv",
        output_directory="output",
        media_type="text/csv",
        input_sha256=digest,
        mapping=_mapping(),
        training_end=date(2025, 3, 11),
        adapter_id="chronos2-zero-shot-v1",
        limits=ForecastLimits(
            max_bytes=1_000_000,
            max_rows=1_000,
            max_entities=10,
            max_history_days=1_000,
        ),
        source_revision="unknown",
        requested_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")

    def fake_runner(**kwargs):
        return run_chronos2_forecast(**kwargs, pipeline=FakeChronosPipeline())

    monkeypatch.setattr(worker_module, "run_chronos2_forecast", fake_runner)
    monkeypatch.setattr(
        pd.DataFrame,
        "to_parquet",
        lambda self, destination, index=False: Path(destination).write_text(
            self.to_csv(index=index), encoding="utf-8"
        ),
    )
    assert worker_module.run_local(request_path, "result.json", tmp_path) == 0
    payload = json.loads((tmp_path / "result.json").read_text())
    assert payload["adapter"]["id"] == "chronos2-zero-shot-v1"
