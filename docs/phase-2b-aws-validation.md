# Phase 2B: ephemeral AWS dataset validation

Phase 2B connects the cloud-neutral Phase 2A validation worker to the deployed
AWS control plane. It deliberately keeps validation logic, parser behavior, and
`validation-request/v1` / `validation-result/v1` contracts unchanged.

This phase adds CPU-only validation orchestration. It does not add forecasting,
anomaly detection, Chronos inference, model training, user-supplied code, or GPU
capacity.

## Request path

```text
Authenticated browser or future agent tool
  -> POST /api/datasets/{dataset_id}/validations
  -> owner-scoped DynamoDB job + active-slot transaction
  -> AWS Batch Fargate job
  -> exact versioned S3 input materialized into ephemeral storage
  -> existing Phase 2A worker
  -> versioned validation-result/v1 JSON in S3
  -> polling reconciliation through GET /api/validations/{job_id}
```

The Fargate compute environment has no EC2 instances and no NAT gateway. It is
bounded to one vCPU globally in this first slice, and each job requests one vCPU
and 2 GiB of memory. Fargate capacity is ephemeral: the stack retains the Batch
control-plane resources, while CPU/memory charges occur only while a job runs.
A public IPv4 address is assigned to the task because the public-only VPC has no
NAT or private service endpoints. Normal S3, ECR, CloudWatch Logs, public IPv4,
and Fargate usage charges still apply during execution.

## API contract

All routes retain the Phase 1 Cognito JWT authorizer and
`vonavy-agent/api` scope.

### Start validation

```http
POST /api/datasets/{dataset_id}/validations
Content-Type: application/json

{
  "requestToken": "59dcd457-833e-48ea-b10a-581077204f77",
  "limits": {
    "max_rows": 100000
  }
}
```

`requestToken` is a canonical client-generated UUID. The server derives a stable
validation job ID from the authenticated owner and request token. Repeating the
same request token returns the same job instead of submitting another Batch job.
Reusing it for a different dataset returns a conflict.

The optional `limits` object may only lower server-owned ceilings. Unknown
fields, booleans, non-integers, values below the documented minimums, and values
above policy are rejected before any AWS Batch write.

Successful first submission returns HTTP `202` and a tool-friendly document:

```json
{
  "validationJobId": "...",
  "datasetId": "...",
  "status": "submitted",
  "createdAt": "...",
  "updatedAt": "...",
  "resultAvailable": false,
  "links": {
    "status": "/api/validations/...",
    "result": "/api/validations/.../result"
  }
}
```

An idempotent repeat returns HTTP `200`. Only one active validation job is
allowed per owner in this first slice. A second active job returns HTTP `429`
with `validation_capacity_exceeded`.

### Read status

```http
GET /api/validations/{job_id}
```

The read reconciles the current AWS Batch state and returns one of:

```text
submitting
submitted
pending
runnable
starting
running
succeeded
invalid
failed
```

Terminal status publication and active-slot release occur in one DynamoDB
transaction. A submission that was interrupted before a Batch job ID was
published is converted to a stable failed state after a short grace period,
rather than remaining stuck indefinitely.

### Read result

```http
GET /api/validations/{job_id}/result
```

Before completion, this returns HTTP `409 validation_not_complete`. After a
terminal worker result is available, it returns the original strict
`validation-result/v1` document. A Batch infrastructure failure without a
result artifact remains visible through the status endpoint and returns
`validation_result_unavailable` from the result route.

## Storage and ownership boundaries

The worker receives an exact immutable reference:

```text
bucket
key under datasets/users/{owner}/{dataset_id}/
S3 VersionId
expected byte size
media type
```

It downloads only that version and verifies the Phase 2A size/checksum/parser
rules. Results are written only to:

```text
validation-results/users/{owner}/datasets/{dataset_id}/jobs/{job_id}/result.json
```

The job role can read finalized dataset prefixes and write validation-result
prefixes. It cannot access DynamoDB, submit other jobs, delete objects, list the
bucket, or modify infrastructure. The trusted worker also verifies the exact
owner/dataset/job path before making an S3 call.

The control-plane Lambda owns Batch submission and reconciliation. Users never
receive AWS credentials, job roles, queue identifiers, or mutable S3 access.

## Agentic interface compatibility

The intended product remains an LLM-mediated agent. Phase 2B does not embed an
LLM yet; instead it creates deterministic operations that can later be exposed
as tools without redesigning the backend:

```text
list_datasets
start_dataset_validation
get_validation_status
get_validation_result
```

Each operation has strict JSON inputs, stable error codes, owner isolation,
idempotency, bounded resources, and explicit links to the next legal action.
The future agent should call these server-side tools and explain their outputs;
it should not receive direct AWS credentials or construct Batch payloads.

## Model repositories

The forecasting, anomaly, and Chronos repositories are intentionally not copied
into the generic validation image. They become source material for Phase 3 and
Phase 4 worker adapters:

- `vonava_predikce` for the proven forecasting pipeline and models;
- `vonave_anomalie` for anomaly-assisted workflows;
- `vonavy_chronos` for the Chronos challenger.

Before those phases, current snapshots should be audited for entry points,
contracts, dependency conflicts, model artifacts, licenses, and leakage-safe
evaluation assumptions. Proven implementation should be wrapped behind the
agent's execution contracts rather than reimplemented from memory or imported
as editable sibling checkouts.

## Failure semantics

- `invalid`: the worker successfully read the artifact but the dataset violates
  validation rules. The Batch process exits successfully because this is a
  valid business outcome.
- `failed`: worker/I/O/parser/infrastructure failure. The worker publishes a
  result when possible and exits non-zero.
- Batch failure without a result artifact becomes `failed` with a stable
  server-owned failure code.

The control plane never parses logs to determine business status.

## Deployment review

Phase 2B introduces material infrastructure and must be reviewed before deploy:

- one VPC with two public subnets and no NAT gateway;
- one no-ingress security group;
- one Fargate Batch compute environment capped at one vCPU;
- one job queue and one job definition;
- task execution and prefix-scoped job roles;
- one Docker image asset;
- three JWT-protected API routes;
- control-plane Lambda permissions for scoped submission and reconciliation;
- static UI updates.

Follow `ops/phase-2b-synth-and-review.md`. A merge does not authorize deployment.
