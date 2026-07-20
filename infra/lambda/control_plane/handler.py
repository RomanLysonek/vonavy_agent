from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
METADATA_TABLE = os.environ["METADATA_TABLE"]
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
MAX_DATASETS_PER_OWNER = int(os.environ.get("MAX_DATASETS_PER_OWNER", "10"))
MAX_TOTAL_BYTES_PER_OWNER = int(
    os.environ.get("MAX_TOTAL_BYTES_PER_OWNER", str(1024 * 1024 * 1024))
)
UPLOAD_RETENTION_DAYS = int(os.environ.get("UPLOAD_RETENTION_DAYS", "14"))
VALIDATION_JOB_QUEUE = os.environ.get("VALIDATION_JOB_QUEUE", "")
VALIDATION_JOB_DEFINITION = os.environ.get("VALIDATION_JOB_DEFINITION", "")
VALIDATION_JOB_TIMEOUT_SECONDS = int(os.environ.get("VALIDATION_JOB_TIMEOUT_SECONDS", "900"))
VALIDATION_MAX_ACTIVE_JOBS_PER_OWNER = int(
    os.environ.get("VALIDATION_MAX_ACTIVE_JOBS_PER_OWNER", "1")
)
AWS_REGION_NAME = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_REGION", "eu-central-1"))

if MAX_UPLOAD_BYTES < 1 or MAX_DATASETS_PER_OWNER < 1:
    raise RuntimeError("Upload policy limits must be positive")
if MAX_UPLOAD_BYTES * MAX_DATASETS_PER_OWNER > MAX_TOTAL_BYTES_PER_OWNER:
    raise RuntimeError("MAX_TOTAL_BYTES_PER_OWNER must cover every server-owned upload slot")
if not VALIDATION_JOB_QUEUE or not VALIDATION_JOB_DEFINITION:
    raise RuntimeError("Validation Batch queue and job definition must be configured")
if not 120 <= VALIDATION_JOB_TIMEOUT_SECONDS <= 3_600:
    raise RuntimeError("VALIDATION_JOB_TIMEOUT_SECONDS must be between 120 and 3600")
if VALIDATION_MAX_ACTIVE_JOBS_PER_OWNER != 1:
    raise RuntimeError("Phase 2B supports exactly one active validation job per owner")

OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

ALLOWED_MEDIA_TYPES = {
    ".csv": {"text/csv", "application/csv", "application/vnd.ms-excel"},
    ".parquet": {"application/vnd.apache.parquet", "application/octet-stream"},
}

_s3: Any | None = None
_ddb_resource: Any | None = None
_table: Any | None = None
_ddb_client: Any | None = None
_batch: Any | None = None
_SERIALIZER = TypeSerializer()
_ATTRIBUTE_VALUE_TYPES = {"S", "N", "B", "SS", "NS", "BS", "M", "L", "NULL", "BOOL"}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


def _clients() -> tuple[Any, Any, Any]:
    global _s3, _ddb_resource, _table, _ddb_client
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION_NAME)
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=AWS_REGION_NAME)
    if _table is None:
        _table = _ddb_resource.Table(METADATA_TABLE)
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb", region_name=AWS_REGION_NAME)
    return _s3, _table, _ddb_client


def _batch_client() -> Any:
    global _batch
    if _batch is None:
        _batch = boto3.client("batch", region_name=AWS_REGION_NAME)
    return _batch


def _av(value: Any) -> dict[str, Any]:
    """Serialize one native Python value into one DynamoDB AttributeValue."""
    if isinstance(value, dict) and set(value).issubset(_ATTRIBUTE_VALUE_TYPES):
        raise TypeError("DynamoDB AttributeValues must be built from native values exactly once")
    serialized = _SERIALIZER.serialize(value)
    if "M" in serialized and isinstance(value, dict):
        raise TypeError("ExpressionAttributeValues must serialize placeholders individually")
    return serialized


def _key(pk: str, sk: str) -> dict[str, dict[str, Any]]:
    return {"pk": _av(pk), "sk": _av(sk)}


def _item(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: _av(value) for name, value in values.items() if value is not None}


def _expression_values(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: _av(value) for name, value in values.items()}


def _attribute_value_type_map(values: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {name: next(iter(value), "Unknown") for name, value in values.items()}


def _sanitize_aws_error_message(message: str) -> str:
    return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "<redacted-email>", message)[:500]


def _json_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(payload, separators=(",", ":"), default=str),
    }


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    if not isinstance(body, str) or not body:
        raise ApiError("invalid_request", "A JSON request body is required", 400)
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError("invalid_request", "Request body is not valid UTF-8", 400) from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApiError("invalid_json", "Request body is not valid JSON", 400) from exc
    if not isinstance(parsed, dict):
        raise ApiError("invalid_request", "Request body must be a JSON object", 400)
    return parsed


def _identity(event: dict[str, Any]) -> tuple[str, str | None]:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if not isinstance(claims, dict):
        raise ApiError("unauthorized", "Authenticated identity is unavailable", 401)
    owner = claims.get("sub")
    if not isinstance(owner, str) or not OWNER_PATTERN.fullmatch(owner):
        raise ApiError("unauthorized", "Authenticated subject is invalid", 401)
    token_use = claims.get("token_use")
    if token_use is not None and token_use != "access":
        raise ApiError("unauthorized", "An access token is required", 401)
    email = claims.get("email")
    return owner, email if isinstance(email, str) else None


def _validate_upload(payload: dict[str, Any]) -> tuple[str, str, str, int]:
    allowed = {"datasetName", "filename", "mediaType", "sizeBytes"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ApiError("invalid_request", "Unknown request fields", 400, {"fields": unknown})

    dataset_name = payload.get("datasetName")
    filename = payload.get("filename")
    media_type = payload.get("mediaType")
    size_bytes = payload.get("sizeBytes")
    if not isinstance(dataset_name, str) or not 1 <= len(dataset_name.strip()) <= 200:
        raise ApiError("invalid_dataset_name", "datasetName must contain 1 to 200 characters", 400)
    if any(ord(character) < 32 or ord(character) == 127 for character in dataset_name):
        raise ApiError("invalid_dataset_name", "datasetName contains control characters", 400)
    if not isinstance(filename, str) or not 1 <= len(filename) <= 255:
        raise ApiError("invalid_filename", "filename must contain 1 to 255 characters", 400)
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise ApiError("invalid_filename", "filename contains control characters", 400)
    if (
        PurePath(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
    ):
        raise ApiError("invalid_filename", "filename must not contain path components", 400)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in ALLOWED_MEDIA_TYPES:
        raise ApiError("unsupported_file_type", "Only CSV and Parquet uploads are accepted", 415)
    if not isinstance(media_type, str) or media_type not in ALLOWED_MEDIA_TYPES[suffix]:
        raise ApiError(
            "unsupported_media_type",
            "mediaType does not match the supported type for the filename",
            415,
        )
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise ApiError("invalid_size", "sizeBytes must be an integer", 400)
    if not 1 <= size_bytes <= MAX_UPLOAD_BYTES:
        raise ApiError(
            "upload_too_large",
            "Upload exceeds the server-owned size policy",
            413,
            {"maximumBytes": MAX_UPLOAD_BYTES},
        )
    return dataset_name.strip(), filename, media_type, size_bytes


def _route(event: dict[str, Any]) -> tuple[str, str]:
    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath")
    if not isinstance(method, str) or not isinstance(path, str):
        raise ApiError("invalid_gateway_event", "Invalid API Gateway event", 500)
    return method.upper(), path


def _slot_is_reserved(item: dict[str, Any], now: int) -> bool:
    status = item.get("status")
    if status == "pending":
        return int(item.get("expires_at", 0)) > now
    return status not in {"released", "free", "expired"}


def _next_owner_upload_slot(table: Any, owner: str, requested_bytes: int) -> int:
    response = table.query(
        KeyConditionExpression=Key("pk").eq(f"USER#{owner}") & Key("sk").begins_with("SLOT#"),
        ConsistentRead=True,
        ProjectionExpression="slot_number, expected_size, #status, expires_at",
        ExpressionAttributeNames={"#status": "status"},
    )
    items = response.get("Items", [])
    used_slots: set[int] = set()
    reserved_bytes = 0
    now = int(time.time())
    for item in items:
        slot_number = int(item["slot_number"])
        if not 0 <= slot_number < MAX_DATASETS_PER_OWNER:
            raise ApiError("quota_state_invalid", "Stored quota state is invalid", 500)
        if not _slot_is_reserved(item, now):
            continue
        used_slots.add(slot_number)
        reserved_bytes += int(item["expected_size"])

    if len(used_slots) >= MAX_DATASETS_PER_OWNER:
        raise ApiError(
            "dataset_quota_exceeded",
            "Dataset count exceeds the server-owned owner quota",
            429,
            {"maximumDatasets": MAX_DATASETS_PER_OWNER},
        )
    if reserved_bytes + requested_bytes > MAX_TOTAL_BYTES_PER_OWNER:
        raise ApiError(
            "storage_quota_exceeded",
            "Stored data exceeds the server-owned owner quota",
            429,
            {"maximumBytes": MAX_TOTAL_BYTES_PER_OWNER},
        )
    return next(slot for slot in range(MAX_DATASETS_PER_OWNER) if slot not in used_slots)


def _transaction_cancel_codes(exc: ClientError) -> list[str]:
    reasons = exc.response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return []
    codes: list[str] = []
    for reason in reasons:
        if isinstance(reason, dict):
            code = reason.get("Code")
            codes.append(code if isinstance(code, str) else "None")
        else:
            codes.append("Unknown")
    return codes


def _is_transaction_canceled(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "TransactionCanceledException"


def _is_validation_exception(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ValidationException"


def _handle_create_upload_transaction_cancel(
    exc: ClientError,
    *,
    upload_id: str,
    dataset_id: str,
) -> None:
    codes = _transaction_cancel_codes(exc)
    request_id = exc.response.get("ResponseMetadata", {}).get("RequestId")
    if (
        codes
        and codes[0] == "ConditionalCheckFailed"
        and all(code in {"None", "ConditionalCheckFailed"} for code in codes[1:])
    ):
        raise ApiError(
            "upload_slot_busy",
            "Upload capacity changed concurrently; retry the request",
            409,
        ) from exc
    LOGGER.warning(
        "Create-upload DynamoDB transaction was canceled",
        extra={
            "route": "POST /api/upload-sessions",
            "aws_request_id": request_id,
            "cancellation_codes": codes,
            "upload_id": upload_id,
            "dataset_id": dataset_id,
            "action_order": ["slot", "dataset", "upload"],
        },
    )
    if "TransactionConflict" in codes:
        raise ApiError(
            "upload_transaction_conflict",
            "Upload metadata changed concurrently; retry the request",
            503,
        ) from exc
    raise ApiError(
        "upload_transaction_failed",
        "Upload metadata could not be reserved",
        503,
        {"requestId": request_id},
    ) from exc


def _log_create_upload_validation_exception(
    exc: ClientError,
    *,
    lambda_request_id: str | None,
    upload_id: str,
    dataset_id: str,
    transaction_items: list[dict[str, Any]],
) -> None:
    first_update = transaction_items[0]["Update"]
    placeholder_type_map = _attribute_value_type_map(
        first_update.get("ExpressionAttributeValues", {})
    )
    error = exc.response.get("Error", {})
    LOGGER.warning(
        "Create-upload DynamoDB transaction request was invalid",
        extra={
            "route": "POST /api/upload-sessions",
            "lambda_request_id": lambda_request_id,
            "aws_request_id": exc.response.get("ResponseMetadata", {}).get("RequestId"),
            "aws_error_code": error.get("Code"),
            "sanitized_aws_error_message": _sanitize_aws_error_message(
                str(error.get("Message", ""))
            ),
            "action_order": ["slot", "dataset", "upload"],
            "expression_placeholder_type_map": placeholder_type_map,
            "dataset_id": dataset_id,
            "upload_id": upload_id,
        },
    )


def _create_upload_session(
    event: dict[str, Any], lambda_request_id: str | None = None
) -> dict[str, Any]:
    owner, email = _identity(event)
    dataset_name, filename, media_type, size_bytes = _validate_upload(_parse_body(event))
    s3, table, ddb_client = _clients()
    created_at = datetime.now(UTC).isoformat()
    owner_pk = f"USER#{owner}"

    for _attempt in range(5):
        slot_number = _next_owner_upload_slot(table, owner, size_bytes)
        now = int(time.time())
        pending_expires_at = now + 86400
        dataset_id = str(uuid.uuid4())
        upload_id = str(uuid.uuid4())
        staging_object_key = f"pending/users/{owner}/datasets/{dataset_id}/{upload_id}/{filename}"
        object_key = f"datasets/users/{owner}/{dataset_id}/{upload_id}/{filename}"
        slot_key = f"SLOT#{slot_number:04d}"
        slot_values = _expression_values(
            {
                ":entity": "UPLOAD_SLOT",
                ":owner": owner,
                ":slot": slot_number,
                ":dataset": dataset_id,
                ":upload": upload_id,
                ":size": size_bytes,
                ":pending": "pending",
                ":released": "released",
                ":free": "free",
                ":expired": "expired",
                ":created": created_at,
                ":updated": created_at,
                ":expires": pending_expires_at,
                ":now": now,
            }
        )
        transaction_items = [
            {
                "Update": {
                    "TableName": METADATA_TABLE,
                    "Key": _key(owner_pk, slot_key),
                    "UpdateExpression": (
                        "SET entity_type = :entity, owner_sub = :owner, "
                        "slot_number = :slot, dataset_id = :dataset, upload_id = :upload, "
                        "expected_size = :size, #status = :pending, "
                        "created_at = if_not_exists(created_at, :created), "
                        "updated_at = :updated, expires_at = :expires"
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(pk) "
                        "OR #status IN (:released, :free, :expired) "
                        "OR (#status = :pending AND expires_at <= :now)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": slot_values,
                }
            },
            {
                "Put": {
                    "TableName": METADATA_TABLE,
                    "Item": _item(
                        {
                            "pk": owner_pk,
                            "sk": f"DATASET#{dataset_id}",
                            "entity_type": "DATASET",
                            "owner_sub": owner,
                            "dataset_id": dataset_id,
                            "dataset_name": dataset_name,
                            "filename": filename,
                            "media_type": media_type,
                            "expected_size": size_bytes,
                            "staging_object_key": staging_object_key,
                            "object_key": object_key,
                            "upload_id": upload_id,
                            "slot_key": slot_key,
                            "status": "upload_pending",
                            "created_at": created_at,
                            "updated_at": created_at,
                            "expires_at": pending_expires_at,
                            "owner_email": email,
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                }
            },
            {
                "Put": {
                    "TableName": METADATA_TABLE,
                    "Item": _item(
                        {
                            "pk": owner_pk,
                            "sk": f"UPLOAD#{upload_id}",
                            "entity_type": "UPLOAD",
                            "owner_sub": owner,
                            "dataset_id": dataset_id,
                            "upload_id": upload_id,
                            "slot_key": slot_key,
                            "staging_object_key": staging_object_key,
                            "object_key": object_key,
                            "media_type": media_type,
                            "expected_size": size_bytes,
                            "status": "pending",
                            "created_at": created_at,
                            "updated_at": created_at,
                            "expires_at": pending_expires_at,
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                }
            },
        ]
        try:
            ddb_client.transact_write_items(TransactItems=transaction_items)
        except ClientError as exc:
            if _is_transaction_canceled(exc):
                _handle_create_upload_transaction_cancel(
                    exc,
                    upload_id=upload_id,
                    dataset_id=dataset_id,
                )
            if _is_validation_exception(exc):
                _log_create_upload_validation_exception(
                    exc,
                    lambda_request_id=lambda_request_id,
                    upload_id=upload_id,
                    dataset_id=dataset_id,
                    transaction_items=transaction_items,
                )
            raise
        break

    post = s3.generate_presigned_post(
        Bucket=UPLOAD_BUCKET,
        Key=staging_object_key,
        Fields={
            "Content-Type": media_type,
            "x-amz-server-side-encryption": "AES256",
        },
        Conditions=[
            {"Content-Type": media_type},
            {"x-amz-server-side-encryption": "AES256"},
            ["content-length-range", size_bytes, size_bytes],
        ],
        ExpiresIn=900,
    )
    return {
        "datasetId": dataset_id,
        "uploadId": upload_id,
        "expiresInSeconds": 900,
        "upload": post,
    }


def _complete_upload(event: dict[str, Any], upload_id: str) -> dict[str, Any]:
    owner, _ = _identity(event)
    s3, table, ddb_client = _clients()
    owner_pk = f"USER#{owner}"
    response = table.get_item(
        Key={"pk": owner_pk, "sk": f"UPLOAD#{upload_id}"},
        ConsistentRead=True,
    )
    upload = response.get("Item")
    if not isinstance(upload, dict) or upload.get("owner_sub") != owner:
        raise ApiError("upload_not_found", "Upload session does not exist", 404)
    if upload.get("status") == "completed":
        return {"datasetId": upload["dataset_id"], "uploadId": upload_id, "status": "uploaded"}
    if upload.get("status") != "pending":
        raise ApiError("upload_not_completable", "Upload session cannot be completed", 409)

    staging_object_key = upload["staging_object_key"]
    object_key = upload["object_key"]
    expected_size = int(upload["expected_size"])
    try:
        copied = s3.copy_object(
            Bucket=DATA_BUCKET,
            Key=object_key,
            CopySource={"Bucket": UPLOAD_BUCKET, "Key": staging_object_key},
            ContentType=upload["media_type"],
            Metadata={"source-upload-id": upload_id},
            MetadataDirective="REPLACE",
            ServerSideEncryption="AES256",
            Tagging="state=complete&retention=demo",
            TaggingDirective="REPLACE",
        )
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code")
        if status == 404 or code in {"NoSuchKey", "NotFound"}:
            raise ApiError("upload_missing", "Uploaded object was not found", 409) from exc
        raise

    object_version_id = copied.get("VersionId")
    if not isinstance(object_version_id, str) or not object_version_id:
        raise ApiError("upload_version_missing", "Final object version is unavailable", 409)

    final_head = s3.head_object(
        Bucket=DATA_BUCKET,
        Key=object_key,
        VersionId=object_version_id,
    )
    actual_size = int(final_head.get("ContentLength", -1))
    if actual_size != expected_size:
        try:
            s3.delete_object(
                Bucket=DATA_BUCKET,
                Key=object_key,
                VersionId=object_version_id,
            )
        except ClientError:
            LOGGER.exception(
                "Could not delete rejected final object version",
                extra={"upload_id": upload_id, "object_version_id": object_version_id},
            )
        raise ApiError(
            "upload_size_mismatch",
            "Uploaded object size does not match the declared size",
            409,
            {"expectedBytes": expected_size, "actualBytes": actual_size},
        )

    updated_at = datetime.now(UTC).isoformat()
    completed_expires_at = int(time.time()) + UPLOAD_RETENTION_DAYS * 86400
    try:
        ddb_client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, f"UPLOAD#{upload_id}"),
                        "UpdateExpression": (
                            "SET #status = :completed, updated_at = :updated, expires_at = :expires"
                        ),
                        "ConditionExpression": "owner_sub = :owner AND #status = :pending",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _expression_values(
                            {
                                ":completed": "completed",
                                ":pending": "pending",
                                ":owner": owner,
                                ":updated": updated_at,
                                ":expires": completed_expires_at,
                            }
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, f"DATASET#{upload['dataset_id']}"),
                        "UpdateExpression": (
                            "SET #status = :uploaded, updated_at = :updated, "
                            "actual_size = :size, object_key = :object_key, "
                            "object_version_id = :version, expires_at = :expires"
                        ),
                        "ConditionExpression": "owner_sub = :owner AND upload_id = :upload",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _expression_values(
                            {
                                ":uploaded": "uploaded",
                                ":owner": owner,
                                ":upload": upload_id,
                                ":updated": updated_at,
                                ":size": actual_size,
                                ":object_key": object_key,
                                ":version": object_version_id,
                                ":expires": completed_expires_at,
                            }
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, upload["slot_key"]),
                        "UpdateExpression": (
                            "SET #status = :released, updated_at = :updated, expires_at = :expires"
                        ),
                        "ConditionExpression": "owner_sub = :owner AND upload_id = :upload",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _expression_values(
                            {
                                ":released": "released",
                                ":owner": owner,
                                ":upload": upload_id,
                                ":updated": updated_at,
                                ":expires": completed_expires_at,
                            }
                        ),
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        latest = table.get_item(
            Key={"pk": owner_pk, "sk": f"UPLOAD#{upload_id}"},
            ConsistentRead=True,
        ).get("Item")
        if not isinstance(latest, dict) or latest.get("status") != "completed":
            raise
    try:
        s3.delete_object(Bucket=UPLOAD_BUCKET, Key=staging_object_key)
    except ClientError:
        LOGGER.warning(
            "Could not delete completed staging upload; lifecycle cleanup will remove it",
            extra={"upload_id": upload_id},
        )
    return {"datasetId": upload["dataset_id"], "uploadId": upload_id, "status": "uploaded"}


VALIDATION_WORKER_MAX_SECONDS = VALIDATION_JOB_TIMEOUT_SECONDS - 30

VALIDATION_LIMIT_POLICY = {
    "max_input_bytes": min(MAX_UPLOAD_BYTES, 250 * 1024**2),
    "max_rows": 500_000,
    "max_columns": 250,
    "max_string_sample_length": 512,
    "max_distinct_values": 20,
    "max_profile_rows": 5_000,
    "max_execution_seconds": VALIDATION_WORKER_MAX_SECONDS,
}
VALIDATION_LIMIT_MINIMUMS = {
    "max_input_bytes": 1,
    "max_rows": 1,
    "max_columns": 1,
    "max_string_sample_length": 16,
    "max_distinct_values": 1,
    "max_profile_rows": 1,
    "max_execution_seconds": 60,
}
VALIDATION_ACTIVE_BATCH_STATUSES = {
    "SUBMITTED": "submitted",
    "PENDING": "pending",
    "RUNNABLE": "runnable",
    "STARTING": "starting",
    "RUNNING": "running",
}
VALIDATION_TERMINAL_STATUSES = {"succeeded", "invalid", "failed"}
VALIDATION_RESULT_MAX_BYTES = 4 * 1024 * 1024
VALIDATION_SUBMISSION_GRACE_SECONDS = 120
VALIDATION_BATCH_VISIBILITY_GRACE_SECONDS = 60
VALIDATION_SLOT_LEASE_SECONDS = max(VALIDATION_JOB_TIMEOUT_SECONDS + 300, 6 * 3600)


def _canonical_uuid(value: str, *, code: str, message: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ApiError(code, message, 400) from exc
    normalized = str(parsed)
    if normalized != value:
        raise ApiError(code, message, 400)
    return normalized


def _validation_submission(payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
    allowed = {"requestToken", "limits"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ApiError("invalid_request", "Unknown request fields", 400, {"fields": unknown})
    request_token = payload.get("requestToken")
    if not isinstance(request_token, str):
        raise ApiError("invalid_request_token", "requestToken must be a UUID", 400)
    request_token = _canonical_uuid(
        request_token,
        code="invalid_request_token",
        message="requestToken must be a canonical UUID",
    )
    requested_limits = payload.get("limits", {})
    if not isinstance(requested_limits, dict):
        raise ApiError("invalid_validation_limits", "limits must be a JSON object", 400)
    unknown_limits = sorted(set(requested_limits) - set(VALIDATION_LIMIT_POLICY))
    if unknown_limits:
        raise ApiError(
            "invalid_validation_limits",
            "Unknown validation limit fields",
            400,
            {"fields": unknown_limits},
        )
    limits = dict(VALIDATION_LIMIT_POLICY)
    for name, value in requested_limits.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError(
                "invalid_validation_limits",
                "Validation limits must be integers",
                400,
                {"field": name},
            )
        if not VALIDATION_LIMIT_MINIMUMS[name] <= value <= VALIDATION_LIMIT_POLICY[name]:
            raise ApiError(
                "validation_policy_exceeded",
                "Requested validation limits exceed the server-owned policy",
                422,
                {
                    "field": name,
                    "minimum": VALIDATION_LIMIT_MINIMUMS[name],
                    "maximum": VALIDATION_LIMIT_POLICY[name],
                },
            )
        limits[name] = value
    return request_token, limits


def _validation_media_type(filename: str, stored_media_type: str) -> str:
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".parquet":
        return "application/vnd.apache.parquet"
    raise ApiError(
        "unsupported_file_type",
        "Only CSV and Parquet datasets can be validated",
        415,
        {"storedMediaType": stored_media_type},
    )


def _validation_job_id(owner: str, request_token: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonavy-validation:{owner}:{request_token}"))


def _validation_request_fingerprint(dataset_id: str, limits: dict[str, int]) -> str:
    canonical = json.dumps(
        {"dataset_id": dataset_id, "limits": limits},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validation_job_item(table: Any, owner: str, job_id: str) -> dict[str, Any] | None:
    response = table.get_item(
        Key={"pk": f"USER#{owner}", "sk": f"VALIDATION#{job_id}"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not isinstance(item, dict) or item.get("owner_sub") != owner:
        return None
    return item


def _dataset_for_validation(table: Any, owner: str, dataset_id: str) -> dict[str, Any]:
    response = table.get_item(
        Key={"pk": f"USER#{owner}", "sk": f"DATASET#{dataset_id}"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not isinstance(item, dict) or item.get("owner_sub") != owner:
        raise ApiError("dataset_not_found", "Dataset does not exist", 404)
    if item.get("status") != "uploaded":
        raise ApiError("dataset_not_ready", "Dataset upload is not complete", 409)
    required = ("object_key", "object_version_id", "filename", "media_type", "actual_size")
    if any(not item.get(name) for name in required):
        raise ApiError("dataset_state_invalid", "Dataset storage metadata is incomplete", 500)
    return item


def _validation_links(job_id: str) -> dict[str, str]:
    return {
        "status": f"/api/validations/{job_id}",
        "result": f"/api/validations/{job_id}/result",
    }


def _validation_job_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "validationJobId": item["job_id"],
        "datasetId": item["dataset_id"],
        "status": item["status"],
        "createdAt": item["created_at"],
        "updatedAt": item["updated_at"],
        "resultAvailable": bool(item.get("result_version_id")),
        "links": _validation_links(item["job_id"]),
    }
    summary = {
        "format": item.get("result_format"),
        "rowCount": item.get("row_count"),
        "columnCount": item.get("column_count"),
        "warningCount": item.get("warning_count"),
        "errorCount": item.get("error_count"),
    }
    if any(value is not None for value in summary.values()):
        payload["summary"] = summary
    if item.get("failure_code"):
        payload["failure"] = {
            "code": item["failure_code"],
            "message": item.get("failure_message", "Validation job failed"),
        }
    return payload


def _validation_request_document(
    *,
    owner: str,
    dataset: dict[str, Any],
    dataset_id: str,
    job_id: str,
    limits: dict[str, int],
    result_key: str,
    requested_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": "validation-request/v1",
        "job_id": job_id,
        "owner_id": owner,
        "dataset_id": dataset_id,
        "input": {
            "storage": "s3",
            "bucket": DATA_BUCKET,
            "key": dataset["object_key"],
            "version_id": dataset["object_version_id"],
            "media_type": _validation_media_type(dataset["filename"], dataset["media_type"]),
            "expected_size_bytes": int(dataset["actual_size"]),
        },
        "output": {
            "storage": "s3",
            "bucket": DATA_BUCKET,
            "key": result_key,
        },
        "limits": limits,
        "requested_at": requested_at,
    }


def _release_validation_slot_after_submission_failure(
    ddb_client: Any,
    *,
    owner: str,
    job_id: str,
    failure_code: str,
    failure_message: str,
) -> None:
    owner_pk = f"USER#{owner}"
    updated_at = datetime.now(UTC).isoformat()
    expires_at = int(time.time()) + UPLOAD_RETENTION_DAYS * 86400
    try:
        ddb_client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, f"VALIDATION#{job_id}"),
                        "UpdateExpression": (
                            "SET #status = :failed, updated_at = :updated, "
                            "failure_code = :code, failure_message = :message, "
                            "expires_at = :expires"
                        ),
                        "ConditionExpression": "owner_sub = :owner AND job_id = :job",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _expression_values(
                            {
                                ":failed": "failed",
                                ":updated": updated_at,
                                ":code": failure_code,
                                ":message": failure_message,
                                ":expires": expires_at,
                                ":owner": owner,
                                ":job": job_id,
                            }
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, "VALIDATION_SLOT#0000"),
                        "UpdateExpression": (
                            "SET #status = :released, updated_at = :updated, expires_at = :expires"
                        ),
                        "ConditionExpression": "owner_sub = :owner AND job_id = :job",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _expression_values(
                            {
                                ":released": "released",
                                ":updated": updated_at,
                                ":expires": expires_at,
                                ":owner": owner,
                                ":job": job_id,
                            }
                        ),
                    }
                },
            ]
        )
    except ClientError:
        LOGGER.exception(
            "Could not record validation submission failure",
            extra={"job_id": job_id, "failure_code": failure_code},
        )


def _create_validation_job(event: dict[str, Any], dataset_id: str) -> tuple[int, dict[str, Any]]:
    owner, _ = _identity(event)
    dataset_id = _canonical_uuid(
        dataset_id,
        code="dataset_not_found",
        message="Dataset does not exist",
    )
    request_token, limits = _validation_submission(_parse_body(event))
    _, table, ddb_client = _clients()
    dataset = _dataset_for_validation(table, owner, dataset_id)
    job_id = _validation_job_id(owner, request_token)
    request_fingerprint = _validation_request_fingerprint(dataset_id, limits)
    existing = _validation_job_item(table, owner, job_id)
    if existing is not None:
        if (
            existing.get("request_token") != request_token
            or existing.get("dataset_id") != dataset_id
            or existing.get("request_fingerprint") != request_fingerprint
        ):
            raise ApiError("validation_request_conflict", "requestToken is already in use", 409)
        return 200, _validation_job_payload(existing)

    created_at = datetime.now(UTC).isoformat()
    now = int(time.time())
    active_expires_at = now + VALIDATION_SLOT_LEASE_SECONDS
    record_expires_at = now + UPLOAD_RETENTION_DAYS * 86400
    owner_pk = f"USER#{owner}"
    result_key = f"validation-results/users/{owner}/datasets/{dataset_id}/jobs/{job_id}/result.json"
    request_document = _validation_request_document(
        owner=owner,
        dataset=dataset,
        dataset_id=dataset_id,
        job_id=job_id,
        limits=limits,
        result_key=result_key,
        requested_at=created_at,
    )
    request_json = json.dumps(request_document, sort_keys=True, separators=(",", ":"))
    transaction_items = [
        {
            "Update": {
                "TableName": METADATA_TABLE,
                "Key": _key(owner_pk, "VALIDATION_SLOT#0000"),
                "UpdateExpression": (
                    "SET entity_type = :entity, owner_sub = :owner, job_id = :job, "
                    "dataset_id = :dataset, #status = :active, "
                    "created_at = if_not_exists(created_at, :created), "
                    "updated_at = :updated, expires_at = :expires"
                ),
                "ConditionExpression": (
                    "attribute_not_exists(pk) OR #status = :released OR expires_at <= :now"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": _expression_values(
                    {
                        ":entity": "VALIDATION_SLOT",
                        ":owner": owner,
                        ":job": job_id,
                        ":dataset": dataset_id,
                        ":active": "active",
                        ":released": "released",
                        ":created": created_at,
                        ":updated": created_at,
                        ":expires": active_expires_at,
                        ":now": now,
                    }
                ),
            }
        },
        {
            "Put": {
                "TableName": METADATA_TABLE,
                "Item": _item(
                    {
                        "pk": owner_pk,
                        "sk": f"VALIDATION#{job_id}",
                        "entity_type": "VALIDATION_JOB",
                        "owner_sub": owner,
                        "job_id": job_id,
                        "request_token": request_token,
                        "request_fingerprint": request_fingerprint,
                        "request_json": request_json,
                        "dataset_id": dataset_id,
                        "input_object_key": dataset["object_key"],
                        "input_version_id": dataset["object_version_id"],
                        "status": "submitting",
                        "result_key": result_key,
                        "created_at": created_at,
                        "updated_at": created_at,
                        "expires_at": record_expires_at,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
            }
        },
    ]
    try:
        ddb_client.transact_write_items(TransactItems=transaction_items)
    except ClientError as exc:
        if _is_transaction_canceled(exc):
            existing = _validation_job_item(table, owner, job_id)
            if (
                existing is not None
                and existing.get("request_token") == request_token
                and existing.get("request_fingerprint") == request_fingerprint
            ):
                return 200, _validation_job_payload(existing)
            codes = _transaction_cancel_codes(exc)
            if codes and codes[0] == "ConditionalCheckFailed":
                raise ApiError(
                    "validation_capacity_exceeded",
                    "Another validation job is already active for this account",
                    429,
                    {"maximumActiveJobs": VALIDATION_MAX_ACTIVE_JOBS_PER_OWNER},
                ) from exc
        raise

    batch = _batch_client()
    try:
        submitted = batch.submit_job(
            jobName=f"vonavy-validation-{job_id}",
            jobQueue=VALIDATION_JOB_QUEUE,
            jobDefinition=VALIDATION_JOB_DEFINITION,
            containerOverrides={
                "environment": [
                    {
                        "name": "VONAVY_VALIDATION_REQUEST_JSON",
                        "value": request_json,
                    }
                ]
            },
            tags={"project": "vonavy-agent", "phase": "2b", "validationJobId": job_id},
            propagateTags=True,
        )
    except ClientError as exc:
        _release_validation_slot_after_submission_failure(
            ddb_client,
            owner=owner,
            job_id=job_id,
            failure_code="batch_submit_failed",
            failure_message="AWS Batch rejected the validation job",
        )
        raise ApiError(
            "validation_submission_failed",
            "Validation job could not be submitted",
            503,
            {"requestId": exc.response.get("ResponseMetadata", {}).get("RequestId")},
        ) from exc

    batch_job_id = submitted.get("jobId")
    if not isinstance(batch_job_id, str) or not batch_job_id:
        _release_validation_slot_after_submission_failure(
            ddb_client,
            owner=owner,
            job_id=job_id,
            failure_code="batch_job_id_missing",
            failure_message="AWS Batch returned no job identifier",
        )
        raise ApiError("validation_submission_failed", "Validation job could not be submitted", 503)
    try:
        table.update_item(
            Key={"pk": owner_pk, "sk": f"VALIDATION#{job_id}"},
            UpdateExpression=(
                "SET #status = :submitted, batch_job_id = :batch, updated_at = :updated"
            ),
            ConditionExpression="owner_sub = :owner AND #status = :submitting",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":submitted": "submitted",
                ":batch": batch_job_id,
                ":updated": datetime.now(UTC).isoformat(),
                ":owner": owner,
                ":submitting": "submitting",
            },
        )
    except ClientError:
        try:
            batch.terminate_job(
                jobId=batch_job_id,
                reason="Validation metadata publication failed",
            )
        except ClientError:
            LOGGER.exception(
                "Could not terminate validation job after metadata failure",
                extra={"job_id": job_id, "batch_job_id": batch_job_id},
            )
        _release_validation_slot_after_submission_failure(
            ddb_client,
            owner=owner,
            job_id=job_id,
            failure_code="metadata_publication_failed",
            failure_message="Validation metadata could not be published",
        )
        raise

    created = _validation_job_item(table, owner, job_id)
    if created is None:
        raise ApiError("validation_state_invalid", "Validation job metadata is unavailable", 500)
    return 202, _validation_job_payload(created)


def _read_validation_result(
    s3: Any,
    item: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    request: dict[str, Any] = {"Bucket": DATA_BUCKET, "Key": item["result_key"]}
    version_id = item.get("result_version_id")
    if isinstance(version_id, str) and version_id:
        request["VersionId"] = version_id
    try:
        response = s3.get_object(**request)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"NoSuchKey", "NoSuchVersion", "NotFound"} or status == 404:
            return None
        raise
    body = response["Body"]
    try:
        payload = body.read(VALIDATION_RESULT_MAX_BYTES + 1)
    finally:
        body.close()
    if len(payload) > VALIDATION_RESULT_MAX_BYTES:
        raise ApiError("validation_result_too_large", "Validation result exceeds policy", 500)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("validation_result_invalid", "Validation result is malformed", 500) from exc
    if not isinstance(parsed, dict):
        raise ApiError("validation_result_invalid", "Validation result is malformed", 500)
    if parsed.get("schema_version") != "validation-result/v1":
        raise ApiError("validation_result_invalid", "Validation result schema is invalid", 500)
    if parsed.get("job_id") != item["job_id"] or parsed.get("dataset_id") != item["dataset_id"]:
        raise ApiError("validation_result_invalid", "Validation result identity is invalid", 500)
    if parsed.get("status") not in VALIDATION_TERMINAL_STATUSES:
        raise ApiError("validation_result_invalid", "Validation result status is invalid", 500)
    result_version = response.get("VersionId")
    if not isinstance(result_version, str) or not result_version:
        raise ApiError("validation_result_invalid", "Validation result version is unavailable", 500)
    return parsed, result_version


def _batch_failure(batch_job: dict[str, Any]) -> tuple[str, str]:
    container = batch_job.get("container")
    reason = batch_job.get("statusReason")
    if isinstance(container, dict):
        reason = container.get("reason") or reason
        exit_code = container.get("exitCode")
        if exit_code is not None:
            reason = f"{reason or 'Container failed'} (exit {exit_code})"
    return "batch_job_failed", _sanitize_aws_error_message(str(reason or "AWS Batch job failed"))


def _terminalize_validation_job(
    ddb_client: Any,
    table: Any,
    *,
    owner: str,
    item: dict[str, Any],
    status: str,
    result: dict[str, Any] | None,
    result_version_id: str | None,
    failure: tuple[str, str] | None = None,
) -> dict[str, Any]:
    updated_at = datetime.now(UTC).isoformat()
    expires_at = int(time.time()) + UPLOAD_RETENTION_DAYS * 86400
    values: dict[str, Any] = {
        ":status": status,
        ":updated": updated_at,
        ":expires": expires_at,
        ":owner": owner,
        ":job": item["job_id"],
        ":released": "released",
    }
    assignments = ["#status = :status", "updated_at = :updated", "expires_at = :expires"]
    if result_version_id is not None:
        values[":version"] = result_version_id
        assignments.append("result_version_id = :version")
    if result is not None:
        warnings = result.get("warnings")
        validation_errors = result.get("validation_errors")
        summary_values = {
            "result_format": result.get("format"),
            "row_count": result.get("row_count"),
            "column_count": result.get("column_count"),
            "warning_count": len(warnings) if isinstance(warnings, list) else 0,
            "error_count": (len(validation_errors) if isinstance(validation_errors, list) else 0),
        }
        for name, value in summary_values.items():
            if value is not None:
                reference = f":{name}"
                values[reference] = value
                assignments.append(f"{name} = {reference}")
    if failure is not None:
        values[":failure_code"] = failure[0]
        values[":failure_message"] = failure[1]
        assignments.extend(["failure_code = :failure_code", "failure_message = :failure_message"])
    owner_pk = f"USER#{owner}"
    job_values = {name: value for name, value in values.items() if name != ":released"}
    slot_values = {
        ":released": "released",
        ":updated": updated_at,
        ":expires": expires_at,
        ":owner": owner,
        ":job": item["job_id"],
    }
    try:
        ddb_client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, f"VALIDATION#{item['job_id']}"),
                        "UpdateExpression": "SET " + ", ".join(assignments),
                        "ConditionExpression": "owner_sub = :owner AND job_id = :job",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _expression_values(job_values),
                    }
                },
                {
                    "Update": {
                        "TableName": METADATA_TABLE,
                        "Key": _key(owner_pk, "VALIDATION_SLOT#0000"),
                        "UpdateExpression": (
                            "SET #status = :released, updated_at = :updated, expires_at = :expires"
                        ),
                        "ConditionExpression": "owner_sub = :owner AND job_id = :job",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _expression_values(slot_values),
                    }
                },
            ]
        )
    except ClientError as exc:
        if not _is_transaction_canceled(exc):
            raise
        latest = _validation_job_item(table, owner, item["job_id"])
        if latest is None or latest.get("status") not in VALIDATION_TERMINAL_STATUSES:
            raise
        return latest
    latest = _validation_job_item(table, owner, item["job_id"])
    if latest is None:
        raise ApiError("validation_state_invalid", "Validation job metadata is unavailable", 500)
    return latest


def _age_seconds(timestamp: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()


def _reconcile_validation_job(
    owner: str, job_id: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    s3, table, ddb_client = _clients()
    item = _validation_job_item(table, owner, job_id)
    if item is None:
        raise ApiError("validation_not_found", "Validation job does not exist", 404)
    if item.get("status") in VALIDATION_TERMINAL_STATUSES:
        stored = _read_validation_result(s3, item) if item.get("result_version_id") else None
        return item, stored[0] if stored else None
    batch_job_id = item.get("batch_job_id")
    if not isinstance(batch_job_id, str) or not batch_job_id:
        if (
            item.get("status") == "submitting"
            and _age_seconds(item.get("updated_at")) >= VALIDATION_SUBMISSION_GRACE_SECONDS
        ):
            terminal = _terminalize_validation_job(
                ddb_client,
                table,
                owner=owner,
                item=item,
                status="failed",
                result=None,
                result_version_id=None,
                failure=(
                    "validation_submission_interrupted",
                    "Validation submission did not publish an AWS Batch job identifier",
                ),
            )
            return terminal, None
        return item, None
    described = _batch_client().describe_jobs(jobs=[batch_job_id]).get("jobs", [])
    if not described:
        if _age_seconds(item.get("updated_at")) < VALIDATION_BATCH_VISIBILITY_GRACE_SECONDS:
            return item, None
        terminal = _terminalize_validation_job(
            ddb_client,
            table,
            owner=owner,
            item=item,
            status="failed",
            result=None,
            result_version_id=None,
            failure=("batch_job_missing", "AWS Batch no longer returns the submitted job"),
        )
        return terminal, None
    batch_job = described[0]
    batch_status_value = batch_job.get("status")
    batch_status = batch_status_value if isinstance(batch_status_value, str) else "UNKNOWN"
    if batch_status in VALIDATION_ACTIVE_BATCH_STATUSES:
        public_status = VALIDATION_ACTIVE_BATCH_STATUSES[batch_status]
        if item.get("status") != public_status:
            table.update_item(
                Key={"pk": f"USER#{owner}", "sk": f"VALIDATION#{job_id}"},
                UpdateExpression="SET #status = :status, updated_at = :updated",
                ConditionExpression="owner_sub = :owner AND job_id = :job",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": public_status,
                    ":updated": datetime.now(UTC).isoformat(),
                    ":owner": owner,
                    ":job": job_id,
                },
            )
            item = _validation_job_item(table, owner, job_id) or item
        return item, None

    try:
        stored_result = _read_validation_result(s3, item)
    except ApiError as exc:
        if not exc.code.startswith("validation_result_"):
            raise
        terminal = _terminalize_validation_job(
            ddb_client,
            table,
            owner=owner,
            item=item,
            status="failed",
            result=None,
            result_version_id=None,
            failure=(exc.code, exc.message),
        )
        return terminal, None
    if stored_result is not None:
        result, result_version_id = stored_result
        result_status = str(result["status"])
        failure = None
        if result_status == "failed":
            errors = result.get("validation_errors", [])
            first_error = errors[0] if isinstance(errors, list) and errors else {}
            failure = (
                str(first_error.get("code", "validation_worker_failed")),
                str(first_error.get("message", "Validation worker failed")),
            )
        terminal = _terminalize_validation_job(
            ddb_client,
            table,
            owner=owner,
            item=item,
            status=result_status,
            result=result,
            result_version_id=result_version_id,
            failure=failure,
        )
        return terminal, result
    failure = _batch_failure(batch_job)
    if batch_status == "SUCCEEDED":
        failure = ("validation_result_missing", "Validation worker published no result")
    terminal = _terminalize_validation_job(
        ddb_client,
        table,
        owner=owner,
        item=item,
        status="failed",
        result=None,
        result_version_id=None,
        failure=failure,
    )
    return terminal, None


def _get_validation_job(event: dict[str, Any], job_id: str) -> dict[str, Any]:
    owner, _ = _identity(event)
    job_id = _canonical_uuid(
        job_id,
        code="validation_not_found",
        message="Validation job does not exist",
    )
    item, _ = _reconcile_validation_job(owner, job_id)
    return _validation_job_payload(item)


def _get_validation_result(event: dict[str, Any], job_id: str) -> dict[str, Any]:
    owner, _ = _identity(event)
    job_id = _canonical_uuid(
        job_id,
        code="validation_not_found",
        message="Validation job does not exist",
    )
    item, result = _reconcile_validation_job(owner, job_id)
    if item.get("status") not in VALIDATION_TERMINAL_STATUSES:
        raise ApiError(
            "validation_not_complete",
            "Validation job has not reached a terminal state",
            409,
            {"status": item.get("status")},
        )
    if result is None and item.get("result_version_id"):
        stored = _read_validation_result(_clients()[0], item)
        result = stored[0] if stored else None
    if result is None:
        raise ApiError(
            "validation_result_unavailable",
            "Validation job produced no result artifact",
            409,
            {"status": item.get("status")},
        )
    return result


def _list_datasets(event: dict[str, Any]) -> dict[str, Any]:
    owner, _ = _identity(event)
    _, table, _ = _clients()
    response = table.query(
        KeyConditionExpression=Key("pk").eq(f"USER#{owner}") & Key("sk").begins_with("DATASET#"),
        ConsistentRead=True,
    )
    items = response.get("Items", [])
    datasets = [
        {
            "datasetId": item["dataset_id"],
            "name": item["dataset_name"],
            "filename": item["filename"],
            "mediaType": item["media_type"],
            "sizeBytes": int(item.get("actual_size", item["expected_size"])),
            "status": item["status"],
            "createdAt": item["created_at"],
            "updatedAt": item["updated_at"],
        }
        for item in items
        if item.get("owner_sub") == owner
    ]
    datasets.sort(key=lambda item: item["createdAt"], reverse=True)
    return {"datasets": datasets}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    lambda_request_id = getattr(context, "aws_request_id", None)
    try:
        method, path = _route(event)
        if method == "GET" and path == "/api/health":
            return _json_response(200, {"status": "ok", "service": "vonavy-agent-control-plane"})
        if method == "POST" and path == "/api/upload-sessions":
            return _json_response(201, _create_upload_session(event, lambda_request_id))
        if (
            method == "POST"
            and path.startswith("/api/upload-sessions/")
            and path.endswith("/complete")
        ):
            upload_id = path.removeprefix("/api/upload-sessions/").removesuffix("/complete")
            try:
                parsed_upload_id = uuid.UUID(upload_id)
            except ValueError as exc:
                raise ApiError("upload_not_found", "Upload session does not exist", 404) from exc
            if str(parsed_upload_id) != upload_id:
                raise ApiError("upload_not_found", "Upload session does not exist", 404)
            return _json_response(200, _complete_upload(event, upload_id))
        if method == "GET" and path == "/api/datasets":
            return _json_response(200, _list_datasets(event))
        if method == "POST" and path.startswith("/api/datasets/") and path.endswith("/validations"):
            dataset_id = path.removeprefix("/api/datasets/").removesuffix("/validations")
            status_code, payload = _create_validation_job(event, dataset_id)
            return _json_response(status_code, payload)
        if method == "GET" and path.startswith("/api/validations/"):
            validation_path = path.removeprefix("/api/validations/")
            if validation_path.endswith("/result"):
                job_id = validation_path.removesuffix("/result")
                return _json_response(200, _get_validation_result(event, job_id))
            return _json_response(200, _get_validation_job(event, validation_path))
        raise ApiError("route_not_found", "Route does not exist", 404)
    except ApiError as exc:
        return _json_response(
            exc.status_code,
            {"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
        )
    except ClientError as exc:
        LOGGER.exception("AWS dependency rejected a control-plane request")
        request_id = exc.response.get("ResponseMetadata", {}).get("RequestId")
        return _json_response(
            503,
            {
                "error": {
                    "code": "aws_service_error",
                    "message": "A dependent AWS service rejected the request",
                    "detail": {"requestId": request_id},
                }
            },
        )
    except Exception:
        LOGGER.exception("Unhandled control-plane exception")
        return _json_response(
            500,
            {
                "error": {
                    "code": "internal_error",
                    "message": "Unexpected server error",
                    "detail": None,
                }
            },
        )
