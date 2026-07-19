from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    Environment,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_authorizers as authorizers,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as integrations,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3deploy,
)
from constructs import Construct


@dataclass(frozen=True, slots=True)
class DeploymentConfig:
    environment_name: str
    max_upload_bytes: int
    max_datasets_per_owner: int
    max_total_bytes_per_owner: int
    upload_retention_days: int
    protect_data: bool
    local_callback_url: str

    def __post_init__(self) -> None:
        normalized = self.environment_name.replace("-", "")
        if (
            not normalized.isalnum()
            or not normalized.islower()
            or not 1 <= len(self.environment_name) <= 20
        ):
            raise ValueError(
                "environment_name must contain 1 to 20 lowercase letters, digits, and hyphens"
            )
        if not 1 <= self.max_upload_bytes <= 500 * 1024 * 1024:
            raise ValueError("max_upload_bytes must be between 1 byte and 500 MiB")
        if not 1 <= self.max_datasets_per_owner <= 1000:
            raise ValueError("max_datasets_per_owner must be between 1 and 1000")
        minimum_hard_quota = self.max_upload_bytes * self.max_datasets_per_owner
        if not minimum_hard_quota <= self.max_total_bytes_per_owner <= 100 * 1024**3:
            raise ValueError(
                "max_total_bytes_per_owner must cover every allowed upload slot "
                "and be at most 100 GiB"
            )
        if not 1 <= self.upload_retention_days <= 365:
            raise ValueError("upload_retention_days must be between 1 and 365")
        callback = urlsplit(self.local_callback_url)
        if (
            callback.scheme != "http"
            or callback.hostname not in {"localhost", "127.0.0.1"}
            or callback.port is None
            or callback.path not in {"", "/"}
            or callback.query
            or callback.fragment
        ):
            raise ValueError("local_callback_url must be a root localhost HTTP URL with a port")

    @property
    def local_origin(self) -> str:
        callback = urlsplit(self.local_callback_url)
        return f"{callback.scheme}://{callback.netloc}"


class ControlPlaneStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: DeploymentConfig,
        env: Environment | dict[str, Any] | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env, description=description)
        self.config = config
        removal_policy = RemovalPolicy.RETAIN if config.protect_data else RemovalPolicy.DESTROY

        upload_bucket = s3.Bucket(
            self,
            "UploadBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=removal_policy,
            auto_delete_objects=not config.protect_data,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="AbortIncompleteMultipartUploads",
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
                s3.LifecycleRule(
                    id="ExpirePendingUploads",
                    expiration=Duration.days(1),
                ),
            ],
        )

        data_bucket = s3.Bucket(
            self,
            "DataBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=removal_policy,
            auto_delete_objects=not config.protect_data,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireDemoDatasets",
                    tag_filters={"retention": "demo"},
                    expiration=Duration.days(config.upload_retention_days),
                ),
                s3.LifecycleRule(
                    id="ExpireOldObjectVersions",
                    noncurrent_version_expiration=Duration.days(config.upload_retention_days),
                ),
            ],
        )

        metadata_table = dynamodb.Table(
            self,
            "MetadataTable",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            time_to_live_attribute="expires_at",
            deletion_protection=config.protect_data,
            removal_policy=removal_policy,
        )

        web_bucket = s3.Bucket(
            self,
            "WebBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=removal_policy,
            auto_delete_objects=not config.protect_data,
        )

        web_security_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "WebSecurityHeaders",
            comment="Security headers for the vonavy-agent static application",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self'; "
                        "connect-src 'self' https://*.amazonaws.com "
                        "https://*.amazoncognito.com; "
                        "img-src 'self' data:; "
                        "style-src 'self'; "
                        "script-src 'self'; "
                        "object-src 'none'; "
                        "base-uri 'none'; "
                        "frame-ancestors 'none'"
                    ),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    preload=False,
                    override=True,
                ),
            ),
        )

        distribution = cloudfront.Distribution(
            self,
            "WebDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(web_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=web_security_headers,
                compress=True,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )
        web_url = f"https://{distribution.distribution_domain_name}/"

        upload_bucket.add_cors_rule(
            allowed_methods=[s3.HttpMethods.POST],
            allowed_origins=[web_url.rstrip("/"), config.local_origin],
            allowed_headers=["*"],
            exposed_headers=["ETag"],
            max_age=900,
        )

        user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_digits=True,
                require_lowercase=True,
                require_symbols=True,
                require_uppercase=True,
                temp_password_validity=Duration.days(3),
            ),
            deletion_protection=config.protect_data,
            removal_policy=removal_policy,
        )
        api_resource_scope = cognito.ResourceServerScope(
            scope_name="api",
            scope_description="Use the vonavy-agent control-plane API",
        )
        resource_server = user_pool.add_resource_server(
            "ResourceServer",
            identifier="vonavy-agent",
            scopes=[api_resource_scope],
        )
        api_scope = cognito.OAuthScope.resource_server(resource_server, api_resource_scope)
        user_pool_client = user_pool.add_client(
            "WebClient",
            generate_secret=False,
            prevent_user_existence_errors=True,
            auth_flows=cognito.AuthFlow(user_srp=True),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(1),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, api_scope],
                callback_urls=[web_url, config.local_callback_url],
                logout_urls=[web_url, config.local_callback_url],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )
        domain_prefix = f"vonavy-{config.environment_name}-{Aws.ACCOUNT_ID}"
        user_pool_domain = user_pool.add_domain(
            "HostedDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix),
        )

        control_plane_log_group = logs.LogGroup(
            self,
            "ControlPlaneLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=removal_policy,
        )
        control_plane_function = lambda_.Function(
            self,
            "ControlPlaneFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(Path(__file__).parents[1] / "lambda/control_plane")),
            timeout=Duration.seconds(15),
            memory_size=256,
            reserved_concurrent_executions=2,
            log_group=control_plane_log_group,
            environment={
                "UPLOAD_BUCKET": upload_bucket.bucket_name,
                "DATA_BUCKET": data_bucket.bucket_name,
                "METADATA_TABLE": metadata_table.table_name,
                "MAX_UPLOAD_BYTES": str(config.max_upload_bytes),
                "MAX_DATASETS_PER_OWNER": str(config.max_datasets_per_owner),
                "MAX_TOTAL_BYTES_PER_OWNER": str(config.max_total_bytes_per_owner),
                "UPLOAD_RETENTION_DAYS": str(config.upload_retention_days),
                "AWS_REGION_NAME": self.region,
            },
        )
        metadata_table.grant_read_write_data(control_plane_function)
        control_plane_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:DeleteObject",
                    "s3:GetObject",
                    "s3:PutObject",
                ],
                resources=[upload_bucket.arn_for_objects("pending/users/*")],
            )
        )
        control_plane_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:DeleteObjectVersion",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:PutObjectTagging",
                ],
                resources=[data_bucket.arn_for_objects("datasets/users/*")],
            )
        )

        integration = integrations.HttpLambdaIntegration(
            "ControlPlaneIntegration",
            control_plane_function,
            payload_format_version=apigwv2.PayloadFormatVersion.VERSION_2_0,
        )
        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            create_default_stage=False,
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[web_url.rstrip("/"), config.local_callback_url.rstrip("/")],
                allow_headers=["authorization", "content-type"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                max_age=Duration.hours(1),
            ),
        )
        jwt_authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[user_pool_client.user_pool_client_id],
        )
        for path, method in (
            ("/api/health", apigwv2.HttpMethod.GET),
            ("/api/upload-sessions", apigwv2.HttpMethod.POST),
            ("/api/upload-sessions/{upload_id}/complete", apigwv2.HttpMethod.POST),
            ("/api/datasets", apigwv2.HttpMethod.GET),
        ):
            http_api.add_routes(
                path=path,
                methods=[method],
                integration=integration,
                authorizer=jwt_authorizer,
                authorization_scopes=["vonavy-agent/api"],
            )

        api_access_logs = logs.LogGroup(
            self,
            "ApiAccessLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=removal_policy,
        )
        api_access_log_format = (
            '{"requestId":"$context.requestId",'
            '"ip":"$context.identity.sourceIp",'
            '"requestTime":"$context.requestTime",'
            '"httpMethod":"$context.httpMethod",'
            '"routeKey":"$context.routeKey",'
            '"status":"$context.status",'
            '"protocol":"$context.protocol",'
            '"responseLength":"$context.responseLength",'
            '"extendedRequestId":"$context.extendedRequestId"}'
        )
        apigwv2.CfnStage(
            self,
            "DefaultStage",
            api_id=http_api.api_id,
            stage_name="$default",
            auto_deploy=True,
            access_log_settings=apigwv2.CfnStage.AccessLogSettingsProperty(
                destination_arn=api_access_logs.log_group_arn,
                format=api_access_log_format,
            ),
            default_route_settings=apigwv2.CfnStage.RouteSettingsProperty(
                throttling_burst_limit=5,
                throttling_rate_limit=2,
            ),
        )

        s3deploy.BucketDeployment(
            self,
            "DeployWeb",
            sources=[
                s3deploy.Source.asset(str(Path(__file__).parents[1] / "web")),
                s3deploy.Source.json_data(
                    "config.json",
                    {
                        "apiBaseUrl": http_api.api_endpoint,
                        "region": self.region,
                        "userPoolId": user_pool.user_pool_id,
                        "userPoolClientId": user_pool_client.user_pool_client_id,
                        "cognitoDomain": user_pool_domain.base_url(),
                        "redirectUri": web_url,
                        "scope": "openid email vonavy-agent/api",
                        "maximumUploadBytes": config.max_upload_bytes,
                        "maximumDatasetsPerOwner": config.max_datasets_per_owner,
                        "maximumTotalBytesPerOwner": config.max_total_bytes_per_owner,
                    },
                ),
            ],
            destination_bucket=web_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
        )

        CfnOutput(self, "WebUrl", value=web_url)
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "UploadBucketName", value=upload_bucket.bucket_name)
        CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name)
        CfnOutput(self, "MetadataTableName", value=metadata_table.table_name)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "CognitoDomain", value=user_pool_domain.base_url())
