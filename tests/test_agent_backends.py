"""Tests for the agent backend abstraction."""

import subprocess
from pathlib import Path

from slrharness.agent_backends import (
    AgentBackend,
    AgentExecutionResult,
    ClaudeCodeBackend,
    KiroBackend,
    available_backends,
    execute_agent,
    get_backend,
)


def test_kiro_satisfies_protocol():
    assert isinstance(KiroBackend(), AgentBackend)


def test_kiro_name():
    assert KiroBackend().name == "kiro"


def test_kiro_build_command_structure():
    cmd = KiroBackend().build_command("slr-manager", "do stuff", Path("/tmp/ws"))
    assert cmd[:2] == ["kiro-cli", "chat"]
    assert cmd[cmd.index("--agent") + 1] == "slr-manager"
    assert cmd[-1] == "do stuff"


def test_kiro_build_command_quotes_special_chars():
    prompt = 'it\'s a "test"; $(touch unsafe)'
    cmd = KiroBackend().build_command("a", prompt, Path("/tmp/a b"))
    assert cmd[0] == "kiro-cli"
    assert cmd[-1] == prompt


def test_get_backend_returns_kiro():
    backend = get_backend("kiro")
    assert isinstance(backend, KiroBackend)


def test_get_backend_unknown_exits(monkeypatch):
    import pytest

    with pytest.raises(SystemExit):
        get_backend("nope")


def test_available_backends_includes_kiro():
    assert "kiro" in available_backends()


def test_claude_passes_requested_agent_name_as_argv():
    cmd = ClaudeCodeBackend().build_command(
        "slr-scoper", "prepare scope", Path("/tmp/ws")
    )
    assert cmd == [
        "claude",
        "--dangerously-skip-permissions",
        "--agent",
        "slr-scoper",
        "-p",
        "prepare scope",
    ]


def test_claude_without_agent_name_is_backwards_compatible():
    cmd = ClaudeCodeBackend().build_command(None, "hello", Path("/tmp/ws"))
    assert cmd == ["claude", "--dangerously-skip-permissions", "-p", "hello"]


def test_claude_prompt_is_one_argv_element():
    prompt = "quotes ' \" ; $(touch nope) && exit 2"
    cmd = ClaudeCodeBackend().build_command("slr-scoper", prompt, Path("/tmp/a b"))
    assert cmd[-1] == prompt


def test_execute_agent_preserves_cwd_output_and_exit_code(monkeypatch, tmp_path: Path):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 4, "out", "err")

    monkeypatch.setattr("slrharness.agent_backends.subprocess.run", fake_run)
    result = execute_agent(
        ClaudeCodeBackend(), "slr-scoper", "unsafe ; prompt", tmp_path, 12
    )
    assert isinstance(result, AgentExecutionResult)
    assert result.exit_code == 4
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert seen["cwd"] == tmp_path
    assert seen["timeout"] == 12
    assert seen["command"][-1] == "unsafe ; prompt"
