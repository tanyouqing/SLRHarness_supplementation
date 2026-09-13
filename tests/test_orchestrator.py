"""Tests for orchestrator task parsing and helpers."""

from __future__ import annotations

from pathlib import Path

from slrharness.orchestrator import Task, parse_pending_tasks

SAMPLE_TASKS = """\
# Tasks: Test Theme

## Round 2

### Pending
- [ ] topics/efficiency/pruning — Survey pruning methods for transformers
- [ ] topics/efficiency/quantization - Compare PTQ approaches
- [ ] topics/benchmarks/latency: Collect latency benchmarks

### Blocked
- [ ] topics/scaling/multi-gpu — Blocked waiting on paper access

## Completed
- [x] topics/efficiency/_index — Already done (round 1)
- [x] topics/benchmarks/_index — Already done (round 1)

## Backlog
- [ ] topics/distillation/survey — Future work
"""


def test_parse_pending_tasks_picks_up_all_separators(tmp_path: Path) -> None:
    """The parser must accept em-dash, hyphen, and colon as separators."""
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(SAMPLE_TASKS, encoding="utf-8")

    tasks = parse_pending_tasks(tasks_md)
    paths = {t.topic_path for t in tasks}

    assert paths == {
        "topics/efficiency/pruning",
        "topics/efficiency/quantization",
        "topics/benchmarks/latency",
    }


def test_parse_pending_tasks_excludes_blocked_and_backlog(tmp_path: Path) -> None:
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(SAMPLE_TASKS, encoding="utf-8")

    paths = {task.topic_path for task in parse_pending_tasks(tasks_md)}

    assert "topics/scaling/multi-gpu" not in paths
    assert "topics/distillation/survey" not in paths


def test_parse_pending_tasks_uses_highest_numbered_round_only(tmp_path: Path) -> None:
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(
        """\
# Tasks

## Round 8
### Pending
- [ ] topics/current/one -- Current task one
- [ ] topics/current/two -- [mode=topic_coordinator] Current task two

### Blocked
- [ ] topics/current/blocked -- Do not dispatch

## Round 3
### Pending
- [ ] topics/old/task -- Do not dispatch an older round

## Backlog
- [ ] topics/future/task -- Do not dispatch backlog
""",
        encoding="utf-8",
    )

    assert parse_pending_tasks(tasks_md) == [
        Task("topics/current/one", "Current task one"),
        Task("topics/current/two", "Current task two", "topic_coordinator"),
    ]


def test_parse_pending_tasks_does_not_fall_back_to_older_round(tmp_path: Path) -> None:
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(
        """\
## Round 1
### Pending
- [ ] topics/old/task -- Still unchecked historically

## Round 2
### Pending

### Blocked
- [ ] topics/new/blocked -- Not runnable
""",
        encoding="utf-8",
    )

    assert parse_pending_tasks(tasks_md) == []


def test_parse_pending_tasks_stays_inside_latest_round_section(tmp_path: Path) -> None:
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(
        """\
## Round 2
### Completed
- [x] topics/current/done -- Finished

## Manual Queue
### Pending
- [ ] topics/manual/task -- Not part of round 2
""",
        encoding="utf-8",
    )

    assert parse_pending_tasks(tasks_md) == []


def test_parse_pending_tasks_supports_legacy_pending_section(tmp_path: Path) -> None:
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(
        """\
# Tasks
### Pending
- [ ] topics/legacy/pending -- Runnable
### Blocked
- [ ] topics/legacy/blocked -- Not runnable
""",
        encoding="utf-8",
    )

    assert parse_pending_tasks(tasks_md) == [Task("topics/legacy/pending", "Runnable")]


def test_parse_pending_tasks_supports_legacy_flat_file(tmp_path: Path) -> None:
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(
        "# Tasks\n- [ ] topics/legacy/flat -- Runnable\n", encoding="utf-8"
    )

    assert parse_pending_tasks(tasks_md) == [Task("topics/legacy/flat", "Runnable")]


def test_parse_pending_tasks_skips_completed(tmp_path: Path) -> None:
    """Checked [x] items must not appear in the pending list."""
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(SAMPLE_TASKS, encoding="utf-8")

    tasks = parse_pending_tasks(tasks_md)
    paths = {t.topic_path for t in tasks}

    assert "topics/efficiency/_index" not in paths
    assert "topics/benchmarks/_index" not in paths


def test_parse_pending_tasks_preserves_descriptions(tmp_path: Path) -> None:
    """Descriptions are captured verbatim (trimmed)."""
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(SAMPLE_TASKS, encoding="utf-8")

    by_path = {t.topic_path: t.description for t in parse_pending_tasks(tasks_md)}

    assert by_path["topics/efficiency/pruning"] == (
        "Survey pruning methods for transformers"
    )
    assert by_path["topics/efficiency/quantization"] == "Compare PTQ approaches"
    assert by_path["topics/benchmarks/latency"] == "Collect latency benchmarks"


def test_parse_pending_tasks_empty_file(tmp_path: Path) -> None:
    """An empty TASKS.md yields no tasks (not an error)."""
    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text("# Tasks: Nothing yet\n", encoding="utf-8")

    assert parse_pending_tasks(tasks_md) == []


def test_task_is_hashable() -> None:
    """Task must be a frozen dataclass so it can go in sets/dict keys."""
    t1 = Task(topic_path="topics/a", description="x")
    t2 = Task(topic_path="topics/a", description="x")
    assert t1 == t2
    assert len({t1, t2}) == 1


def test_count_completed_tasks_counts_only_x(tmp_path: Path) -> None:
    """count_completed_tasks ignores pending and non-topic lines."""
    from slrharness.orchestrator import count_completed_tasks

    tasks_md = tmp_path / "TASKS.md"
    tasks_md.write_text(SAMPLE_TASKS, encoding="utf-8")

    # SAMPLE_TASKS has exactly two `[x] topics/...` lines.
    assert count_completed_tasks(tasks_md) == 2


def test_count_completed_tasks_missing_file(tmp_path: Path) -> None:
    """count_completed_tasks returns 0 if TASKS.md does not exist."""
    from slrharness.orchestrator import count_completed_tasks

    assert count_completed_tasks(tmp_path / "missing.md") == 0
