from __future__ import annotations

import json
import uuid
from typing import Any

AGENT_ASYNC_SCHEMA_VERSION = "forecast-agent-async-turn/v1"
AGENT_ASYNC_SOURCE = "vonavy.agent-turn"
AGENT_ACTIVE_TURN_STATUSES = frozenset({"queued", "processing"})
_AGENT_ID_NAMESPACE = uuid.UUID("be09988a-1d1d-4f9d-8f86-48e10ce8860d")


class AgentAsyncValueError(ValueError):
    pass


def clean_agent_message(value: object) -> str:
    if not isinstance(value, str):
        raise AgentAsyncValueError("message must be text")
    clean = " ".join(value.replace("\\x00", " ").split())
    if not clean:
        raise AgentAsyncValueError("message is required")
    if len(clean) > 2_000:
        raise AgentAsyncValueError("message exceeds 2000 characters")
    return clean


def canonical_request_token(value: object) -> str:
    if not isinstance(value, str):
        raise AgentAsyncValueError("requestToken must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise AgentAsyncValueError("requestToken must be a UUID") from exc
    if parsed.version != 4:
        raise AgentAsyncValueError("requestToken must be a UUIDv4")
    return str(parsed)


def session_id_for(owner: str, dataset_id: str, request_token: str) -> str:
    return str(uuid.uuid5(_AGENT_ID_NAMESPACE, f"session:{owner}:{dataset_id}:{request_token}"))


def turn_id_for(session_id: str, request_token: str) -> str:
    return str(uuid.uuid5(_AGENT_ID_NAMESPACE, f"turn:{session_id}:{request_token}"))


def invocation_payload(owner: str, session_id: str, turn_id: str) -> bytes:
    return json.dumps(
        {
            "schemaVersion": AGENT_ASYNC_SCHEMA_VERSION,
            "source": AGENT_ASYNC_SOURCE,
            "owner": owner,
            "sessionId": session_id,
            "turnId": turn_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def turn_view(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("turn_status")
    if not isinstance(status, str):
        status = "succeeded" if int(item.get("turn_count", 0)) else "idle"
    turn_id = item.get("turn_id")
    error = None
    if status == "failed":
        error = {
            "code": str(item.get("turn_error_code") or "agent_turn_failed"),
            "message": str(
                item.get("turn_error_message") or "The agent turn could not be completed."
            ),
        }
    return {
        "id": turn_id if isinstance(turn_id, str) else None,
        "status": status,
        "pending": status in AGENT_ACTIVE_TURN_STATUSES,
        "submittedAt": item.get("turn_submitted_at"),
        "startedAt": item.get("turn_started_at"),
        "completedAt": item.get("turn_completed_at"),
        "error": error,
    }
