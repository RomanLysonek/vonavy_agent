from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vonavy_agent.validation_contracts import (
    InputArtifact,
    S3InputArtifact,
    S3OutputArtifact,
)
from vonavy_agent.validation_worker.artifacts import (
    ArtifactTooLargeError,
    UnsafeArtifactPathError,
)


@dataclass(frozen=True, slots=True)
class S3WriteReceipt:
    bucket: str
    key: str
    version_id: str


class S3FileArtifactReader:
    """Materialize one immutable S3 object version into bounded ephemeral storage."""

    def __init__(
        self,
        client: Any,
        *,
        allowed_bucket: str,
        allowed_key_prefix: str,
        max_bytes: int,
        temporary_root: Path | None = None,
    ) -> None:
        self.client = client
        self.allowed_bucket = allowed_bucket
        self.allowed_key_prefix = allowed_key_prefix
        self.max_bytes = max_bytes
        self.temporary_root = temporary_root

    def _validate(self, artifact: InputArtifact) -> S3InputArtifact:
        if not isinstance(artifact, S3InputArtifact):
            raise UnsafeArtifactPathError("S3 reader cannot materialize a non-S3 artifact")
        if artifact.bucket != self.allowed_bucket:
            raise UnsafeArtifactPathError("S3 input bucket is outside the allowed boundary")
        if not artifact.key.startswith(self.allowed_key_prefix):
            raise UnsafeArtifactPathError("S3 input key is outside the allowed owner boundary")
        return artifact

    @contextmanager
    def materialize(self, artifact: InputArtifact) -> Iterator[Path]:
        source = self._validate(artifact)
        head = self.client.head_object(
            Bucket=source.bucket,
            Key=source.key,
            VersionId=source.version_id,
        )
        content_length = int(head.get("ContentLength", -1))
        if content_length < 0:
            raise OSError("S3 object metadata did not include a valid content length")
        if content_length > self.max_bytes:
            raise ArtifactTooLargeError("S3 object exceeds the configured materialization limit")

        temp_root = str(self.temporary_root) if self.temporary_root is not None else None
        with tempfile.TemporaryDirectory(prefix="vonavy-validation-", dir=temp_root) as directory:
            path = Path(directory) / "input"
            response = self.client.get_object(
                Bucket=source.bucket,
                Key=source.key,
                VersionId=source.version_id,
            )
            body = response["Body"]
            written = 0
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise ArtifactTooLargeError(
                                "S3 object exceeds the configured materialization limit"
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                with suppress(Exception):
                    body.close()
            if written != content_length:
                raise OSError("S3 object length changed during materialization")
            yield path


class S3FileArtifactWriter:
    """Publish a result as one immutable version in a server-owned S3 prefix."""

    def __init__(
        self,
        client: Any,
        *,
        allowed_bucket: str,
        allowed_key_prefix: str,
    ) -> None:
        self.client = client
        self.allowed_bucket = allowed_bucket
        self.allowed_key_prefix = allowed_key_prefix

    def write_bytes(self, artifact: S3OutputArtifact, content: bytes) -> S3WriteReceipt:
        if artifact.bucket != self.allowed_bucket:
            raise UnsafeArtifactPathError("S3 output bucket is outside the allowed boundary")
        if not artifact.key.startswith(self.allowed_key_prefix):
            raise UnsafeArtifactPathError("S3 output key is outside the allowed owner boundary")
        response = self.client.put_object(
            Bucket=artifact.bucket,
            Key=artifact.key,
            Body=content,
            ContentType="application/json",
            ServerSideEncryption="AES256",
            Tagging="state=validation-result&retention=demo",
        )
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id:
            raise OSError("Versioned S3 result publication returned no version identifier")
        return S3WriteReceipt(
            bucket=artifact.bucket,
            key=artifact.key,
            version_id=version_id,
        )


__all__ = [
    "S3FileArtifactReader",
    "S3FileArtifactWriter",
    "S3WriteReceipt",
]
