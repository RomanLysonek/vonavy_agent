# AWS account bootstrap

Primary region: `eu-central-1`.

## Human-only console steps

Perform these while no automation is operating as the root user:

1. Sign in as the AWS account root identity.
2. Enable root MFA.
3. Confirm that root has no access keys.
4. Enable IAM Identity Center in `eu-central-1`.
5. Create a normal administrative user for Roman.
6. Assign administrative access temporarily for bootstrap.
7. Sign out from root and do not use root for ordinary work.

## Local temporary credentials

Install AWS CLI v2, then configure an SSO profile:

```bash
aws configure sso --profile vonavy-admin
aws sso login --profile vonavy-admin
aws sts get-caller-identity --profile vonavy-admin
aws configure set region eu-central-1 --profile vonavy-admin
aws configure set output json --profile vonavy-admin
```

Expected properties:

- the caller is an IAM Identity Center assumed role, not root;
- the region is `eu-central-1`;
- no long-lived key is written into the repository.

## Cost controls

Before any Batch or EC2 deployment, create:

- monthly notifications at USD 10, 20, 30, 40, and 60;
- a daily notification at USD 10;
- a deployment policy that permits no more than one GPU worker and one GPU per job.

AWS Budgets is an alerting mechanism, not a real-time hard cap. Infrastructure limits remain mandatory.
