from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from vonavy_infra.control_plane_stack import ControlPlaneStack, DeploymentConfig


def _template() -> Template:
    app = cdk.App()
    stack = ControlPlaneStack(
        app,
        "TestControlPlane",
        config=DeploymentConfig(
            environment_name="test",
            max_upload_bytes=100 * 1024 * 1024,
            max_datasets_per_owner=10,
            max_total_bytes_per_owner=1024 * 1024 * 1024,
            upload_retention_days=14,
            protect_data=True,
            local_callback_url="http://localhost:5173/",
        ),
        env=cdk.Environment(account="111122223333", region="eu-central-1"),
    )
    return Template.from_stack(stack)


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _policy_statements(template: Template) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        policy_statements = policy["Properties"]["PolicyDocument"]["Statement"]
        statements.extend(policy_statements)
    return statements


def _actions(statement: dict[str, Any]) -> set[str]:
    actions = statement.get("Action", [])
    if isinstance(actions, str):
        return {actions}
    return set(actions)


def _resource_mentions(statement: dict[str, Any], fragment: str) -> bool:
    return fragment in _json_text(statement.get("Resource"))


def _resource_by_logical_id_prefix(
    template: Template, resource_type: str, prefix: str
) -> dict[str, Any]:
    matches = {
        logical_id: resource
        for logical_id, resource in template.find_resources(resource_type).items()
        if logical_id.startswith(prefix)
    }
    assert len(matches) == 1, matches.keys()
    return next(iter(matches.values()))


def test_stack_is_serverless_and_has_no_network_compute() -> None:
    template = _template()
    template.resource_count_is("AWS::EC2::Instance", 0)
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::RDS::DBInstance", 0)
    template.resource_count_is("AWS::SageMaker::Endpoint", 0)
    template.resource_count_is("AWS::Batch::ComputeEnvironment", 0)
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.resource_count_is("AWS::Cognito::UserPool", 1)
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::S3::Bucket", 3)


def test_storage_is_private_encrypted_and_pay_per_request() -> None:
    template = _template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "SSESpecification": {"SSEEnabled": True},
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
        },
    )
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
        },
    )


def test_cloudfront_sets_explicit_security_headers() -> None:
    template = _template()
    template.has_resource_properties(
        "AWS::CloudFront::ResponseHeadersPolicy",
        {
            "ResponseHeadersPolicyConfig": {
                "SecurityHeadersConfig": {
                    "ContentSecurityPolicy": {
                        "ContentSecurityPolicy": Match.string_like_regexp(
                            ".*frame-ancestors 'none'.*"
                        ),
                        "Override": True,
                    },
                    "FrameOptions": {"FrameOption": "DENY", "Override": True},
                    "ContentTypeOptions": {"Override": True},
                }
            }
        },
    )


def test_cognito_disables_public_registration_and_api_uses_jwt() -> None:
    template = _template()
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
            "MfaConfiguration": "OPTIONAL",
        },
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "IdentitySource": ["$request.header.Authorization"],
            "JwtConfiguration": {
                "Audience": [Match.any_value()],
                "Issuer": Match.any_value(),
            },
        },
    )

    authorizers = template.find_resources("AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    properties = next(iter(authorizers.values()))["Properties"]
    jwt_configuration = properties["JwtConfiguration"]

    audience_text = _json_text(jwt_configuration["Audience"])
    issuer_text = _json_text(jwt_configuration["Issuer"])

    assert "UserPoolWebClient" in audience_text
    assert "cognito-idp.eu-central-1" in issuer_text
    assert "UserPool" in issuer_text


def test_api_stage_is_throttled_and_access_logged() -> None:
    template = _template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "AutoDeploy": True,
            "DefaultRouteSettings": {
                "ThrottlingBurstLimit": 5,
                "ThrottlingRateLimit": 2,
            },
            "AccessLogSettings": {
                "DestinationArn": Match.any_value(),
                "Format": Match.string_like_regexp(r".*\$context\.requestId.*"),
            },
        },
    )


def test_lambda_separates_staging_and_immutable_data_permissions() -> None:
    statements = _policy_statements(_template())
    staging_statements = [
        statement for statement in statements if _resource_mentions(statement, "pending/users/*")
    ]
    data_statements = [
        statement for statement in statements if _resource_mentions(statement, "datasets/users/*")
    ]

    assert len(staging_statements) == 1
    assert len(data_statements) == 1

    staging_statement = staging_statements[0]
    data_statement = data_statements[0]

    assert staging_statement["Effect"] == "Allow"
    assert {
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:PutObject",
    } <= _actions(staging_statement)

    assert data_statement["Effect"] == "Allow"
    assert {
        "s3:DeleteObjectVersion",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:PutObject",
        "s3:PutObjectTagging",
    } <= _actions(data_statement)


def test_lambda_uses_unreserved_concurrency_and_upload_policy() -> None:
    template = _template()
    functions = template.find_resources("AWS::Lambda::Function")
    control_plane_functions = [
        resource
        for logical_id, resource in functions.items()
        if logical_id.startswith("ControlPlaneFunction")
    ]
    assert len(control_plane_functions) == 1
    control_plane_function = control_plane_functions[0]

    assert "ReservedConcurrentExecutions" not in control_plane_function["Properties"]
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "MemorySize": 256,
            "Timeout": 15,
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "UPLOAD_BUCKET": Match.any_value(),
                        "DATA_BUCKET": Match.any_value(),
                        "MAX_UPLOAD_BYTES": str(100 * 1024 * 1024),
                        "MAX_DATASETS_PER_OWNER": "10",
                        "MAX_TOTAL_BYTES_PER_OWNER": str(1024 * 1024 * 1024),
                        "UPLOAD_RETENTION_DAYS": "14",
                    }
                )
            },
        },
    )


def test_durable_resources_are_rollback_safe_and_retained_after_create() -> None:
    template = _template()
    resources = [
        ("AWS::S3::Bucket", "UploadBucket"),
        ("AWS::S3::Bucket", "DataBucket"),
        ("AWS::DynamoDB::Table", "MetadataTable"),
        ("AWS::Cognito::UserPool", "UserPool"),
        ("AWS::Logs::LogGroup", "ApiAccessLogs"),
        ("AWS::Logs::LogGroup", "ControlPlaneLogs"),
    ]

    for resource_type, logical_id_prefix in resources:
        resource = _resource_by_logical_id_prefix(template, resource_type, logical_id_prefix)
        assert resource["DeletionPolicy"] == "RetainExceptOnCreate"
        assert resource["UpdateReplacePolicy"] == "Retain"


def test_static_web_bucket_is_destroyed_with_auto_delete_helper() -> None:
    template = _template()
    web_bucket = _resource_by_logical_id_prefix(template, "AWS::S3::Bucket", "WebBucket")
    assert web_bucket["DeletionPolicy"] == "Delete"
    assert web_bucket["UpdateReplacePolicy"] == "Delete"

    auto_delete_resources = template.find_resources("Custom::S3AutoDeleteObjects")
    web_bucket_auto_delete_resources = [
        resource
        for resource in auto_delete_resources.values()
        if _json_text(resource.get("Properties", {}).get("BucketName"))
        == _json_text({"Ref": "WebBucket12880F5B"})
    ]
    assert len(web_bucket_auto_delete_resources) == 1


def test_every_api_route_requires_jwt_and_custom_scope() -> None:
    routes = _template().find_resources("AWS::ApiGatewayV2::Route")
    assert len(routes) == 4
    for route in routes.values():
        properties = route["Properties"]
        assert properties["AuthorizationType"] == "JWT"
        assert properties["AuthorizationScopes"] == ["vonavy-agent/api"]
        assert "AuthorizerId" in properties


def test_staging_policy_does_not_grant_object_tagging() -> None:
    staging_statements = [
        statement
        for statement in _policy_statements(_template())
        if _resource_mentions(statement, "pending/users/*")
    ]

    assert len(staging_statements) == 1
    actions = _actions(staging_statements[0])
    assert "s3:GetObjectTagging" not in actions
    assert "s3:PutObjectTagging" not in actions
