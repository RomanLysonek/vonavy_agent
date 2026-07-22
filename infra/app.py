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


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


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
    validation_job_timeout_seconds=int(os.getenv("VONAVY_VALIDATION_JOB_TIMEOUT_SECONDS", "900")),
    validation_max_active_jobs_per_owner=int(
        os.getenv("VONAVY_VALIDATION_MAX_ACTIVE_JOBS_PER_OWNER", "1")
    ),
    source_revision=os.getenv("VONAVY_SOURCE_REVISION", "unknown"),
    public_domain_name=_optional("VONAVY_PUBLIC_DOMAIN_NAME"),
    public_hosted_zone_id=_optional("VONAVY_PUBLIC_HOSTED_ZONE_ID"),
    public_certificate_arn=_optional("VONAVY_PUBLIC_CERTIFICATE_ARN"),
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
