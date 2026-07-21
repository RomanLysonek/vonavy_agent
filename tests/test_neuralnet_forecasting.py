from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import torch
from pydantic import ValidationError

from vonavy_agent.forecasting.contracts import (
    ForecastLimits,
    ForecastMapping,
    InputIdentity,
    LocalForecastRequest,
)
from vonavy_agent.forecasting.neural_net import (
    FINAL_SEEDS,
    NEURALNET_PARAMETERS,
    run_neuralnet_forecast,
)


def _panel() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    start = pd.Timestamp("2025-01-01")
    for entity, base, category in (("A", 10.0, "skin"), ("B", 20.0, "fragrance")):
        for offset in range(77):
            timestamp = start + pd.Timedelta(days=offset)
            future = offset >= 70
            rows.append(
                {
                    "DateKey": timestamp,
                    "ProductId": entity,
                    "Quantity": None
                    if future
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


def test_neuralnet_contract_accepts_only_supported_adapter_ids() -> None:
    common = {
        "dataset_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "input_path": "input.csv",
        "output_directory": "output",
        "media_type": "text/csv",
        "input_sha256": "0" * 64,
        "mapping": _mapping(),
        "training_end": date(2025, 3, 11),
        "limits": ForecastLimits(
            max_bytes=1_000_000,
            max_rows=1_000,
            max_entities=10,
            max_history_days=1_000,
        ),
        "source_revision": "unknown",
        "requested_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    request = LocalForecastRequest(adapter_id="neuralnet-direct-v1", **common)
    assert request.adapter_id == "neuralnet-direct-v1"
    with pytest.raises(ValidationError):
        LocalForecastRequest(adapter_id="made-up-model", **common)


def test_neuralnet_parameters_match_the_proven_direct_architecture() -> None:
    assert NEURALNET_PARAMETERS["architecture"] == "embedding-mlp-residual"
    assert NEURALNET_PARAMETERS["hidden_dims"] == "256,128,64"
    assert NEURALNET_PARAMETERS["dropout"] == "0.20,0.15,0.10"
    assert NEURALNET_PARAMETERS["entity_embedding_dim"] == 12
    assert NEURALNET_PARAMETERS["categorical_embedding_dim"] == 4
    assert NEURALNET_PARAMETERS["horizon_embedding_dim"] == 4
    assert NEURALNET_PARAMETERS["epochs"] == 30
    assert NEURALNET_PARAMETERS["loss"] == "mse"
    assert NEURALNET_PARAMETERS["target"] == "log1p_residual"
    assert FINAL_SEEDS == (42, 123, 777)


def test_neuralnet_produces_versionable_nonnegative_forecast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write_csv_bytes(frame: pd.DataFrame, destination: Path) -> None:
        destination.write_bytes(frame.to_csv(index=False).encode())

    monkeypatch.setattr(
        "vonavy_agent.forecasting.neural_net._write_forecast",
        write_csv_bytes,
    )
    output = run_neuralnet_forecast(
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
    assert output.result.adapter.id == "neuralnet-direct-v1"
    assert output.manifest.adapter.id == "neuralnet-direct-v1"
    assert output.result.holdout.supported is True
    assert output.result.holdout.wape is not None
    forecast = pd.read_csv(output.forecast_path)
    assert len(forecast) == 14
    assert np.isfinite(forecast["prediction"]).all()
    assert (forecast["prediction"] >= 0).all()
    assert output.model_path.name == "model.pt"
    artifact = torch.load(output.model_path, map_location="cpu", weights_only=False)
    assert artifact["schema_version"] == "neuralnet-direct-artifact/v1"
    assert artifact["adapter_id"] == "neuralnet-direct-v1"
    assert len(artifact["model_state_dicts"]) == 3
    assert artifact["category_dims"][0] == 12
    assert output.model_path.stat().st_size > 0
