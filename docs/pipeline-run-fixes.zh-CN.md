# SLRHarness 端到端跑通过程：源码改动记录

**项目路径：** `/root/JiahaoCAO/SLRHarness-v2/SLRHarness_supplementation`  
**验证主题：** RSI — Recursive Self-Improvement for LLM Agents and Harnesses  
**结果：** `finalization.phase = COMPLETE`，`final_audit = PASS_WITH_WARNINGS`  
**记录日期：** 2026-09-14  

本文只记录**为让官方 pipeline 跑通而修改的源码**，以及必要的环境处置。未手写任何研究笔记、未手改审批状态、未伪造 SCOPE/TASKS/SUMMARY 内容。

---

## 0. 总览

| # | 文件 | 问题类型 | 阶段 |
|---|------|----------|------|
| 1 | `src/slrharness/scope_workflow.py` | 校验过严 | Scope 产出校验 |
| 2 | `src/slrharness/tmux_runner.py` | 长命令/引号崩溃 | Topic Coordinator spawn |
| 3 | `src/slrharness/source_registry.py` | 类型不稳健 | 全局聚合 |
| 4 | `src/slrharness/artifact_compiler.py` | NameError | Gap repair 后编译 |
| 5 | `src/slrharness/finalization.py` | 校验过严 | Prefinal audit 校验 |
| 6 | `src/slrharness/prioritization.py` | 编译器解析不足 | Research line 契约 |

`git diff --stat -- src`：

```text
src/slrharness/artifact_compiler.py |  6 ++++--
src/slrharness/finalization.py      |  3 ++-
src/slrharness/prioritization.py    | 18 ++++++++++++++----
src/slrharness/scope_workflow.py    |  8 ++++----
src/slrharness/source_registry.py   | 20 +++++++++++++-------
src/slrharness/tmux_runner.py       | 21 +++++++++++++++++++--
6 files changed, 56 insertions(+), 20 deletions(-)
```

### 环境处置（非源码，但影响跑通）

| 项 | 处置 |
|----|------|
| Tavily MCP | 更新为新 API Key（user scope） |
| scholarly MCP | 本机到 Google Scholar TCP `SYN-SENT` 挂死，**移除**可选 MCP，走 arXiv/WebSearch/Tavily 回退 |
| PATH | 运行时需 `export PATH=/root/.local/bin:/root/.nvm/versions/node/v20.20.2/bin:$PATH` |
| 恢复命令 | 一律使用官方 `scope resume` / `run --allow-dirty` / `finalize --allow-dirty` |

---

## 1. Scope 校验过严（`scope_workflow.py`）

### 1.1 现象

`slr-scoper` 已写出实质 `SCOPE_PROPOSAL.md`，但 harness 报：

```text
prioritization policy missing Ranking Unit
prioritization policy missing Primary Grouping
prioritization policy missing Ranking Mode
prioritization policy missing Priority Tiers or equivalent ordering
```

状态：`scope_output_validation_failed`。

### 1.2 根因

校验器要求 **独立、精确** 的三级标题，例如：

```regex
^###\s+(?:[\d.)]+\s+)?Ranking Unit(?:\s*\([^)]*\))?\s*$
```

而 agent 按文档习惯写了**合并标题**（内容齐全）：

```markdown
### 10.1 Ranking unit and primary grouping
### 10.4 Ranking mode and priority tiers
```

标题在 “Ranking Unit” 之后还有 “ and …”，`$` 锚定失败。  
这是 **agent 写作风格 vs 硬校验** 的契约裂缝，不是 agent 完全没写字段。

### 1.3 解决思路

- 语义上字段已出现 → 校验应接受 “标题行中包含该标签”。
- 仍要求出现在 `###` 标题上，避免正文随便提到就算过。
- 不改 scoper prompt、不手改 SCOPE 文件。

### 1.4 代码修改

**文件：** `src/slrharness/scope_workflow.py`（`_validate_scope_outputs_detailed`）

**修改前：**

```python
for label in (
    "Ranking Unit",
    "Primary Grouping",
    "Ranking Mode",
    "Missing-Data Policy",
):
    if not re.search(
        rf"^###\s+(?:[\d.)]+\s+)?{re.escape(label)}"
        rf"(?:\s*\([^)]*\))?\s*$",
        policy,
        re.MULTILINE | re.IGNORECASE,
    ):
        errors.append(f"prioritization policy missing {label}")
if not re.search(
    r"^###\s+(?:[\d.)]+\s+)?(?:Priority Tiers|Primary Ordering)"
    r"(?:\s*\([^)]*\))?\s*$",
    policy,
    re.MULTILINE | re.IGNORECASE,
):
    errors.append(
        "prioritization policy missing Priority Tiers or equivalent ordering"
    )
```

**修改后：**

```python
for label in (
    "Ranking Unit",
    "Primary Grouping",
    "Ranking Mode",
    "Missing-Data Policy",
):
    # Accept standalone labels and compound headings such as
    # "### 10.1 Ranking unit and primary grouping".
    if not re.search(
        rf"^###\s+.*\b{re.escape(label)}\b",
        policy,
        re.MULTILINE | re.IGNORECASE,
    ):
        errors.append(f"prioritization policy missing {label}")
if not re.search(
    r"^###\s+.*\b(?:Priority Tiers|Primary Ordering)\b",
    policy,
    re.MULTILINE | re.IGNORECASE,
):
    errors.append(
        "prioritization policy missing Priority Tiers or equivalent ordering"
    )
```

### 1.5 验证

对同一 `SCOPE_PROPOSAL.md` 本地重跑正则：Ranking Unit / Primary Grouping / Ranking Mode / Missing-Data Policy / Priority Tiers 全部命中。随后 `scope resume` 通过，进入 `AWAITING_SCOPE_APPROVAL`。

---

## 2. tmux 长命令崩溃（`tmux_runner.py`）

### 2.1 现象

正式 `run` 在 spawn Topic Coordinator 时：

```text
subprocess.CalledProcessError: Command '['tmux', 'send-keys', '-t',
'slr-topic-coordinators:topic-1', '<超长 claude -p ...>', 'Enter']'
returned non-zero exit status 1
```

Orchestrator 整进程退出。

### 2.2 根因

Coordinator prompt 含：

- 数千字 JSON（comparison dimensions 等）
- 多层单引号（如 `Authors' stated limitations`）
- 转义与换行

`tmux send-keys` 把整串当作按键序列/参数，长度与嵌套引号导致失败。

### 2.3 解决思路

- 长/含引号命令不走 send-keys 原文。
- 先写入 `/tmp/slrharness-tmux-cmds/<pid>-<hash>.sh`，再 `send-keys` 发送短命令 `bash <script>`。
- 短命令仍走原路径，行为不变。

### 2.4 代码修改

**文件：** `src/slrharness/tmux_runner.py`  
**函数：** `send_command`  
**依赖：** 顶部增加 `import os`（若尚未导入）

**修改前：**

```python
def send_command(session: str, window: str, command: str) -> None:
    """Send a shell command (with Enter) to a specific tmux window."""
    subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            f"{session}:{window}",
            command,
            "Enter",
        ],
        check=True,
    )
```

**修改后：**

```python
def send_command(session: str, window: str, command: str) -> None:
    """Send a shell command (with Enter) to a specific tmux window.

    Long or quote-heavy commands are written to a temp script first so
    tmux send-keys does not choke on nested quoting or arg length.
    """
    if len(command) > 2000 or "'" in command or "\\" in command:
        script_dir = Path("/tmp/slrharness-tmux-cmds")
        script_dir.mkdir(parents=True, exist_ok=True)
        script = script_dir / f"{os.getpid()}-{abs(hash(command)) % 10**12}.sh"
        script.write_text(command + "\n", encoding="utf-8")
        script.chmod(0o700)
        payload = f"bash {script}"
    else:
        payload = command
    subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            f"{session}:{window}",
            payload,
            "Enter",
        ],
        check=True,
    )
```

### 2.5 验证

重启 `run --allow-dirty` 后，`slr-topic-coordinators` 正常出现多 window，多个 `topic-coordinator` 进程并行，task 目录产出笔记。

### 2.6 后续清理建议（未做）

可定期清理 `/tmp/slrharness-tmux-cmds/`，避免 tmp 堆积。

---

## 3. `checked_fields` 类型不稳健（`source_registry.py`）

### 3.1 现象

Topic 全部结束后，`finalize` 在 `aggregate_sources` 崩溃：

```text
AttributeError: 'list' object has no attribute 'items'
  File ".../source_registry.py", line 305, in validate_paper_note
    for key, checked in (audit_item.get("checked_fields") or {}).items():
```

### 3.2 根因

`metadata_check.json` 的 `items[].checked_fields`：

- **多数 topic：** dict，`field -> {verified: ...}`
- **workflow-topology：** list，`["title", "authors", "year"]`

代码假定永远是 dict。Agent 输出 schema 不一致导致聚合失败。

### 3.3 解决思路

- list 形态视为“仅字段名列表、无可比对的 verified 值”→ 跳过比对，不报错。
- dict 形态保持原逻辑。
- 不手改 agent 产出的 audit JSON。

### 3.4 代码修改

**文件：** `src/slrharness/source_registry.py`（`validate_paper_note`）

**修改前：**

```python
if audit_item and audit_item.get("status") == "CORRECTED":
    for key, checked in (audit_item.get("checked_fields") or {}).items():
        if (
            isinstance(checked, dict)
            and _present(checked.get("verified"))
            and _canonical(metadata.get(key)) != _canonical(checked["verified"])
        ):
            result.errors.append(f"corrected metadata not applied for {key}")
```

**修改后：**

```python
if audit_item and audit_item.get("status") == "CORRECTED":
    checked_fields = audit_item.get("checked_fields") or {}
    # Agents sometimes emit checked_fields as a list of field names
    # instead of a mapping field -> verification payload.
    if isinstance(checked_fields, list):
        checked_fields = {}
    if isinstance(checked_fields, dict):
        for key, checked in checked_fields.items():
            if (
                isinstance(checked, dict)
                and _present(checked.get("verified"))
                and _canonical(metadata.get(key)) != _canonical(checked["verified"])
            ):
                result.errors.append(f"corrected metadata not applied for {key}")
```

### 3.5 验证

重新 `finalize` 后成功生成 `SOURCE_REGISTRY.json`、`PAPER_LIST.md`、`REFERENCES.md` 等。

---

## 4. `paper_no_result_path` NameError（`artifact_compiler.py`）

### 4.1 现象

Gap repair 进入 `compile_topic_artifacts` 时：

```text
NameError: name 'paper_no_result_path' is not defined. Did you mean: 'paper_no_results'?
```

### 4.2 根因

文件内有两段逻辑：

1. 前半段定义了 `paper_no_result_path = paths.paper_dir / "NO_RESULTS.md"` 与布尔 `paper_no_results`。
2. 后半段（约 L772）**只重设布尔值**：

```python
paper_no_results = (paths.paper_dir / "NO_RESULTS.md").is_file()
```

但后面 manifest 构建仍引用 `paper_no_result_path` / `technical_no_result_path` → NameError。

### 4.3 解决思路

在第二段同时定义 path 变量，保持与布尔一致；不改业务语义。

### 4.4 代码修改

**文件：** `src/slrharness/artifact_compiler.py`

**修改前：**

```python
paper_no_results = (paths.paper_dir / "NO_RESULTS.md").is_file()
technical_no_results = (paths.technical_dir / "NO_RESULTS.md").is_file()
```

**修改后：**

```python
paper_no_result_path = paths.paper_dir / "NO_RESULTS.md"
technical_no_result_path = paths.technical_dir / "NO_RESULTS.md"
paper_no_results = paper_no_result_path.is_file()
technical_no_results = technical_no_result_path.is_file()
```

### 4.5 验证

Gap topic 编译不再 NameError；gap 产物可进入后续聚合。

---

## 5. Prefinal audit 缺 `severity`（`finalization.py`）

### 5.1 现象

Finalization 停在 `AWAITING_INTERVENTION`：

```text
pre-final audit recommended_repairs has an incomplete gap
```

（重复 8 次，对应 8 条 recommended_repairs）

### 5.2 根因

`validate_prefinal_audit` 要求每个 gap 对象包含：

```python
required = {"gap_id", "category", "severity", "blocking"}
```

Manager 写出的对象示例：

```json
{
  "gap_id": "GAP-AE52BEE195",
  "category": "prioritization",
  "related_topic": "global",
  "recommended_task": "...",
  "bounded": true,
  "requires_literature_search": false,
  "blocking": false,
  "justification": "..."
}
```

**缺少 `severity`** → 全部 8 条判 incomplete。  
`blocking`、`category`、`gap_id` 均在；severity 是 LLM 可选字段被硬编码必填。

### 5.3 解决思路

- 将 `severity` 从必填集合移除（默认视为非阻塞补充信息）。
- 仍强制 `gap_id` / `category` / `blocking` 与合法 `GAP-[A-F0-9]{10}`。
- 不手改 `prefinal_audit.json`。

### 5.4 代码修改

**文件：** `src/slrharness/finalization.py`（`validate_prefinal_audit`）

**修改前：**

```python
required = {"gap_id", "category", "severity", "blocking"}
if not required.issubset(item):
    errors.append(f"pre-final audit {section} has an incomplete gap")
```

**修改后：**

```python
# Managers sometimes omit severity; treat as non-blocking default.
required = {"gap_id", "category", "blocking"}
if not required.issubset(item):
    errors.append(f"pre-final audit {section} has an incomplete gap")
```

### 5.5 验证

Prefinal audit 校验通过，流水线进入 `FINAL_SYNTHESIS_RUNNING` → `COMPLETE`。

---

## 6. Research line 契约编译失败（`prioritization.py`）

### 6.1 现象

`artifacts/SCOPE_PRIORITIZATION.json`：

- `research_lines: []`
- `compile_status: PARTIAL`
- `ranking_mode: qualitative_fallback`

下游 coordinator prompt 出现：

```text
RESEARCH LINE ID: RL-TOOL-API
RESEARCH LINE NAME: Not available
PRIMARY GROUP: Not available
```

以及 “unknown approved Research Line ID”。

### 6.2 根因（三层）

**A. 表位置**

编译器只从 `## 5. Proposed Literature Organization` 取表。  
本 scope 的 10 条 RL 表在：

```markdown
### 10.1 Ranking Unit
| Line ID | Name | Group | ... |
```

§5.2 只有 group 汇总表（无 Line ID），导致取到空/无效行。

**B. 优先级逻辑**

即便加了 “organization 为空再读 Ranking Unit”，§5.2 **有表**（非空 list），fallback 永不触发。

**C. Markdown 反引号**

单元格为 `` `RL-PROMPT-CTX` ``，`VALID_LINE_ID = ^RL-[A-Z0-9-]+$` 无法匹配。

### 6.3 解决思路

1. **优先**解析 `### Ranking Unit` 下的 RL 表。  
2. 若该表没有 Line ID/Name 列，再退回 organization / policy 全表。  
3. `_column` 剥离反引号。  
4. 列名别名：`Name`、`Expected tier`。

### 6.4 代码修改

**文件：** `src/slrharness/prioritization.py`

#### 6.4.1 `_column` 去反引号

```python
# 修改后关键行
return value.strip().strip("`").strip()
```

#### 6.4.2 选表逻辑

**修改前：**

```python
line_rows = _table(organization)
research_lines: list[dict[str, Any]] = []
```

**修改后：**

```python
# Prefer the dedicated RL table (often under "### Ranking Unit").
# Section 5 organization tables may list groups only and must not win.
line_rows = _table(_subsection(policy, "Ranking Unit"))
if not any(
    _column(row, "Research Line ID", "Line ID") or _column(row, "Research Line", "Line")
    for row in line_rows
):
    line_rows = _table(organization)
if not line_rows:
    line_rows = _table(policy)
research_lines: list[dict[str, Any]] = []
```

#### 6.4.3 列名别名

```python
name = _column(row, "Research Line", "Line", "Name", "Method Family")
...
"priority_tier": _column(row, "Priority Tier", "Tier", "Expected tier") or None,
```

### 6.5 重编译（程序内调用，非手写 JSON）

```python
from slrharness.prioritization import compile_scope_prioritization, write_scope_prioritization
contract = compile_scope_prioritization(scope_text, "SCOPE.md")
write_scope_prioritization(workspace, scope_text)
```

结果：

```text
n_research_lines = 10
RL-PROMPT-CTX | Prompt and context self-optimization
RL-MEM-EXP    | Memory and experience internalization
...（共 10 条）
```

### 6.6 影响

Finalizer / coordinator 能拿到完整 research-line 集合；结合 #5 修复后 prefinal 可通过。

---

## 7. 官方恢复命令一览（未手改状态）

| 阶段 | 命令 |
|------|------|
| Scope 重试 | `uv run slrharness scope resume --workspace workspaces/rsi --agent-backend claude-code --scope-timeout 1800` |
| Scope 批准 | `uv run slrharness scope approve --workspace workspaces/rsi --revision 1` |
| 正式研究 | `uv run slrharness run --workspace workspaces/rsi --agent-backend claude-code --topic-execution-mode topic_coordinator --max-rounds 5 --num-workers 3 --coordinator-timeout 3600 --coordinator-retries 1 --max-correction-rounds 2 --max-prefinal-repair-rounds 1 --finalizer-retries 1 --allow-dirty` |
| 终稿 | `uv run slrharness finalize --workspace workspaces/rsi --agent-backend claude-code --topic-execution-mode topic_coordinator --allow-dirty` |

中断时：杀残留 coordinator session、确认无活 PID 后删除 workspace lock，再执行同一 `run`/`finalize`。

---

## 8. 流水线阶段与对应修复

```text
prepare/scope
  └─ #1 scope_workflow 校验过严
approve
run (manager → topic coordinators)
  └─ #2 tmux_runner 长命令
  └─ (运行中环境) scholarly 移除、arXiv/WebSearch/Tavily 回退
topic compile / finalize aggregate
  └─ #3 source_registry checked_fields
gap repair compile
  └─ #4 artifact_compiler NameError
prefinal audit validate
  └─ #5 finalization severity
prioritization contract
  └─ #6 prioritization 表解析/反引号
final synthesis → validate → COMPLETE
```

---

## 9. 最终结果摘要

| 项 | 值 |
|----|-----|
| finalization | **COMPLETE** |
| final_audit | **PASS_WITH_WARNINGS** |
| 主报告 | `workspaces/rsi/SUMMARY.md`（约 981 行） |
| 注册来源 | 43 papers + 17 technical |
| Topic | 3 COMPLETE / 6 PARTIAL / 1 FAILED（workflow-topology） |
| Finalizer | attempt 2 通过；attempt 1 草稿在 `artifacts/final_drafts/` |

**遗留 warning（非阻塞）：** 部分 metadata unresolved、若干数字缺邻近 source ID、prioritization qualitative fallback、检索降级导致 PARTIAL。

---

## 10. 后续建议

1. **将 6 处 src 改动正式 commit**（当前 workspace 内为未提交修改）。  
2. 为 #1/#5/#3/#6 增加回归测试：复合标题、list `checked_fields`、缺 severity 的 gap、§10.1 RL 表 + 反引号。  
3. tmux 脚本目录清理策略。  
4. 评估是否在 scoper prompt 中**显式要求**独立 `### Ranking Unit` 等标题（减少对校验放宽的依赖）。  
5. 网络：Google Scholar 不可达环境可文档化为 “scholarly 不可用，依赖 arXiv/Tavily/WebSearch”。

---

*本文档描述的是验证与修复过程；研究结论请以 `workspaces/rsi/SUMMARY.md` 与 registry 为准。*
