"""Main orchestration loop for SLRHarness.

Drives the manager-worker loop:
  scope.py init -> [manager-plan -> workers (parallel) -> manager-review] x N

Workers never write to TASKS.md, SUMMARY.md, or SCOPE.md -- only to topics/.
The manager is the sole owner of control files and git commits.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from slrharness.agent_backends import (
    DEFAULT_BACKEND,
    AgentBackend,
    available_backends,
    get_backend,
)
from slrharness.scope_workflow import (
    formal_research_block_reason,
    load_scope_state,
    state_path,
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
    build_coordinator_prompt,
    finish_topic_attempt,
    initialize_topic_attempt,
    record_topic_process,
    topic_paths,
    validate_coordinator_outputs,
)

DEFAULT_MAX_ROUNDS = 5
DEFAULT_NUM_WORKERS = 3
DEFAULT_WORKER_TIMEOUT = 600  # seconds
DEFAULT_MANAGER_TIMEOUT = 900  # seconds (manager does heavier reasoning)

WORKER_SESSION = "slr-workers"
COORDINATOR_SESSION = "slr-topic-coordinators"
MANAGER_SESSION = "slr-manager"


@dataclass(frozen=True)
class Task:
    """A single pending task parsed from TASKS.md."""

    topic_path: str  # e.g., "topics/efficiency/pruning"
    description: str  # Free-form description
    execution_mode: str | None = None


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

    Only the first 'Pending' section under the latest round is consumed.
    """
    content = tasks_md.read_text(encoding="utf-8")
    tasks: list[Task] = []

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
    for match in pattern.finditer(content):
        topic_path = match.group(1).rstrip("/")
        description = match.group(2).strip()
        execution_mode = None
        mode_match = re.match(
            r"^\[mode=(legacy_worker|topic_coordinator)\]\s*", description
        )
        if mode_match:
            execution_mode = mode_match.group(1)
            description = description[mode_match.end() :].strip()
        tasks.append(
            Task(
                topic_path=topic_path,
                description=description,
                execution_mode=execution_mode,
            )
        )

    return tasks


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
        2. Before changing SCOPE.md, re-read SCOPE_ORIGINAL.md and verify:
           - The research question is preserved verbatim
           - All original inclusion/exclusion criteria remain present
           - All original comparison dimensions remain (new ones may be appended)
           - Original ranking & grouping criteria remain intact
           - Log any additions in a `## Scope Evolution Log` section at the bottom
        3. You OWN TASKS.md, SUMMARY.md, and SCOPE.md. Workers never touch these.
        4. You never search databases directly -- that is workers' job.
        5. SUMMARY.md must be organized per the ranking & grouping criteria in
           SCOPE.md. Items must appear in the order those criteria dictate.
        6. At the end of this phase, commit all changes with git.

        Workspace files:
        - SCOPE_ORIGINAL.md (immutable baseline)
        - SCOPE.md (living scope, includes ranking & grouping criteria)
        - TASKS.md (task registry, checkbox format)
        - SUMMARY.md (evolving synthesis with comparison tables)
        - topics/ (READ ONLY). A TASKS entry `topics/x/y` has exactly one
          primary synthesis at `topics/x/y.md`. A same-stem directory
          `topics/x/y/` contains supporting paper notes, technical notes,
          audits, task state, and a coordinator manifest; these are evidence
          attachments, not additional topics.
    """)

    if phase == "plan":
        phase_instructions = textwrap.dedent(f"""\
            PLAN PASS -- your responsibilities this invocation:

            1. Read SCOPE_ORIGINAL.md, SCOPE.md, TASKS.md, SUMMARY.md.
            2. If this is round 1 and TASKS.md has no pending tasks, break down
               SCOPE.md into concrete research tasks. Each task should be narrow
               enough for a single worker to complete. Format each pending task as:
                 - [ ] topics/{{topic}}/{{subtopic}} -- {{one-line description}}
            3. If there are new files in topics/ since the last review commit,
               incorporate their findings into SUMMARY.md comparison tables,
               maintaining the ranking & grouping order defined in SCOPE.md.
               When workers extracted ranking scores in their `## Ranking Scores`
               section, use those scores (recompute the composite if defined).
            4. If workers proposed new comparison dimensions (in their
               "## Proposed Additions" sections), consider them and -- if valid --
               append to SCOPE.md (respecting invariants above).
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
               `coordinator_manifest.json`, `task.json`, paper notes, technical
               notes, and metadata audit as supporting evidence. Validate:
               - Required sections present (Summary, Key Findings,
                 Comparison Data, References, Sources, Related Topics)
               - Inclusion/exclusion criteria from SCOPE.md were applied
               - All comparison dimensions from SCOPE.md are filled in
               - Sources include working links (paper, project page, code, dataset
                 where applicable)
               - Workspace context links at top are correct
               - If SCOPE.md defines ranking criteria, the worker's
                 `## Ranking Scores` section is present and follows the rubric
               - `## Related Topics` connects to at least one sibling topic when
                 such connections exist
            3. In TASKS.md, mark successfully completed tasks with `[x]` and move
               them to the "Completed" section with the round number.
            4. Leave failed/incomplete tasks as `[ ]` with a note explaining why.
            5. Update SUMMARY.md with validated findings. Re-sort the comparison
               table and findings sections per the ranking & grouping criteria in
               SCOPE.md now that new items are in play.
            6. Run:
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
    exit_code = _read_agent_exit_code(status_path)
    if not completed or exit_code != 0:
        print(
            f"[round {round_num}] Manager {phase} failed "
            f"(completed={completed}, exit_code={exit_code})."
        )
        if output.strip():
            print(output.rstrip())
    return completed and exit_code == 0


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
        results[invocation.task.topic_path] = CoordinatorProcessResult(
            completed=completed[spec.window_name],
            exit_code=_read_agent_exit_code(spec.exit_status_path),
            timed_out=not completed[spec.window_name],
            output=capture_pane(COORDINATOR_SESSION, spec.window_name),
            pid=pids[spec.window_name],
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


def run_topic_coordinators(
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
            elif process.timed_out or not process.completed:
                diagnostics.append("Coordinator process timed out")
            elif process.exit_code != 0:
                diagnostics.append(
                    f"Coordinator exited with status {process.exit_code}"
                )
            else:
                validation = validate_coordinator_outputs(workspace, paths, config)
                diagnostics.extend(validation.errors)
                diagnostics.extend(validation.warnings)

            accepted = validation is not None and validation.accepted
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


def run_slr(
    workspace: Path,
    max_rounds: int,
    num_workers: int,
    worker_timeout: int,
    manager_timeout: int,
    backend: AgentBackend,
    allow_dirty: bool = False,
    topic_config: TopicExecutionConfig | None = None,
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

        # Extract pending tasks after the plan pass
        pending = parse_pending_tasks(tasks_md)
        if not pending:
            print(f"[round {round_num}] No pending tasks. Review complete.")
            tag_round(workspace, round_num)
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
            worker_results.update(
                run_workers(workspace, legacy_tasks, round_num, worker_timeout, backend)
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

        tag_round(workspace, round_num)
        print(f"[round {round_num}] Complete. Tagged round-{round_num}.")
        print()

    print("Orchestration complete.")
    print(f"View history: cd {workspace} && git log --oneline")


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared between the top-level parser and the 'run' subcommand."""
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
        default=3600,
        help="Shared timeout in seconds for a topic-coordinator batch.",
    )
    parser.add_argument(
        "--coordinator-retries",
        type=int,
        default=1,
        help="Retries after a failed coordinator attempt (default: 1).",
    )
    parser.add_argument(
        "--allow-partial-completion",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Accept disclosed PARTIAL coordinator output (default: enabled).",
    )
    parser.add_argument("--target-papers", type=int, default=10)
    parser.add_argument("--max-paper-candidates", type=int, default=30)
    parser.add_argument("--target-technical-sources", type=int, default=5)
    parser.add_argument("--max-technical-candidates", type=int, default=15)
    parser.add_argument("--max-correction-rounds", type=int, default=2)
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
    if argv and argv[0] in ("run", "status"):
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

    # "run" subcommand (also the default for back-compat)
    parser = argparse.ArgumentParser(
        prog="slrharness.orchestrator run",
        description=(
            "Run or resume the SLRHarness manager-worker loop. Re-running "
            "the same workspace continues from the last completed round."
        ),
    )
    _add_run_args(parser)
    args = parser.parse_args(argv)

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
        )
    except ValueError as exc:
        parser.error(str(exc))
    run_slr(
        workspace=args.workspace.resolve(),
        max_rounds=args.max_rounds,
        num_workers=args.num_workers,
        worker_timeout=args.worker_timeout,
        manager_timeout=args.manager_timeout,
        backend=backend,
        allow_dirty=args.allow_dirty,
        topic_config=topic_config,
    )


if __name__ == "__main__":
    main()
