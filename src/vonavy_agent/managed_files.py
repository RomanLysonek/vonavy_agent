from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO

from vonavy_agent.errors import AgentError
from vonavy_agent.settings import Settings


def verify_fd(fd: int, expected_hash: str, expected_size: int | None = None) -> int:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise AgentError("artifact_integrity_failure", "Managed artifact is not a regular file")
    if expected_size is not None and info.st_size != expected_size:
        raise AgentError("artifact_integrity_failure", "Managed artifact size is invalid")
    digest = sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise AgentError("artifact_integrity_failure", "Managed artifact hash is invalid")
    os.lseek(fd, 0, os.SEEK_SET)
    return info.st_size


@contextmanager
def verified_managed_file(
    settings: Settings,
    relative: Path,
    expected_hash: str,
    expected_size: int | None = None,
) -> Iterator[BinaryIO]:
    try:
        fd = settings.open_managed_file(relative)
        verify_fd(fd, expected_hash, expected_size)
    except (AgentError, OSError, ValueError):
        if "fd" in locals():
            os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        yield handle


def publish_bytes(
    settings: Settings,
    directory: Path,
    filename: str,
    content: bytes,
    expected_hash: str,
) -> Path:
    parent_fd = settings.open_managed_dir_fd(directory, create=True)
    temp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temp_fd: int | None = None
    try:
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(temp_fd, "wb") as output:
            temp_fd = None
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        with suppress(FileExistsError):
            os.link(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        winner_fd = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            verify_fd(winner_fd, expected_hash, len(content))
        finally:
            os.close(winner_fd)
        return directory / filename
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=parent_fd)
        os.close(parent_fd)
