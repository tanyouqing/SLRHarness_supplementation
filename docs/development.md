# Development guide

- Add an Agent under `plugins/agents/`, add its filename to
  `CLAUDE_AGENT_FILES`, constrain its tools, and test workspace deployment.
- Add an MCP provider only as an optional Agent tool plus an explicit fallback;
  never make credentials or connectivity a program import requirement.
- Add deterministic validators beside the artifact owner and compose them in
  `diagnostics.validate_project`. Validators must not call agents or networks.
- Add lifecycle phases to the centralized phase constants and transition map,
  then define interruption, retry, and artifact reconciliation behavior.
- Fake backends implement the small `AgentBackend` protocol. Unit and offline
  integration tests monkeypatch process boundaries and write realistic artifacts.

Run the default, free offline suite with:

```bash
uv sync --group dev
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest
uv build
```

Live tests must be explicitly enabled with `SLRHARNESS_RUN_LIVE_TESTS=1`; they
may use a logged-in Claude Code account and incur model/network cost. Default
tests never do. Inspect packaged resources by listing the wheel and by creating
a temporary workspace from an installed wheel.

For schema evolution, keep readers compatible with missing-version legacy v1
artifacts, reject unknown versions, never reinterpret a field silently, and add
fixtures for both the old and current representation. Avoid global mutable state,
shell command construction, unbounded loops, and secrets in fixtures or logs.
