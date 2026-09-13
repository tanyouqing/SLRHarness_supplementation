"""Agent backend abstraction for SLRHarness.

This module isolates all agent-platform-specific logic (CLI binary names,
flags, calling conventions) behind a common interface.  The orchestrator
imports only the ``AgentBackend`` protocol and the ``get_backend`` factory;
it never references a specific agent CLI directly.

Adding a new backend
--------------------
1. Create a class that satisfies the ``AgentBackend`` protocol.
2. Register it in ``_BACKENDS`` at the bottom of this file.
3. Done — users select it with ``--agent-backend <name>``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Public protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentBackend(Protocol):
    """Interface every agent backend must satisfy."""

    @property
    def name(self) -> str:
        """Short identifier shown in logs and ``--agent-backend`` help."""
        ...

    def check_available(self) -> None:
        """Raise ``SystemExit`` if the backend's CLI tool is not on PATH."""
        ...

    def build_command(
        self,
        agent_name: str | None,
        prompt: str,
        cwd: Path,
    ) -> list[str]:
        """Return an argv list suitable for direct execution.

        ``cwd`` is supplied separately to the process runner; it remains in
        the interface so backends can use it for platform-specific flags.
        """
        ...


@dataclass(frozen=True)
class AgentExecutionResult:
    """Captured result from one non-interactive agent invocation."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def execute_agent(
    backend: AgentBackend,
    agent_name: str | None,
    prompt: str,
    cwd: Path,
    timeout: int,
) -> AgentExecutionResult:
    """Execute an agent directly using an argv list and capture its result."""
    command = backend.build_command(agent_name, prompt, cwd)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return AgentExecutionResult(
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            timed_out=True,
        )
    return AgentExecutionResult(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


# ---------------------------------------------------------------------------
# Concrete backends
# ---------------------------------------------------------------------------


class KiroBackend:
    """Backend for the Kiro CLI (``kiro-cli``)."""

    @property
    def name(self) -> str:
        return "kiro"

    def check_available(self) -> None:
        import sys

        if not shutil.which("kiro-cli"):
            sys.exit(
                "kiro-cli is not installed or not on PATH.\n"
                "Install Kiro and ensure `kiro-cli` is available."
            )

    def build_command(
        self,
        agent_name: str | None,
        prompt: str,
        cwd: Path,
    ) -> list[str]:
        command = [
            "kiro-cli",
            "chat",
            "--no-interactive",
            "--trust-all-tools",
        ]
        if agent_name:
            command.extend(["--agent", agent_name])
        command.append(prompt)
        return command


class ClaudeCodeBackend:
    """Backend for the Claude Code CLI (``claude``)."""

    @property
    def name(self) -> str:
        return "claude-code"

    def check_available(self) -> None:
        import sys

        if not shutil.which("claude"):
            sys.exit(
                "claude is not installed or not on PATH.\n"
                "Install Claude Code and ensure `claude` is available."
            )

    def build_command(
        self,
        agent_name: str | None,
        prompt: str,
        cwd: Path,
    ) -> list[str]:
        # Disable Claude Code's "background tasks running after 600s; terminating"
        # ceiling. The harness relies on long-running subagents (paper workers,
        # metadata checkers) that often exceed 10 minutes when fetching and
        # reading many papers; the harness enforces its own per-attempt timeouts
        # (coordinator_timeout, academic_max_turns, etc.) so we never want
        # Claude itself to kill subagents silently mid-flight.
        command = [
            "env",
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0",
            "claude",
            "--dangerously-skip-permissions",
        ]
        if agent_name:
            command.extend(["--agent", agent_name])
        command.extend(["-p", prompt])
        return command


# ---------------------------------------------------------------------------
# Registry & factory
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[AgentBackend]] = {
    "kiro": KiroBackend,
    "claude-code": ClaudeCodeBackend,
}

DEFAULT_BACKEND = "kiro"


def available_backends() -> list[str]:
    """Return the names of all registered backends."""
    return sorted(_BACKENDS)


def get_backend(name: str) -> AgentBackend:
    """Instantiate a backend by name.

    Raises ``SystemExit`` with a helpful message if *name* is not registered.
    """
    import sys

    cls = _BACKENDS.get(name)
    if cls is None:
        sys.exit(
            f"Unknown agent backend: {name!r}\n"
            f"Available backends: {', '.join(available_backends())}"
        )
    return cls()
