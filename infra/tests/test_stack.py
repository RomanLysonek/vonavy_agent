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


def test_stack_adds_only_ephemeral_fargate_compute() -> None:
    template = _template()
    template.resource_count_is("AWS::EC2::Instance", 0)
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::RDS::DBInstance", 0)
    template.resource_count_is("AWS::SageMaker::Endpoint", 0)
    template.resource_count_is("AWS::Batch::ComputeEnvironment", 1)
    template.resource_count_is("AWS::Batch::JobQueue", 1)
    template.resource_count_is("AWS::Batch::JobDefinition", 2)
    template.resource_count_is("AWS::EC2::VPC", 1)
    template.resource_count_is("AWS::EC2::InternetGateway", 1)
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
    assert len(data_statements) == 3

    staging_statement = staging_statements[0]
    dataset_read_statement = next(
        statement
        for statement in data_statements
        if {"s3:GetObject", "s3:GetObjectVersion"} == _actions(statement)
    )
    data_lifecycle_statement = next(
        statement
        for statement in data_statements
        if {
            "s3:DeleteObjectVersion",
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:PutObject",
            "s3:PutObjectTagging",
        }
        <= _actions(statement)
    )

    assert staging_statement["Effect"] == "Allow"
    assert {
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:PutObject",
    } <= _actions(staging_statement)

    assert dataset_read_statement["Effect"] == "Allow"
    assert data_lifecycle_statement["Effect"] == "Allow"


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
        ("AWS::Logs::LogGroup", "ValidationWorkerLogs"),
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
    assert len(routes) == 11
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


def test_validation_compute_is_scale_to_zero_and_has_no_nat() -> None:
    template = _template()
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    compute = _resource_by_logical_id_prefix(
        template,
        "AWS::Batch::ComputeEnvironment",
        "ValidationComputeEnvironment",
    )
    properties = compute["Properties"]
    assert properties["Type"] == "managed"
    resources = properties["ComputeResources"]
    assert resources["Type"] == "FARGATE"
    assert resources["MaxvCpus"] == 1
    assert len(resources["Subnets"]) == 2
    assert len(resources["SecurityGroupIds"]) == 1


def test_validation_job_definition_is_bounded_fargate() -> None:
    job_definition = _resource_by_logical_id_prefix(
        _template(),
        "AWS::Batch::JobDefinition",
        "ValidationJobDefinition",
    )
    properties = job_definition["Properties"]
    serialized = _json_text(properties)
    assert properties["Type"] == "container"
    assert properties["PlatformCapabilities"] == ["FARGATE"]
    assert properties["Timeout"]["AttemptDurationSeconds"] == 900
    assert properties["RetryStrategy"]["Attempts"] == 1
    assert "PropagateTags" not in properties
    assert not any(
        "batch:TagResource" in _actions(statement) for statement in _policy_statements(_template())
    )
    assert '"Type":"VCPU","Value":"1"' in serialized
    assert '"Type":"MEMORY","Value":"2048"' in serialized
    assert "VONAVY_DATA_BUCKET" in serialized
    assert '"AssignPublicIp":"ENABLED"' in serialized
    assert "validation" in serialized


def test_validation_worker_permissions_are_prefix_scoped() -> None:
    statements = _policy_statements(_template())
    dataset_reads = [
        statement
        for statement in statements
        if _resource_mentions(statement, "datasets/users/*")
        and {"s3:GetObject", "s3:GetObjectVersion"} <= _actions(statement)
    ]
    result_writes = [
        statement
        for statement in statements
        if _resource_mentions(statement, "validation-results/users/*")
        and {"s3:PutObject", "s3:PutObjectTagging"} <= _actions(statement)
    ]
    assert dataset_reads
    assert len(result_writes) == 1
    assert "s3:DeleteObject" not in _actions(result_writes[0])
    assert "s3:DeleteObjectVersion" not in _actions(result_writes[0])

    job_role_policy = _resource_by_logical_id_prefix(
        _template(),
        "AWS::IAM::Policy",
        "ValidationJobRoleDefaultPolicy",
    )
    job_role_text = _json_text(job_role_policy)
    assert "dynamodb:" not in job_role_text.lower()
    assert "batch:" not in job_role_text.lower()
    assert "s3:ListBucket" not in job_role_text
    assert "s3:DeleteObject" not in job_role_text


def test_validation_security_group_has_no_ingress() -> None:
    template = _template()
    template.resource_count_is("AWS::EC2::SecurityGroupIngress", 0)


def test_control_plane_can_submit_and_reconcile_only_validation_jobs() -> None:
    statements = _policy_statements(_template())
    submit = [statement for statement in statements if "batch:SubmitJob" in _actions(statement)]
    assert len(submit) == 2
    validation_submit = next(
        statement
        for statement in submit
        if "ValidationJobDefinition" in _json_text(statement["Resource"])
    )
    submit_resources = _json_text(validation_submit["Resource"])
    assert "ValidationJobQueue" in submit_resources
    assert "ValidationJobDefinition" in submit_resources

    pass_role = [statement for statement in statements if "iam:PassRole" in _actions(statement)]
    assert len(pass_role) == 2
    validation_pass_role = next(
        statement
        for statement in pass_role
        if "ValidationExecutionRole" in _json_text(statement["Resource"])
    )
    assert _actions(validation_pass_role) == {"iam:PassRole"}
    pass_role_resources = _json_text(validation_pass_role["Resource"])
    assert "ValidationExecutionRole" in pass_role_resources
    assert "ValidationJobRole" in pass_role_resources
    assert validation_pass_role["Resource"] != "*"

    reconciliation = [
        statement
        for statement in statements
        if {"batch:DescribeJobs", "batch:TerminateJob"} <= _actions(statement)
    ]
    assert len(reconciliation) == 2
    assert all(statement["Resource"] == "*" for statement in reconciliation)


def test_submit_job_permission_includes_only_validation_family() -> None:
    statements = _policy_statements(_template())
    submit = [statement for statement in statements if "batch:SubmitJob" in _actions(statement)]
    validation_submit = next(
        statement
        for statement in submit
        if "ValidationJobDefinition" in _json_text(statement["Resource"])
    )
    resources = validation_submit["Resource"]
    assert isinstance(resources, list)
    assert len(resources) == 3
    assert all(resource != "*" for resource in resources)
    serialized_resources = [_json_text(resource) for resource in resources]
    assert sum("ValidationJobQueue" in resource for resource in serialized_resources) == 1
    assert sum("ValidationJobDefinition" in resource for resource in serialized_resources) == 2
    family_resources = [
        resource for resource in serialized_resources if "job-definition/" in resource
    ]
    assert len(family_resources) == 1
    assert "ValidationJobDefinition" in family_resources[0]


def test_forecast_job_reuses_queue_and_is_bounded_fargate() -> None:
    template = _template()
    template.resource_count_is("AWS::Batch::ComputeEnvironment", 1)
    template.resource_count_is("AWS::Batch::JobQueue", 1)
    job_definition = _resource_by_logical_id_prefix(
        template,
        "AWS::Batch::JobDefinition",
        "ForecastJobDefinition",
    )
    properties = job_definition["Properties"]
    serialized = _json_text(properties)
    assert properties["Type"] == "container"
    assert properties["PlatformCapabilities"] == ["FARGATE"]
    assert properties["Timeout"]["AttemptDurationSeconds"] == 3600
    assert properties["RetryStrategy"]["Attempts"] == 1
    assert "PropagateTags" not in properties
    assert '"Type":"VCPU","Value":"1"' in serialized
    assert '"Type":"MEMORY","Value":"4096"' in serialized
    assert '"AssignPublicIp":"ENABLED"' in serialized
    assert "VONAVY_DATA_BUCKET" in serialized


def test_forecast_worker_and_control_plane_are_least_privilege() -> None:
    template = _template()
    statements = _policy_statements(template)
    forecast_writes = [
        statement
        for statement in statements
        if _resource_mentions(statement, "forecast-results/users/*")
        and {"s3:PutObject", "s3:PutObjectTagging"} <= _actions(statement)
    ]
    assert len(forecast_writes) == 1
    assert "s3:DeleteObject" not in _actions(forecast_writes[0])
    assert "s3:ListBucket" not in _actions(forecast_writes[0])

    submit = [statement for statement in statements if "batch:SubmitJob" in _actions(statement)]
    forecast_submit = next(
        statement
        for statement in submit
        if "ForecastJobDefinition" in _json_text(statement["Resource"])
    )
    resources = forecast_submit["Resource"]
    assert isinstance(resources, list)
    assert len(resources) == 3
    serialized = [_json_text(resource) for resource in resources]
    assert sum("ValidationJobQueue" in resource for resource in serialized) == 1
    assert sum("ForecastJobDefinition" in resource for resource in serialized) == 2
    assert all(resource != "*" for resource in resources)

    pass_role = [statement for statement in statements if "iam:PassRole" in _actions(statement)]
    forecast_pass_role = next(
        statement
        for statement in pass_role
        if "ForecastExecutionRole" in _json_text(statement["Resource"])
    )
    role_resources = _json_text(forecast_pass_role["Resource"])
    assert "ForecastExecutionRole" in role_resources
    assert "ForecastJobRole" in role_resources
    assert forecast_pass_role["Resource"] != "*"
    assert not any("batch:TagResource" in _actions(statement) for statement in statements)


def test_forecast_agent_is_pinned_and_reads_only_exact_secret_parameter() -> None:
    template = _template()
    function = _resource_by_logical_id_prefix(
        template,
        "AWS::Lambda::Function",
        "ForecastControlPlaneFunction",
    )
    properties = function["Properties"]
    assert properties["Timeout"] == 45
    environment = properties["Environment"]["Variables"]
    assert environment["OPENAI_API_KEY_PARAMETER"] == "/vonavy-agent/dev/openai-api-key"
    assert environment["OPENAI_MODEL"] == "gpt-5-mini-2025-08-07"
    assert environment["OPENAI_TIMEOUT_SECONDS"] == "25"
    assert environment["AGENT_DAILY_LIMIT"] == "20"

    statements = _policy_statements(template)
    parameter_reads = [
        statement for statement in statements if "ssm:GetParameter" in _actions(statement)
    ]
    assert len(parameter_reads) == 1
    assert _actions(parameter_reads[0]) == {"ssm:GetParameter"}
    assert parameter_reads[0]["Resource"] != "*"
    assert "parameter/vonavy-agent/dev/openai-api-key" in _json_text(parameter_reads[0]["Resource"])

    validation_reads = [
        statement
        for statement in statements
        if _resource_mentions(statement, "validation-results/users/*")
        and {"s3:GetObject", "s3:GetObjectVersion"} <= _actions(statement)
    ]
    assert len(validation_reads) >= 2


def test_forecast_routes_are_jwt_protected() -> None:
    routes = _template().find_resources("AWS::ApiGatewayV2::Route")
    by_key = {route["Properties"]["RouteKey"]: route["Properties"] for route in routes.values()}
    expected = {
        "POST /api/datasets/{dataset_id}/forecast-agent",
        "POST /api/datasets/{dataset_id}/forecasts",
        "GET /api/forecasts/{run_id}",
        "GET /api/forecasts/{run_id}/result",
    }
    assert expected <= set(by_key)
    for key in expected:
        assert by_key[key]["AuthorizationType"] == "JWT"
        assert by_key[key]["AuthorizationScopes"] == ["vonavy-agent/api"]


def test_validation_routes_are_agent_friendly_and_jwt_protected() -> None:
    routes = _template().find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {route["Properties"]["RouteKey"] for route in routes.values()}
    assert "POST /api/datasets/{dataset_id}/validations" in route_keys
    assert "GET /api/validations/{job_id}" in route_keys
    assert "GET /api/validations/{job_id}/result" in route_keys
