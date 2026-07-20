from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from vonavy_agent.forecasting.contracts import (
    ForecastIssue,
    ForecastProfile,
    ForecastResult,
    ForecastStatus,
    ForecastTiming,
    HoldoutMetrics,
    InputIdentity,
    LocalForecastRequest,
)
from vonavy_agent.forecasting.model import run_xgboost_forecast

MAX_RESULT_BYTES = 2 * 1024 * 1024


def _safe_path(workspace: Path, relative: str, *, must_exist: bool) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("artifact paths must be workspace-relative")
    root = workspace.resolve(strict=True)
    resolved = (root / candidate).resolve(strict=must_exist)
    if resolved != root and root not in resolved.parents:
        raise ValueError("artifact path escapes workspace")
    if must_exist and (not resolved.is_file() or resolved.is_symlink()):
        raise ValueError("input artifact must be a regular non-symlink file")
    return resolved


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path, media_type: str) -> pd.DataFrame:
    if media_type == "text/csv":
        return pd.read_csv(path)
    if media_type == "application/vnd.apache.parquet":
        return pd.read_parquet(path)
    raise ValueError("unsupported media type")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise ValueError("result exceeds the worker output limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def run_local(request_path: Path, result_relative: str, workspace: Path | None = None) -> int:
    workspace = (workspace or request_path.parent).resolve(strict=True)
    request_path = _safe_path(workspace, str(request_path.relative_to(workspace)), must_exist=True)
    result_path = _safe_path(workspace, result_relative, must_exist=False)
    started = datetime.now(UTC)
    try:
        request = LocalForecastRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        input_path = _safe_path(workspace, request.input_path, must_exist=True)
        if input_path.stat().st_size > request.limits.max_bytes:
            raise ValueError("input exceeds max_bytes")
        if _hash(input_path) != request.input_sha256:
            raise ValueError("input SHA-256 does not match the immutable request")
        output_directory = _safe_path(workspace, request.output_directory, must_exist=False)
        output_directory.mkdir(parents=True, exist_ok=True)
        raw = _load(input_path, request.media_type)
        output = run_xgboost_forecast(
            raw=raw,
            mapping=request.mapping,
            training_end=pd.Timestamp(request.training_end),
            output_directory=output_directory,
            owner_id=request.owner_id,
            dataset_id=request.dataset_id,
            run_id=request.run_id,
            input_identity=InputIdentity(sha256=request.input_sha256),
            source_revision=request.source_revision,
            max_rows=request.limits.max_rows,
            max_entities=request.limits.max_entities,
            max_history_days=request.limits.max_history_days,
        )
        _atomic_json(result_path, output.result.model_dump(mode="json"))
        print(json.dumps({"event": "forecast_complete", "run_id": request.run_id}))
        return 0
    except (ValueError, ValidationError) as exc:
        failure = ForecastResult(
            status=ForecastStatus.INVALID,
            owner_id="unknown",
            dataset_id="00000000-0000-0000-0000-000000000000",
            run_id="00000000-0000-0000-0000-000000000000",
            input=InputIdentity(sha256="0" * 64),
            profile=ForecastProfile(
                rows=0,
                entities=0,
                history_start=None,
                training_end=started.date(),
                forecast_start=started.date(),
                forecast_end=started.date(),
                trainable_rows=0,
                fallback_rows=0,
            ),
            holdout=HoldoutMetrics(
                supported=False,
                rows=0,
                coverage=0.0,
                unsupported_reason="request or dataset invalid",
            ),
            artifacts=None,
            failure=ForecastIssue(code="forecast_invalid", message=str(exc)),
            timing=ForecastTiming(
                prepare_seconds=0,
                holdout_seconds=0,
                fit_seconds=0,
                forecast_seconds=0,
                total_seconds=0,
            ),
            started_at=started,
            finished_at=datetime.now(UTC),
        )
        _atomic_json(result_path, failure.model_dump(mode="json"))
        print(json.dumps({"event": "forecast_invalid", "message": str(exc)}))
        return 2
    except Exception as exc:
        print(
            json.dumps({"event": "forecast_failed", "error_type": type(exc).__name__}),
            file=sys.stderr,
        )
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="vonavy-agent-forecast-worker")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    raise SystemExit(run_local(args.request, args.result, args.workspace))


if __name__ == "__main__":
    main()
