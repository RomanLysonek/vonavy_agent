from __future__ import annotations

import os

import aws_cdk as cdk

from vonavy_infra.control_plane_stack import ControlPlaneStack, DeploymentConfig


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


app = cdk.App()
config = DeploymentConfig(
    environment_name=os.getenv("VONAVY_ENVIRONMENT", "dev"),
    max_upload_bytes=int(os.getenv("VONAVY_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))),
    max_datasets_per_owner=int(os.getenv("VONAVY_MAX_DATASETS_PER_OWNER", "10")),
    max_total_bytes_per_owner=int(
        os.getenv("VONAVY_MAX_TOTAL_BYTES_PER_OWNER", str(1024 * 1024 * 1024))
    ),
    upload_retention_days=int(os.getenv("VONAVY_UPLOAD_RETENTION_DAYS", "14")),
    protect_data=_boolean("VONAVY_PROTECT_DATA", True),
    local_callback_url=os.getenv("VONAVY_LOCAL_CALLBACK_URL", "http://localhost:5173/"),
)

ControlPlaneStack(
    app,
    f"VonavyAgent-{config.environment_name}-ControlPlane",
    config=config,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION", "eu-central-1"),
    ),
    description="Serverless authenticated control plane for vonavy-agent",
)
app.synth()
