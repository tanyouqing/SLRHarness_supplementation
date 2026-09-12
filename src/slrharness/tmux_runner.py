"""Reusable tmux session/window management with wait-for synchronization.

This module handles:
- Creating and tearing down named tmux sessions
- Spawning commands in new windows
- Blocking until a command completes (via tmux wait-for channels)
- Graceful timeouts

All functions shell out to the tmux binary — no Python tmux bindings required.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class TmuxError(RuntimeError):
    """Raised when a tmux command fails unexpectedly."""


@dataclass(frozen=True)
class WorkerSpec:
    """Specification for a single worker to spawn in a tmux window."""

    window_name: str
    command: list[str] | tuple[str, ...] | str
    done_channel: str  # tmux wait-for channel name to signal completion
    cwd: Path | None = None
    exit_status_path: Path | None = None


def check_tmux_available() -> None:
    """Raise if tmux is not on PATH."""
    if not shutil.which("tmux"):
        raise TmuxError(
            "tmux is not installed or not on PATH. Install with `brew install tmux`."
        )


def session_exists(session: str) -> bool:
    """Return True if a tmux session with the given name exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def kill_session(session: str) -> None:
    """Kill a tmux session if it exists. Idempotent."""
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
    )


def create_session(
    session: str, first_window_name: str, cwd: Path | None = None
) -> None:
    """Create a new detached tmux session with a named first window.

    If a session with the same name already exists, it is killed first.
    """
    check_tmux_available()
    kill_session(session)
    command = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        first_window_name,
    ]
    if cwd is not None:
        command.extend(["-c", str(cwd)])
    subprocess.run(
        command,
        check=True,
    )


def new_window(session: str, window_name: str, cwd: Path | None = None) -> None:
    """Create a new window in an existing session."""
    command = [
        "tmux",
        "new-window",
        "-t",
        session,
        "-n",
        window_name,
    ]
    if cwd is not None:
        command.extend(["-c", str(cwd)])
    subprocess.run(
        command,
        check=True,
    )


def send_command(session: str, window: str, command: str) -> None:
    """Send a shell command (with Enter) to a specific tmux window."""
    subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            f"{session}:{window}",
            command,
            "Enter",
        ],
        check=True,
    )


def wait_for_channel(channel: str, timeout: int) -> bool:
    """Block until the tmux wait-for channel is signaled, or timeout.

    Returns True if the signal was received, False if the timeout fired.
    """
    # Enforce the timeout in Python rather than shelling out to a `timeout`
    # binary — macOS doesn't ship one by default, and this keeps the
    # orchestrator portable without requiring `brew install coreutils`.
    try:
        result = subprocess.run(
            ["tmux", "wait-for", channel],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def capture_pane(session: str, window: str, lines: int = 200) -> str:
    """Capture the visible pane content of a window as text."""
    result = subprocess.run(
        [
            "tmux",
            "capture-pane",
            "-t",
            f"{session}:{window}",
            "-p",  # print to stdout
            "-S",
            f"-{lines}",  # history lines
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout


def get_pane_pid(session: str, window: str) -> int | None:
    """Return the shell PID for a tmux window, if it can be queried."""
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            f"{session}:{window}",
            "#{pane_pid}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def spawn_workers(
    session: str,
    workers: Iterable[WorkerSpec],
) -> list[WorkerSpec]:
    """Spawn a set of workers in a fresh tmux session.

    Each worker runs in its own window. The given command is wrapped so that
    a tmux wait-for signal is sent when it completes, enabling synchronization.

    Returns the list of WorkerSpecs actually spawned.
    """
    check_tmux_available()
    workers_list = list(workers)
    if not workers_list:
        return []

    # Create session with the first worker's window
    first = workers_list[0]
    create_session(session, first.window_name, first.cwd)
    send_command(session, first.window_name, _wrap_with_signal(first))

    # Add windows for remaining workers
    for worker in workers_list[1:]:
        new_window(session, worker.window_name, worker.cwd)
        send_command(session, worker.window_name, _wrap_with_signal(worker))

    return workers_list


def wait_for_all(
    workers: Iterable[WorkerSpec],
    timeout: int,
) -> dict[str, bool]:
    """Wait for all workers to signal completion.

    Returns a dict mapping window_name -> True (completed) or False (timed out).

    The total wait time is bounded by `timeout` seconds, shared across all
    workers. Workers run in parallel, so the wait time is the max of any
    single worker, not the sum.
    """
    deadline = time.monotonic() + timeout
    results: dict[str, bool] = {}
    for worker in workers:
        remaining = max(1, int(deadline - time.monotonic()))
        completed = wait_for_channel(worker.done_channel, timeout=remaining)
        results[worker.window_name] = completed
    return results


def _wrap_with_signal(worker: WorkerSpec) -> str:
    """Wrap a command so that it signals the done channel on completion.

    The wrapped command runs the user's command, then signals tmux regardless
    of exit status (using a shell `;` separator). This way a crashed worker
    still unblocks the orchestrator.
    """
    command = (
        worker.command
        if isinstance(worker.command, str)
        else shlex.join(worker.command)
    )
    signal_cmd = f"tmux wait-for -S {shlex.quote(worker.done_channel)}"
    if worker.exit_status_path is None:
        return f"{command}; {signal_cmd}"

    status_path = shlex.quote(str(worker.exit_status_path))
    return (
        f"{command}; slrharness_status=$?; "
        f"printf '%s' \"$slrharness_status\" > {status_path}; {signal_cmd}"
    )
