# SLRHarness — Design Document

An agentic system for conducting reproducible literature reviews using CLI agents (Kiro, Claude Code, etc.) as autonomous research workers.

## 1. Motivation

Literature reviews are labor-intensive: a typical systematic review takes 6–18 months with multiple reviewers. SLRHarness automates the mechanical parts — search, extraction, and synthesis — while keeping a human in the loop for scoping and quality judgment.

The system is closer to a **semi-automated rapid review** or **scoping review** than a full systematic review in the Cochrane sense (which requires dual-reviewer screening, inter-rater reliability, and pre-registered protocols). That's a deliberate trade-off: we optimize for speed and breadth over formal rigor. The output is a structured, version-controlled knowledge base — not a publication-ready SLR, but a strong foundation for one.

The architecture is a **manager–worker loop**: a manager agent maintains the protocol, assigns tasks, and synthesizes findings, while worker agents each deep-dive into narrow topics in parallel.

## 2. Methodology

SLRHarness follows a three-phase process informed by Kitchenham's SLR methodology:

| Phase | What happens | Who |
| ----- | ------------ | --- |
| **Planning** — Define research question, criteria, dimensions | Scope agent proposes; human explicitly approves, or supplies an approved scope | Human + scope agent |
| **Conducting** — Search, screen, extract, synthesize iteratively | Manager–worker loop via orchestrator | Agents |
| **Reporting** — Final synthesis with comparison tables | Manager produces final `SUMMARY.md` | Manager |

The conducting phase runs as an iterative loop. Each **round** (not "sprint" — there's no velocity estimation or time-boxing) is one pass through: manager plans → workers research → manager reviews.

## 3. Scope Preparation and Approval

SLRHarness accepts either an initial research direction or an explicitly
approved complete scope. An initial direction enters a program-controlled,
recoverable preparation lifecycle before the existing manager/worker loop:

```text
Initial topic → scope research → SCOPE_PROPOSAL.md + SCOPE_SOURCES.md
              → AWAITING_SCOPE_APPROVAL → explicit revision approval
              → formal SCOPE.md → manager plan
```

The scope agent performs a bounded orientation search and produces content;
Python exclusively owns `SLR_STATE.json`, revision history, validation, and the
approval transition. No manager, formal worker, task registry, topic note, or
summary is created before approval. Missing MCP servers and even zero
successfully retrieved surveys are non-fatal when the proposal discloses the
limitation and marks model-knowledge claims `[UNVERIFIED]`.

Every workspace receives project-scoped Claude definitions under
`.claude/agents/` and the scoping skill under `.claude/skills/`, so the Claude
backend can select `slr-scoper` deterministically with `--agent`. Valid
proposal revisions and source logs are archived under
`scope_revisions/revision-N/`.

For an already complete scope, the user may treat it as a draft and run scope
preparation, or explicitly mark it approved and retain the historical direct
initialization behavior. Approval is never inferred from document length.

### Direct Human Scoping

The user may still write `SCOPE.md` by copying `SCOPE_TEMPLATE.md` and filling it in. They can do this manually, with any LLM, or by using the **scoping skill** (`plugins/skills/slr-scoping/SKILL.md`) which encodes literature review scoping best practices. Direct initialization is an explicit assertion that this supplied scope is already approved.

### Scoping Skill

The scoping skill (`plugins/skills/slr-scoping/SKILL.md`) provides domain knowledge for writing a good scope — it is not just a template but a guide that teaches:

- **Question formulation** — How to structure a research question using the PICo framework (Population, Interest, Context) for scoping reviews, or PICO (Population, Intervention, Comparison, Outcome) for intervention-focused reviews
- **Search string construction** — How to combine seed terms with Boolean operators (AND/OR/NOT), use truncation and wildcards, and adapt queries per database syntax
- **Dimension design** — How to choose meaningful comparison dimensions: prefer observable/extractable properties over subjective judgments, ensure dimensions are orthogonal, include at least one quantitative and one qualitative dimension
- **Criteria calibration** — How to write inclusion/exclusion criteria that are specific enough to be actionable by an agent but broad enough to avoid missing relevant work
- **Database selection** — Which databases cover which domains (e.g., Semantic Scholar for CS breadth, PubMed for biomedical, DBLP for venue-specific coverage)

The skill is loaded on-demand when the user asks for scoping help. It does not run automatically — it's a knowledge resource, not a workflow.

### Required Inputs

- **Research question** — The central question the review aims to answer
- **Key concepts and seed terms** — Initial search vocabulary, synonyms, MeSH terms if applicable
- **Inclusion/exclusion criteria** — Publication date range, study types, languages, domains
- **Comparison dimensions** — The rubric columns for the synthesis table (e.g., method type, dataset used, reported metric, scalability)
- **Ranking & grouping criteria** — How items should be ordered and grouped in `SUMMARY.md`. Can be objective (descending by a numeric dimension), subjective-with-rubric (weighted composite like `0.4*ease-of-access + 0.3*data-size + 0.3*SOTA`), or closeness-to-reference. A concrete scoring rubric is required for subjective orderings so workers can produce consistent scores.
- **Target databases** — Which sources to search (Semantic Scholar, arXiv, PubMed, Scopus, IEEE Xplore, Google Scholar, DBLP)
- **Review depth** — Exhaustive (all results) vs. targeted (top-N per query)

### SCOPE.md Schema

```markdown
# Scope: {Theme Title}

## Research Question
{One clear, answerable question}

## Key Concepts & Search Terms
| Concept | Terms / Synonyms |
| ------- | ---------------- |
| ...     | ...              |

## Inclusion Criteria
- ...

## Exclusion Criteria
- ...

## Comparison Dimensions
| Dimension   | Description | Value Type |
| ----------- | ----------- | ---------- |
| ...         | ...         | ...        |

## Ranking & Grouping Criteria

### Grouping
{How to partition items into sections}

### Primary ordering
{The main ranking criterion — may be numeric, a weighted composite, or closeness-to-reference}

### Notes on subjective scoring
{If using a weighted composite, define each factor's 0-1 rubric precisely}

## Target Databases
- [ ] Semantic Scholar
- [ ] arXiv
- [ ] ...

## Review Depth
{exhaustive | targeted, top-N}

## Notes
{Any additional context}
```

### Scope Preservation

When the workspace is initialized, `scope.py` copies `SCOPE.md` to `SCOPE_ORIGINAL.md`. This original is **never modified by any agent**.

- `SCOPE_ORIGINAL.md` — Immutable snapshot of the human's initial intent.
- `SCOPE.md` — Living document. The manager may append new dimensions or search terms as the review progresses.

The manager agent's prompt includes an explicit constraint: before writing any change to `SCOPE.md`, re-read `SCOPE_ORIGINAL.md` and verify:

1. The original research question is preserved verbatim.
2. All original inclusion/exclusion criteria remain present and unmodified.
3. All original comparison dimensions remain (new ones may be appended, not removed).
4. Any additions are logged in a `## Scope Evolution Log` section at the bottom.

```markdown
## Scope Evolution Log

### Round 2
- Added dimension: "Hardware Requirements" — discovered during GPU benchmark survey
- Added search term: "inference latency" under Concept "Efficiency"
```

## 4. File Structure

### Optional coordinated topic execution

`--topic-execution-mode topic_coordinator` changes only the execution of each
Manager-created topic task. The outer orchestrator still owns scheduling and
starts one top-level process per task, but that process invokes three ordinary
Claude Code subagents inside one session: an academic paper worker and a
technical-source worker in parallel, followed by a metadata-only checker. The
legacy `topics/<topic>/<subtopic>.md` remains the Manager's primary input.

Supporting artifacts live next to it under
`topics/<topic>/<subtopic>/` (`task.json`, stable per-source notes and indexes,
metadata audit, correction queue, coordination log, and coordinator manifest).
The outer orchestrator validates identity, containment, required files, counts,
status, and partial-result disclosure before Manager Review. Accepted tasks are
skipped on resume; failed tasks retain their evidence and receive bounded
retries with prior diagnostics.

No Agent Teams are enabled. Metadata corrections use the durable
`audits/correction_requests.jsonl` queue, after which the coordinator re-invokes
the academic worker and checker within a fixed correction budget.

### Global finalization state machine

The scope lifecycle remains authoritative and approved. A separate
`SLR_STATE.json.finalization` subtree records program-owned progress through
topic completion, source aggregation, pre-final audit, optional bounded repair,
ready/running synthesis, final validation, COMPLETE, or awaiting intervention.
Every phase is restartable; source aggregation is deterministic and finalizer
validation failure retries only the Manager writing phase.

The program builds `artifacts/SOURCE_REGISTRY.json`, `PAPER_LIST.md`, and
`REFERENCES.md` from accepted topic manifests. It validates the bundled
paper-note contract, merges DOI/arXiv/version duplicates while retaining every
note path, and keeps technical sources in a separate registry collection. One
semantic pre-final Manager pass may propose stable gap IDs; at most the
configured repair limit is executed through the existing Topic Coordinator.

Finally, the existing Manager writes canonical `SUMMARY.md` from explicit
persisted inputs without searching. Program validation—not agent exit status—
enforces the ordered five-section English structure, provenance IDs,
references, comparison content, delivery statistics, and warning policy before
the state becomes COMPLETE.

```text
PACKAGE_ROOT/
├── plugins/
│   ├── agents/
│   │   ├── slr-manager.json              # Manager agent config (reference)
│   │   └── slr-worker.json               # Worker agent config (reference)
│   └── skills/
│       └── slr-scoping/
│           └── SKILL.md                   # Scoping best practices skill
├── src/slrharness/
│   ├── __init__.py
│   ├── agent_backends.py             # Agent platform abstraction & registry
│   ├── orchestrator.py               # Main loop + CLI entry point
│   ├── tmux_runner.py                # Reusable tmux spawn/wait utilities
│   ├── scope.py                      # Scope lifecycle CLI + initialization
│   ├── scope_workflow.py             # State, revisions, validation, approval
│   └── workspace_assets.py           # Claude agent/skill deployment
├── workspaces/                        # Gitignored by outer repo
│   └── {theme_slug}/                  # ← Independent git repository
│       ├── .git/
│       ├── .gitignore
│       ├── SCOPE_ORIGINAL.md          # Immutable initial scope
│       ├── SCOPE.md                   # Living scope
│       ├── TASKS.md                   # Task backlog (manager-owned)
│       ├── SUMMARY.md                 # Evolving synthesis
│       ├── assets/                    # Images, PDFs, supplementary files
│       └── topics/                    # Research output
│           ├── {topic_1}/
│           │   ├── _index.md
│           │   └── {subtopic_a}.md
│           └── {topic_2}/
│               └── ...
├── .gitignore                         # Outer repo: excludes workspaces/*/
├── SCOPE_TEMPLATE.md                  # Copy-and-fill template
└── design.md                          # This document
```

### Key naming decisions

- **`TASKS.md`** instead of `TODO.md` — this is the central task registry that drives orchestration, not informal notes.
- **No `Table_of_Contents.md`** — the directory tree under `topics/` *is* the table of contents. The orchestrator can auto-generate an index with `find` if needed; no reason to ask an LLM to maintain what a shell command can produce.
- **No `scripts/`, no `config.py`** — the orchestrator has its own CLI args. One entry point.
- **No `kiro_parallel.py`** — the prototype parallel runner is superseded by `tmux_runner.py`, which adds wait-for synchronization, timeout handling, and cleanup. One place for tmux logic.
- **Scoping skill lives in `plugins/skills/`** — it encodes domain knowledge (PICo framing, search string construction, dimension design) that a template alone can't provide. Users copy or adapt these reference files for their agent platform. Conventions for agents are still inlined in their prompts.

### Workspace Git Isolation

Each `workspaces/{theme_slug}/` is its own git repository, independent of the outer SLRHarness project.

**Initialization** (performed by `scope.py`):

```bash
git init
git add SCOPE_ORIGINAL.md SCOPE.md TASKS.md SUMMARY.md .gitignore
git commit -m "round-0: initialize workspace from scoping"
```

**Per-round commits** (performed by the manager agent):

The manager runs `git add -A` and `git commit -m "round-{N}: {summary}"` as its final action each round. This produces:

```text
round-0: initialize workspace from scoping
round-1: initial task breakdown, 4 topics assigned
round-2: 3 subtopics completed, new dimension "hardware requirements"
round-3: benchmark comparison done, 2 tasks blocked
round-4: final synthesis, all tasks complete
```

Benefits: `git diff round-1..round-2` shows exactly what changed. Workspaces can be pushed to separate remotes. The outer repo stays clean.

### TASKS.md Format

`TASKS.md` is **owned exclusively by the manager agent**. No other agent writes to it.

```markdown
# Tasks: {Theme Title}

## Round 3

### Pending
- [ ] topics/efficiency/pruning — Survey pruning methods for transformer models
- [ ] topics/efficiency/quantization — Compare post-training quantization approaches
- [ ] topics/benchmarks/latency — Collect inference latency benchmarks

### Blocked
- [ ] topics/scaling/multi-gpu — Waiting: no accessible papers found in round 2

## Completed
- [x] topics/efficiency/_index — Overview of efficiency methods (round 1)
- [x] topics/benchmarks/_index — Overview of benchmark landscape (round 2)

## Backlog
- [ ] topics/distillation — Emerged from round 2 findings
```

Standard `[x]` / `[ ]` checkboxes only — no custom markers. Blocked tasks get a text note explaining why.

### Topic Markdown Convention (Obsidian-Compatible)

Each leaf file in `topics/` follows this template. The template emphasizes three things beyond basic content: (1) workspace-context backlinks so readers can jump to the globals, (2) canonical source links (paper, project page, code, dataset) for reproducibility, (3) sibling cross-references so the review reads as a connected body of work, not a pile of disconnected notes.

```markdown
---
title: "{Narrow Topic Title}"
tags: ["{theme}", "{topic}"]
created: 2026-04-27
updated: 2026-04-27
status: draft | review | final
round: {N}
---

# {Narrow Topic Title}

> Workspace context:
> [SCOPE](../../SCOPE.md) -
> [TASKS](../../TASKS.md) -
> [SUMMARY](../../SUMMARY.md)

## Summary
{2–3 sentence summary of findings}

## Key Findings
- ...

## Comparison Data
| Dimension | Value |
| --------- | ----- |
| ...       | ...   |

## Ranking Scores
(Only if SCOPE.md defines ranking criteria. Use the scoring rubric there.)
| Factor | Score | Justification |
| ------ | ----- | ------------- |
| ...    | ...   | ...           |
| Composite | ... | |

## Related Topics
(Sibling files this work connects to. Relative paths. One sentence each.)
- [topics/efficiency/pruning](../efficiency/pruning.md) — {relationship}

## Open Questions
- ...

## Sources
Canonical links for the primary works cited.
- **{Short name / first-author year}**
  - Paper: {DOI or arXiv link}
  - Project page: {URL or "none"}
  - Code: {GitHub URL or "none"}
  - Dataset: {URL or "none"}
  - Notes: {1 sentence on what was used from this source}

## References
- [Author et al., Year](https://doi.org/...)

## Proposed Additions (optional)
{Only if this work uncovered a new dimension, criterion, or topic that should extend SCOPE.md.}
```

**Worker responsibilities around cross-linking:** a worker's primary output is its assigned topic file, but the worker may also append a one-line back-link to sibling files' `## Related Topics` sections when its work connects to them (same method family, same dataset, builds on, contrasts with, etc.). Workers never edit other sections of sibling files — back-links only. This keeps the review a connected graph without introducing edit conflicts.

## 5. Agent Architecture

### 5.1 System Diagram

```text
┌─────────────────────────────────────────────────────────────┐
│                        Human                                │
│  Writes SCOPE.md from template                              │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    scope.py init
                    (git init, copy SCOPE_ORIGINAL.md)
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   Orchestrator (Python)                      │
│                                                             │
│  Round 0: Manager creates initial TASKS.md                  │
│                                                             │
│  for i in range(max_rounds):                                │
│                                                             │
│    ┌──────────────────────────────────────────────┐         │
│    │  Manager — Plan Pass                         │         │
│    │  • Reads SCOPE.md, TASKS.md, SUMMARY.md      │         │
│    │  • Reviews new/changed files in topics/       │         │
│    │  • Updates SUMMARY.md with new findings       │         │
│    │  • Evolves SCOPE.md (preserving original)     │         │
│    │  • Selects pending tasks for this round       │         │
│    │  • git add -A && git commit                   │         │
│    └──────────────────┬───────────────────────────┘         │
│                       │                                      │
│    ┌──────────────────▼───────────────────────────┐         │
│    │  Workers (parallel, one per task)             │         │
│    │  • Reads SCOPE.md for criteria & dimensions   │         │
│    │  • Receives task description via prompt        │         │
│    │  • Searches databases, applies criteria        │         │
│    │  • Writes topic markdown files in topics/      │         │
│    │  • Does NOT touch TASKS.md or SUMMARY.md      │         │
│    └──────────────────┬───────────────────────────┘         │
│                       │                                      │
│    ┌──────────────────▼───────────────────────────┐         │
│    │  Manager — Review Pass                        │         │
│    │  • Checks what workers produced (git status)  │         │
│    │  • Validates output quality                    │         │
│    │  • Updates TASKS.md (mark done / add new)     │         │
│    │  • Updates SUMMARY.md comparison tables        │         │
│    │  • git add -A && git commit                   │         │
│    └──────────────────────────────────────────────┘         │
│                                                             │
│  Final: Manager writes closing SUMMARY.md + git tag         │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 Manager Agent (`slr-manager.json`)

The manager is called **twice per round** with different prompts to reduce cognitive load per invocation:

**Plan pass** — read state, review worker output from previous round, update synthesis, select tasks:

- Read `SCOPE_ORIGINAL.md` + `SCOPE.md` + `TASKS.md` + `SUMMARY.md`
- Check `topics/` for new or modified files since last commit
- Update `SUMMARY.md` comparison tables with new findings, re-sorted per the ranking & grouping criteria in `SCOPE.md`
- Evolve `SCOPE.md` if workers discovered new dimensions (preserving original invariants including the original ranking criteria)
- Select pending tasks from `TASKS.md` for workers this round
- Add new tasks to backlog if gaps are identified
- `git add -A && git commit -m "round-{N}-plan: ..."`

**Review pass** — validate worker output, update task status, commit:

- Check what workers produced (new/modified files in `topics/`)
- Validate completeness: required sections present, dimensions filled in, ranking scores present where required, source links present, workspace-context links resolve, sibling cross-links bidirectional
- Mark completed tasks as `[x]` in `TASKS.md`
- Mark failed/incomplete tasks with a note for retry
- Update `SUMMARY.md` with validated findings, re-sorted per ranking criteria
- `git add -A && git commit -m "round-{N}-review: ..."`

**Key constraint:** The manager never searches databases. All research is delegated to workers.

### 5.3 Worker Agent (`slr-worker.json`)

Each worker receives a single task via its prompt and operates semi-independently — independent in its research, but aware of sibling work.

**Before searching** (required reading):

- `SCOPE.md` — criteria, dimensions, ranking & grouping rules, scoring rubrics
- `TASKS.md` — what other topics exist (completed, in-progress, pending)
- `SUMMARY.md` — the current synthesis; what themes have emerged
- Sibling files under `topics/` — at minimum, titles and summaries of completed ones

This context is not optional. It's what turns the review into a connected body of work rather than a pile of independent notes. The worker identifies which existing topics its new work relates to (same method family, builds on, contrasts with, shared dataset, cites the same reference, etc.) and plans cross-links before writing.

**During research:**

- Searches target databases using web search, web fetch, deep research MCP
- Applies inclusion/exclusion criteria from `SCOPE.md`
- Extracts a value for each comparison dimension
- If `SCOPE.md` defines ranking criteria with a scoring rubric, computes each factor's score per the rubric and reports the composite
- Collects canonical source links per item: paper DOI/arXiv, project page, code repository, dataset URL

**Writing:**

- Primary output: the assigned topic file under `topics/`
- Workspace-context links at the top (`SCOPE`, `TASKS`, `SUMMARY`) using relative paths
- A `## Sources` section with per-item links to paper, project page, code, and dataset
- A `## Related Topics` section pointing to sibling files the work connects to
- Optional `## Proposed Additions` if new dimensions, criteria, or topics emerged

**Sibling back-links:**

For each related existing topic, the worker appends a single line to that file's `## Related Topics` section pointing back to the new work (creating the section if it does not exist). The worker never modifies any other section of a sibling file. This keeps the cross-reference graph bidirectional without introducing merge conflicts: each edit adds a line; none rewrite existing content.

**Key constraints:**

- Workers never write to `TASKS.md`, `SUMMARY.md`, `SCOPE.md`, or `SCOPE_ORIGINAL.md`
- Workers only write within `topics/` and `assets/`
- Edits to sibling topic files are limited to the `## Related Topics` section (append-only)
- The task description is passed entirely via the agent CLI prompt — workers don't parse `TASKS.md` for their own task, they read it to understand context

### 5.4 Tool Access

| Tool               | Manager              | Worker                       |
| ------------------- | -------------------- | ---------------------------- |
| `read` (filesystem) | ✅                    | ✅                            |
| `write` (filesystem)| ✅                    | ✅ (topics/ and assets/ only) |
| `grep` / search     | ✅                    | ✅                            |
| `web_search`         | ❌                    | ✅                            |
| `web_fetch`          | ❌                    | ✅                            |
| `deep_research` MCP | ❌                    | ✅                            |
| `shell`             | ✅ (git only)         | ❌                            |

## 6. Orchestration

The orchestrator is a Python script that drives the loop:

```text
scope prepare → user approval → [manager-plan → workers → manager-review] × N
```

### Execution Flow

```python
def run_slr(workspace: Path, max_rounds: int, num_workers: int, worker_timeout: int):
    """
    0. Verify workspace git repo exists
    1. Manager plan pass: read SCOPE.md, create initial TASKS.md breakdown
    2. For each round:
       a. Orchestrator reads TASKS.md, extracts pending tasks (up to num_workers)
       b. Spawn workers in parallel tmux windows, one task each
       c. Wait for all workers (with timeout)
       d. Manager review pass: validate output, update TASKS.md + SUMMARY.md, commit
       e. Manager plan pass: preserve approved scope, plan next round, commit
    3. Manager writes final SUMMARY.md, orchestrator tags
    """
```

### Task Assignment

The orchestrator (Python) reads `TASKS.md`, extracts pending `- [ ]` lines under the current round, and passes each as a prompt to a worker. This is simple text parsing — no structured format beyond markdown checkboxes.

```python
# Pseudocode
pending_tasks = extract_pending_from_tasks_md(workspace / "TASKS.md")
tasks_this_round = pending_tasks[:num_workers]

for i, task in enumerate(tasks_this_round):
    spawn_worker(
        tmux_session="slr-workers",
        window=f"worker-{i}",
        prompt=f"You are researching: {task.description}. "
               f"Read SCOPE.md for criteria. Write your findings to {task.topic_path}.",
        agent="slr-worker",
    )
```

### Worker Synchronization

Workers signal completion using `tmux wait-for`:

```bash
# Launch: run agent CLI, then signal done
tmux send-keys -t slr-workers:worker-{i} \
  '<agent-cli> <flags> "{task_prompt}"; tmux wait-for -S worker-{i}-done' Enter

# Orchestrator waits with timeout
tmux wait-for worker-{i}-done   # Python subprocess with timeout
```

If a worker times out:

1. Orchestrator captures partial output
2. The manager's review pass will see incomplete files and handle accordingly
3. The task stays as `[ ]` in `TASKS.md` for retry next round

### Round Termination

The loop ends when any of:

- `max_rounds` reached
- No pending tasks remain in `TASKS.md` (all `[x]` or empty backlog)
- Manager's plan pass adds no new tasks (saturation)

## 7. Implementation

### Module Breakdown

| File | Purpose |
| ---- | ------- |
| `src/slrharness/orchestrator.py` | Main loop, CLI entry point (argparse), task extraction |
| `src/slrharness/agent_backends.py` | Agent platform abstraction (protocol, registry, backend implementations) |
| `src/slrharness/tmux_runner.py` | Spawn tmux sessions/windows, wait-for, cleanup |
| `src/slrharness/scope.py` | Scope lifecycle CLI and compatible direct initialization |
| `src/slrharness/scope_workflow.py` | Scope state, revision archive, validation, approval gate |
| `src/slrharness/workspace_assets.py` | Deploy Claude agents and scoping skill into workspaces |
| `plugins/agents/slr-scoper.md` | Claude Code scope-preparation agent |
| `plugins/agents/slr-manager.json` | Manager agent config (platform-agnostic reference) |
| `plugins/agents/slr-worker.json` | Worker agent config (platform-agnostic reference) |
| `plugins/skills/slr-scoping/SKILL.md` | Scoping best practices (PICo, search strings, dimensions) |
| `SCOPE_TEMPLATE.md` | Copy-and-fill template for users |

### Entry Point

```bash
# 1. Create workspace from a filled-in SCOPE.md
python -m slrharness.scope \
  --theme "transformer-efficiency" \
  --scope path/to/my-filled-scope.md

# 2. Run the review loop (default backend: kiro)
python -m slrharness.orchestrator \
  --workspace workspaces/transformer-efficiency \
  --agent-backend kiro \
  --max-rounds 5 \
  --num-workers 3 \
  --worker-timeout 600
```

### Dependencies

Zero pip dependencies beyond the standard library. External tools:

- An agent CLI backend — selected via `--agent-backend` (default: `kiro`). See `src/slrharness/agent_backends.py` for supported backends.
- `tmux` — parallel worker management (`brew install tmux`)
- MCP servers (platform-specific config) if workers need deep research tools

## 8. Quality Controls

### Worker Output Validation (Manager Review Pass)

The manager checks each worker's output against:

- **Completeness** — Does the topic file have all required sections (Summary, Key Findings, Comparison Data, Sources, References, Related Topics)?
- **Criteria adherence** — Were inclusion/exclusion criteria from `SCOPE.md` applied?
- **Dimension coverage** — Are all comparison dimensions from `SCOPE.md` filled in with extracted values (or explicit "not applicable")?
- **Ranking scores** — If `SCOPE.md` defines ranking criteria, is a `## Ranking Scores` section present and consistent with the rubric?
- **Source quality** — Does each cited source have working links? Where applicable, does `## Sources` include paper, project page, code, and dataset links (or explicit "none")?
- **Workspace context links** — Do the top-of-file links to `SCOPE.md`, `TASKS.md`, `SUMMARY.md` resolve with correct relative paths?
- **Cross-linking** — Is there at least one `## Related Topics` entry when a sibling topic shares method family, dataset, citation link, or other clear relationship? Has the worker added the reciprocal back-link in the sibling's file?
- **Novelty** — Does this add information not already in `SUMMARY.md`?

If output fails validation, the manager leaves the task as `[ ]` with a note, or rewrites it with clearer instructions for the next round.

### SUMMARY.md Organization

The manager re-organizes `SUMMARY.md` each round per the ranking & grouping criteria in `SCOPE.md`:

- The `## Comparison Table` is sorted and grouped per the criteria. New items are inserted in their correct position, not appended.
- The `## Findings by Group` narrative mirrors the grouping from `SCOPE.md`.
- When the ranking criteria are a weighted composite, the manager computes scores from each worker's `## Ranking Scores` section and re-sorts on the updated scores.

### Reproducibility

The workspace git history is the audit trail:

- `SCOPE_ORIGINAL.md` committed once at round-0, never modified
- Every round produces two commits (plan + review) with structured messages
- Topic files have frontmatter recording which round produced them
- `git log --oneline` gives the complete review history
- `git diff round-N-plan..round-N-review` shows exactly what workers produced

## 9. Future Extensions

- **Deduplication** — Detect when multiple workers find the same paper
- **Citation snowballing** — Use Semantic Scholar citation API to expand from seed papers
- **Export formats** — Generate LaTeX tables, BibTeX, PRISMA flow diagrams from `SUMMARY.md`
- **Human-in-the-loop checkpoints** — Pause after N rounds for human review
- **Incremental updates** — Re-run with updated scope without redoing completed work
- **Steering files** — Shared conventions in platform-specific config (e.g., `.kiro/steering/`) if the project grows beyond two agents
