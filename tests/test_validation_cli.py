from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from vonavy_agent.validation_contracts import ValidationResult


def write_request(workspace: Path, input_name: str, media_type: str = "text/csv") -> Path:
    payload = {
        "schema_version": "validation-request/v1",
        "job_id": "job-cli",
        "owner_id": "owner-cli",
        "dataset_id": "dataset-cli",
        "input": {
            "storage": "local",
            "path": input_name,
            "media_type": media_type,
        },
        "output": {"storage": "local", "path": "result.json"},
        "requested_at": datetime(2026, 7, 20, tzinfo=UTC).isoformat(),
    }
    path = workspace / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run_cli(workspace: Path, request: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "vonavy_agent.cli",
            "validate-dataset",
            "--workspace",
            str(workspace),
            "--request",
            str(request),
            "--result",
            "result.json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_exit_codes_and_result_contract(tmp_path: Path) -> None:
    valid = tmp_path / "valid.csv"
    valid.write_text("value\n1\n2\n")
    request = write_request(tmp_path, "valid.csv")
    completed = run_cli(tmp_path, request)
    assert completed.returncode == 0
    result = ValidationResult.model_validate_json((tmp_path / "result.json").read_text())
    assert result.status.value == "succeeded"
    assert "1\n2" not in completed.stdout

    invalid = tmp_path / "invalid.csv"
    invalid.write_text("value\n")
    request = write_request(tmp_path, "invalid.csv")
    completed = run_cli(tmp_path, request)
    assert completed.returncode == 2
    result = ValidationResult.model_validate_json((tmp_path / "result.json").read_text())
    assert result.status.value == "invalid"

    missing_request = write_request(tmp_path, "missing.csv")
    completed = run_cli(tmp_path, missing_request)
    assert completed.returncode == 1
    result = ValidationResult.model_validate_json((tmp_path / "result.json").read_text())
    assert result.status.value == "failed"
    assert result.validation_errors[0].code == "file_not_found"


def test_cli_writes_failure_result_for_invalid_request(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text('{"schema_version":"wrong"}', encoding="utf-8")
    completed = run_cli(tmp_path, request)
    assert completed.returncode == 1
    result = ValidationResult.model_validate_json((tmp_path / "result.json").read_text())
    assert result.status.value == "failed"
    assert result.validation_errors[0].code == "invalid_request"


def test_cli_reports_atomic_output_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    from vonavy_agent.validation_worker import cli as validation_cli

    input_path = tmp_path / "valid.csv"
    input_path.write_text("value\n1\n", encoding="utf-8")
    request = write_request(tmp_path, "valid.csv")

    def fail_write(self, relative: str, content: bytes):
        raise OSError("simulated output failure")

    monkeypatch.setattr(validation_cli.LocalFileArtifactWriter, "write_bytes", fail_write)
    assert validation_cli.run_cli(request, "result.json", tmp_path) == 1
    assert "output_write_failure" in capsys.readouterr().out


def test_local_cli_rejects_s3_contract_without_cloud_side_effects(tmp_path: Path) -> None:
    payload = {
        "schema_version": "validation-request/v1",
        "job_id": "job-s3",
        "owner_id": "owner-s3",
        "dataset_id": "dataset-s3",
        "input": {
            "storage": "s3",
            "bucket": "vonavy-data-bucket",
            "key": "datasets/input.csv",
            "version_id": "version-1",
            "media_type": "text/csv",
        },
        "output": {
            "storage": "s3",
            "bucket": "vonavy-results-bucket",
            "key": "validation/result.json",
        },
        "requested_at": datetime(2026, 7, 20, tzinfo=UTC).isoformat(),
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    completed = run_cli(tmp_path, request)
    assert completed.returncode == 1
    result = ValidationResult.model_validate_json((tmp_path / "result.json").read_text())
    assert result.validation_errors[0].code == "unsupported_storage"
