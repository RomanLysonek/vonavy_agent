from __future__ import annotations

import base64
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
AWS_REGION_NAME = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_REGION", "eu-central-1"))

if MAX_UPLOAD_BYTES < 1 or MAX_DATASETS_PER_OWNER < 1:
    raise RuntimeError("Upload policy limits must be positive")
if MAX_UPLOAD_BYTES * MAX_DATASETS_PER_OWNER > MAX_TOTAL_BYTES_PER_OWNER:
    raise RuntimeError("MAX_TOTAL_BYTES_PER_OWNER must cover every server-owned upload slot")

OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

ALLOWED_MEDIA_TYPES = {
    ".csv": {"text/csv", "application/csv", "application/vnd.ms-excel"},
    ".parquet": {"application/vnd.apache.parquet", "application/octet-stream"},
}

_s3: Any | None = None
_table: Any | None = None
_SERIALIZER = TypeSerializer()
_ATTRIBUTE_VALUE_TYPES = {"S", "N", "B", "SS", "NS", "BS", "M", "L", "NULL", "BOOL"}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


def _clients() -> tuple[Any, Any]:
    global _s3, _table
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION_NAME)
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=AWS_REGION_NAME).Table(METADATA_TABLE)
    return _s3, _table


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
    s3, table = _clients()
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
            table.meta.client.transact_write_items(TransactItems=transaction_items)
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
    s3, table = _clients()
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
        table.meta.client.transact_write_items(
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


def _list_datasets(event: dict[str, Any]) -> dict[str, Any]:
    owner, _ = _identity(event)
    _, table = _clients()
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
