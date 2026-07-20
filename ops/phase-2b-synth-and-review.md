# Phase 2B synth and review

Phase 2B adds the first cost-bearing ephemeral compute path. Complete every
read-only gate before requesting deployment approval.

## Expected baseline

- repository `main` includes the merged Phase 2B implementation;
- Phase 1 stack remains `UPDATE_COMPLETE`;
- Phase 1 upload and authentication tests remain green;
- no validation Batch resources are deployed before explicit approval.

## Lock and test gates

The root package adds an `aws` optional dependency and includes `boto3` in the
development dependency set for adapter tests. Regenerate and inspect the root
lockfile before frozen validation:

```bash
uv lock --python 3.12

git diff -- pyproject.toml uv.lock
```

Then run Python 3.11 and 3.12 root matrices, infrastructure tests, JavaScript
syntax checks, and both validation containers. The Batch image must run as UID
10001, expose no port, and use its fixed Python module entrypoint.

## Synth review

Set `VONAVY_SOURCE_REVISION` to the exact clean `main` commit. Synthesize in
account `147856894016`, Region `eu-central-1`, then record:

- complete resource inventory;
- Docker asset identifier and platform;
- full CDK diff;
- security-only diff;
- estimated persistent and per-job costs;
- all IAM statements grouped by principal.

Reject the diff if it includes:

- EC2 instances;
- a NAT gateway;
- RDS, SageMaker, EKS, or GPU resources;
- more than one Batch compute environment, queue, or job definition;
- Batch `maxvCpus` above 1;
- unbounded task CPU/memory;
- user-controlled container commands or images;
- worker access to DynamoDB;
- S3 list/delete permissions on the validation job role;
- unauthenticated validation routes;
- wildcard `batch:SubmitJob` permissions;
- unrelated Phase 1 resource replacement.

The expected material additions are:

```text
AWS::EC2::VPC
public subnets, route tables, internet gateway
no-ingress security group
AWS::Batch::ComputeEnvironment (FARGATE, maxvCpus=1)
AWS::Batch::JobQueue
AWS::Batch::JobDefinition
ECS task execution role
validation job role
validation worker log group
Docker image asset
three JWT-protected API routes
control-plane code/config/IAM updates
static web asset update
```

## Deployment and certification boundary

Do not deploy merely because CI and synth pass. Return the full/security diff
and request explicit approval.

After an approved deployment, certify with two ephemeral Cognito users:

1. upload one tiny CSV;
2. start validation as user A;
3. verify idempotent repeated submission;
4. verify a second active job is rejected;
5. verify user B cannot read A's job or result;
6. poll through Batch states to `succeeded`;
7. verify exact result object version and validation contents;
8. verify authenticated UI status/result;
9. delete exact validation result version, dataset objects/metadata, and users;
10. verify pristine owner partitions/prefixes and zero CDK diff.

Record actual Batch runtime, public IPv4 duration, log volume, and cost signals.
