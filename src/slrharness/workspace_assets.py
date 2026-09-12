"""Deploy bundled Claude Code agents and skills into a research workspace."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

CLAUDE_AGENT_FILES = (
    "slr-scoper.md",
    "slr-manager.md",
    "slr-worker.md",
    "topic-coordinator.md",
    "academic-paper-worker.md",
    "academic-metadata-checker.md",
    "technical-source-worker.md",
)
CLAUDE_SKILL_DIRS = ("slr-scoping", "slr-topic-research")


def _plugins_root() -> Path:
    """Locate plugin assets in a checkout or an installed wheel."""
    checkout_root = Path(__file__).resolve().parents[2] / "plugins"
    if checkout_root.is_dir():
        return checkout_root
    bundled = resources.files("slrharness").joinpath("resources", "plugins")
    return Path(str(bundled))


def deploy_claude_assets(workspace: Path) -> list[Path]:
    """Copy required project-scoped agents and skill into ``workspace``."""
    source = _plugins_root()
    agents_dir = workspace / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    deployed: list[Path] = []
    for filename in CLAUDE_AGENT_FILES:
        src = source / "agents" / filename
        if not src.is_file():
            raise FileNotFoundError(f"Bundled Claude agent is missing: {src}")
        dst = agents_dir / filename
        shutil.copyfile(src, dst)
        deployed.append(dst)

    for skill_name in CLAUDE_SKILL_DIRS:
        skill_src = source / "skills" / skill_name / "SKILL.md"
        if not skill_src.is_file():
            raise FileNotFoundError(f"Bundled Claude skill is missing: {skill_src}")
        skill_dst = workspace / ".claude" / "skills" / skill_name / "SKILL.md"
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src, skill_dst)
        deployed.append(skill_dst)
    return deployed
