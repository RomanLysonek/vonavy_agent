from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]

from vonavy_agent.hashing import canonical_json
from vonavy_agent.validation_contracts import (
    S3InputArtifact,
    S3OutputArtifact,
    ValidationRequest,
    ValidationStatus,
)
from vonavy_agent.validation_worker.aws_artifacts import (
    S3FileArtifactReader,
    S3FileArtifactWriter,
)
from vonavy_agent.validation_worker.worker import validate_request

OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


def _environment_request() -> ValidationRequest:
    raw = os.environ.get("VONAVY_VALIDATION_REQUEST_JSON")
    if not raw:
        raise ValueError("VONAVY_VALIDATION_REQUEST_JSON is required")
    request = ValidationRequest.model_validate_json(raw)
    if not isinstance(request.input, S3InputArtifact):
        raise ValueError("Batch validation requires an S3 input artifact")
    if not isinstance(request.output, S3OutputArtifact):
        raise ValueError("Batch validation requires an S3 output artifact")
    if not OWNER_PATTERN.fullmatch(request.owner_id):
        raise ValueError("Validation owner identifier is invalid")
    return request


def _validate_boundaries(request: ValidationRequest, data_bucket: str) -> tuple[str, str]:
    input_artifact = request.input
    output_artifact = request.output
    assert isinstance(input_artifact, S3InputArtifact)
    assert isinstance(output_artifact, S3OutputArtifact)

    input_prefix = f"datasets/users/{request.owner_id}/{request.dataset_id}/"
    output_prefix = (
        f"validation-results/users/{request.owner_id}/datasets/{request.dataset_id}/"
        f"jobs/{request.job_id}/"
    )
    expected_output_key = f"{output_prefix}result.json"
    if input_artifact.bucket != data_bucket or not input_artifact.key.startswith(input_prefix):
        raise ValueError("Validation input is outside the immutable owner dataset boundary")
    if output_artifact.bucket != data_bucket or output_artifact.key != expected_output_key:
        raise ValueError("Validation output is outside the exact owner job boundary")
    return input_prefix, output_prefix


def run() -> int:
    try:
        request = _environment_request()
        data_bucket = os.environ["VONAVY_DATA_BUCKET"]
        input_prefix, output_prefix = _validate_boundaries(request, data_bucket)
        region = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION")
        s3 = boto3.client("s3", region_name=region)
        reader = S3FileArtifactReader(
            s3,
            allowed_bucket=data_bucket,
            allowed_key_prefix=input_prefix,
            max_bytes=request.limits.max_input_bytes,
            temporary_root=Path(os.environ.get("VONAVY_TEMP_ROOT", "/tmp")),
        )
        writer = S3FileArtifactWriter(
            s3,
            allowed_bucket=data_bucket,
            allowed_key_prefix=output_prefix,
        )
        result = validate_request(request, reader)
        payload = (canonical_json(result.model_dump(mode="json")) + "\n").encode("utf-8")
        assert isinstance(request.output, S3OutputArtifact)
        receipt = writer.write_bytes(request.output, payload)
        print(
            canonical_json(
                {
                    "job_id": request.job_id,
                    "dataset_id": request.dataset_id,
                    "status": result.status.value,
                    "result_version_id": receipt.version_id,
                }
            )
        )
        return 1 if result.status == ValidationStatus.FAILED else 0
    except (KeyError, ValueError, OSError) as exc:
        summary: dict[str, Any] = {
            "status": "failed",
            "code": "batch_worker_failure",
            "error_type": type(exc).__name__,
        }
        print(canonical_json(summary))
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
