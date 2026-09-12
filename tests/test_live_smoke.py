"""Explicitly opt-in Claude Code smoke test; skipped by the default suite."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from slrharness.agent_backends import ClaudeCodeBackend, execute_agent
from slrharness.workspace_assets import deploy_claude_assets


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("SLRHARNESS_RUN_LIVE_TESTS") != "1",
    reason="set SLRHARNESS_RUN_LIVE_TESTS=1 to spend a minimal Claude invocation",
)
def test_claude_discovers_project_scoper(tmp_path: Path) -> None:
    if not shutil.which("claude"):
        pytest.skip("Claude Code is not installed")
    deploy_claude_assets(tmp_path)
    result = execute_agent(
        ClaudeCodeBackend(),
        "slr-scoper",
        "Do not search or use MCP. Reply with exactly LIVE-SMOKE-OK; write no files.",
        tmp_path,
        60,
    )
    assert not result.timed_out
    assert result.exit_code == 0
    assert "LIVE-SMOKE-OK" in result.stdout
