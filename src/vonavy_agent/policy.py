from __future__ import annotations

from dataclasses import dataclass

from vonavy_agent.domain import ResourceLimits
from vonavy_agent.errors import AgentError


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """Server-owned resource ceilings applied to untrusted run specifications."""

    max_rows: int
    max_entities: int
    max_origins: int
    max_models: int
    max_wall_seconds: int
    max_memory_mb: int

    def validate(self, requested: ResourceLimits) -> None:
        checks = {
            "max_rows": (requested.max_rows, self.max_rows),
            "max_entities": (requested.max_entities, self.max_entities),
            "max_origins": (requested.max_origins, self.max_origins),
            "max_models": (requested.max_models, self.max_models),
            "wall_seconds": (requested.wall_seconds, self.max_wall_seconds),
            "memory_mb": (requested.memory_mb, self.max_memory_mb),
        }
        exceeded = {
            name: {"requested": requested_value, "allowed": allowed_value}
            for name, (requested_value, allowed_value) in checks.items()
            if requested_value > allowed_value
        }
        if exceeded:
            raise AgentError(
                "resource_policy_exceeded",
                "Requested experiment resources exceed server policy",
                detail={"limits": exceeded},
                status_code=422,
            )
