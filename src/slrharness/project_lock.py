"""Single-workspace process lock used by mutating orchestrator commands."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

LOCK_FILENAME = ".slrharness.lock"


def project_lock_path(workspace: Path) -> Path:
    root = workspace.resolve()
    git_dir = root / ".git"
    return (git_dir if git_dir.is_dir() else root) / LOCK_FILENAME


class ProjectLockedError(RuntimeError):
    pass


class ProjectLock:
    def __init__(self, workspace: Path) -> None:
        self.path = project_lock_path(workspace)
        self._owned = False

    def __enter__(self) -> ProjectLock:
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            detail = self.path.read_text(encoding="utf-8", errors="replace")[:1000]
            raise ProjectLockedError(
                f"workspace is already locked: {self.path}; owner={detail}. "
                "If no process is running, remove only this stale lock file."
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self._owned = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False
