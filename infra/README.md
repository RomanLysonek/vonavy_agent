# vonavy-agent AWS infrastructure

This directory contains the Phase 1 AWS CDK application. It defines a
serverless, authenticated control plane with separate staging and finalized S3
storage. It deliberately creates no model execution, EC2, NAT Gateway, RDS,
SageMaker, Batch, or GPU resources.

## Toolchains

- Python 3.11 or 3.12;
- `uv` for Python dependencies;
- Node.js 22 and npm for the locally pinned CDK CLI;
- AWS CLI v2 with an IAM Identity Center profile.

Both `uv.lock` and `package-lock.json` must be generated and committed by the
executor before CI or deployment review. This delivery environment cannot
retrieve the CDK packages and therefore does not fabricate lock files.

## Local verification

```bash
cd infra
uv lock --python 3.12
npm install --package-lock-only
uv sync --frozen --extra dev
npm ci
uv run ruff check .
uv run ruff format --check .
uv run mypy vonavy_infra
uv run pytest
node --check web/app.js
npm exec cdk -- synth
```

## Configuration

Environment variables consumed by `app.py`:

- `VONAVY_ENVIRONMENT` — lowercase deployment name, default `dev`;
- `VONAVY_MAX_UPLOAD_BYTES` — default 100 MiB;
- `VONAVY_MAX_DATASETS_PER_OWNER` — default 10;
- `VONAVY_MAX_TOTAL_BYTES_PER_OWNER` — default 1 GiB;
- `VONAVY_UPLOAD_RETENTION_DAYS` — default 14;
- `VONAVY_PROTECT_DATA` — default true;
- `VONAVY_LOCAL_CALLBACK_URL` — default `http://localhost:5173/`.

The total-byte policy must be at least the per-upload limit multiplied by the
number of upload slots. This makes the storage ceiling hard under concurrent
requests rather than a race-prone preflight estimate.

Follow `../ops/phase-1-synth-and-review.md`. Do not bootstrap or deploy from this
directory until the synthesized resource inventory and IAM diff are approved.

## Upload storage boundary

The browser uploads through a 15-minute presigned POST into an unversioned
staging bucket. The POST policy fixes the key, media type, encryption field,
tag, and exact byte range. Completion validates the object and copies it into a
separate versioned data bucket. Only the finalized key and exact version are
published to later phases.
