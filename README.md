# SLRHarness

**Scientific Literature Review Harness** — an artifact-first system for conducting
reproducible literature reviews with CLI agents (Kiro, Claude Code, etc.).

SLRHarness turns an initial research direction into an approved, structured,
version-controlled literature review. A Python orchestrator owns lifecycle state,
validation, retry, and Git boundaries; bounded Claude Code agents own research and
written synthesis.

The current canonical path is state-driven rather than agent-driven. Python owns
`SLR_STATE.json`, task and invocation IDs, staging imports, issue transitions,
artifact compilation, source aggregation, report assembly, and completion. Agents
produce research observations and Markdown content; their reported counts,
statuses, paths, and completion claims are never authoritative.

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

SLRHarness currently invokes Claude Code with
`--dangerously-skip-permissions`, so run it only in a dedicated, disposable
container or VM with restricted filesystem mounts, network egress, and no host
credentials. Prefer a non-root user inside that boundary. Claude Code rejects
this flag for UID 0 unless it is told that an external sandbox already exists;
see [Root users and sandboxed execution](#root-users-and-sandboxed-execution).

Create a scope project from a preliminary direction. This command creates an
independent Git workspace, deploys the project-scoped Claude agents and skill,
runs `slr-scoper`, writes a proposal, and then exits at the approval gate.

```bash
uv run slrharness scope prepare \
  --theme "agent memory" \
  --topic "Agent memory for LLM-based autonomous agents" \
  --agent-backend claude-code
```

It does **not** start the manager or formal workers. Inspect the current state,
proposal, and source-attempt log:

```bash
uv run slrharness scope show \
  --workspace workspaces/agent-memory
```

Request a new, auditable revision when boundaries need adjustment:

```bash
uv run slrharness scope revise \
  --workspace workspaces/agent-memory \
  --feedback "Exclude ordinary RAG and focus on persistent autonomous-agent memory." \
  --agent-backend claude-code
```

Approve the exact revision printed by `scope show`:

```bash
uv run slrharness scope approve \
  --workspace workspaces/agent-memory \
  --revision 2
```

Approval copies the proposal unchanged into `SCOPE.md` and
`SCOPE_ORIGINAL.md`, creates the original manager/worker control files, and
sets `formal_research_allowed` to true. Only then can the existing loop run:

```bash
uv run slrharness run \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --max-rounds 5
```

If scope preparation was interrupted or failed validation, retry the same
revision without losing its state or earlier revision archives:

```bash
uv run slrharness scope resume \
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
uv run slrharness scope prepare \
  --theme "agent memory" \
  --draft-scope my-scope.md \
  --agent-backend claude-code
```

Or explicitly declare the supplied file already approved and skip preliminary
scope research:

```bash
uv run slrharness scope init \
  --theme "agent memory" \
  --approved-scope my-scope.md
```

The historical module command without an `init` subcommand remains supported
and is also treated as an explicit, already-approved input:

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
claude mcp add --transport stdio --scope user scholar -- uvx scholar-mcp
claude mcp add --env TAVILY_API_KEY=YOUR_KEY --transport stdio --scope user tavily -- npx -y tavily-mcp@latest
```

Verify configuration with:

```bash
claude mcp list
claude mcp get arxiv
claude mcp get scholarly
claude mcp get scholar
claude mcp get tavily
```

Inside interactive Claude Code, `/mcp` shows connection status. Basic arXiv
and mcp-scholarly usage normally needs no API key, although Google Scholar
scraping can be unstable. Tavily requires a key and is only the last optional
fallback. Never commit API keys to Git, agent files, prompts, fixtures, logs,
or scope outputs.

The scope agent falls back from available scholarly, arXiv, and scholar tools
to Claude Code WebSearch/WebFetch, then optional Tavily. If all network retrieval fails,
it must still produce a substantive proposal using limited model knowledge,
mark those claims `[UNVERIFIED]`, and record the failures in
`SCOPE_SOURCES.md`. Zero successfully read surveys is valid; it is not evidence
that the field has no literature.

## Coordinated topic execution (Claude Code)

The structured scheduler is the canonical path for newly planned topic tasks;
the original single-worker and coordinator artifacts remain readable for
legacy migration. Run:

```bash
uv run slrharness run \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator \
  --num-workers 3 \
  --coordinator-timeout 3600 \
  --coordinator-retries 1
```

The Python orchestrator is the stage scheduler. It launches the academic and
technical workers concurrently, imports their staging Markdown to canonical
paths, then runs metadata checking, exact-field repair and re-check, and finally
invokes `topic-coordinator` only as a content synthesizer. The coordinator does
not launch agents or own lifecycle decisions.

Checker output is observation data. The Harness converts mismatches into the
single program-owned `artifacts/ISSUES.jsonl` ledger, dispatches exact repairs,
checks the note diff, and exclusively controls `open → repair_dispatched →
applied → verified_closed/unresolved`. Resume state lives in `SLR_STATE.json`.

Each task keeps its Manager-compatible primary synthesis and an adjacent
supporting tree:

```text
SLR_STATE.json                              # canonical lifecycle/task/invocation state
TASKS.md                                    # rendered human-readable projection
artifacts/SCOPE_CONTRACT.json               # approved machine scope
artifacts/ISSUES.jsonl                      # canonical issue event ledger
artifacts/staging/<invocation-id>/           # non-canonical agent output
topics/<topic>/<subtopic>.md                 # topic synthesis
topics/<topic>/<subtopic>/papers/*.md        # canonical paper notes
topics/<topic>/<subtopic>/technical_sources/*.md
topics/<topic>/<subtopic>/task.json           # program-owned topic contract/state
topics/<topic>/<subtopic>/checkpoint.json     # recoverable artifact inventory
topics/<topic>/<subtopic>/coordinator_manifest.json
topics/<topic>/<subtopic>/audits/             # normalization/metadata/correction traces
sections/01-terminology-scope.md             # through section 05
SUMMARY.md                                   # deterministic program assembly
```

For current runs, the Harness generates the per-topic contract, checkpoint,
indexes, audit files, and coordinator manifest from discovered artifacts. These
files remain canonical program output; agents do not own their IDs, counts,
statuses, timestamps, or paths. Legacy agent-authored versions can be imported
as observations and rebuilt. `TASKS.md` is likewise a human-readable projection
of `SLR_STATE.json`, not an independent source of executable work after state
migration.

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
scholarly/arXiv/scholar, then Tavily, then WebSearch/WebFetch; the technical
worker uses Tavily plus WebSearch/WebFetch. No API key is written to project
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
uv run slrharness finalize \
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

The Manager role is reused as a bounded report writer. The program first builds
an explicit report packet from the approved scope, topic syntheses, registry,
paper list, generated references, note roots, and audits. It invokes and
validates the five report sections, then assembles them deterministically with
coverage, provenance, references, and exact delivery statistics into the sole
canonical `SUMMARY.md`. Report-writing invocations have no search responsibility.
The five generated sections are, in order:

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

Section or final-report errors trigger only bounded report-writer retries with
precise diagnostics; completed topic research is not rerun. Warnings are
recorded and may complete by default; use
`--no-allow-complete-with-warnings` for strict handling. Inspect progress and
audits with implemented commands:

```bash
uv run slrharness status workspaces/agent-memory

cat workspaces/agent-memory/artifacts/audits/prefinal_audit.json
cat workspaces/agent-memory/artifacts/audits/final_audit.json
```

To resume an interrupted aggregation, repair, or failed Finalizer, rerun the
same `finalize` command. It rebuilds aggregation safely, does not duplicate
stable repair tasks, preserves failed report drafts under
`artifacts/final_drafts/`, and skips report generation once a valid COMPLETE
state exists.

### Root users and sandboxed execution

Claude Code intentionally rejects `--dangerously-skip-permissions` when the
process runs as root. Current Claude Code builds also recognize
`IS_SANDBOX=1` as an escape hatch for environments that are **already** isolated:

```bash
IS_SANDBOX=1 uv run slrharness run \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator
```

Use this only inside a container or VM whose isolation is independently
enforced. `IS_SANDBOX=1` does not create a sandbox, restrict mounts, filter
network access, or make root safe; it only suppresses Claude Code's root guard.
It is currently an implementation-level compatibility mechanism rather than a
documented stable Claude Code interface, so it may change between releases.
Prefer running Claude Code as a non-root user. If root is unavoidable, mount
only the review workspace, do not mount SSH/cloud credentials or the Docker
socket, restrict outbound network access, and verify the boundary before
setting the variable. See Claude Code's official
[sandboxing](https://code.claude.com/docs/en/sandboxing),
[permissions](https://code.claude.com/docs/en/permissions), and
[development-container](https://code.claude.com/docs/en/devcontainer) guidance.
The current root-guard behavior and escape-hatch condition are also discussed
in the upstream
[Claude Code issue](https://github.com/anthropics/claude-code/issues/58197).

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
.claude/templates/{paper-note,technical-note,report-section-01..05,final-report}.md
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

This creates `workspaces/{theme-slug}/` as an **independent git repository**
(not tracked by the outer SLRHarness repo), copies the approved scope to
`SCOPE_ORIGINAL.md` and `SCOPE.md`, and commits it as `round-0`. Both scope files
are read-only during formal research; proposed future changes remain notes rather
than silently changing the approved contract.

```bash
uv run slrharness scope init \
  --theme "transformer efficiency" \
  --approved-scope my-scope.md
```

Flags:

- `--theme` (required) — free-form; slugified to a directory name
- `--approved-scope` (required) — path to the explicitly approved scope file
- `--workspaces-dir` — parent directory for workspaces (default: `./workspaces`)
- `--force` — overwrite an existing workspace with the same slug

### 4. Run the review loop

```bash
uv run slrharness run \
  --workspace workspaces/transformer-efficiency \
  --agent-backend kiro \
  --max-rounds 5 \
  --num-workers 3 \
  --worker-timeout 1800
```

Historical module entry points remain compatible, but the installed
`slrharness` command is canonical.

Flags:

- `--workspace` (required) — path to the workspace created by `slrharness.scope`
- `--agent-backend` — which agent CLI to use: `kiro` (default) or `claude-code`
- `--max-rounds` — **additional** rounds to run from the current state (default: `5`). When resuming, this is rounds on top of whatever is already committed.
- `--num-workers` — max parallel workers per round (default: `3`)
- `--worker-timeout` — per-worker timeout in seconds (default: `1800`)
- `--manager-timeout` — per-manager-invocation timeout in seconds (default: `3600`)
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

Each round runs as: **manager plan pass → program-scheduled topic stages →
manager review pass**. Python owns lifecycle/task state and re-renders
`TASKS.md`; Manager owns synthesis content and the required Git phase commits.

### Resuming a review

Just re-run the same command on the same workspace. The orchestrator detects completed rounds via the `round-N` git tags and continues from the next one. You can adjust `--max-rounds` on each invocation to run exactly as many additional rounds as you want.

```bash
# Did 10 rounds, now want 5 more
uv run slrharness run \
  --workspace workspaces/transformer-efficiency \
  --max-rounds 5
```

To see where you are before resuming:

```bash
uv run slrharness status workspaces/transformer-efficiency
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
│   │   ├── slr-worker.md           # Legacy Claude topic worker
│   │   ├── topic-coordinator.md     # Validated-note topic synthesis
│   │   ├── academic-paper-worker.md
│   │   ├── academic-metadata-checker.md
│   │   ├── technical-source-worker.md
│   │   ├── slr-manager.json        # Kiro/reference manager config
│   │   └── slr-worker.json         # Kiro/reference worker config
│   ├── skills/                     # Scope and topic research contracts
│   ├── templates/                  # Canonical paper/final report contracts
│   └── config/example-config.json  # Key-free configuration reference
├── src/slrharness/
│   ├── __init__.py
│   ├── agent_backends.py           # Agent platform abstraction & registry
│   ├── orchestrator.py             # Round/topic/finalization scheduler
│   ├── control_plane.py            # Authoritative tasks, invocations, issues
│   ├── artifact_compiler.py        # Staging import and topic normalization
│   ├── report_sections.py          # Section validation and report assembly
│   ├── prioritization.py           # Scope-to-research-line contract
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
│       ├── SLR_STATE.json          # Authoritative lifecycle/task state
│       ├── TASKS.md                # Program-rendered task projection
│       ├── SUMMARY.md              # Canonical assembled report
│       ├── topics/                 # Topic synthesis + supporting evidence
│       ├── artifacts/              # Registry, staging, issues, and audits
│       ├── sections/               # Validated report sections 01–05
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
2. **Plan pass.** The manager proposes bounded topic tasks from the immutable
   approved scope. Python imports them into `SLR_STATE.json`, assigns stable IDs,
   and renders `TASKS.md`; agents cannot change lifecycle state.
3. **Staged topic execution.** Academic and technical retrieval run concurrently
   into invocation staging directories. Python imports and normalizes notes,
   runs metadata observation, owns exact-field repair and re-check, then invokes
   the topic coordinator only for synthesis.
4. **Artifact acceptance and review.** The Harness rebuilds indexes, checkpoint,
   audits, and manifest from actual files. It accepts only deterministically
   validated `COMPLETE` or disclosed `PARTIAL` artifacts; Manager Review then
   updates the evolving synthesis.
5. **Aggregate and finalize.** Bounded rounds end in global source deduplication,
   paper-note validation, pre-final audit/optional repair, five-section report
   generation, deterministic assembly, final validation, and `COMPLETE`.

## Troubleshooting

- `claude command not found`, login failures, missing Git/tmux, or missing packaged
  agents/templates: run `slrharness doctor` and fix every hard requirement.
- Root rejection of `--dangerously-skip-permissions`: prefer a non-root container
  user. Set `IS_SANDBOX=1` only when an external container/VM boundary is already
  enforced; the variable itself provides no isolation.
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
