from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

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


def _event(method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "rawPath": path,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "12345678-1234-1234-1234-123456789012",
                        "token_use": "access",
                        "email": "reviewer@example.com",
                    }
                }
            },
        },
    }


class FakeDynamoClient:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transactions.append(kwargs)


class FakeTable:
    def __init__(self) -> None:
        self.client = FakeDynamoClient()
        self.meta = SimpleNamespace(client=self.client)
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.query_items: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        item = self.items.get((key["pk"], key["sk"]))
        return {"Item": item} if item else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"Items": self.query_items}


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
    slot_item = transaction[0]["Put"]["Item"]
    dataset_item = transaction[1]["Put"]["Item"]
    assert slot_item["sk"]["S"] == "SLOT#0000"
    assert slot_item["status"]["S"] == "pending"
    assert dataset_item["owner_sub"]["S"] == "12345678-1234-1234-1234-123456789012"
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
