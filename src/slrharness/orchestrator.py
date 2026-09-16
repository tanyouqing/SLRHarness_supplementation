"""Main orchestration loop for SLRHarness.

Drives the manager-worker loop:
  scope.py init -> [manager-plan -> workers (parallel) -> manager-review] x N

Workers never write to TASKS.md, SUMMARY.md, or SCOPE.md -- only to topics/.
The manager is the sole owner of control files and git commits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from slrharness.agent_backends import (
    DEFAULT_BACKEND,
    AgentBackend,
    available_backends,
    get_backend,
)
from slrharness.artifact_compiler import compile_topic_artifacts, import_staged_notes
from slrharness.contracts import AgentInvocation, SCHEMA_VERSION, atomic_write_json
from slrharness.control_plane import (
    apply_verification_observation,
    blocking_issue_ids,
    current_issues,
    ingest_metadata_observation,
    migrate_legacy_tasks,
    now as control_now,
    record_invocation,
    register_tasks,
    staging_directory,
    topic_tasks as state_topic_tasks,
    transition_issue,
    update_topic_task,
    update_round_status,
)
from slrharness.finalization import (
    FINAL_AUDIT_PATH,
    PREFINAL_AUDIT_PATH,
    FinalizationConfig,
    aggregate_and_audit,
    blocking_gap_summary,
    build_prefinal_prompt,
    build_repair_plan_prompt,
    collect_blocking_gaps,
    delivery_statistics,
    ensure_stable_repair_tasks,
    finalization_state,
    merge_semantic_prefinal_audit,
    update_finalization_state,
    validate_final_report,
    validate_prefinal_audit,
)
from slrharness.project_lock import ProjectLock, ProjectLockedError
from slrharness.prioritization import (
    PRIORITIZATION_PATH,
    VALID_LINE_ID,
    fallback_research_line_id,
    load_scope_prioritization,
)
from slrharness.report_sections import (
    assemble_report,
    build_report_packet,
    build_section_prompt,
    validate_section,
)
from slrharness.scope_workflow import (
    formal_research_block_reason,
    load_scope_state,
    state_path,
)
from slrharness.source_registry import (
    normalize_arxiv,
    normalize_doi,
    parse_frontmatter,
    write_frontmatter_field,
)
from slrharness.tmux_runner import (
    WorkerSpec,
    capture_pane,
    get_pane_pid,
    kill_session,
    spawn_workers,
    wait_for_all,
)
from slrharness.topic_execution import (
    LEGACY_WORKER,
    TOPIC_COORDINATOR,
    TopicExecutionConfig,
    build_stage_prompt,
    build_coordinator_prompt,
    derive_topic_status,
    finish_topic_attempt,
    initialize_topic_attempt,
    load_observations,
    record_topic_process,
    task_id_for_topic_path,
    topic_paths,
    validate_coordinator_outputs,
)
from slrharness.workspace_assets import deploy_claude_assets

DEFAULT_MAX_ROUNDS = 5
DEFAULT_NUM_WORKERS = 3
# Generous defaults: retrieval + multi-agent synthesis often exceeds 10 minutes.
DEFAULT_WORKER_TIMEOUT = 1800  # seconds
DEFAULT_MANAGER_TIMEOUT = 3600  # seconds (manager does heavier reasoning)

WORKER_SESSION = "slr-workers"
COORDINATOR_SESSION = "slr-topic-coordinators"
MANAGER_SESSION = "slr-manager"


@dataclass(frozen=True)
class Task:
    """A single pending task parsed from TASKS.md."""

    topic_path: str  # e.g., "topics/efficiency/pruning"
    description: str  # Free-form description
    execution_mode: str | None = None
    research_line_id: str | None = None
    prioritization_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.research_line_id is None:
            object.__setattr__(
                self, "research_line_id", fallback_research_line_id(self.topic_path)
            )
            if not self.prioritization_warnings:
                object.__setattr__(
                    self,
                    "prioritization_warnings",
                    ("missing line marker; stable topic-path fallback used",),
                )


@dataclass(frozen=True)
class CoordinatorInvocation:
    """One deterministic top-level coordinator process request."""

    task: Task
    attempt: int
    command: list[str]
    prompt: str


@dataclass(frozen=True)
class CoordinatorProcessResult:
    """Observable process result returned by a coordinator batch executor."""

    completed: bool
    exit_code: int | None
    timed_out: bool
    output: str = ""
    pid: int | None = None


def check_common_dependencies() -> None:
    """Verify non-agent external tools (tmux, git) are available."""
    missing = []
    if not shutil.which("tmux"):
        missing.append("tmux")
    if not shutil.which("git"):
        missing.append("git")
    if missing:
        sys.exit(f"Missing required tools: {', '.join(missing)}")


def parse_pending_tasks(tasks_md: Path) -> list[Task]:
    """Extract pending tasks (unchecked checkboxes) from TASKS.md.

    Looks for lines like:
      - [ ] topics/foo/bar — description
      - [ ] topics/foo/bar - description
      - [ ] topics/foo/bar: description

    When numbered round sections exist, only the ``### Pending`` subsection
    under the highest-numbered round is consumed.  This prevents unchecked
    entries in older rounds, ``Blocked``, or ``Backlog`` sections from being
    dispatched.  Pre-round legacy files remain supported: their Pending
    subsection is preferred, with a whole-file fallback for the oldest flat
    checkbox format.
    """
    content = tasks_md.read_text(encoding="utf-8")
    tasks: list[Task] = []
    known_lines: set[str] | None = None
    if (tasks_md.parent / PRIORITIZATION_PATH).is_file():
        contract = load_scope_prioritization(tasks_md.parent)
        known_lines = {
            str(item.get("line_id"))
            for item in contract.get("research_lines", [])
            if isinstance(item, dict) and item.get("line_id")
        }

    round_pattern = re.compile(
        r"^\s*##\s+Round\s+(\d+)\s*$", re.MULTILINE | re.IGNORECASE
    )
    round_matches = list(round_pattern.finditer(content))
    if round_matches:
        # Use the highest round number rather than merely the last heading so
        # a malformed/out-of-order historical section cannot become active.
        active_round = max(
            enumerate(round_matches), key=lambda item: (int(item[1].group(1)), item[0])
        )[1]
        following_section = re.search(
            r"^\s*##\s+.+$", content[active_round.end() :], re.MULTILINE
        )
        round_end = (
            active_round.end() + following_section.start()
            if following_section is not None
            else len(content)
        )
        round_content = content[active_round.end() : round_end]
        pending_content = _pending_section(round_content)
        if pending_content is None:
            return []
    else:
        # Compatibility for workspaces created before numbered rounds were
        # introduced.  If they have headings, respect Pending boundaries;
        # otherwise retain support for a flat list of checkboxes.
        pending_content = _pending_section(content)
        if pending_content is None:
            pending_content = content

    # Match checkbox lines with a topic path prefix. Accept several separators:
    # "topics/foo/bar — desc", "topics/foo/bar - desc", "topics/foo/bar -- desc",
    # "topics/foo/bar: desc". The separator requires whitespace on both sides
    # for hyphens (so hyphens inside path segments like "multi-gpu" aren't
    # mistaken for the separator), or must be a colon directly after the path.
    pattern = re.compile(
        r"""^\s*-\s*\[\s\]\s+
            (topics/\S+?)                   # topic path (non-greedy)
            \s*                              # optional trailing whitespace
            (?:                              # separator (one of):
                :\s+                         #   colon + whitespace
                | \s+(?:—|--|-)\s+          #   em-dash, double- or single-hyphen
            )
            (.+?)\s*$                        # description
        """,
        re.MULTILINE | re.VERBOSE,
    )
    for match in pattern.finditer(pending_content):
        topic_path = match.group(1).rstrip("/")
        description = match.group(2).strip()
        execution_mode = None
        research_line_id = None
        task_warnings: list[str] = []
        while True:
            marker = re.match(
                r"^\[(mode|line)=([^\]]+)\]\s*", description, re.IGNORECASE
            )
            if marker is None:
                break
            key, value = marker.group(1).lower(), marker.group(2).strip()
            description = description[marker.end() :].strip()
            if key == "mode" and value in {LEGACY_WORKER, TOPIC_COORDINATOR}:
                execution_mode = value
            elif key == "mode":
                task_warnings.append(f"unknown execution mode marker: {value}")
            elif value.upper() == "NOT_APPLICABLE":
                research_line_id = "NOT_APPLICABLE"
            elif VALID_LINE_ID.fullmatch(value.upper()):
                research_line_id = value.upper()
                if known_lines is not None and research_line_id not in known_lines:
                    task_warnings.append(
                        "unknown approved Research Line ID retained: "
                        f"{research_line_id}"
                    )
            else:
                task_warnings.append(f"invalid line marker ignored: {value}")
        tasks.append(
            Task(
                topic_path=topic_path,
                description=description,
                execution_mode=execution_mode,
                research_line_id=research_line_id,
                prioritization_warnings=tuple(task_warnings),
            )
        )

    return tasks


def _pending_section(content: str) -> str | None:
    """Return one Markdown ``### Pending`` body, bounded by peer headings."""
    pending = re.search(r"^\s*###\s+Pending\s*$", content, re.MULTILINE | re.IGNORECASE)
    if pending is None:
        return None
    following_heading = re.search(
        r"^\s*#{1,3}\s+.+$", content[pending.end() :], re.MULTILINE
    )
    end = (
        pending.end() + following_heading.start()
        if following_heading is not None
        else len(content)
    )
    return content[pending.end() : end]


def current_round(workspace: Path) -> int:
    """Determine the current round by counting existing round tags."""
    result = subprocess.run(
        ["git", "tag", "--list", "round-*"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    tags = [t.strip() for t in result.stdout.splitlines() if t.strip()]
    if not tags:
        return 0
    numbers = []
    for tag in tags:
        try:
            numbers.append(int(tag.removeprefix("round-")))
        except ValueError:
            continue
    return max(numbers) if numbers else 0


def is_workspace_clean(workspace: Path) -> bool:
    """Return True if the workspace git tree has no uncommitted changes.

    A clean tree is a precondition for starting (or resuming) a run: the
    manager commits once per phase, so any pre-existing uncommitted changes
    would get bundled into the first commit of the new round.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == ""


def count_completed_tasks(tasks_md: Path) -> int:
    """Count '- [x] topics/...' entries in TASKS.md."""
    if not tasks_md.is_file():
        return 0
    content = tasks_md.read_text(encoding="utf-8")
    return len(re.findall(r"^\s*-\s*\[x\]\s+topics/", content, flags=re.MULTILINE))


def tag_round(workspace: Path, round_num: int) -> None:
    """Create a git tag marking the end of a round. Idempotent."""
    subprocess.run(
        ["git", "tag", "-f", f"round-{round_num}"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )


def build_manager_prompt(workspace: Path, round_num: int, phase: str) -> str:
    """Build the prompt for a manager invocation.

    phase: "plan" or "review"
    """
    assert phase in ("plan", "review"), f"Invalid phase: {phase}"

    common_preamble = textwrap.dedent(f"""\
        You are the MANAGER AGENT for a literature review in workspace: {workspace}.
        This is round {round_num}, phase: {phase.upper()}.

        CRITICAL INVARIANTS (non-negotiable):
        1. SCOPE_ORIGINAL.md is READ-ONLY. Never modify it.
        2. SCOPE.md is the canonical approved scope and is READ-ONLY. Record any
           proposed future scope change as a non-binding note; never edit it.
        3. The Harness owns TASKS.md and lifecycle state. You may propose tasks
           during PLAN; the Harness imports them and immediately re-renders the
           file. You own only research synthesis content in SUMMARY.md.
        4. You never search databases directly -- that is workers' job.
        5. SUMMARY.md must be organized per the ranking & grouping criteria in
           SCOPE.md. Items must appear in the order those criteria dictate.
        6. At the end of this phase, commit all changes with git.

        Workspace files:
        - SCOPE_ORIGINAL.md (immutable baseline)
        - SCOPE.md (canonical approved scope; immutable during formal research)
        - {PRIORITIZATION_PATH} (program-derived approved research-line contract)
        - TASKS.md (task registry, checkbox format)
        - SUMMARY.md (evolving synthesis with comparison tables)
        - topics/ (READ ONLY). A TASKS entry `topics/x/y` has exactly one
          primary synthesis at `topics/x/y.md`. A same-stem directory
          `topics/x/y/` contains supporting paper notes, technical notes,
          but no authoritative task state; these are evidence attachments,
          not additional topics.
    """)

    if phase == "plan":
        phase_instructions = textwrap.dedent(f"""\
            PLAN PASS -- your responsibilities this invocation:

            1. Read SCOPE_ORIGINAL.md, SCOPE.md, TASKS.md, SUMMARY.md.
            2. If this is round 1 and TASKS.md has no pending tasks, read SCOPE.md
               and {PRIORITIZATION_PATH}, then break the approved research lines
               into concrete tasks. Prefer one research line (or one clear
               subquestion of it) per topic. Format each pending task as:
                 - [ ] topics/{{group}}/{{topic}} -- [line=RL-...] {{description}}
               Use `[line=NOT_APPLICABLE]` only for genuinely cross-cutting work.
               `[line=...]` and optional `[mode=...]` markers may appear in either
               order. Do not silently add a new line to the approved contract.
            3. If there are new files in topics/ since the last review commit,
               incorporate their findings into SUMMARY.md comparison tables,
               maintaining the ranking & grouping order defined in SCOPE.md.
               Aggregate research-line assessments from coordinator manifests;
               retain differing proposed tiers for final corpus-wide calibration.
               For legacy-worker `## Ranking Scores`, use them only as optional
               supporting input and recompute any approved composite.
            4. If workers propose new comparison dimensions, use only those
               already covered by the approved scope; record broader ideas as
               future work without editing SCOPE.md.
            5. Plan which pending tasks should run this round. Leave them as
               `- [ ]` in TASKS.md; the orchestrator will pick them up.
            6. Run:
                 git add -A
                 git commit -m "round-{round_num}-plan: <brief summary>"

            When finished, print DONE on a line by itself.
        """)
    else:  # review
        phase_instructions = textwrap.dedent(f"""\
            REVIEW PASS -- your responsibilities this invocation:

            1. Check `git status` and `git diff` to see what workers produced.
            2. For each TASKS topic, validate its primary `topics/.../*.md`
               synthesis. Do not count supporting notes below its same-stem
               directory as separate topics. You may consult
               paper notes, technical notes, the source registry, and the
               program issue ledger as supporting evidence. Validate:
               - Required sections present (Summary, Key Findings,
                 Comparison Data, References, Sources, Related Topics)
               - Inclusion/exclusion criteria from SCOPE.md were applied
               - All comparison dimensions from SCOPE.md are filled in
               - Sources include working links (paper, project page, code, dataset
                 where applicable)
               - Workspace context links at top are correct
               - Topic-coordinator output uses `## Scope-Driven Research-Line
                 Assessment`; incomplete optional prioritization is a warning,
                 not a reason to reject otherwise complete evidence artifacts
               - Legacy-worker output retains `## Ranking Scores` when SCOPE.md
                 defines an operational legacy ranking rubric
               - `## Related Topics` connects to at least one sibling topic when
                 such connections exist
            3. Do not change task IDs, checkboxes, stages, counts, or statuses;
               the Harness derives and renders them from SLR_STATE.json.
            4. Update SUMMARY.md with validated findings. Re-sort the comparison
               table and findings sections per the ranking & grouping criteria in
               SCOPE.md now that new items are in play.
            5. Run:
                 git add -A
                 git commit -m "round-{round_num}-review: <brief summary>"

            When finished, print DONE on a line by itself.
        """)

    return common_preamble + "\n" + phase_instructions


def build_worker_prompt(
    workspace: Path, topic_path: str, description: str, round_num: int
) -> str:
    """Build the prompt for a single worker invocation."""
    # Compute how many levels deep the topic file is inside topics/ so the
    # worker can form correct relative paths back to the workspace root.
    # e.g. topics/efficiency/pruning.md -> depth 2 under topics/ -> "../../"
    depth_below_root = len(Path(topic_path).parts)
    workspace_rel = "../" * depth_below_root

    return textwrap.dedent(f"""\
        You are a WORKER AGENT for a literature review in workspace: {workspace}.
        This is round {round_num}.

        YOUR TASK:
        {description}

        YOUR OUTPUT PATH:
        {topic_path}.md (within the workspace)

        PATHS (use these for workspace-context links at the top of your file):
        - SCOPE.md:    {workspace_rel}SCOPE.md
        - TASKS.md:    {workspace_rel}TASKS.md
        - SUMMARY.md:  {workspace_rel}SUMMARY.md

        CRITICAL CONSTRAINTS:
        1. Write ONLY under topics/ and assets/ in the workspace.
        2. Your PRIMARY write target is {topic_path}.md.
        3. You MAY make minor back-link edits to OTHER existing topic files --
           but only to append to their `## Related Topics` section. Do not edit
           any other section of another topic file.
        4. You MUST NOT modify TASKS.md, SUMMARY.md, SCOPE.md, or SCOPE_ORIGINAL.md.
        5. You MUST NOT run git commands. The manager handles commits.

        WORKFLOW:
        1. Read {workspace}/SCOPE.md -- criteria, comparison dimensions, and
           ranking & grouping criteria.
        2. Read {workspace}/TASKS.md -- what other topics exist (completed and
           in-progress).
        3. Read {workspace}/SUMMARY.md -- the current synthesis; what has already
           been found.
        4. Scan existing files in {workspace}/topics/ -- at minimum, read titles
           and summaries of completed files to identify siblings you should
           connect to.
        5. Search relevant databases (web search, web fetch, deep research tools).
        6. Apply the inclusion/exclusion criteria from SCOPE.md to filter.
        7. Extract a value for each comparison dimension. If SCOPE.md defines
           ranking criteria with a scoring rubric, compute each score per that
           rubric.
        8. Write {workspace}/{topic_path}.md using the template below.
        9. For each sibling topic your work connects to (same method family,
           builds on, contrasts with, shared dataset, etc.), append one line to
           THAT file's `## Related Topics` section describing the relationship
           and linking back to your file. Create `## Related Topics` in that
           file if it does not yet exist.

        OUTPUT TEMPLATE:

        ```markdown
        ---
        title: "<Narrow Topic Title>"
        tags: ["<theme>", "<topic>"]
        status: draft
        round: {round_num}
        ---

        # <Narrow Topic Title>

        > Workspace context:
        > [SCOPE]({workspace_rel}SCOPE.md) -
        > [TASKS]({workspace_rel}TASKS.md) -
        > [SUMMARY]({workspace_rel}SUMMARY.md)

        ## Summary
        <2-3 sentence summary of findings>

        ## Key Findings
        - <finding with inline citation>

        ## Comparison Data
        | Dimension | Value |
        | --------- | ----- |
        | <dim from SCOPE.md> | <extracted value> |

        ## Ranking Scores
        (Only if SCOPE.md defines ranking criteria. Use the scoring rubric there.)
        | Factor | Score | Justification |
        | ------ | ----- | ------------- |
        | <factor> | <0-1> | <why per rubric> |
        | Composite | <weighted sum> | |

        ## Related Topics
        (Sibling topic files you connect to. Relative paths.
        One sentence per link.)
        - [<relative/path/to/sibling>](../<path>.md) -- <relationship>

        ## Open Questions
        - ...

        ## Sources
        Canonical links for the primary works cited.
        - **<Short name / first-author year>**
          - Paper: <DOI or arXiv URL>
          - Project page: <URL or "none">
          - Code: <GitHub URL or "none">
          - Dataset: <URL or "none">
          - Notes: <1 sentence>

        ## References
        - [Author et al., Year. Title](https://doi.org/...)

        ## Proposed Additions (optional)
        <Only include if you found a dimension, criterion, or topic that should
        extend SCOPE.md.>
        ```

        QUALITY BAR:
        - At least 3 distinct, real sources with working links
        - Every comparison dimension must have a value (or explicit
          "not applicable" with reason)
        - If SCOPE.md has ranking criteria, `## Ranking Scores` is required
        - Each cited source appears in both `## Sources` and `## References`
        - Workspace-context links at the top use the paths above and resolve
        - At least one `## Related Topics` entry if any sibling shares method
          family, dataset, direct citation, or other clear link

        When finished (including back-link updates to sibling files), print
        DONE on a line by itself.
    """)


def run_manager(
    workspace: Path,
    round_num: int,
    phase: str,
    timeout: int,
    backend: AgentBackend,
) -> bool:
    """Run a single manager invocation in a dedicated tmux session.

    Returns True if the manager completed within the timeout.
    """
    prompt = build_manager_prompt(workspace, round_num, phase)
    protected_paths = [
        workspace / "SLR_STATE.json",
        workspace / "SCOPE.md",
        workspace / "SCOPE_ORIGINAL.md",
        workspace / "artifacts" / "ISSUES.jsonl",
    ]
    if phase == "review":
        protected_paths.append(workspace / "TASKS.md")
    protected = {
        path: path.read_bytes() if path.is_file() else None for path in protected_paths
    }
    command = backend.build_command("slr-manager", prompt, cwd=workspace)
    status_path = _agent_status_path(workspace, f"manager-round-{round_num}-{phase}")

    spec = WorkerSpec(
        window_name="manager",
        command=command,
        done_channel=f"manager-round-{round_num}-{phase}-done",
        cwd=workspace,
        exit_status_path=status_path,
    )
    spawn_workers(MANAGER_SESSION, [spec])
    results = wait_for_all([spec], timeout=timeout)
    completed = results[spec.window_name]
    output = capture_pane(MANAGER_SESSION, spec.window_name)
    kill_session(MANAGER_SESSION)
    _restore_protected_paths(protected)
    exit_code = _read_agent_exit_code(status_path)
    if not completed or exit_code != 0:
        print(
            f"[round {round_num}] Manager {phase} failed "
            f"(completed={completed}, exit_code={exit_code})."
        )
        if output.strip():
            print(output.rstrip())
    _write_agent_run_record(
        workspace,
        f"manager-round-{round_num}-{phase}",
        "slr-manager",
        prompt,
        timeout,
        completed,
        exit_code,
        output,
    )
    return completed and exit_code == 0


def run_named_agent(
    workspace: Path,
    agent_name: str,
    prompt: str,
    run_name: str,
    timeout: int,
    backend: AgentBackend,
) -> bool:
    """Run one named top-level agent with captured status and bounded waiting."""
    protected_paths = [
        workspace / "SLR_STATE.json",
        workspace / "TASKS.md",
        workspace / "SCOPE.md",
        workspace / "SCOPE_ORIGINAL.md",
        workspace / "artifacts" / "ISSUES.jsonl",
    ]
    protected = {
        path: path.read_bytes() if path.is_file() else None for path in protected_paths
    }
    command = backend.build_command(agent_name, prompt, cwd=workspace)
    status_path = _agent_status_path(workspace, run_name)
    spec = WorkerSpec(
        window_name=run_name[:40],
        command=command,
        done_channel=f"{run_name}-done",
        cwd=workspace,
        exit_status_path=status_path,
    )
    spawn_workers(MANAGER_SESSION, [spec])
    completed = wait_for_all([spec], timeout=timeout)[spec.window_name]
    output = capture_pane(MANAGER_SESSION, spec.window_name)
    kill_session(MANAGER_SESSION)
    _restore_protected_paths(protected)
    exit_code = _read_agent_exit_code(status_path)
    if not completed or exit_code != 0:
        print(
            f"Agent {agent_name} failed (completed={completed}, exit_code={exit_code})."
        )
        if output.strip():
            print(output.rstrip())
    _write_agent_run_record(
        workspace,
        run_name,
        agent_name,
        prompt,
        timeout,
        completed,
        exit_code,
        output,
    )
    return completed and exit_code == 0


def _restore_protected_paths(protected: dict[Path, bytes | None]) -> None:
    """Undo any attempted Agent write to program-owned state/scope files."""
    for path, original in protected.items():
        if original is None:
            path.unlink(missing_ok=True)
        elif not path.is_file() or path.read_bytes() != original:
            path.write_bytes(original)


def run_workers(
    workspace: Path,
    tasks: list[Task],
    round_num: int,
    timeout: int,
    backend: AgentBackend,
) -> dict[str, bool]:
    """Run workers in parallel, one tmux window per task.

    Returns {topic_path: completed?} for each task.
    """
    if not tasks:
        return {}

    specs = []
    topic_to_window = {}
    for i, task in enumerate(tasks):
        window = f"worker-{i + 1}"
        prompt = build_worker_prompt(
            workspace, task.topic_path, task.description, round_num
        )
        command = backend.build_command("slr-worker", prompt, cwd=workspace)
        status_path = _agent_status_path(workspace, f"worker-round-{round_num}-{i + 1}")
        specs.append(
            WorkerSpec(
                window_name=window,
                command=command,
                done_channel=f"worker-round-{round_num}-{i + 1}-done",
                cwd=workspace,
                exit_status_path=status_path,
            )
        )
        topic_to_window[task.topic_path] = window

    spawn_workers(WORKER_SESSION, specs)
    results = wait_for_all(specs, timeout=timeout)
    exit_codes = {
        spec.window_name: _read_agent_exit_code(spec.exit_status_path) for spec in specs
    }
    kill_session(WORKER_SESSION)

    return {
        topic: results[window] and exit_codes[window] == 0
        for topic, window in topic_to_window.items()
    }


CoordinatorBatchExecutor = Callable[
    [Path, list[CoordinatorInvocation], int],
    dict[str, CoordinatorProcessResult],
]


def _execute_coordinator_batch(
    workspace: Path,
    invocations: list[CoordinatorInvocation],
    timeout: int,
) -> dict[str, CoordinatorProcessResult]:
    """Run one top-level coordinator process per topic in parallel via tmux."""
    if not invocations:
        return {}
    specs: list[WorkerSpec] = []
    by_window: dict[str, CoordinatorInvocation] = {}
    for index, invocation in enumerate(invocations, start=1):
        window = f"topic-{index}"
        task_id = topic_paths(workspace, invocation.task.topic_path).task_id
        status_path = _agent_status_path(
            workspace,
            f"coordinator-{task_id}-attempt-{invocation.attempt}",
        )
        spec = WorkerSpec(
            window_name=window,
            command=invocation.command,
            done_channel=f"coordinator-{task_id}-{invocation.attempt}-done",
            cwd=workspace,
            exit_status_path=status_path,
        )
        specs.append(spec)
        by_window[window] = invocation

    spawn_workers(COORDINATOR_SESSION, specs)
    pids: dict[str, int | None] = {}
    for spec in specs:
        invocation = by_window[spec.window_name]
        paths = topic_paths(workspace, invocation.task.topic_path)
        pid = get_pane_pid(COORDINATOR_SESSION, spec.window_name)
        pids[spec.window_name] = pid
        state = json.loads(paths.task_state.read_text(encoding="utf-8"))
        record_topic_process(paths, state, COORDINATOR_SESSION, spec.window_name, pid)

    completed = wait_for_all(specs, timeout=timeout)
    results: dict[str, CoordinatorProcessResult] = {}
    for spec in specs:
        invocation = by_window[spec.window_name]
        output = capture_pane(COORDINATOR_SESSION, spec.window_name)
        exit_code = _read_agent_exit_code(spec.exit_status_path)
        result = CoordinatorProcessResult(
            completed=completed[spec.window_name],
            exit_code=exit_code,
            timed_out=not completed[spec.window_name],
            output=output,
            pid=pids[spec.window_name],
        )
        results[invocation.task.topic_path] = result
        _write_agent_run_record(
            workspace,
            f"coordinator-{invocation.task.topic_path.replace('/', '--')}-"
            f"attempt-{invocation.attempt}",
            "topic-coordinator",
            invocation.prompt,
            timeout,
            result.completed,
            exit_code,
            output,
        )
    kill_session(COORDINATOR_SESSION)
    return results


def _read_topic_state(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _run_legacy_topic_coordinators(
    workspace: Path,
    tasks: list[Task],
    round_num: int,
    config: TopicExecutionConfig,
    backend: AgentBackend,
    batch_executor: CoordinatorBatchExecutor = _execute_coordinator_batch,
) -> dict[str, bool]:
    """Run, validate, retry, and resume deterministic topic coordinators."""
    results = {task.topic_path: False for task in tasks}
    pending: dict[str, tuple[Task, int, list[str]]] = {}

    for task in tasks:
        paths = topic_paths(workspace, task.topic_path)
        prior = _read_topic_state(paths.task_state)
        prior_status = prior.get("status") if prior else None
        if prior_status in {"COMPLETE", "PARTIAL"}:
            compile_topic_artifacts(workspace, paths, config, None)
            validation = validate_coordinator_outputs(workspace, paths, config)
            if validation.accepted:
                results[task.topic_path] = True
                continue
        previous_attempt = prior.get("attempt", 0) if prior else 0
        if not isinstance(previous_attempt, int):
            previous_attempt = 0
        diagnostics = prior.get("diagnostics", []) if prior else []
        if not isinstance(diagnostics, list):
            diagnostics = []
        pending[task.topic_path] = (
            task,
            previous_attempt + 1,
            [str(item) for item in diagnostics],
        )

    for retry_index in range(config.coordinator_retries + 1):
        if not pending:
            break
        invocations: list[CoordinatorInvocation] = []
        states: dict[str, dict[str, object]] = {}
        for topic_path, (task, attempt, diagnostics) in pending.items():
            paths = topic_paths(workspace, topic_path)
            state = initialize_topic_attempt(
                paths,
                task.description,
                round_num,
                attempt,
                config,
                diagnostics,
                task.research_line_id,
                task.prioritization_warnings,
            )
            states[topic_path] = state
            prompt = build_coordinator_prompt(
                workspace,
                paths,
                task.description,
                round_num,
                attempt,
                config,
                diagnostics,
                task.research_line_id,
                task.prioritization_warnings,
            )
            invocations.append(
                CoordinatorInvocation(
                    task=task,
                    attempt=attempt,
                    command=backend.build_command(
                        config.coordinator_agent, prompt, cwd=workspace
                    ),
                    prompt=prompt,
                )
            )

        process_results = batch_executor(
            workspace, invocations, config.coordinator_timeout_seconds
        )
        next_pending: dict[str, tuple[Task, int, list[str]]] = {}
        for invocation in invocations:
            task = invocation.task
            paths = topic_paths(workspace, task.topic_path)
            process = process_results.get(task.topic_path)
            diagnostics: list[str] = []
            validation = None
            if process is None:
                diagnostics.append("Coordinator executor returned no process result")
                compilation_process = CoordinatorProcessResult(False, None, False)
            else:
                compilation_process = process
                if process.timed_out or not process.completed:
                    diagnostics.append("Coordinator process timed out")
                elif process.exit_code != 0:
                    diagnostics.append(
                        f"Coordinator exited with status {process.exit_code}"
                    )
            compilation = compile_topic_artifacts(
                workspace, paths, config, compilation_process
            )
            diagnostics.extend(compilation.errors)
            diagnostics.extend(compilation.warnings)
            validation = validate_coordinator_outputs(workspace, paths, config)
            diagnostics.extend(validation.errors)
            diagnostics.extend(validation.warnings)

            accepted = validation is not None and validation.accepted
            if not accepted:
                diagnostics.append(
                    "Checkpoint inventory: "
                    + json.dumps(compilation.checkpoint, ensure_ascii=False)
                )
            status = validation.effective_status if accepted else "FAILED"
            state = states[task.topic_path]
            if process:
                state = record_topic_process(
                    paths,
                    state,
                    COORDINATOR_SESSION,
                    f"topic-{invocations.index(invocation) + 1}",
                    process.pid,
                )
            finish_topic_attempt(
                paths,
                state,
                status,
                process.exit_code if process else None,
                process.timed_out if process else False,
                diagnostics,
            )
            if accepted:
                results[task.topic_path] = True
            elif retry_index < config.coordinator_retries:
                next_pending[task.topic_path] = (
                    task,
                    invocation.attempt + 1,
                    diagnostics,
                )
        pending = next_pending

    return results


def _agent_status_path(workspace: Path, run_name: str) -> Path:
    """Return a per-invocation exit-status path inside the workspace git dir."""
    run_dir = workspace / ".git" / "slrharness-runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / f"{run_name}.status"
    status_path.unlink(missing_ok=True)
    return status_path


def _read_agent_exit_code(path: Path | None) -> int | None:
    """Read an agent exit code recorded by the tmux command wrapper."""
    if path is None or not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _write_agent_run_record(
    workspace: Path,
    run_name: str,
    agent_name: str,
    prompt: str,
    timeout: int,
    completed: bool,
    exit_code: int | None,
    output: str,
) -> None:
    """Persist bounded, secret-minimizing invocation metadata under .git/."""
    path = workspace / ".git" / "slrharness-runs" / f"{run_name}.json"
    atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "run_name": run_name,
            "agent": agent_name,
            "recorded_at": datetime.now(UTC).isoformat(),
            "timeout_seconds": timeout,
            "completed_signal": completed,
            "exit_code": exit_code,
            "artifact_validation_passed": None,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_characters": len(prompt),
            "output_tail": re.sub(
                r"(?i)(?:api[_-]?key|token|authorization)\s*[:=]\s*\S+",
                "[REDACTED]",
                output[-20000:],
            ),
        },
    )


def print_status(workspace: Path) -> None:
    """Print a concise status summary of the workspace."""
    if not (workspace / ".git").is_dir():
        sys.exit(f"Not a workspace (no .git): {workspace}")

    tasks_md = workspace / "TASKS.md"
    summary_md = workspace / "SUMMARY.md"
    topics_dir = workspace / "topics"

    last_round = current_round(workspace)
    pending = parse_pending_tasks(tasks_md) if tasks_md.is_file() else []
    completed = count_completed_tasks(tasks_md)
    topic_files = (
        [
            p
            for p in topics_dir.rglob("*.md")
            if p.name != "_index.md"
            and "papers" not in p.parts
            and "technical_sources" not in p.parts
            and "audits" not in p.parts
        ]
        if topics_dir.is_dir()
        else []
    )
    clean = is_workspace_clean(workspace)

    print(f"Workspace: {workspace}")
    if state_path(workspace).is_file():
        scope_state = load_scope_state(workspace)
        print(f"  Scope status: {scope_state.get('status')}")
        print(f"  Scope revision: {scope_state.get('revision')}")
        final_state = scope_state.get("finalization")
        if isinstance(final_state, dict):
            print(f"  Finalization phase: {final_state.get('phase')}")
            print(f"  Repair rounds used: {final_state.get('repair_rounds_used', 0)}")
    print(f"  Last completed round: {last_round}")
    print(f"  Next round (if you run): {last_round + 1}")
    print(f"  Git working tree: {'clean' if clean else 'DIRTY (uncommitted changes)'}")
    print(f"  Tasks pending:   {len(pending)}")
    print(f"  Tasks completed: {completed}")
    print(f"  Topic files:     {len(topic_files)}")
    print(
        f"  SUMMARY.md size: "
        f"{summary_md.stat().st_size if summary_md.is_file() else 0} bytes"
    )
    print()
    print("Recent git history:")
    result = subprocess.run(
        ["git", "log", "--oneline", "-n", "10"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        print(f"  {line}")


def _git_commit_leftovers(
    workspace: Path, round_num: int, phase: str, reason: str
) -> None:
    """Commit any uncommitted changes as a recovery commit.

    Called after a manager phase when the tree is dirty — which means the
    agent edited files but did not commit. We never want to lose work or
    carry dirty state between rounds, so we commit the leftovers under a
    clearly-labeled recovery message.
    """
    subprocess.run(
        ["git", "add", "-A"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"round-{round_num}-{phase}-recovery: {reason}",
        ],
        cwd=workspace,
        capture_output=True,
        check=False,
    )


def _verify_phase_committed(
    workspace: Path, round_num: int, phase: str, agent_ok: bool
) -> bool:
    """Ensure the workspace is clean after a manager phase.

    Returns True if the phase is in a committable state (tree clean, or
    recovery commit succeeded). Returns False only when the agent timed out
    AND left no work behind (nothing to salvage).
    """
    if is_workspace_clean(workspace):
        # Clean tree: phase is fine if the agent finished, bad if it timed out
        # (a timeout with no work done means no progress possible).
        return agent_ok

    # Tree is dirty — agent changed files but didn't commit.
    reason = (
        "agent did not commit after completing phase"
        if agent_ok
        else "agent timed out mid-phase; salvaging partial work"
    )
    print(
        f"[round {round_num}] WARNING: {phase} pass left uncommitted changes. "
        f"Creating recovery commit."
    )
    _git_commit_leftovers(workspace, round_num, phase, reason)

    # Recovery commit should have made the tree clean.
    if not is_workspace_clean(workspace):
        print(f"[round {round_num}] ERROR: recovery commit failed; tree still dirty.")
        return False

    # If the agent timed out we still want to stop, even though we salvaged
    # the work — a timeout means something is wrong that needs human review.
    return agent_ok


def _commit_finalization_artifacts(workspace: Path, message: str) -> None:
    """Commit program-owned state/audits so every resume starts cleanly."""
    if is_workspace_clean(workspace):
        return
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=workspace, check=True)


def _run_repair_tasks(
    workspace: Path,
    tasks: list[Task],
    round_num: int,
    num_workers: int,
    topic_config: TopicExecutionConfig,
    backend: AgentBackend,
) -> dict[str, bool]:
    """Execute bounded repair tasks through the existing coordinator pipeline."""
    results: dict[str, bool] = {}
    repair_config = replace(topic_config, mode=TOPIC_COORDINATOR)
    for start in range(0, len(tasks), num_workers):
        batch = tasks[start : start + num_workers]
        results.update(
            run_topic_coordinators(workspace, batch, round_num, repair_config, backend)
        )
    return results


def _run_program_stage_batch(
    workspace: Path,
    invocations: list[tuple[str, str, str, int]],
    backend: AgentBackend,
) -> dict[str, bool]:
    """Run independent program-scheduled roles concurrently in tmux."""
    if not invocations:
        return {}
    protected_paths = [
        workspace / "SLR_STATE.json",
        workspace / "TASKS.md",
        workspace / "SCOPE.md",
        workspace / "SCOPE_ORIGINAL.md",
        workspace / "artifacts" / "ISSUES.jsonl",
    ]
    protected = {
        path: path.read_bytes() if path.is_file() else None for path in protected_paths
    }
    specs: list[WorkerSpec] = []
    for index, (invocation_id, role, prompt, _timeout) in enumerate(invocations, 1):
        window = f"stage-{index}"
        status_path = _agent_status_path(workspace, invocation_id)
        specs.append(
            WorkerSpec(
                window_name=window,
                command=backend.build_command(role, prompt, cwd=workspace),
                done_channel=f"{invocation_id}-done",
                cwd=workspace,
                exit_status_path=status_path,
            )
        )
    timeout = max(value[3] for value in invocations)
    spawn_workers(COORDINATOR_SESSION, specs)
    completed = wait_for_all(specs, timeout=timeout)
    results: dict[str, bool] = {}
    for spec, values in zip(specs, invocations, strict=True):
        invocation_id, role, prompt, role_timeout = values
        exit_code = _read_agent_exit_code(spec.exit_status_path)
        output = capture_pane(COORDINATOR_SESSION, spec.window_name)
        ok = completed[spec.window_name] and exit_code == 0
        results[invocation_id] = ok
        _write_agent_run_record(
            workspace,
            invocation_id,
            role,
            prompt,
            role_timeout,
            completed[spec.window_name],
            exit_code,
            output,
        )
    kill_session(COORDINATOR_SESSION)
    _restore_protected_paths(protected)
    return results


def _record_stage_invocation(
    workspace: Path,
    task_id: str,
    invocation_id: str,
    role: str,
    stage: str,
    attempt: int,
    staging: Path,
    status: str,
    diagnostic: str | None = None,
) -> None:
    prior = None
    try:
        prior = json.loads((workspace / "SLR_STATE.json").read_text(encoding="utf-8"))["control"]["invocations"].get(invocation_id)
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    record_invocation(
        workspace,
        AgentInvocation(
            invocation_id=invocation_id,
            task_id=task_id,
            role=role,
            stage=stage,
            attempt=attempt,
            status=status,
            staging_path=staging.resolve().relative_to(workspace.resolve()).as_posix(),
            started_at=(prior or {}).get("started_at") or control_now(),
            completed_at=control_now() if status in {"COMPLETE", "FAILED"} else None,
            diagnostic=diagnostic,
        ),
    )


def _run_program_topic(
    workspace: Path,
    task: Task,
    round_num: int,
    config: TopicExecutionConfig,
    backend: AgentBackend,
) -> bool:
    paths = topic_paths(workspace, task.topic_path)
    records = {item["task_id"]: item for item in state_topic_tasks(workspace)}
    record = records[paths.task_id]
    if record.get("status") in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
        return True
    attempt = int(record.get("attempt", 0)) + 1
    update_topic_task(
        workspace, paths.task_id, attempt=attempt, status="RUNNING", stage="RESEARCH"
    )

    # The two retrieval roles are the only deliberately concurrent topic stages.
    role_inputs: list[tuple[str, str, str, int]] = []
    role_staging: dict[str, Path] = {}
    import_diagnostics: list[str] = []
    resume_stage = str(record.get("stage") or "PLANNED")
    academic_done = paths.paper_dir.is_dir() and any(paths.paper_dir.glob("*.md"))
    technical_done = paths.technical_dir.is_dir() and any(
        paths.technical_dir.glob("*.md")
    )
    retrieval_roles = tuple(
        item
        for item in (
            ("academic", config.academic_agent, config.academic_max_turns),
            ("technical", config.technical_agent, config.technical_max_turns),
        )
        if not (academic_done if item[0] == "academic" else technical_done)
    )
    for label, role, _turns in retrieval_roles:
        invocation_id = f"{paths.task_id}-{label}-{attempt}"
        staging = staging_directory(workspace, invocation_id)
        role_staging[label] = staging
        prompt = build_stage_prompt(
            workspace, paths, role, staging, task.description, config
        )
        _record_stage_invocation(
            workspace, paths.task_id, invocation_id, role, "RESEARCH", attempt,
            staging, "RUNNING"
        )
        role_inputs.append((invocation_id, role, prompt, config.coordinator_timeout_seconds))
    stage_results = _run_program_stage_batch(workspace, role_inputs, backend)
    for label, role, _turns in retrieval_roles:
        invocation_id = f"{paths.task_id}-{label}-{attempt}"
        imported = import_staged_notes(
            workspace,
            paths,
            role_staging[label] / ("papers" if label == "academic" else "technical"),
            kind="paper" if label == "academic" else "technical",
            diagnostics=import_diagnostics,
        ) if (role_staging[label] / ("papers" if label == "academic" else "technical")).is_dir() else []
        ok = stage_results.get(invocation_id, False) or bool(imported)
        _record_stage_invocation(
            workspace, paths.task_id, invocation_id, role, "RESEARCH", attempt,
            role_staging[label], "COMPLETE" if ok else "FAILED",
            None if ok else "agent failed and produced no importable notes"
        )
    if retrieval_roles:
        update_topic_task(
            workspace,
            paths.task_id,
            stage="NOTES_NORMALIZED",
            diagnostics=import_diagnostics,
        )

    invocation_records = json.loads(
        (workspace / "SLR_STATE.json").read_text(encoding="utf-8")
    ).get("control", {}).get("invocations", {})
    metadata_done = any(
        isinstance(value, dict)
        and value.get("task_id") == paths.task_id
        and value.get("role") == config.metadata_agent
        and value.get("status") == "COMPLETE"
        for value in invocation_records.values()
    ) if isinstance(invocation_records, dict) else False
    prefix = paths.paper_dir.resolve().relative_to(workspace.resolve()).as_posix()
    topic_open_issues = [
        issue for issue in current_issues(workspace).values()
        if str(issue.get("target", "")).startswith(prefix)
        and issue.get("status") != "verified_closed"
    ]
    if (
        (not metadata_done or topic_open_issues)
        and any(paths.paper_dir.glob("*.md"))
        and not (paths.paper_dir / "NO_RESULTS.md").is_file()
    ):
        checker_id = f"{paths.task_id}-metadata-{attempt}"
        checker_staging = staging_directory(workspace, checker_id)
        checker_prompt = build_stage_prompt(
            workspace, paths, config.metadata_agent, checker_staging,
            task.description, config
        )
        _record_stage_invocation(
            workspace, paths.task_id, checker_id, config.metadata_agent,
            "METADATA_CHECK", attempt, checker_staging, "RUNNING"
        )
        checker_ok = _run_program_stage_batch(
            workspace,
            [(checker_id, config.metadata_agent, checker_prompt, config.coordinator_timeout_seconds)],
            backend,
        ).get(checker_id, False)
        observations = load_observations(checker_staging / "metadata_observations.jsonl")
        for observation in observations:
            try:
                ingest_metadata_observation(workspace, observation, checker_id)
                apply_verification_observation(workspace, observation)
            except ValueError:
                # Malformed checker rows must not abort topic finalization.
                continue
        _record_stage_invocation(
            workspace, paths.task_id, checker_id, config.metadata_agent,
            "METADATA_CHECK", attempt, checker_staging,
            "COMPLETE" if checker_ok and observations else "FAILED",
            None if observations else "checker produced no observations"
        )

        corrections_used = int(record.get("correction_rounds_used", 0))
        for correction_round in range(
            corrections_used + 1, config.max_correction_rounds + 1
        ):
            repair_issues = [
                issue for issue in current_issues(workspace).values()
                if str(issue.get("target", "")).startswith(prefix)
                and issue.get("status") in {"open", "unresolved", "repair_dispatched"}
            ]
            applied_issues = [
                issue for issue in current_issues(workspace).values()
                if str(issue.get("target", "")).startswith(prefix)
                and issue.get("status") == "applied"
            ]
            if not repair_issues and not applied_issues:
                break
            update_topic_task(
                workspace,
                paths.task_id,
                correction_rounds_used=correction_round,
            )
            # Program-first repairs: if the checker already verified a value,
            # apply it deterministically. Only leftover issues need an agent.
            leftover_repairs: list[dict] = []
            for issue in repair_issues:
                field_name = str(issue.get("field") or "")
                verified = (issue.get("evidence") or {}).get("verified")
                target = workspace / str(issue.get("target") or "")
                if (
                    field_name
                    and verified not in (None, "")
                    and target.is_file()
                    and str(issue.get("target", "")).startswith(prefix)
                ):
                    if write_frontmatter_field(target, field_name, verified):
                        issue_id = str(issue["issue_id"])
                        # Legal path: open/unresolved -> repair_dispatched -> applied
                        if issue.get("status") != "repair_dispatched":
                            transition_issue(
                                workspace, issue_id, "repair_dispatched"
                            )
                        transition_issue(
                            workspace, issue_id, "applied",
                            applied_by_invocation=f"program-verified-{issue_id}"
                        )
                        _record_stage_invocation(
                            workspace, paths.task_id,
                            f"{paths.task_id}-program-repair-{issue['issue_id']}",
                            config.academic_agent, "METADATA_FINISHED", attempt,
                            staging_directory(
                                workspace,
                                f"{paths.task_id}-program-repair-{issue['issue_id']}",
                            ),
                            "COMPLETE",
                        )
                        continue
                leftover_repairs.append(issue)

            # Cap agent-mediated repairs to avoid one-call-per-field storms.
            max_agent_repairs = max(0, int(getattr(config, "max_metadata_repairs", 6)))
            if len(leftover_repairs) > max_agent_repairs:
                for issue in leftover_repairs[max_agent_repairs:]:
                    transition_issue(
                        workspace, str(issue["issue_id"]), "unresolved",
                        diagnostic="skipped agent repair: per-topic repair cap reached",
                    )
                leftover_repairs = leftover_repairs[:max_agent_repairs]

            for issue in leftover_repairs:
                if issue.get("status") != "repair_dispatched":
                    transition_issue(
                        workspace, str(issue["issue_id"]), "repair_dispatched"
                    )
                repair_id = f"{paths.task_id}-repair-{issue['issue_id']}-{correction_round}"
                repair_staging = staging_directory(workspace, repair_id)
                target = workspace / str(issue["target"])
                before_meta, before_body = parse_frontmatter(target)
                prompt = build_stage_prompt(
                    workspace, paths, config.academic_agent, repair_staging,
                    task.description, config, issue=issue
                )
                _record_stage_invocation(
                    workspace, paths.task_id, repair_id, config.academic_agent,
                    "METADATA_FINISHED", attempt, repair_staging, "RUNNING"
                )
                repair_ok = _run_program_stage_batch(
                    workspace,
                    [(repair_id, config.academic_agent, prompt, config.coordinator_timeout_seconds)],
                    backend,
                ).get(repair_id, False)
                after_meta, after_body = parse_frontmatter(target)
                changed_fields = {
                    key for key in set(before_meta) | set(after_meta)
                    if before_meta.get(key) != after_meta.get(key)
                }
                expected = {str(issue["field"])}
                verified = (issue.get("evidence") or {}).get("verified")
                actual = after_meta.get(str(issue["field"]))
                field_name = str(issue["field"])
                if field_name == "doi":
                    value_matches = normalize_doi(actual) == normalize_doi(verified)
                elif field_name == "arxiv_id":
                    value_matches = normalize_arxiv(actual) == normalize_arxiv(verified)
                else:
                    value_matches = re.sub(r"\s+", " ", str(actual).strip()).casefold() == re.sub(
                        r"\s+", " ", str(verified).strip()
                    ).casefold()
                if (
                    repair_ok
                    and changed_fields == expected
                    and before_body == after_body
                    and value_matches
                ):
                    transition_issue(
                        workspace, str(issue["issue_id"]), "applied",
                        applied_by_invocation=repair_id
                    )
                    repair_status = "COMPLETE"
                else:
                    transition_issue(
                        workspace, str(issue["issue_id"]), "unresolved",
                        diagnostic="repair did not change exactly the allowed field"
                    )
                    repair_status = "FAILED"
                _record_stage_invocation(
                    workspace, paths.task_id, repair_id, config.academic_agent,
                    "METADATA_FINISHED", attempt, repair_staging, repair_status
                )
            recheck_id = f"{paths.task_id}-metadata-recheck-{attempt}-{correction_round}"
            recheck_staging = staging_directory(workspace, recheck_id)
            prompt = build_stage_prompt(
                workspace, paths, config.metadata_agent, recheck_staging,
                task.description, config
            )
            _record_stage_invocation(
                workspace, paths.task_id, recheck_id, config.metadata_agent,
                "METADATA_FINISHED", attempt, recheck_staging, "RUNNING"
            )
            recheck_ok = _run_program_stage_batch(
                workspace,
                [(recheck_id, config.metadata_agent, prompt, config.coordinator_timeout_seconds)],
                backend,
            )
            recheck_observations = load_observations(
                recheck_staging / "metadata_observations.jsonl"
            )
            for observation in recheck_observations:
                apply_verification_observation(workspace, observation)
            _record_stage_invocation(
                workspace, paths.task_id, recheck_id, config.metadata_agent,
                "METADATA_FINISHED", attempt, recheck_staging,
                "COMPLETE" if recheck_ok and recheck_observations else "FAILED"
            )
    if resume_stage not in {"METADATA_FINISHED", "TERMINAL"}:
        update_topic_task(workspace, paths.task_id, stage="METADATA_FINISHED")

    synthesis_valid = paths.synthesis.is_file() and len(
        paths.synthesis.read_text(encoding="utf-8").strip()
    ) >= 200
    synthesis_ok = synthesis_valid
    if not synthesis_valid:
        synthesis_id = f"{paths.task_id}-synthesis-{attempt}"
        synthesis_staging = staging_directory(workspace, synthesis_id)
        synthesis_prompt = build_stage_prompt(
            workspace, paths, config.coordinator_agent, synthesis_staging,
            task.description, config
        )
        update_topic_task(workspace, paths.task_id, stage="SYNTHESIS")
        _record_stage_invocation(
            workspace, paths.task_id, synthesis_id, config.coordinator_agent,
            "SYNTHESIS", attempt, synthesis_staging, "RUNNING"
        )
        synthesis_ok = _run_program_stage_batch(
            workspace,
            [(synthesis_id, config.coordinator_agent, synthesis_prompt, config.coordinator_timeout_seconds)],
            backend,
        ).get(synthesis_id, False)
        _record_stage_invocation(
            workspace, paths.task_id, synthesis_id, config.coordinator_agent,
            "SYNTHESIS", attempt, synthesis_staging,
            "COMPLETE" if synthesis_ok else "FAILED"
        )
    status, diagnostics = derive_topic_status(workspace, paths)
    diagnostics = [
        *(str(value) for value in record.get("diagnostics", [])),
        *import_diagnostics,
        *diagnostics,
    ]
    if not synthesis_ok and status == "COMPLETE":
        status = "COMPLETE_WITH_WARNINGS"
        diagnostics.append("synthesizer process failed after producing valid synthesis")
    update_topic_task(
        workspace, paths.task_id, stage="TERMINAL", status=status,
        diagnostics=diagnostics
    )
    return status in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}


def run_topic_coordinators(
    workspace: Path,
    tasks: list[Task],
    round_num: int,
    config: TopicExecutionConfig,
    backend: AgentBackend,
    batch_executor: CoordinatorBatchExecutor = _execute_coordinator_batch,
) -> dict[str, bool]:
    """Run program-owned stages; retain injected legacy executor for old tests/tools."""
    if batch_executor is not _execute_coordinator_batch:
        return _run_legacy_topic_coordinators(
            workspace, tasks, round_num, config, backend, batch_executor
        )
    register_tasks(workspace, tasks, round_num)
    return {
        task.topic_path: _run_program_topic(
            workspace, task, round_num, config, backend
        )
        for task in tasks
    }


def _load_prefinal_json(workspace: Path) -> dict[str, object] | None:
    try:
        value = json.loads(
            (workspace / PREFINAL_AUDIT_PATH).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_finalization_pipeline(
    workspace: Path,
    round_num: int,
    num_workers: int,
    manager_timeout: int,
    backend: AgentBackend,
    topic_config: TopicExecutionConfig,
    config: FinalizationConfig,
) -> bool:
    """Aggregate, audit, optionally repair once, finalize, and validate."""
    if not config.enabled:
        return True
    deploy_claude_assets(workspace, overwrite=False)
    prior = finalization_state(workspace)
    if prior.get("phase") == "COMPLETE":
        prior_audit = _load_prefinal_json(workspace) or {}
        if collect_blocking_gaps(prior_audit):
            update_finalization_state(
                workspace,
                "AWAITING_INTERVENTION",
                config,
                failure=blocking_gap_summary(prior_audit),
            )
            return False
        validation = validate_final_report(workspace, config)
        if validation.accepted:
            print("Final report is already complete; resume skipped regeneration.")
            _commit_finalization_artifacts(
                workspace, "finalization: refresh deterministic final audit"
            )
            return True

    update_finalization_state(
        workspace, "TOPIC_RESEARCH_COMPLETE", config, last_round=round_num
    )
    registry, audit = aggregate_and_audit(workspace, config)
    _commit_finalization_artifacts(workspace, "finalization: aggregate sources")

    if config.enable_prefinal_audit:
        deterministic_audit = audit
        semantic_ok = run_named_agent(
            workspace,
            config.finalizer_agent,
            build_prefinal_prompt(workspace),
            "manager-prefinal-audit",
            manager_timeout,
            backend,
        )
        semantic_audit = _load_prefinal_json(workspace)
        if not semantic_ok or not isinstance(semantic_audit, dict):
            update_finalization_state(
                workspace,
                "AWAITING_INTERVENTION",
                config,
                failure="pre-final audit agent failed or wrote invalid JSON",
            )
            _commit_finalization_artifacts(
                workspace, "finalization: record pre-final audit failure"
            )
            return False
        audit = merge_semantic_prefinal_audit(
            workspace, deterministic_audit, semantic_audit
        )
        audit_ok, audit_errors = validate_prefinal_audit(workspace)
        if not audit_ok:
            update_finalization_state(
                workspace, "AWAITING_INTERVENTION", config,
                failure="; ".join(audit_errors),
            )
            return False
        _commit_finalization_artifacts(workspace, "finalization: pre-final audit")

    final_state = finalization_state(workspace)
    repairs_used = int(final_state.get("repair_rounds_used", 0))
    if (
        audit.get("repair_required")
        and repairs_used < config.max_prefinal_repair_rounds
    ):
        update_finalization_state(workspace, "GAP_REPAIR_PLANNED", config)
        repair_plan_ok = run_named_agent(
            workspace,
            config.finalizer_agent,
            build_repair_plan_prompt(workspace, round_num + 1),
            "manager-gap-repair-plan",
            manager_timeout,
            backend,
        )
        if not repair_plan_ok:
            update_finalization_state(
                workspace,
                "AWAITING_INTERVENTION",
                config,
                failure="gap-repair planning failed",
            )
            _commit_finalization_artifacts(
                workspace, "finalization: record repair-plan failure"
            )
            return False
        ensure_stable_repair_tasks(workspace, audit)
        if not _verify_phase_committed(
            workspace, round_num + 1, "gap-repair-plan", repair_plan_ok
        ):
            update_finalization_state(
                workspace,
                "AWAITING_INTERVENTION",
                config,
                failure="gap-repair plan was not committed",
            )
            _commit_finalization_artifacts(
                workspace, "finalization: record uncommitted repair plan"
            )
            return False
        repair_ids = {
            str(gap.get("gap_id"))
            for gap in audit.get("recommended_repairs", [])
            if isinstance(gap, dict) and gap.get("blocking") is True
        }
        repair_tasks = [
            task
            for task in parse_pending_tasks(workspace / "TASKS.md")
            if any(gap_id in task.description for gap_id in repair_ids)
        ]
        if not repair_tasks:
            # Fallback: control-plane pending tasks may still carry the gap id
            # even when TASKS.md round-section parsing missed them.
            repair_tasks = [
                Task(
                    str(item["topic_path"]),
                    str(item.get("description", "")),
                    str(item.get("execution_mode") or TOPIC_COORDINATOR),
                    str(item.get("research_line_id") or "NOT_APPLICABLE"),
                )
                for item in state_topic_tasks(workspace, status="PENDING")
                if any(
                    gap_id in str(item.get("description", ""))
                    or gap_id.lower() in str(item.get("topic_path", ""))
                    for gap_id in repair_ids
                )
            ]
        if repair_tasks:
            repairs_used += 1
            update_finalization_state(
                workspace,
                "GAP_REPAIR_RUNNING",
                config,
                repair_rounds_used=repairs_used,
            )
            repair_results = _run_repair_tasks(
                workspace,
                repair_tasks,
                round_num + 1,
                num_workers,
                topic_config,
                backend,
            )
            print(
                f"Gap repair: {sum(repair_results.values())}/"
                f"{len(repair_results)} topic tasks completed."
            )
            review_ok = run_manager(
                workspace, round_num + 1, "review", manager_timeout, backend
            )
            if not _verify_phase_committed(
                workspace, round_num + 1, "gap-repair-review", review_ok
            ):
                update_finalization_state(
                    workspace,
                    "AWAITING_INTERVENTION",
                    config,
                    failure="gap-repair review failed",
                )
                _commit_finalization_artifacts(
                    workspace, "finalization: record repair-review failure"
                )
                return False
            tag_round(workspace, round_num + 1)
            registry, audit = aggregate_and_audit(workspace, config)
            _commit_finalization_artifacts(
                workspace, "finalization: rebuild sources after repair"
            )
            if collect_blocking_gaps(audit) and audit.get("status") == "FAILED":
                update_finalization_state(
                    workspace,
                    "AWAITING_INTERVENTION",
                    config,
                    failure=blocking_gap_summary(audit),
                )
                _commit_finalization_artifacts(
                    workspace, "finalization: repair left blockers"
                )
                return False
            if config.enable_prefinal_audit:
                deterministic_audit = audit
                reaudit_agent_ok = run_named_agent(
                    workspace,
                    config.finalizer_agent,
                    build_prefinal_prompt(workspace),
                    "manager-prefinal-reaudit",
                    manager_timeout,
                    backend,
                )
                semantic_audit = _load_prefinal_json(workspace)
                if reaudit_agent_ok and isinstance(semantic_audit, dict):
                    audit = merge_semantic_prefinal_audit(
                        workspace, deterministic_audit, semantic_audit
                    )
                    audit_ok, audit_errors = validate_prefinal_audit(workspace)
                    if not audit_ok:
                        update_finalization_state(
                            workspace, "AWAITING_INTERVENTION", config,
                            failure="; ".join(audit_errors),
                        )
                        return False
                else:
                    update_finalization_state(
                        workspace,
                        "AWAITING_INTERVENTION",
                        config,
                        failure="; ".join(
                            ["pre-final re-audit agent failed or wrote invalid JSON"]
                        ),
                    )
                    _commit_finalization_artifacts(
                        workspace, "finalization: record re-audit failure"
                    )
                    return False
                _commit_finalization_artifacts(
                    workspace, "finalization: pre-final audit after repair"
                )
        else:
            audit = {
                **audit,
                "status": "FAILED",
                "repair_required": False,
                "repair_unavailable": True,
            }
            atomic_write_json(workspace / PREFINAL_AUDIT_PATH, audit)
            update_finalization_state(
                workspace,
                "AWAITING_INTERVENTION",
                config,
                failure="pre-final audit required repair but produced no repair task",
            )
            _commit_finalization_artifacts(
                workspace, "finalization: missing repair task"
            )
            return False

    blockers = collect_blocking_gaps(audit)
    ledger_blockers = blocking_issue_ids(workspace)
    incomplete_tasks = [
        str(item.get("task_id"))
        for item in state_topic_tasks(workspace)
        if item.get("status") not in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}
    ]
    if blockers or ledger_blockers or incomplete_tasks:
        failure = (
            blocking_gap_summary(audit)
            if blockers
            else "open blocking issues: " + ", ".join(ledger_blockers)
            if ledger_blockers
            else "incomplete topic tasks: " + ", ".join(incomplete_tasks)
        )
        if config.allow_finalize_with_limitations:
            # Disclose blockers in the final report instead of stalling when
            # repair budget is exhausted or topics are incomplete.
            update_finalization_state(
                workspace,
                "READY_FOR_FINAL_SYNTHESIS",
                config,
                source_counts={
                    "papers": len((registry or {}).get("papers", [])),
                    "technical_sources": len(
                        (registry or {}).get("technical_sources", [])
                    ),
                },
                failure=f"disclosed blockers: {failure}",
            )
        else:
            update_finalization_state(
                workspace,
                "AWAITING_INTERVENTION",
                config,
                failure=failure,
            )
            _commit_finalization_artifacts(workspace, "finalization: blocked")
            return False
    if (
        audit.get("status") == "PASS_WITH_LIMITATIONS"
        and not config.allow_finalize_with_limitations
    ):
        update_finalization_state(
            workspace,
            "AWAITING_INTERVENTION",
            config,
            failure="pre-final limitations require intervention by configuration",
        )
        _commit_finalization_artifacts(workspace, "finalization: limitations blocked")
        return False

    update_finalization_state(
        workspace,
        "READY_FOR_FINAL_SYNTHESIS",
        config,
        source_counts={
            "papers": len(registry.get("papers", [])),
            "technical_sources": len(registry.get("technical_sources", [])),
        },
    )
    delivery = delivery_statistics(workspace, registry)
    build_report_packet(workspace, delivery)
    diagnostics: list[str] = []
    valid_sections: set[int] = set()
    for attempt in range(1, config.finalizer_retries + 2):
        report = workspace / config.canonical_report
        if report.is_file():
            drafts = workspace / "artifacts" / "final_drafts"
            drafts.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(report, drafts / f"SUMMARY.before-attempt-{attempt}.md")
        update_finalization_state(
            workspace,
            "FINAL_SYNTHESIS_RUNNING",
            config,
            finalizer_attempt=attempt,
            validation_diagnostics=diagnostics,
        )
        finalizer_ok = True
        diagnostics = []
        for section_number in range(1, 6):
            if section_number in valid_sections:
                continue
            section_ok = run_named_agent(
                workspace,
                config.finalizer_agent,
                build_section_prompt(workspace, section_number),
                f"manager-section-{section_number}-attempt-{attempt}",
                config.finalizer_timeout_seconds,
                backend,
            )
            valid, section_errors = validate_section(workspace, section_number)
            if section_ok and valid:
                valid_sections.add(section_number)
            else:
                finalizer_ok = False
                diagnostics.extend(
                    f"section {section_number}: {error}" for error in section_errors
                )
        if len(valid_sections) == 5:
            try:
                assemble_report(workspace, config.canonical_report, delivery)
            except ValueError as exc:
                finalizer_ok = False
                diagnostics.append(str(exc))
        else:
            finalizer_ok = False
        update_finalization_state(
            workspace, "FINAL_VALIDATION", config, finalizer_attempt=attempt
        )
        validation = validate_final_report(workspace, config)
        diagnostics = [*diagnostics, *validation.errors, *validation.warnings]
        if not finalizer_ok:
            diagnostics.insert(0, "finalizer process failed or timed out")
        if finalizer_ok and validation.accepted:
            update_finalization_state(
                workspace,
                "COMPLETE",
                config,
                finalizer_attempt=attempt,
                final_audit=FINAL_AUDIT_PATH,
                completion_status=(
                    "COMPLETE_WITH_WARNINGS"
                    if validation.warnings
                    else "COMPLETE"
                ),
                completed_at=validation.audit["generated_at"],
            )
            _commit_finalization_artifacts(
                workspace, "finalization: validate canonical report"
            )
            return True
        _commit_finalization_artifacts(
            workspace, f"finalization: preserve failed finalizer attempt {attempt}"
        )

    update_finalization_state(
        workspace,
        "AWAITING_INTERVENTION",
        config,
        failure="; ".join(diagnostics),
    )
    _commit_finalization_artifacts(workspace, "finalization: intervention required")
    return False


def _run_slr_unlocked(
    workspace: Path,
    max_rounds: int,
    num_workers: int,
    worker_timeout: int,
    manager_timeout: int,
    backend: AgentBackend,
    allow_dirty: bool = False,
    topic_config: TopicExecutionConfig | None = None,
    finalization_config: FinalizationConfig | None = None,
) -> None:
    """Execute the manager-worker loop.

    This function both starts a fresh review and resumes an existing one;
    the distinction comes from what is already in the workspace. Previous
    rounds are detected via the `round-N` git tags written at the end of
    each completed round. `max_rounds` is the number of additional rounds
    to run from the current state, not the total target.
    """
    if not (workspace / ".git").is_dir():
        sys.exit(
            f"Workspace is not a git repo: {workspace}\n"
            f"Run `python -m slrharness.scope --theme ... --scope ...` first."
        )

    scope_block = formal_research_block_reason(workspace)
    if scope_block is not None:
        sys.exit(
            f"Formal research is blocked for workspace: {workspace}\n"
            f"{scope_block}\n"
            "Use `python -m slrharness.scope show --workspace ...` and "
            "approve the current revision explicitly."
        )

    tasks_md = workspace / "TASKS.md"
    if not tasks_md.is_file():
        sys.exit(f"TASKS.md not found in workspace: {tasks_md}")

    if not is_workspace_clean(workspace) and not allow_dirty:
        sys.exit(
            f"Workspace has uncommitted changes: {workspace}\n"
            f"Commit or stash them first, or pass --allow-dirty to include them "
            f"in the next manager commit."
        )

    topic_config = topic_config or TopicExecutionConfig()
    finalization_config = finalization_config or FinalizationConfig()
    migrate_legacy_tasks(workspace)
    last_round = current_round(workspace)
    start_round = last_round + 1
    end_round = start_round + max_rounds  # exclusive

    if last_round == 0:
        print(f"Starting a new review; running rounds {start_round}..{end_round - 1}")
    else:
        print(
            f"Resuming review from round-{last_round}; "
            f"running rounds {start_round}..{end_round - 1} "
            f"(+{max_rounds} additional)"
        )
    print(f"Workers per round: {num_workers}, worker timeout: {worker_timeout}s")
    print()

    for round_num in range(start_round, end_round):
        print(f"━━━ Round {round_num} ━━━")

        # Plan pass — manager reviews previous output (if any) and plans tasks
        print(f"[round {round_num}] Manager plan pass...")
        agent_ok = run_manager(workspace, round_num, "plan", manager_timeout, backend)
        if not _verify_phase_committed(workspace, round_num, "plan", agent_ok):
            print(
                f"[round {round_num}] Stopping after plan pass (agent_ok={agent_ok})."
            )
            break

        # The plan Markdown is a one-way agent proposal. Import it, then use
        # structured state and re-render TASKS.md for every later decision.
        proposed = parse_pending_tasks(tasks_md)
        register_tasks(workspace, proposed, round_num, legacy_import=True)
        pending = [
            Task(
                str(item["topic_path"]),
                str(item.get("description", "")),
                str(item.get("execution_mode") or TOPIC_COORDINATOR),
                str(item.get("research_line_id") or "NOT_APPLICABLE"),
                tuple(str(value) for value in item.get("diagnostics", [])),
            )
            for item in state_topic_tasks(workspace, status="PENDING")
        ]
        if not pending:
            print(f"[round {round_num}] No pending tasks. Starting finalization.")
            finalized = run_finalization_pipeline(
                workspace,
                round_num,
                num_workers,
                manager_timeout,
                backend,
                topic_config,
                finalization_config,
            )
            if finalized:
                update_round_status(workspace, round_num, "COMPLETE")
                _git_commit_leftovers(
                    workspace,
                    round_num,
                    "state",
                    "record program-owned round completion",
                )
                tag_round(workspace, round_num)
            else:
                update_round_status(workspace, round_num, "AWAITING_INTERVENTION")
                _git_commit_leftovers(
                    workspace,
                    round_num,
                    "state",
                    "record finalization intervention",
                )
            break

        tasks_this_round = pending[:num_workers]
        print(f"[round {round_num}] Dispatching {len(tasks_this_round)} worker(s):")
        for task in tasks_this_round:
            print(f"  - {task.topic_path}: {task.description[:70]}")

        legacy_tasks = [
            task
            for task in tasks_this_round
            if (task.execution_mode or topic_config.mode) == LEGACY_WORKER
        ]
        coordinator_tasks = [
            task
            for task in tasks_this_round
            if (task.execution_mode or topic_config.mode) == TOPIC_COORDINATOR
        ]
        worker_results: dict[str, bool] = {}
        if legacy_tasks:
            legacy_results = run_workers(
                workspace, legacy_tasks, round_num, worker_timeout, backend
            )
            worker_results.update(legacy_results)
            for task in legacy_tasks:
                update_topic_task(
                    workspace,
                    task_id_for_topic_path(task.topic_path),
                    stage="TERMINAL",
                    status="COMPLETE" if legacy_results.get(task.topic_path) else "FAILED",
                )
        if coordinator_tasks:
            worker_results.update(
                run_topic_coordinators(
                    workspace,
                    coordinator_tasks,
                    round_num,
                    topic_config,
                    backend,
                )
            )
        completed = sum(1 for v in worker_results.values() if v)
        print(
            f"[round {round_num}] "
            f"{completed}/{len(tasks_this_round)} workers completed in time."
        )

        # Review pass — manager validates and commits
        print(f"[round {round_num}] Manager review pass...")
        agent_ok = run_manager(workspace, round_num, "review", manager_timeout, backend)
        if not _verify_phase_committed(workspace, round_num, "review", agent_ok):
            print(
                f"[round {round_num}] Stopping after review pass (agent_ok={agent_ok})."
            )
            break

        update_round_status(workspace, round_num, "COMPLETE")
        _git_commit_leftovers(
            workspace, round_num, "state", "record program-owned round completion"
        )
        tag_round(workspace, round_num)
        print(f"[round {round_num}] Complete. Tagged round-{round_num}.")
        print()

        if not state_topic_tasks(workspace, status="PENDING"):
            print(f"[round {round_num}] Topic research complete. Finalizing...")
            run_finalization_pipeline(
                workspace,
                round_num,
                num_workers,
                manager_timeout,
                backend,
                topic_config,
                finalization_config,
            )
            break

    print("Orchestration complete.")
    print(f"View history: cd {workspace} && git log --oneline")


def run_slr(
    workspace: Path,
    max_rounds: int,
    num_workers: int,
    worker_timeout: int,
    manager_timeout: int,
    backend: AgentBackend,
    allow_dirty: bool = False,
    topic_config: TopicExecutionConfig | None = None,
    finalization_config: FinalizationConfig | None = None,
) -> None:
    """Run or resume while refusing a second writer for this workspace."""
    try:
        with ProjectLock(workspace):
            _run_slr_unlocked(
                workspace,
                max_rounds,
                num_workers,
                worker_timeout,
                manager_timeout,
                backend,
                allow_dirty,
                topic_config,
                finalization_config,
            )
    except ProjectLockedError as exc:
        raise SystemExit(str(exc)) from exc


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared between the top-level parser and the 'run' subcommand."""
    topic_defaults = TopicExecutionConfig()
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Path to workspace directory (created by slrharness.scope).",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        help=(
            "Additional rounds to run from the current state "
            f"(default: {DEFAULT_MAX_ROUNDS}). "
            "Resuming: this is rounds on top of whatever is already committed."
        ),
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=DEFAULT_NUM_WORKERS,
        help=f"Max parallel workers per round (default: {DEFAULT_NUM_WORKERS}).",
    )
    parser.add_argument(
        "--worker-timeout",
        type=int,
        default=DEFAULT_WORKER_TIMEOUT,
        help=f"Per-worker timeout in seconds (default: {DEFAULT_WORKER_TIMEOUT}).",
    )
    parser.add_argument(
        "--manager-timeout",
        type=int,
        default=DEFAULT_MANAGER_TIMEOUT,
        help=(
            "Per-manager-invocation timeout in seconds "
            f"(default: {DEFAULT_MANAGER_TIMEOUT})."
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Proceed even if the workspace has uncommitted changes. "
            "Those changes will be folded into the next manager commit."
        ),
    )
    parser.add_argument(
        "--topic-execution-mode",
        choices=(LEGACY_WORKER, TOPIC_COORDINATOR),
        default=LEGACY_WORKER,
        help="How each unannotated topic task is executed (default: legacy_worker).",
    )
    parser.add_argument(
        "--coordinator-timeout",
        type=int,
        default=topic_defaults.coordinator_timeout_seconds,
        help="Shared timeout in seconds for a topic-coordinator batch.",
    )
    parser.add_argument(
        "--coordinator-retries",
        type=int,
        default=topic_defaults.coordinator_retries,
        help="Retries after a failed coordinator attempt (default: 1).",
    )
    parser.add_argument(
        "--allow-partial-completion",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Accept disclosed PARTIAL coordinator output (default: enabled).",
    )
    parser.add_argument(
        "--target-papers", type=int, default=topic_defaults.target_papers
    )
    parser.add_argument(
        "--max-paper-candidates",
        type=int,
        default=topic_defaults.max_paper_candidates,
    )
    parser.add_argument(
        "--target-technical-sources",
        type=int,
        default=topic_defaults.target_technical_sources,
    )
    parser.add_argument(
        "--max-technical-candidates",
        type=int,
        default=topic_defaults.max_technical_candidates,
    )
    parser.add_argument(
        "--max-correction-rounds",
        type=int,
        default=topic_defaults.max_correction_rounds,
    )
    parser.add_argument(
        "--accept-valid-artifacts-after-process-failure",
        action=argparse.BooleanOptionalAction,
        default=topic_defaults.accept_valid_artifacts_after_process_failure,
    )
    parser.add_argument(
        "--finalization",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run source aggregation, audits, and final synthesis (default: enabled).",
    )
    parser.add_argument("--max-prefinal-repair-rounds", type=int, default=1)
    parser.add_argument(
        "--allow-finalize-with-limitations",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--finalizer-timeout", type=int, default=3600)
    parser.add_argument("--finalizer-retries", type=int, default=1)
    parser.add_argument(
        "--allow-complete-with-warnings",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--agent-backend",
        default=DEFAULT_BACKEND,
        choices=available_backends(),
        help=(
            "Agent CLI backend to use for manager and worker invocations "
            f"(default: {DEFAULT_BACKEND}). "
            f"Available: {', '.join(available_backends())}."
        ),
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point. Supports both:

    python -m slrharness.orchestrator --workspace X --max-rounds 5   (back-compat)
    python -m slrharness.orchestrator run --workspace X --max-rounds 5
    python -m slrharness.orchestrator status --workspace X
    """
    if argv is None:
        argv = sys.argv[1:]

    # Route to the right subparser. If the first token is a known subcommand
    # name, strip it and use that subparser; otherwise default to "run".
    subcommand = "run"
    if argv and argv[0] in ("run", "status", "finalize", "compile-topic"):
        subcommand = argv[0]
        argv = argv[1:]

    if subcommand == "status":
        parser = argparse.ArgumentParser(
            prog="slrharness.orchestrator status",
            description="Print a status summary of an existing workspace.",
        )
        parser.add_argument(
            "--workspace",
            type=Path,
            required=True,
            help="Path to workspace directory.",
        )
        args = parser.parse_args(argv)
        print_status(args.workspace.resolve())
        return

    if subcommand == "compile-topic":
        parser = argparse.ArgumentParser(
            prog="slrharness.orchestrator compile-topic",
            description="Compile canonical control artifacts for one topic.",
        )
        parser.add_argument("--workspace", type=Path, required=True)
        parser.add_argument("--topic", required=True)
        parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args(argv)
        workspace = args.workspace.resolve()
        paths = topic_paths(workspace, args.topic)
        result = compile_topic_artifacts(
            workspace,
            paths,
            replace(TopicExecutionConfig(), mode=TOPIC_COORDINATOR),
            None,
            dry_run=args.dry_run,
        )
        print(
            json.dumps(
                {
                    "compiled": result.compiled,
                    "status": result.status,
                    "errors": result.errors,
                    "warnings": result.warnings,
                    "recovered_paths": result.recovered_paths,
                    "paper_count": result.paper_count,
                    "technical_source_count": result.technical_source_count,
                    "metadata_status": result.metadata_status,
                    "checkpoint": result.checkpoint,
                    "manifest": result.manifest,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not result.compiled:
            raise SystemExit(1)
        return

    # "run" and explicit "finalize" (run remains the default for back-compat)
    parser = argparse.ArgumentParser(
        prog=f"slrharness.orchestrator {subcommand}",
        description=(
            "Finalize an existing reviewed workspace."
            if subcommand == "finalize"
            else "Run or resume the SLRHarness manager-worker loop. Re-running "
            "the same workspace continues from the last completed round."
        ),
    )
    _add_run_args(parser)
    args = parser.parse_args(argv)

    if not 1 <= args.max_rounds <= 100:
        parser.error("--max-rounds must be between 1 and 100")
    if not 1 <= args.num_workers <= 32:
        parser.error("--num-workers must be between 1 and 32")
    if args.worker_timeout <= 0 or args.manager_timeout <= 0:
        parser.error("worker and manager timeouts must be positive")

    backend = get_backend(args.agent_backend)
    check_common_dependencies()
    backend.check_available()
    try:
        topic_config = TopicExecutionConfig(
            mode=args.topic_execution_mode,
            allow_partial_completion=args.allow_partial_completion,
            coordinator_timeout_seconds=args.coordinator_timeout,
            coordinator_retries=args.coordinator_retries,
            target_papers=args.target_papers,
            max_paper_candidates=args.max_paper_candidates,
            target_technical_sources=args.target_technical_sources,
            max_technical_candidates=args.max_technical_candidates,
            max_correction_rounds=args.max_correction_rounds,
            accept_valid_artifacts_after_process_failure=(
                args.accept_valid_artifacts_after_process_failure
            ),
        )
        final_config = FinalizationConfig(
            enabled=args.finalization,
            max_prefinal_repair_rounds=args.max_prefinal_repair_rounds,
            allow_finalize_with_limitations=args.allow_finalize_with_limitations,
            finalizer_timeout_seconds=args.finalizer_timeout,
            finalizer_retries=args.finalizer_retries,
            allow_complete_with_warnings=args.allow_complete_with_warnings,
        )
    except ValueError as exc:
        parser.error(str(exc))
    workspace = args.workspace.resolve()
    if subcommand == "finalize":
        if formal_research_block_reason(workspace) is not None:
            parser.error("scope approval is required before finalization")
        if not args.allow_dirty and not is_workspace_clean(workspace):
            parser.error("workspace is dirty; commit changes or pass --allow-dirty")
        try:
            with ProjectLock(workspace):
                run_finalization_pipeline(
                    workspace,
                    current_round(workspace),
                    args.num_workers,
                    args.manager_timeout,
                    backend,
                    topic_config,
                    final_config,
                )
        except ProjectLockedError as exc:
            parser.error(str(exc))
        return
    run_slr(
        workspace=workspace,
        max_rounds=args.max_rounds,
        num_workers=args.num_workers,
        worker_timeout=args.worker_timeout,
        manager_timeout=args.manager_timeout,
        backend=backend,
        allow_dirty=args.allow_dirty,
        topic_config=topic_config,
        finalization_config=final_config,
    )


if __name__ == "__main__":
    main()
