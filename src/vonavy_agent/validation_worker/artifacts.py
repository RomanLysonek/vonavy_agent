from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from pathlib import Path
from typing import Protocol

from vonavy_agent.validation_contracts import InputArtifact, LocalInputArtifact


class UnsafeArtifactPathError(OSError):
    pass


class ArtifactReader(Protocol):
    def materialize(self, artifact: InputArtifact) -> AbstractContextManager[Path]: ...


class ArtifactWriter(Protocol):
    def write_bytes(self, relative: str, content: bytes) -> Path: ...


class LocalWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        if not self.root.exists():
            self.root.mkdir(parents=True, mode=0o700)
        info = self.root.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UnsafeArtifactPathError("workspace root must be a real directory")

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        path = Path(relative)
        if path.is_absolute() or not path.parts:
            raise UnsafeArtifactPathError("artifact path must be relative")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise UnsafeArtifactPathError("artifact path contains an unsafe component")
        return path.parts

    def _open_directory(self, parts: tuple[str, ...], *, create: bool) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current = os.open(self.root, flags)
        try:
            for part in parts:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=current)
                next_fd = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = next_fd
            return current
        except OSError:
            os.close(current)
            raise

    def open_input_fd(self, relative: str) -> int:
        parts = self._parts(relative)
        parent = self._open_directory(parts[:-1], create=False)
        try:
            fd = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise UnsafeArtifactPathError("input artifact must be a regular non-symlink file")
            return fd
        except Exception:
            os.close(fd)
            raise

    def write_bytes(self, relative: str, content: bytes) -> Path:
        parts = self._parts(relative)
        parent = self._open_directory(parts[:-1], create=True)
        temp_name = f".{parts[-1]}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        temp_fd: int | None = None
        try:
            try:
                existing = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
            ):
                raise UnsafeArtifactPathError("output destination must be a regular file")
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(temp_fd, "wb") as handle:
                temp_fd = None
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            return self.root.joinpath(*parts)
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            with suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=parent)
            os.close(parent)


class LocalFileArtifactReader:
    def __init__(self, workspace: LocalWorkspace) -> None:
        self.workspace = workspace

    @contextmanager
    def materialize(self, artifact: InputArtifact) -> Iterator[Path]:
        if not isinstance(artifact, LocalInputArtifact):
            raise UnsafeArtifactPathError("local reader cannot materialize non-local artifacts")
        fd = self.workspace.open_input_fd(artifact.path)
        try:
            candidates = (Path(f"/proc/self/fd/{fd}"), Path(f"/dev/fd/{fd}"))
            stable_path = next((candidate for candidate in candidates if candidate.exists()), None)
            if stable_path is None:
                raise UnsafeArtifactPathError(
                    "this platform cannot expose a stable descriptor-backed input path"
                )
            yield stable_path
        finally:
            os.close(fd)


class LocalFileArtifactWriter:
    def __init__(self, workspace: LocalWorkspace) -> None:
        self.workspace = workspace

    def write_bytes(self, relative: str, content: bytes) -> Path:
        return self.workspace.write_bytes(relative, content)
