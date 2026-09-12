# SLRHarness

**Scientific Literature Review Harness** — an agentic system for conducting reproducible literature reviews using CLI agents (Kiro, Claude Code, etc.) in a manager–worker loop.

SLRHarness turns a filled-in scope document into a structured, version-controlled knowledge base. A **manager agent** plans, synthesizes, and commits; **worker agents** search databases in parallel and produce narrow topic notes. A lightweight Python orchestrator drives the loop and records each round as git commits.

For the architecture, design rationale, and methodology, see [`docs/design.md`](docs/design.md).

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
- [`tmux`](https://github.com/tmux/tmux) — required for the formal
  manager/worker rounds. Run SLRHarness on Linux, macOS, or WSL; this release
  does not add native Windows orchestration support.
- An agent CLI backend (at least one):
  - [`kiro-cli`](https://kiro.dev/docs/cli/) — Kiro agent runtime (authenticated), or
  - [`claude`](https://docs.anthropic.com/en/docs/claude-code) — Claude Code CLI
- `git` on PATH

## Installation

```bash
git clone https://github.com/yourname/slrharness.git
cd slrharness
uv sync --group dev
```

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

### Scope workspace files and discovery

Before approval, a new workspace contains:

```text
INITIAL_TOPIC.md
SCOPE_PROPOSAL.md
SCOPE_SOURCES.md
SLR_STATE.json
scope_revisions/revision-N/
.claude/agents/{slr-scoper,slr-manager,slr-worker}.md
.claude/skills/slr-scoping/SKILL.md
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
├── src/slrharness/
│   ├── __init__.py
│   ├── agent_backends.py           # Agent platform abstraction & registry
│   ├── orchestrator.py             # Main loop, CLI, task parsing
│   ├── scope_workflow.py            # Scope state, revisions, approval gate
│   ├── tmux_runner.py              # Parallel spawn + wait-for sync
│   ├── workspace_assets.py          # Workspace agent/skill deployment
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
├── docs/
│   └── design.md                   # Full architecture document
├── SCOPE_TEMPLATE.md               # Copy-and-fill scope template
├── README.md                       # This file
└── pyproject.toml
```

## How it works

1. **Scope preparation and approval.** Start from a topic or draft, review and
   explicitly approve the generated proposal; or explicitly initialize an
   already-approved scope. Python owns the approval gate and preserves the
   approved content as `SCOPE_ORIGINAL.md`.
2. **Plan pass.** The manager agent reads the scope, reviews new files in `topics/`, updates `SUMMARY.md`, optionally evolves `SCOPE.md` (subject to invariants in the design doc), and writes pending tasks to `TASKS.md`.
3. **Worker dispatch.** The orchestrator parses `TASKS.md`, extracts up to `--num-workers` pending `- [ ]` lines, and launches each as an agent CLI session in its own tmux window. Workers signal completion via `tmux wait-for` channels.
4. **Review pass.** The manager validates worker output, marks completed tasks `[x]`, updates `SUMMARY.md`, and commits.
5. **Repeat** until `--max-rounds` reached, no pending tasks remain, or manager signals saturation.

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
