from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("DATA_BUCKET", "data-bucket")
os.environ.setdefault("METADATA_TABLE", "metadata-table")
os.environ.setdefault("FORECAST_JOB_QUEUE", "queue-arn")
os.environ.setdefault("FORECAST_JOB_DEFINITION", "job-definition-arn:1")
os.environ.setdefault("SOURCE_REVISION", "unknown")

HANDLER_PATH = Path(__file__).parents[1] / "lambda/forecast_control_plane/handler.py"
SPEC = importlib.util.spec_from_file_location("forecast_control_plane_handler", HANDLER_PATH)
assert SPEC is not None and SPEC.loader is not None
handler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handler)


class FakeDdb:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transactions.append(kwargs)


def test_request_document_contains_no_batch_tags() -> None:
    request = handler._request_document(
        owner="owner",
        dataset_id="00000000-0000-0000-0000-000000000001",
        dataset={
            "object_key": "datasets/users/owner/dataset/input.csv",
            "object_version_id": "version-1",
            "filename": "input.csv",
            "actual_size": 123,
        },
        run_id="00000000-0000-0000-0000-000000000002",
        mapping={
            "timestamp_column": "DateKey",
            "target_column": "Quantity",
            "entity_column": "ProductId",
            "availability_column": None,
            "known_future_numeric": [],
            "known_future_categorical": [],
            "static_numeric": [],
            "static_categorical": [],
            "excluded": [],
        },
        training_end="2025-01-01",
        requested_at="2025-01-01T00:00:00+00:00",
    )
    serialized = json.dumps(request)
    assert "tags" not in request
    assert "propagateTags" not in serialized
    assert request["limits"]["threads"] == 1
    assert request["input"]["version_id"] == "version-1"


def test_mapping_rejects_duplicate_roles() -> None:
    try:
        handler._mapping(
            {
                "mapping": {
                    "timestampColumn": "DateKey",
                    "targetColumn": "Quantity",
                    "entityColumn": "Quantity",
                }
            }
        )
    except handler.ApiError as exc:
        assert exc.code == "invalid_forecast_mapping"
        assert exc.status == 422
    else:
        raise AssertionError("duplicate mapping role was accepted")


def test_terminal_transaction_uses_only_expression_values_needed_by_each_update() -> None:
    ddb = FakeDdb()
    item = {
        "run_id": "00000000-0000-0000-0000-000000000002",
        "dataset_id": "00000000-0000-0000-0000-000000000001",
        "input_version_id": "version-1",
    }
    result = {
        "schema_version": "forecast-result/v1",
        "status": "succeeded",
        "owner_id": "owner",
        "dataset_id": item["dataset_id"],
        "run_id": item["run_id"],
        "input": {"version_id": "version-1"},
        "profile": {"entities": 2, "fallback_rows": 0},
        "holdout": {"wape": 0.2},
    }
    handler._terminalize(ddb, "owner", item, result, "result-version")
    transaction = ddb.transactions[0]["TransactItems"]
    run_values = transaction[0]["Update"]["ExpressionAttributeValues"]
    slot_values = transaction[1]["Update"]["ExpressionAttributeValues"]
    assert ":released" not in run_values
    assert ":version" not in slot_values
    assert ":rows" not in slot_values


def test_lambda_returns_404_for_unknown_route() -> None:
    response = handler.lambda_handler(
        {
            "routeKey": "GET /not-real",
            "rawPath": "/not-real",
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "owner"}}}},
        },
        type("Context", (), {"aws_request_id": "request-1"})(),
    )
    assert response["statusCode"] == 404
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "not_found"
