"""Tests for safe tmux shell serialization and exit-status recording."""

import threading
from pathlib import Path

from slrharness.tmux_runner import WorkerSpec, _wrap_with_signal, wait_for_all


def test_wrap_serializes_argv_and_records_exit_status() -> None:
    spec = WorkerSpec(
        window_name="scope",
        command=["claude", "-p", "topic; touch should-not-run"],
        done_channel="scope-done",
        cwd=Path("/tmp/work space"),
        exit_status_path=Path("/tmp/work space/result.status"),
    )
    wrapped = _wrap_with_signal(spec)
    assert "'topic; touch should-not-run'" in wrapped
    assert "slrharness_status=$?" in wrapped
    assert "result.status" in wrapped
    assert "tmux wait-for -S scope-done" in wrapped


def test_wait_for_all_registers_channels_concurrently(monkeypatch) -> None:
    barrier = threading.Barrier(2)
    observed: list[str] = []

    def fake_wait(channel: str, timeout: int) -> bool:
        observed.append(channel)
        barrier.wait(timeout=1)
        return True

    monkeypatch.setattr("slrharness.tmux_runner.wait_for_channel", fake_wait)
    workers = [
        WorkerSpec("one", ["true"], "done-one"),
        WorkerSpec("two", ["true"], "done-two"),
    ]
    assert wait_for_all(workers, 2) == {"one": True, "two": True}
    assert set(observed) == {"done-one", "done-two"}
