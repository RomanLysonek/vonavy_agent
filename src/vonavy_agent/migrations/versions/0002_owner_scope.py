"""Add owner scope to all user-created aggregates."""

import sqlalchemy as sa
from alembic import op

revision = "0002_owner_scope"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

OWNER_TABLES = (
    "datasets",
    "dataset_versions",
    "dataset_mappings",
    "data_profiles",
    "experiment_specs",
    "gate_results",
    "jobs",
    "runs",
    "planner_proposals",
    "adapter_snapshots",
    "exports",
)


def upgrade() -> None:
    for table in OWNER_TABLES:
        op.add_column(
            table,
            sa.Column(
                "owner_id",
                sa.String(128),
                nullable=False,
                server_default="local",
            ),
        )
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])


def downgrade() -> None:
    for table in reversed(OWNER_TABLES):
        op.drop_index(f"ix_{table}_owner_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("owner_id")
