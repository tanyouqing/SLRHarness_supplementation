# SLRHarness v1 更新总览

本文说明当前第一版相对于最初的 Manager–Worker Harness 增加了哪些阶段、能力和文件，以及这些文件在完整流水线中的职责。

## 1. 更新前后的差异

早期版本以一份人工准备的 `SCOPE.md` 为起点，由 Manager 创建任务、多个 legacy worker 生成 topic Markdown，再由 Manager 汇总。它已经具备 Git round、并行 worker 和基本 resume，但缺少正式的 Scope 审批、论文级证据契约、Topic 内角色分工、全局来源聚合，以及“验证通过后才能完成”的最终交付门禁。

当前 v1 将流程扩展为：

```text
Initial topic
→ bounded Scope research
→ Scope Proposal + Sources
→ explicit user approval/revision
→ Manager Plan
→ one Topic Coordinator per topic
→ Academic Worker + Technical Worker
→ Metadata Checker + bounded correction
→ topic synthesis
→ Manager Review / additional rounds
→ global source aggregation and deduplication
→ pre-final audit
→ optional bounded gap repair
→ five-section English report
→ deterministic final validation
→ COMPLETE
```

Legacy worker 模式仍被保留，但不提供新版 topic 内三角色证据流水线。

## 2. 新增阶段和功能

### 2.1 Scope preparation 与审批门禁

- 用户可以只提交初始研究方向，不必先写完整 Scope。
- `slr-scoper` 执行有界的初步检索并生成 `SCOPE_PROPOSAL.md` 与 `SCOPE_SOURCES.md`。
- 项目停在 `AWAITING_SCOPE_APPROVAL`，未明确批准时不能进入正式研究。
- 支持 revise、reject、重复 approve 幂等处理和 revision archive。
- 正式批准后，`SCOPE.md` 成为只读 canonical scope；Agent 无权修改审批状态。

### 2.2 Topic Coordinator 与三个专门角色

每个 Manager topic task 由一个外部 Coordinator 进程负责。Coordinator 在同一 Claude Code 会话内调度：

- `academic-paper-worker`：论文检索、阅读、citation chaining 和逐篇笔记；
- `technical-source-worker`：官方文档、仓库、博客等非论文技术来源；
- `academic-metadata-checker`：只核查标题、作者、年份、venue、DOI、arXiv ID、版本与重复关系。

Checker 不直接修改论文笔记，而是通过 `correction_requests.jsonl` 提交修订请求，再由 Academic Worker 修改并接受复查。修订轮数有硬上限。

### 2.3 Canonical paper-note contract

每篇纳入论文必须有独立 Markdown note，记录稳定 Paper ID、版本、访问深度、阅读状态、研究方法、实验、定量结果、limitations 和 evidence locator。合理缺失使用 `Not reported`、`Not applicable` 或 `[UNVERIFIED]`，不得猜测补齐。

确定性 validator 检查结构、身份字段、访问状态、数字上下文、note/index 一致性和 corrected metadata 一致性。

### 2.4 Supporting artifact contract

每个 topic 除原兼容 synthesis 外，还必须产生：

```text
topics/<topic>.md
topics/<topic>/
├── task.json
├── papers/index.json 或 papers/NO_RESULTS.md
├── papers/*.md
├── technical_sources/index.json 或 technical_sources/NO_RESULTS.md
├── technical_sources/*.md
├── audits/metadata_check.json
├── audits/correction_requests.jsonl
├── coordination_log.jsonl
└── coordinator_manifest.json
```

Orchestrator 不以 Claude 退出码作为完成依据，而是验证 task ID、路径 containment、文件存在性、counts、metadata 状态、correction queue 和 manifest 状态。

### 2.5 全局来源聚合

所有 topic 完成后，程序单线程生成：

- `artifacts/SOURCE_REGISTRY.json`：全局 academic/technical source registry；
- `artifacts/PAPER_LIST.md`：纳入论文清单；
- `artifacts/REFERENCES.md`：Finalizer 可使用的唯一参考文献来源；
- `artifacts/audits/paper_note_validation.json`：论文笔记验证；
- `artifacts/audits/source_deduplication.json`：跨 topic 去重与版本映射。

论文匹配优先级为 DOI、arXiv ID、显式 version group，最后才是无标识时的精确 title/author 回退。原始 topic notes 不会被删除。

### 2.6 Pre-final audit 与 bounded gap repair

程序先检查 Scope、topic synthesis、manifest、indexes、metadata audit、paper notes 和 registry。Manager 再执行一次不联网的语义覆盖审计。

真正阻塞最终五部分报告的缺口会获得稳定 `GAP-...` ID，并复用原 Manager/Topic Coordinator 机制创建补充任务。默认最多一轮，硬上限三轮，不会形成无限研究循环。

### 2.7 Finalizer 与固定报告结构

复用 `slr-manager` 作为 Finalizer。它只能读取程序列出的 Scope、topic syntheses、notes、registry、references 和 audits，不得重新搜索或加入未审计论文。

Canonical 输出仍为 `SUMMARY.md`，使用英文并固定包含：

1. Academic Terminology and Problem Boundaries；
2. Background, Importance, and Broader Significance；
3. Existing Research: Motivations, Methodologies, and Findings；
4. Research Landscape: Consensus, Differences, and Experimental Practice；
5. Evidence-Backed Research Opportunities；
6. Coverage and Limitations；
7. Sources and Provenance；
8. References；
9. Delivery Status。

### 2.8 Final validation

程序验证 Section 顺序、comparison table、实验比较、consensus、differences、stable source IDs、References、数字引用、占位符、英文程度和 Delivery Status counts。只有 `PASS`，或配置允许的 `PASS_WITH_WARNINGS`，才能进入 `COMPLETE`。

### 2.9 可靠性和运维能力

- JSON schema 统一为 `1.0`；缺失版本作为 legacy v1 警告读取，未知版本拒绝。
- Scope 和 finalization 合法状态转换集中检查。
- 程序管理的 JSON/Markdown 使用同目录临时文件、flush、fsync 和原子替换。
- `.git/.slrharness.lock` 阻止两个 Orchestrator 同时写同一 workspace。
- 支持 `slrharness doctor`、`status`、`validate` 和 `config`。
- Agent 修改 `SLR_STATE.json`、`SCOPE.md` 或 `SCOPE_ORIGINAL.md` 时，程序恢复受保护快照。
- 运行元数据保存在 workspace 的 `.git/slrharness-runs/`，日志长度受限并做基础 secret redaction。
- Agent 被明确要求把论文、网页和 README 视为不可信证据，而不是可执行指令。

## 3. 新增核心文件及作用

### 3.1 Python 模块

| 文件 | 作用 |
|---|---|
| `src/slrharness/cli.py` | 安装后的统一 `slrharness` 命令入口，路由 scope、run、finalize、doctor、status、validate 和 config。 |
| `src/slrharness/config.py` | 加载和验证 JSON/TOML v1 配置，应用集中默认值和范围检查；当前 formal execution 仍主要通过 CLI flags 传参。 |
| `src/slrharness/contracts.py` | 统一 schema version、workspace 路径 containment、原子 JSON/文本写入。 |
| `src/slrharness/diagnostics.py` | 实现环境 doctor 和只读项目一致性验证。 |
| `src/slrharness/project_lock.py` | 实现单 workspace 运行锁和 stale-lock 诊断。 |
| `src/slrharness/source_registry.py` | Paper note 解析/验证、稳定 P/T ID、聚合、去重、Paper List 和 References 生成。 |
| `src/slrharness/finalization.py` | Finalization 配置与状态、pre-final audit、Gap ID、repair task、Finalizer Prompt 和 final report validator。 |

### 3.2 Agent、Skill 与模板

| 文件 | 作用 |
|---|---|
| `plugins/agents/slr-scoper.md` | 有界 Scope 预调研，只写 Proposal 和 Sources。 |
| `plugins/agents/topic-coordinator.md` | 组织一个 topic 内的三个 Subagent 和修订循环，最后写 synthesis/manifest。 |
| `plugins/agents/academic-paper-worker.md` | 逐篇论文检索、阅读、evidence note 和 paper index。 |
| `plugins/agents/academic-metadata-checker.md` | 独立元信息核查和 correction request。 |
| `plugins/agents/technical-source-worker.md` | 非论文技术来源检索、分类和 note/index。 |
| `plugins/skills/slr-scoping/SKILL.md` | Scope Proposal 的研究和内容契约。 |
| `plugins/skills/slr-topic-research/SKILL.md` | Topic 内 paper、technical、metadata、communication、manifest 的共享契约。 |
| `plugins/templates/paper-note.md` | Canonical 单篇论文笔记模板。 |
| `plugins/templates/final-report.md` | Canonical 五部分最终报告模板。 |

原有 `slr-manager.md`、`slr-worker.md`、`orchestrator.py`、`scope_workflow.py`、`topic_execution.py` 和 `workspace_assets.py` 被扩展，而不是建立重复实现。

### 3.3 配置、测试和文档

| 文件 | 作用 |
|---|---|
| `plugins/config/example-config.json` | 不含密钥的完整 v1 配置契约示例。 |
| `tests/test_finalization.py` | Paper contract、registry、prefinal/final validation 和 Finalizer retry 测试。 |
| `tests/test_phase4_integration.py` | schema、锁、配置、Scope→COMPLETE、双 Coordinator 和 Gap repair 集成测试。 |
| `tests/test_live_smoke.py` | 由 `SLRHARNESS_RUN_LIVE_TESTS=1` 显式启用的付费 Claude smoke test。 |
| `docs/architecture.md` | 确定性边界、Agent 边界、流程和降级设计。 |
| `docs/artifact-contracts.md` | 主要 artifact 的 producer、consumer、ownership、validation 和 resume 契约。 |
| `docs/development.md` | 新 Agent/MCP/validator/state/backend 的扩展和测试说明。 |

## 4. 当前验证状态

- Ruff format：通过；
- Ruff lint：通过；
- 离线测试：`87 passed`；
- live Claude test：`1 skipped`，默认不调用付费模型；
- wheel 与 sdist：构建通过；
- 从安装后的 wheel 部署 Claude 资源：`11/11` 通过；
- 未引入 Ralph Loop 或 Claude Code Agent Teams；
- legacy worker 兼容模式仍保留。

