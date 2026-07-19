# Phase 1: serverless authenticated control plane

Phase 1 converts the cloud boundary introduced in Phase 0 into a deployable,
scale-to-zero AWS control plane. It deliberately stops before execution or GPU
infrastructure.

## Implemented slice

```text
CloudFront private static site
        │
        ├── Cognito authorization-code login with PKCE
        │
        ▼
API Gateway HTTP API + JWT scope enforcement
        │
        ▼
Bounded Lambda control plane
        ├── DynamoDB owner-scoped metadata
        ├── presigned direct upload to private staging S3
        └── accepted immutable copy in versioned data S3
```

The browser never sends a dataset through Lambda or API Gateway. Lambda creates
a short-lived, policy-constrained S3 POST into a dedicated unversioned staging
bucket. The POST policy requires the exact declared byte length, media type,
server-side encryption field, object key, and pending tag. Replaying a valid POST
can overwrite only the same bounded staging key; it cannot create unbounded
object versions.

Completion first copies the staging object into a separate versioned data
bucket under an owner-scoped final key. Lambda then inspects that exact copied
`VersionId`, verifies its byte length, and only afterward publishes the dataset
metadata transaction. A rejected copied version is deleted explicitly. On
success, Lambda records the exact S3 `VersionId`, applies completion and
retention tags, and deletes the staging object. This ordering closes the race in
which a still-valid staging form could otherwise overwrite the source between a
pre-copy check and the copy. All later validation or training phases must read
that final key and exact version. A replayed staging form therefore cannot
mutate the accepted dataset.

## Security properties

- Cognito public sign-up is disabled.
- Ownership comes only from the validated JWT `sub` claim.
- The browser cannot submit an owner ID.
- Protected API routes require the custom `vonavy-agent/api` access-token scope.
- OAuth uses authorization code plus PKCE and validates the OAuth state value.
- Access tokens are kept in `sessionStorage`, not durable local storage.
- Upload, data, and web buckets block public access and enforce TLS.
- The staging bucket is unversioned, encrypted with SSE-S3, and expires every
  object after one day.
- The data bucket is versioned, encrypted with SSE-S3, and lifecycle-managed.
- Completed demo data is copied into the data bucket and tagged for configured
  automatic current-version expiry.
- DynamoDB is on-demand, encrypted, owner-partitioned, TTL-enabled, and protected
  by point-in-time recovery.
- A fixed per-owner upload-slot set is reserved transactionally. Concurrent
  requests cannot exceed the configured dataset-count ceiling.
- The deployment configuration requires `max_upload_bytes × upload_slots` to be
  no larger than the configured owner storage ceiling, creating a hard upper
  bound even under concurrency.
- Pending slot, upload, and dataset records expire after one day; completion
  extends all three records to the configured demo retention period.
- Lambda has bounded concurrency, memory, and wall time.
- Lambda permissions separate the staging upload prefix from the finalized
  owner-scoped data prefix; no bucket listing permission is granted.
- File extensions, media types, names, declared sizes, and route identifiers are
  validated before AWS writes.

DynamoDB TTL and S3 lifecycle deletion are asynchronous. They may retain expired
items briefly, which can conservatively block a new slot; they do not increase
the configured active-slot limit. A replay can leave at most one bounded object
at its unversioned staging key until the one-day lifecycle removes it. Finalized
data versions can remain as noncurrent versions for up to one additional
configured retention period before permanent deletion.

Phase 2 must still inspect the actual file format and decompressed structure in
an isolated validation job. A browser-provided extension or media type is not
proof that a file is safe or structurally valid.

## API surface

All Lambda-backed routes are authenticated and scope-protected:

- `GET /api/health`
- `POST /api/upload-sessions`
- `POST /api/upload-sessions/{upload_id}/complete`
- `GET /api/datasets`

## Default server policy

- 100 MiB maximum per upload;
- 10 active dataset slots per owner;
- 1 GiB hard maximum represented by those slots;
- 15-minute presigned POST lifetime;
- one-day pending-upload retention;
- one-day unversioned staging retention;
- 14-day finalized current-version demo retention and at most one additional
  14-day noncurrent-version lifecycle window;
- two concurrent control-plane Lambda invocations;
- API stage throttle of two steady-state requests per second and burst five.

The client displays policy values but cannot increase them.

## Deliberate non-goals

Phase 1 does not create:

- AWS Batch, Fargate, or GPU compute;
- Step Functions orchestration;
- model training, evaluation, or inference;
- a custom domain or ACM certificate;
- GitHub deployment credentials;
- budgets or quota changes;
- an RDS or Aurora dependency.

The initial deployment uses the generated CloudFront domain. The purchased
custom domain is connected only after the control plane passes login, ownership,
upload, lifecycle, and teardown smoke tests.

## Cost shape

At idle, the stack contains no continuously running compute. Charges, if any,
come from stored S3/DynamoDB data, CloudFront/API/Lambda requests, logs, and
Cognito usage. Dataset and metadata retention are bounded by lifecycle and TTL
rules. CDK helper Lambdas may exist in the synthesized template for asset
deployment or log retention, but they are not permanently running workers.

## Acceptance criteria

Phase 1 is accepted only when:

1. root application tests remain green;
2. infrastructure Ruff, formatting, mypy, and pytest checks pass;
3. CDK synthesis succeeds from committed lock files;
4. the synthesized template contains no EC2, NAT Gateway, RDS, SageMaker, or
   Batch compute resources;
5. the IAM diff is reviewed explicitly;
6. two Cognito users cannot list or complete one another's uploads;
7. an oversized upload is rejected before a presigned POST is issued;
8. an object with the wrong size or media type cannot be completed;
9. accepted data is copied from staging into the versioned final-data bucket;
10. the accepted exact final S3 object version is persisted;
11. replaying an upload cannot mutate the accepted final object;
12. staging-bucket expiry and finalized-object retention tags are verified in S3;
13. no deployment occurs before Roman approves the exact CDK diff.
