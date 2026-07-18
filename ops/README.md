# Operations

These runbooks establish the AWS account and executor environment without creating application infrastructure prematurely.

Order:

1. `account-bootstrap.md`
2. `gpu-quotas.md`
3. `executor-contract.md`
4. apply and verify the Phase 0 patch locally
5. only then begin the AWS control-plane CDK phase

Never place AWS access keys, SSO caches, `.env` files, uploaded datasets, or generated model artifacts in Git.
