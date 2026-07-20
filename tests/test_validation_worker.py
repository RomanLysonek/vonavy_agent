from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from vonavy_agent.hashing import canonical_json
from vonavy_agent.validation_contracts import (
    LocalInputArtifact,
    LocalOutputArtifact,
    ValidationLimits,
    ValidationRequest,
    ValidationStatus,
)
from vonavy_agent.validation_worker.artifacts import (
    LocalFileArtifactReader,
    LocalWorkspace,
    UnsafeArtifactPathError,
)
from vonavy_agent.validation_worker.worker import validate_request


def request_for(
    path: str,
    media_type: str,
    *,
    limits: ValidationLimits | None = None,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> ValidationRequest:
    return ValidationRequest(
        job_id="job-1",
        owner_id="owner-1",
        dataset_id="dataset-1",
        input=LocalInputArtifact(
            path=path,
            media_type=media_type,
            expected_size_bytes=expected_size,
            expected_sha256=expected_sha256,
        ),
        output=LocalOutputArtifact(path="output/result.json"),
        limits=limits or ValidationLimits(),
        requested_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def column(result, name: str):
    return next(profile for profile in result.columns if profile.name == name)


def validate_local(request: ValidationRequest, workspace: LocalWorkspace):
    return validate_request(request, LocalFileArtifactReader(workspace))


def test_valid_mixed_csv_profiles_without_non_json_numbers(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    input_path = tmp_path / "input" / "mixed.csv"
    input_path.parent.mkdir()
    input_path.write_text(
        "date,value,active,label\n"
        "2026-01-01,1,true,alpha\n"
        "2026-01-02,2,false,=SUM(A1:A2)\n"
        "2026-01-03,,true,\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    result = validate_local(
        request_for(
            "input/mixed.csv",
            "text/csv",
            expected_size=input_path.stat().st_size,
            expected_sha256=digest,
        ),
        workspace,
    )

    assert result.status == ValidationStatus.SUCCEEDED
    assert result.row_count == 3
    assert result.column_count == 4
    assert column(result, "value").logical_type == "numeric"
    assert column(result, "active").boolean is not None
    date_profile = column(result, "date")
    assert date_profile.temporal is not None
    assert date_profile.temporal.timezone_aware is False
    label = column(result, "label")
    assert label.string is not None
    assert any(item.value == "=SUM(A1:A2)" for item in label.string.top_values)
    serialized = canonical_json(result.model_dump(mode="json"))
    assert "NaN" not in serialized
    assert "Infinity" not in serialized


def test_valid_parquet_equivalent(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    input_path = tmp_path / "input" / "mixed.parquet"
    input_path.parent.mkdir()
    frame = pd.DataFrame(
        {
            "value": [1.0, 2.0, float("nan"), float("inf")],
            "active": [True, False, True, None],
            "when": pd.to_datetime(["2026-01-01", "2026-01-02", None, "2026-01-04"], utc=True),
        }
    )
    pytest.importorskip("pyarrow")
    frame.to_parquet(input_path, index=False)
    result = validate_local(
        request_for("input/mixed.parquet", "application/vnd.apache.parquet"),
        workspace,
    )

    assert result.status == ValidationStatus.SUCCEEDED
    numeric = column(result, "value").numeric
    assert numeric is not None
    assert numeric.non_finite_count == 1
    assert any(issue.code == "non_finite_values" for issue in result.warnings)
    assert column(result, "when").logical_type == "timestamp"


def test_size_checksum_and_type_validation(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    path = tmp_path / "data.csv"
    path.write_text("a\n1\n", encoding="utf-8")

    size = validate_local(request_for("data.csv", "text/csv", expected_size=999), workspace)
    checksum = validate_local(
        request_for("data.csv", "text/csv", expected_sha256="0" * 64), workspace
    )
    media = validate_local(request_for("data.csv", "application/json"), workspace)

    assert size.status == ValidationStatus.INVALID
    assert size.validation_errors[0].code == "size_mismatch"
    assert checksum.status == ValidationStatus.INVALID
    assert checksum.validation_errors[0].code == "checksum_mismatch"
    assert media.status == ValidationStatus.INVALID
    assert media.validation_errors[0].code == "unsupported_media_type"


@pytest.mark.parametrize(
    ("content", "code", "status"),
    [
        ("", "empty_dataset", ValidationStatus.INVALID),
        ("a,b\n", "empty_dataset", ValidationStatus.INVALID),
        ("a,a\n1,2\n", "duplicate_columns", ValidationStatus.INVALID),
        ('a,b\n1,"unterminated\n', "malformed_csv", ValidationStatus.FAILED),
    ],
)
def test_csv_failure_classes(
    tmp_path: Path, content: str, code: str, status: ValidationStatus
) -> None:
    workspace = LocalWorkspace(tmp_path)
    path = tmp_path / "case.csv"
    path.write_text(content, encoding="utf-8")
    result = validate_local(request_for("case.csv", "text/csv"), workspace)
    assert result.status == status
    assert result.validation_errors[0].code == code


def test_limits_and_string_bounding(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    path = tmp_path / "wide.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    too_wide = validate_local(
        request_for("wide.csv", "text/csv", limits=ValidationLimits(max_columns=2)),
        workspace,
    )
    assert too_wide.status == ValidationStatus.INVALID
    assert too_wide.validation_errors[0].code == "too_many_columns"

    path.write_text("a\n1\n2\n", encoding="utf-8")
    too_long = validate_local(
        request_for("wide.csv", "text/csv", limits=ValidationLimits(max_rows=1)),
        workspace,
    )
    assert too_long.status == ValidationStatus.INVALID
    assert too_long.validation_errors[0].code == "too_many_rows"

    long_path = tmp_path / "long.csv"
    long_path.write_text("label\n" + "x" * 100 + "\n", encoding="utf-8")
    bounded = validate_local(
        request_for(
            "long.csv",
            "text/csv",
            limits=ValidationLimits(max_string_sample_length=16),
        ),
        workspace,
    )
    stats = column(bounded, "label").string
    assert stats is not None
    assert stats.values_exceeding_sample_limit == 1
    assert len(stats.top_values[0].value) == 16
    assert any(issue.code == "string_values_truncated" for issue in bounded.warnings)


def test_sampling_is_deterministic_and_labelled(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    path = tmp_path / "sample.csv"
    path.write_text("value\n" + "\n".join(str(index) for index in range(100)) + "\n")
    request = request_for(
        "sample.csv",
        "text/csv",
        limits=ValidationLimits(max_profile_rows=10),
    )
    first = validate_local(request, workspace)
    second = validate_local(request, workspace)

    assert first.status == second.status == ValidationStatus.SUCCEEDED
    assert first.columns == second.columns
    assert first.warnings == second.warnings
    assert first.resource_usage.profiled_rows == 10
    assert first.resource_usage.profiling_sampled is True


def test_malformed_parquet_and_nested_types(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    workspace = LocalWorkspace(tmp_path)
    broken = tmp_path / "broken.parquet"
    broken.write_bytes(b"PAR1broken")
    result = validate_local(request_for("broken.parquet", "application/x-parquet"), workspace)
    assert result.status == ValidationStatus.FAILED
    assert result.validation_errors[0].code == "malformed_parquet"

    nested = tmp_path / "nested.parquet"
    table = pa.table({"items": pa.array([[1, 2], [3]], type=pa.list_(pa.int64()))})
    pq.write_table(table, nested)
    nested_result = validate_local(
        request_for("nested.parquet", "application/x-parquet"), workspace
    )
    assert nested_result.status == ValidationStatus.INVALID
    assert nested_result.validation_errors[0].code == "unsupported_column_type"


def test_symlink_input_is_rejected_without_reading_target(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    secret = tmp_path / "secret.csv"
    secret.write_text("secret\nshould-not-leak\n")
    link = tmp_path / "link.csv"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = validate_local(request_for("link.csv", "text/csv"), workspace)
    assert result.status == ValidationStatus.FAILED
    assert result.validation_errors[0].code == "file_not_found"
    assert "should-not-leak" not in canonical_json(result.model_dump(mode="json"))


def test_atomic_writer_rejects_symlink_and_preserves_existing_file(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    output = tmp_path / "output.json"
    output.write_text("old")
    workspace.write_bytes("output.json", b"new")
    assert output.read_bytes() == b"new"

    target = tmp_path / "target.json"
    target.write_text("secret")
    output.unlink()
    try:
        output.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafeArtifactPathError):
        workspace.write_bytes("output.json", b"replacement")
    assert target.read_text() == "secret"


def test_utf8_bom_and_high_cardinality_profile(tmp_path: Path) -> None:
    workspace = LocalWorkspace(tmp_path)
    path = tmp_path / "bom.csv"
    values = "\n".join(f"value-{index}" for index in range(30))
    path.write_bytes(("name\n" + values + "\n").encode("utf-8-sig"))
    result = validate_local(
        request_for(
            "bom.csv",
            "text/csv",
            limits=ValidationLimits(max_distinct_values=5),
        ),
        workspace,
    )
    assert result.status == ValidationStatus.SUCCEEDED
    profile = column(result, "name").string
    assert profile is not None
    assert profile.distinct_count == 30
    assert len(profile.top_values) == 5


def test_input_byte_limit_and_unexpected_parser_error_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = LocalWorkspace(tmp_path)
    path = tmp_path / "data.csv"
    path.write_text("value\nsecret-row-content\n", encoding="utf-8")
    too_large = validate_local(
        request_for(
            "data.csv",
            "text/csv",
            limits=ValidationLimits(max_input_bytes=1),
        ),
        workspace,
    )
    assert too_large.status == ValidationStatus.INVALID
    assert too_large.validation_errors[0].code == "input_too_large"

    def explode(*args, **kwargs):
        raise RuntimeError("secret-row-content")

    monkeypatch.setattr("vonavy_agent.validation_worker.worker.scan_csv", explode)
    failed = validate_local(request_for("data.csv", "text/csv"), workspace)
    payload = canonical_json(failed.model_dump(mode="json"))
    assert failed.status == ValidationStatus.FAILED
    assert failed.validation_errors[0].code == "internal_error"
    assert "secret-row-content" not in payload


def test_atomic_writer_failure_preserves_previous_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = LocalWorkspace(tmp_path)
    destination = tmp_path / "result.json"
    destination.write_bytes(b"previous")

    def fail_replace(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("vonavy_agent.validation_worker.artifacts.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated rename failure"):
        workspace.write_bytes("result.json", b"new")
    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.glob(".result.json.*.tmp")) == []


def test_core_accepts_s3_contract_through_an_adapter(tmp_path: Path) -> None:
    from contextlib import contextmanager

    from vonavy_agent.validation_contracts import S3InputArtifact, S3OutputArtifact

    path = tmp_path / "materialized.csv"
    path.write_text("value\n1\n2\n", encoding="utf-8")

    class FakeS3Reader:
        @contextmanager
        def materialize(self, artifact):
            assert artifact.storage == "s3"
            yield path

    request = ValidationRequest(
        job_id="job-s3",
        owner_id="owner-s3",
        dataset_id="dataset-s3",
        input=S3InputArtifact(
            bucket="vonavy-data-bucket",
            key="datasets/input.csv",
            version_id="version-1",
            media_type="text/csv",
        ),
        output=S3OutputArtifact(
            bucket="vonavy-results-bucket",
            key="validation/result.json",
        ),
        requested_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    result = validate_request(request, FakeS3Reader())
    assert result.status == ValidationStatus.SUCCEEDED
    assert result.input_identity is not None
    assert result.input_identity.storage == "s3"
    assert result.input_identity.version_id == "version-1"


def test_worker_detects_input_changes_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = LocalWorkspace(tmp_path)
    path = tmp_path / "mutable.csv"
    path.write_text("value\n1\n2\n", encoding="utf-8")

    from vonavy_agent.validation_worker import worker as worker_module

    original_scan = worker_module.scan_csv

    def mutate_after_scan(scan_path, limits, checksum, deadline):
        summary = original_scan(scan_path, limits, checksum, deadline)
        path.write_text("value\n9\n9\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(worker_module, "scan_csv", mutate_after_scan)
    result = validate_local(request_for("mutable.csv", "text/csv"), workspace)
    assert result.status == ValidationStatus.FAILED
    assert result.validation_errors[0].code == "input_changed"
