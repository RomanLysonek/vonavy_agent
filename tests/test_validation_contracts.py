from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vonavy_agent.validation_contracts import ValidationRequest


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": "validation-request/v1",
        "job_id": "job-1",
        "owner_id": "owner-1",
        "dataset_id": "dataset-1",
        "input": {
            "storage": "local",
            "path": "input/data.csv",
            "media_type": "text/csv",
        },
        "output": {"storage": "local", "path": "output/result.json"},
        "requested_at": datetime(2026, 7, 20, tzinfo=UTC).isoformat(),
    }


def test_validation_request_v1_is_strict_and_versioned() -> None:
    request = ValidationRequest.model_validate(valid_payload())
    assert request.schema_version == "validation-request/v1"
    assert request.input.path == "input/data.csv"

    unknown_version = valid_payload()
    unknown_version["schema_version"] = "validation-request/v2"
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(unknown_version)

    unknown_field = valid_payload()
    unknown_field["future"] = True
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(unknown_field)


def test_validation_request_rejects_unsafe_paths_and_naive_time() -> None:
    traversal = valid_payload()
    traversal["input"] = {
        "storage": "local",
        "path": "../secret.csv",
        "media_type": "text/csv",
    }
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(traversal)

    absolute = valid_payload()
    absolute["output"] = {"storage": "local", "path": "/tmp/result.json"}
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(absolute)

    naive = valid_payload()
    naive["requested_at"] = "2026-07-20T12:00:00"
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(naive)


def test_validation_request_v1_describes_immutable_s3_artifacts() -> None:
    payload = valid_payload()
    payload["input"] = {
        "storage": "s3",
        "bucket": "vonavy-data-bucket",
        "key": "datasets/owner/dataset/input.parquet",
        "version_id": "immutable-version",
        "media_type": "application/vnd.apache.parquet",
        "expected_size_bytes": 123,
        "expected_sha256": "a" * 64,
    }
    payload["output"] = {
        "storage": "s3",
        "bucket": "vonavy-results-bucket",
        "key": "validation/job/result.json",
    }
    request = ValidationRequest.model_validate(payload)
    assert request.input.storage == "s3"
    assert request.output.storage == "s3"

    missing_version = valid_payload()
    missing_version["input"] = {
        "storage": "s3",
        "bucket": "vonavy-data-bucket",
        "key": "datasets/input.parquet",
        "media_type": "application/vnd.apache.parquet",
    }
    with pytest.raises(ValidationError):
        ValidationRequest.model_validate(missing_version)
