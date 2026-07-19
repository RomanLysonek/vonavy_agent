from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

os.environ.update(
    {
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DEFAULT_REGION": "eu-central-1",
        "UPLOAD_BUCKET": "unit-upload-bucket",
        "DATA_BUCKET": "unit-data-bucket",
        "METADATA_TABLE": "unit-metadata-table",
        "MAX_UPLOAD_BYTES": "1024",
        "MAX_DATASETS_PER_OWNER": "2",
        "MAX_TOTAL_BYTES_PER_OWNER": "2048",
        "UPLOAD_RETENTION_DAYS": "14",
        "AWS_REGION_NAME": "eu-central-1",
        "USER_POOL_ID": "eu-central-1_unit",
        "USER_POOL_CLIENT_ID": "unit-client",
        "COGNITO_DOMAIN": "https://unit.auth.eu-central-1.amazoncognito.com",
        "WEB_URL": "https://unit.cloudfront.net/",
    }
)


def _load_handler() -> ModuleType:
    path = Path(__file__).parents[1] / "lambda/control_plane/handler.py"
    spec = importlib.util.spec_from_file_location("control_plane_handler", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handler = _load_handler()


def _event(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    owner: str = "12345678-1234-1234-1234-123456789012",
) -> dict[str, Any]:
    return {
        "rawPath": path,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": owner,
                        "token_use": "access",
                        "email": "reviewer@example.com",
                    }
                }
            },
        },
    }


def _client_error(codes: list[str], *, request_id: str = "unit-request") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "Canceled"},
            "CancellationReasons": [{"Code": code} for code in codes],
            "ResponseMetadata": {"RequestId": request_id},
        },
        "TransactWriteItems",
    )


def _ddb_value(value: dict[str, str]) -> str | int:
    if "S" in value:
        return value["S"]
    if "N" in value:
        return int(value["N"])
    raise AssertionError(f"unsupported DynamoDB value {value!r}")


def _ddb_item(item: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {key: _ddb_value(value) for key, value in item.items()}


class FakeDynamoClient:
    def __init__(self, table: FakeTable) -> None:
        self.table = table
        self.transactions: list[dict[str, Any]] = []
        self.forced_cancellation_codes: list[str] | None = None

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transactions.append(kwargs)
        if self.forced_cancellation_codes is not None:
            raise _client_error(self.forced_cancellation_codes)
        snapshot = dict(self.table.items)
        updated = dict(self.table.items)
        reasons: list[str] = []
        for action in kwargs["TransactItems"]:
            try:
                if "Update" in action:
                    self._apply_update(updated, action["Update"], snapshot)
                elif "Put" in action:
                    self._apply_put(updated, action["Put"], snapshot)
                else:
                    raise AssertionError(f"unsupported action {action!r}")
            except AssertionError:
                raise
            except Exception:
                reasons.append("ConditionalCheckFailed")
            else:
                reasons.append("None")
        if any(code != "None" for code in reasons):
            raise _client_error(reasons)
        self.table.items = updated

    def _apply_put(
        self,
        updated: dict[tuple[str, str], dict[str, Any]],
        put: dict[str, Any],
        snapshot: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        item = _ddb_item(put["Item"])
        key = (item["pk"], item["sk"])
        if "attribute_not_exists" in put.get("ConditionExpression", "") and key in snapshot:
            raise ValueError("condition failed")
        updated[key] = item

    def _apply_update(
        self,
        updated: dict[tuple[str, str], dict[str, Any]],
        update: dict[str, Any],
        snapshot: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        key = (
            update["Key"]["pk"]["S"],
            update["Key"]["sk"]["S"],
        )
        current = snapshot.get(key)
        names = update.get("ExpressionAttributeNames", {})
        values = {
            name: _ddb_value(value) for name, value in update["ExpressionAttributeValues"].items()
        }
        condition = update.get("ConditionExpression", "")
        status_attr = names.get("#status", "status")
        status = current.get(status_attr) if current else None
        expires_at = int(current.get("expires_at", 0)) if current else 0
        if condition.startswith("attribute_not_exists(pk)"):
            allowed = (
                current is None
                or status in {"released", "free", "expired"}
                or (status == "pending" and expires_at <= int(values[":now"]))
            )
        elif "owner_sub = :owner AND #status = :pending" in condition:
            allowed = bool(
                current
                and current.get("owner_sub") == values[":owner"]
                and current.get(status_attr) == values[":pending"]
            )
        elif "owner_sub = :owner AND upload_id = :upload" in condition:
            allowed = bool(
                current
                and current.get("owner_sub") == values[":owner"]
                and current.get("upload_id") == values[":upload"]
            )
        else:
            raise AssertionError(f"unsupported condition {condition}")
        if not allowed:
            raise ValueError("condition failed")
        new_item = dict(current or {"pk": key[0], "sk": key[1]})
        expression = update["UpdateExpression"]
        set_clause = expression.removeprefix("SET ")
        assignments: list[str] = []
        depth = 0
        chunk = []
        for char in set_clause:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                assignments.append("".join(chunk).strip())
                chunk = []
            else:
                chunk.append(char)
        assignments.append("".join(chunk).strip())
        for assignment in assignments:
            attr, value_ref = [part.strip() for part in assignment.split("=", 1)]
            attr = names.get(attr, attr)
            if value_ref.startswith("if_not_exists"):
                if attr not in new_item:
                    fallback = value_ref.split(",", 1)[1].rstrip(") ").strip()
                    new_item[attr] = values[fallback]
            else:
                new_item[attr] = values[value_ref]
        updated[key] = new_item


class FakeTable:
    def __init__(self) -> None:
        self.client = FakeDynamoClient(self)
        self.meta = SimpleNamespace(client=self.client)
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.query_items: list[dict[str, Any]] | None = None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self.items.get((key["pk"], key["sk"]))
        return {"Item": item} if item else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if self.query_items is not None:
            return {"Items": self.query_items}
        return {
            "Items": [
                item for (_pk, sk), item in sorted(self.items.items()) if sk.startswith("SLOT#")
            ]
        }


class FakeS3:
    def __init__(self) -> None:
        self.presigned: dict[str, Any] | None = None
        self.head: dict[str, Any] = {}
        self.head_requests: list[dict[str, Any]] = []
        self.copied: dict[str, Any] | None = None
        self.deleted_requests: list[dict[str, Any]] = []
        self.copy_version_id = "version-1"

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
        self.presigned = kwargs
        return {"url": "https://s3.example.test", "fields": kwargs["Fields"]}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_requests.append(kwargs)
        return self.head

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        self.copied = kwargs
        return {"VersionId": self.copy_version_id}

    def delete_object(self, **kwargs: Any) -> None:
        self.deleted_requests.append(kwargs)


def test_upload_validation_rejects_paths_and_server_limit() -> None:
    with pytest.raises(handler.ApiError, match="path components"):
        handler._validate_upload(
            {
                "datasetName": "Panel",
                "filename": "../panel.csv",
                "mediaType": "text/csv",
                "sizeBytes": 100,
            }
        )
    with pytest.raises(handler.ApiError) as error:
        handler._validate_upload(
            {
                "datasetName": "Panel",
                "filename": "panel.csv",
                "mediaType": "text/csv",
                "sizeBytes": 1025,
            }
        )
    assert error.value.code == "upload_too_large"
    assert error.value.status_code == 413

    with pytest.raises(handler.ApiError, match="control characters"):
        handler._validate_upload(
            {
                "datasetName": "Panel",
                "filename": "panel\n.csv",
                "mediaType": "text/csv",
                "sizeBytes": 100,
            }
        )
    with pytest.raises(handler.ApiError, match="path components"):
        handler._validate_upload(
            {
                "datasetName": "Panel",
                "filename": "folder\\panel.csv",
                "mediaType": "text/csv",
                "sizeBytes": 100,
            }
        )


def test_identity_is_derived_from_authorizer_claims() -> None:
    owner, email = handler._identity(_event("GET", "/api/datasets"))
    assert owner == "12345678-1234-1234-1234-123456789012"
    assert email == "reviewer@example.com"

    event = _event("GET", "/api/datasets")
    event["requestContext"]["authorizer"]["jwt"]["claims"]["token_use"] = "id"
    with pytest.raises(handler.ApiError) as error:
        handler._identity(event)
    assert error.value.code == "unauthorized"


def test_owner_upload_slots_enforce_hard_dataset_count() -> None:
    table = FakeTable()
    owner = "12345678-1234-1234-1234-123456789012"
    table.query_items = [{"slot_number": 0, "expected_size": 700}]
    assert handler._next_owner_upload_slot(table, owner, 900) == 1

    table.query_items = [
        {"slot_number": 0, "expected_size": 100},
        {"slot_number": 1, "expected_size": 100},
    ]
    with pytest.raises(handler.ApiError) as count_error:
        handler._next_owner_upload_slot(table, owner, 100)
    assert count_error.value.code == "dataset_quota_exceeded"


def test_create_upload_session_scopes_storage_to_authenticated_owner(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event(
            "POST",
            "/api/upload-sessions",
            body={
                "datasetName": "Interview panel",
                "filename": "panel.csv",
                "mediaType": "text/csv",
                "sizeBytes": 512,
            },
        ),
        None,
    )
    assert response["statusCode"] == 201
    payload = json.loads(response["body"])
    assert "x-amz-tagging" not in payload["upload"]["fields"]
    assert fake_s3.presigned is not None
    assert fake_s3.presigned["Bucket"] == "unit-upload-bucket"
    assert fake_s3.presigned["Key"].startswith(
        "pending/users/12345678-1234-1234-1234-123456789012/datasets/"
    )
    assert fake_s3.presigned["Conditions"][-1] == ["content-length-range", 512, 512]
    transaction = fake_table.client.transactions[0]["TransactItems"]
    slot_update = transaction[0]["Update"]
    dataset_item = transaction[1]["Put"]["Item"]
    upload_item = transaction[2]["Put"]["Item"]
    slot_item = fake_table.items[("USER#12345678-1234-1234-1234-123456789012", "SLOT#0000")]
    assert slot_update["Key"]["sk"] == {"S": "SLOT#0000"}
    assert "attribute_not_exists(pk)" in slot_update["ConditionExpression"]
    assert "if_not_exists(created_at" in slot_update["UpdateExpression"]
    assert slot_item["status"] == "pending"
    assert slot_item["upload_id"] == payload["uploadId"]
    assert dataset_item["owner_sub"]["S"] == "12345678-1234-1234-1234-123456789012"
    assert upload_item["owner_sub"]["S"] == "12345678-1234-1234-1234-123456789012"
    assert dataset_item["status"]["S"] == "upload_pending"
    assert dataset_item["slot_key"]["S"] == "SLOT#0000"
    assert dataset_item["staging_object_key"]["S"].startswith("pending/users/")
    assert dataset_item["object_key"]["S"].startswith("datasets/users/")


def test_complete_upload_copies_to_immutable_data_storage(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_s3.head = {
        "ContentLength": 512,
        "ContentType": "text/csv",
    }
    fake_table = FakeTable()
    owner = "12345678-1234-1234-1234-123456789012"
    upload_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    staging_key = "pending/users/owner/datasets/dataset-id/upload/panel.csv"
    object_key = "datasets/users/owner/dataset-id/upload/panel.csv"
    fake_table.items[(f"USER#{owner}", f"UPLOAD#{upload_id}")] = {
        "owner_sub": owner,
        "dataset_id": "dataset-id",
        "upload_id": upload_id,
        "slot_key": "SLOT#0000",
        "staging_object_key": staging_key,
        "object_key": object_key,
        "media_type": "text/csv",
        "expected_size": 512,
        "status": "pending",
    }
    fake_table.items[(f"USER#{owner}", "DATASET#dataset-id")] = {
        "owner_sub": owner,
        "dataset_id": "dataset-id",
        "upload_id": upload_id,
        "status": "upload_pending",
    }
    fake_table.items[(f"USER#{owner}", "SLOT#0000")] = {
        "owner_sub": owner,
        "slot_number": 0,
        "upload_id": upload_id,
        "expected_size": 512,
        "status": "pending",
    }
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", f"/api/upload-sessions/{upload_id}/complete", body={}),
        None,
    )
    assert response["statusCode"] == 200
    assert fake_s3.copied == {
        "Bucket": "unit-data-bucket",
        "Key": object_key,
        "CopySource": {"Bucket": "unit-upload-bucket", "Key": staging_key},
        "ContentType": "text/csv",
        "Metadata": {"source-upload-id": upload_id},
        "MetadataDirective": "REPLACE",
        "ServerSideEncryption": "AES256",
        "Tagging": "state=complete&retention=demo",
        "TaggingDirective": "REPLACE",
    }
    assert fake_s3.head_requests == [
        {"Bucket": "unit-data-bucket", "Key": object_key, "VersionId": "version-1"}
    ]
    assert fake_s3.deleted_requests == [{"Bucket": "unit-upload-bucket", "Key": staging_key}]
    completion_transaction = fake_table.client.transactions[0]["TransactItems"]
    assert len(completion_transaction) == 3
    dataset_update = completion_transaction[1]["Update"]
    slot_update = completion_transaction[2]["Update"]
    assert dataset_update["ExpressionAttributeValues"][":object_key"] == {"S": object_key}
    assert dataset_update["ExpressionAttributeValues"][":version"] == {"S": "version-1"}
    assert slot_update["Key"]["sk"] == {"S": "SLOT#0000"}


def test_complete_upload_rejects_and_deletes_wrong_final_size(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_s3.head = {"ContentLength": 511, "ContentType": "text/csv"}
    fake_table = FakeTable()
    owner = "12345678-1234-1234-1234-123456789012"
    upload_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    staging_key = "pending/users/owner/datasets/dataset-id/upload/panel.csv"
    object_key = "datasets/users/owner/dataset-id/upload/panel.csv"
    fake_table.items[(f"USER#{owner}", f"UPLOAD#{upload_id}")] = {
        "owner_sub": owner,
        "dataset_id": "dataset-id",
        "upload_id": upload_id,
        "slot_key": "SLOT#0000",
        "staging_object_key": staging_key,
        "object_key": object_key,
        "media_type": "text/csv",
        "expected_size": 512,
        "status": "pending",
    }
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", f"/api/upload-sessions/{upload_id}/complete", body={}),
        None,
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["error"]["code"] == "upload_size_mismatch"
    assert fake_s3.deleted_requests == [
        {
            "Bucket": "unit-data-bucket",
            "Key": object_key,
            "VersionId": "version-1",
        }
    ]
    assert fake_table.client.transactions == []


def test_list_datasets_filters_defensively_by_owner(monkeypatch) -> None:
    fake_table = FakeTable()
    owner = "12345678-1234-1234-1234-123456789012"
    fake_table.query_items = [
        {
            "owner_sub": owner,
            "dataset_id": "mine",
            "dataset_name": "Mine",
            "filename": "mine.csv",
            "media_type": "text/csv",
            "expected_size": 10,
            "status": "uploaded",
            "created_at": "2026-07-18T10:00:00+00:00",
            "updated_at": "2026-07-18T10:00:00+00:00",
        },
        {
            "owner_sub": "another-owner",
            "dataset_id": "hidden",
            "dataset_name": "Hidden",
            "filename": "hidden.csv",
            "media_type": "text/csv",
            "expected_size": 10,
            "status": "uploaded",
            "created_at": "2026-07-18T10:00:00+00:00",
            "updated_at": "2026-07-18T10:00:00+00:00",
        },
    ]
    monkeypatch.setattr(handler, "_table", fake_table)
    monkeypatch.setattr(handler, "_s3", FakeS3())

    response = handler.lambda_handler(_event("GET", "/api/datasets"), None)
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert [dataset["datasetId"] for dataset in payload["datasets"]] == ["mine"]


def _upload_body(name: str = "Panel") -> dict[str, Any]:
    return {
        "datasetName": name,
        "filename": "panel.csv",
        "mediaType": "text/csv",
        "sizeBytes": 512,
    }


def test_brand_new_owner_first_upload_creates_slot_and_metadata(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    owner = "new-owner-sub"
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner),
        None,
    )

    assert response["statusCode"] == 201
    payload = json.loads(response["body"])
    owner_pk = f"USER#{owner}"
    assert fake_table.items[(owner_pk, "SLOT#0000")]["upload_id"] == payload["uploadId"]
    assert (
        fake_table.items[(owner_pk, f"DATASET#{payload['datasetId']}")]["status"]
        == "upload_pending"
    )
    assert fake_table.items[(owner_pk, f"UPLOAD#{payload['uploadId']}")]["status"] == "pending"
    assert payload["upload"]["url"] == "https://s3.example.test"


def test_existing_released_slot_is_reserved_for_new_upload(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    owner = "12345678-1234-1234-1234-123456789012"
    fake_table.items[(f"USER#{owner}", "SLOT#0000")] = {
        "pk": f"USER#{owner}",
        "sk": "SLOT#0000",
        "owner_sub": owner,
        "slot_number": 0,
        "expected_size": 1,
        "status": "released",
        "created_at": "old",
        "expires_at": 1,
    }
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner), None
    )

    assert response["statusCode"] == 201
    slot = fake_table.items[(f"USER#{owner}", "SLOT#0000")]
    assert slot["status"] == "pending"
    assert slot["expected_size"] == 512
    assert slot["created_at"] == "old"


def test_existing_expired_pending_slot_is_reclaimed(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    owner = "12345678-1234-1234-1234-123456789012"
    fake_table.items[(f"USER#{owner}", "SLOT#0000")] = {
        "pk": f"USER#{owner}",
        "sk": "SLOT#0000",
        "owner_sub": owner,
        "slot_number": 0,
        "expected_size": 1,
        "status": "pending",
        "created_at": "old",
        "expires_at": 1,
    }
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner), None
    )

    assert response["statusCode"] == 201
    slot = fake_table.items[(f"USER#{owner}", "SLOT#0000")]
    assert slot["status"] == "pending"
    assert slot["expected_size"] == 512
    assert slot["expires_at"] > 1


def test_active_slot_transaction_race_reports_upload_slot_busy(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    fake_table.client.forced_cancellation_codes = ["ConditionalCheckFailed", "None", "None"]
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", "/api/upload-sessions", body=_upload_body()), None
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["error"]["code"] == "upload_slot_busy"
    assert fake_table.items == {}


def test_create_upload_session_transaction_is_atomic_when_metadata_fails(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    owner = "12345678-1234-1234-1234-123456789012"
    dataset_id = "11111111-1111-4111-8111-111111111111"
    upload_id = "22222222-2222-4222-8222-222222222222"
    fake_table.items[(f"USER#{owner}", f"DATASET#{dataset_id}")] = {
        "pk": f"USER#{owner}",
        "sk": f"DATASET#{dataset_id}",
        "owner_sub": owner,
    }
    uuids = iter([dataset_id, upload_id])
    monkeypatch.setattr(handler.uuid, "uuid4", lambda: next(uuids))
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner), None
    )

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "upload_transaction_failed"
    assert (f"USER#{owner}", "SLOT#0000") not in fake_table.items
    assert (f"USER#{owner}", f"UPLOAD#{upload_id}") not in fake_table.items


def test_unexpected_conditional_cancellation_is_not_reported_as_busy(monkeypatch, caplog) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    fake_table.client.forced_cancellation_codes = ["None", "ConditionalCheckFailed", "None"]
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", "/api/upload-sessions", body=_upload_body()), None
    )

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "upload_transaction_failed"
    assert any(
        "Create-upload DynamoDB transaction was canceled" in r.message for r in caplog.records
    )


def test_transaction_conflict_is_retryable_not_slot_busy(monkeypatch) -> None:
    fake_s3 = FakeS3()
    fake_table = FakeTable()
    fake_table.client.forced_cancellation_codes = ["TransactionConflict", "None", "None"]
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    response = handler.lambda_handler(
        _event("POST", "/api/upload-sessions", body=_upload_body()), None
    )

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "upload_transaction_conflict"


def test_complete_upload_updates_slot_and_remains_owner_isolated_and_idempotent(
    monkeypatch,
) -> None:
    fake_s3 = FakeS3()
    fake_s3.head = {"ContentLength": 512, "ContentType": "text/csv"}
    fake_table = FakeTable()
    owner = "owner-a"
    other_owner = "owner-b"
    upload_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    dataset_id = "dataset-id"
    staging_key = "pending/users/owner-a/datasets/dataset-id/upload/panel.csv"
    object_key = "datasets/users/owner-a/dataset-id/upload/panel.csv"
    owner_pk = f"USER#{owner}"
    fake_table.items[(owner_pk, f"UPLOAD#{upload_id}")] = {
        "owner_sub": owner,
        "dataset_id": dataset_id,
        "upload_id": upload_id,
        "slot_key": "SLOT#0000",
        "staging_object_key": staging_key,
        "object_key": object_key,
        "media_type": "text/csv",
        "expected_size": 512,
        "status": "pending",
    }
    fake_table.items[(owner_pk, f"DATASET#{dataset_id}")] = {
        "owner_sub": owner,
        "dataset_id": dataset_id,
        "upload_id": upload_id,
        "status": "upload_pending",
    }
    fake_table.items[(owner_pk, "SLOT#0000")] = {
        "owner_sub": owner,
        "slot_number": 0,
        "upload_id": upload_id,
        "expected_size": 512,
        "status": "pending",
    }
    monkeypatch.setattr(handler, "_s3", fake_s3)
    monkeypatch.setattr(handler, "_table", fake_table)

    other_response = handler.lambda_handler(
        _event(
            "POST",
            f"/api/upload-sessions/{upload_id}/complete",
            body={},
            owner=other_owner,
        ),
        None,
    )
    assert other_response["statusCode"] == 404

    response = handler.lambda_handler(
        _event("POST", f"/api/upload-sessions/{upload_id}/complete", body={}, owner=owner), None
    )
    assert response["statusCode"] == 200
    assert fake_table.items[(owner_pk, f"UPLOAD#{upload_id}")]["status"] == "completed"
    assert fake_table.items[(owner_pk, f"DATASET#{dataset_id}")]["status"] == "uploaded"
    assert fake_table.items[(owner_pk, "SLOT#0000")]["status"] == "completed"
    assert fake_s3.deleted_requests == [{"Bucket": "unit-upload-bucket", "Key": staging_key}]

    repeat = handler.lambda_handler(
        _event("POST", f"/api/upload-sessions/{upload_id}/complete", body={}, owner=owner), None
    )
    assert repeat["statusCode"] == 200
    assert json.loads(repeat["body"]) == json.loads(response["body"])
    assert len(fake_s3.deleted_requests) == 1
