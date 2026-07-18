from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from vonavy_agent.errors import AgentError

LOCAL_OWNER_ID = "local"
OWNER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


@dataclass(frozen=True, slots=True)
class IdentityContext:
    """Authenticated principal used for every owner-scoped operation."""

    owner_id: str
    subject: str
    authentication_mode: str

    def __post_init__(self) -> None:
        if not OWNER_ID_PATTERN.fullmatch(self.owner_id):
            raise ValueError("owner_id must be a stable safe identifier")
        if not self.subject:
            raise ValueError("subject must not be empty")


class IdentityProvider(Protocol):
    """Resolve request metadata into a trusted identity.

    Cloud adapters can validate API Gateway/Cognito claims and return the Cognito
    subject. The local adapter intentionally maps every request to one fixed owner.
    """

    def resolve(self, headers: Mapping[str, str]) -> IdentityContext: ...


class LocalIdentityProvider:
    def __init__(self, owner_id: str = LOCAL_OWNER_ID) -> None:
        try:
            self._identity = IdentityContext(
                owner_id=owner_id,
                subject=owner_id,
                authentication_mode="local",
            )
        except ValueError as exc:
            raise AgentError("invalid_local_owner", str(exc), status_code=500) from exc

    def resolve(self, headers: Mapping[str, str]) -> IdentityContext:
        del headers
        return self._identity
