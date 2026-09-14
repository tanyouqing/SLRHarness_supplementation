# SLRHarness

**Scientific Literature Review Harness** — an agentic system for conducting reproducible literature reviews using CLI agents (Kiro, Claude Code, etc.) in a manager–worker loop.

SLRHarness turns an initial research direction into an approved, structured,
version-controlled literature review. A Python orchestrator owns lifecycle state,
validation, retry, and Git boundaries; bounded Claude Code agents own research and
written synthesis.

See [`docs/architecture.md`](docs/architecture.md),
[`docs/artifact-contracts.md`](docs/artifact-contracts.md), and
[`docs/development.md`](docs/development.md).

Chinese v1 handoff documents:

- [`docs/v1-update-overview.zh-CN.md`](docs/v1-update-overview.zh-CN.md) — stages,
  features, and file responsibilities added in v1;
- [`docs/claude-code-setup-guide.zh-CN.md`](docs/claude-code-setup-guide.zh-CN.md)
  — installation, Claude Code/MCP setup, first run, and remaining live checks.

## Bring your own tools

SLRHarness orchestrates agents — it does **not** bundle search or retrieval tools itself. The agents it spawns are only as capable as the tools you give them. Install and expose whatever tools your agents need through your platform's mechanism: MCP servers, direct function calls, API integrations, or any other tool-binding approach your agent runtime supports.

Useful tool categories to consider:

- **Web fetch / browse** — retrieve and read arbitrary URLs
- **Search** — Semantic Scholar, OpenAlex, PubMed, Exa, Tavily, or any search MCP
- **Deep research** — long-running research agents (e.g., Tavily Research, deep-research MCPs)
- **PDF reading** — extract text, tables, and metadata from papers
- **Database access** — any database MCP for storing and querying extracted data
- **Citation graph** — tools for forward/backward citation traversal

The more capable your agent's toolset, the more ground each worker can cover per round. See your agent platform's docs for how to register tools (e.g., `.kiro/settings/mcp.json` for Kiro, `~/.claude/` for Claude Code).

## Prerequisites

- Python ≥ 3.11
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- [`tmux`](https://github.com/tmux/tmux) — required for formal rounds. The v1
  runtime target is Linux or WSL; native Windows and macOS orchestration are not
  supported.
- An agent CLI backend (at least one):
  - [`kiro-cli`](https://kiro.dev/docs/cli/) — Kiro agent runtime (authenticated), or
  - [`claude`](https://docs.anthropic.com/en/docs/claude-code) — Claude Code CLI
- `git` on PATH

## Installation

```bash
git clone https://github.com/yourname/slrharness.git
cd slrharness
uv sync --group dev
uv run slrharness doctor
uv run pytest
```

The installed `slrharness` entry point is canonical. Historical
`python -m slrharness.scope` and `python -m slrharness.orchestrator` commands
remain supported. Diagnose the environment and validate a project without
running agents or changing files:

```bash
slrharness doctor --workspace workspaces/agent-memory
slrharness status workspaces/agent-memory
slrharness validate workspaces/agent-memory
```

`status` is a read-only validation snapshot. Errors, warnings, and informational
counts are printed separately. Mutating `run` and `finalize` commands take a
workspace-specific lock under `.git/.slrharness.lock`; a second process is
rejected. Remove that exact file only after confirming its recorded PID is no
longer running.

Validate and expand the key-free v1 configuration example with:

```bash
slrharness config plugins/config/example-config.json
```

Missing fields receive centralized defaults; unknown sections/fields, unsupported
schema versions, invalid modes, negative budgets, and excessive bounds fail
validation. CLI flags remain the execution interface in v1; the example documents
the equivalent complete configuration contract without storing credentials.

## First use with Claude Code: start from a topic

Verify that Claude Code is installed and authenticated without storing any
credentials in this repository:

```bash
claude --version
claude -p "Reply with OK"
```

Create a scope project from a preliminary direction. This command creates an
independent Git workspace, deploys the project-scoped Claude agents and skill,
runs `slr-scoper`, writes a proposal, and then exits at the approval gate.

```bash
uv run python -m slrharness.scope prepare \
  --theme "agent memory" \
  --topic "Agent memory for LLM-based autonomous agents" \
  --agent-backend claude-code
```

It does **not** start the manager or formal workers. Inspect the current state,
proposal, and source-attempt log:

```bash
uv run python -m slrharness.scope show \
  --workspace workspaces/agent-memory
```

Request a new, auditable revision when boundaries need adjustment:

```bash
uv run python -m slrharness.scope revise \
  --workspace workspaces/agent-memory \
  --feedback "Exclude ordinary RAG and focus on persistent autonomous-agent memory." \
  --agent-backend claude-code
```

Approve the exact revision printed by `scope show`:

```bash
uv run python -m slrharness.scope approve \
  --workspace workspaces/agent-memory \
  --revision 2
```

Approval copies the proposal unchanged into `SCOPE.md` and
`SCOPE_ORIGINAL.md`, creates the original manager/worker control files, and
sets `formal_research_allowed` to true. Only then can the existing loop run:

```bash
uv run python -m slrharness.orchestrator run \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --max-rounds 5
```

If scope preparation was interrupted or failed validation, retry the same
revision without losing its state or earlier revision archives:

```bash
uv run python -m slrharness.scope resume \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code
```

Running `scope resume` while a valid proposal awaits approval only prints the
approval instructions; it never silently regenerates or approves the scope.
After approval, resume formal research by rerunning `orchestrator run` as
before.

### Starting from a complete scope

Treat a complete file as a draft and let the scope agent research and improve
it:

```bash
uv run python -m slrharness.scope prepare \
  --theme "agent memory" \
  --draft-scope my-scope.md \
  --agent-backend claude-code
```

Or explicitly declare the supplied file already approved and skip preliminary
scope research:

```bash
uv run python -m slrharness.scope init \
  --theme "agent memory" \
  --approved-scope my-scope.md
```

The historical command without an `init` subcommand remains supported and is
also treated as an explicit, already-approved input:

```bash
uv run python -m slrharness.scope --theme "agent memory" --scope my-scope.md
```

Use `scope reject --workspace ... --revision N` to reject the current proposal.
A rejected project remains blocked until `scope revise` records feedback and
generates another revision.

### Optional literature-search MCP servers

MCP servers improve preliminary source discovery but are never startup or
completion requirements. Configure them at user scope rather than committing
credentials or machine-specific settings:

```bash
claude mcp add --transport stdio --scope user arxiv -- uvx arxiv-mcp-server
claude mcp add --transport stdio --scope user scholarly -- uvx mcp-scholarly
claude mcp add --env TAVILY_API_KEY=YOUR_KEY --transport stdio --scope user tavily -- npx -y tavily-mcp@latest
```

Verify configuration with:

```bash
claude mcp list
claude mcp get arxiv
claude mcp get scholarly
claude mcp get tavily
```

Inside interactive Claude Code, `/mcp` shows connection status. Basic arXiv
and mcp-scholarly usage normally needs no API key, although Google Scholar
scraping can be unstable. Tavily requires a key and is only the last optional
fallback. Never commit API keys to Git, agent files, prompts, fixtures, logs,
or scope outputs.

The scope agent falls back from available scholarly and arXiv tools to Claude
Code WebSearch/WebFetch, then optional Tavily. If all network retrieval fails,
it must still produce a substantive proposal using limited model knowledge,
mark those claims `[UNVERIFIED]`, and record the failures in
`SCOPE_SOURCES.md`. Zero successfully read surveys is valid; it is not evidence
that the field has no literature.

## Coordinated topic execution (Claude Code)

The original single-worker path remains the default for old commands and
workspaces. To upgrade each selected topic task to one deterministic top-level
Claude Code coordinator, run:

```bash
uv run python -m slrharness.orchestrator run \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator \
  --num-workers 3 \
  --coordinator-timeout 3600 \
  --coordinator-retries 1
```

The orchestrator starts exactly one `claude --agent topic-coordinator -p ...`
process for each selected topic and owns its task ID, attempt, timeout, PID,
exit status, validation, retry, and resume decision. Inside that one session,
the coordinator launches `academic-paper-worker` and
`technical-source-worker` concurrently as ordinary named subagents, then runs
`academic-metadata-checker` after the paper manifest exists. The checker only
audits publication identity and metadata.

This mode deliberately does not enable Claude Code Agent Teams. Ordinary
subagents currently do not expose team peer `SendMessage`, so corrections use
the durable `audits/correction_requests.jsonl` queue: the coordinator invokes
the academic worker to apply pending requests, then invokes the checker again,
for at most `--max-correction-rounds` rounds. This is also the mandatory
fallback if messaging capabilities change or fail in a later CLI version.

Each task keeps its Manager-compatible primary synthesis and an adjacent
supporting tree:

```text
topics/<topic>/<subtopic>.md                 # primary Manager input
topics/<topic>/<subtopic>/
├── task.json                               # program-owned attempt/process state
├── task_contract.json                      # program-owned paths and identity
├── papers/index.json                       # or papers/NO_RESULTS.md
├── papers/<stable-paper-slug>.md
├── technical_sources/index.json            # or NO_RESULTS.md
├── technical_sources/<stable-source-slug>.md
├── audits/metadata_findings.jsonl           # checker observations
├── audits/metadata_check.json
├── audits/correction_requests.jsonl
├── audits/artifact_normalization.json
├── audits/checkpoint.json
├── coordination_log.jsonl
└── coordinator_manifest.json
```

Workers now own semantic evidence only: academic/technical Markdown notes,
metadata findings and correction requests. The Coordinator owns the topic
synthesis and coordination log. After every process outcome—including timeout
or nonzero exit—the Python Artifact Compiler discovers notes inside that task
root, safely flattens recoverable `papers/notes/` or
`technical_sources/notes/` paths, generates stable IDs, recomputes counts, and
writes the canonical indexes, metadata audit, checkpoint, normalization report,
and manifest. Agent-written counts, task IDs, and manifests are treated as
legacy input rather than authoritative state.

Valid complete artifacts left by a timed-out/nonzero Coordinator are accepted
as `PARTIAL` by default. A retry reads `audits/checkpoint.json` and is told to
complete only `missing_steps`; existing notes are preserved. Disable recovery
with `--no-accept-valid-artifacts-after-process-failure`.

To inspect or rebuild one topic without Claude or tmux:

```bash
slrharness compile-topic --workspace workspaces/agent-memory \
  --topic topics/memory/retrieval --dry-run
slrharness compile-topic --workspace workspaces/agent-memory \
  --topic topics/memory/retrieval
```

The dry run reports discovery and proposed repairs without moving or writing
files. Inspect `audits/artifact_normalization.json` for actual repairs,
generated IDs, imported legacy artifacts, warnings, and hard errors. Missing or
short synthesis, no academic notes/no-result record, no technical
notes/no-result record, unidentifiable notes, path escape, or an unbuildable
audit remains `FAILED`. Recovered paths, incomplete metadata, missing evidence
roles/prioritization, or recovered process failure normally becomes `PARTIAL`.

The Manager treats only the task's top-level `.md` file as a topic. Supporting
notes and audits may be consulted as evidence but are never counted as extra
topics. A coordinator output is accepted only when the synthesis, academic
index/no-result record, technical index/no-result record, metadata audit, task
identity, paths, counts, and final status pass deterministic validation.
Disclosed `PARTIAL` results are accepted by default; use
`--no-allow-partial-completion` to reject them.

New topic-coordinator runs use resource-conscious soft defaults: 4 included
papers from at most 12 candidates, 1 technical source from at most 5 candidates,
and 1 correction round. Citation chaining remains bounded to the most relevant
1–2 seed papers and never recursively expands. CLI flags or explicit project
configuration may raise these limits; legacy-worker execution is unchanged.

Re-run the same command to resume. A previously accepted COMPLETE/PARTIAL task
is not launched again; failed attempts retain valid notes and receive the last
diagnostics in the next coordinator prompt. To force the historical behavior,
use `--topic-execution-mode legacy_worker`. An individual TASKS entry can
override the global mode by starting its description with
`[mode=legacy_worker]` or `[mode=topic_coordinator]`.

Search access is role-scoped: academic worker and metadata checker use
scholarly/arXiv, then Tavily, then WebSearch/WebFetch; the technical worker uses
Tavily plus WebSearch/WebFetch. No API key is written to project
files. Search snippets support discovery and cross-confirmation but are not
final authoritative metadata; the checker prefers arXiv, DOI/publisher,
official venue, and author/project pages. This implementation was exercised
with Claude Code 2.1.269; use a
current stable Claude Code release and verify agents/MCP connections before a
real run.

## Research-line prioritization

The approved scope organizes the review around research lines or method
families, not a ranking of individual papers. Comparison dimensions are facts
extracted from papers or lines; priority factors are cross-line judgments used
by the Manager/Finalizer. Before approval, review the proposed groups, stable
`RL-...` IDs, factor/tier rubric, ordering and tiebreaker, missing-data policy,
and contradictory-evidence policy.

Approval compiles the policy into `artifacts/SCOPE_PRIORITIZATION.json`.
Manager tasks may carry `[line=RL-...]` beside `[mode=topic_coordinator]`; old
tasks receive a stable topic-path fallback ID. Topic syntheses provide local
line assessments, paper notes record optional evidence roles, and
`SOURCE_REGISTRY.json` merges these into `research_lines`. Missing optional
assessments remain warnings and unknown values never become zero.

The default ordinal tiers are Core, Supporting, Peripheral, and Insufficient
Evidence. They control narrative prominence: Core lines receive full treatment,
Supporting lines concise comparison, Peripheral lines brief/table coverage,
and evidence-limited lines explicit disclosure. Paper roles—anchor,
representative, supporting, contradictory, peripheral, and unassigned—describe
evidentiary function, not quality. Material contradictory evidence must appear
in final Section 4 regardless of tier. Numeric or weighted ranking is used only
when the approved scope supplies an operational rubric; otherwise the pipeline
records a qualitative fallback.

Final Section 3 contains a research-line prioritization table and findings by
approved group/order. Prioritization limitations disclose incomplete contracts,
unranked lines, missing factors, access-depth effects, and agent judgment.
Citation counts are time-biased, technical sources are not independent academic
evidence, and the result is not an objective paper leaderboard.

## Finalization, global provenance, and bounded repair

When Manager Review leaves no pending tasks, the same orchestrator now runs:

```text
topic completion
→ source aggregation
→ pre-final audit
→ optional bounded gap repair
→ final synthesis
→ deterministic final validation
→ COMPLETE
```

`SUMMARY.md` remains the single canonical report. The explicit `finalize`
subcommand resumes this pipeline without rerunning completed topics:

```bash
uv run python -m slrharness.orchestrator finalize \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator
```

Normal `orchestrator run` invokes the same finalization automatically. Use
`--no-finalization` only when intentionally running the historical research
rounds without a final report. Finalization state is program-owned under
`SLR_STATE.json.finalization`; the top-level scope approval state remains
unchanged.

### Canonical paper notes

Every included paper follows the distributed template at
`.claude/templates/paper-note.md` (source asset:
`plugins/templates/paper-note.md`). Notes carry machine-readable frontmatter,
a stable local Paper ID, access and reading depth, reported facts versus
reviewer interpretation, experimental context, limitations, and evidence
locators. `Not reported`, `Not applicable`, unavailable values, and
`[UNVERIFIED]` are valid; missing evidence is never guessed. Metadata Checker
continues to verify only identity/version metadata.

The deterministic validator checks required fields and sections, legal access
and reading states, index/note agreement, corrected metadata, duplicate
DOI/arXiv identities, and whether concrete numbers retain evidence context. It
writes `artifacts/audits/paper_note_validation.json` without deleting notes.

### Global source registry

Aggregation walks parseable accepted coordinator manifests rather than asking
an LLM to guess the directory tree. It creates:

```text
artifacts/
├── PAPER_LIST.md
├── SOURCE_REGISTRY.json
├── REFERENCES.md
└── audits/
    ├── paper_note_validation.json
    ├── source_deduplication.json
    ├── prefinal_audit.json
    └── final_audit.json
```

Academic IDs are deterministic SHA-256-derived `P...` values, preferring a
normalized DOI, then version-free arXiv ID, then an exact normalized
title/first-author fallback. Technical IDs are deterministic `T...` values
based primarily on canonical URL. Existing registry IDs are reused on rebuild.
DOI, arXiv ID, explicit version group, and only then exact title/author identity
drive merging. Original topic notes are retained, aliases and every covered
topic are recorded, and technical/web sources remain separate from papers.

`REFERENCES.md` is generated from the registry, with unresolved fields marked
`[UNVERIFIED METADATA]`; the Finalizer cannot invent a replacement reference.

### Pre-final audit and repair limit

The program first checks required syntheses, manifests, indexes/no-result
records, metadata audits, note validation, registry/dedup output, unresolved
metadata, and supporting-note traceability. One bounded `slr-manager`
invocation then checks semantic coverage for the five report sections without
web search. Stable `GAP-...` IDs prevent repair tasks from being rediscovered
under new dynamic IDs.

Blocking gaps reuse the existing Manager task format and Topic Coordinator
pipeline. The default maximum is one repair round, configurable from zero to
three:

```bash
--max-prefinal-repair-rounds 1
```

After repair, aggregation and audit run again. At the limit, the system either
finalizes with explicit limitations (default) or stops when
`--no-allow-finalize-with-limitations` is set. This is a bounded state machine,
not Ralph Loop or Agent Teams.

### Five-section English report and validation

The Manager is reused as the Finalizer because it already owns `SUMMARY.md`
and Git commits. It receives explicit scope, topic, registry, paper-list,
references, note-root, and audit paths and has no search MCP. It must write, in
order:

1. Academic Terminology and Problem Boundaries
2. Background, Importance, and Broader Significance
3. Existing Research: Motivations, Methodologies, and Findings
4. Research Landscape: Consensus, Differences, and Experimental Practice
5. Evidence-Backed Research Opportunities

Coverage and Limitations, Sources and Provenance, References, and Delivery
Status follow those five sections. Final validation checks headings/order,
comparison table, experimental/consensus/difference coverage, supported
opportunities, known registry IDs, reference uniqueness/completeness, numeric
claim citations, placeholders, English-language mixing, canonical path, and
exact program-provided Delivery Status counts. A zero exit code alone never
marks the review complete.

Errors trigger only a bounded Finalizer retry and are passed back as precise
diagnostics; completed topic research is not rerun. Warnings are recorded and
may complete by default; use `--no-allow-complete-with-warnings` for strict
handling. Inspect progress and audits with implemented commands:

```bash
uv run python -m slrharness.orchestrator status \
  --workspace workspaces/agent-memory

cat workspaces/agent-memory/artifacts/audits/prefinal_audit.json
cat workspaces/agent-memory/artifacts/audits/final_audit.json
```

To resume an interrupted aggregation, repair, or failed Finalizer, rerun the
same `finalize` command. It rebuilds aggregation safely, does not duplicate
stable repair tasks, preserves failed report drafts under
`artifacts/final_drafts/`, and skips report generation once a valid COMPLETE
state exists.

### Scope workspace files and discovery

Before approval, a new workspace contains:

```text
INITIAL_TOPIC.md
SCOPE_PROPOSAL.md
SCOPE_SOURCES.md
SLR_STATE.json
scope_revisions/revision-N/
.claude/agents/{slr-scoper,slr-manager,slr-worker,topic-coordinator,
  academic-paper-worker,academic-metadata-checker,technical-source-worker}.md
.claude/skills/{slr-scoping,slr-topic-research}/SKILL.md
.claude/templates/{paper-note,final-report}.md
```

`SLR_STATE.json` records the initial topic, current status and revision,
generation and approval timestamps, approved revision, agent result, failure
details, feedback history, configuration, and the formal-research gate. Only
Python lifecycle code writes these protected fields. Every valid proposal and
source log is copied into `scope_revisions/revision-N/` before approval.

Agent and skill definitions are copied automatically into the actual nested
research workspace. Claude Code discovers project agents from
`.claude/agents/`, and the backend invokes the requested identity with
`claude --agent <name>`. No global agent configuration or manual copying is
required. The package build includes the reference `plugins/` assets so this
also works from an installed wheel.

After approval, the workspace additionally receives the original `SCOPE.md`,
`SCOPE_ORIGINAL.md`, `TASKS.md`, `SUMMARY.md`, `topics/`, and `assets/` files,
plus the `round-0` Git tag.

## Quick start with an explicitly approved scope

### 1. Write a scope

Copy `SCOPE_TEMPLATE.md` to a new file and fill it in. For guidance on PICO/PICo question formulation, Boolean search strings, and comparison dimension design, see the scoping skill at [`plugins/skills/slr-scoping/SKILL.md`](plugins/skills/slr-scoping/SKILL.md).

```bash
cp SCOPE_TEMPLATE.md my-scope.md
# Edit my-scope.md
```

### 2. Set up agent configuration

The `plugins/` directory contains Claude Code project-agent definitions,
Kiro/reference JSON definitions, and the shared scoping skill:

```text
plugins/
├── agents/
│   ├── slr-manager.json   # Manager agent: plans, reviews, commits
│   ├── slr-worker.json    # Worker agent: researches one topic
│   ├── slr-scoper.md      # Claude Code scope agent
│   ├── slr-manager.md     # Claude Code manager agent
│   └── slr-worker.md      # Claude Code worker agent
└── skills/
    └── slr-scoping/
        └── SKILL.md        # Scoping best practices for writing SCOPE.md
```

New workspaces receive the Claude Code Markdown definitions and skill
automatically. Kiro users can still copy the JSON references manually:

**Kiro** — copy into `.kiro/`:

```bash
cp plugins/agents/*.json .kiro/agents/
cp -r plugins/skills/* .kiro/skills/
```

Other platforms can adapt the reference definitions to their own format.

### 3. Initialize a workspace

This creates `workspaces/{theme-slug}/` as an **independent git repository** (not tracked by the outer SLRHarness repo), copies your scope to `SCOPE_ORIGINAL.md` (immutable) and `SCOPE.md` (living), and commits it as `round-0`.

```bash
uv run python -m slrharness.scope \
  --theme "transformer efficiency" \
  --scope my-scope.md
```

Flags:

- `--theme` (required) — free-form; slugified to a directory name
- `--scope` (required) — path to the filled-in scope file
- `--workspaces-dir` — parent directory for workspaces (default: `./workspaces`)
- `--force` — overwrite an existing workspace with the same slug

### 4. Run the review loop

```bash
uv run python -m slrharness.orchestrator run \
  --workspace workspaces/transformer-efficiency \
  --agent-backend kiro \
  --max-rounds 5 \
  --num-workers 3 \
  --worker-timeout 600
```

(The `run` subcommand is the default; `uv run python -m slrharness.orchestrator --workspace ...` without `run` works too.)

Flags:

- `--workspace` (required) — path to the workspace created by `slrharness.scope`
- `--agent-backend` — which agent CLI to use: `kiro` (default) or `claude-code`
- `--max-rounds` — **additional** rounds to run from the current state (default: `5`). When resuming, this is rounds on top of whatever is already committed.
- `--num-workers` — max parallel workers per round (default: `3`)
- `--worker-timeout` — per-worker timeout in seconds (default: `600`)
- `--manager-timeout` — per-manager-invocation timeout in seconds (default: `900`)
- `--allow-dirty` — proceed even if the workspace has uncommitted changes (they'll be folded into the next manager commit)
- `--topic-execution-mode` — `legacy_worker` (default) or `topic_coordinator`
- `--coordinator-timeout` — shared timeout for a coordinator batch (default: `3600`)
- `--coordinator-retries` — retries after the initial attempt (default: `1`)
- `--[no-]allow-partial-completion` — accept or reject disclosed partial output
- `--target-papers`, `--max-paper-candidates` — bounded academic search budgets
- `--target-technical-sources`, `--max-technical-candidates` — bounded technical search budgets
- `--max-correction-rounds` — bounded file-queue correction/recheck cycles
- `--[no-]finalization` — enable/disable the final pipeline (enabled by default)
- `--max-prefinal-repair-rounds` — bounded repair rounds, `0..3` (default: `1`)
- `--[no-]allow-finalize-with-limitations` — control post-limit finalization
- `--finalizer-timeout`, `--finalizer-retries` — bounded Manager Finalizer runs
- `--[no-]allow-complete-with-warnings` — control warning-only completion

Each round runs as: **manager plan pass → workers (parallel) → manager review pass**. The manager owns all control files and commits twice per round (`round-N-plan`, `round-N-review`). Workers write only to `topics/` and `assets/`.

### Resuming a review

Just re-run the same command on the same workspace. The orchestrator detects completed rounds via the `round-N` git tags and continues from the next one. You can adjust `--max-rounds` on each invocation to run exactly as many additional rounds as you want.

```bash
# Did 10 rounds, now want 5 more
uv run python -m slrharness.orchestrator run \
  --workspace workspaces/transformer-efficiency \
  --max-rounds 5
```

To see where you are before resuming:

```bash
uv run python -m slrharness.orchestrator status \
  --workspace workspaces/transformer-efficiency
```

This prints the last completed round, next round to run, whether the git tree is clean, pending/completed task counts, and recent commit history. The orchestrator refuses to start on a dirty tree unless you pass `--allow-dirty`.

### 5. Watch workers live (optional)

In another terminal:

```bash
tmux attach -t slr-workers      # during worker phase
tmux attach -t slr-manager      # during manager phases
```

Each worker runs in its own tmux window named `worker-1`, `worker-2`, etc. Navigate with `Ctrl-b n` / `Ctrl-b p`.

### 6. Review the output

The workspace is a git repo — the full history of the review is there.

```bash
cd workspaces/transformer-efficiency
git log --oneline                       # one entry per plan/review phase
git diff round-1..round-2                # what changed between rounds
cat SUMMARY.md                           # current synthesis
ls topics/                               # per-topic research notes
```

## Browse a workspace in the browser

For comfortable reading of wide tables and cross-linked notes, launch the
lightweight markdown viewer:

```bash
uv sync --group viewer   # one-time

uv run --group viewer python tools/viewer.py \
  --workspace workspaces/transformer-efficiency
```

Open <http://localhost:8765>. Links between workspace files open in new
tabs, so you can keep several topics side by side. Pass `--port` to change
the port.

## Project structure

```text
slrharness/
├── plugins/
│   ├── agents/
│   │   ├── slr-scoper.md           # Claude scope-preparation agent
│   │   ├── slr-manager.md          # Claude manager agent
│   │   ├── slr-worker.md           # Claude worker agent
│   │   ├── slr-manager.json        # Kiro/reference manager config
│   │   └── slr-worker.json         # Kiro/reference worker config
│   └── skills/slr-scoping/
│       └── SKILL.md                # Scoping best practices
│   ├── templates/                  # Canonical paper/final report contracts
│   └── config/example-config.yaml  # Key-free configuration reference
├── src/slrharness/
│   ├── __init__.py
│   ├── agent_backends.py           # Agent platform abstraction & registry
│   ├── orchestrator.py             # Main loop, CLI, task parsing
│   ├── scope_workflow.py            # Scope state, revisions, approval gate
│   ├── tmux_runner.py              # Parallel spawn + wait-for sync
│   ├── workspace_assets.py          # Workspace agent/skill deployment
│   ├── contracts.py                # Schema, path and atomic-write contracts
│   ├── diagnostics.py              # Read-only doctor/project validation
│   ├── project_lock.py             # Per-workspace writer lock
│   ├── source_registry.py          # Validation, stable IDs and aggregation
│   ├── finalization.py             # Audit, bounded repair, final validation
│   ├── cli.py                      # Installed unified entry point
│   └── scope.py                     # Scope lifecycle CLI + workspace init
├── tests/
│   ├── test_init.py
│   └── test_orchestrator.py        # Task-parsing regex coverage
├── workspaces/                     # Independent git repos (gitignored)
│   └── {theme-slug}/
│       ├── .git/
│       ├── SCOPE_ORIGINAL.md       # Immutable baseline
│       ├── SCOPE.md                # Living scope
│       ├── TASKS.md                # Task registry (manager-owned)
│       ├── SUMMARY.md              # Evolving synthesis
│       ├── topics/                 # Worker output
│       └── assets/                 # Supporting files
├── docs/                           # Architecture, contracts and development
├── SCOPE_TEMPLATE.md               # Copy-and-fill scope template
├── README.md                       # This file
└── pyproject.toml
```

## How it works

1. **Scope preparation and approval.** Start from a topic or draft, review and
   explicitly approve the generated proposal; or explicitly initialize an
   already-approved scope. Python owns the approval gate and preserves the
   approved content as `SCOPE_ORIGINAL.md`.
2. **Plan pass.** The manager reads the immutable approved scope and writes
   bounded pending topic tasks. It cannot change approval or lifecycle state.
3. **Worker dispatch.** The orchestrator parses `TASKS.md`, extracts up to `--num-workers` pending `- [ ]` lines, and launches each as an agent CLI session in its own tmux window. Workers signal completion via `tmux wait-for` channels.
4. **Review pass.** The manager validates worker output, marks completed tasks `[x]`, updates `SUMMARY.md`, and commits.
5. **Repeat and finalize.** Bounded additional rounds end in registry aggregation,
   pre-final audit/optional repair, one canonical synthesis, validation, and COMPLETE.

## Troubleshooting

- `claude command not found`, login failures, missing Git/tmux, or missing packaged
  agents/templates: run `slrharness doctor` and fix every hard requirement.
- `agent not found`: rerun the command; workspace assets are deployed automatically.
  Verify `.claude/agents/` rather than copying global files manually.
- MCP disconnected, Scholar throttled, inaccessible pages, or missing Tavily key:
  these are optional-capability warnings. Workers fall back and must write
  limited-access/no-result records instead of claiming the literature is empty.
- Waiting for Scope approval: inspect with `slrharness show --workspace ...`, then
  revise or explicitly approve the printed revision.
- Coordinator PARTIAL or unresolved metadata: inspect the adjacent manifest,
  `task.json`, metadata audit, correction queue, and synthesis limitations; resume
  `slrharness run` to complete only unfinished work.
- Final validation failed: inspect `artifacts/audits/final_audit.json` and rerun
  `slrharness finalize`; only the Finalizer is retried with those diagnostics.
- Stale lock: confirm the PID recorded in `.git/.slrharness.lock` is not running,
  then remove that exact file. Never recursively clean the workspace.
- Resume appears idle: `slrharness status PROJECT` reports approval, pending tasks,
  artifact errors, and finalization phase. COMPLETE projects intentionally do nothing.
- Legacy project: validation warns about missing schemas/manifests but does not
  rewrite or approve it. Use `legacy_worker` for historical topics.

## Development

```bash
uv sync --group dev

uv run pytest                      # run tests
uv run ruff check src/ tests/      # lint
uv run ruff format src/ tests/     # format
```

To add support for a new agent backend, install in dev mode (`uv sync --group dev`) and add a new class in [`src/slrharness/agent_backends.py`](src/slrharness/agent_backends.py). Register it in the `_BACKENDS` dict and it will be available via `--agent-backend`.

## License

MIT — see [LICENCE](LICENCE).
