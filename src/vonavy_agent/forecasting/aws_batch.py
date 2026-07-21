from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import boto3  # type: ignore[import-untyped]
import pandas as pd
from botocore.config import Config  # type: ignore[import-untyped]
from pydantic import ValidationError

from vonavy_agent.forecasting.contracts import (
    AdapterIdentity,
    ForecastIssue,
    ForecastProfile,
    ForecastRequest,
    ForecastResult,
    ForecastStatus,
    ForecastTiming,
    HoldoutMetrics,
    InputIdentity,
)
from vonavy_agent.forecasting.model import run_xgboost_forecast, sha256_file
from vonavy_agent.forecasting.neural_net import run_neuralnet_forecast

RESULT_MAX_BYTES = 2 * 1024 * 1024
S3_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"})


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _validate_scope(request: ForecastRequest) -> None:
    expected_input = f"datasets/users/{request.owner_id}/"
    expected_output = (
        f"forecast-results/users/{request.owner_id}/datasets/"
        f"{request.dataset_id}/runs/{request.run_id}/"
    )
    if not request.input.key.startswith(expected_input):
        raise ValueError("input key is outside the owner dataset prefix")
    if request.output.prefix != expected_output:
        raise ValueError("output prefix is not the canonical owner/run prefix")
    configured_bucket = _required_environment("VONAVY_DATA_BUCKET")
    if request.input.bucket != configured_bucket or request.output.bucket != configured_bucket:
        raise ValueError("request bucket does not match the deployed data bucket")


def _download(s3: Any, request: ForecastRequest, destination: Path) -> str:
    response = s3.get_object(
        Bucket=request.input.bucket,
        Key=request.input.key,
        VersionId=request.input.version_id,
    )
    content_length = int(response.get("ContentLength", 0))
    if content_length != request.input.byte_size or content_length > request.limits.max_bytes:
        raise ValueError("input object size does not match the immutable request")
    digest = hashlib.sha256()
    written = 0
    with destination.open("wb") as handle:
        body = response["Body"]
        while chunk := body.read(1024 * 1024):
            written += len(chunk)
            if written > request.limits.max_bytes:
                raise ValueError("input object exceeded max_bytes while streaming")
            digest.update(chunk)
            handle.write(chunk)
    actual_sha256 = digest.hexdigest()
    if written != request.input.byte_size:
        raise ValueError("input object bytes do not match the immutable request")
    if request.input.sha256 is not None and actual_sha256 != request.input.sha256:
        raise ValueError("input SHA-256 does not match the immutable request")
    return actual_sha256


def _load(path: Path, media_type: str) -> pd.DataFrame:
    if media_type == "text/csv":
        return pd.read_csv(path)
    if media_type == "application/vnd.apache.parquet":
        return pd.read_parquet(path)
    raise ValueError("unsupported media type")


def _upload(
    s3: Any, *, bucket: str, key: str, path: Path, content_type: str
) -> tuple[str, str, int]:
    response = s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=path.read_bytes(),
        ContentType=content_type,
        ServerSideEncryption="AES256",
        Tagging=urlencode({"retention": "demo", "artifact": "forecast"}),
    )
    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or not version_id:
        raise RuntimeError("versioned forecast artifact write returned no VersionId")
    return version_id, sha256_file(path), path.stat().st_size


def _invalid_result(
    request: ForecastRequest,
    *,
    input_sha256: str,
    message: str,
    started_at: datetime,
) -> ForecastResult:
    training_end = request.training_end
    return ForecastResult(
        status=ForecastStatus.INVALID,
        adapter=AdapterIdentity(id=request.adapter_id),
        owner_id=request.owner_id,
        dataset_id=request.dataset_id,
        run_id=request.run_id,
        input=InputIdentity(
            bucket=request.input.bucket,
            key=request.input.key,
            version_id=request.input.version_id,
            sha256=input_sha256,
        ),
        profile=ForecastProfile(
            rows=0,
            entities=0,
            history_start=None,
            training_end=training_end,
            forecast_start=training_end + pd.Timedelta(days=1),
            forecast_end=training_end + pd.Timedelta(days=7),
            trainable_rows=0,
            fallback_rows=0,
        ),
        holdout=HoldoutMetrics(
            supported=False,
            rows=0,
            coverage=0.0,
            unsupported_reason="Dataset or forecast mapping is invalid",
        ),
        artifacts=None,
        failure=ForecastIssue(code="forecast_invalid", message=message[:500]),
        timing=ForecastTiming(
            prepare_seconds=0.0,
            holdout_seconds=0.0,
            fit_seconds=0.0,
            forecast_seconds=0.0,
            total_seconds=max((datetime.now(UTC) - started_at).total_seconds(), 0.0),
        ),
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def _publish_result(
    s3: Any, request: ForecastRequest, result: ForecastResult, directory: Path
) -> str:
    result_path = directory / "result.json"
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    if result_path.stat().st_size > RESULT_MAX_BYTES:
        raise ValueError("forecast result exceeds output policy")
    version, _, _ = _upload(
        s3,
        bucket=request.output.bucket,
        key=request.output.prefix + "result.json",
        path=result_path,
        content_type="application/json",
    )
    return version


def main() -> None:
    request = ForecastRequest.model_validate_json(
        _required_environment("VONAVY_FORECAST_REQUEST_JSON")
    )
    _validate_scope(request)
    started_at = datetime.now(UTC)
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION_NAME"), config=S3_CONFIG)
    with tempfile.TemporaryDirectory(prefix="vonavy-forecast-") as temp:
        root = Path(temp)
        suffix = ".csv" if request.input.media_type == "text/csv" else ".parquet"
        input_path = root / f"input{suffix}"
        output_directory = root / "output"
        output_directory.mkdir(parents=True, exist_ok=True)
        actual_sha256 = request.input.sha256 or "0" * 64
        try:
            actual_sha256 = _download(s3, request, input_path)
            raw = _load(input_path, request.input.media_type)
            runner = (
                run_neuralnet_forecast
                if request.adapter_id == "neuralnet-direct-v1"
                else run_xgboost_forecast
            )
            output = runner(
                raw=raw,
                mapping=request.mapping,
                training_end=pd.Timestamp(request.training_end),
                output_directory=output_directory,
                owner_id=request.owner_id,
                dataset_id=request.dataset_id,
                run_id=request.run_id,
                input_identity=InputIdentity(
                    bucket=request.input.bucket,
                    key=request.input.key,
                    version_id=request.input.version_id,
                    sha256=actual_sha256,
                ),
                source_revision=request.source_revision,
                max_rows=request.limits.max_rows,
                max_entities=request.limits.max_entities,
                max_history_days=request.limits.max_history_days,
            )
            artifacts = output.result.artifacts
            if artifacts is None:
                raise RuntimeError("successful forecast produced no artifacts")
            uploads = (
                (
                    "forecast",
                    output.forecast_path,
                    "forecast.parquet",
                    "application/vnd.apache.parquet",
                ),
                ("model", output.model_path, output.model_path.name, "application/octet-stream"),
                ("manifest", output.manifest_path, "model-manifest.json", "application/json"),
            )
            for name, path, filename, content_type in uploads:
                key = request.output.prefix + filename
                version, digest, byte_size = _upload(
                    s3,
                    bucket=request.output.bucket,
                    key=key,
                    path=path,
                    content_type=content_type,
                )
                reference = getattr(artifacts, name)
                reference.key = key
                reference.version_id = version
                reference.sha256 = digest
                reference.byte_size = byte_size
            result = output.result
            event = "forecast_published"
        except (ValueError, ValidationError) as exc:
            result = _invalid_result(
                request,
                input_sha256=actual_sha256,
                message=str(exc),
                started_at=started_at,
            )
            event = "forecast_invalid"
        result_version = _publish_result(s3, request, result, output_directory)
        print(
            json.dumps(
                {"event": event, "run_id": request.run_id, "result_version_id": result_version}
            )
        )


if __name__ == "__main__":
    main()
