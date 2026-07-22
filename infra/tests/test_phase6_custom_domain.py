from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from vonavy_infra.control_plane_stack import ControlPlaneStack, DeploymentConfig

DOMAIN = "vonava-predikce.fun"
ZONE_ID = "Z0123456789EXAMPLE"
CERTIFICATE_ARN = (
    "arn:aws:acm:us-east-1:111122223333:certificate/00000000-0000-4000-8000-000000000001"
)


def _config(**overrides: object) -> DeploymentConfig:
    values: dict[str, object] = {
        "environment_name": "test",
        "max_upload_bytes": 100 * 1024 * 1024,
        "max_datasets_per_owner": 10,
        "max_total_bytes_per_owner": 1024 * 1024 * 1024,
        "upload_retention_days": 14,
        "protect_data": True,
        "local_callback_url": "http://localhost:5173/",
        "source_revision": "0123456789abcdef0123456789abcdef01234567",
    }
    values.update(overrides)
    return DeploymentConfig(**values)  # type: ignore[arg-type]


def _template(config: DeploymentConfig) -> Template:
    app = cdk.App()
    stack = ControlPlaneStack(
        app,
        "TestControlPlane",
        config=config,
        env=cdk.Environment(account="111122223333", region="eu-central-1"),
    )
    return Template.from_stack(stack)


def _serialized_resources(template: Template, resource_type: str) -> str:
    return json.dumps(template.find_resources(resource_type), sort_keys=True)


def test_custom_domain_is_optional_and_all_or_none() -> None:
    template = _template(_config())
    template.resource_count_is("AWS::Route53::RecordSet", 0)

    with pytest.raises(ValueError, match="must be configured together"):
        _config(public_domain_name=DOMAIN)
    with pytest.raises(ValueError, match="lowercase DNS name"):
        _config(
            public_domain_name="Vonava-Predikce.fun",
            public_hosted_zone_id=ZONE_ID,
            public_certificate_arn=CERTIFICATE_ARN,
        )
    with pytest.raises(ValueError, match="us-east-1"):
        _config(
            public_domain_name=DOMAIN,
            public_hosted_zone_id=ZONE_ID,
            public_certificate_arn=CERTIFICATE_ARN.replace("us-east-1", "eu-central-1"),
        )


def test_custom_domain_configures_cloudfront_and_route53_aliases() -> None:
    template = _template(
        _config(
            public_domain_name=DOMAIN,
            public_hosted_zone_id=ZONE_ID,
            public_certificate_arn=CERTIFICATE_ARN,
        )
    )
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": {
                "Aliases": [DOMAIN, f"www.{DOMAIN}"],
                "ViewerCertificate": {
                    "AcmCertificateArn": CERTIFICATE_ARN,
                    "MinimumProtocolVersion": "TLSv1.2_2021",
                    "SslSupportMethod": "sni-only",
                },
            }
        },
    )
    template.resource_count_is("AWS::Route53::RecordSet", 4)
    records = template.find_resources("AWS::Route53::RecordSet")
    record_text = json.dumps(records, sort_keys=True)
    assert record_text.count('"Type": "A"') == 2
    assert record_text.count('"Type": "AAAA"') == 2
    assert DOMAIN in record_text
    assert "www" in record_text
    assert "WebDistribution" in record_text


def test_custom_domain_is_allowed_by_cognito_api_and_upload_cors() -> None:
    template = _template(
        _config(
            public_domain_name=DOMAIN,
            public_hosted_zone_id=ZONE_ID,
            public_certificate_arn=CERTIFICATE_ARN,
        )
    )
    client_text = _serialized_resources(template, "AWS::Cognito::UserPoolClient")
    assert f"https://{DOMAIN}/" in client_text
    assert f"https://www.{DOMAIN}/" in client_text
    assert "localhost:5173" in client_text
    assert "WebDistribution" in client_text

    api_text = _serialized_resources(template, "AWS::ApiGatewayV2::Api")
    assert f"https://{DOMAIN}" in api_text
    assert f"https://www.{DOMAIN}" in api_text
    assert "localhost:5173" in api_text
    assert "WebDistribution" in api_text

    buckets = template.find_resources("AWS::S3::Bucket")
    cors_buckets = [
        resource
        for resource in buckets.values()
        if "CorsConfiguration" in resource.get("Properties", {})
    ]
    assert len(cors_buckets) == 1
    cors_text = json.dumps(cors_buckets[0], sort_keys=True)
    assert f"https://{DOMAIN}" in cors_text
    assert f"https://www.{DOMAIN}" in cors_text
    assert "localhost:5173" in cors_text
    assert "WebDistribution" in cors_text


def test_custom_domain_does_not_change_api_authority() -> None:
    template = _template(
        _config(
            public_domain_name=DOMAIN,
            public_hosted_zone_id=ZONE_ID,
            public_certificate_arn=CERTIFICATE_ARN,
        )
    )
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 14
    for route in routes.values():
        properties = route["Properties"]
        assert properties["AuthorizationType"] == "JWT"
        assert properties["AuthorizationScopes"] == ["vonavy-agent/api"]
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {
            "CorsConfiguration": {
                "AllowOrigins": Match.array_with([f"https://{DOMAIN}", f"https://www.{DOMAIN}"])
            }
        },
    )
