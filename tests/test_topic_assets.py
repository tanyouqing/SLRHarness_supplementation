"""The deployed Claude agents expose only role-appropriate capabilities."""

from pathlib import Path

from slrharness.workspace_assets import deploy_claude_assets


def test_topic_agent_definitions_and_skill_are_deployed(tmp_path: Path) -> None:
    deploy_claude_assets(tmp_path)
    agents = tmp_path / ".claude" / "agents"
    coordinator = (agents / "topic-coordinator.md").read_text(encoding="utf-8")
    academic = (agents / "academic-paper-worker.md").read_text(encoding="utf-8")
    checker = (agents / "academic-metadata-checker.md").read_text(encoding="utf-8")
    technical = (agents / "technical-source-worker.md").read_text(encoding="utf-8")
    skill = (
        tmp_path / ".claude" / "skills" / "slr-topic-research" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "  - Agent" in coordinator
    assert "mcp__arxiv__*" not in coordinator
    assert "SendMessage" not in coordinator.split("---", 2)[1]
    assert "mcp__arxiv__*" in academic
    assert "mcp__scholarly__*" in academic
    assert "mcp__arxiv__*" in checker
    assert "mcp__tavily__*" in technical
    assert "mcp__tavily__*" not in academic
    assert "TAVILY_API_KEY=" not in coordinator + academic + checker + technical
    assert "Evidence ID" in skill
    assert "correction_requests.jsonl" in skill
    assert "coordinator_manifest.json" in skill
