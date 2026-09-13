"""Workspace initialization from a filled-in SCOPE.md.

Creates a new workspace under workspaces/{theme_slug}/ with:
- SCOPE_ORIGINAL.md (immutable copy)
- SCOPE.md (living document)
- TASKS.md (empty, manager will populate)
- SUMMARY.md (empty, manager will populate)
- topics/, assets/ directories
- .gitignore
- Initialized git repo with an initial commit
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from slrharness.agent_backends import get_backend
from slrharness.scope_workflow import (
    AWAITING_SCOPE_APPROVAL,
    EMPTY_SUMMARY_MD,
    EMPTY_TASKS_MD,
    PROPOSAL_FILENAME,
    SOURCES_FILENAME,
    WORKSPACE_GITIGNORE,
    ScopeConfig,
    approve_scope,
    initialize_direct_approved_state,
    initialize_scope_project,
    load_scope_state,
    reject_scope,
    request_scope_revision,
    run_scope_preparation,
)
from slrharness.prioritization import write_scope_prioritization
from slrharness.workspace_assets import deploy_claude_assets


def slugify(text: str) -> str:
    """Convert a theme name to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        raise ValueError(f"Cannot slugify empty or invalid theme: {text!r}")
    return slug


def run_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the workspace directory."""
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def init_workspace(
    theme: str,
    scope_source: Path,
    workspaces_dir: Path,
    force: bool = False,
) -> Path:
    """Initialize a new workspace from a filled-in SCOPE.md.

    Returns the path to the created workspace directory.
    """
    if not scope_source.is_file():
        sys.exit(f"Scope file not found: {scope_source}")

    theme_slug = slugify(theme)
    workspace = workspaces_dir / theme_slug

    if workspace.exists():
        if not force:
            sys.exit(
                f"Workspace already exists: {workspace}\n"
                f"Use --force to overwrite (destroys existing workspace)."
            )
        shutil.rmtree(workspace)

    # Create directory structure
    workspace.mkdir(parents=True)
    (workspace / "topics").mkdir()
    (workspace / "assets").mkdir()

    # Copy scope to both SCOPE.md and SCOPE_ORIGINAL.md
    scope_content = scope_source.read_text(encoding="utf-8")
    (workspace / "SCOPE.md").write_text(scope_content, encoding="utf-8")
    (workspace / "SCOPE_ORIGINAL.md").write_text(scope_content, encoding="utf-8")

    # Initialize empty control files
    (workspace / "TASKS.md").write_text(
        EMPTY_TASKS_MD.format(theme=theme), encoding="utf-8"
    )
    (workspace / "SUMMARY.md").write_text(
        EMPTY_SUMMARY_MD.format(theme=theme), encoding="utf-8"
    )
    (workspace / ".gitignore").write_text(WORKSPACE_GITIGNORE, encoding="utf-8")

    # Placeholder to keep empty directories in git
    (workspace / "topics" / ".gitkeep").touch()
    (workspace / "assets" / ".gitkeep").touch()
    deploy_claude_assets(workspace)
    initialize_direct_approved_state(workspace, theme)
    write_scope_prioritization(workspace, scope_content)

    # Initialize git repo
    run_git(workspace, "init", "-b", "main")
    run_git(workspace, "add", "-A")
    run_git(
        workspace,
        "commit",
        "-m",
        "round-0: initialize workspace from scoping",
    )
    run_git(workspace, "tag", "round-0")

    return workspace


def _add_workspace_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace", type=Path, required=True, help="Path to the research workspace."
    )


def _add_backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent-backend",
        choices=["claude-code"],
        default="claude-code",
        help=(
            "Agent backend for scope preparation (currently: claude-code). "
            "Other backends remain available for formal review rounds."
        ),
    )
    parser.add_argument(
        "--scope-timeout",
        type=int,
        default=900,
        help="Scope-agent timeout in seconds (default: 900).",
    )


def _print_scope_next_step(workspace: Path) -> None:
    state = load_scope_state(workspace)
    print(f"Scope status: {state['status']}")
    print(f"Proposal revision: {state['revision']}")
    if state["status"] == AWAITING_SCOPE_APPROVAL:
        print(f"Review: python -m slrharness.scope show --workspace {workspace}")
        print(
            "Approve: python -m slrharness.scope approve "
            f"--workspace {workspace} --revision {state['revision']}"
        )
    elif state.get("failure"):
        print(f"Failure: {state['failure']}")
        print(f"Retry: python -m slrharness.scope resume --workspace {workspace}")


def _run_scope_cli(args: argparse.Namespace) -> None:
    workspace = args.workspace.resolve()
    backend = get_backend(args.agent_backend)
    backend.check_available()
    ok = run_scope_preparation(workspace, backend, timeout=args.scope_timeout)
    _print_scope_next_step(workspace)
    if not ok:
        raise SystemExit(1)


def _main_subcommand(argv: list[str]) -> None:
    command = argv[0]
    parser = argparse.ArgumentParser(prog=f"slrharness.scope {command}")

    if command == "prepare":
        parser.add_argument("--theme", required=True)
        parser.add_argument("--topic")
        parser.add_argument("--draft-scope", type=Path)
        parser.add_argument("--workspaces-dir", type=Path, default=Path("workspaces"))
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--target-surveys", type=int, default=3)
        parser.add_argument("--max-initial-candidates", type=int, default=5)
        _add_backend_args(parser)
        args = parser.parse_args(argv[1:])
        if not args.topic and not args.draft_scope:
            parser.error("provide --topic or --draft-scope")
        if args.draft_scope and not args.draft_scope.is_file():
            parser.error(f"draft scope not found: {args.draft_scope}")
        topic = args.topic or f"Draft scope: {args.draft_scope.stem}"
        workspace = args.workspaces_dir.resolve() / slugify(args.theme)
        if workspace.exists():
            if not args.force:
                parser.error(f"workspace already exists: {workspace}")
            shutil.rmtree(workspace)
        config = ScopeConfig(
            target_surveys=args.target_surveys,
            max_initial_candidates=args.max_initial_candidates,
        )
        initialize_scope_project(args.theme, topic, workspace, config, args.draft_scope)
        args.workspace = workspace
        _run_scope_cli(args)
        return

    if command == "resume":
        _add_workspace_arg(parser)
        _add_backend_args(parser)
        args = parser.parse_args(argv[1:])
        state = load_scope_state(args.workspace.resolve())
        if state["status"] == AWAITING_SCOPE_APPROVAL:
            _print_scope_next_step(args.workspace.resolve())
            return
        _run_scope_cli(args)
        return

    if command == "show":
        _add_workspace_arg(parser)
        args = parser.parse_args(argv[1:])
        workspace = args.workspace.resolve()
        state = load_scope_state(workspace)
        print(json.dumps(state, indent=2, ensure_ascii=False))
        proposal_name = str(state.get("proposal_path") or PROPOSAL_FILENAME)
        sources_name = state.get("sources_path")
        proposal = workspace / proposal_name
        sources = workspace / str(sources_name or SOURCES_FILENAME)
        if proposal.is_file():
            print(f"\n--- {proposal_name} ---\n")
            print(proposal.read_text(encoding="utf-8"))
        if sources_name and sources.is_file():
            print(f"\n--- {sources_name} ---\n")
            print(sources.read_text(encoding="utf-8"))
        return

    if command in ("approve", "reject"):
        _add_workspace_arg(parser)
        parser.add_argument("--revision", type=int, required=True)
        args = parser.parse_args(argv[1:])
        workspace = args.workspace.resolve()
        changed = (
            approve_scope(workspace, args.revision)
            if command == "approve"
            else reject_scope(workspace, args.revision)
        )
        action = "Approved" if command == "approve" else "Rejected"
        message = (
            f"{action} scope revision {args.revision}."
            if changed
            else f"Scope revision {args.revision} was already {action.lower()}."
        )
        print(message)
        if command == "approve":
            print(
                "Continue: python -m slrharness.orchestrator run "
                f"--workspace {workspace} --agent-backend claude-code"
            )
        return

    if command == "revise":
        _add_workspace_arg(parser)
        feedback_group = parser.add_mutually_exclusive_group(required=True)
        feedback_group.add_argument("--feedback")
        feedback_group.add_argument("--feedback-file", type=Path)
        _add_backend_args(parser)
        args = parser.parse_args(argv[1:])
        feedback = args.feedback
        if args.feedback_file:
            if not args.feedback_file.is_file():
                parser.error(f"feedback file not found: {args.feedback_file}")
            feedback = args.feedback_file.read_text(encoding="utf-8")
        request_scope_revision(args.workspace.resolve(), feedback)
        _run_scope_cli(args)
        return

    if command == "init":
        parser.add_argument("--theme", required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--approved-scope", dest="scope", type=Path)
        group.add_argument("--scope", dest="scope", type=Path)
        parser.add_argument("--workspaces-dir", type=Path, default=Path("workspaces"))
        parser.add_argument("--force", action="store_true")
        args = parser.parse_args(argv[1:])
        workspace = init_workspace(
            args.theme, args.scope, args.workspaces_dir, args.force
        )
        print(f"Initialized approved workspace: {workspace}")
        return

    raise AssertionError(f"Unhandled scope subcommand: {command}")


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    subcommands = {"prepare", "resume", "show", "approve", "revise", "reject", "init"}
    if argv and argv[0] in subcommands:
        _main_subcommand(argv)
        return

    # Backwards-compatible explicit approved-scope initialization.
    parser = argparse.ArgumentParser(
        description=(
            "Initialize an already-approved SLRHarness workspace. For an initial "
            "topic, use the `prepare` subcommand instead."
        ),
    )
    parser.add_argument(
        "--theme",
        required=True,
        help="Theme name. Will be slugified for the workspace directory.",
    )
    parser.add_argument(
        "--scope",
        type=Path,
        required=True,
        help="Path to the filled-in SCOPE.md file.",
    )
    parser.add_argument(
        "--workspaces-dir",
        type=Path,
        default=Path("workspaces"),
        help="Parent directory for workspaces (default: ./workspaces).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing workspace with the same slug.",
    )
    args = parser.parse_args(argv)

    workspace = init_workspace(
        theme=args.theme,
        scope_source=args.scope,
        workspaces_dir=args.workspaces_dir,
        force=args.force,
    )
    print(f"Initialized workspace: {workspace}")
    print("  git repo with initial commit (tag: round-0)")
    print("  SCOPE_ORIGINAL.md preserved as immutable baseline")
    print()
    print(f"Next: python -m slrharness.orchestrator --workspace {workspace}")


if __name__ == "__main__":
    main()
