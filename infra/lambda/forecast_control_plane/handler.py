from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import PurePath
from typing import Any

import boto3  # type: ignore[import-untyped]
from agent import AgentPlanError, build_forecast_agent_plan
from boto3.dynamodb.types import TypeSerializer  # type: ignore[import-untyped]
from botocore.config import Config
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

DATA_BUCKET = os.environ["DATA_BUCKET"]
METADATA_TABLE = os.environ["METADATA_TABLE"]
FORECAST_JOB_QUEUE = os.environ["FORECAST_JOB_QUEUE"]
FORECAST_JOB_DEFINITION = os.environ["FORECAST_JOB_DEFINITION"]
FORECAST_JOB_TIMEOUT_SECONDS = int(os.environ.get("FORECAST_JOB_TIMEOUT_SECONDS", "3600"))
UPLOAD_RETENTION_DAYS = int(os.environ.get("UPLOAD_RETENTION_DAYS", "7"))
SOURCE_REVISION = os.environ.get("SOURCE_REVISION", "unknown")
AWS_REGION_NAME = os.environ.get("AWS_REGION_NAME")

MAX_BODY_BYTES = 32 * 1024
MAX_VALIDATION_RESULT_BYTES = 4 * 1024 * 1024
AGENT_DAILY_LIMIT = int(os.environ.get("AGENT_DAILY_LIMIT", "20"))
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 500_000_000
SLOT_LEASE_SECONDS = FORECAST_JOB_TIMEOUT_SECONDS + 900
ACTIVE_BATCH = {"SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"}
TERMINAL = {"succeeded", "invalid", "failed"}
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
COLUMN_PATTERN = re.compile(r"^.{1,128}$", re.DOTALL)
SERIALIZER = TypeSerializer()
BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"})

_S3 = None
_TABLE = None
_DDB = None
_BATCH = None


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


def _clients():
    global _S3, _TABLE, _DDB, _BATCH
    if _S3 is None:
        _S3 = boto3.client("s3", region_name=AWS_REGION_NAME, config=BOTO_CONFIG)
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION_NAME, config=BOTO_CONFIG)
        _TABLE = dynamodb.Table(METADATA_TABLE)
        _DDB = boto3.client("dynamodb", region_name=AWS_REGION_NAME, config=BOTO_CONFIG)
        _BATCH = boto3.client("batch", region_name=AWS_REGION_NAME, config=BOTO_CONFIG)
    return _S3, _TABLE, _DDB, _BATCH


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ),
    }


def _identity(event: dict[str, Any]) -> str:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    owner = claims.get("sub")
    if not isinstance(owner, str) or not OWNER_PATTERN.fullmatch(owner):
        raise ApiError("unauthorized", "Authenticated owner identity is unavailable", 401)
    return owner


def _parse_json_body(event: dict[str, Any], maximum_bytes: int) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError("invalid_json", "Request body is not valid JSON", 400) from exc
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum_bytes:
        raise ApiError("request_too_large", "Request body exceeds the server limit", 413)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("invalid_json", "Request body is not valid JSON", 400) from exc
    if not isinstance(payload, dict):
        raise ApiError("invalid_request", "Request body must be a JSON object", 400)
    return payload


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    return _parse_json_body(event, MAX_BODY_BYTES)


def _canonical_uuid(value: object, *, code: str, message: str, status: int) -> str:
    if not isinstance(value, str):
        raise ApiError(code, message, status)
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ApiError(code, message, status) from exc
    if str(parsed) != value:
        raise ApiError(code, message, status)
    return value


def _column(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not COLUMN_PATTERN.fullmatch(value):
        raise ApiError(
            "invalid_forecast_mapping",
            "Mapped column names must contain 1-128 characters",
            422,
        )
    return value


def _columns(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise ApiError("invalid_forecast_mapping", "Column role values must be arrays", 422)
    result = [_column(item, required=True) for item in value]
    clean = [str(item) for item in result]
    if len(clean) != len(set(clean)):
        raise ApiError(
            "invalid_forecast_mapping",
            "Column role arrays must not contain duplicates",
            422,
        )
    return clean


def _mapping(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("mapping")
    if not isinstance(value, dict):
        raise ApiError("invalid_forecast_mapping", "mapping is required", 422)
    mapping = {
        "timestamp_column": _column(value.get("timestampColumn"), required=True),
        "target_column": _column(value.get("targetColumn"), required=True),
        "entity_column": _column(value.get("entityColumn")),
        "availability_column": _column(value.get("availabilityColumn")),
        "known_future_numeric": _columns(value.get("knownFutureNumeric")),
        "known_future_categorical": _columns(value.get("knownFutureCategorical")),
        "static_numeric": _columns(value.get("staticNumeric")),
        "static_categorical": _columns(value.get("staticCategorical")),
        "excluded": _columns(value.get("excluded")),
    }
    flattened = [
        mapping["timestamp_column"],
        mapping["target_column"],
        mapping["entity_column"],
        mapping["availability_column"],
        *mapping["known_future_numeric"],
        *mapping["known_future_categorical"],
        *mapping["static_numeric"],
        *mapping["static_categorical"],
        *mapping["excluded"],
    ]
    present = [item for item in flattened if item is not None]
    if len(present) != len(set(present)):
        raise ApiError(
            "invalid_forecast_mapping",
            "Each mapped column may have only one role",
            422,
        )
    return mapping


def _training_end(payload: dict[str, Any]) -> str:
    value = payload.get("trainingEnd")
    if not isinstance(value, str):
        raise ApiError("invalid_training_end", "trainingEnd must be an ISO date", 422)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError("invalid_training_end", "trainingEnd must be an ISO date", 422) from exc
    if parsed > datetime.now(UTC).date() + timedelta(days=366):
        raise ApiError("invalid_training_end", "trainingEnd is outside the supported range", 422)
    return parsed.isoformat()


def _media_type(filename: str) -> str:
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".parquet":
        return "application/vnd.apache.parquet"
    raise ApiError("unsupported_file_type", "Only CSV and Parquet datasets can be forecast", 415)


def _dataset(table: Any, owner: str, dataset_id: str) -> dict[str, Any]:
    item = table.get_item(
        Key={"pk": f"USER#{owner}", "sk": f"DATASET#{dataset_id}"},
        ConsistentRead=True,
    ).get("Item")
    if not isinstance(item, dict) or item.get("owner_sub") != owner:
        raise ApiError("dataset_not_found", "Dataset does not exist", 404)
    if item.get("status") != "uploaded":
        raise ApiError("dataset_not_ready", "Dataset upload is not complete", 409)
    required = ("object_key", "object_version_id", "filename", "actual_size")
    if any(item.get(name) in (None, "") for name in required):
        raise ApiError("dataset_state_invalid", "Dataset storage metadata is incomplete", 500)
    if int(item["actual_size"]) > MAX_INPUT_BYTES:
        raise ApiError(
            "forecast_policy_exceeded",
            "Dataset is larger than the forecast-worker policy",
            422,
        )
    return item


def _item(table: Any, owner: str, run_id: str) -> dict[str, Any] | None:
    item = table.get_item(
        Key={"pk": f"USER#{owner}", "sk": f"FORECAST#{run_id}"},
        ConsistentRead=True,
    ).get("Item")
    if not isinstance(item, dict) or item.get("owner_sub") != owner:
        return None
    return item


def _fingerprint(dataset_id: str, mapping: dict[str, Any], training_end: str) -> str:
    encoded = json.dumps(
        {"dataset_id": dataset_id, "mapping": mapping, "training_end": training_end},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _serialize(value: Any) -> dict[str, Any]:
    return SERIALIZER.serialize(value)


def _expression(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: _serialize(value) for name, value in values.items()}


def _key(pk: str, sk: str) -> dict[str, dict[str, str]]:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _ddb_item(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: _serialize(value) for name, value in values.items()}


def _links(run_id: str) -> dict[str, str]:
    return {
        "status": f"/api/forecasts/{run_id}",
        "result": f"/api/forecasts/{run_id}/result",
    }


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "forecastRunId": item["run_id"],
        "datasetId": item["dataset_id"],
        "status": item["status"],
        "createdAt": item["created_at"],
        "updatedAt": item["updated_at"],
        "resultAvailable": bool(item.get("result_version_id")),
        "links": _links(item["run_id"]),
    }
    if item.get("batch_job_id"):
        payload["batchJobId"] = item["batch_job_id"]
    if item.get("failure_code"):
        payload["failure"] = {
            "code": item["failure_code"],
            "message": item.get("failure_message", "Forecast run failed"),
        }
    if item.get("holdout_wape") is not None:
        value = float(item["holdout_wape"])
        payload["summary"] = {
            "holdoutWape": None if value < 0 else value,
            "forecastRows": int(item.get("forecast_rows", 0)),
            "fallbackRows": int(item.get("fallback_rows", 0)),
        }
    return payload


def _request_document(
    *,
    owner: str,
    dataset_id: str,
    dataset: dict[str, Any],
    run_id: str,
    mapping: dict[str, Any],
    training_end: str,
    requested_at: str,
) -> dict[str, Any]:
    prefix = f"forecast-results/users/{owner}/datasets/{dataset_id}/runs/{run_id}/"
    return {
        "schema_version": "forecast-request/v1",
        "owner_id": owner,
        "dataset_id": dataset_id,
        "run_id": run_id,
        "input": {
            "bucket": DATA_BUCKET,
            "key": dataset["object_key"],
            "version_id": dataset["object_version_id"],
            "sha256": None,
            "media_type": _media_type(str(dataset["filename"])),
            "byte_size": int(dataset["actual_size"]),
        },
        "output": {"bucket": DATA_BUCKET, "prefix": prefix},
        "mapping": mapping,
        "training_end": training_end,
        "horizon_days": 7,
        "adapter_id": "xgboost-direct-v1",
        "seed": 42,
        "limits": {
            "max_bytes": MAX_INPUT_BYTES,
            "max_rows": 2_000_000,
            "max_entities": 20_000,
            "max_history_days": 3_000,
            "threads": 1,
        },
        "source_revision": SOURCE_REVISION,
        "requested_at": requested_at,
    }


def _release_failure(ddb: Any, owner: str, run_id: str, code: str, message: str) -> None:
    now = datetime.now(UTC).isoformat()
    expires = int(time.time()) + UPLOAD_RETENTION_DAYS * 86400
    try:
        ddb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(f"USER#{owner}", f"FORECAST#{run_id}"),
                        "UpdateExpression": (
                            "SET #s=:failed, updated_at=:now, failure_code=:code, "
                            "failure_message=:message, expires_at=:expires"
                        ),
                        "ConditionExpression": "owner_sub=:owner AND run_id=:run",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": _expression(
                            {
                                ":failed": "failed",
                                ":now": now,
                                ":code": code,
                                ":message": message,
                                ":expires": expires,
                                ":owner": owner,
                                ":run": run_id,
                            }
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(f"USER#{owner}", "FORECAST_SLOT#0000"),
                        "UpdateExpression": "SET #s=:released, updated_at=:now, expires_at=:expires",
                        "ConditionExpression": "owner_sub=:owner AND run_id=:run",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": _expression(
                            {
                                ":released": "released",
                                ":now": now,
                                ":expires": expires,
                                ":owner": owner,
                                ":run": run_id,
                            }
                        ),
                    }
                },
            ]
        )
    except ClientError:
        LOGGER.exception("could not release failed forecast", extra={"run_id": run_id})


def _create(event: dict[str, Any], dataset_id: str) -> tuple[int, dict[str, Any]]:
    owner = _identity(event)
    dataset_id = _canonical_uuid(
        dataset_id,
        code="dataset_not_found",
        message="Dataset does not exist",
        status=404,
    )
    body = _parse_body(event)
    token = _canonical_uuid(
        body.get("requestToken"),
        code="invalid_request_token",
        message="requestToken must be a canonical UUID",
        status=422,
    )
    mapping = _mapping(body)
    training_end = _training_end(body)
    _, table, ddb, batch = _clients()
    dataset = _dataset(table, owner, dataset_id)
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonavy-forecast:{owner}:{token}"))
    fingerprint = _fingerprint(dataset_id, mapping, training_end)
    existing = _item(table, owner, run_id)
    if existing is not None:
        if (
            existing.get("request_fingerprint") != fingerprint
            or existing.get("request_token") != token
        ):
            raise ApiError("forecast_request_conflict", "requestToken is already in use", 409)
        return 200, _payload(existing)

    created = datetime.now(UTC).isoformat()
    now = int(time.time())
    request = _request_document(
        owner=owner,
        dataset_id=dataset_id,
        dataset=dataset,
        run_id=run_id,
        mapping=mapping,
        training_end=training_end,
        requested_at=created,
    )
    owner_pk = f"USER#{owner}"
    try:
        ddb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, "FORECAST_SLOT#0000"),
                        "UpdateExpression": (
                            "SET entity_type=:entity, owner_sub=:owner, run_id=:run, "
                            "dataset_id=:dataset, #s=:active, updated_at=:created, expires_at=:expires"
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) OR #s=:released OR expires_at<=:now"
                        ),
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": _expression(
                            {
                                ":entity": "FORECAST_SLOT",
                                ":owner": owner,
                                ":run": run_id,
                                ":dataset": dataset_id,
                                ":active": "active",
                                ":released": "released",
                                ":created": created,
                                ":expires": now + SLOT_LEASE_SECONDS,
                                ":now": now,
                            }
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": METADATA_TABLE,
                        "Item": _ddb_item(
                            {
                                "pk": owner_pk,
                                "sk": f"FORECAST#{run_id}",
                                "entity_type": "FORECAST",
                                "owner_sub": owner,
                                "run_id": run_id,
                                "dataset_id": dataset_id,
                                "request_token": token,
                                "request_fingerprint": fingerprint,
                                "status": "submitting",
                                "created_at": created,
                                "updated_at": created,
                                "expires_at": now + UPLOAD_RETENTION_DAYS * 86400,
                                "input_version_id": dataset["object_version_id"],
                                "result_key": request["output"]["prefix"] + "result.json",
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        existing = _item(table, owner, run_id)
        if existing is not None and existing.get("request_fingerprint") == fingerprint:
            return 200, _payload(existing)
        raise ApiError(
            "forecast_capacity_exceeded", "Another forecast is already active", 429
        ) from exc

    try:
        submitted = batch.submit_job(
            jobName=f"forecast-{run_id}",
            jobQueue=FORECAST_JOB_QUEUE,
            jobDefinition=FORECAST_JOB_DEFINITION,
            containerOverrides={
                "environment": [
                    {
                        "name": "VONAVY_FORECAST_REQUEST_JSON",
                        "value": json.dumps(request, sort_keys=True, separators=(",", ":")),
                    }
                ]
            },
        )
        batch_job_id = submitted.get("jobId")
        if not isinstance(batch_job_id, str) or not batch_job_id:
            raise RuntimeError("AWS Batch returned no jobId")
        table.update_item(
            Key={"pk": owner_pk, "sk": f"FORECAST#{run_id}"},
            UpdateExpression="SET #s=:submitted, batch_job_id=:batch, updated_at=:now",
            ConditionExpression="owner_sub=:owner AND run_id=:run",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":submitted": "submitted",
                ":batch": batch_job_id,
                ":now": datetime.now(UTC).isoformat(),
                ":owner": owner,
                ":run": run_id,
            },
        )
    except Exception as exc:
        _release_failure(
            ddb,
            owner,
            run_id,
            "batch_submit_failed",
            "AWS Batch rejected the forecast job",
        )
        LOGGER.exception("forecast submission failed", extra={"run_id": run_id})
        raise ApiError(
            "forecast_submission_failed", "Forecast job could not be submitted", 503
        ) from exc
    current = _item(table, owner, run_id)
    if current is None:
        raise ApiError("forecast_state_invalid", "Forecast metadata disappeared", 500)
    return 202, _payload(current)


def _terminalize(
    ddb: Any,
    owner: str,
    item: dict[str, Any],
    result: dict[str, Any],
    version_id: str,
) -> None:
    status = result.get("status")
    if status not in TERMINAL:
        raise ApiError("forecast_result_invalid", "Forecast result has invalid status", 502)
    identity = result.get("input") if isinstance(result.get("input"), dict) else {}
    if (
        result.get("schema_version") != "forecast-result/v1"
        or result.get("owner_id") != owner
        or result.get("run_id") != item["run_id"]
        or result.get("dataset_id") != item["dataset_id"]
        or identity.get("version_id") != item["input_version_id"]
    ):
        raise ApiError(
            "forecast_result_invalid", "Forecast result identity does not match the run", 502
        )
    now = datetime.now(UTC).isoformat()
    expires = int(time.time()) + UPLOAD_RETENTION_DAYS * 86400
    profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
    holdout = result.get("holdout") if isinstance(result.get("holdout"), dict) else {}
    run_values = {
        ":status": status,
        ":now": now,
        ":version": version_id,
        ":expires": expires,
        ":owner": owner,
        ":run": item["run_id"],
        ":rows": int(profile.get("entities", 0)) * 7,
        ":fallback": int(profile.get("fallback_rows", 0)),
        ":wape": (
            Decimal(str(holdout["wape"])) if holdout.get("wape") is not None else Decimal("-1")
        ),
    }
    slot_values = {
        ":released": "released",
        ":now": now,
        ":expires": expires,
        ":owner": owner,
        ":run": item["run_id"],
    }
    try:
        ddb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(f"USER#{owner}", f"FORECAST#{item['run_id']}"),
                        "UpdateExpression": (
                            "SET #s=:status, updated_at=:now, result_version_id=:version, "
                            "forecast_rows=:rows, fallback_rows=:fallback, holdout_wape=:wape, "
                            "expires_at=:expires"
                        ),
                        "ConditionExpression": "owner_sub=:owner AND run_id=:run",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": _expression(run_values),
                    }
                },
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(f"USER#{owner}", "FORECAST_SLOT#0000"),
                        "UpdateExpression": "SET #s=:released, updated_at=:now, expires_at=:expires",
                        "ConditionExpression": "owner_sub=:owner AND run_id=:run",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": _expression(slot_values),
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise


def _load_result(s3: Any, item: dict[str, Any]) -> tuple[dict[str, Any], str]:
    response = s3.get_object(Bucket=DATA_BUCKET, Key=item["result_key"])
    if int(response.get("ContentLength", 0)) > MAX_RESULT_BYTES:
        raise ApiError("forecast_result_invalid", "Forecast result exceeds the server limit", 502)
    version_id = response.get("VersionId")
    if not isinstance(version_id, str) or not version_id:
        raise ApiError("forecast_result_invalid", "Forecast result has no immutable version", 502)
    body = response["Body"].read(MAX_RESULT_BYTES + 1)
    if len(body) > MAX_RESULT_BYTES:
        raise ApiError("forecast_result_invalid", "Forecast result exceeds the server limit", 502)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError("forecast_result_invalid", "Forecast result is not valid JSON", 502) from exc
    if not isinstance(payload, dict):
        raise ApiError("forecast_result_invalid", "Forecast result must be a JSON object", 502)
    return payload, version_id


def _refresh(owner: str, item: dict[str, Any]) -> dict[str, Any]:
    if item.get("status") in TERMINAL or not item.get("batch_job_id"):
        return item
    s3, table, ddb, batch = _clients()
    response = batch.describe_jobs(jobs=[item["batch_job_id"]])
    jobs = response.get("jobs", [])
    if not jobs:
        _release_failure(
            ddb, owner, item["run_id"], "batch_job_missing", "AWS Batch job disappeared"
        )
        refreshed = _item(table, owner, item["run_id"])
        return refreshed or item
    batch_status = str(jobs[0].get("status", "")).upper()
    if batch_status in ACTIVE_BATCH:
        status = batch_status.casefold()
        table.update_item(
            Key={"pk": f"USER#{owner}", "sk": f"FORECAST#{item['run_id']}"},
            UpdateExpression="SET #s=:status, updated_at=:now",
            ConditionExpression="owner_sub=:owner AND run_id=:run",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":now": datetime.now(UTC).isoformat(),
                ":owner": owner,
                ":run": item["run_id"],
            },
        )
    elif batch_status == "FAILED":
        reason = str(jobs[0].get("statusReason") or "Forecast worker failed")[:500]
        _release_failure(ddb, owner, item["run_id"], "batch_job_failed", reason)
    elif batch_status == "SUCCEEDED":
        try:
            result, version_id = _load_result(s3, item)
            _terminalize(ddb, owner, item, result, version_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in {"NoSuchKey", "NoSuchVersion"}:
                raise
    refreshed = _item(table, owner, item["run_id"])
    return refreshed or item


def _status(event: dict[str, Any], run_id: str) -> dict[str, Any]:
    owner = _identity(event)
    run_id = _canonical_uuid(
        run_id,
        code="forecast_not_found",
        message="Forecast run does not exist",
        status=404,
    )
    _, table, _, _ = _clients()
    item = _item(table, owner, run_id)
    if item is None:
        raise ApiError("forecast_not_found", "Forecast run does not exist", 404)
    return _payload(_refresh(owner, item))


def _result(event: dict[str, Any], run_id: str) -> dict[str, Any]:
    owner = _identity(event)
    run_id = _canonical_uuid(
        run_id,
        code="forecast_not_found",
        message="Forecast run does not exist",
        status=404,
    )
    s3, table, _, _ = _clients()
    item = _item(table, owner, run_id)
    if item is None:
        raise ApiError("forecast_not_found", "Forecast run does not exist", 404)
    item = _refresh(owner, item)
    version_id = item.get("result_version_id")
    if not isinstance(version_id, str) or not version_id:
        raise ApiError("forecast_result_not_ready", "Forecast result is not available", 409)
    response = s3.get_object(
        Bucket=DATA_BUCKET,
        Key=item["result_key"],
        VersionId=version_id,
    )
    if int(response.get("ContentLength", 0)) > MAX_RESULT_BYTES:
        raise ApiError("forecast_result_invalid", "Forecast result exceeds the server limit", 502)
    body = response["Body"].read(MAX_RESULT_BYTES + 1)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError("forecast_result_invalid", "Forecast result is not valid JSON", 502) from exc
    if not isinstance(payload, dict):
        raise ApiError("forecast_result_invalid", "Forecast result must be a JSON object", 502)
    identity = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    if (
        payload.get("owner_id") != owner
        or payload.get("run_id") != run_id
        or payload.get("dataset_id") != item["dataset_id"]
        or identity.get("version_id") != item["input_version_id"]
    ):
        raise ApiError(
            "forecast_result_invalid", "Forecast result identity does not match the run", 502
        )
    prefix = f"forecast-results/users/{owner}/datasets/{item['dataset_id']}/runs/{run_id}/"
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    downloads: dict[str, str] = {}
    for name in ("forecast", "model", "manifest"):
        artifact = artifacts.get(name) if isinstance(artifacts.get(name), dict) else None
        if artifact is None:
            continue
        key = artifact.get("key")
        artifact_version = artifact.get("version_id")
        if (
            not isinstance(key, str)
            or not key.startswith(prefix)
            or not isinstance(artifact_version, str)
            or not artifact_version
        ):
            raise ApiError("forecast_result_invalid", "Forecast artifact identity is invalid", 502)
        downloads[name] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": DATA_BUCKET, "Key": key, "VersionId": artifact_version},
            ExpiresIn=900,
        )
    payload["downloads"] = downloads
    return payload


def _consume_agent_quota(table: Any, owner: str) -> None:
    today = datetime.now(UTC).date()
    now = datetime.now(UTC).isoformat()
    expires = int(time.time()) + 2 * 86400
    try:
        table.update_item(
            Key={"pk": f"USER#{owner}", "sk": f"AGENT_QUOTA#{today.isoformat()}"},
            UpdateExpression=(
                "SET entity_type=:entity, owner_sub=:owner, updated_at=:now, "
                "expires_at=:expires ADD call_count :one"
            ),
            ConditionExpression="attribute_not_exists(call_count) OR call_count < :limit",
            ExpressionAttributeValues={
                ":entity": "AGENT_QUOTA",
                ":owner": owner,
                ":now": now,
                ":expires": expires,
                ":one": 1,
                ":limit": AGENT_DAILY_LIMIT,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ApiError(
                "agent_rate_limit_exceeded",
                "The daily AI planning limit has been reached",
                429,
            ) from exc
        raise


def _validation_result_for_agent(
    s3: Any,
    table: Any,
    *,
    owner: str,
    dataset: dict[str, Any],
    dataset_id: str,
    validation_job_id: str,
) -> dict[str, Any]:
    response = table.get_item(
        Key={"pk": f"USER#{owner}", "sk": f"VALIDATION#{validation_job_id}"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if (
        not isinstance(item, dict)
        or item.get("owner_sub") != owner
        or item.get("dataset_id") != dataset_id
    ):
        raise ApiError("validation_not_found", "Validation job does not exist", 404)
    if item.get("status") != "succeeded":
        raise ApiError(
            "validation_not_complete",
            "A successful validation is required before AI planning",
            409,
            {"status": item.get("status")},
        )
    result_key = item.get("result_key")
    result_version_id = item.get("result_version_id")
    if not isinstance(result_key, str) or not isinstance(result_version_id, str):
        raise ApiError(
            "validation_result_unavailable",
            "The immutable validation result is unavailable",
            409,
        )
    try:
        stored = s3.get_object(
            Bucket=DATA_BUCKET,
            Key=result_key,
            VersionId=result_version_id,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "NoSuchVersion", "NotFound"}:
            raise ApiError(
                "validation_result_unavailable",
                "The immutable validation result is unavailable",
                409,
            ) from exc
        raise
    body = stored["Body"]
    try:
        raw = body.read(MAX_VALIDATION_RESULT_BYTES + 1)
    finally:
        body.close()
    if len(raw) > MAX_VALIDATION_RESULT_BYTES:
        raise ApiError(
            "validation_result_too_large",
            "The validation result exceeds the planning limit",
            500,
        )
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            "validation_result_invalid",
            "The validation result is malformed",
            500,
        ) from exc
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != "validation-result/v1"
        or result.get("status") != "succeeded"
        or result.get("job_id") != validation_job_id
        or result.get("dataset_id") != dataset_id
    ):
        raise ApiError(
            "validation_result_invalid",
            "The validation result identity is invalid",
            500,
        )
    identity = result.get("input_identity")
    if not isinstance(identity, dict) or identity.get("version_id") != dataset.get(
        "object_version_id"
    ):
        raise ApiError(
            "validation_result_mismatch",
            "The validation result does not match the immutable dataset version",
            409,
        )
    return result


def _agent_plan(event: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    owner = _identity(event)
    dataset_id = _canonical_uuid(
        dataset_id,
        code="dataset_not_found",
        message="Dataset does not exist",
        status=404,
    )
    body = _parse_body(event)
    validation_job_id = _canonical_uuid(
        body.get("validationJobId"),
        code="validation_not_found",
        message="Validation job does not exist",
        status=404,
    )
    s3, table, _, _ = _clients()
    dataset = _dataset(table, owner, dataset_id)
    validation_result = _validation_result_for_agent(
        s3,
        table,
        owner=owner,
        dataset=dataset,
        dataset_id=dataset_id,
        validation_job_id=validation_job_id,
    )
    _consume_agent_quota(table, owner)
    try:
        return build_forecast_agent_plan(
            dataset_id=dataset_id,
            dataset_version_id=str(dataset["object_version_id"]),
            validation_result=validation_result,
            objective=body.get("objective"),
        )
    except AgentPlanError as exc:
        raise ApiError(exc.code, exc.message, exc.status, exc.detail) from exc


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = getattr(context, "aws_request_id", None)
    try:
        route = event.get("routeKey")
        path = event.get("rawPath", "")
        parts = path.split("/")
        if route == "POST /api/datasets/{dataset_id}/forecast-agent" and len(parts) == 5:
            return _response(200, _agent_plan(event, parts[3]))
        if route == "POST /api/datasets/{dataset_id}/forecasts" and len(parts) == 5:
            status, payload = _create(event, parts[3])
            return _response(status, payload)
        if route == "GET /api/forecasts/{run_id}" and len(parts) == 4:
            return _response(200, _status(event, parts[3]))
        if route == "GET /api/forecasts/{run_id}/result" and len(parts) == 5:
            return _response(200, _result(event, parts[3]))
        raise ApiError("not_found", "Route does not exist", 404)
    except ApiError as exc:
        payload: dict[str, Any] = {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "requestId": request_id,
            }
        }
        if exc.detail:
            payload["error"]["detail"] = exc.detail
        return _response(exc.status, payload)
    except Exception:
        LOGGER.exception("unhandled forecast control-plane error", extra={"request_id": request_id})
        return _response(
            500,
            {
                "error": {
                    "code": "internal_error",
                    "message": "The forecast request could not be completed",
                    "requestId": request_id,
                }
            },
        )
