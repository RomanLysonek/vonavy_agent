# Executor assignment: clean Phase 1 verification

You are the execution agent for `vonavy-agent`. The accepted Phase 0 source of
truth is commit `5cce02f71bd747ed7a90897695ca68457f67a8cc`. Phase 1 must be
established in a fresh Git worktree by the clean-restart bundle; do not reuse
any earlier dirty Phase 1 branch, patch, repair script, or generated file.

Read completely:

- `ops/executor-contract.md`
- `ops/phase-1-synth-and-review.md`
- `ops/aws-agent-toolkit-hardening.md`
- `ops/aurora-cost-cleanup.md`
- `docs/phase-1-serverless-control-plane.md`

## Objective boundary

This phase implements only the near-zero-idle-cost control plane:

- private CloudFront/S3 static frontend;
- invite-only Cognito authentication using authorization code + PKCE;
- JWT- and custom-scope-protected HTTP API;
- owner-scoped DynamoDB metadata;
- direct browser uploads into a one-day staging bucket;
- immutable versioned finalized dataset storage;
- server-owned upload and retention quotas.

This phase must not create EC2, NAT Gateway, RDS, SageMaker, Batch, Fargate,
GPU, model-training, or inference resources. Those arrive in later phases.

## Source establishment

Run the bundle's `bootstrap_clean_phase1.sh`. It creates a new worktree and
branch from the exact accepted Phase 0 commit, then copies the reviewed Phase 1
overlay byte-for-byte. Do not apply Git patches or edit the old Phase 1 tree.

Expected branch:

```text
phase/1-serverless-control-plane-clean-v2
```

Expected base:

```text
5cce02f71bd747ed7a90897695ca68457f67a8cc
```

## Repository work

1. Generate `infra/uv.lock` with Python 3.12 and `infra/package-lock.json`.
2. Review resolved dependencies and include both locks in the Phase 1 commit.
3. Run every root and infrastructure quality gate.
4. Run CDK synthesis and read-only diff exactly as documented.
5. Verify the synthesized stack contains three private S3 buckets: static web,
   unversioned staging upload, and versioned finalized data.
6. Verify every API route requires Cognito JWT authorization and the
   `vonavy-agent/api` scope.
7. Verify Lambda has no bucket-list permission and only the required object
   permissions on staging and finalized prefixes.
8. Verify the browser POST does not use unsupported S3 `x-amz-tagging`; staging
   cleanup is bucket-wide lifecycle expiry, while finalized copies receive the
   `retention=demo` tag.
9. Verify completion copies to versioned finalized storage, validates the exact
   copied `VersionId`, records it transactionally, and removes staging.
10. Confirm no persistent compute or training resource is created.

Only import sorting and deterministic formatting may be corrected mechanically:

```bash
uv run ruff check . --select I --fix
uv run ruff format .
```

Stop and report any other failure. Do not redesign infrastructure independently.

After all checks pass:

```bash
git add -A
git diff --cached --check
git commit -m "Finalize clean Phase 1 control plane"
git status --short
git rev-parse HEAD
```

Push the clean branch only if existing GitHub authentication permits it.

## AWS boundary

Permitted:

- read-only SSO verification through `vonavy-readonly`;
- quota and resource inventory;
- `cdk synth`;
- `cdk diff --no-change-set` and `--security-only`.

Prohibited without Roman's explicit approval:

- CDK bootstrap, deploy, or destroy;
- IAM, Cognito, S3, DynamoDB, budget, or quota writes;
- EC2, Batch, Fargate, SageMaker, or GPU launches;
- DNS or certificate changes.

Return the complete gate output, synthesized resource inventory, IAM/security
diff, exact commit, and clean status. Stop before deployment.
