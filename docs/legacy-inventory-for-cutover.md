# Legacy inventory for cutover

- Repo: `/home/lama/projects/vonavy_agent`
- Branch: `main`
- HEAD: `f507f80d0fdcc4ff6c176f96d916ef42e668053d`
- Origin main: `f507f80d0fdcc4ff6c176f96d916ef42e668053d`

## Counts

- cloud_persistence_references: 68
- legacy_imports: 296
- legacy_routes: 135
- legacy_source_files_by_term: 123
- migration_tables: 14
- old_dependencies: 21
- old_tests_likely_tied_to_forecast_dataset_chronos: 39
- orm_entities: 15
- user_visible_old_term_occurrences: 205

## Scope notes

- Mechanical inventory only; no source-code edits.
- Long tests, crawl, and deploy were intentionally not run.
- src/skincare_advisor_agent/web is absent on current main; infra/web and actual src/vonavy_agent/web were scanned for user-visible old terms.

## Legacy imports

|path|line|statement|matched_keywords|
|---|---|---|---|
|infra/app.py|7|from vonavy_infra.control_plane_stack import ControlPlaneStack, DeploymentConfig|["vonavy_infra"]|
|infra/lambda/control_plane/handler.py|15|import boto3|["boto3"]|
|infra/lambda/control_plane/handler.py|16|from boto3.dynamodb.conditions import Key|["boto3"]|
|infra/lambda/control_plane/handler.py|17|from boto3.dynamodb.types import TypeSerializer|["boto3"]|
|infra/lambda/forecast_control_plane/agent.py|10|import boto3  # type: ignore[import-untyped]|["boto3"]|
|infra/lambda/forecast_control_plane/handler.py|16|import boto3  # type: ignore[import-untyped]|["boto3"]|
|infra/lambda/forecast_control_plane/handler.py|17|from agent import AgentPlanError, build_forecast_agent_plan|["forecast"]|
|infra/lambda/forecast_control_plane/handler.py|30|from boto3.dynamodb.types import TypeSerializer  # type: ignore[import-untyped]|["boto3"]|
|infra/lambda/forecast_control_plane/orchestrator.py|10|import boto3  # type: ignore[import-untyped]|["boto3"]|
|infra/tests/test_handler.py|10|import boto3|["boto3"]|
|infra/tests/test_phase6_custom_domain.py|9|from vonavy_infra.control_plane_stack import ControlPlaneStack, DeploymentConfig|["vonavy_infra"]|
|infra/tests/test_stack.py|9|from vonavy_infra.control_plane_stack import ControlPlaneStack, DeploymentConfig|["vonavy_infra"]|
|src/vonavy_agent/__main__.py|1|from vonavy_agent.cli import main|["vonavy_agent"]|
|src/vonavy_agent/adapters.py|10|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|src/vonavy_agent/adapters.py|11|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/adapters.py|13|from vonavy_agent.domain import ExperimentSpec, StrictModel|["vonavy_agent"]|
|src/vonavy_agent/adapters.py|14|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/adapters.py|15|from vonavy_agent.hashing import canonical_json|["vonavy_agent"]|
|src/vonavy_agent/adapters.py|16|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/adapters.py|17|from vonavy_agent.managed_files import publish_bytes|["vonavy_agent"]|
|src/vonavy_agent/adapters.py|18|from vonavy_agent.persistence import AdapterSnapshot, session_scope|["vonavy_agent"]|
|src/vonavy_agent/adapters.py|19|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/api.py|11|from alembic import command|["alembic"]|
|src/vonavy_agent/api.py|12|from alembic.config import Config|["alembic"]|
|src/vonavy_agent/api.py|17|from sqlalchemy import select|["sqlalchemy"]|
|src/vonavy_agent/api.py|18|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/api.py|20|from vonavy_agent.adapters import (     PreparedInvocation,     adapter_capabilities,     dry_run_invocation,     import_adapter_snapshot, )|["vonavy_agent"]|
|src/vonavy_agent/api.py|26|from vonavy_agent.datasets import DatasetRegistry|["dataset", "vonavy_agent"]|
|src/vonavy_agent/api.py|27|from vonavy_agent.domain import DatasetMappingSpec, ExperimentSpec, JobState|["dataset", "vonavy_agent"]|
|src/vonavy_agent/api.py|28|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/api.py|29|from vonavy_agent.experiments import create_experiment_spec|["vonavy_agent"]|
|src/vonavy_agent/api.py|30|from vonavy_agent.identity import IdentityContext, IdentityProvider, LocalIdentityProvider|["vonavy_agent"]|
|src/vonavy_agent/api.py|31|from vonavy_agent.jobs import (     enqueue_export,     enqueue_job,     enqueue_run,     request_cancellation, )|["vonavy_agent"]|
|src/vonavy_agent/api.py|37|from vonavy_agent.managed_files import verified_managed_file|["vonavy_agent"]|
|src/vonavy_agent/api.py|38|from vonavy_agent.persistence import (     DataProfile,     Dataset,     DatasetVersion,     ExperimentSpecRow,     Export,     GateResultRow,     Job,     Run,     RunMetric,     create_db_engine,     new_id, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/api.py|51|from vonavy_agent.planner import confirm_proposal, propose_experiments|["vonavy_agent"]|
|src/vonavy_agent/api.py|52|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/backtest.py|22|from sklearn.compose import ColumnTransformer|["sklearn"]|
|src/vonavy_agent/backtest.py|23|from sklearn.linear_model import Ridge|["sklearn"]|
|src/vonavy_agent/backtest.py|24|from sklearn.pipeline import Pipeline|["sklearn"]|
|src/vonavy_agent/backtest.py|25|from sklearn.preprocessing import OneHotEncoder, StandardScaler|["sklearn"]|
|src/vonavy_agent/backtest.py|26|from sqlalchemy import delete|["sqlalchemy"]|
|src/vonavy_agent/backtest.py|27|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|src/vonavy_agent/backtest.py|28|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/backtest.py|30|from vonavy_agent.datasets import DatasetRegistry, observation_availability|["dataset", "vonavy_agent"]|
|src/vonavy_agent/backtest.py|31|from vonavy_agent.domain import (     AvailabilityKind,     DatasetMappingSpec,     ExperimentSpec,     FeatureMapping,     FeatureRole,     MovingAverageConfig,     RidgeDirectConfig,     SeasonalNaiveConfig, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/backtest.py|41|from vonavy_agent.eligibility import expected_grid|["vonavy_agent"]|
|src/vonavy_agent/backtest.py|42|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/backtest.py|43|from vonavy_agent.hashing import canonical_hash, canonical_json, file_hash|["vonavy_agent"]|
|src/vonavy_agent/backtest.py|44|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/backtest.py|45|from vonavy_agent.managed_files import fsync_tree|["vonavy_agent"]|
|src/vonavy_agent/backtest.py|46|from vonavy_agent.persistence import (     DataProfile,     DatasetMapping,     ExperimentSpecRow,     GateResultRow,     Job,     Run,     RunMetric, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/backtest.py|55|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/backtest.py|675|from vonavy_agent.persistence import DatasetVersion|["dataset", "vonavy_agent"]|
|src/vonavy_agent/cli.py|9|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/cli.py|13|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/cli.py|79|from vonavy_agent.api import create_app, migrate|["vonavy_agent"]|
|src/vonavy_agent/cli.py|80|from vonavy_agent.jobs import Worker|["vonavy_agent"]|
|src/vonavy_agent/cli.py|81|from vonavy_agent.persistence import create_db_engine|["vonavy_agent"]|
|src/vonavy_agent/cli.py|73|from vonavy_agent.validation_worker.cli import run_cli as run_validation_cli|["vonavy_agent"]|
|src/vonavy_agent/datasets.py|20|from sqlalchemy import func, select|["sqlalchemy"]|
|src/vonavy_agent/datasets.py|21|from sqlalchemy.dialects.sqlite import insert as sqlite_insert|["sqlalchemy"]|
|src/vonavy_agent/datasets.py|22|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|src/vonavy_agent/datasets.py|23|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/datasets.py|25|from vonavy_agent.domain import (     AvailabilityKind,     DatasetMappingSpec, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/datasets.py|29|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/datasets.py|30|from vonavy_agent.hashing import canonical_hash, canonical_json, file_hash|["vonavy_agent"]|
|src/vonavy_agent/datasets.py|31|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/datasets.py|32|from vonavy_agent.persistence import (     Blob,     DataProfile,     Dataset,     DatasetMapping,     DatasetVersion,     session_scope, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/datasets.py|40|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/eligibility.py|8|from vonavy_agent.domain import OriginSpec|["vonavy_agent"]|
|src/vonavy_agent/executor.py|16|from sqlalchemy import update|["sqlalchemy"]|
|src/vonavy_agent/executor.py|17|from sqlalchemy.engine import CursorResult|["sqlalchemy"]|
|src/vonavy_agent/executor.py|18|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/executor.py|20|from vonavy_agent.backtest import RunCancelled, persist_run, run_backtest|["vonavy_agent"]|
|src/vonavy_agent/executor.py|21|from vonavy_agent.datasets import (     DatasetRegistry,     compute_profile,     publish_profile, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/executor.py|26|from vonavy_agent.domain import JobState|["vonavy_agent"]|
|src/vonavy_agent/executor.py|27|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/executor.py|28|from vonavy_agent.experiments import compute_gate, publish_gate|["vonavy_agent"]|
|src/vonavy_agent/executor.py|29|from vonavy_agent.exporting import stage_static_export|["vonavy_agent"]|
|src/vonavy_agent/executor.py|30|from vonavy_agent.hashing import canonical_json|["vonavy_agent"]|
|src/vonavy_agent/executor.py|31|from vonavy_agent.persistence import (     Export,     Job,     JobEvent,     Run,     create_db_engine,     new_id,     session_scope, )|["vonavy_agent"]|
|src/vonavy_agent/executor.py|40|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/experiments.py|9|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|src/vonavy_agent/experiments.py|10|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/experiments.py|12|from vonavy_agent.datasets import DatasetRegistry, observation_availability|["dataset", "vonavy_agent"]|
|src/vonavy_agent/experiments.py|13|from vonavy_agent.domain import (     AvailabilityKind,     DatasetMappingSpec,     ExperimentSpec,     FeatureRole,     GateReason,     GateReport, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/experiments.py|21|from vonavy_agent.eligibility import expected_grid|["vonavy_agent"]|
|src/vonavy_agent/experiments.py|22|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/experiments.py|23|from vonavy_agent.hashing import canonical_hash, canonical_json|["vonavy_agent"]|
|src/vonavy_agent/experiments.py|24|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/experiments.py|25|from vonavy_agent.persistence import (     DataProfile,     DatasetMapping,     DatasetVersion,     ExperimentSpecRow,     GateResultRow,     session_scope, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/experiments.py|33|from vonavy_agent.policy import ResourcePolicy|["vonavy_agent"]|
|src/vonavy_agent/experiments.py|363|from vonavy_agent.backtest import _prepare_data, model_feasibility|["vonavy_agent"]|
|src/vonavy_agent/exporting.py|13|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|src/vonavy_agent/exporting.py|14|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/exporting.py|16|from vonavy_agent.domain import JobState|["vonavy_agent"]|
|src/vonavy_agent/exporting.py|17|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/exporting.py|18|from vonavy_agent.hashing import canonical_json, file_hash|["vonavy_agent"]|
|src/vonavy_agent/exporting.py|19|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/exporting.py|20|from vonavy_agent.managed_files import fsync_tree, verified_managed_file|["vonavy_agent"]|
|src/vonavy_agent/exporting.py|21|from vonavy_agent.persistence import (     ExperimentSpecRow,     GateResultRow,     Job,     Run, )|["vonavy_agent"]|
|src/vonavy_agent/exporting.py|27|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/forecasting/__init__.py|3|from vonavy_agent.forecasting.contracts import (     ADAPTER_ID,     CHRONOS2_ADAPTER_ID,     NEURALNET_ADAPTER_ID, )|["chronos", "forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/aws_batch.py|13|import boto3  # type: ignore[import-untyped]|["boto3"]|
|src/vonavy_agent/forecasting/aws_batch.py|18|from vonavy_agent.forecasting.chronos2 import run_chronos2_forecast|["chronos", "forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/aws_batch.py|19|from vonavy_agent.forecasting.contracts import (     AdapterIdentity,     ForecastIssue,     ForecastProfile,     ForecastRequest,     ForecastResult,     ForecastStatus,     ForecastTiming,     HoldoutMetrics,     InputIdentity, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/aws_batch.py|30|from vonavy_agent.forecasting.model import run_xgboost_forecast, sha256_file|["forecast", "vonavy_agent", "xgboost"]|
|src/vonavy_agent/forecasting/aws_batch.py|31|from vonavy_agent.forecasting.neural_net import run_neuralnet_forecast|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/chronos2.py|18|from vonavy_agent.forecasting.contracts import (     CHRONOS2_ADAPTER_ID,     CHRONOS2_SOURCE_REPOSITORY,     CHRONOS2_SOURCE_REVISION,     AdapterIdentity,     ArtifactReference,     ForecastArtifacts,     ForecastMapping,     ForecastProfile,     ForecastResult,     ForecastStatus,     ForecastTiming,     HoldoutMetrics,     InputIdentity,     ModelArtifactManifest, )|["chronos", "forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/chronos2.py|34|from vonavy_agent.forecasting.evaluation import build_forecast_evaluation|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/chronos2.py|35|from vonavy_agent.forecasting.model import ForecastRunOutput, sha256_file|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/chronos2.py|36|from vonavy_agent.forecasting.panel import PreparedPanel, build_panel_frames, prepare_daily_panel|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/chronos2.py|84|import torch|["torch"]|
|src/vonavy_agent/forecasting/chronos2.py|85|from chronos import BaseChronosPipeline|["chronos"]|
|src/vonavy_agent/forecasting/evaluation.py|11|from vonavy_agent.forecasting.contracts import (     BaselineSkillEvidence,     EntityErrorEvidence,     EvaluationSafetyEvidence,     FeatureShiftEvidence,     ForecastEvaluationEvidence, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/mapping.py|13|from vonavy_agent.forecasting.contracts import ADAPTER_ID, MODEL_SOURCE_REVISION, ForecastMapping|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/model.py|15|from vonavy_agent.forecasting.contracts import (     AdapterIdentity,     ArtifactReference,     ForecastArtifacts,     ForecastMapping,     ForecastProfile,     ForecastResult,     ForecastStatus,     ForecastTiming,     HoldoutMetrics,     InputIdentity,     ModelArtifactManifest, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/model.py|28|from vonavy_agent.forecasting.evaluation import build_forecast_evaluation|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/model.py|29|from vonavy_agent.forecasting.panel import (     PanelFrames,     PreparedPanel,     build_panel_frames,     prepare_daily_panel,     tree_frame, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/model.py|71|from xgboost import XGBRegressor|["xgboost"]|
|src/vonavy_agent/forecasting/neural_net.py|13|import torch|["torch"]|
|src/vonavy_agent/forecasting/neural_net.py|14|from torch import nn|["torch"]|
|src/vonavy_agent/forecasting/neural_net.py|16|from vonavy_agent.forecasting.contracts import (     NEURALNET_ADAPTER_ID,     AdapterIdentity,     ArtifactReference,     ForecastArtifacts,     ForecastIssue,     ForecastMapping,     ForecastProfile,     ForecastResult,     ForecastStatus,     ForecastTiming,     HoldoutMetrics,     InputIdentity,     ModelArtifactManifest, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/neural_net.py|31|from vonavy_agent.forecasting.evaluation import build_forecast_evaluation|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/neural_net.py|32|from vonavy_agent.forecasting.model import (     ForecastRunOutput,     _metrics,     _write_forecast,     sha256_file, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/neural_net.py|38|from vonavy_agent.forecasting.panel import (     PanelFrames,     PreparedPanel,     build_panel_frames,     prepare_daily_panel, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/panel.py|11|from vonavy_agent.forecasting.contracts import ForecastIssue, ForecastMapping|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/worker.py|14|from vonavy_agent.forecasting.chronos2 import run_chronos2_forecast|["chronos", "forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/worker.py|15|from vonavy_agent.forecasting.contracts import (     ADAPTER_ID,     AdapterId,     AdapterIdentity,     ForecastIssue,     ForecastProfile,     ForecastResult,     ForecastStatus,     ForecastTiming,     HoldoutMetrics,     InputIdentity,     LocalForecastRequest, )|["forecast", "vonavy_agent"]|
|src/vonavy_agent/forecasting/worker.py|28|from vonavy_agent.forecasting.model import run_xgboost_forecast|["forecast", "vonavy_agent", "xgboost"]|
|src/vonavy_agent/forecasting/worker.py|29|from vonavy_agent.forecasting.neural_net import run_neuralnet_forecast|["forecast", "vonavy_agent"]|
|src/vonavy_agent/identity.py|8|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/jobs.py|17|from sqlalchemy import delete, select, update|["sqlalchemy"]|
|src/vonavy_agent/jobs.py|18|from sqlalchemy.engine import CursorResult, Engine|["sqlalchemy"]|
|src/vonavy_agent/jobs.py|19|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/jobs.py|21|from vonavy_agent.domain import (     CURRENT_GATE_POLICY_VERSION,     TERMINAL_JOB_STATES,     JobState, )|["vonavy_agent"]|
|src/vonavy_agent/jobs.py|26|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/jobs.py|27|from vonavy_agent.hashing import canonical_json, file_hash|["vonavy_agent"]|
|src/vonavy_agent/jobs.py|28|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/jobs.py|29|from vonavy_agent.managed_files import verified_managed_file, verify_run_bundle|["vonavy_agent"]|
|src/vonavy_agent/jobs.py|30|from vonavy_agent.persistence import (     DataProfile,     DatasetMapping,     DatasetVersion,     ExperimentSpecRow,     Export,     GateResultRow,     Job,     JobEvent,     Run,     RunMetric,     session_scope, )|["dataset", "vonavy_agent"]|
|src/vonavy_agent/jobs.py|43|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/jobs.py|1042|from vonavy_agent.backtest import _environment, _source_revision|["vonavy_agent"]|
|src/vonavy_agent/managed_files.py|13|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/managed_files.py|14|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|src/vonavy_agent/migrations/env.py|3|from alembic import context|["alembic"]|
|src/vonavy_agent/migrations/env.py|4|from sqlalchemy import engine_from_config, pool|["sqlalchemy"]|
|src/vonavy_agent/migrations/env.py|6|from vonavy_agent.persistence import Base|["vonavy_agent"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|3|import sqlalchemy as sa|["sqlalchemy"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|4|from alembic import op|["alembic"]|
|src/vonavy_agent/migrations/versions/0002_owner_scope.py|3|import sqlalchemy as sa|["sqlalchemy"]|
|src/vonavy_agent/migrations/versions/0002_owner_scope.py|4|from alembic import op|["alembic"]|
|src/vonavy_agent/persistence.py|9|from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, create_engine, event|["sqlalchemy"]|
|src/vonavy_agent/persistence.py|10|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|src/vonavy_agent/persistence.py|11|from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column|["sqlalchemy"]|
|src/vonavy_agent/persistence.py|13|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/planner.py|7|from sqlalchemy import select|["sqlalchemy"]|
|src/vonavy_agent/planner.py|8|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|src/vonavy_agent/planner.py|9|from sqlalchemy.orm import Session|["sqlalchemy"]|
|src/vonavy_agent/planner.py|11|from vonavy_agent.adapters import adapter_capabilities|["vonavy_agent"]|
|src/vonavy_agent/planner.py|12|from vonavy_agent.domain import ExperimentSpec, JobState|["vonavy_agent"]|
|src/vonavy_agent/planner.py|13|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/planner.py|14|from vonavy_agent.experiments import create_experiment_spec|["vonavy_agent"]|
|src/vonavy_agent/planner.py|15|from vonavy_agent.hashing import canonical_hash, canonical_json|["vonavy_agent"]|
|src/vonavy_agent/planner.py|16|from vonavy_agent.identity import LOCAL_OWNER_ID|["vonavy_agent"]|
|src/vonavy_agent/planner.py|17|from vonavy_agent.persistence import (     DataProfile,     ExperimentSpecRow,     Job,     PlannerProposal,     Run,     RunMetric,     session_scope, )|["vonavy_agent"]|
|src/vonavy_agent/planner.py|26|from vonavy_agent.policy import ResourcePolicy|["vonavy_agent"]|
|src/vonavy_agent/policy.py|5|from vonavy_agent.domain import ResourceLimits|["vonavy_agent"]|
|src/vonavy_agent/policy.py|6|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|src/vonavy_agent/ports.py|9|from vonavy_agent.domain import EvaluationSpec, ForecastSpec, InferenceSpec|["forecast", "vonavy_agent"]|
|src/vonavy_agent/ports.py|10|from vonavy_agent.identity import IdentityContext|["vonavy_agent"]|
|src/vonavy_agent/settings.py|11|from vonavy_agent.policy import ResourcePolicy|["vonavy_agent"]|
|src/vonavy_agent/validation_contracts.py|10|from vonavy_agent.domain import StrictModel|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/__init__.py|1|from vonavy_agent.validation_worker.worker import validate_request|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/artifacts.py|11|from vonavy_agent.validation_contracts import InputArtifact, LocalInputArtifact|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/aws_artifacts.py|11|from vonavy_agent.validation_contracts import (     InputArtifact,     S3InputArtifact,     S3OutputArtifact, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/aws_artifacts.py|16|from vonavy_agent.validation_worker.artifacts import (     ArtifactTooLargeError,     UnsafeArtifactPathError, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/aws_batch.py|8|import boto3  # type: ignore[import-untyped]|["boto3"]|
|src/vonavy_agent/validation_worker/aws_batch.py|10|from vonavy_agent.hashing import canonical_json|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/aws_batch.py|11|from vonavy_agent.validation_contracts import (     S3InputArtifact,     S3OutputArtifact,     ValidationRequest,     ValidationStatus, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/aws_batch.py|17|from vonavy_agent.validation_worker.aws_artifacts import (     S3FileArtifactReader,     S3FileArtifactWriter, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/aws_batch.py|21|from vonavy_agent.validation_worker.worker import validate_request|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/cli.py|10|from vonavy_agent import __version__|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/cli.py|11|from vonavy_agent.hashing import canonical_json|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/cli.py|12|from vonavy_agent.validation_contracts import (     LocalInputArtifact,     LocalOutputArtifact,     ValidationIssue,     ValidationRequest,     ValidationResourceUsage,     ValidationResult,     ValidationStatus, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/cli.py|21|from vonavy_agent.validation_worker.artifacts import (     LocalFileArtifactReader,     LocalFileArtifactWriter,     LocalWorkspace,     UnsafeArtifactPathError, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/cli.py|27|from vonavy_agent.validation_worker.worker import validate_request|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/profiling.py|15|from vonavy_agent.validation_contracts import (     BooleanStatistics,     ColumnLogicalType,     ColumnProfile,     NumericStatistics,     QuantileName,     StringStatistics,     TemporalStatistics,     TopValue,     ValidationIssue,     ValidationLimits, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/worker.py|13|from vonavy_agent import __version__|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/worker.py|14|from vonavy_agent.validation_contracts import (     SUPPORTED_VALIDATION_MEDIA_TYPES,     ColumnProfile,     InputArtifact,     InputIdentity,     LocalInputArtifact,     LocalInputIdentity,     S3InputIdentity,     ValidationIssue,     ValidationRequest,     ValidationResourceUsage,     ValidationResult,     ValidationStatus, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/worker.py|28|from vonavy_agent.validation_worker.artifacts import (     ArtifactReader,     ArtifactTooLargeError,     UnsafeArtifactPathError, )|["vonavy_agent"]|
|src/vonavy_agent/validation_worker/worker.py|33|from vonavy_agent.validation_worker.profiling import (     Deadline,     ScanProblem,     build_profiles,     scan_csv,     scan_parquet, )|["vonavy_agent"]|
|tests/conftest.py|10|from sqlalchemy.engine import Engine|["sqlalchemy"]|
|tests/conftest.py|12|from vonavy_agent.api import migrate|["vonavy_agent"]|
|tests/conftest.py|13|from vonavy_agent.datasets import DatasetRegistry, build_profile|["dataset", "vonavy_agent"]|
|tests/conftest.py|14|from vonavy_agent.domain import (     AvailabilityKind,     AvailabilityPolicy,     DatasetMappingSpec,     DateRange,     ExperimentSpec,     FeatureMapping,     FeatureRole,     MovingAverageConfig,     OriginSpec,     ResourceLimits,     RidgeDirectConfig,     SeasonalNaiveConfig, )|["dataset", "vonavy_agent"]|
|tests/conftest.py|28|from vonavy_agent.experiments import create_experiment_spec|["vonavy_agent"]|
|tests/conftest.py|29|from vonavy_agent.persistence import DataProfile, DatasetMapping, DatasetVersion, create_db_engine|["dataset", "vonavy_agent"]|
|tests/conftest.py|30|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|tests/test_api.py|6|from vonavy_agent.api import create_app|["vonavy_agent"]|
|tests/test_blocker_regressions.py|18|from sqlalchemy import func, select|["sqlalchemy"]|
|tests/test_blocker_regressions.py|19|from sqlalchemy.orm import Session|["sqlalchemy"]|
|tests/test_blocker_regressions.py|21|from vonavy_agent.backtest import _metric_records, _source_revision|["vonavy_agent"]|
|tests/test_blocker_regressions.py|22|from vonavy_agent.datasets import build_profile|["dataset", "vonavy_agent"]|
|tests/test_blocker_regressions.py|23|from vonavy_agent.domain import (     AvailabilityKind,     AvailabilityPolicy,     DatasetMappingSpec,     ExperimentSpec,     FeatureMapping,     FeatureRole,     JobState,     MovingAverageConfig,     RidgeDirectConfig,     SeasonalNaiveConfig, )|["dataset", "vonavy_agent"]|
|tests/test_blocker_regressions.py|35|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|tests/test_blocker_regressions.py|36|from vonavy_agent.experiments import create_experiment_spec, run_gate|["vonavy_agent"]|
|tests/test_blocker_regressions.py|37|from vonavy_agent.jobs import (     StreamCollector,     Worker,     enqueue_job,     enqueue_run,     request_cancellation, )|["vonavy_agent"]|
|tests/test_blocker_regressions.py|44|from vonavy_agent.persistence import (     Blob,     DatasetVersion,     Job,     Run,     session_scope, )|["dataset", "vonavy_agent"]|
|tests/test_blocker_regressions.py|51|from vonavy_agent.planner import confirm_proposal, propose_experiments|["vonavy_agent"]|
|tests/test_blocker_regressions.py|52|from vonavy_agent.settings import Settings|["vonavy_agent"]|
|tests/test_chronos2_forecasting.py|11|from vonavy_agent.forecasting.chronos2 import (     CHRONOS2_MODEL_ID,     CHRONOS2_MODEL_REVISION,     run_chronos2_forecast, )|["chronos", "forecast", "vonavy_agent"]|
|tests/test_chronos2_forecasting.py|16|from vonavy_agent.forecasting.contracts import (     CHRONOS2_SOURCE_REPOSITORY,     CHRONOS2_SOURCE_REVISION,     AdapterIdentity,     ForecastLimits,     ForecastMapping,     InputIdentity,     LocalForecastRequest, )|["chronos", "forecast", "vonavy_agent"]|
|tests/test_chronos2_forecasting.py|202|import vonavy_agent.forecasting.worker as worker_module|["forecast", "vonavy_agent"]|
|tests/test_comparison_request_json_validation.py|5|from vonavy_agent.forecasting.aws_batch import _validate_comparison_request|["forecast", "vonavy_agent"]|
|tests/test_dataset_and_gate.py|9|from vonavy_agent.datasets import build_profile|["dataset", "vonavy_agent"]|
|tests/test_dataset_and_gate.py|10|from vonavy_agent.domain import (     AvailabilityKind,     AvailabilityPolicy,     DatasetMappingSpec, )|["dataset", "vonavy_agent"]|
|tests/test_dataset_and_gate.py|15|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|tests/test_dataset_and_gate.py|16|from vonavy_agent.experiments import create_experiment_spec, run_gate|["vonavy_agent"]|
|tests/test_dataset_and_gate.py|17|from vonavy_agent.hashing import file_hash|["vonavy_agent"]|
|tests/test_dataset_and_gate.py|18|from vonavy_agent.persistence import Blob|["vonavy_agent"]|
|tests/test_dataset_and_gate.py|84|from vonavy_agent.domain import MovingAverageConfig, SeasonalNaiveConfig|["vonavy_agent"]|
|tests/test_final_review_regressions.py|14|from sqlalchemy import func, select|["sqlalchemy"]|
|tests/test_final_review_regressions.py|15|from sqlalchemy.exc import IntegrityError|["sqlalchemy"]|
|tests/test_final_review_regressions.py|16|from sqlalchemy.orm import Session|["sqlalchemy"]|
|tests/test_final_review_regressions.py|18|import vonavy_agent.jobs as jobs_module|["vonavy_agent"]|
|tests/test_final_review_regressions.py|19|from vonavy_agent.adapters import import_adapter_snapshot|["vonavy_agent"]|
|tests/test_final_review_regressions.py|20|from vonavy_agent.datasets import (     build_profile,     compute_profile,     publish_profile, )|["dataset", "vonavy_agent"]|
|tests/test_final_review_regressions.py|25|from vonavy_agent.domain import (     AvailabilityKind,     AvailabilityPolicy,     DatasetMappingSpec,     JobState,     MovingAverageConfig,     SeasonalNaiveConfig, )|["dataset", "vonavy_agent"]|
|tests/test_final_review_regressions.py|33|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|tests/test_final_review_regressions.py|34|from vonavy_agent.executor import ExecutionContext, LeaseLost|["vonavy_agent"]|
|tests/test_final_review_regressions.py|35|from vonavy_agent.experiments import create_experiment_spec, run_gate|["vonavy_agent"]|
|tests/test_final_review_regressions.py|36|from vonavy_agent.exporting import create_static_export, safe_embedded_json|["vonavy_agent"]|
|tests/test_final_review_regressions.py|37|from vonavy_agent.jobs import (     OUTPUT_LIMIT_BYTES,     Worker,     enqueue_export,     enqueue_job,     enqueue_run, )|["vonavy_agent"]|
|tests/test_final_review_regressions.py|44|from vonavy_agent.managed_files import verified_managed_file|["vonavy_agent"]|
|tests/test_final_review_regressions.py|45|from vonavy_agent.persistence import (     AdapterSnapshot,     DataProfile,     Export,     Job,     Run,     RunMetric,     new_id,     session_scope, )|["vonavy_agent"]|
|tests/test_final_review_regressions.py|55|from vonavy_agent.planner import propose_experiments|["vonavy_agent"]|
|tests/test_forecasting.py|13|from vonavy_agent.forecasting.contracts import (     ForecastLimits,     ForecastMapping,     InputIdentity,     LocalForecastRequest, )|["forecast", "vonavy_agent"]|
|tests/test_forecasting.py|19|from vonavy_agent.forecasting.mapping import (     build_forecast_plan,     confirmation_token_for_forecast_plan,     suggest_forecast_mapping, )|["forecast", "vonavy_agent"]|
|tests/test_forecasting.py|24|from vonavy_agent.forecasting.model import XGBOOST_PARAMETERS, run_xgboost_forecast|["forecast", "vonavy_agent", "xgboost"]|
|tests/test_forecasting.py|25|from vonavy_agent.forecasting.panel import build_panel_frames, prepare_daily_panel|["forecast", "vonavy_agent"]|
|tests/test_forecasting.py|26|from vonavy_agent.forecasting.worker import run_local|["forecast", "vonavy_agent"]|
|tests/test_last_delta_regressions.py|11|from sqlalchemy import select|["sqlalchemy"]|
|tests/test_last_delta_regressions.py|12|from sqlalchemy.orm import Session|["sqlalchemy"]|
|tests/test_last_delta_regressions.py|14|from vonavy_agent.api import create_app|["vonavy_agent"]|
|tests/test_last_delta_regressions.py|15|from vonavy_agent.datasets import build_profile|["dataset", "vonavy_agent"]|
|tests/test_last_delta_regressions.py|16|from vonavy_agent.domain import (     AvailabilityKind,     AvailabilityPolicy,     DatasetMappingSpec,     JobState,     SeasonalNaiveConfig, )|["dataset", "vonavy_agent"]|
|tests/test_last_delta_regressions.py|23|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|tests/test_last_delta_regressions.py|24|from vonavy_agent.experiments import create_experiment_spec, run_gate|["vonavy_agent"]|
|tests/test_last_delta_regressions.py|25|from vonavy_agent.exporting import create_static_export|["vonavy_agent"]|
|tests/test_last_delta_regressions.py|26|from vonavy_agent.hashing import canonical_json|["vonavy_agent"]|
|tests/test_last_delta_regressions.py|27|from vonavy_agent.jobs import (     Worker,     enqueue_export,     enqueue_run, )|["vonavy_agent"]|
|tests/test_last_delta_regressions.py|32|from vonavy_agent.persistence import (     GateResultRow,     Job,     Run,     RunMetric,     new_id,     session_scope, )|["vonavy_agent"]|
|tests/test_neuralnet_forecasting.py|10|import torch|["torch"]|
|tests/test_neuralnet_forecasting.py|13|from vonavy_agent.forecasting import neural_net as neural_net_module|["forecast", "vonavy_agent"]|
|tests/test_neuralnet_forecasting.py|14|from vonavy_agent.forecasting.contracts import (     ForecastLimits,     ForecastMapping,     InputIdentity,     LocalForecastRequest, )|["forecast", "vonavy_agent"]|
|tests/test_neuralnet_forecasting.py|20|from vonavy_agent.forecasting.neural_net import (     FINAL_SEEDS,     NEURALNET_PARAMETERS,     run_neuralnet_forecast, )|["forecast", "vonavy_agent"]|
|tests/test_phase0_cloud_boundaries.py|7|from alembic import command|["alembic"]|
|tests/test_phase0_cloud_boundaries.py|8|from alembic.config import Config|["alembic"]|
|tests/test_phase0_cloud_boundaries.py|11|from sqlalchemy import create_engine, inspect, text|["sqlalchemy"]|
|tests/test_phase0_cloud_boundaries.py|13|from vonavy_agent.api import create_app|["vonavy_agent"]|
|tests/test_phase0_cloud_boundaries.py|14|from vonavy_agent.domain import (     DateRange,     ForecastSpec,     InferenceSpec,     MovingAverageConfig,     parse_run_spec, )|["forecast", "vonavy_agent"]|
|tests/test_phase0_cloud_boundaries.py|21|from vonavy_agent.identity import IdentityContext|["vonavy_agent"]|
|tests/test_phase4d_evaluation.py|10|from vonavy_agent.forecasting.contracts import ForecastEvaluationEvidence|["forecast", "vonavy_agent"]|
|tests/test_phase4d_evaluation.py|11|from vonavy_agent.forecasting.evaluation import build_forecast_evaluation|["forecast", "vonavy_agent"]|
|tests/test_runner_planner_export.py|11|from sqlalchemy import func, select|["sqlalchemy"]|
|tests/test_runner_planner_export.py|12|from sqlalchemy.orm import Session|["sqlalchemy"]|
|tests/test_runner_planner_export.py|14|from vonavy_agent.adapters import PreparedInvocation, dry_run_invocation|["vonavy_agent"]|
|tests/test_runner_planner_export.py|15|from vonavy_agent.backtest import PreparedData, _ridge_predictions|["vonavy_agent"]|
|tests/test_runner_planner_export.py|16|from vonavy_agent.domain import DatasetMappingSpec, JobState, RidgeDirectConfig|["dataset", "vonavy_agent"]|
|tests/test_runner_planner_export.py|17|from vonavy_agent.errors import AgentError|["vonavy_agent"]|
|tests/test_runner_planner_export.py|18|from vonavy_agent.experiments import run_gate|["vonavy_agent"]|
|tests/test_runner_planner_export.py|19|from vonavy_agent.exporting import create_static_export|["vonavy_agent"]|
|tests/test_runner_planner_export.py|20|from vonavy_agent.jobs import Worker, enqueue_job, enqueue_run, request_cancellation|["vonavy_agent"]|
|tests/test_runner_planner_export.py|21|from vonavy_agent.persistence import Job, Run, RunMetric, session_scope|["vonavy_agent"]|
|tests/test_runner_planner_export.py|22|from vonavy_agent.planner import propose_experiments|["vonavy_agent"]|
|tests/test_terminal_bundle_regressions.py|6|from sqlalchemy.orm import Session|["sqlalchemy"]|
|tests/test_terminal_bundle_regressions.py|8|from vonavy_agent.domain import JobState|["vonavy_agent"]|
|tests/test_terminal_bundle_regressions.py|9|from vonavy_agent.experiments import run_gate|["vonavy_agent"]|
|tests/test_terminal_bundle_regressions.py|10|from vonavy_agent.hashing import canonical_json|["vonavy_agent"]|
|tests/test_terminal_bundle_regressions.py|11|from vonavy_agent.jobs import Worker, enqueue_export, enqueue_run|["vonavy_agent"]|
|tests/test_terminal_bundle_regressions.py|12|from vonavy_agent.persistence import (     Export,     Job,     Run,     RunMetric,     new_id,     session_scope, )|["vonavy_agent"]|
|tests/test_validation_aws.py|10|from vonavy_agent.validation_contracts import S3InputArtifact, S3OutputArtifact|["vonavy_agent"]|
|tests/test_validation_aws.py|11|from vonavy_agent.validation_worker import aws_batch|["vonavy_agent"]|
|tests/test_validation_aws.py|12|from vonavy_agent.validation_worker.artifacts import ArtifactTooLargeError, UnsafeArtifactPathError|["vonavy_agent"]|
|tests/test_validation_aws.py|13|from vonavy_agent.validation_worker.aws_artifacts import (     S3FileArtifactReader,     S3FileArtifactWriter, )|["vonavy_agent"]|
|tests/test_validation_cli.py|9|from vonavy_agent.validation_contracts import ValidationResult|["vonavy_agent"]|
|tests/test_validation_cli.py|88|from vonavy_agent.validation_worker import cli as validation_cli|["vonavy_agent"]|
|tests/test_validation_contracts.py|8|from vonavy_agent.validation_contracts import ValidationRequest|["vonavy_agent"]|
|tests/test_validation_worker.py|10|from vonavy_agent.hashing import canonical_json|["vonavy_agent"]|
|tests/test_validation_worker.py|11|from vonavy_agent.validation_contracts import (     LocalInputArtifact,     LocalOutputArtifact,     ValidationLimits,     ValidationRequest,     ValidationStatus, )|["vonavy_agent"]|
|tests/test_validation_worker.py|18|from vonavy_agent.validation_worker.artifacts import (     LocalFileArtifactReader,     LocalWorkspace,     UnsafeArtifactPathError, )|["vonavy_agent"]|
|tests/test_validation_worker.py|23|from vonavy_agent.validation_worker.worker import validate_request|["vonavy_agent"]|
|tests/test_validation_worker.py|338|from vonavy_agent.validation_contracts import S3InputArtifact, S3OutputArtifact|["vonavy_agent"]|
|tests/test_validation_worker.py|379|from vonavy_agent.validation_worker import worker as worker_module|["vonavy_agent"]|

## Legacy routes

|path|line|route|context|
|---|---|---|---|
|docs/phase-1-serverless-control-plane.md|86|GET /api/health|- `GET /api/health`|
|docs/phase-1-serverless-control-plane.md|87|POST /api/upload-sessions|- `POST /api/upload-sessions`|
|docs/phase-1-serverless-control-plane.md|88|POST /api/upload-sessions/{upload_id|- `POST /api/upload-sessions/{upload_id}/complete`|
|docs/phase-1-serverless-control-plane.md|89|GET /api/datasets|- `GET /api/datasets`|
|docs/phase-2b-aws-validation.md|15|POST /api/datasets/{dataset_id|-> POST /api/datasets/{dataset_id}/validations|
|docs/phase-2b-aws-validation.md|21|GET /api/validations/{job_id|-> polling reconciliation through GET /api/validations/{job_id}|
|docs/phase-2b-aws-validation.md|40|POST /api/datasets/{dataset_id|POST /api/datasets/{dataset_id}/validations|
|docs/phase-2b-aws-validation.md|71|/api/validations/...|"status": "/api/validations/...",|
|docs/phase-2b-aws-validation.md|72|/api/validations/.../result|"result": "/api/validations/.../result"|
|docs/phase-2b-aws-validation.md|84|GET /api/validations/{job_id|GET /api/validations/{job_id}|
|docs/phase-2b-aws-validation.md|109|GET /api/validations/{job_id|GET /api/validations/{job_id}/result|
|infra/lambda/control_plane/handler.py|332|POST /api/upload-sessions|"route": "POST /api/upload-sessions",|
|infra/lambda/control_plane/handler.py|370|POST /api/upload-sessions|"route": "POST /api/upload-sessions",|
|infra/lambda/control_plane/handler.py|1521|/api/health|if method == "GET" and path == "/api/health":|
|infra/lambda/control_plane/handler.py|1523|/api/upload-sessions|if method == "POST" and path == "/api/upload-sessions":|
|infra/lambda/control_plane/handler.py|1527|/api/upload-sessions/|and path.startswith("/api/upload-sessions/")|
|infra/lambda/control_plane/handler.py|1530|/api/upload-sessions/|upload_id = path.removeprefix("/api/upload-sessions/").removesuffix("/complete")|
|infra/lambda/control_plane/handler.py|1538|/api/datasets|if method == "GET" and path == "/api/datasets":|
|infra/lambda/control_plane/handler.py|1540|/api/datasets/|if method == "POST" and path.startswith("/api/datasets/") and path.endswith("/validations"):|
|infra/lambda/control_plane/handler.py|1541|/api/datasets/|dataset_id = path.removeprefix("/api/datasets/").removesuffix("/validations")|
|infra/lambda/control_plane/handler.py|1544|/api/validations/|if method == "GET" and path.startswith("/api/validations/"):|
|infra/lambda/control_plane/handler.py|1545|/api/validations/|validation_path = path.removeprefix("/api/validations/")|
|infra/lambda/forecast_control_plane/handler.py|2165|/api/forecast-agent/sessions/{item[|"self": f"/api/forecast-agent/sessions/{item['session_id']}",|
|infra/lambda/forecast_control_plane/handler.py|2166|/api/forecast-agent/sessions/{item[|"messages": f"/api/forecast-agent/sessions/{item['session_id']}/messages",|
|infra/lambda/forecast_control_plane/handler.py|2169|/api/datasets/{item[|links["execute"] = f"/api/datasets/{item['dataset_id']}/forecasts"|
|infra/lambda/forecast_control_plane/handler.py|2788|POST /api/datasets/{dataset_id|if route == "POST /api/datasets/{dataset_id}/forecast-agent/sessions" and len(parts) == 6:|
|infra/lambda/forecast_control_plane/handler.py|2790|POST /api/forecasts/{run_id|if route == "POST /api/forecasts/{run_id}/agent/sessions" and len(parts) == 6:|
|infra/lambda/forecast_control_plane/handler.py|2792|POST /api/forecast-agent/sessions/{session_id|if route == "POST /api/forecast-agent/sessions/{session_id}/messages" and len(parts) == 6:|
|infra/lambda/forecast_control_plane/handler.py|2794|GET /api/forecast-agent/sessions/{session_id|if route == "GET /api/forecast-agent/sessions/{session_id}" and len(parts) == 5:|
|infra/lambda/forecast_control_plane/handler.py|2796|POST /api/datasets/{dataset_id|if route == "POST /api/datasets/{dataset_id}/forecast-agent" and len(parts) == 5:|
|infra/lambda/forecast_control_plane/handler.py|2798|POST /api/datasets/{dataset_id|if route == "POST /api/datasets/{dataset_id}/forecasts" and len(parts) == 5:|
|infra/lambda/forecast_control_plane/handler.py|2801|GET /api/forecasts/{run_id|if route == "GET /api/forecasts/{run_id}" and len(parts) == 4:|
|infra/lambda/forecast_control_plane/handler.py|2803|GET /api/forecasts/{run_id|if route == "GET /api/forecasts/{run_id}/result" and len(parts) == 5:|
|infra/tests/auth_token_refresh_smoke.mjs|75|/api/forecasts/run/agent/sessions|'api("/api/forecasts/run/agent/sessions", { method: "POST", body: "{}" })',|
|infra/tests/auth_token_refresh_smoke.mjs|102|/api/forecasts/run/agent/sessions|'api("/api/forecasts/run/agent/sessions", { method: "POST", body: "{}" })',|
|infra/tests/phase5_api_client_smoke.mjs|43|/api/health|const getResult = await vm.runInContext('api("/api/health")', context);|
|infra/tests/phase5_api_client_smoke.mjs|60|/api/upload-sessions|'api("/api/upload-sessions", { method: "POST", body: "{}" })',|
|infra/tests/test_agent_result_conversation.py|96|POST /api/forecasts/{run_id|assert "POST /api/forecasts/{run_id}/agent/sessions" in handler|
|infra/tests/test_forecast_handler.py|134|GET /not-real|"routeKey": "GET /not-real",|
|infra/tests/test_forecast_handler.py|158|POST /api/datasets/{dataset_id}/forecast-agent|"routeKey": "POST /api/datasets/{dataset_id}/forecast-agent",|
|infra/tests/test_forecast_handler.py|158|POST /api/datasets/{dataset_id|"routeKey": "POST /api/datasets/{dataset_id}/forecast-agent",|
|infra/tests/test_forecast_handler.py|159|/api/datasets/00000000-0000-0000-0000-000000000001/forecast-agent|"rawPath": "/api/datasets/00000000-0000-0000-0000-000000000001/forecast-agent",|
|infra/tests/test_forecast_handler.py|452|POST /api/datasets/{dataset_id}/forecast-agent/sessions|"routeKey": "POST /api/datasets/{dataset_id}/forecast-agent/sessions",|
|infra/tests/test_forecast_handler.py|452|POST /api/datasets/{dataset_id|"routeKey": "POST /api/datasets/{dataset_id}/forecast-agent/sessions",|
|infra/tests/test_forecast_handler.py|454|/api/datasets/00000000-0000-0000-0000-000000000001/forecast-agent/sessions|"/api/datasets/00000000-0000-0000-0000-000000000001/forecast-agent/sessions"|
|infra/tests/test_handler.py|465|/api/datasets|owner, email = handler._identity(_event("GET", "/api/datasets"))|
|infra/tests/test_handler.py|469|/api/datasets|event = _event("GET", "/api/datasets")|
|infra/tests/test_handler.py|500|/api/upload-sessions|"/api/upload-sessions",|
|infra/tests/test_handler.py|709|/api/datasets|response = handler.lambda_handler(_event("GET", "/api/datasets"), None)|
|infra/tests/test_handler.py|732|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner),|
|infra/tests/test_handler.py|757|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body(), owner="shape-owner"),|
|infra/tests/test_handler.py|828|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body())|
|infra/tests/test_handler.py|916|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner), None|
|infra/tests/test_handler.py|944|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner), None|
|infra/tests/test_handler.py|962|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body()), None|
|infra/tests/test_handler.py|987|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body(), owner=owner), None|
|infra/tests/test_handler.py|1004|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body()), None|
|infra/tests/test_handler.py|1022|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body()), None|
|infra/tests/test_handler.py|1039|/api/upload-sessions|_event("POST", "/api/upload-sessions", body=_upload_body()), context|
|infra/tests/test_handler.py|1051|POST /api/upload-sessions|assert record.route == "POST /api/upload-sessions"|
|infra/tests/test_phase4_agentic_sources.py|19|POST /api/datasets/{dataset_id|assert "POST /api/datasets/{dataset_id}/forecast-agent/sessions" in handler|
|infra/tests/test_phase4_agentic_sources.py|20|POST /api/forecast-agent/sessions/{session_id|assert "POST /api/forecast-agent/sessions/{session_id}/messages" in handler|
|infra/tests/test_phase4_agentic_sources.py|21|GET /api/forecast-agent/sessions/{session_id|assert "GET /api/forecast-agent/sessions/{session_id}" in handler|
|infra/tests/test_stack.py|567|POST /api/datasets/{dataset_id|"POST /api/datasets/{dataset_id}/forecast-agent",|
|infra/tests/test_stack.py|568|POST /api/datasets/{dataset_id|"POST /api/datasets/{dataset_id}/forecasts",|
|infra/tests/test_stack.py|569|GET /api/forecasts/{run_id|"GET /api/forecasts/{run_id}",|
|infra/tests/test_stack.py|570|GET /api/forecasts/{run_id|"GET /api/forecasts/{run_id}/result",|
|infra/tests/test_stack.py|571|POST /api/forecasts/{run_id|"POST /api/forecasts/{run_id}/agent/sessions",|
|infra/tests/test_stack.py|582|POST /api/datasets/{dataset_id|assert "POST /api/datasets/{dataset_id}/validations" in route_keys|
|infra/tests/test_stack.py|583|GET /api/validations/{job_id|assert "GET /api/validations/{job_id}" in route_keys|
|infra/tests/test_stack.py|584|GET /api/validations/{job_id|assert "GET /api/validations/{job_id}/result" in route_keys|
|infra/vonavy_infra/control_plane_stack.py|920|/api/health|("/api/health", apigwv2.HttpMethod.GET),|
|infra/vonavy_infra/control_plane_stack.py|921|/api/upload-sessions|("/api/upload-sessions", apigwv2.HttpMethod.POST),|
|infra/vonavy_infra/control_plane_stack.py|923|/api/datasets|("/api/datasets", apigwv2.HttpMethod.GET),|
|infra/vonavy_infra/control_plane_stack.py|977|$context.routeKey|'"routeKey":"$context.routeKey",'|
|infra/web/app.js|391|/api/upload-sessions|const session = await api("/api/upload-sessions", {|
|infra/web/app.js|1386|/api/datasets|const payload = await api("/api/datasets");|
|src/vonavy_agent/api.py|171|GET /api/health|@app.get("/api/health")|
|src/vonavy_agent/api.py|171|/api/health|@app.get("/api/health")|
|src/vonavy_agent/api.py|175|GET /api/capabilities|@app.get("/api/capabilities")|
|src/vonavy_agent/api.py|175|/api/capabilities|@app.get("/api/capabilities")|
|src/vonavy_agent/api.py|184|GET /api/inbox|@app.get("/api/inbox")|
|src/vonavy_agent/api.py|184|/api/inbox|@app.get("/api/inbox")|
|src/vonavy_agent/api.py|189|POST /api/inbox/import|@app.post("/api/inbox/import")|
|src/vonavy_agent/api.py|189|/api/inbox/import|@app.post("/api/inbox/import")|
|src/vonavy_agent/api.py|202|POST /api/datasets/upload|@app.post("/api/datasets/upload")|
|src/vonavy_agent/api.py|202|/api/datasets/upload|@app.post("/api/datasets/upload")|
|src/vonavy_agent/api.py|222|GET /api/datasets|@app.get("/api/datasets")|
|src/vonavy_agent/api.py|222|/api/datasets|@app.get("/api/datasets")|
|src/vonavy_agent/api.py|250|GET /api/dataset-versions/{version_id}|@app.get("/api/dataset-versions/{version_id}")|
|src/vonavy_agent/api.py|260|POST /api/dataset-versions/{version_id}/mappings|@app.post("/api/dataset-versions/{version_id}/mappings")|
|src/vonavy_agent/api.py|274|POST /api/profiles|@app.post("/api/profiles")|
|src/vonavy_agent/api.py|274|/api/profiles|@app.post("/api/profiles")|
|src/vonavy_agent/api.py|279|GET /api/profiles/{profile_id}|@app.get("/api/profiles/{profile_id}")|
|src/vonavy_agent/api.py|291|POST /api/specs|@app.post("/api/specs")|
|src/vonavy_agent/api.py|291|/api/specs|@app.post("/api/specs")|
|src/vonavy_agent/api.py|301|GET /api/specs/{spec_id}|@app.get("/api/specs/{spec_id}")|
|src/vonavy_agent/api.py|311|POST /api/specs/{spec_id}/gate|@app.post("/api/specs/{spec_id}/gate")|
|src/vonavy_agent/api.py|321|GET /api/gates/{gate_id}|@app.get("/api/gates/{gate_id}")|
|src/vonavy_agent/api.py|329|POST /api/runs|@app.post("/api/runs")|
|src/vonavy_agent/api.py|329|/api/runs|@app.post("/api/runs")|
|src/vonavy_agent/api.py|340|GET /api/runs|@app.get("/api/runs")|
|src/vonavy_agent/api.py|340|/api/runs|@app.get("/api/runs")|
|src/vonavy_agent/api.py|354|GET /api/runs/{run_id}|@app.get("/api/runs/{run_id}")|
|src/vonavy_agent/api.py|365|GET /api/jobs/{job_id}|@app.get("/api/jobs/{job_id}")|
|src/vonavy_agent/api.py|373|POST /api/jobs/{job_id}/cancel|@app.post("/api/jobs/{job_id}/cancel")|
|src/vonavy_agent/api.py|377|POST /api/comparisons|@app.post("/api/comparisons")|
|src/vonavy_agent/api.py|377|/api/comparisons|@app.post("/api/comparisons")|
|src/vonavy_agent/api.py|425|POST /api/planner/proposals/{spec_id}|@app.post("/api/planner/proposals/{spec_id}")|
|src/vonavy_agent/api.py|430|POST /api/planner/proposals/{proposal_id}/confirm|@app.post("/api/planner/proposals/{proposal_id}/confirm")|
|src/vonavy_agent/api.py|446|GET /api/adapters|@app.get("/api/adapters")|
|src/vonavy_agent/api.py|446|/api/adapters|@app.get("/api/adapters")|
|src/vonavy_agent/api.py|450|POST /api/adapters/import|@app.post("/api/adapters/import")|
|src/vonavy_agent/api.py|450|/api/adapters/import|@app.post("/api/adapters/import")|
|src/vonavy_agent/api.py|470|POST /api/adapters/{adapter_kind}/dry-run|@app.post("/api/adapters/{adapter_kind}/dry-run")|
|src/vonavy_agent/api.py|489|POST /api/exports|@app.post("/api/exports")|
|src/vonavy_agent/api.py|489|/api/exports|@app.post("/api/exports")|
|src/vonavy_agent/api.py|495|GET /api/exports/{export_id}|@app.get("/api/exports/{export_id}")|
|src/vonavy_agent/api.py|515|GET /api/exports/{export_id}/download|@app.get("/api/exports/{export_id}/download")|
|src/vonavy_agent/web/app.js|34|/api/datasets/upload|state.version=await api("/api/datasets/upload",{method:"POST",body:data});|
|src/vonavy_agent/web/app.js|57|/api/profiles|const queued=await api("/api/profiles",jsonOptions({dataset_version_id:state.version.id,mapping_id:state.mapping.id}));|
|src/vonavy_agent/web/app.js|95|/api/specs|state.spec=await api("/api/specs",jsonOptions(spec));|
|src/vonavy_agent/web/app.js|111|/api/runs|const created=await api("/api/runs",jsonOptions({spec_id:state.spec.id,gate_result_id:state.gate.id,confirmation_token:state.gate.report.confirmation_token}));|
|src/vonavy_agent/web/app.js|133|/api/runs|const response=await api("/api/runs");state.runs=response.runs;|
|src/vonavy_agent/web/app.js|144|/api/comparisons|const comparison=await api("/api/comparisons",jsonOptions({run_ids:ids})),node=resultNode("compare-result");|
|src/vonavy_agent/web/app.js|154|/api/exports|const queued=await api("/api/exports",jsonOptions({run_ids:ids})),job=await pollJob(queued.job.id);|
|tests/test_api.py|12|/api/health|health = client.get("/api/health")|
|tests/test_api.py|15|/api/datasets/upload|"/api/datasets/upload",|
|tests/test_api.py|21|/api/dataset-versions/not-found|missing = client.get("/api/dataset-versions/not-found")|
|tests/test_last_delta_regressions.py|118|/api/comparisons|comparison = client.post("/api/comparisons", json={"run_ids": [run.id]})|
|tests/test_phase0_cloud_boundaries.py|38|/api/datasets/upload|"/api/datasets/upload",|
|tests/test_phase0_cloud_boundaries.py|46|/api/datasets|alice = client.get("/api/datasets", headers={"x-test-owner": "alice"})|
|tests/test_phase0_cloud_boundaries.py|47|/api/datasets|bob = client.get("/api/datasets", headers={"x-test-owner": "bob"})|
|tests/test_phase0_cloud_boundaries.py|58|/api/inbox|inbox = client.get("/api/inbox", headers={"x-test-owner": "alice"})|
|tests/test_phase0_cloud_boundaries.py|68|/api/specs|response = client.post("/api/specs", json=spec.model_dump(mode="json"))|

## Legacy persistence entities/classes/tables

### ORM entities

|path|line|class|table|bases|columns|
|---|---|---|---|---|---|
|src/vonavy_agent/persistence.py|16|Base|None|["DeclarativeBase"]|[]|
|src/vonavy_agent/persistence.py|24|Dataset|datasets|["Base"]|["owner_id", "id", "name", "created_at"]|
|src/vonavy_agent/persistence.py|32|Blob|blobs|["Base"]|["sha256", "media_type", "byte_size", "relative_path", "created_at"]|
|src/vonavy_agent/persistence.py|41|DatasetVersion|dataset_versions|["Base"]|["owner_id", "id", "dataset_id", "version_number", "parent_id", "ingest_mode", "original_name", "source_blob_sha256", "materialized_blob_sha256", "row_count", "created_at"]|
|src/vonavy_agent/persistence.py|56|DatasetMapping|dataset_mappings|["Base"]|["owner_id", "id", "dataset_version_id", "mapping_hash", "canonical_json", "created_at"]|
|src/vonavy_agent/persistence.py|66|DataProfile|data_profiles|["Base"]|["owner_id", "id", "dataset_version_id", "mapping_id", "profile_hash", "canonical_json", "created_at"]|
|src/vonavy_agent/persistence.py|77|ExperimentSpecRow|experiment_specs|["Base"]|["owner_id", "id", "spec_hash", "canonical_json", "dataset_version_id", "mapping_id", "profile_id", "created_at"]|
|src/vonavy_agent/persistence.py|89|GateResultRow|gate_results|["Base"]|["owner_id", "id", "spec_id", "spec_hash", "profile_hash", "status", "canonical_json", "confirmation_token", "created_at"]|
|src/vonavy_agent/persistence.py|102|Job|jobs|["Base"]|["owner_id", "id", "kind", "state", "payload_json", "attempt", "worker_id", "lease_token", "lease_expires_at", "cancel_requested", "error_json", "result_json", "created_at", "updated_at"]|
|src/vonavy_agent/persistence.py|120|JobEvent|job_events|["Base"]|["id", "job_id", "from_state", "to_state", "detail_json", "created_at"]|
|src/vonavy_agent/persistence.py|130|Run|runs|["Base"]|["owner_id", "id", "job_id", "spec_id", "gate_result_id", "artifact_relative_path", "manifest_hash", "summary_json", "created_at"]|
|src/vonavy_agent/persistence.py|143|RunMetric|run_metrics|["Base"]|["id", "run_id", "role", "model", "seed", "origin", "horizon", "metric", "value", "row_count", "coverage", "unsupported_reason"]|
|src/vonavy_agent/persistence.py|159|PlannerProposal|planner_proposals|["Base"]|["owner_id", "id", "input_hash", "canonical_json", "confirmed_spec_id", "created_at"]|
|src/vonavy_agent/persistence.py|171|AdapterSnapshot|adapter_snapshots|["Base"]|["owner_id", "id", "adapter_kind", "manifest_kind", "schema_version", "source_sha256", "canonical_json", "created_at"]|
|src/vonavy_agent/persistence.py|183|Export|exports|["Base"]|["owner_id", "id", "job_id", "run_ids_json", "relative_path", "manifest_hash", "created_at"]|

### Migration tables

|path|line|table|columns|
|---|---|---|---|
|src/vonavy_agent/migrations/versions/0001_initial.py|13|datasets|["id", "name", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|19|blobs|["sha256", "media_type", "byte_size", "relative_path", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|27|dataset_versions|["id", "dataset_id", "version_number", "parent_id", "ingest_mode", "original_name", "source_blob_sha256", "materialized_blob_sha256", "row_count", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|55|dataset_mappings|["id", "dataset_version_id", "mapping_hash", "canonical_json", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|69|data_profiles|["id", "dataset_version_id", "mapping_id", "profile_hash", "canonical_json", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|89|experiment_specs|["id", "spec_hash", "canonical_json", "dataset_version_id", "mapping_id", "profile_id", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|115|gate_results|["id", "spec_id", "spec_hash", "profile_hash", "status", "canonical_json", "confirmation_token", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|131|jobs|["id", "kind", "state", "payload_json", "attempt", "worker_id", "lease_token", "lease_expires_at", "cancel_requested", "error_json", "result_json", "created_at", "updated_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|149|job_events|["id", "job_id", "from_state", "to_state", "detail_json", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|159|runs|["id", "job_id", "spec_id", "gate_result_id", "artifact_relative_path", "manifest_hash", "summary_json", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|186|run_metrics|["id", "run_id", "role", "model", "seed", "origin", "horizon", "metric", "value", "row_count", "coverage", "unsupported_reason"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|202|planner_proposals|["id", "input_hash", "canonical_json", "confirmed_spec_id", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|210|adapter_snapshots|["id", "adapter_kind", "manifest_kind", "schema_version", "source_sha256", "canonical_json", "created_at"]|
|src/vonavy_agent/migrations/versions/0001_initial.py|220|exports|["id", "job_id", "run_ids_json", "relative_path", "manifest_hash", "created_at"]|

### Cloud persistence references

|path|line|context|
|---|---|---|
|infra/vonavy_infra/control_plane_stack.py|225|data_bucket = s3.Bucket(|
|infra/vonavy_infra/control_plane_stack.py|247|metadata_table = dynamodb.Table(|
|infra/vonavy_infra/control_plane_stack.py|253|encryption=dynamodb.TableEncryption.AWS_MANAGED,|
|infra/vonavy_infra/control_plane_stack.py|307|resources=[data_bucket.arn_for_objects("datasets/users/*")],|
|infra/vonavy_infra/control_plane_stack.py|313|resources=[data_bucket.arn_for_objects("validation-results/users/*")],|
|infra/vonavy_infra/control_plane_stack.py|343|"VONAVY_DATA_BUCKET": data_bucket.bucket_name,|
|infra/vonavy_infra/control_plane_stack.py|399|resources=[data_bucket.arn_for_objects("datasets/users/*")],|
|infra/vonavy_infra/control_plane_stack.py|405|resources=[data_bucket.arn_for_objects("forecast-results/users/*")],|
|infra/vonavy_infra/control_plane_stack.py|440|"VONAVY_DATA_BUCKET": data_bucket.bucket_name,|
|infra/vonavy_infra/control_plane_stack.py|683|"UPLOAD_BUCKET": upload_bucket.bucket_name,|
|infra/vonavy_infra/control_plane_stack.py|684|"DATA_BUCKET": data_bucket.bucket_name,|
|infra/vonavy_infra/control_plane_stack.py|685|"METADATA_TABLE": metadata_table.table_name,|
|infra/vonavy_infra/control_plane_stack.py|700|metadata_table.grant_read_write_data(control_plane_function)|
|infra/vonavy_infra/control_plane_stack.py|720|resources=[data_bucket.arn_for_objects("datasets/users/*")],|
|infra/vonavy_infra/control_plane_stack.py|727|resources=[data_bucket.arn_for_objects("validation-results/users/*")],|
|infra/vonavy_infra/control_plane_stack.py|780|"DATA_BUCKET": data_bucket.bucket_name,|
|infra/vonavy_infra/control_plane_stack.py|781|"METADATA_TABLE": metadata_table.table_name,|
|infra/vonavy_infra/control_plane_stack.py|799|metadata_table.grant_read_write_data(forecast_control_plane_function)|
|infra/vonavy_infra/control_plane_stack.py|850|data_bucket.arn_for_objects("forecast-results/users/*"),|
|infra/vonavy_infra/control_plane_stack.py|851|data_bucket.arn_for_objects("validation-results/users/*"),|
|infra/vonavy_infra/control_plane_stack.py|1042|CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name)|
|infra/vonavy_infra/control_plane_stack.py|1043|CfnOutput(self, "MetadataTableName", value=metadata_table.table_name)|
|infra/lambda/control_plane/handler.py|20|UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]|
|infra/lambda/control_plane/handler.py|21|DATA_BUCKET = os.environ["DATA_BUCKET"]|
|infra/lambda/control_plane/handler.py|22|METADATA_TABLE = os.environ["METADATA_TABLE"]|
|infra/lambda/control_plane/handler.py|83|_table = _ddb_resource.Table(METADATA_TABLE)|
|infra/lambda/control_plane/handler.py|401|object_key = f"datasets/users/{owner}/{dataset_id}/{upload_id}/{filename}"|
|infra/lambda/control_plane/handler.py|424|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|444|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|472|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|517|Bucket=UPLOAD_BUCKET,|
|infra/lambda/control_plane/handler.py|559|Bucket=DATA_BUCKET,|
|infra/lambda/control_plane/handler.py|561|CopySource={"Bucket": UPLOAD_BUCKET, "Key": staging_object_key},|
|infra/lambda/control_plane/handler.py|581|Bucket=DATA_BUCKET,|
|infra/lambda/control_plane/handler.py|589|Bucket=DATA_BUCKET,|
|infra/lambda/control_plane/handler.py|612|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|632|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|657|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|687|s3.delete_object(Bucket=UPLOAD_BUCKET, Key=staging_object_key)|
|infra/lambda/control_plane/handler.py|894|"bucket": DATA_BUCKET,|
|infra/lambda/control_plane/handler.py|902|"bucket": DATA_BUCKET,|
|infra/lambda/control_plane/handler.py|926|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|950|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|1004|result_key = f"validation-results/users/{owner}/datasets/{dataset_id}/jobs/{job_id}/result.json"|
|infra/lambda/control_plane/handler.py|1018|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|1048|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|1180|request: dict[str, Any] = {"Bucket": DATA_BUCKET, "Key": item["result_key"]}|
|infra/lambda/control_plane/handler.py|1286|"TableName": METADATA_TABLE,|
|infra/lambda/control_plane/handler.py|1296|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|38|DATA_BUCKET = os.environ["DATA_BUCKET"]|
|infra/lambda/forecast_control_plane/handler.py|39|METADATA_TABLE = os.environ["METADATA_TABLE"]|
|infra/lambda/forecast_control_plane/handler.py|95|_TABLE = dynamodb.Table(METADATA_TABLE)|
|infra/lambda/forecast_control_plane/handler.py|498|prefix = f"forecast-results/users/{owner}/datasets/{dataset_id}/runs/{run_id}/"|
|infra/lambda/forecast_control_plane/handler.py|507|"bucket": DATA_BUCKET,|
|infra/lambda/forecast_control_plane/handler.py|514|"output": {"bucket": DATA_BUCKET, "prefix": prefix},|
|infra/lambda/forecast_control_plane/handler.py|547|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|570|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|683|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|715|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|1021|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|1036|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|1110|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|1124|"TableName": METADATA_TABLE,|
|infra/lambda/forecast_control_plane/handler.py|1142|response = s3.get_object(Bucket=DATA_BUCKET, Key=item["result_key"])|
|infra/lambda/forecast_control_plane/handler.py|1668|Params={"Bucket": DATA_BUCKET, "Key": key, "VersionId": artifact_version},|
|infra/lambda/forecast_control_plane/handler.py|1800|Bucket=DATA_BUCKET,|
|infra/lambda/forecast_control_plane/handler.py|1813|parent_prefix = f"forecast-results/users/{owner}/datasets/{item['dataset_id']}/runs/{run_id}/"|
|infra/lambda/forecast_control_plane/handler.py|2059|Bucket=DATA_BUCKET,|

## Old tests likely tied to forecast/dataset/Chronos

|path|matched_terms|matching_line_count|
|---|---|---|
|infra/tests/auth_token_refresh_smoke.mjs|["forecast"]|2|
|infra/tests/phase6_agent_experience_smoke.mjs|["dataset"]|1|
|infra/tests/test_agent_result_conversation.py|["Forecast", "dataset", "forecast", "xgboost"]|11|
|infra/tests/test_forecast_agent.py|["chronos", "dataset", "forecast", "neuralnet", "validation", "xgboost"]|49|
|infra/tests/test_forecast_handler.py|["Validation", "XGBoost", "chronos", "dataset", "forecast", "neuralnet", "validation", "xgboost"]|71|
|infra/tests/test_forecast_orchestrator.py|["NeuralNet", "chronos", "dataset", "forecast", "neuralnet", "validation", "xgboost"]|28|
|infra/tests/test_forecast_result_persistence.py|["Dataset", "Forecast", "dataset", "forecast", "validation"]|10|
|infra/tests/test_handler.py|["Validation", "dataset", "validation"]|133|
|infra/tests/test_multi_model_comparison_contract.py|["chronos", "dataset", "forecast", "neuralnet", "xgboost"]|12|
|infra/tests/test_phase4_agentic_sources.py|["Dataset", "Forecast", "dataset", "forecast"]|9|
|infra/tests/test_phase4b_model_selection.py|["chronos", "dataset", "forecast", "neuralnet", "xgboost"]|20|
|infra/tests/test_phase4c_preprocessing_plan.py|["chronos", "dataset", "forecast", "neuralnet", "xgboost"]|28|
|infra/tests/test_phase4c_sources.py|["forecast"]|4|
|infra/tests/test_phase4d_result_review.py|["forecast"]|13|
|infra/tests/test_phase4d_sources.py|["Forecast", "chronos", "forecast"]|16|
|infra/tests/test_phase5_release_hardening.py|["forecast"]|3|
|infra/tests/test_phase6_agent_experience.py|["dataset", "forecast"]|7|
|infra/tests/test_phase6_custom_domain.py|["dataset"]|1|
|infra/tests/test_stack.py|["Forecast", "Validation", "dataset", "forecast", "validation"]|71|
|tests/conftest.py|["Dataset", "dataset"]|13|
|tests/test_api.py|["dataset"]|4|
|tests/test_blocker_regressions.py|["Dataset", "Validation", "dataset"]|14|
|tests/test_chronos2_forecasting.py|["Chronos", "Forecast", "chronos", "dataset", "forecast"]|45|
|tests/test_comparison_request_json_validation.py|["chronos", "dataset", "forecast", "neuralnet", "xgboost"]|13|
|tests/test_dataset_and_gate.py|["Dataset", "dataset"]|4|
|tests/test_final_review_regressions.py|["Dataset", "Validation", "chronos", "dataset"]|12|
|tests/test_forecast_container_build.py|["Chronos", "chronos", "forecast"]|14|
|tests/test_forecasting.py|["Forecast", "dataset", "forecast", "xgboost"]|36|
|tests/test_last_delta_regressions.py|["Dataset", "dataset", "forecast"]|7|
|tests/test_multi_model_comparison.py|["Forecast", "forecast"]|5|
|tests/test_neuralnet_forecasting.py|["Forecast", "Validation", "dataset", "forecast", "neuralnet"]|32|
|tests/test_phase0_cloud_boundaries.py|["Forecast", "dataset", "forecast"]|22|
|tests/test_phase4d_evaluation.py|["Forecast", "Validation", "forecast"]|11|
|tests/test_runner_planner_export.py|["Dataset", "chronos"]|3|
|tests/test_validation_aws.py|["dataset", "validation"]|21|
|tests/test_validation_cli.py|["Validation", "dataset", "validation"]|19|
|tests/test_validation_container.py|["dataset", "validation"]|3|
|tests/test_validation_contracts.py|["Validation", "dataset", "validation"]|26|
|tests/test_validation_worker.py|["Validation", "dataset", "validation"]|60|

## Old dependencies

|source|group|name|spec|
|---|---|---|---|
|pyproject.toml|project.dependencies|alembic|alembic>=1.14,<2|
|pyproject.toml|project.dependencies|fastapi|fastapi>=0.115,<1|
|pyproject.toml|project.dependencies|numpy|numpy>=2.1,<3|
|pyproject.toml|project.dependencies|pandas|pandas>=2.2,<3|
|pyproject.toml|project.dependencies|pyarrow|pyarrow>=18,<24|
|pyproject.toml|project.dependencies|pydantic|pydantic>=2.10,<3|
|pyproject.toml|project.dependencies|pydantic-settings|pydantic-settings>=2.7,<3|
|pyproject.toml|project.dependencies|scikit-learn|scikit-learn>=1.6,<2|
|pyproject.toml|project.dependencies|sqlalchemy|sqlalchemy>=2.0,<3|
|pyproject.toml|project.dependencies|uvicorn|uvicorn>=0.34,<1|
|pyproject.toml|project.optional-dependencies.aws|boto3|boto3>=1.40,<2|
|pyproject.toml|project.optional-dependencies.modeling|torch|torch>=2.10,<3|
|pyproject.toml|project.optional-dependencies.modeling|xgboost|xgboost>=3.1,<4|
|pyproject.toml|project.optional-dependencies.chronos|chronos-forecasting|chronos-forecasting==2.3.1|
|pyproject.toml|project.optional-dependencies.dev|boto3|boto3>=1.40,<2|
|pyproject.toml|project.optional-dependencies.dev|pandas-stubs|pandas-stubs>=2.2,<3|
|pyproject.toml|project.optional-dependencies.dev|torch|torch>=2.10,<3|
|pyproject.toml|project.optional-dependencies.dev|xgboost|xgboost>=3.1,<4|
|infra/pyproject.toml|project.dependencies|aws-cdk-lib|aws-cdk-lib==2.261.0|
|infra/pyproject.toml|project.optional-dependencies.dev|boto3|boto3>=1.40,<2|
|infra/package.json|devDependencies|aws-cdk|2.1131.0|

## User-visible old terms in web

- src/skincare_advisor_agent/web: exists=False (requested path is absent on current main)
- infra/web: exists=True
- src/vonavy_agent/web: exists=True (actual tracked legacy web path on current main; scanned because requested skincare path is absent)

|path|line|terms|context|
|---|---|---|---|
|infra/web/app.js|5|["forecast", "vonavy"]|const FORECAST_RUN_STORAGE_PREFIX = "vonavy_forecast_runs";|
|infra/web/app.js|13|["validation"]|validationResults: new Map(),|
|infra/web/app.js|57|["vonavy"]|sessionStorage.setItem("vonavy_tokens", JSON.stringify(next));|
|infra/web/app.js|60|["vonavy"]|sessionStorage.removeItem("vonavy_tokens");|
|infra/web/app.js|73|["vonavy"]|const raw = sessionStorage.getItem("vonavy_tokens");|
|infra/web/app.js|82|["vonavy"]|sessionStorage.removeItem("vonavy_tokens");|
|infra/web/app.js|87|["vonavy"]|sessionStorage.removeItem("vonavy_tokens");|
|infra/web/app.js|92|["forecast"]|function forecastRunStorageKey() {|
|infra/web/app.js|96|["Forecast"]|function loadRememberedForecastRuns() {|
|infra/web/app.js|97|["forecast"]|const key = forecastRunStorageKey();|
|infra/web/app.js|113|["Forecast"]|function saveRememberedForecastRuns(runs) {|
|infra/web/app.js|114|["forecast"]|const key = forecastRunStorageKey();|
|infra/web/app.js|120|["Forecast"]|// Forecast execution remains usable even when durable browser storage is unavailable.|
|infra/web/app.js|124|["Forecast"]|function rememberForecastRun(run) {|
|infra/web/app.js|125|["dataset"]|const datasetId = run?.datasetId;|
|infra/web/app.js|126|["forecast"]|const forecastRunId = run?.forecastRunId;|
|infra/web/app.js|128|["dataset", "forecast"]|if (!datasetId \|\| !forecastRunId \|\| typeof status !== "string") return;|
|infra/web/app.js|129|["Forecast"]|const runs = loadRememberedForecastRuns();|
|infra/web/app.js|130|["dataset"]|runs[datasetId] = {|
|infra/web/app.js|131|["dataset"]|datasetId,|
|infra/web/app.js|132|["forecast"]|forecastRunId,|
|infra/web/app.js|146|["Forecast"]|saveRememberedForecastRuns(runs);|
|infra/web/app.js|148|["Forecast", "dataset"]|function rememberedForecastRun(datasetId) {|
|infra/web/app.js|149|["Forecast", "dataset"]|return loadRememberedForecastRuns()[datasetId] \|\| null;|
|infra/web/app.js|152|["Forecast", "dataset"]|function forgetForecastRun(datasetId) {|
|infra/web/app.js|153|["Forecast"]|const runs = loadRememberedForecastRuns();|
|infra/web/app.js|154|["dataset"]|if (!(datasetId in runs)) return;|
|infra/web/app.js|155|["dataset"]|delete runs[datasetId];|
|infra/web/app.js|156|["Forecast"]|saveRememberedForecastRuns(runs);|
|infra/web/app.js|272|["vonavy"]|const requestId = response.headers.get("x-vonavy-request-id");|
|infra/web/app.js|273|["vonavy"]|const sourceRevision = response.headers.get("x-vonavy-source-revision");|
|infra/web/app.js|324|["vonavy"]|sessionStorage.setItem("vonavy_pkce_verifier", verifier);|
|infra/web/app.js|325|["vonavy"]|sessionStorage.setItem("vonavy_oauth_state", oauthState);|
|infra/web/app.js|339|["vonavy"]|const verifier = sessionStorage.getItem("vonavy_pkce_verifier");|
|infra/web/app.js|340|["vonavy"]|const expectedState = sessionStorage.getItem("vonavy_oauth_state");|
|infra/web/app.js|361|["vonavy"]|sessionStorage.removeItem("vonavy_pkce_verifier");|
|infra/web/app.js|362|["vonavy"]|sessionStorage.removeItem("vonavy_oauth_state");|
|infra/web/app.js|377|["dataset"]|const file = $("dataset-file").files[0];|
|infra/web/app.js|378|["dataset"]|const name = $("dataset-name").value.trim();|
|infra/web/app.js|394|["dataset"]|datasetName: name,|
|infra/web/app.js|411|["dataset", "validation"]|$("status").textContent = "Upload complete. The dataset is ready for validation.";|
|infra/web/app.js|413|["Datasets"]|await listDatasets();|
|infra/web/app.js|421|["validation"]|function validationMessage(job, result = null) {|
|infra/web/app.js|426|["validation"]|const codes = result.validation_errors.map((issue) => issue.code).join(", ");|
|infra/web/app.js|427|["Dataset", "validation"]|return `Dataset is invalid: ${codes \|\| "validation rules failed"}.`;|
|infra/web/app.js|430|["Validation"]|return job.failure?.message \|\| "Validation worker failed.";|
|infra/web/app.js|432|["Validation"]|return `Validation status: ${job.status}.`;|
|infra/web/app.js|435|["Validation"]|async function waitForValidation(job, output, button) {|
|infra/web/app.js|438|["validation"]|const maxAttempts = Math.ceil((state.config.validationJobTimeoutSeconds + 600) / 3);|
|infra/web/app.js|440|["validation"]|output.textContent = validationMessage(current);|
|infra/web/app.js|447|["dataset", "validation"]|state.validationResults.set(current.datasetId, {|
|infra/web/app.js|448|["validation"]|jobId: current.validationJobId,|
|infra/web/app.js|452|["validation"]|output.textContent = validationMessage(current, result);|
|infra/web/app.js|459|["Validation"]|output.textContent = "Validation is still running. Refresh to check it again.";|
|infra/web/app.js|463|["Dataset", "dataset"]|async function validateDataset(dataset, output, button) {|
|infra/web/app.js|465|["validation"]|output.textContent = "Submitting an ephemeral CPU validation job…";|
|infra/web/app.js|467|["dataset", "datasets", "validation"]|const job = await api(`/api/datasets/${dataset.datasetId}/validations`, {|
|infra/web/app.js|471|["Validation"]|await waitForValidation(job, output, button);|
|infra/web/app.js|478|["forecast"]|function forecastMessage(run, result = null) {|
|infra/web/app.js|479|["forecast"]|if (result?.schema_version === "forecast-comparison-result/v1") {|
|infra/web/app.js|497|["Forecast"]|return `Forecast complete: ${result.profile.entities * 7} rows.${quality}`;|
|infra/web/app.js|500|["forecast"]|return result.failure?.message \|\| "The forecast mapping or data is invalid.";|
|infra/web/app.js|503|["Forecast"]|if (run.status === "failed") return run.failure?.message \|\| "Forecast worker failed.";|
|infra/web/app.js|508|["Forecast"]|return `Forecast status: ${run.status}.`;|
|infra/web/app.js|512|["Forecast"]|if (value === null) throw new Error("Forecast setup cancelled.");|
|infra/web/app.js|522|["Forecast"]|if (value === null) throw new Error("Forecast setup cancelled.");|
|infra/web/app.js|534|["forecast"]|details.className = "forecast-review";|
|infra/web/app.js|572|["experiment"]|title.textContent = "Measured next experiments";|
|infra/web/app.js|616|["forecast"]|Number(entry.forecast_rows \|\| 0).toLocaleString(),|
|infra/web/app.js|663|["Forecast"]|function showForecastResult(output, run, result) {|
|infra/web/app.js|664|["forecast"]|output.replaceChildren(document.createTextNode(forecastMessage(run, result)));|
|infra/web/app.js|666|["forecast"]|if (result.schema_version === "forecast-comparison-result/v1") {|
|infra/web/app.js|678|["Forecast"]|reviewForecastResult(run, output, discuss);|
|infra/web/app.js|683|["Forecast"]|async function waitForForecast(run, output, button) {|
|infra/web/app.js|685|["Forecast"]|rememberForecastRun(current);|
|infra/web/app.js|690|["forecast"]|: state.config.forecastJobTimeoutSeconds * requestedModels;|
|infra/web/app.js|693|["forecast"]|output.textContent = forecastMessage(current);|
|infra/web/app.js|697|["Forecast"]|showForecastResult(output, current, result);|
|infra/web/app.js|704|["Forecast"]|rememberForecastRun(current);|
|infra/web/app.js|706|["Forecast"]|output.textContent = "Forecast is still running. Refresh to restore its current state.";|
|infra/web/app.js|709|["Forecast", "dataset"]|async function restoreForecast(dataset, output, button) {|
|infra/web/app.js|710|["Forecast", "dataset"]|const remembered = rememberedForecastRun(dataset.datasetId);|
|infra/web/app.js|714|["forecast"]|output.textContent = "Restoring the latest forecast…";|
|infra/web/app.js|717|["Forecast"]|rememberForecastRun(run);|
|infra/web/app.js|721|["Forecast"]|showForecastResult(output, run, result);|
|infra/web/app.js|723|["forecast"]|output.textContent = forecastMessage(run);|
|infra/web/app.js|728|["Forecast"]|await waitForForecast(run, output, button);|
|infra/web/app.js|731|["Forecast", "dataset"]|forgetForecastRun(dataset.datasetId);|
|infra/web/app.js|734|["forecast"]|output.textContent = `Latest forecast could not be restored: ${error.message}`;|
|infra/web/app.js|817|["dataset"]|if (language) code.dataset.language = language.slice(0, 32);|
|infra/web/app.js|923|["forecast"]|session?.maximumTurns ?? state.config?.forecastAgentMaximumTurns ?? 20,|
|infra/web/app.js|1012|["Forecast", "forecast"]|`Forecast: ${plan.forecastStart} through ${plan.forecastEnd}`,|
|infra/web/app.js|1036|["Dataset", "Forecast", "dataset"]|async function agenticForecastDataset(dataset, output, button) {|
|infra/web/app.js|1037|["dataset", "validation"]|const validation = state.validationResults.get(dataset.datasetId);|
|infra/web/app.js|1038|["validation"]|if (!validation?.jobId) {|
|infra/web/app.js|1039|["dataset"]|output.textContent = "Validate this dataset first so the agent can inspect its safe profile.";|
|infra/web/app.js|1045|["dataset"]|dataset,|
|infra/web/app.js|1048|["validation"]|validation,|
|infra/web/app.js|1064|["forecasting"]|`Tell me the forecasting objective. You have up to ${agentMaximumTurns()} turns. If you ask to run multiple models, the confirmable plan will list every adapter and execute a real all-terminal comparison.`,|
|infra/web/app.js|1069|["Forecast"]|function reviewForecastResult(run, output, button) {|
|infra/web/app.js|1091|["forecast"]|`Ask me about this completed ${run.runMode === "comparison" ? "model comparison" : "forecast"} for up to ${agentMaximumTurns()} turns. I can explain measured evidence and practical implications, but I cannot rerun or modify it.`,|
|infra/web/app.js|1131|["forecast", "forecasts"]|queued = await api(`/api/forecasts/${agentContext.run.forecastRunId}/agent/sessions`, {|
|infra/web/app.js|1137|["dataset", "datasets", "forecast"]|`/api/datasets/${agentContext.dataset.datasetId}/forecast-agent/sessions`,|
|infra/web/app.js|1141|["validation"]|validationJobId: agentContext.validation.jobId,|
|infra/web/app.js|1230|["Forecast", "forecast"]|`Forecast: ${plan.forecastStart} through ${plan.forecastEnd}\n` +|
|infra/web/app.js|1256|["dataset", "datasets", "forecasts"]|const run = await api(`/api/datasets/${agentContext.dataset.datasetId}/forecasts`, {|
|infra/web/app.js|1265|["Forecast"]|await waitForForecast(run, output, button);|
|infra/web/app.js|1282|["Dataset", "dataset", "forecast"]|async function forecastDataset(dataset, output, button) {|
|infra/web/app.js|1285|["dataset", "validation"]|const validation = state.validationResults.get(dataset.datasetId);|
|infra/web/app.js|1286|["validation"]|if (!validation?.jobId) {|
|infra/web/app.js|1287|["dataset"]|throw new Error("Validate this dataset first so the AI can inspect its safe profile.");|
|infra/web/app.js|1290|["forecast"]|"What should the forecast prioritize? (optional)",|
|infra/web/app.js|1291|["Forecast"]|"Forecast the next seven days of demand using known future context.",|
|infra/web/app.js|1293|["Forecast"]|if (objective === null) throw new Error("Forecast setup cancelled.");|
|infra/web/app.js|1294|["forecast"]|output.textContent = "Asking the AI to prepare a leakage-safe forecast plan…";|
|infra/web/app.js|1295|["dataset", "datasets", "forecast"]|const plan = await api(`/api/datasets/${dataset.datasetId}/forecast-agent`, {|
|infra/web/app.js|1298|["validation"]|validationJobId: validation.jobId,|
|infra/web/app.js|1304|["chronos", "neuralnet", "xgboost"]|"Choose model: xgboost, neuralnet, or chronos",|
|infra/web/app.js|1305|["chronos"]|"chronos",|
|infra/web/app.js|1307|["Forecast"]|if (modelChoice === null) throw new Error("Forecast setup cancelled.");|
|infra/web/app.js|1309|["neuralnet"]|const adapterId = normalisedModel === "neuralnet"|
|infra/web/app.js|1310|["neuralnet"]|? "neuralnet-direct-v1"|
|infra/web/app.js|1311|["xgboost"]|: normalisedModel === "xgboost"|
|infra/web/app.js|1312|["xgboost"]|? "xgboost-direct-v1"|
|infra/web/app.js|1313|["chronos"]|: normalisedModel === "chronos"|
|infra/web/app.js|1314|["chronos2"]|? "chronos2-zero-shot-v1"|
|infra/web/app.js|1316|["chronos", "neuralnet", "xgboost"]|if (!adapterId) throw new Error("Model must be xgboost, neuralnet, or chronos.");|
|infra/web/app.js|1317|["neuralnet"]|const modelLabel = adapterId === "neuralnet-direct-v1"|
|infra/web/app.js|1318|["NeuralNet"]|? "Best NeuralNet"|
|infra/web/app.js|1319|["chronos2"]|: adapterId === "chronos2-zero-shot-v1"|
|infra/web/app.js|1320|["Chronos-2"]|? "Chronos-2 Zero-shot"|
|infra/web/app.js|1321|["XGBoost"]|: "Quick XGBoost";|
|infra/web/app.js|1364|["Forecast", "forecast"]|`Forecast: ${plan.forecastStart} through ${plan.forecastEnd}\n` +|
|infra/web/app.js|1368|["Forecast"]|if (!approved) throw new Error("Forecast plan was not confirmed.");|
|infra/web/app.js|1369|["forecast"]|output.textContent = `Submitting the confirmed ${modelLabel} forecast plan…`;|
|infra/web/app.js|1370|["dataset", "datasets", "forecasts"]|const run = await api(`/api/datasets/${dataset.datasetId}/forecasts`, {|
|infra/web/app.js|1379|["Forecast"]|await waitForForecast(run, output, button);|
|infra/web/app.js|1385|["Datasets"]|async function listDatasets() {|
|infra/web/app.js|1386|["datasets"]|const payload = await api("/api/datasets");|
|infra/web/app.js|1387|["datasets"]|const root = $("datasets");|
|infra/web/app.js|1389|["datasets"]|if (!payload.datasets.length) {|
|infra/web/app.js|1390|["datasets"]|root.textContent = "No datasets uploaded yet.";|
|infra/web/app.js|1393|["dataset", "datasets"]|for (const dataset of payload.datasets) {|
|infra/web/app.js|1395|["dataset"]|item.className = "dataset";|
|infra/web/app.js|1397|["dataset"]|title.textContent = dataset.name;|
|infra/web/app.js|1399|["dataset"]|meta.textContent = `${dataset.filename} · ${dataset.status} · ${dataset.sizeBytes.toLocaleString()} bytes`;|
|infra/web/app.js|1401|["dataset"]|actions.className = "dataset-actions";|
|infra/web/app.js|1402|["validation"]|const validationStatus = document.createElement("span");|
|infra/web/app.js|1403|["validation"]|validationStatus.className = "validation-status";|
|infra/web/app.js|1404|["dataset"]|if (dataset.status === "uploaded") {|
|infra/web/app.js|1408|["dataset"]|validateButton.textContent = "Validate dataset";|
|infra/web/app.js|1410|["Dataset", "dataset", "validation"]|validateDataset(dataset, validationStatus, validateButton);|
|infra/web/app.js|1413|["forecast"]|const forecastButton = document.createElement("button");|
|infra/web/app.js|1414|["forecast"]|forecastButton.type = "button";|
|infra/web/app.js|1415|["forecast"]|forecastButton.className = "secondary";|
|infra/web/app.js|1416|["Forecast", "forecast"]|forecastButton.textContent = "AI → Forecast";|
|infra/web/app.js|1417|["forecast"]|forecastButton.addEventListener("click", () => {|
|infra/web/app.js|1418|["Dataset", "Forecast", "dataset", "forecast", "validation"]|agenticForecastDataset(dataset, validationStatus, forecastButton);|
|infra/web/app.js|1420|["forecast", "validation"]|actions.append(forecastButton, validationStatus);|
|infra/web/app.js|1421|["Forecast", "dataset", "forecast", "validation"]|restoreForecast(dataset, validationStatus, forecastButton);|
|infra/web/app.js|1433|["Datasets", "dataset"]|$("upload-policy").textContent = `Server policy: up to ${state.config.maximumUploadBytes.toLocaleString()} bytes per file and ${state.config.maximumDatasetsPerOwner} retained dataset slots per account.`;|
|infra/web/app.js|1450|["Datasets"]|await listDatasets();|
|infra/web/app.js|1466|["Datasets"]|listDatasets().catch((error) => {|
|infra/web/index.html|16|["dataset", "experiment", "forecasting", "validation"]|<p class="lead">Invite-only dataset intake and ephemeral validation for leakage-safe forecasting experiments.</p>|
|infra/web/index.html|35|["dataset"]|<h2>Upload dataset</h2>|
|infra/web/index.html|40|["Dataset"]|Dataset name|
|infra/web/index.html|41|["dataset"]|<input id="dataset-name" maxlength="200" required placeholder="Interview demand panel">|
|infra/web/index.html|45|["dataset"]|<input id="dataset-file" type="file" accept=".csv,.parquet" required>|
|infra/web/index.html|55|["datasets"]|<h2>Your datasets</h2>|
|infra/web/index.html|56|["Validation", "datasets", "validation"]|<p>Only owner-scoped datasets and validation jobs are returned. Validation runs on ephemeral CPU capacity.</p>|
|infra/web/index.html|60|["datasets"]|<div id="datasets"></div>|
|infra/web/index.html|68|["forecast"]|<section id="agent-plan" class="agent-plan hidden" aria-label="Confirmable forecast plan">|
|infra/web/index.html|77|["forecast"]|placeholder="Ask about the data, compare models, or request a forecast plan."></textarea>|
|infra/web/index.html|82|["validation"]|<span id="agent-status" class="validation-status"></span>|
|infra/web/styles.css|30|["dataset"]|.dataset { display: grid; gap: 6px; padding: 14px 0; border-top: 1px solid #3f3f46; }|
|infra/web/styles.css|31|["dataset"]|.dataset span { color: #a1a1aa; font-size: 14px; }|
|infra/web/styles.css|36|["dataset"]|.dataset-actions { display: flex; align-items: center; gap: 12px; margin-top: 6px; }|
|infra/web/styles.css|37|["validation"]|.validation-status { color: #bfdbfe; font-size: 14px; }|
|infra/web/styles.css|39|["dataset"]|.dataset-actions { align-items: flex-start; flex-direction: column; }|
|infra/web/styles.css|109|["validation"]|.agent-footer .validation-status { max-width: min(42vw, 520px); }|
|infra/web/styles.css|134|["validation"]|.agent-footer .validation-status { max-width: none; }|
|src/vonavy_agent/web/app.js|31|["dataset"]|event.preventDefault();notice("Copying and hashing dataset…");|
|src/vonavy_agent/web/app.js|33|["dataset"]|const data=new FormData();data.append("dataset_name",$("dataset-name").value);data.append("file",$("dataset-file").files[0]);|
|src/vonavy_agent/web/app.js|34|["datasets"]|state.version=await api("/api/datasets/upload",{method:"POST",body:data});|
|src/vonavy_agent/web/app.js|35|["dataset"]|show("dataset-result",`Version ${state.version.version_number} / ${state.version.row_count} rows / SHA-256 ${state.version.materialized_sha256}`);|
|src/vonavy_agent/web/app.js|36|["dataset"]|notice("Immutable dataset version created. Define the mapping.");|
|src/vonavy_agent/web/app.js|41|["dataset"]|event.preventDefault();if(!state.version){notice("Ingest a dataset first.",true);return}|
|src/vonavy_agent/web/app.js|56|["dataset"]|state.mapping=await api(`/api/dataset-versions/${state.version.id}/mappings`,jsonOptions(mapping));|
|src/vonavy_agent/web/app.js|57|["dataset"]|const queued=await api("/api/profiles",jsonOptions({dataset_version_id:state.version.id,mapping_id:state.mapping.id}));|
|src/vonavy_agent/web/app.js|72|["dataset"]|event.preventDefault();if(!state.profile){notice("Profile the mapped dataset first.",true);return}|
|src/vonavy_agent/web/app.js|80|["dataset"]|schema_version:"1.0",dataset_version_id:state.version.id,mapping_id:state.mapping.id,profile_id:state.profile.id,frequency:"D",|
|src/vonavy_agent/web/app.js|109|["Experiment"]|if(!$("run-confirm").checked\|\|!state.gate)return;notice("Experiment queued; monitoring the separate worker…");|
|src/vonavy_agent/web/app.js|117|["Experiment"]|notice("Experiment succeeded. Compare common-row evidence or export it.");await refreshRuns();|
|src/vonavy_agent/web/app.js|126|["experiment"]|if(!items.length){node.textContent="No bounded next experiment is currently justified.";return}|
|src/vonavy_agent/web/app.js|156|["experiment"]|const node=resultNode("export-result"),link=document.createElement("a");link.href=`/api/exports/${queued.export_id}/download`;link.textContent=`Download experiment-agent-report-${queued.export_id}.zip`;node.append(link);|
|src/vonavy_agent/web/index.html|6|["Experiment"]|<title>Experiment Agent / Interview Assignment</title>|
|src/vonavy_agent/web/index.html|13|["Experiment"]|<h1>Experiment Agent</h1>|
|src/vonavy_agent/web/index.html|14|["experiment", "forecasting"]|<p class="lede">Leakage-safe local forecasting experiments with evidence you can export.</p>|
|src/vonavy_agent/web/index.html|19|["Chronos", "chronos", "vonavy"]|<a href="https://romanlysonek.github.io/vonavy_chronos/">Chronos</a>|
|src/vonavy_agent/web/index.html|34|["Dataset", "dataset"]|<label>Dataset name<input id="dataset-name" name="dataset_name" value="Interview demand demo" required maxlength="200"></label>|
|src/vonavy_agent/web/index.html|35|["dataset"]|<label>File<input id="dataset-file" name="file" type="file" accept=".csv,.parquet" required></label>|
|src/vonavy_agent/web/index.html|38|["dataset"]|<div id="dataset-result" class="result muted">No dataset version selected.</div>|
|src/vonavy_agent/web/index.html|60|["dataset"]|<div id="profile-result" class="result muted">Ingest a dataset first.</div>|
|src/vonavy_agent/web/index.html|88|["experiment"]|<div id="gate-result" class="result muted">Profile data before configuring an experiment.</div>|
|src/vonavy_agent/web/index.html|94|["experiment"]|<div class="actions"><button id="run-button" disabled>Enqueue experiment</button><button id="planner-button" class="secondary" disabled>Propose next experiments</button></div>|

## Legacy source files by term

|path|matched_terms|occurrence_count|
|---|---|---|
|.github/workflows/ci.yml|["validation", "vonavy", "vonavy-agent", "vonavy_agent"]|14|
|README.md|["Chronos", "Dataset", "Experiment", "Forecast", "dataset", "experiment", "forecast", "forecasting", "validation", "vonavy-agent"]|39|
|alembic.ini|["vonavy_agent"]|1|
|docs/phase-0-cloud-boundaries.md|["Experiment", "Forecast", "backtest", "dataset", "forecast", "forecasting", "validation"]|10|
|docs/phase-1-serverless-control-plane.md|["Dataset", "dataset", "datasets", "validation", "vonavy-agent"]|11|
|docs/phase-2a-validation-worker.md|["Dataset", "Validation", "dataset", "validation", "vonavy-agent", "vonavy_agent"]|31|
|docs/phase-2b-aws-validation.md|["Chronos", "chronos", "dataset", "datasets", "forecasting", "validation", "vonavy", "vonavy-agent"]|54|
|docs/phase-6-agent-experience.md|["forecast"]|3|
|docs/phase-6-domain-cutover.md|["vonavy-agent"]|1|
|docs/release-hardening.md|["dataset", "forecast", "validation", "vonavy"]|10|
|infra/README.md|["dataset", "validation", "vonavy", "vonavy-agent"]|8|
|infra/app.py|["Vonavy", "datasets", "forecast", "validation", "vonavy", "vonavy-agent"]|7|
|infra/lambda/control_plane/handler.py|["Dataset", "Datasets", "Validation", "dataset", "datasets", "validation", "vonavy", "vonavy-agent"]|253|
|infra/lambda/forecast_control_plane/agent.py|["Dataset", "chronos2", "dataset", "forecast", "forecasting", "neuralnet", "validation", "vonavy-agent", "xgboost"]|62|
|infra/lambda/forecast_control_plane/agent_async.py|["dataset", "forecast", "vonavy"]|4|
|infra/lambda/forecast_control_plane/handler.py|["Dataset", "Forecast", "Validation", "backtest", "chronos2", "dataset", "datasets", "experiment", "forecast", "forecasts", "neuralnet", "validation", "vonavy", "xgboost"]|419|
|infra/lambda/forecast_control_plane/orchestrator.py|["Chronos-2", "Dataset", "Forecast", "NeuralNet", "XGBoost", "chronos2", "dataset", "experiment", "forecast", "forecasting", "neuralnet", "validation", "vonavy-agent", "xgboost"]|120|
|infra/package-lock.json|["vonavy-agent"]|2|
|infra/package.json|["vonavy-agent"]|1|
|infra/pyproject.toml|["vonavy", "vonavy-agent"]|4|
|infra/tests/auth_token_refresh_smoke.mjs|["forecasts", "vonavy"]|3|
|infra/tests/phase5_api_client_smoke.mjs|["vonavy"]|6|
|infra/tests/phase6_agent_experience_smoke.mjs|["dataset"]|1|
|infra/tests/test_agent_result_conversation.py|["Forecast", "dataset", "forecast", "forecasts", "vonavy", "xgboost"]|12|
|infra/tests/test_auth_token_refresh.py|["vonavy"]|2|
|infra/tests/test_forecast_agent.py|["chronos2", "dataset", "forecast", "neuralnet", "validation", "vonavy-agent", "xgboost"]|57|
|infra/tests/test_forecast_handler.py|["Validation", "XGBoost", "chronos", "chronos2", "dataset", "datasets", "forecast", "neuralnet", "validation", "xgboost"]|94|
|infra/tests/test_forecast_orchestrator.py|["NeuralNet", "chronos", "chronos2", "dataset", "forecast", "neuralnet", "validation", "xgboost"]|34|
|infra/tests/test_forecast_result_persistence.py|["Datasets", "Forecast", "dataset", "forecast", "forecasts", "validation", "vonavy"]|18|
|infra/tests/test_handler.py|["Validation", "dataset", "datasets", "validation"]|182|
|infra/tests/test_multi_model_comparison_contract.py|["chronos2", "dataset", "forecast", "forecasting", "neuralnet", "vonavy_agent", "xgboost"]|14|
|infra/tests/test_phase4_agentic_sources.py|["Dataset", "Forecast", "dataset", "datasets", "forecast", "vonavy"]|15|
|infra/tests/test_phase4b_model_selection.py|["chronos", "chronos2", "dataset", "forecast", "neuralnet", "xgboost"]|22|
|infra/tests/test_phase4c_preprocessing_plan.py|["chronos2", "dataset", "forecast", "neuralnet", "xgboost"]|30|
|infra/tests/test_phase4c_sources.py|["forecast", "vonavy"]|5|
|infra/tests/test_phase4d_result_review.py|["backtest", "experiment", "forecast"]|19|
|infra/tests/test_phase4d_sources.py|["Forecast", "chronos", "chronos2", "experiment", "forecast", "forecasting", "vonavy", "vonavy_agent"]|26|
|infra/tests/test_phase5_release_hardening.py|["forecast", "vonavy"]|13|
|infra/tests/test_phase6_agent_experience.py|["dataset", "forecast", "vonavy", "vonavy-agent"]|11|
|infra/tests/test_phase6_custom_domain.py|["datasets", "vonavy", "vonavy-agent"]|3|
|infra/tests/test_stack.py|["Forecast", "Validation", "dataset", "datasets", "forecast", "forecasts", "validation", "vonavy", "vonavy-agent"]|81|
|infra/uv.lock|["vonavy-agent"]|1|
|infra/vonavy_infra/__init__.py|["vonavy-agent"]|1|
|infra/vonavy_infra/control_plane_stack.py|["Datasets", "Forecast", "Validation", "dataset", "datasets", "forecast", "forecasts", "validation", "vonavy", "vonavy-agent", "xgboost"]|167|
|infra/web/app.js|["Chronos-2", "Dataset", "Datasets", "Forecast", "NeuralNet", "Validation", "XGBoost", "chronos", "chronos2", "dataset", "datasets", "experiment", "forecast", "forecasting", "forecasts", "neuralnet", "validation", "vonavy", "xgboost"]|233|
|infra/web/index.html|["Dataset", "Validation", "dataset", "datasets", "experiment", "forecast", "forecasting", "validation"]|16|
|infra/web/styles.css|["dataset", "validation"]|7|
|ops/EXECUTOR_BOOTSTRAP_PROMPT.md|["datasets", "forecast", "vonavy", "vonavy-agent"]|21|
|ops/PHASE1_EXECUTOR_PROMPT.md|["dataset", "vonavy", "vonavy-agent"]|4|
|ops/README.md|["datasets"]|1|
|ops/account-bootstrap.md|["vonavy"]|5|
|ops/aurora-cost-cleanup.md|["vonavy", "vonavy-agent"]|8|
|ops/aws-agent-toolkit-hardening.md|["Vonavy", "vonavy"]|10|
|ops/executor-contract.md|["datasets"]|1|
|ops/gpu-quotas.md|["vonavy"]|1|
|ops/phase-1-synth-and-review.md|["Vonavy", "vonavy"]|12|
|ops/phase-2b-synth-and-review.md|["dataset", "validation"]|11|
|pyproject.toml|["chronos", "experiment", "forecasting", "vonavy-agent", "vonavy_agent", "xgboost"]|18|
|src/vonavy_agent/__init__.py|["Experiment"]|1|
|src/vonavy_agent/__main__.py|["vonavy_agent"]|1|
|src/vonavy_agent/adapters.py|["Chronos", "Experiment", "chronos", "dataset", "vonavy_agent"]|16|
|src/vonavy_agent/api.py|["Dataset", "Experiment", "chronos", "dataset", "datasets", "experiment", "vonavy_agent"]|79|
|src/vonavy_agent/backtest.py|["Dataset", "Experiment", "backtest", "dataset", "datasets", "forecast", "vonavy-agent", "vonavy_agent"]|45|
|src/vonavy_agent/cli.py|["dataset", "validation", "vonavy-agent", "vonavy_agent"]|12|
|src/vonavy_agent/datasets.py|["Dataset", "dataset", "vonavy_agent"]|97|
|src/vonavy_agent/domain.py|["Dataset", "Experiment", "Forecast", "dataset", "forecast"]|28|
|src/vonavy_agent/eligibility.py|["forecast", "vonavy_agent"]|8|
|src/vonavy_agent/executor.py|["Dataset", "backtest", "dataset", "datasets", "experiment", "vonavy_agent"]|19|
|src/vonavy_agent/experiments.py|["Dataset", "Experiment", "backtest", "dataset", "datasets", "experiment", "forecast", "vonavy_agent"]|52|
|src/vonavy_agent/exporting.py|["Chronos", "Experiment", "experiment", "vonavy_agent"]|14|
|src/vonavy_agent/forecasting/__init__.py|["forecasting", "vonavy_agent"]|3|
|src/vonavy_agent/forecasting/aws_batch.py|["Dataset", "Forecast", "Validation", "chronos2", "dataset", "datasets", "forecast", "forecasting", "neuralnet", "vonavy", "vonavy_agent", "xgboost"]|112|
|src/vonavy_agent/forecasting/chronos2.py|["Chronos", "Chronos-2", "Forecast", "chronos", "chronos2", "dataset", "forecast", "forecasting", "vonavy_agent"]|93|
|src/vonavy_agent/forecasting/contracts.py|["Forecast", "chronos", "chronos2", "dataset", "experiment", "forecast", "neuralnet", "vonavy", "vonavy_agent", "xgboost"]|56|
|src/vonavy_agent/forecasting/evaluation.py|["Forecast", "forecast", "forecasting", "vonavy_agent"]|6|
|src/vonavy_agent/forecasting/mapping.py|["Forecast", "dataset", "forecast", "forecasting", "vonavy_agent"]|25|
|src/vonavy_agent/forecasting/model.py|["Forecast", "dataset", "forecast", "forecasting", "vonavy_agent", "xgboost"]|67|
|src/vonavy_agent/forecasting/neural_net.py|["Forecast", "NeuralNet", "dataset", "forecast", "forecasting", "neuralnet", "vonavy_agent"]|79|
|src/vonavy_agent/forecasting/panel.py|["Forecast", "dataset", "forecast", "forecasting", "vonavy_agent"]|18|
|src/vonavy_agent/forecasting/worker.py|["Forecast", "Validation", "chronos2", "dataset", "forecast", "forecasting", "neuralnet", "vonavy-agent", "vonavy_agent", "xgboost"]|51|
|src/vonavy_agent/identity.py|["vonavy_agent"]|1|
|src/vonavy_agent/jobs.py|["Dataset", "Experiment", "backtest", "dataset", "experiment", "vonavy_agent"]|31|
|src/vonavy_agent/managed_files.py|["vonavy_agent"]|2|
|src/vonavy_agent/migrations/env.py|["vonavy_agent"]|1|
|src/vonavy_agent/migrations/versions/0001_initial.py|["Experiment", "dataset", "datasets", "experiment"]|31|
|src/vonavy_agent/migrations/versions/0002_owner_scope.py|["dataset", "datasets", "experiment"]|4|
|src/vonavy_agent/persistence.py|["Dataset", "Experiment", "dataset", "datasets", "experiment", "vonavy_agent"]|23|
|src/vonavy_agent/planner.py|["Experiment", "chronos", "experiment", "vonavy_agent"]|26|
|src/vonavy_agent/policy.py|["experiment", "vonavy_agent"]|3|
|src/vonavy_agent/ports.py|["Experiment", "Forecast", "forecast", "vonavy_agent"]|7|
|src/vonavy_agent/settings.py|["Experiment", "datasets", "vonavy-agent", "vonavy_agent"]|4|
|src/vonavy_agent/validation_contracts.py|["Validation", "dataset", "validation", "vonavy_agent"]|29|
|src/vonavy_agent/validation_worker/__init__.py|["validation", "vonavy_agent"]|2|
|src/vonavy_agent/validation_worker/artifacts.py|["validation", "vonavy_agent"]|2|
|src/vonavy_agent/validation_worker/aws_artifacts.py|["validation", "vonavy", "vonavy_agent"]|7|
|src/vonavy_agent/validation_worker/aws_batch.py|["Validation", "dataset", "datasets", "validation", "vonavy_agent"]|26|
|src/vonavy_agent/validation_worker/cli.py|["Validation", "dataset", "validation", "vonavy_agent"]|33|
|src/vonavy_agent/validation_worker/profiling.py|["Dataset", "Validation", "dataset", "validation", "vonavy_agent"]|30|
|src/vonavy_agent/validation_worker/worker.py|["Validation", "dataset", "validation", "vonavy_agent"]|41|
|src/vonavy_agent/web/app.js|["Experiment", "dataset", "datasets", "experiment"]|16|
|src/vonavy_agent/web/index.html|["Chronos", "Dataset", "Experiment", "chronos", "dataset", "experiment", "forecasting", "vonavy"]|17|
|tests/conftest.py|["Dataset", "Experiment", "dataset", "datasets", "experiment", "vonavy_agent"]|31|
|tests/test_api.py|["dataset", "datasets", "vonavy_agent"]|5|
|tests/test_blocker_regressions.py|["Dataset", "Experiment", "Validation", "backtest", "dataset", "datasets", "experiment", "vonavy_agent"]|35|
|tests/test_chronos2_forecasting.py|["Chronos", "Forecast", "chronos", "chronos2", "dataset", "forecast", "forecasting", "vonavy_agent"]|60|
|tests/test_comparison_request_json_validation.py|["chronos2", "dataset", "datasets", "forecast", "forecasting", "neuralnet", "vonavy_agent", "xgboost"]|17|
|tests/test_dataset_and_gate.py|["Dataset", "dataset", "datasets", "experiment", "vonavy_agent"]|15|
|tests/test_final_review_regressions.py|["Dataset", "Validation", "chronos", "dataset", "datasets", "experiment", "vonavy_agent"]|34|
|tests/test_forecast_container_build.py|["Chronos-2", "chronos", "forecast", "vonavy"]|18|
|tests/test_forecasting.py|["Forecast", "dataset", "forecast", "forecasting", "vonavy_agent", "xgboost"]|48|
|tests/test_last_delta_regressions.py|["Dataset", "datasets", "experiment", "forecast", "vonavy_agent"]|20|
|tests/test_multi_model_comparison.py|["Forecast", "forecast", "forecasting", "vonavy_agent"]|6|
|tests/test_neuralnet_forecasting.py|["Forecast", "Validation", "dataset", "forecast", "forecasting", "neuralnet", "vonavy_agent"]|42|
|tests/test_phase0_cloud_boundaries.py|["Forecast", "dataset", "datasets", "forecast", "vonavy_agent"]|30|
|tests/test_phase4d_evaluation.py|["Forecast", "Validation", "forecast", "forecasting", "vonavy_agent"]|15|
|tests/test_runner_planner_export.py|["Dataset", "backtest", "chronos", "experiment", "vonavy_agent"]|20|
|tests/test_terminal_bundle_regressions.py|["experiment", "vonavy_agent"]|6|
|tests/test_validation_aws.py|["dataset", "datasets", "validation", "vonavy_agent"]|36|
|tests/test_validation_cli.py|["Validation", "dataset", "datasets", "validation", "vonavy", "vonavy_agent"]|28|
|tests/test_validation_container.py|["dataset", "validation", "vonavy-agent"]|4|
|tests/test_validation_contracts.py|["Validation", "dataset", "datasets", "validation", "vonavy", "vonavy_agent"]|33|
|tests/test_validation_worker.py|["Validation", "dataset", "datasets", "validation", "vonavy", "vonavy_agent"]|74|
|uv.lock|["chronos", "forecasting", "vonavy-agent", "xgboost"]|34|
