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
from agent_async import (
    AGENT_ACTIVE_TURN_STATUSES,
    AGENT_ASYNC_SCHEMA_VERSION,
    AGENT_ASYNC_SOURCE,
    AgentAsyncValueError,
    canonical_request_token,
    clean_agent_message,
    invocation_payload,
    session_id_for,
    turn_id_for,
    turn_view,
)
from boto3.dynamodb.types import TypeSerializer  # type: ignore[import-untyped]
from botocore.config import Config
from botocore.exceptions import ClientError
from orchestrator import OrchestratorError, run_agent_turn

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
AGENT_SESSION_MAX_TURNS = int(os.environ.get("AGENT_SESSION_MAX_TURNS", "8"))
AGENT_SESSION_TTL_DAYS = int(os.environ.get("AGENT_SESSION_TTL_DAYS", "7"))
MAX_RESULT_BYTES = 2 * 1024 * 1024
RESULT_REVIEW_POLICY_VERSION = "forecast-result-review/v1"
MAX_INPUT_BYTES = 500_000_000
SLOT_LEASE_SECONDS = FORECAST_JOB_TIMEOUT_SECONDS + 900
ACTIVE_BATCH = {"SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"}
SUPPORTED_ADAPTERS = {
    "xgboost-direct-v1",
    "neuralnet-direct-v1",
    "chronos2-zero-shot-v1",
}
TERMINAL = {"succeeded", "invalid", "failed"}
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
COLUMN_PATTERN = re.compile(r"^.{1,128}$", re.DOTALL)
SERIALIZER = TypeSerializer()
BOTO_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"})

_S3 = None
_TABLE = None
_DDB = None
_BATCH = None
_LAMBDA = None


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


def _new_response_id() -> str:
    return str(uuid.UUID(bytes=os.urandom(16), version=4))


def _response_log_message(response_id: str, status_code: int) -> str:
    return json.dumps(
        {
            "event": "api_response",
            "requestId": response_id,
            "sourceRevision": SOURCE_REVISION,
            "statusCode": status_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    response_id = _new_response_id()
    LOGGER.info(_response_log_message(response_id, status))
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
            "x-vonavy-request-id": response_id,
            "x-vonavy-source-revision": SOURCE_REVISION,
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


def _adapter_id(payload: dict[str, Any]) -> str:
    value = payload.get("adapterId", "xgboost-direct-v1")
    if value not in SUPPORTED_ADAPTERS:
        raise ApiError(
            "unsupported_forecast_adapter",
            "adapterId must be xgboost-direct-v1, neuralnet-direct-v1, or chronos2-zero-shot-v1",
            422,
        )
    return str(value)


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


def _fingerprint(
    dataset_id: str, mapping: dict[str, Any], training_end: str, adapter_id: str
) -> str:
    encoded = json.dumps(
        {
            "dataset_id": dataset_id,
            "mapping": mapping,
            "training_end": training_end,
            "adapter_id": adapter_id,
        },
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
        "adapterId": item.get("adapter_id", "xgboost-direct-v1"),
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
    adapter_id: str,
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
        "adapter_id": adapter_id,
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
    adapter_id = _adapter_id(body)
    _, table, ddb, batch = _clients()
    dataset = _dataset(table, owner, dataset_id)
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonavy-forecast:{owner}:{token}"))
    fingerprint = _fingerprint(dataset_id, mapping, training_end, adapter_id)
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
        adapter_id=adapter_id,
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
                                "adapter_id": adapter_id,
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
    adapter = result.get("adapter") if isinstance(result.get("adapter"), dict) else {}
    if (
        result.get("schema_version") != "forecast-result/v1"
        or result.get("owner_id") != owner
        or result.get("run_id") != item["run_id"]
        or result.get("dataset_id") != item["dataset_id"]
        or identity.get("version_id") != item["input_version_id"]
        or adapter.get("id") != item.get("adapter_id", "xgboost-direct-v1")
    ):
        raise ApiError(
            "forecast_result_invalid", "Forecast result identity does not match the run", 502
        )
    if status == "succeeded":
        _validated_result_evaluation(result)
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


def _evaluation_error(message: str) -> ApiError:
    return ApiError("forecast_result_invalid", message, 502)


def _evaluation_int(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _evaluation_error("Forecast evaluation integer evidence is invalid")
    return value


def _evaluation_number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    optional: bool = False,
) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _evaluation_error("Forecast evaluation numeric evidence is invalid")
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        raise _evaluation_error("Forecast evaluation numeric evidence is non-finite")
    if minimum is not None and number < minimum:
        raise _evaluation_error("Forecast evaluation numeric evidence is below its bound")
    if maximum is not None and number > maximum:
        raise _evaluation_error("Forecast evaluation numeric evidence exceeds its bound")
    return number


def _validated_result_evaluation(
    result: dict[str, Any], *, required: bool = False
) -> dict[str, Any] | None:
    value = result.get("evaluation")
    if value is None:
        if required:
            raise _evaluation_error("Successful forecast result is missing evaluation evidence")
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "forecast-evaluation/v1"
        or value.get("evidence_basis") != "worker-holdout-and-aggregate-features"
    ):
        raise _evaluation_error("Forecast evaluation evidence is invalid")
    holdout_origin = value.get("holdout_origin")
    if holdout_origin is not None and (
        not isinstance(holdout_origin, str)
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", holdout_origin)
    ):
        raise _evaluation_error("Forecast evaluation holdout origin is invalid")
    safety = value.get("safety")
    if not isinstance(safety, dict) or safety != {
        "deterministic": True,
        "worker_computed": True,
        "raw_rows_exported": False,
        "raw_entity_values_exported": False,
        "automatic_experiment_execution": False,
    }:
        raise _evaluation_error("Forecast evaluation safety evidence is invalid")

    skill = value.get("baseline_skill")
    if not isinstance(skill, dict):
        raise _evaluation_error("Forecast baseline evidence is invalid")
    if not isinstance(skill.get("supported"), bool) or skill.get("metric") != "wape":
        raise _evaluation_error("Forecast baseline evidence is invalid")
    _evaluation_int(skill.get("common_rows"))
    model_value = _evaluation_number(skill.get("model_value"), minimum=0, optional=True)
    baseline_value = _evaluation_number(skill.get("baseline_value"), minimum=0, optional=True)
    improvement = _evaluation_number(skill.get("relative_improvement"), optional=True)
    verdict = skill.get("verdict")
    if verdict not in {"better", "tied", "worse", "unavailable"}:
        raise _evaluation_error("Forecast baseline verdict is invalid")
    if skill["supported"]:
        if None in {model_value, baseline_value, improvement} or verdict == "unavailable":
            raise _evaluation_error("Supported forecast baseline evidence is incomplete")
    elif verdict != "unavailable":
        raise _evaluation_error("Unsupported forecast baseline evidence has a verdict")
    reason = skill.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 300):
        raise _evaluation_error("Forecast baseline reason is invalid")

    worst = value.get("worst_entities")
    shifts = value.get("feature_shifts")
    unavailable = value.get("unavailable")
    if (
        not isinstance(worst, list)
        or len(worst) > 10
        or not isinstance(shifts, list)
        or len(shifts) > 10
        or not isinstance(unavailable, list)
        or len(unavailable) > 12
        or any(not isinstance(item, str) or len(item) > 256 for item in unavailable)
    ):
        raise _evaluation_error("Forecast evaluation evidence exceeds bounds")

    evaluated_entities = _evaluation_int(value.get("evaluated_entity_count"))
    cold_entities = _evaluation_int(value.get("cold_start_entity_count"))
    cold_rate = _evaluation_number(value.get("cold_start_rate"), minimum=0, maximum=1)
    if cold_entities > evaluated_entities:
        raise _evaluation_error("Forecast cold-start evidence is inconsistent")
    expected_cold_rate = cold_entities / evaluated_entities if evaluated_entities else 0.0
    if cold_rate is None or abs(cold_rate - expected_cold_rate) > 1e-9:
        raise _evaluation_error("Forecast cold-start rate is inconsistent")

    _evaluation_int(value.get("evaluated_feature_count"))
    extrapolated = _evaluation_int(value.get("extrapolated_value_count"))
    evaluated_values = _evaluation_int(value.get("evaluated_value_count"))
    extrapolation_rate = _evaluation_number(
        value.get("feature_extrapolation_rate"), minimum=0, maximum=1
    )
    if extrapolated > evaluated_values:
        raise _evaluation_error("Forecast extrapolation evidence is inconsistent")
    expected_extrapolation_rate = extrapolated / evaluated_values if evaluated_values else 0.0
    if extrapolation_rate is None or abs(extrapolation_rate - expected_extrapolation_rate) > 1e-9:
        raise _evaluation_error("Forecast extrapolation rate is inconsistent")

    for item in worst:
        if not isinstance(item, dict):
            raise _evaluation_error("Forecast entity evidence is invalid")
        entity_key = item.get("entity_key")
        if not isinstance(entity_key, str) or not re.fullmatch(r"entity-[0-9a-f]{16}", entity_key):
            raise _evaluation_error("Forecast entity evidence is invalid")
        _evaluation_int(item.get("rows"), minimum=1)
        _evaluation_number(item.get("model_wape"), minimum=0, optional=True)
        _evaluation_number(item.get("baseline_wape"), minimum=0, optional=True)
        _evaluation_number(item.get("relative_improvement"), optional=True)
        _evaluation_number(item.get("model_mae"), minimum=0)
        _evaluation_number(item.get("bias"))

    for item in shifts:
        if not isinstance(item, dict):
            raise _evaluation_error("Forecast feature evidence is invalid")
        feature = item.get("feature")
        kind = item.get("kind")
        statistic = item.get("statistic")
        if not isinstance(feature, str) or not 1 <= len(feature) <= 128:
            raise _evaluation_error("Forecast feature evidence is invalid")
        if kind not in {"numeric", "categorical"}:
            raise _evaluation_error("Forecast feature kind is invalid")
        expected_statistic = "unseen_rate" if kind == "categorical" else "standardized_mean_shift"
        if statistic != expected_statistic or item.get("severity") not in {
            "info",
            "notice",
            "warning",
        }:
            raise _evaluation_error("Forecast feature evidence is invalid")
        _evaluation_number(item.get("value"), minimum=0)
        _evaluation_int(item.get("reference_count"), minimum=1)
        fresh_count = _evaluation_int(item.get("fresh_count"), minimum=1)
        feature_extrapolated = _evaluation_int(item.get("extrapolated_count"))
        if feature_extrapolated > fresh_count:
            raise _evaluation_error("Forecast feature extrapolation evidence is inconsistent")
    return value


def _finding(
    finding_id: str,
    severity: str,
    message: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "findingId": finding_id,
        "severity": severity,
        "confidence": "measured",
        "message": message,
        "evidence": evidence,
    }


def _recommendation(
    recommendation_id: str,
    priority: str,
    action: str,
    rationale: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "recommendationId": recommendation_id,
        "priority": priority,
        "action": action,
        "rationale": rationale,
        "evidence": evidence,
        "executesAutomatically": False,
    }


def _forecast_result_review(result: dict[str, Any]) -> dict[str, Any]:
    evaluation = _validated_result_evaluation(result)
    safety = {
        "deterministic": True,
        "workerEvidenceOnly": True,
        "rawRowsRead": False,
        "rawEntityValuesRead": False,
        "bedrockInvoked": False,
        "automaticRerun": False,
    }
    if evaluation is None:
        return {
            "policyVersion": RESULT_REVIEW_POLICY_VERSION,
            "status": "insufficient_evidence",
            "headline": "This result predates worker-produced evaluation evidence.",
            "findings": [
                _finding(
                    "evaluation.unavailable",
                    "notice",
                    "Model-versus-baseline and drift diagnostics are unavailable for this run.",
                    {"reason": "missing forecast-evaluation/v1"},
                )
            ],
            "recommendations": [
                _recommendation(
                    "experiment.new_evaluated_run",
                    "normal",
                    "Create a new explicitly confirmed forecast run to generate bounded evaluation evidence.",
                    "Historical result artifacts are immutable and are not rewritten in place.",
                    {"requiredSchema": "forecast-evaluation/v1"},
                )
            ],
            "safety": safety,
        }

    findings: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    skill = evaluation.get("baseline_skill")
    if not isinstance(skill, dict):
        raise ApiError("forecast_result_invalid", "Forecast baseline evidence is invalid", 502)
    verdict = skill.get("verdict")
    improvement = skill.get("relative_improvement")
    skill_evidence = {
        "metric": skill.get("metric"),
        "commonRows": skill.get("common_rows"),
        "modelValue": skill.get("model_value"),
        "baselineValue": skill.get("baseline_value"),
        "relativeImprovement": improvement,
    }
    if verdict == "better":
        findings.append(
            _finding(
                "skill.model_vs_baseline",
                "info"
                if isinstance(improvement, (int, float)) and improvement >= 0.05
                else "notice",
                "The selected model beat the seasonal baseline on the common recent holdout.",
                skill_evidence,
            )
        )
    elif verdict == "worse":
        findings.append(
            _finding(
                "skill.model_vs_baseline",
                "warning",
                "The selected model underperformed the seasonal baseline on the common recent holdout.",
                skill_evidence,
            )
        )
        recommendations.append(
            _recommendation(
                "experiment.compare_adapter",
                "high",
                "Manually compare another supported adapter on the same immutable dataset version and mapping.",
                "The measured holdout skill is negative; another model class is the highest-value next experiment.",
                skill_evidence,
            )
        )
    elif verdict == "tied":
        findings.append(
            _finding(
                "skill.model_vs_baseline",
                "notice",
                "The selected model and seasonal baseline were effectively tied on the recent holdout.",
                skill_evidence,
            )
        )
    else:
        findings.append(
            _finding(
                "skill.model_vs_baseline",
                "notice",
                "A common model-versus-baseline WAPE could not be calculated.",
                {**skill_evidence, "reason": skill.get("reason")},
            )
        )

    cold_count = int(evaluation.get("cold_start_entity_count", 0))
    cold_rate = float(evaluation.get("cold_start_rate", 0.0))
    if cold_count:
        severity = "warning" if cold_rate >= 0.10 else "notice"
        evidence = {
            "coldStartEntities": cold_count,
            "evaluatedEntities": int(evaluation.get("evaluated_entity_count", 0)),
            "coldStartRate": cold_rate,
        }
        findings.append(
            _finding(
                "entities.cold_start",
                severity,
                "Some forecast entities were absent from the adapter's fitted or contextual evidence.",
                evidence,
            )
        )
        recommendations.append(
            _recommendation(
                "experiment.cold_start_strategy",
                "high" if severity == "warning" else "normal",
                "Compare a zero-shot or pooled fallback strategy for the measured cold-start entities.",
                "Cold-start entities cannot benefit from the same entity-specific history as established entities.",
                evidence,
            )
        )

    extrapolation_rate = float(evaluation.get("feature_extrapolation_rate", 0.0))
    if extrapolation_rate > 0:
        severity = "warning" if extrapolation_rate >= 0.05 else "notice"
        evidence = {
            "rate": extrapolation_rate,
            "extrapolatedValues": int(evaluation.get("extrapolated_value_count", 0)),
            "evaluatedValues": int(evaluation.get("evaluated_value_count", 0)),
        }
        findings.append(
            _finding(
                "features.extrapolation",
                severity,
                "Fresh forecast features contain values outside the fitted numeric ranges or categorical levels.",
                evidence,
            )
        )
        recommendations.append(
            _recommendation(
                "experiment.extrapolation_backtest",
                "high" if severity == "warning" else "normal",
                "Run an explicitly confirmed recent-origin backtest or alternate adapter comparison before relying on extrapolated covariates.",
                "The worker measured feature values outside the training support.",
                evidence,
            )
        )

    shifts = evaluation.get("feature_shifts", [])
    warning_shifts = [
        item for item in shifts if isinstance(item, dict) and item.get("severity") == "warning"
    ]
    if warning_shifts:
        evidence = {
            "warningFeatureCount": len(warning_shifts),
            "topFeatures": [
                {
                    "feature": item.get("feature"),
                    "statistic": item.get("statistic"),
                    "value": item.get("value"),
                }
                for item in warning_shifts[:5]
            ],
        }
        findings.append(
            _finding(
                "features.train_fresh_shift",
                "warning",
                "One or more fresh feature distributions differ materially from the fitted evidence.",
                evidence,
            )
        )
        recommendations.append(
            _recommendation(
                "experiment.recent_window",
                "high",
                "Compare the current setup with a more recent training window while preserving the same leakage-safe holdout protocol.",
                "Large aggregate train/fresh shifts make stale relationships a plausible failure mode.",
                evidence,
            )
        )

    worst = evaluation.get("worst_entities", [])
    worst_item = worst[0] if worst and isinstance(worst[0], dict) else None
    worst_wape = worst_item.get("model_wape") if worst_item else None
    if isinstance(worst_wape, (int, float)) and worst_wape >= 0.50:
        severity = "warning" if worst_wape >= 1.0 else "notice"
        evidence = {
            "entityKey": worst_item.get("entity_key"),
            "modelWape": worst_wape,
            "rows": worst_item.get("rows"),
            "bias": worst_item.get("bias"),
        }
        findings.append(
            _finding(
                "entities.worst_case",
                severity,
                "The worst measured entity has materially higher recent holdout error.",
                evidence,
            )
        )
        recommendations.append(
            _recommendation(
                "experiment.entity_segment",
                "normal",
                "Inspect a bounded segment-level backtest for the worst entity cohort before changing the global model.",
                "Aggregate model quality can hide concentrated entity-level failure.",
                evidence,
            )
        )

    if not recommendations:
        recommendations.append(
            _recommendation(
                "experiment.additional_origin",
                "normal",
                "Validate the selected adapter on an additional recent rolling origin before production use.",
                "The current result is encouraging, but one recent holdout is not a stability guarantee.",
                {"currentVerdict": verdict, "currentHoldoutRows": skill.get("common_rows")},
            )
        )

    attention = any(item["severity"] == "warning" for item in findings)
    return {
        "policyVersion": RESULT_REVIEW_POLICY_VERSION,
        "status": "needs_attention" if attention else "ready",
        "headline": (
            "Measured result evidence contains issues that should be reviewed before reuse."
            if attention
            else "Measured result evidence is available with no warning-level finding."
        ),
        "findings": findings,
        "recommendations": recommendations[:6],
        "unavailable": evaluation.get("unavailable", []),
        "safety": safety,
    }


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
    adapter = payload.get("adapter") if isinstance(payload.get("adapter"), dict) else {}
    if (
        payload.get("owner_id") != owner
        or payload.get("run_id") != run_id
        or payload.get("dataset_id") != item["dataset_id"]
        or identity.get("version_id") != item["input_version_id"]
        or adapter.get("id") != item.get("adapter_id", "xgboost-direct-v1")
    ):
        raise ApiError(
            "forecast_result_invalid", "Forecast result identity does not match the run", 502
        )
    payload["review"] = _forecast_result_review(payload)
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


def _agent_session_key(owner: str, session_id: str) -> dict[str, str]:
    return {"pk": f"USER#{owner}", "sk": f"AGENT_SESSION#{session_id}"}


def _agent_session_item(table: Any, owner: str, session_id: str) -> dict[str, Any]:
    response = table.get_item(
        Key=_agent_session_key(owner, session_id),
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not isinstance(item, dict) or item.get("owner_sub") != owner:
        raise ApiError("agent_session_not_found", "Agent session does not exist", 404)
    return item


def _agent_session_json(value: object, field: str) -> object:
    if value is None or value == "":
        return None if field == "draft_plan_json" else []
    if not isinstance(value, str):
        raise ApiError("agent_session_invalid", "Stored agent session is invalid", 500)
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ApiError("agent_session_invalid", "Stored agent session is invalid", 500) from exc


def _agent_session_response(item: dict[str, Any]) -> dict[str, Any]:
    history = _agent_session_json(item.get("messages_json"), "messages_json")
    draft = _agent_session_json(item.get("draft_plan_json"), "draft_plan_json")
    if not isinstance(history, list) or (draft is not None and not isinstance(draft, dict)):
        raise ApiError("agent_session_invalid", "Stored agent session is invalid", 500)
    message = ""
    for entry in reversed(history):
        if isinstance(entry, dict) and entry.get("role") == "assistant":
            value = entry.get("text")
            if isinstance(value, str):
                message = value
                break
    turn = turn_view(item)
    tool_audit = _agent_session_json(item.get("turn_tool_audit_json"), "messages_json")
    privacy = _agent_session_json(item.get("turn_privacy_json"), "draft_plan_json")
    if not isinstance(tool_audit, list) or (privacy is not None and not isinstance(privacy, dict)):
        raise ApiError("agent_session_invalid", "Stored agent session is invalid", 500)
    return {
        "schemaVersion": "forecast-agent-session/v2",
        "sessionId": item["session_id"],
        "datasetId": item["dataset_id"],
        "validationJobId": item["validation_job_id"],
        "turnCount": int(item.get("turn_count", 0)),
        "message": message,
        "history": history,
        "draftPlan": draft,
        "requiresConfirmation": draft is not None and not turn["pending"],
        "turn": turn,
        "toolAudit": tool_audit,
        "provider": item.get("turn_provider"),
        "model": item.get("turn_model"),
        "privacy": privacy or {},
        "links": {
            "self": f"/api/forecast-agent/sessions/{item['session_id']}",
            "messages": f"/api/forecast-agent/sessions/{item['session_id']}/messages",
            "execute": f"/api/datasets/{item['dataset_id']}/forecasts",
        },
    }


def _run_agent_session_turn(
    *,
    table: Any,
    owner: str,
    dataset: dict[str, Any],
    dataset_id: str,
    validation_result: dict[str, Any],
    message: object,
    history: object,
) -> dict[str, Any]:
    _consume_agent_quota(table, owner)
    try:
        turn = run_agent_turn(
            dataset_id=dataset_id,
            dataset_version_id=str(dataset["object_version_id"]),
            validation_result=validation_result,
            message=message,
            history=history,
        )
    except OrchestratorError as exc:
        raise ApiError(exc.code, exc.message, exc.status, exc.detail) from exc
    return turn.as_dict()


def _lambda_client() -> Any:
    global _LAMBDA
    if _LAMBDA is None:
        _LAMBDA = boto3.client("lambda", region_name=AWS_REGION_NAME, config=BOTO_CONFIG)
    return _LAMBDA


def _agent_request(body: dict[str, Any]) -> tuple[str, str]:
    try:
        return clean_agent_message(body.get("message")), canonical_request_token(
            body.get("requestToken")
        )
    except AgentAsyncValueError as exc:
        raise ApiError("invalid_agent_message", str(exc), 422) from exc


def _enqueue_agent_turn(owner: str, session_id: str, turn_id: str) -> None:
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        raise ApiError("agent_enqueue_failed", "Agent worker identity is unavailable", 503)
    try:
        response = _lambda_client().invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=invocation_payload(owner, session_id, turn_id),
        )
    except ClientError as exc:
        raise ApiError("agent_enqueue_failed", "The agent turn could not be queued", 503) from exc
    if int(response.get("StatusCode", 0)) != 202:
        raise ApiError("agent_enqueue_failed", "The agent turn could not be queued", 503)


def _store_agent_turn_failure(
    table: Any,
    *,
    owner: str,
    session_id: str,
    turn_id: str,
    code: str,
    message: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    try:
        table.update_item(
            Key=_agent_session_key(owner, session_id),
            UpdateExpression=(
                "SET #turn_status=:failed, turn_error_code=:code, "
                "turn_error_message=:message, turn_completed_at=:now, updated_at=:now "
                "REMOVE turn_message, turn_started_at"
            ),
            ConditionExpression=("owner_sub=:owner AND session_id=:session AND turn_id=:turn"),
            ExpressionAttributeNames={"#turn_status": "turn_status"},
            ExpressionAttributeValues={
                ":failed": "failed",
                ":code": code[:96],
                ":message": message[:500],
                ":now": now,
                ":owner": owner,
                ":session": session_id,
                ":turn": turn_id,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            LOGGER.exception("Could not persist the asynchronous agent-turn failure")


def _agent_turn_worker(event: dict[str, Any]) -> None:
    owner = event.get("owner")
    session_id = event.get("sessionId")
    turn_id = event.get("turnId")
    if (
        event.get("schemaVersion") != AGENT_ASYNC_SCHEMA_VERSION
        or event.get("source") != AGENT_ASYNC_SOURCE
        or not isinstance(owner, str)
        or not OWNER_PATTERN.fullmatch(owner)
    ):
        LOGGER.warning("Rejected an invalid asynchronous agent-turn event")
        return
    try:
        session_id = _canonical_uuid(
            session_id,
            code="agent_session_not_found",
            message="Agent session does not exist",
            status=404,
        )
        turn_id = _canonical_uuid(
            turn_id,
            code="agent_turn_not_found",
            message="Agent turn does not exist",
            status=404,
        )
    except ApiError:
        LOGGER.warning("Rejected an invalid asynchronous agent-turn identity")
        return

    s3, table, _, _ = _clients()
    try:
        item = _agent_session_item(table, owner, session_id)
    except ApiError:
        return
    if item.get("turn_id") != turn_id or item.get("turn_status") != "queued":
        return

    started = datetime.now(UTC).isoformat()
    try:
        table.update_item(
            Key=_agent_session_key(owner, session_id),
            UpdateExpression=(
                "SET #turn_status=:processing, turn_started_at=:started, updated_at=:started"
            ),
            ConditionExpression=(
                "owner_sub=:owner AND session_id=:session AND turn_id=:turn "
                "AND #turn_status=:queued"
            ),
            ExpressionAttributeNames={"#turn_status": "turn_status"},
            ExpressionAttributeValues={
                ":processing": "processing",
                ":started": started,
                ":owner": owner,
                ":session": session_id,
                ":turn": turn_id,
                ":queued": "queued",
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return
        LOGGER.exception("Could not claim the asynchronous agent turn")
        return

    try:
        dataset_id = str(item["dataset_id"])
        dataset = _dataset(table, owner, dataset_id)
        if str(dataset.get("object_version_id")) != str(item.get("dataset_version_id")):
            raise ApiError("agent_session_stale", "The dataset version changed", 409)
        validation_result = _validation_result_for_agent(
            s3,
            table,
            owner=owner,
            dataset=dataset,
            dataset_id=dataset_id,
            validation_job_id=str(item["validation_job_id"]),
        )
        history = _agent_session_json(item.get("messages_json"), "messages_json")
        turn = _run_agent_session_turn(
            table=table,
            owner=owner,
            dataset=dataset,
            dataset_id=dataset_id,
            validation_result=validation_result,
            message=item.get("turn_message"),
            history=history,
        )
        turns = int(item.get("turn_count", 0))
        now = datetime.now(UTC).isoformat()
        expires = int(time.time()) + AGENT_SESSION_TTL_DAYS * 86400
        table.update_item(
            Key=_agent_session_key(owner, session_id),
            UpdateExpression=(
                "SET messages_json=:messages, draft_plan_json=:draft, turn_count=:turns, "
                "#turn_status=:succeeded, turn_completed_at=:now, updated_at=:now, "
                "expires_at=:expires, turn_tool_audit_json=:audit, "
                "turn_provider=:provider, turn_model=:model, turn_privacy_json=:privacy "
                "REMOVE turn_message, turn_error_code, turn_error_message"
            ),
            ConditionExpression=(
                "owner_sub=:owner AND session_id=:session AND turn_id=:turn "
                "AND #turn_status=:processing"
            ),
            ExpressionAttributeNames={"#turn_status": "turn_status"},
            ExpressionAttributeValues={
                ":messages": json.dumps(turn["history"], separators=(",", ":")),
                ":draft": json.dumps(turn.get("draftPlan"), separators=(",", ":")),
                ":turns": turns + 1,
                ":succeeded": "succeeded",
                ":now": now,
                ":expires": expires,
                ":audit": json.dumps(turn["toolAudit"], separators=(",", ":")),
                ":provider": turn["provider"],
                ":model": turn["model"],
                ":privacy": json.dumps(turn["privacy"], separators=(",", ":")),
                ":owner": owner,
                ":session": session_id,
                ":turn": turn_id,
                ":processing": "processing",
            },
        )
    except ApiError as exc:
        _store_agent_turn_failure(
            table,
            owner=owner,
            session_id=session_id,
            turn_id=turn_id,
            code=exc.code,
            message=exc.message,
        )
    except Exception:
        LOGGER.exception("Asynchronous agent turn failed")
        _store_agent_turn_failure(
            table,
            owner=owner,
            session_id=session_id,
            turn_id=turn_id,
            code="agent_turn_failed",
            message="The agent turn could not be completed.",
        )


def _agent_session_create(event: dict[str, Any], dataset_id: str) -> dict[str, Any]:
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
    message, request_token = _agent_request(body)
    s3, table, _, _ = _clients()
    dataset = _dataset(table, owner, dataset_id)
    _validation_result_for_agent(
        s3,
        table,
        owner=owner,
        dataset=dataset,
        dataset_id=dataset_id,
        validation_job_id=validation_job_id,
    )
    session_id = session_id_for(owner, dataset_id, request_token)
    turn_id = turn_id_for(session_id, request_token)
    now = datetime.now(UTC).isoformat()
    expires = int(time.time()) + AGENT_SESSION_TTL_DAYS * 86400
    item = {
        **_agent_session_key(owner, session_id),
        "owner_sub": owner,
        "session_id": session_id,
        "dataset_id": dataset_id,
        "dataset_version_id": str(dataset["object_version_id"]),
        "validation_job_id": validation_job_id,
        "messages_json": "[]",
        "draft_plan_json": "null",
        "turn_count": 0,
        "turn_id": turn_id,
        "turn_status": "queued",
        "turn_message": message,
        "turn_request_token": request_token,
        "turn_submitted_at": now,
        "created_at": now,
        "updated_at": now,
        "expires_at": expires,
    }
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = _agent_session_item(table, owner, session_id)
        if existing.get("turn_request_token") == request_token:
            return _agent_session_response(existing)
        raise ApiError("agent_session_conflict", "The agent session already exists", 409) from exc
    try:
        _enqueue_agent_turn(owner, session_id, turn_id)
    except ApiError as exc:
        _store_agent_turn_failure(
            table,
            owner=owner,
            session_id=session_id,
            turn_id=turn_id,
            code=exc.code,
            message=exc.message,
        )
        raise
    return _agent_session_response(item)


def _agent_session_message(event: dict[str, Any], session_id: str) -> dict[str, Any]:
    owner = _identity(event)
    session_id = _canonical_uuid(
        session_id,
        code="agent_session_not_found",
        message="Agent session does not exist",
        status=404,
    )
    body = _parse_body(event)
    message, request_token = _agent_request(body)
    _, table, _, _ = _clients()
    item = _agent_session_item(table, owner, session_id)
    if item.get("turn_request_token") == request_token:
        return _agent_session_response(item)
    if item.get("turn_status") in AGENT_ACTIVE_TURN_STATUSES:
        raise ApiError("agent_turn_in_progress", "Wait for the current agent turn to finish", 409)
    turns = int(item.get("turn_count", 0))
    if turns >= AGENT_SESSION_MAX_TURNS:
        raise ApiError(
            "agent_session_limit",
            f"Agent sessions support at most {AGENT_SESSION_MAX_TURNS} turns",
            429,
        )
    dataset_id = str(item["dataset_id"])
    dataset = _dataset(table, owner, dataset_id)
    if str(dataset.get("object_version_id")) != str(item.get("dataset_version_id")):
        raise ApiError("agent_session_stale", "The dataset version changed", 409)

    turn_id = turn_id_for(session_id, request_token)
    now = datetime.now(UTC).isoformat()
    expires = int(time.time()) + AGENT_SESSION_TTL_DAYS * 86400
    try:
        table.update_item(
            Key=_agent_session_key(owner, session_id),
            UpdateExpression=(
                "SET #turn_status=:queued, turn_id=:turn, turn_message=:message, "
                "turn_request_token=:token, turn_submitted_at=:now, updated_at=:now, "
                "expires_at=:expires, draft_plan_json=:draft "
                "REMOVE turn_started_at, turn_completed_at, turn_error_code, "
                "turn_error_message, turn_tool_audit_json, turn_provider, turn_model, "
                "turn_privacy_json"
            ),
            ConditionExpression=(
                "owner_sub=:owner AND session_id=:session AND turn_count=:expected_turns "
                "AND (attribute_not_exists(#turn_status) OR #turn_status=:idle "
                "OR #turn_status=:succeeded OR #turn_status=:failed)"
            ),
            ExpressionAttributeNames={"#turn_status": "turn_status"},
            ExpressionAttributeValues={
                ":queued": "queued",
                ":turn": turn_id,
                ":message": message,
                ":token": request_token,
                ":now": now,
                ":expires": expires,
                ":draft": "null",
                ":owner": owner,
                ":session": session_id,
                ":expected_turns": turns,
                ":idle": "idle",
                ":succeeded": "succeeded",
                ":failed": "failed",
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        current = _agent_session_item(table, owner, session_id)
        if current.get("turn_request_token") == request_token:
            return _agent_session_response(current)
        raise ApiError(
            "agent_session_conflict",
            "The agent session changed; reload it before sending another message",
            409,
        ) from exc

    item.update(
        {
            "turn_status": "queued",
            "turn_id": turn_id,
            "turn_message": message,
            "turn_request_token": request_token,
            "turn_submitted_at": now,
            "updated_at": now,
            "expires_at": expires,
            "draft_plan_json": "null",
        }
    )
    for field in (
        "turn_started_at",
        "turn_completed_at",
        "turn_error_code",
        "turn_error_message",
        "turn_tool_audit_json",
        "turn_provider",
        "turn_model",
        "turn_privacy_json",
    ):
        item.pop(field, None)
    try:
        _enqueue_agent_turn(owner, session_id, turn_id)
    except ApiError as exc:
        _store_agent_turn_failure(
            table,
            owner=owner,
            session_id=session_id,
            turn_id=turn_id,
            code=exc.code,
            message=exc.message,
        )
        raise
    return _agent_session_response(item)


def _agent_session_get(event: dict[str, Any], session_id: str) -> dict[str, Any]:
    owner = _identity(event)
    session_id = _canonical_uuid(
        session_id,
        code="agent_session_not_found",
        message="Agent session does not exist",
        status=404,
    )
    _, table, _, _ = _clients()
    return _agent_session_response(_agent_session_item(table, owner, session_id))


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
    if event.get("source") == AGENT_ASYNC_SOURCE:
        _agent_turn_worker(event)
        return {"statusCode": 202}
    request_id = getattr(context, "aws_request_id", None)
    try:
        route = event.get("routeKey")
        path = event.get("rawPath", "")
        parts = path.split("/")
        if route == "POST /api/datasets/{dataset_id}/forecast-agent/sessions" and len(parts) == 6:
            return _response(201, _agent_session_create(event, parts[3]))
        if route == "POST /api/forecast-agent/sessions/{session_id}/messages" and len(parts) == 6:
            return _response(200, _agent_session_message(event, parts[4]))
        if route == "GET /api/forecast-agent/sessions/{session_id}" and len(parts) == 5:
            return _response(200, _agent_session_get(event, parts[4]))
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
