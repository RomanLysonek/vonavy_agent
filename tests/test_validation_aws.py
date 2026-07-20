from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vonavy_agent.validation_contracts import S3InputArtifact, S3OutputArtifact
from vonavy_agent.validation_worker import aws_batch
from vonavy_agent.validation_worker.artifacts import ArtifactTooLargeError, UnsafeArtifactPathError
from vonavy_agent.validation_worker.aws_artifacts import (
    S3FileArtifactReader,
    S3FileArtifactWriter,
)


class StreamingBody:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    def iter_chunks(self, chunk_size: int) -> Any:
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeS3:
    def __init__(self, content: bytes = b"") -> None:
        self.content = content
        self.head_requests: list[dict[str, Any]] = []
        self.get_requests: list[dict[str, Any]] = []
        self.put_requests: list[dict[str, Any]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_requests.append(kwargs)
        return {"ContentLength": len(self.content)}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_requests.append(kwargs)
        return {"Body": StreamingBody(self.content), "VersionId": "input-version"}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.put_requests.append(kwargs)
        return {"VersionId": "result-version"}


def _request(content: bytes, *, owner: str = "owner-001") -> dict[str, Any]:
    job_id = "validation-001"
    dataset_id = "dataset-001"
    return {
        "schema_version": "validation-request/v1",
        "job_id": job_id,
        "owner_id": owner,
        "dataset_id": dataset_id,
        "input": {
            "storage": "s3",
            "bucket": "data-bucket",
            "key": f"datasets/users/{owner}/{dataset_id}/upload/file.csv",
            "version_id": "input-version",
            "media_type": "text/csv",
            "expected_size_bytes": len(content),
        },
        "output": {
            "storage": "s3",
            "bucket": "data-bucket",
            "key": (
                f"validation-results/users/{owner}/datasets/{dataset_id}/jobs/{job_id}/result.json"
            ),
        },
        "limits": {
            "max_input_bytes": 1024,
            "max_rows": 100,
            "max_columns": 10,
            "max_string_sample_length": 64,
            "max_distinct_values": 10,
            "max_profile_rows": 100,
            "max_execution_seconds": 60,
        },
        "requested_at": datetime.now(UTC).isoformat(),
    }


def test_s3_reader_materializes_exact_version(tmp_path: Path) -> None:
    content = b"date,value\n2026-01-01,1\n"
    client = FakeS3(content)
    artifact = S3InputArtifact(
        bucket="data-bucket",
        key="datasets/users/owner/dataset/upload/file.csv",
        version_id="input-version",
        media_type="text/csv",
        expected_size_bytes=len(content),
    )
    reader = S3FileArtifactReader(
        client,
        allowed_bucket="data-bucket",
        allowed_key_prefix="datasets/users/owner/",
        max_bytes=1024,
        temporary_root=tmp_path,
    )

    with reader.materialize(artifact) as path:
        assert path.read_bytes() == content

    assert client.head_requests == [
        {
            "Bucket": "data-bucket",
            "Key": artifact.key,
            "VersionId": "input-version",
        }
    ]
    assert client.get_requests == client.head_requests


def test_s3_reader_enforces_owner_boundary_and_size(tmp_path: Path) -> None:
    client = FakeS3(b"x" * 11)
    reader = S3FileArtifactReader(
        client,
        allowed_bucket="data-bucket",
        allowed_key_prefix="datasets/users/owner-a/",
        max_bytes=10,
        temporary_root=tmp_path,
    )
    wrong_owner = S3InputArtifact(
        bucket="data-bucket",
        key="datasets/users/owner-b/dataset/upload/file.csv",
        version_id="version",
        media_type="text/csv",
    )
    with pytest.raises(UnsafeArtifactPathError), reader.materialize(wrong_owner):
        pass

    too_large = wrong_owner.model_copy(
        update={"key": "datasets/users/owner-a/dataset/upload/file.csv"}
    )
    with pytest.raises(ArtifactTooLargeError), reader.materialize(too_large):
        pass


def test_s3_writer_publishes_encrypted_versioned_result() -> None:
    client = FakeS3()
    writer = S3FileArtifactWriter(
        client,
        allowed_bucket="data-bucket",
        allowed_key_prefix="validation-results/users/owner/",
    )
    artifact = S3OutputArtifact(
        bucket="data-bucket",
        key="validation-results/users/owner/datasets/d/jobs/j/result.json",
    )
    receipt = writer.write_bytes(artifact, b"{}\n")

    assert receipt.version_id == "result-version"
    assert client.put_requests == [
        {
            "Bucket": "data-bucket",
            "Key": artifact.key,
            "Body": b"{}\n",
            "ContentType": "application/json",
            "ServerSideEncryption": "AES256",
            "Tagging": "state=validation-result&retention=demo",
        }
    ]


def test_batch_runner_publishes_success_result(monkeypatch, capsys) -> None:
    content = b"date,value\n2026-01-01,1\n2026-01-02,2\n"
    client = FakeS3(content)
    request = _request(content)
    monkeypatch.setenv("VONAVY_VALIDATION_REQUEST_JSON", json.dumps(request))
    monkeypatch.setenv("VONAVY_DATA_BUCKET", "data-bucket")
    monkeypatch.setattr(aws_batch.boto3, "client", lambda *_args, **_kwargs: client)

    assert aws_batch.run() == 0

    result = json.loads(client.put_requests[0]["Body"])
    assert result["status"] == "succeeded"
    assert result["row_count"] == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["result_version_id"] == "result-version"


def test_batch_runner_treats_invalid_dataset_as_completed(monkeypatch) -> None:
    content = b"date,value\n"
    client = FakeS3(content)
    request = _request(content)
    monkeypatch.setenv("VONAVY_VALIDATION_REQUEST_JSON", json.dumps(request))
    monkeypatch.setenv("VONAVY_DATA_BUCKET", "data-bucket")
    monkeypatch.setattr(aws_batch.boto3, "client", lambda *_args, **_kwargs: client)

    assert aws_batch.run() == 0
    result = json.loads(client.put_requests[0]["Body"])
    assert result["status"] == "invalid"
    assert result["validation_errors"][0]["code"] == "empty_dataset"


def test_batch_runner_rejects_cross_owner_key(monkeypatch) -> None:
    content = b"date,value\n2026-01-01,1\n"
    client = FakeS3(content)
    request = _request(content)
    request["input"]["key"] = "datasets/users/other/dataset/upload/file.csv"
    monkeypatch.setenv("VONAVY_VALIDATION_REQUEST_JSON", json.dumps(request))
    monkeypatch.setenv("VONAVY_DATA_BUCKET", "data-bucket")
    monkeypatch.setattr(aws_batch.boto3, "client", lambda *_args, **_kwargs: client)

    assert aws_batch.run() == 1
    assert client.get_requests == []
    assert client.put_requests == []
