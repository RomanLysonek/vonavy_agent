from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VONAVY_AGENT_", extra="ignore")

    managed_root: Path = Field(default=Path(".vonavy-agent"))
    host: str = "127.0.0.1"
    port: int = 8765
    max_upload_bytes: int = 250 * 1024 * 1024
    max_profile_categories: int = 20
    worker_lease_seconds: int = 30
    worker_poll_seconds: float = 0.5
    worker_max_attempts: int = 2
    supervise_worker: bool = True

    @property
    def database_path(self) -> Path:
        return self.managed_root / "agent.sqlite3"

    @property
    def inbox_path(self) -> Path:
        return self.managed_root / "inbox"

    def ensure_directories(self) -> None:
        root = self.managed_root.absolute()
        if root.exists() or root.is_symlink():
            root_stat = root.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise OSError(f"Managed root must be a real directory: {root}")
        else:
            root.mkdir(parents=True, mode=0o700)
        database = root / "agent.sqlite3"
        if database.exists() or database.is_symlink():
            database_stat = database.lstat()
            if stat.S_ISLNK(database_stat.st_mode) or not stat.S_ISREG(database_stat.st_mode):
                raise OSError(f"Managed database must be a real file: {database}")
        for relative in (
            "inbox",
            "blobs/sha256",
            "datasets",
            "jobs/tmp",
            "runs",
            "imports",
            "exports",
        ):
            directory_fd = self.open_managed_dir_fd(Path(relative), create=True)
            os.close(directory_fd)

    def open_managed_dir_fd(self, relative: Path, *, create: bool = False) -> int:
        parts = self._relative_parts(relative)
        root = self.managed_root.absolute()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open(root, flags)
        try:
            for part in parts:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except OSError:
            os.close(current_fd)
            raise

    def open_managed_file(self, relative: Path, flags: int = os.O_RDONLY) -> int:
        parts = self._relative_parts(relative)
        if not parts:
            raise ValueError("Managed file path must not be empty")
        parent_fd = self.open_managed_dir_fd(Path(*parts[:-1]))
        try:
            return os.open(
                parts[-1],
                flags | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)

    @staticmethod
    def _relative_parts(relative: Path) -> tuple[str, ...]:
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Managed paths must be normal relative paths")
        return relative.parts
