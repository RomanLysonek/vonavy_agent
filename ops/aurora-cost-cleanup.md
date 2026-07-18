# Aurora cost investigation and cleanup gate

The Phase 0 account inventory found an Aurora PostgreSQL Serverless v2 cluster
that is unrelated to the vonavy-agent design. Phase 1 does not use Aurora.
Auto-pause can reduce compute charges, but cluster storage, backups, snapshots,
I/O, and periods of activity can still produce cost.

Do not commit account identifiers or unrelated resource identifiers to this
repository. Set them only in the executor shell from the accepted inventory:

```bash
export AURORA_CLUSTER_ID='<discovered-cluster-id>'
export AURORA_INSTANCE_ID='<discovered-instance-id>'
```

## Read-only investigation

Run with `vonavy-readonly` in `eu-central-1`:

```bash
: "${AURORA_CLUSTER_ID:?Set AURORA_CLUSTER_ID}"
: "${AURORA_INSTANCE_ID:?Set AURORA_INSTANCE_ID}"

aws rds describe-db-clusters \
  --db-cluster-identifier "$AURORA_CLUSTER_ID" \
  --profile vonavy-readonly \
  --region eu-central-1

aws rds describe-db-instances \
  --db-instance-identifier "$AURORA_INSTANCE_ID" \
  --profile vonavy-readonly \
  --region eu-central-1

CLUSTER_ARN="$(aws rds describe-db-clusters \
  --db-cluster-identifier "$AURORA_CLUSTER_ID" \
  --profile vonavy-readonly \
  --region eu-central-1 \
  --query 'DBClusters[0].DBClusterArn' \
  --output text)"

aws rds list-tags-for-resource \
  --resource-name "$CLUSTER_ARN" \
  --profile vonavy-readonly \
  --region eu-central-1

aws rds describe-db-cluster-snapshots \
  --db-cluster-identifier "$AURORA_CLUSTER_ID" \
  --snapshot-type manual \
  --profile vonavy-readonly \
  --region eu-central-1

aws rds describe-events \
  --source-type db-cluster \
  --source-identifier "$AURORA_CLUSTER_ID" \
  --duration 10080 \
  --profile vonavy-readonly \
  --region eu-central-1
```

Also inspect Cost Explorer for RDS usage and CloudTrail for the creator identity
and creation event. Report whether the cluster contains intentional data before
recommending deletion.

## Decision gate

No snapshot or deletion is automatic.

- If it belongs to another project, tag it and retain it knowingly.
- If it is an empty accidental console artifact, deletion without a final
  snapshot may avoid creating another billable artifact.
- If its contents are uncertain, take a named final cluster snapshot first,
  document the continuing snapshot-storage cost, then delete the instance and
  cluster only after explicit approval.

The executor must show the exact commands, identifiers, deletion-protection
state, and final-snapshot choice before Roman approves a write.
