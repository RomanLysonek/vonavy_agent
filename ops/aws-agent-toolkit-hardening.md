# AWS Agent Toolkit hardening

The AWS skills installation is not the risky component. The authenticated AWS
MCP proxy executes downstream AWS API calls using the permissions of the
selected AWS CLI profile. The current Hermes entry must therefore not inherit
the `vonavy-admin` AdministratorAccess SSO profile.

## Required identity separation

Create an IAM Identity Center permission set named `VonavyReadOnly` and attach
the AWS managed `ReadOnlyAccess` policy. Assign it only to Roman's normal Identity Center user in the target account
returned by `aws sts get-caller-identity --profile vonavy-admin`.

Create a separate CLI profile:

```bash
aws configure sso --profile vonavy-readonly
aws sso login --profile vonavy-readonly
aws sts get-caller-identity --profile vonavy-readonly
```

Verify that its ARN is an assumed Identity Center role for `VonavyReadOnly`, not
`AdministratorAccess` and not root.

## Pin and allowlist the MCP proxy

Do not use `mcp-proxy-for-aws@latest`. Pin the reviewed version and expose only
the read-only profile. The first profile is the default profile for calls where
an agent omits `aws_profile`.

Hermes configuration:

```yaml
mcp_servers:
  aws_mcp:
    command: "uvx"
    args:
      - "mcp-proxy-for-aws==1.6.3"
      - "https://aws-mcp.us-east-1.api.aws/mcp"
      - "--metadata"
      - "AWS_REGION=eu-central-1"
      - "--metadata"
      - "INSTALL_SOURCE=aws-cli"
      - "--profile"
      - "vonavy-readonly"
    timeout: 180
    connect_timeout: 60
    sampling:
      enabled: false
```

Do not add `vonavy-admin` as another `--profile` value or through
`AWS_MCP_PROXY_PROFILES`. Doing so would make it selectable by an agent tool
call and would defeat the approval boundary.

## Verification after Hermes restart

Ask the executor to perform only read operations:

1. return the caller ARN;
2. list CloudFormation stacks;
3. read the two G/VT quota values;
4. list EC2 instances;
5. attempt no mutation.

Review CloudTrail after the test. Any write-capable tool exposure or caller ARN
containing `AdministratorAccess` is a stop condition.

## Write operations

Use `vonavy-admin` only in an explicit terminal command after Roman approves the
exact operation. Do not add it to Hermes MCP. Future routine deployment should
move to a repository- and branch-restricted GitHub OIDC role instead of an
administrator session.
