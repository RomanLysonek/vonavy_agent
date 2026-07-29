from __future__ import annotations

from datetime import date, datetime

from vonavy_agent.forecasting.aws_batch import _validate_comparison_request


def test_comparison_request_accepts_json_arrays_and_iso_temporals(monkeypatch) -> None:
    monkeypatch.setenv("VONAVY_DATA_BUCKET", "data-bucket")
    dataset_id = "00000000-0000-0000-0000-000000000001"
    run_id = "00000000-0000-0000-0000-000000000002"
    child_runs = [
        {
            "adapter_id": "xgboost-direct-v1",
            "run_id": "00000000-0000-0000-0000-000000000003",
        },
        {
            "adapter_id": "neuralnet-direct-v1",
            "run_id": "00000000-0000-0000-0000-000000000004",
        },
        {
            "adapter_id": "chronos2-zero-shot-v1",
            "run_id": "00000000-0000-0000-0000-000000000005",
        },
    ]
    raw = {
        "schema_version": "forecast-comparison-request/v1",
        "owner_id": "owner",
        "dataset_id": dataset_id,
        "run_id": run_id,
        "input": {
            "bucket": "data-bucket",
            "key": "datasets/users/owner/input.parquet",
            "version_id": "version-1",
            "sha256": None,
            "media_type": "application/vnd.apache.parquet",
            "byte_size": 290_275,
        },
        "output": {
            "bucket": "data-bucket",
            "prefix": (f"forecast-results/users/owner/datasets/{dataset_id}/runs/{run_id}/"),
        },
        "mapping": {
            "timestamp_column": "DateKey",
            "target_column": "Quantity",
            "entity_column": "ProductId",
            "availability_column": "ProductAvailable",
            "known_future_numeric": [],
            "known_future_categorical": [],
            "static_numeric": [],
            "static_categorical": [],
            "excluded": ["Unused"],
        },
        "training_end": "2026-01-11",
        "horizon_days": 7,
        "seed": 42,
        "limits": {
            "max_bytes": 500_000_000,
            "max_rows": 2_000_000,
            "max_entities": 20_000,
            "max_history_days": 3_000,
            "threads": 1,
        },
        "source_revision": "unknown",
        "requested_at": "2026-07-29T13:28:13.187149+00:00",
        "adapter_ids": [
            "xgboost-direct-v1",
            "neuralnet-direct-v1",
            "chronos2-zero-shot-v1",
        ],
        "child_runs": child_runs,
    }

    request, normalized_children = _validate_comparison_request(raw)

    assert request.training_end == date(2026, 1, 11)
    assert request.requested_at == datetime.fromisoformat("2026-07-29T13:28:13.187149+00:00")
    assert request.mapping.known_future_numeric == ()
    assert request.mapping.excluded == ("Unused",)
    assert request.adapter_id == "xgboost-direct-v1"
    assert normalized_children == tuple(child_runs)
