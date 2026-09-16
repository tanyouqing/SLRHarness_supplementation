# 使用 Claude Code 安装和运行 SLRHarness

本文给出从空环境到最终 `SUMMARY.md` 的完整操作路径，并说明哪些 Agent、Skill、模板和 MCP 需要用户配置。

## 1. 先看结论：还需要配置什么

| 项目 | 是否必须 | 是否由 Harness 自动处理 |
|---|---:|---:|
| Linux 或 WSL 环境 | 必须 | 否 |
| Python 3.11+ | 必须 | 否 |
| Git | 必须 | 否 |
| tmux | 必须 | 否 |
| Claude Code CLI 和有效登录 | 必须 | 否 |
| `slr-scoper`、`slr-manager`、`topic-coordinator` 等 Agent 文件 | 必须 | 是，创建/恢复 workspace 时自动部署 |
| `slr-scoping`、`slr-topic-research` Skill | 必须 | 是 |
| paper-note、technical-note、分节报告和 final-report 模板 | 必须 | 是 |
| `.claude/settings.json` | 不是启动必需 | 不生成 |
| arXiv MCP | 可选但推荐 | 需要用户配置 |
| scholarly MCP | 可选但推荐 | 需要用户配置 |
| scholar MCP | 可选但推荐 | 需要用户配置（多源聚合检索） |
| Tavily MCP | 可选 | 需要用户配置和认证/Key |
| workspace `.mcp.json` | 不必提供 | 不生成；推荐使用 user-scope MCP |
| `example-config.json` | 仅用于检查/参考 | 随包提供，但不会自动替代 CLI flags |

因此，当前代码不缺 Agent 身份文件。正常的 `prepare`、`init` 和后续 finalization 会把包内资源复制到研究 workspace 的 `.claude/`。真正需要在目标机器上补齐的是 Linux/WSL、tmux、Claude Code 登录，以及你希望使用的可选 MCP。

## 2. 支持环境

Harness v1 的正式运行目标是 Linux 或 WSL，因为并行 Manager/Coordinator 进程依赖 tmux。Claude Code 本身可能支持更多平台，但这不代表 Harness 的 tmux orchestration 已支持原生 Windows。

推荐在专用容器或 VM 中以非 root 用户运行。当前 Claude backend 固定使用
`--dangerously-skip-permissions`；它适合无人值守 Harness，但不适合直接在含有
宿主机凭证和广泛挂载的 root 环境中运行。root 容器的兼容方式及其限制见
下文第 4 节。

以下示例以 Ubuntu/WSL 为准：

```bash
sudo apt update
sudo apt install -y git tmux python3 python3-venv curl
```

安装 `uv`：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
```

## 3. 安装和登录 Claude Code

一种常用安装方式是：

```bash
npm install -g @anthropic-ai/claude-code
claude --version
claude doctor
claude
```

不要对 npm global install 使用 `sudo`。第一次执行 `claude` 时，根据界面完成 Anthropic、Claude subscription 或受支持 provider 的登录。然后验证非交互模式：

```bash
claude -p "Reply with exactly OK"
```

Harness 实际使用
`env CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 claude --dangerously-skip-permissions --agent <name> -p <prompt>`，因为 Orchestrator 需要可设置 timeout、记录 exit code、重试并验证持久化 artifact，而不是等待人工交互。后台等待上限由 Harness 关闭，真正的阶段 timeout 仍由 Harness 自己控制。

Claude Code 官方安装与设置说明：

- [Set up Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started)
- [Claude Code settings](https://code.claude.com/docs/en/configuration)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)

## 4. root、容器与 `--dangerously-skip-permissions`

Claude Code 会拒绝 root/UID 0 直接使用
`--dangerously-skip-permissions`。当前版本的 root guard 会把
`IS_SANDBOX=1` 识别为“调用者已提供外部隔离”的声明。因此，在**已经经过独立
验证的容器或 VM 内**，可以这样启动 Harness：

```bash
IS_SANDBOX=1 uv run slrharness run \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator
```

变量名必须是大写 `IS_SANDBOX`，不是 `insandbox`、`is_sandbox` 或
`IN_SANDBOX`。也可以在容器编排配置中将它作为进程环境变量注入。

但这个方案有三个重要边界：

1. `IS_SANDBOX=1` **不会创建任何 sandbox**，只会绕过 Claude Code 的 root
   启动保护；
2. 它不是 Claude Code 官方文档当前承诺的稳定公共配置，升级 Claude Code 后
   应重新验证；当前 root guard 和该 escape hatch 可参考上游
   [Claude Code issue](https://github.com/anthropics/claude-code/issues/58197)；
3. 即使设置了该变量，root 进程仍拥有容器内的全部权限；错误的 bind mount、
   Docker socket、SSH key、云凭证或开放网络都可能把风险带回宿主机。

因此推荐顺序是：

1. 容器/VM 内使用非 root 用户；
2. 使用 Claude Code 官方 sandbox，并设置 `sandbox.failIfUnavailable: true`，
   防止 sandbox 启动失败后静默降级为非隔离执行；
3. 只有外部隔离已由 Docker/Kubernetes/VM 等机制强制执行、且确实必须以 root
   运行时，才设置 `IS_SANDBOX=1`；
4. 只挂载 review workspace，禁止挂载 `/var/run/docker.sock`、`~/.ssh`、云凭证
   和不必要的宿主机目录，并限制网络出口。

Claude Code 官方明确建议 bypass permissions 只用于 sandboxed container/VM；
同时其内置 sandbox 只直接约束 Bash 及其子进程，权限系统仍负责 Read/Edit/MCP
等工具，因此两层不能相互替代：

- [Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Configure permissions](https://code.claude.com/docs/en/permissions)
- [Development containers](https://code.claude.com/docs/en/devcontainer)

## 5. 安装 SLRHarness

将 `<REPO_URL>` 替换为实际仓库地址：

```bash
git clone <REPO_URL> slrharness
cd slrharness
uv sync --group dev
uv run slrharness --help
uv run slrharness doctor --workspace .
uv run pytest
```

Doctor 的 hard requirements 应全部通过。MCP 缺失只应显示 warning。

如果从 wheel 安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install /path/to/slrharness-0.1.0-py3-none-any.whl
slrharness --help
```

## 6. MCP 应配置在哪里

推荐配置为 Claude Code `user` scope。这样每个新生成的 review workspace 都能使用同一组 MCP，不需要在每个 workspace 中复制 `.mcp.json`，密钥也不会进入研究仓库。

Claude Code 当前有三种常用 MCP scope：

- `local`：只对当前 project path 生效，保存在用户目录的 project state 中；
- `project`：写到项目根目录 `.mcp.json`，可提交给团队，但会要求一次信任确认；
- `user`：对该用户的所有项目生效，保存在 `~/.claude.json`。

Harness 的 Agent frontmatter 使用 `mcp__arxiv__*`、`mcp__scholarly__*`、`mcp__scholar__*` 和 `mcp__tavily__*`，所以服务器名称应严格使用：

```text
arxiv
scholarly
scholar
tavily
```

不要命名为 `arxiv-server` 或 `my-tavily`，否则当前 Agent allowlist 可能匹配不到工具。

Claude Code MCP scope 与命令语法以官方文档为准：[Connect Claude Code to tools via MCP](https://code.claude.com/docs/en/mcp)。

## 7. 配置 arXiv MCP

先确认 `uvx` 可用，然后执行：

```bash
claude mcp add --transport stdio --scope user arxiv -- uvx arxiv-mcp-server
claude mcp get arxiv
```

该 server 是第三方开源项目，不是 Anthropic 内置组件。首次使用前应检查其仓库、版本和安全策略：[blazickjp/arxiv-mcp-server](https://github.com/blazickjp/arxiv-mcp-server)。通常不需要 API Key。

## 8. 配置 scholarly MCP

```bash
claude mcp add --transport stdio --scope user scholarly -- uvx mcp-scholarly
claude mcp get scholarly
```

这也是第三方开源 MCP：[adityak74/mcp-scholarly](https://github.com/adityak74/mcp-scholarly)。基础 arXiv/Scholar 查询通常不需要 Key，但 Google Scholar 抓取可能遇到限流、验证码或空结果。

## 9. 配置 scholar MCP

`scholar` 是多源聚合学术检索（OpenAlex / Semantic Scholar / arXiv / Crossref 等），与 `scholarly` 不是同一个服务器：

```bash
claude mcp add --transport stdio --scope user scholar -- uvx scholar-mcp
claude mcp get scholar
```

上游项目：[Liyux3/scholar-mcp](https://github.com/Liyux3/scholar-mcp)。核心检索通常无需 API Key；可选 `S2_API_KEY`、`OPENALEX_EMAIL` 等提升覆盖与限流余量。

学术侧推荐顺序：`scholarly` → `arxiv` → `scholar` → Tavily → WebSearch/WebFetch；单个 arXiv/scholar 操作连续失败最多 2 次后换源或回退。

## 10. 配置 Tavily MCP

Tavily 不是必需项。优先考虑官方 remote MCP 的 OAuth 模式，避免把 Key 写进命令历史或 URL：

```bash
claude mcp add --transport http --scope user tavily https://mcp.tavily.com/mcp
claude
```

进入 Claude Code 后执行：

```text
/mcp
```

选择 `tavily` 并完成认证。

如果必须使用本地 stdio server，可使用：

```bash
export TAVILY_API_KEY='your-key'
claude mcp add --transport stdio --scope user \
  --env TAVILY_API_KEY="$TAVILY_API_KEY" \
  tavily -- npx -y tavily-mcp@latest
claude mcp get tavily
```

注意：这种写法会把展开后的 Key 保存到用户级 Claude 配置；虽然不会进入 Git，仍应保护 `~/.claude.json`。不要把 Key 写入 `.mcp.json`、`example-config.json`、Agent 文件、测试 fixture 或日志。Tavily 官方实现和认证方式见 [tavily-ai/tavily-mcp](https://github.com/tavily-ai/tavily-mcp)。

## 11. 验证 MCP

```bash
claude mcp list
claude mcp get arxiv
claude mcp get scholarly
claude mcp get tavily
```

再进入项目目录启动交互式 Claude Code：

```bash
claude
```

执行：

```text
/mcp
```

确认所有已配置的 server 为 connected 且能够列出 tools。如果 project-scoped server 被拒绝过，可以检查信任选择；connected 但零工具时，尝试 `/mcp` 中 reconnect 或：

```bash
claude --debug mcp
```

不要在 Doctor 中默认发起真实论文检索；它只检查可见的本地能力状态。

## 12. Agent、Skill 和模板是否需要手工复制

不需要。Harness 在创建或恢复 workspace 时自动部署以下 17 项资源：

```text
.claude/agents/
├── slr-scoper.md
├── slr-manager.md
├── slr-worker.md
├── topic-coordinator.md
├── academic-paper-worker.md
├── academic-metadata-checker.md
└── technical-source-worker.md

.claude/skills/
├── slr-scoping/SKILL.md
└── slr-topic-research/SKILL.md

.claude/templates/
├── paper-note.md
├── technical-note.md
├── report-section-01.md
├── report-section-02.md
├── report-section-03.md
├── report-section-04.md
├── report-section-05.md
└── final-report.md
```

Claude Code 会从项目 `.claude/agents/` 发现 project-scoped subagents。相关官方说明见 [Claude Code configuration scopes](https://code.claude.com/docs/en/configuration) 和 [.claude directory](https://code.claude.com/docs/en/claude-directory)。

`plugins/agents/slr-manager.json` 与 `slr-worker.json` 是 legacy Kiro/reference 文件；以 Claude Code 为 backend 时不需要手工配置它们。

## 13. 从一个初始 topic 创建项目

必须在 SLRHarness 仓库根目录运行：

```bash
uv run slrharness prepare \
  --theme "agent memory" \
  --topic "Agent memory for LLM-based autonomous agents" \
  --workspaces-dir workspaces \
  --agent-backend claude-code
```

该命令会：

1. 创建 `workspaces/agent-memory/`；
2. 初始化独立 Git repository；
3. 自动部署 `.claude/agents`、Skills 和 templates；
4. 运行 `slr-scoper`；
5. 生成 Proposal/Sources；
6. 停在审批门禁。

## 14. 查看、修订和批准 Scope

```bash
uv run slrharness show --workspace workspaces/agent-memory
```

如果需要缩小或调整范围：

```bash
uv run slrharness revise \
  --workspace workspaces/agent-memory \
  --feedback "Exclude ordinary RAG and focus on persistent autonomous-agent memory." \
  --agent-backend claude-code
```

然后再次 `show`，用输出中的当前 revision 明确批准：

```bash
uv run slrharness approve \
  --workspace workspaces/agent-memory \
  --revision 2
```

未批准时运行 formal research 会被程序拒绝。

## 15. 启动完整新版研究流程

以下两个参数不要省略：

```bash
uv run slrharness run \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator \
  --max-rounds 5 \
  --num-workers 3 \
  --coordinator-timeout 3600 \
  --coordinator-retries 1 \
  --max-correction-rounds 2 \
  --max-prefinal-repair-rounds 1 \
  --finalizer-retries 1
```

原因是：formal run 当前为了向后兼容，backend 默认仍可能是 Kiro，topic execution 默认仍是 `legacy_worker`。只有显式选择 Claude Code 与 `topic_coordinator`，才会启用当前完整 v1 topic evidence pipeline。

Finalization 默认开启；没有必要时不需要单独运行 `finalize`。

当前 canonical topic pipeline 由 Python 调度：academic 与 technical worker
并行写 invocation staging，程序导入并规范化 notes，随后串行执行 metadata
observation、精确字段修复与 re-check，最后才让 `topic-coordinator` 基于已验证
notes 写 synthesis。

Agent 的 task ID、路径、计数、状态和完成声明不具有权威性。Harness 根据实际文件
生成 per-topic task contract、indexes、checkpoint、audit 和
`coordinator_manifest.json`。`SLR_STATE.json` 是 task/invocation 状态源，
`TASKS.md` 是其人类可读投影。

## 16. 查看状态和产物

```bash
uv run slrharness status workspaces/agent-memory
uv run slrharness validate workspaces/agent-memory
```

常用产物：

```bash
cat workspaces/agent-memory/SCOPE.md
cat workspaces/agent-memory/TASKS.md
find workspaces/agent-memory/topics -maxdepth 5 -type f
find workspaces/agent-memory/artifacts/staging -maxdepth 3 -type f
cat workspaces/agent-memory/artifacts/SOURCE_REGISTRY.json
cat workspaces/agent-memory/artifacts/audits/prefinal_audit.json
cat workspaces/agent-memory/artifacts/audits/final_audit.json
cat workspaces/agent-memory/SUMMARY.md
```

`status` 和 `validate` 是只读命令，不会重新运行 Agent 或修改状态。

## 17. 中断、恢复和单独重试 Finalizer

Topic 或 Manager 阶段中断后，重新执行同一个 `run` 命令。程序根据 Git round、
`SLR_STATE.json` 中的 task/invocation stage、issue ledger、checkpoint 和已存在的
canonical notes 只恢复未完成工作。进程退出成功并不等于 artifact 成功；反过来，
进程超时或非零退出但留下完整 artifact 时，Harness 可按配置恢复并标记为
`PARTIAL`。

Scope preparation 中断：

```bash
uv run slrharness resume \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code
```

Finalizer 或 final validation 失败：

```bash
uv run slrharness finalize \
  --workspace workspaces/agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator
```

报告阶段先生成并逐节验证 `sections/01-*.md` 到 `sections/05-*.md`，再由程序按固定
顺序组装唯一 canonical `SUMMARY.md`，并追加 coverage、provenance、references 和
精确 delivery statistics。报告阶段重试不会重跑全部 topics；失败草稿保存在
`artifacts/final_drafts/`。

## 18. 配置文件的真实状态

仓库包含：

```text
plugins/config/example-config.json
```

可以验证并查看展开后的默认值：

```bash
uv run slrharness config plugins/config/example-config.json
```

当前 v1 的执行参数仍由 CLI flags 传给 Scope、Topic 和 Finalization dataclass；`run --config ...` 尚未实现。因此不要误以为修改 example config 会自动改变一次运行。该文件现在用于契约说明、类型/边界验证和后续配置入口兼容。

## 19. 当前距离真实完整使用还差什么

代码层面的 Agent、Skill、模板、状态机、验证器和 packaging 已齐全。本文不固化
容易过期的测试通过数量；发布或部署前应在目标 Linux/WSL 环境执行当前 checkout 的
完整测试与 live smoke。要在真实环境完成一次 review，仍需：

1. 在 Linux/WSL 安装 tmux；
2. 在同一 Linux/WSL 环境安装并登录 Claude Code；
3. 至少执行一次 `claude -p` 验证非交互调用；
4. 推荐添加并验证 `arxiv`、`scholarly`、`scholar` MCP；Tavily 可选；
5. 用很小的 topic 运行 opt-in live smoke；
6. 再运行正式 review，并在 Scope gate 明确批准。

当前不需要额外编写 Agent identity 文件，也不需要手工复制 `.claude/agents/`。当前没有必须存在的 MCP 文件：使用 user-scope MCP 时不会在 workspace 产生 `.mcp.json`。

尚未在受支持的 Linux/WSL 环境完成真实 Claude + tmux + MCP 端到端测试，因此生产使用状态应描述为：

```text
代码与离线集成：以当前 checkout 的测试结果为准
安装产物和资源部署：需在目标环境验证
真实 Linux/WSL Claude smoke：NOT TESTED
真实 MCP provider 故障/限流：NOT TESTED
```

## 20. 安全提示

当前 Claude backend 在 `agent_backends.py` 中固定使用
`--dangerously-skip-permissions`。这使无人值守 Agent 可以正常执行，但也扩大了
本机权限风险。首次真实运行应使用独立 workspace、非 root 最小权限账户、无敏感
文件的容器/VM，并先检查 Agent tool allowlist。`IS_SANDBOX=1` 只适用于已经存在的
外部隔离边界，不能把普通 root shell 变成安全 sandbox。若要用于生产或共享机器，
permission mode 和 sandbox policy 应成为显式部署配置，而不是仅依赖固定绕过参数。

不要在 review workspace 中保存 API Key、SSH key、云凭证或其他与研究无关的敏感文件。

