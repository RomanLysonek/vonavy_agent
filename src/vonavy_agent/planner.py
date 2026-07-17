from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vonavy_agent.adapters import adapter_capabilities
from vonavy_agent.domain import ExperimentSpec
from vonavy_agent.errors import AgentError
from vonavy_agent.experiments import create_experiment_spec
from vonavy_agent.hashing import canonical_hash, canonical_json
from vonavy_agent.persistence import (
    DataProfile,
    ExperimentSpecRow,
    PlannerProposal,
    Run,
    RunMetric,
    session_scope,
)


def propose_experiments(engine: Engine, spec_id: str) -> PlannerProposal:
    with Session(engine) as session:
        spec_row = session.get(ExperimentSpecRow, spec_id)
        if spec_row is None:
            raise AgentError("spec_not_found", "Experiment spec does not exist", status_code=404)
        spec = ExperimentSpec.model_validate_json(spec_row.canonical_json)
        profile = session.get_one(DataProfile, spec.profile_id)
        profile_json = json.loads(profile.canonical_json)
        prior_runs = session.scalars(
            select(Run).where(Run.spec_id == spec_id, Run.summary_json.is_not(None))
        ).all()
        calibration_metrics = (
            session.scalars(
                select(RunMetric).where(
                    RunMetric.run_id.in_([run.id for run in prior_runs]),
                    RunMetric.role == "calibration",
                    RunMetric.origin.is_(None),
                    RunMetric.horizon.is_(None),
                )
            ).all()
            if prior_runs
            else []
        )
    proposals: list[dict[str, Any]] = []
    base_payload = spec.model_dump(mode="json")
    if not prior_runs:
        proposals.append(
            {
                "rank": 1,
                "kind": "baseline_first",
                "reason_code": "no_comparable_successful_run",
                "reason": "Establish the three always-available baselines before adding complexity.",
                "evidence": {"profile_hash": profile.profile_hash, "successful_runs": 0},
                "estimated_cost": "low",
                "requires_confirmation": True,
                "spec": base_payload,
            }
        )
    active_features = list(spec.selected_features())
    if active_features and any(model.kind == "ridge_direct" for model in spec.models):
        ablated = spec.model_copy(update={"feature_allow_list": ()})
        proposals.append(
            {
                "rank": 0,
                "kind": "feature_ablation",
                "reason_code": "measure_incremental_feature_value",
                "reason": "Remove candidate features to measure their incremental calibration value.",
                "evidence": {
                    "profile_hash": profile.profile_hash,
                    "features": [feature.name for feature in active_features],
                },
                "estimated_cost": "low",
                "requires_confirmation": True,
                "spec": ablated.model_dump(mode="json"),
            }
        )
    if profile_json["date_start"] and profile_json["date_end"]:
        history_days = (
            date.fromisoformat(profile_json["date_end"])
            - date.fromisoformat(profile_json["date_start"])
        ).days + 1
        expanded_window = min(spec.training_window_days * 2, 3650)
        if history_days >= expanded_window + spec.horizon_days:
            window_spec = spec.model_copy(update={"training_window_days": expanded_window})
            proposals.append(
                {
                    "rank": 0,
                    "kind": "window_sensitivity",
                    "reason_code": "sufficient_history_for_longer_window",
                    "reason": "Test whether a bounded longer history changes calibration evidence.",
                    "evidence": {
                        "profile_hash": profile.profile_hash,
                        "history_days": history_days,
                        "current_window_days": spec.training_window_days,
                    },
                    "estimated_cost": "medium",
                    "requires_confirmation": True,
                    "spec": window_spec.model_dump(mode="json"),
                }
            )
    capabilities = {item["adapter_kind"]: item for item in adapter_capabilities(engine)}
    anomaly = capabilities["anomaly"]
    if anomaly["available"] and len(proposals) < 3:
        proposals.append(
            {
                "rank": 0,
                "kind": "anomaly_diagnostic",
                "reason_code": "validated_optional_capability",
                "reason": (
                    "Optionally inspect exceedance/alert rates as a diagnostic; without truth labels "
                    "they are not false-alarm rates."
                ),
                "evidence": {"capability": anomaly},
                "estimated_cost": "medium",
                "requires_confirmation": True,
                "spec": base_payload,
            }
        )
    chronos = capabilities["chronos"]
    baseline_wape = [
        metric.value
        for metric in calibration_metrics
        if metric.metric == "wape" and metric.value is not None
    ]
    if (
        chronos["available"]
        and chronos["supports_probability_calibration"]
        and baseline_wape
        and len(proposals) < 3
    ):
        proposals.append(
            {
                "rank": 0,
                "kind": "chronos_challenger",
                "reason_code": "capability_and_baseline_prerequisites_passed",
                "reason": "Compare a provenance-complete optional challenger only after baseline evidence.",
                "evidence": {
                    "capability": chronos,
                    "best_calibration_wape": min(baseline_wape),
                },
                "estimated_cost": "high",
                "requires_confirmation": True,
                "requires_heavyweight_confirmation": True,
                "spec": base_payload,
            }
        )
    kind_order = {
        "baseline_first": 0,
        "feature_ablation": 1,
        "window_sensitivity": 2,
        "anomaly_diagnostic": 3,
        "chronos_challenger": 4,
    }
    proposals = sorted(proposals, key=lambda item: kind_order[item["kind"]])[:3]
    for index, proposal in enumerate(proposals, 1):
        proposal["rank"] = index
    input_evidence = {
        "spec_hash": spec_row.spec_hash,
        "profile_hash": profile.profile_hash,
        "run_ids": sorted(run.id for run in prior_runs),
        "capabilities": capabilities,
    }
    payload = {
        "schema_version": "1.0",
        "input_hash": canonical_hash(input_evidence),
        "input_evidence": input_evidence,
        "proposals": proposals,
    }
    with session_scope(engine) as session:
        row = PlannerProposal(
            input_hash=payload["input_hash"],
            canonical_json=canonical_json(payload),
        )
        session.add(row)
        session.flush()
        row_id = row.id
    with Session(engine) as session:
        return session.get_one(PlannerProposal, row_id)


def confirm_proposal(engine: Engine, proposal_id: str, rank: int) -> ExperimentSpecRow:
    with session_scope(engine) as session:
        proposal = session.get(PlannerProposal, proposal_id)
        if proposal is None:
            raise AgentError(
                "proposal_not_found", "Planner proposal does not exist", status_code=404
            )
        if proposal.confirmed_spec_id:
            raise AgentError("proposal_already_confirmed", "Proposal was already confirmed")
        payload = json.loads(proposal.canonical_json)
        selected = next(
            (item for item in payload["proposals"] if item["rank"] == rank),
            None,
        )
        if selected is None:
            raise AgentError("proposal_rank_not_found", "Requested proposal rank does not exist")
        spec = ExperimentSpec.model_validate(selected["spec"])
    row = create_experiment_spec(engine, spec)
    with session_scope(engine) as session:
        session.get_one(PlannerProposal, proposal_id).confirmed_spec_id = row.id
    return row
