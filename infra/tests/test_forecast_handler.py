from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("DATA_BUCKET", "data-bucket")
os.environ.setdefault("METADATA_TABLE", "metadata-table")
os.environ.setdefault("FORECAST_JOB_QUEUE", "queue-arn")
os.environ.setdefault("FORECAST_JOB_DEFINITION", "job-definition-arn:1")
os.environ.setdefault("SOURCE_REVISION", "unknown")

HANDLER_PATH = Path(__file__).parents[1] / "lambda/forecast_control_plane/handler.py"
sys.path.insert(0, str(HANDLER_PATH.parent))
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
        adapter_id="xgboost-direct-v1",
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


def test_adapter_selection_is_validated_and_bound_to_fingerprint() -> None:
    assert handler._adapter_id({}) == "xgboost-direct-v1"
    assert handler._adapter_id({"adapterId": "neuralnet-direct-v1"}) == "neuralnet-direct-v1"
    assert handler._adapter_id({"adapterId": "chronos2-zero-shot-v1"}) == "chronos2-zero-shot-v1"
    try:
        handler._adapter_id({"adapterId": "unknown"})
    except handler.ApiError as exc:
        assert exc.code == "unsupported_forecast_adapter"
        assert exc.status == 422
    else:
        raise AssertionError("unsupported adapter was accepted")

    mapping = {"timestamp_column": "DateKey", "target_column": "Quantity"}
    xgb = handler._fingerprint("dataset", mapping, "2025-01-01", "xgboost-direct-v1")
    neural = handler._fingerprint("dataset", mapping, "2025-01-01", "neuralnet-direct-v1")
    chronos = handler._fingerprint("dataset", mapping, "2025-01-01", "chronos2-zero-shot-v1")
    assert xgb != neural
    assert chronos not in {xgb, neural}


def test_terminal_transaction_uses_only_expression_values_needed_by_each_update() -> None:
    ddb = FakeDdb()
    item = {
        "run_id": "00000000-0000-0000-0000-000000000002",
        "dataset_id": "00000000-0000-0000-0000-000000000001",
        "input_version_id": "version-1",
        "adapter_id": "neuralnet-direct-v1",
    }
    result = {
        "schema_version": "forecast-result/v1",
        "status": "succeeded",
        "owner_id": "owner",
        "dataset_id": item["dataset_id"],
        "run_id": item["run_id"],
        "input": {"version_id": "version-1"},
        "adapter": {"id": "neuralnet-direct-v1"},
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


def test_agent_route_returns_confirmable_plan(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        handler,
        "_agent_plan",
        lambda event, dataset_id: {
            "schemaVersion": "forecast-agent-plan/v1",
            "datasetId": dataset_id,
            "requiresConfirmation": True,
            "agentMode": "bedrock",
        },
    )
    response = handler.lambda_handler(
        {
            "routeKey": "POST /api/datasets/{dataset_id}/forecast-agent",
            "rawPath": "/api/datasets/00000000-0000-0000-0000-000000000001/forecast-agent",
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "owner"}}}},
        },
        type("Context", (), {"aws_request_id": "request-1"})(),
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["requiresConfirmation"] is True
    assert payload["agentMode"] == "bedrock"


class QuotaTable:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def update_item(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def test_agent_quota_is_owner_scoped_and_bounded() -> None:
    table = QuotaTable()
    handler._consume_agent_quota(table, "owner")
    request = table.calls[0]
    assert request["Key"]["pk"] == "USER#owner"
    assert request["Key"]["sk"].startswith("AGENT_QUOTA#")
    assert request["ExpressionAttributeValues"][":limit"] == 20
    assert "call_count < :limit" in request["ConditionExpression"]


def test_agent_quota_limit_returns_429() -> None:
    error = handler.ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "limit"}},
        "UpdateItem",
    )
    try:
        handler._consume_agent_quota(QuotaTable(error), "owner")
    except handler.ApiError as exc:
        assert exc.code == "agent_rate_limit_exceeded"
        assert exc.status == 429
    else:
        raise AssertionError("exhausted AI quota was accepted")


class StoredBody:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.content = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.content if amount < 0 else self.content[:amount]

    def close(self) -> None:
        self.closed = True


class ValidationTable:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.requests: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return {"Item": self.item}


class ValidationS3:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return {"Body": StoredBody(self.payload), "VersionId": "result-version"}


def test_agent_reads_exact_server_owned_validation_result() -> None:
    owner = "owner"
    dataset_id = "00000000-0000-0000-0000-000000000001"
    validation_job_id = "00000000-0000-0000-0000-000000000002"
    dataset = {"object_version_id": "dataset-version"}
    payload = {
        "schema_version": "validation-result/v1",
        "status": "succeeded",
        "job_id": validation_job_id,
        "dataset_id": dataset_id,
        "input_identity": {"version_id": "dataset-version"},
        "columns": [{"name": "DateKey", "logical_type": "date"}],
    }
    table = ValidationTable(
        {
            "owner_sub": owner,
            "dataset_id": dataset_id,
            "status": "succeeded",
            "result_key": "validation-results/users/owner/result.json",
            "result_version_id": "result-version",
        }
    )
    s3 = ValidationS3(payload)

    result = handler._validation_result_for_agent(
        s3,
        table,
        owner=owner,
        dataset=dataset,
        dataset_id=dataset_id,
        validation_job_id=validation_job_id,
    )

    assert result == payload
    assert table.requests == [
        {
            "Key": {
                "pk": "USER#owner",
                "sk": f"VALIDATION#{validation_job_id}",
            },
            "ConsistentRead": True,
        }
    ]
    assert s3.requests == [
        {
            "Bucket": "data-bucket",
            "Key": "validation-results/users/owner/result.json",
            "VersionId": "result-version",
        }
    ]


def test_agent_hides_other_owner_validation_result() -> None:
    table = ValidationTable(
        {
            "owner_sub": "other-owner",
            "dataset_id": "00000000-0000-0000-0000-000000000001",
            "status": "succeeded",
        }
    )
    try:
        handler._validation_result_for_agent(
            ValidationS3({}),
            table,
            owner="owner",
            dataset={"object_version_id": "dataset-version"},
            dataset_id="00000000-0000-0000-0000-000000000001",
            validation_job_id="00000000-0000-0000-0000-000000000002",
        )
    except handler.ApiError as exc:
        assert exc.code == "validation_not_found"
        assert exc.status == 404
    else:
        raise AssertionError("cross-owner validation result was exposed")


class AgentSessionTable:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.updates: list[dict[str, Any]] = []

    def update_item(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        if self.error is not None:
            raise self.error


def _session_turn() -> dict[str, Any]:
    return {
        "history": [
            {"role": "user", "text": "Compare the models"},
            {"role": "assistant", "text": "XGBoost is the lowest-cost trained option."},
        ],
        "draftPlan": {"adapterId": "xgboost-direct-v1"},
        "toolAudit": [{"name": "compare_models", "status": "succeeded"}],
        "provider": "amazon-bedrock",
        "model": "eu.anthropic.claude-opus-4-6-v1",
        "privacy": {"rawRowsSentToProvider": False},
    }


def test_agent_session_message_uses_optimistic_turn_lock(monkeypatch: Any) -> None:
    session_id = "00000000-0000-0000-0000-000000000003"
    table = AgentSessionTable()
    item = {
        "owner_sub": "owner",
        "session_id": session_id,
        "dataset_id": "00000000-0000-0000-0000-000000000001",
        "dataset_version_id": "version-1",
        "validation_job_id": "00000000-0000-0000-0000-000000000002",
        "messages_json": "[]",
        "draft_plan_json": "null",
        "turn_count": 1,
    }
    monkeypatch.setattr(handler, "_identity", lambda event: "owner")
    monkeypatch.setattr(handler, "_parse_body", lambda event: {"message": "Compare models"})
    monkeypatch.setattr(handler, "_clients", lambda: (None, table, None, None))
    monkeypatch.setattr(handler, "_agent_session_item", lambda *args: dict(item))
    monkeypatch.setattr(
        handler,
        "_dataset",
        lambda *args: {"object_version_id": "version-1"},
    )
    monkeypatch.setattr(handler, "_validation_result_for_agent", lambda *args, **kwargs: {})
    monkeypatch.setattr(handler, "_run_agent_session_turn", lambda **kwargs: _session_turn())

    response = handler._agent_session_message({}, session_id)

    update = table.updates[0]
    assert "turn_count=:expected_turns" in update["ConditionExpression"]
    assert update["ExpressionAttributeValues"][":expected_turns"] == 1
    assert response["turnCount"] == 2
    assert response["draftPlan"]["adapterId"] == "xgboost-direct-v1"


def test_agent_session_concurrent_message_returns_409(monkeypatch: Any) -> None:
    error = handler.ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "changed"}},
        "UpdateItem",
    )
    table = AgentSessionTable(error)
    session_id = "00000000-0000-0000-0000-000000000003"
    item = {
        "owner_sub": "owner",
        "session_id": session_id,
        "dataset_id": "00000000-0000-0000-0000-000000000001",
        "dataset_version_id": "version-1",
        "validation_job_id": "00000000-0000-0000-0000-000000000002",
        "messages_json": "[]",
        "draft_plan_json": "null",
        "turn_count": 1,
    }
    monkeypatch.setattr(handler, "_identity", lambda event: "owner")
    monkeypatch.setattr(handler, "_parse_body", lambda event: {"message": "Compare models"})
    monkeypatch.setattr(handler, "_clients", lambda: (None, table, None, None))
    monkeypatch.setattr(handler, "_agent_session_item", lambda *args: dict(item))
    monkeypatch.setattr(
        handler,
        "_dataset",
        lambda *args: {"object_version_id": "version-1"},
    )
    monkeypatch.setattr(handler, "_validation_result_for_agent", lambda *args, **kwargs: {})
    monkeypatch.setattr(handler, "_run_agent_session_turn", lambda **kwargs: _session_turn())

    try:
        handler._agent_session_message({}, session_id)
    except handler.ApiError as exc:
        assert exc.code == "agent_session_conflict"
        assert exc.status == 409
    else:
        raise AssertionError("concurrent agent-session update was accepted")


def test_agent_session_route_returns_persistent_session(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        handler,
        "_agent_session_create",
        lambda event, dataset_id: {
            "schemaVersion": "forecast-agent-session/v1",
            "sessionId": "00000000-0000-0000-0000-000000000003",
            "datasetId": dataset_id,
        },
    )
    response = handler.lambda_handler(
        {
            "routeKey": "POST /api/datasets/{dataset_id}/forecast-agent/sessions",
            "rawPath": (
                "/api/datasets/00000000-0000-0000-0000-000000000001/forecast-agent/sessions"
            ),
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "owner"}}}},
        },
        type("Context", (), {"aws_request_id": "request-1"})(),
    )
    assert response["statusCode"] == 201
    payload = json.loads(response["body"])
    assert payload["schemaVersion"] == "forecast-agent-session/v1"
