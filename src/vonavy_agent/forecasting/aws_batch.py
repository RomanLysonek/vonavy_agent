from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import boto3  # type: ignore[import-untyped]
import pandas as pd
from botocore.config import Config  # type: ignore[import-untyped]
from pydantic import ValidationError

from vonavy_agent.forecasting.chronos2 import run_chronos2_forecast
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
SUPPORTED_ADAPTERS = (
    "xgboost-direct-v1",
    "neuralnet-direct-v1",
    "chronos2-zero-shot-v1",
)
RUNNERS = {
    "xgboost-direct-v1": run_xgboost_forecast,
    "neuralnet-direct-v1": run_neuralnet_forecast,
    "chronos2-zero-shot-v1": run_chronos2_forecast,
}


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


def _failed_result(
    request: ForecastRequest,
    *,
    input_sha256: str,
    message: str,
    started_at: datetime,
) -> ForecastResult:
    invalid = _invalid_result(
        request,
        input_sha256=input_sha256,
        message=message,
        started_at=started_at,
    )
    return invalid.model_copy(
        update={
            "status": ForecastStatus.FAILED,
            "failure": ForecastIssue(code="forecast_failed", message=message[:500]),
        }
    )


def _publish_payload(
    s3: Any, request: ForecastRequest, payload: dict[str, Any], directory: Path
) -> str:
    result_path = directory / "result.json"
    result_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
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


def _publish_result(
    s3: Any, request: ForecastRequest, result: ForecastResult, directory: Path
) -> str:
    return _publish_payload(s3, request, result.model_dump(mode="json"), directory)


def _child_request(
    request: ForecastRequest,
    *,
    adapter_id: str,
    child_run_id: str,
) -> ForecastRequest:
    prefix = request.output.prefix + f"models/{adapter_id}/"
    return request.model_copy(
        update={
            "run_id": child_run_id,
            "adapter_id": adapter_id,
            "output": request.output.model_copy(update={"prefix": prefix}),
        }
    )


def _validate_comparison_request(
    raw: dict[str, Any],
) -> tuple[ForecastRequest, tuple[dict[str, str], ...]]:
    if raw.get("schema_version") != "forecast-comparison-request/v1":
        raise ValueError("unsupported forecast request schema")
    adapter_ids = raw.get("adapter_ids")
    child_runs = raw.get("child_runs")
    if (
        not isinstance(adapter_ids, list)
        or not 2 <= len(adapter_ids) <= len(SUPPORTED_ADAPTERS)
        or any(adapter not in SUPPORTED_ADAPTERS for adapter in adapter_ids)
        or len(set(adapter_ids)) != len(adapter_ids)
    ):
        raise ValueError(
            "comparison adapter_ids must contain two or three unique supported adapters"
        )
    if not isinstance(child_runs, list) or len(child_runs) != len(adapter_ids):
        raise ValueError("comparison child_runs do not match adapter_ids")
    normalized: list[dict[str, str]] = []
    for index, child in enumerate(child_runs):
        if not isinstance(child, dict) or child.get("adapter_id") != adapter_ids[index]:
            raise ValueError("comparison child run order does not match adapter_ids")
        run_id = child.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("comparison child run id is invalid")
        try:
            parsed = uuid.UUID(run_id)
        except ValueError as exc:
            raise ValueError("comparison child run id is invalid") from exc
        if str(parsed) != run_id:
            raise ValueError("comparison child run id is not canonical")
        normalized.append({"adapter_id": adapter_ids[index], "run_id": run_id})
    base = dict(raw)
    base["schema_version"] = "forecast-request/v1"
    base["adapter_id"] = adapter_ids[0]
    base.pop("adapter_ids", None)
    base.pop("child_runs", None)
    request = ForecastRequest.model_validate(base)
    _validate_scope(request)
    return request, tuple(normalized)


def _run_model(
    s3: Any,
    *,
    request: ForecastRequest,
    raw: pd.DataFrame,
    input_sha256: str,
    output_directory: Path,
    started_at: datetime,
) -> ForecastResult:
    output_directory.mkdir(parents=True, exist_ok=True)
    try:
        output = RUNNERS[request.adapter_id](
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
                sha256=input_sha256,
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
        return output.result
    except (ValueError, ValidationError) as exc:
        return _invalid_result(
            request,
            input_sha256=input_sha256,
            message=str(exc),
            started_at=started_at,
        )
    except Exception as exc:
        return _failed_result(
            request,
            input_sha256=input_sha256,
            message=f"{type(exc).__name__}: {exc}",
            started_at=started_at,
        )


def _comparison_status(results: list[ForecastResult]) -> str:
    succeeded = sum(result.status == ForecastStatus.SUCCEEDED for result in results)
    invalid = sum(result.status == ForecastStatus.INVALID for result in results)
    if succeeded == len(results):
        return "succeeded"
    if succeeded:
        return "partial"
    if invalid == len(results):
        return "invalid"
    return "failed"


def _comparison_basis(results: list[ForecastResult]) -> dict[str, Any]:
    successful = [result for result in results if result.status == ForecastStatus.SUCCEEDED]
    if len(successful) < 2:
        return {
            "comparable": False,
            "reason": "At least two successful adapters are required for a ranked comparison.",
            "holdout_origin": None,
            "common_rows": 0,
        }
    signatures: list[tuple[str | None, int, float, int]] = []
    for result in successful:
        evaluation = result.evaluation
        common_rows = (
            int(evaluation.baseline_skill.common_rows)
            if evaluation is not None
            else int(result.holdout.rows)
        )
        signatures.append(
            (
                result.holdout.origin.isoformat() if result.holdout.origin else None,
                int(result.holdout.rows),
                round(float(result.holdout.coverage), 12),
                common_rows,
            )
        )
    if any(not result.holdout.supported or result.holdout.wape is None for result in successful):
        return {
            "comparable": False,
            "reason": "One or more successful adapters lack a supported holdout WAPE.",
            "holdout_origin": None,
            "common_rows": 0,
        }
    if len(set(signatures)) != 1:
        return {
            "comparable": False,
            "reason": "Successful adapters were not evaluated on the same holdout contract.",
            "holdout_origin": None,
            "common_rows": 0,
        }
    origin, _, _, common_rows = signatures[0]
    return {
        "comparable": True,
        "reason": None,
        "holdout_origin": origin,
        "common_rows": common_rows,
    }


def _comparison_leaderboard(
    results: list[ForecastResult], *, comparable: bool
) -> list[dict[str, Any]]:
    def order(result: ForecastResult) -> tuple[int, float, str]:
        if result.status != ForecastStatus.SUCCEEDED:
            return (2, float("inf"), result.adapter.id)
        if not comparable or result.holdout.wape is None:
            return (1, float("inf"), result.adapter.id)
        return (0, float(result.holdout.wape), result.adapter.id)

    ordered = sorted(results, key=order)
    leaderboard: list[dict[str, Any]] = []
    successful_rank = 0
    for result in ordered:
        ranked = comparable and result.status == ForecastStatus.SUCCEEDED
        if ranked:
            successful_rank += 1
        leaderboard.append(
            {
                "rank": successful_rank if ranked else None,
                "adapter_id": result.adapter.id,
                "run_id": result.run_id,
                "status": result.status.value,
                "holdout_origin": (
                    result.holdout.origin.isoformat() if result.holdout.origin else None
                ),
                "holdout_rows": result.holdout.rows,
                "holdout_coverage": result.holdout.coverage,
                "holdout_wape": result.holdout.wape,
                "forecast_rows": result.profile.entities * 7,
                "fallback_rows": result.profile.fallback_rows,
                "failure": result.failure.model_dump(mode="json") if result.failure else None,
            }
        )
    return leaderboard


def _comparison_payload(
    *,
    request: ForecastRequest,
    children: tuple[dict[str, str], ...],
    results: list[ForecastResult],
    input_sha256: str,
    started_at: datetime,
) -> dict[str, Any]:
    basis = _comparison_basis(results)
    leaderboard = _comparison_leaderboard(results, comparable=bool(basis["comparable"]))
    succeeded = sum(result.status == ForecastStatus.SUCCEEDED for result in results)
    failed = len(results) - succeeded
    best_adapter_id = next(
        (
            item["adapter_id"]
            for item in leaderboard
            if item["status"] == "succeeded" and item["rank"] == 1
        ),
        None,
    )
    return {
        "schema_version": "forecast-comparison-result/v1",
        "status": _comparison_status(results),
        "owner_id": request.owner_id,
        "dataset_id": request.dataset_id,
        "run_id": request.run_id,
        "input": {
            "bucket": request.input.bucket,
            "key": request.input.key,
            "version_id": request.input.version_id,
            "sha256": input_sha256,
        },
        "adapter_ids": [child["adapter_id"] for child in children],
        "child_runs": list(children),
        "models": [result.model_dump(mode="json") for result in results],
        "leaderboard": leaderboard,
        "summary": {
            "requested_models": len(results),
            "succeeded_models": succeeded,
            "failed_models": failed,
            "forecast_rows": sum(result.profile.entities * 7 for result in results),
            "fallback_rows": sum(result.profile.fallback_rows for result in results),
            "best_adapter_id": best_adapter_id,
            "leaderboard_comparable": basis["comparable"],
            "comparison_reason": basis["reason"],
            "holdout_origin": basis["holdout_origin"],
            "common_rows": basis["common_rows"],
        },
        "source_revision": request.source_revision,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
    }


def _single_main(raw_request: str) -> None:
    request = ForecastRequest.model_validate_json(raw_request)
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
            result = _run_model(
                s3,
                request=request,
                raw=raw,
                input_sha256=actual_sha256,
                output_directory=output_directory,
                started_at=started_at,
            )
        except (ValueError, ValidationError) as exc:
            result = _invalid_result(
                request,
                input_sha256=actual_sha256,
                message=str(exc),
                started_at=started_at,
            )
        result_version = _publish_result(s3, request, result, output_directory)
        event = (
            "forecast_published"
            if result.status == ForecastStatus.SUCCEEDED
            else "forecast_invalid"
        )
        print(
            json.dumps(
                {
                    "event": event,
                    "run_id": request.run_id,
                    "result_version_id": result_version,
                }
            )
        )


def _comparison_main(raw: dict[str, Any]) -> None:
    request, children = _validate_comparison_request(raw)
    started_at = datetime.now(UTC)
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION_NAME"), config=S3_CONFIG)
    with tempfile.TemporaryDirectory(prefix="vonavy-comparison-") as temp:
        root = Path(temp)
        suffix = ".csv" if request.input.media_type == "text/csv" else ".parquet"
        input_path = root / f"input{suffix}"
        aggregate_directory = root / "aggregate"
        aggregate_directory.mkdir(parents=True, exist_ok=True)
        actual_sha256 = request.input.sha256 or "0" * 64
        results: list[ForecastResult] = []
        try:
            actual_sha256 = _download(s3, request, input_path)
            frame = _load(input_path, request.input.media_type)
            for child in children:
                child_request = _child_request(
                    request,
                    adapter_id=child["adapter_id"],
                    child_run_id=child["run_id"],
                )
                results.append(
                    _run_model(
                        s3,
                        request=child_request,
                        raw=frame.copy(deep=True),
                        input_sha256=actual_sha256,
                        output_directory=root / child["adapter_id"],
                        started_at=datetime.now(UTC),
                    )
                )
        except (ValueError, ValidationError) as exc:
            completed = {result.adapter.id for result in results}
            for child in children:
                if child["adapter_id"] in completed:
                    continue
                child_request = _child_request(
                    request,
                    adapter_id=child["adapter_id"],
                    child_run_id=child["run_id"],
                )
                results.append(
                    _invalid_result(
                        child_request,
                        input_sha256=actual_sha256,
                        message=str(exc),
                        started_at=started_at,
                    )
                )
        except Exception as exc:
            completed = {result.adapter.id for result in results}
            for child in children:
                if child["adapter_id"] in completed:
                    continue
                child_request = _child_request(
                    request,
                    adapter_id=child["adapter_id"],
                    child_run_id=child["run_id"],
                )
                results.append(
                    _failed_result(
                        child_request,
                        input_sha256=actual_sha256,
                        message=f"{type(exc).__name__}: {exc}",
                        started_at=started_at,
                    )
                )
        payload = _comparison_payload(
            request=request,
            children=children,
            results=results,
            input_sha256=actual_sha256,
            started_at=started_at,
        )
        result_version = _publish_payload(s3, request, payload, aggregate_directory)
        print(
            json.dumps(
                {
                    "event": "forecast_comparison_published",
                    "run_id": request.run_id,
                    "status": payload["status"],
                    "result_version_id": result_version,
                }
            )
        )


def main() -> None:
    raw_request = _required_environment("VONAVY_FORECAST_REQUEST_JSON")
    try:
        parsed = json.loads(raw_request)
    except json.JSONDecodeError:
        parsed = None
    if (
        isinstance(parsed, dict)
        and parsed.get("schema_version") == "forecast-comparison-request/v1"
    ):
        _comparison_main(parsed)
    else:
        _single_main(raw_request)


if __name__ == "__main__":
    main()
