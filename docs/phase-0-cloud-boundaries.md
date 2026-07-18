# Phase 0: cloud-neutral boundaries

This phase deliberately does not deploy AWS resources. It changes the local application so the later AWS adapters can be added without baking SQLite, anonymous ownership, and local subprocesses into the public service contract.

## Implemented

### Owner-scoped aggregates

Every user-created aggregate now carries a stable `owner_id`:

- dataset and dataset version;
- mapping and profile;
- evaluation specification and leakage gate;
- job and run;
- planner proposal and adapter snapshot;
- export.

Public API reads and mutations derive the owner from an `IdentityProvider` and return a not-found response for another owner's identifiers. Local mode uses one fixed owner named `local`, preserving the current single-user workflow.

Migration `0002_owner_scope` upgrades existing databases and assigns legacy records to the local owner.

### Trusted resource policy

`ExperimentSpec.resources` remains a requested execution envelope, but it is no longer authoritative. `Settings.resource_policy` defines server ceilings and rejects specifications that request more rows, entities, origins, models, wall time, or memory than the deployment allows.

AWS will later map a trusted resource profile to an allow-listed Batch job definition and instance family. A browser will never choose an EC2 type directly.

### Distinct execution contracts

The domain now distinguishes:

- `EvaluationSpec`: historical rolling-origin evaluation with known outcomes;
- `ForecastSpec`: fit on eligible history and forecast dates whose targets are unknown;
- `InferenceSpec`: apply a stored model artifact to compatible future input data.

Only evaluation is executable in the current local engine. Forecast and inference contracts are validated now so later implementations cannot quietly reuse the historical backtest path and call it future forecasting.

### Cloud ports

`ports.py` defines boundaries for:

- immutable object storage;
- owner-scoped metadata persistence;
- job submission and cancellation;
- allow-listed model adapters.

The next implementation phases will provide S3, DynamoDB, AWS Batch, and model-runner adapters behind these boundaries.

## Security invariants introduced

1. Ownership is taken from a trusted identity provider, never from request JSON.
2. Cross-owner identifiers are treated as nonexistent.
3. The job retains owner identity through subprocess execution and artifact publication.
4. Profiles, gates, runs, planner proposals, adapter snapshots, and exports inherit the initiating owner.
5. Server resource ceilings override client requests.
6. Existing local data upgrades into one explicit local tenant.

## Deliberately deferred

- Cognito JWT validation;
- presigned S3 upload sessions;
- DynamoDB repository implementation;
- Step Functions orchestration;
- Batch Fargate and Batch GPU backends;
- actual future forecasting;
- stored-model batch inference;
- retention and per-user usage quotas.

These belong to subsequent phases and should not be simulated in the local API.
