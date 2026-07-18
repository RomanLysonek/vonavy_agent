"""Create initial local Experiment Agent schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "blobs",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("media_type", sa.String(30), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.String(36),
            sa.ForeignKey("datasets.id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("dataset_versions.id")),
        sa.Column("ingest_mode", sa.String(20), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column(
            "source_blob_sha256",
            sa.String(64),
            sa.ForeignKey("blobs.sha256"),
            nullable=False,
        ),
        sa.Column(
            "materialized_blob_sha256",
            sa.String(64),
            sa.ForeignKey("blobs.sha256"),
            nullable=False,
        ),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "dataset_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "dataset_version_id",
            sa.String(36),
            sa.ForeignKey("dataset_versions.id"),
            nullable=False,
        ),
        sa.Column("mapping_hash", sa.String(64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dataset_mappings_mapping_hash", "dataset_mappings", ["mapping_hash"])
    op.create_table(
        "data_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "dataset_version_id",
            sa.String(36),
            sa.ForeignKey("dataset_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "mapping_id",
            sa.String(36),
            sa.ForeignKey("dataset_mappings.id"),
            nullable=False,
        ),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_data_profiles_profile_hash", "data_profiles", ["profile_hash"])
    op.create_table(
        "experiment_specs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column(
            "dataset_version_id",
            sa.String(36),
            sa.ForeignKey("dataset_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "mapping_id",
            sa.String(36),
            sa.ForeignKey("dataset_mappings.id"),
            nullable=False,
        ),
        sa.Column(
            "profile_id",
            sa.String(36),
            sa.ForeignKey("data_profiles.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_experiment_specs_spec_hash", "experiment_specs", ["spec_hash"])
    op.create_table(
        "gate_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "spec_id",
            sa.String(36),
            sa.ForeignKey("experiment_specs.id"),
            nullable=False,
        ),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("confirmation_token", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(100)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_json", sa.Text()),
        sa.Column("result_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_jobs_state", "jobs", ["state"])
    op.create_index("ix_jobs_lease_token", "jobs", ["lease_token"])
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("from_state", sa.String(20)),
        sa.Column("to_state", sa.String(20), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("jobs.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "spec_id",
            sa.String(36),
            sa.ForeignKey("experiment_specs.id"),
            nullable=False,
        ),
        sa.Column(
            "gate_result_id",
            sa.String(36),
            sa.ForeignKey("gate_results.id"),
            nullable=False,
        ),
        sa.Column("artifact_relative_path", sa.String(500)),
        sa.Column("manifest_hash", sa.String(64)),
        sa.Column("summary_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "run_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("model", sa.String(50), nullable=False),
        sa.Column("seed", sa.Integer()),
        sa.Column("origin", sa.String(10)),
        sa.Column("horizon", sa.Integer()),
        sa.Column("metric", sa.String(30), nullable=False),
        sa.Column("value", sa.Float()),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("unsupported_reason", sa.String(200)),
    )
    op.create_index("ix_run_metrics_run_id", "run_metrics", ["run_id"])
    op.create_table(
        "planner_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("confirmed_spec_id", sa.String(36), sa.ForeignKey("experiment_specs.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "adapter_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("adapter_kind", sa.String(30), nullable=False),
        sa.Column("manifest_kind", sa.String(30), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("run_ids_json", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.String(500)),
        sa.Column("manifest_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("exports")
    op.drop_table("adapter_snapshots")
    op.drop_table("planner_proposals")
    op.drop_index("ix_run_metrics_run_id", table_name="run_metrics")
    op.drop_table("run_metrics")
    op.drop_table("runs")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_jobs_lease_token", table_name="jobs")
    op.drop_index("ix_jobs_state", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("gate_results")
    op.drop_index("ix_experiment_specs_spec_hash", table_name="experiment_specs")
    op.drop_table("experiment_specs")
    op.drop_index("ix_data_profiles_profile_hash", table_name="data_profiles")
    op.drop_table("data_profiles")
    op.drop_index("ix_dataset_mappings_mapping_hash", table_name="dataset_mappings")
    op.drop_table("dataset_mappings")
    op.drop_table("dataset_versions")
    op.drop_table("blobs")
    op.drop_table("datasets")
