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
CLAUDE_TEMPLATE_FILES = (
    "paper-note.md",
    "technical-note.md",
    "final-report.md",
)


def _plugins_root() -> Path:
    """Locate plugin assets in a checkout or an installed wheel."""
    checkout_root = Path(__file__).resolve().parents[2] / "plugins"
    if checkout_root.is_dir():
        return checkout_root
    bundled = resources.files("slrharness").joinpath("resources", "plugins")
    return Path(str(bundled))


def required_asset_sources() -> list[Path]:
    """List every runtime asset that must exist in a checkout or wheel."""
    root = _plugins_root()
    return [
        *(root / "agents" / name for name in CLAUDE_AGENT_FILES),
        *(root / "skills" / name / "SKILL.md" for name in CLAUDE_SKILL_DIRS),
        *(root / "templates" / name for name in CLAUDE_TEMPLATE_FILES),
    ]


def deploy_claude_assets(workspace: Path, *, overwrite: bool = True) -> list[Path]:
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
        if overwrite or not dst.exists():
            shutil.copyfile(src, dst)
        deployed.append(dst)

    for skill_name in CLAUDE_SKILL_DIRS:
        skill_src = source / "skills" / skill_name / "SKILL.md"
        if not skill_src.is_file():
            raise FileNotFoundError(f"Bundled Claude skill is missing: {skill_src}")
        skill_dst = workspace / ".claude" / "skills" / skill_name / "SKILL.md"
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not skill_dst.exists():
            shutil.copyfile(skill_src, skill_dst)
        deployed.append(skill_dst)
    for filename in CLAUDE_TEMPLATE_FILES:
        template_src = source / "templates" / filename
        if not template_src.is_file():
            raise FileNotFoundError(
                f"Bundled Claude template is missing: {template_src}"
            )
        template_dst = workspace / ".claude" / "templates" / filename
        template_dst.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not template_dst.exists():
            shutil.copyfile(template_src, template_dst)
        deployed.append(template_dst)
    return deployed
