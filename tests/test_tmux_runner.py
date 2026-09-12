"""Tests for safe tmux shell serialization and exit-status recording."""

from pathlib import Path

from slrharness.tmux_runner import WorkerSpec, _wrap_with_signal


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
