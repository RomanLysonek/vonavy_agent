# Phase 1 synthesis and review runbook

This runbook is read-only with respect to AWS. It creates local lock files,
installs local dependencies, synthesizes CloudFormation, and compares the
proposed stack with the account. It does not bootstrap or deploy CDK.

## 1. Establish the exact source

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git merge-base --is-ancestor 5cce02f71bd747ed7a90897695ca68457f67a8cc HEAD
```

The branch must be `phase/1-serverless-control-plane-clean-v2`. The working tree is expected to contain only the clean Phase 1 overlay before dependency generation.

## 2. Generate and inspect lock files

```bash
cd infra
uv lock --python 3.12
npm install --package-lock-only

git diff -- uv.lock package-lock.json
uv tree --locked
npm ls --all
```

Do not weaken or float the direct CDK versions to make resolution succeed.
Commit the generated lock files with the Phase 1 branch after all checks pass.

## 3. Root quality gates

From repository root:

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## 4. Infrastructure quality gates

```bash
cd infra
uv sync --frozen --extra dev
npm ci
uv run ruff check .
uv run ruff format --check .
uv run mypy vonavy_infra
uv run pytest
node --check web/app.js
```

Only import sorting and deterministic formatting may be corrected mechanically:

```bash
uv run ruff check . --select I --fix
uv run ruff format .
```

Return every other failure rather than redesigning the infrastructure.

## 5. Synthesize locally

```bash
export AWS_PROFILE=vonavy-readonly
export AWS_REGION=eu-central-1
export AWS_DEFAULT_REGION=eu-central-1
export CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_REGION=eu-central-1
export VONAVY_ENVIRONMENT=dev
export VONAVY_PROTECT_DATA=true
export VONAVY_MAX_UPLOAD_BYTES="$((100 * 1024 * 1024))"
export VONAVY_MAX_DATASETS_PER_OWNER=10
export VONAVY_MAX_TOTAL_BYTES_PER_OWNER="$((1024 * 1024 * 1024))"
export VONAVY_UPLOAD_RETENTION_DAYS=14

npm exec cdk -- synth
```

Inspect the generated resource inventory:

```bash
TEMPLATE="cdk.out/VonavyAgent-dev-ControlPlane.template.json"
test -f "$TEMPLATE"

jq -r '
  .Resources
  | to_entries
  | group_by(.value.Type)
  | map({type: .[0].value.Type, count: length})
  | sort_by(.type)
  | .[]
  | "\(.count)\t\(.type)"
' "$TEMPLATE"

for forbidden in \
  AWS::EC2::Instance \
  AWS::EC2::NatGateway \
  AWS::RDS::DBInstance \
  AWS::RDS::DBCluster \
  AWS::SageMaker::Endpoint \
  AWS::Batch::ComputeEnvironment \
  AWS::Batch::JobQueue; do
  jq -e --arg type "$forbidden" \
    '[.Resources[] | select(.Type == $type)] | length == 0' \
    "$TEMPLATE" >/dev/null
done
```

Inspect the data-bucket IAM statement and confirm it grants only the owner-scoped
object prefix, including `GetObjectVersion` for exact-version verification and
`DeleteObjectVersion` only for removal of a rejected copied version. No bucket
list permission is expected.

CDK may synthesize helper Lambda functions and custom resources for static asset
deployment, bucket cleanup in disposable environments, or log retention. Count
and review them; do not mistake them for continuously running compute.

## 6. Read-only AWS diff

```bash
npm exec cdk -- diff \
  VonavyAgent-dev-ControlPlane \
  --profile vonavy-readonly \
  --no-change-set \
  --security-only

npm exec cdk -- diff \
  VonavyAgent-dev-ControlPlane \
  --profile vonavy-readonly \
  --no-change-set
```

If the read-only profile lacks an inspection permission, report the exact denied
action. Do not silently switch MCP or CDK to `vonavy-admin`.

Export the complete diff to files and return them with the executor report:

```bash
npm exec cdk -- diff VonavyAgent-dev-ControlPlane \
  --profile vonavy-readonly --no-change-set \
  > ../phase1-cdk-diff.txt 2>&1

npm exec cdk -- diff VonavyAgent-dev-ControlPlane \
  --profile vonavy-readonly --no-change-set --security-only \
  > ../phase1-cdk-security-diff.txt 2>&1
```

## 7. Stop boundary

Do not run:

```text
cdk bootstrap
cdk deploy
cdk destroy
aws cloudformation deploy
aws iam create-*
aws cognito-idp create-*
aws s3api create-bucket
aws dynamodb create-table
```

Synthesis and diff evidence must be reviewed before any write operation.
