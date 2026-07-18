# Executor operating contract

The executor runs on a trusted machine that is separate from the chat environment. It applies reviewed patches, runs tests, and performs approved AWS commands.

## May execute without additional approval

- repository inspection and Git status/diff commands;
- dependency installation inside a project virtual environment;
- unit tests, linters, type checks, Docker builds;
- `cdk synth` and read-only `cdk diff` preparation;
- read-only AWS identity, quota, stack, Batch, S3 metadata, and log inspection;
- creation of local reports that redact credentials.

## Must stop for approval before executing

- `cdk bootstrap`, `cdk deploy`, or `cdk destroy`;
- IAM, OIDC, Cognito, DNS, certificate, budget, or quota writes;
- submitting On-Demand or Spot jobs;
- any operation expected to incur material cost;
- deletion or overwrite of cloud or local data;
- making a GitHub repository public.

## Never execute

- root access-key creation;
- printing or copying SSO token caches or credentials;
- committing `.env`, `.aws`, datasets, credentials, or generated model artifacts;
- broadening runtime IAM roles to administrator access;
- opening inbound SSH or application access to `0.0.0.0/0`;
- deploying arbitrary user-provided images or executing uploaded code;
- automatically destroying an environment.

## Required report after each stage

Return:

1. exact commands executed, with secrets redacted;
2. environment versions;
3. `git status --short` and `git diff --stat`;
4. test, lint, type-check, and build results;
5. AWS caller ARN, account ID, region, and quota values;
6. every action that still requires human approval;
7. any cost-bearing resource discovered.
