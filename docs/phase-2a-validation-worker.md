# Phase 2A: dataset validation worker

Phase 2A adds a cloud-neutral, CPU-only worker for validating and profiling immutable CSV and Parquet artifacts. It is production code intended to run unchanged inside a later AWS Batch/Fargate job. This phase does **not** add Batch, queues, ECR, task roles, or any AWS deployment.

## Execution contract

The worker accepts `validation-request/v1` and writes `validation-result/v1`. Both contracts are strict Pydantic models in `vonavy_agent.validation_contracts`; unknown fields and unknown schema versions are rejected.

The v1 request supports two immutable artifact-reference shapes:

- `storage=local`: a workspace-relative path plus optional expected size/checksum;
- `storage=s3`: bucket, object key, mandatory object version ID, media type, and optional expected size/checksum.

Phase 2A's local CLI implements only `LocalFileArtifactReader` and `LocalFileArtifactWriter`. The validation core depends on the `ArtifactReader` protocol; Phase 2B supplies separate S3 adapters and Batch orchestration without changing this request/result schema. The local CLI still returns `unsupported_storage` for S3 contracts and performs no cloud call.

All local artifact paths are relative to a declared workspace. Absolute paths, `..`, symlinks, and non-regular input files are rejected. Inputs are opened with `O_NOFOLLOW` and exposed to parsers through a descriptor-backed path. The worker hashes the input before and after scanning and fails with `input_changed` if the bytes change during validation. The result is written atomically by creating and fsyncing a temporary file, renaming it in the destination directory, and fsyncing that directory.

Example request:

```json
{
  "schema_version": "validation-request/v1",
  "job_id": "validation-001",
  "owner_id": "owner-001",
  "dataset_id": "dataset-001",
  "input": {
    "storage": "local",
    "path": "input/dataset.csv",
    "media_type": "text/csv",
    "expected_size_bytes": 12345,
    "expected_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "output": {
    "storage": "local",
    "path": "output/result.json"
  },
  "limits": {
    "max_input_bytes": 262144000,
    "max_rows": 500000,
    "max_columns": 250,
    "max_string_sample_length": 512,
    "max_distinct_values": 20,
    "max_profile_rows": 5000,
    "max_execution_seconds": 900
  },
  "requested_at": "2026-07-20T05:00:00Z"
}
```

Run it locally:

```bash
uv run vonavy-agent validate-dataset \
  --workspace /absolute/path/to/workspace \
  --request /absolute/path/to/workspace/request.json \
  --result output/result.json
```

The `--result` value must match `request.output.path`. The explicit CLI argument provides a known destination even when the request itself is malformed.

Exit codes:

| Code | Meaning |
|---:|---|
| `0` | Dataset validated successfully. |
| `2` | Dataset was readable but violated a validation rule. |
| `1` | Worker, I/O, parser, request, or result-publication failure. |

The worker writes a terminal result whenever the destination is writable. Standard output contains only a concise status and result path; it does not print rows, tokens, credentials, checksums supplied as secrets, or parser exception text.

## Supported formats

Supported media types are:

- `text/csv`
- `application/vnd.apache.parquet`
- `application/x-parquet`

CSV is read as UTF-8, including UTF-8 BOM. The worker detects empty or duplicate headers before pandas can rename them, rejects malformed rows, and treats spreadsheet-formula-like strings as ordinary data.

Parquet validation reads footer/schema metadata first, enforces row and column limits before profiling, rejects nested schema types in v1, and streams row groups instead of loading the entire file solely to count rows.

## Validation outcomes

`status=succeeded` means the artifact was valid and a profile was produced.

`status=invalid` means the artifact was readable but violated a stable rule, for example:

- unsupported media type;
- input byte, row, or column limit exceeded;
- size or checksum mismatch;
- no columns or data rows;
- empty or duplicate column names;
- unsupported nested Parquet type.

`status=failed` means the worker could not safely complete, for example:

- missing, unreadable, or symlinked input;
- malformed CSV or Parquet;
- execution timeout;
- unexpected parser or worker failure;
- result write failure;
- input bytes changing while the worker is scanning them.

Callers use machine-readable error codes and never need to parse prose.

## Profiling

Every column reports exact row/null counts and a parser/physical type. Expensive statistics use either the full dataset or a deterministic reservoir sample seeded from the input SHA-256. Each column states whether its statistics are `exact` or `sampled`.

Profiles include, where applicable:

- numeric min/max/mean/sample standard deviation, p01/p05/p25/p50/p75/p95/p99, signs, zeroes, and non-finite count;
- string length metrics, bounded distinct/top values, empty strings, and count of values truncated for sampling;
- boolean true/false counts;
- date/timestamp min/max, parse failures, and timezone metadata.

Result serialization is strict JSON. `NaN`, positive infinity, and negative infinity are never emitted as JSON numbers.

## Determinism and limits

Given identical worker code, request, file bytes, and limits, schema, validation decisions, sampling, warnings, and column profiles are deterministic. Runtime timestamps, duration, CPU time, and peak RSS are explicitly runtime-specific.

Default limits are conservative and independently configurable in the request. The worker checks the deadline while hashing and between streamed batches. Phase 2B additionally enforces process-level CPU, memory, and wall-clock ceilings in AWS Batch.

## Container

Build:

```bash
docker build \
  --file Dockerfile.validation-worker \
  --build-arg VCS_REF="$(git rev-parse HEAD)" \
  --tag vonavy-agent-validation:local \
  .
```

Run with only the workspace mounted writable:

```bash
docker run --rm \
  --read-only \
  --network none \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --volume "$PWD/workspace:/workspace" \
  vonavy-agent-validation:local \
  --workspace /workspace \
  --request /workspace/request.json \
  --result output/result.json
```

The runtime image:

- uses Python 3.12.3;
- installs from `uv.lock` without development dependencies;
- runs as UID/GID `10001`;
- exposes no port and starts no daemon;
- contains no AWS credentials or cloud-specific adapter.

## Phase 2B extension

Phase 2B implements S3-backed reader/writer adapters and ephemeral AWS Batch/Fargate orchestration around this validation core. The AWS adapter is isolated in `vonavy_agent.validation_worker.aws_artifacts` and is installed through the optional `aws` dependency group. The core worker and local container still do not import boto3 or depend on AWS lifecycle, identity, or metadata APIs. See `phase-2b-aws-validation.md`.
