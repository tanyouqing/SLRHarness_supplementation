# SLRHarness 端到端 Demo 录制方案

**目标：** 在视频中完整演示「从 topic 定义 → Scope 审批 → 正式调研 → 最终五段报告」的 pipeline。  
**原则：** 小规模、可预期、少等待；全程用 tmux，SSH 断线不影响。

---

## 0. Demo 叙事（建议 12–18 分钟）

| 段落 | 时长 | 你要讲什么 |
|------|------|------------|
| A. 项目是什么 | 1–2 min | Manager–Worker 文献综述编排；程序管状态，Agent 管内容 |
| B. 环境就绪 | 1 min | doctor / MCP / tmux |
| C. Scope 阶段 | 3–4 min | prepare → 看提案 → approve |
| D. 正式调研 | 4–6 min | run；看 topic 笔记与状态 |
| E. Finalize | 3–4 min | 五段报告 + registry |
| F. 结果解读 | 2 min | SUMMARY 结构、限制披露、可复现性 |

**总时长控制：** 准备 5 分钟 + 录制 15 分钟；正式 run 可用「已跑通的 workspace」做结果展示，或现场只跑 1–2 个 topic。

---

## 1. 录制前准备（不进正片或快速带过）

### 1.1 服务器

```bash
ssh root@<SERVER>
export PATH=/root/.local/bin:/root/.nvm/versions/node/v20.20.2/bin:$PATH
export HOME=/root
cd ~/JiahaoCAO/SLRHarness-v2/SLRHarness_supplementation
```

### 1.2 代码版本（必须）

```bash
git log --oneline -3
# 期望包含: verified agent-memory pipeline / merge origin/main
git status -sb
# 期望 clean，与 origin/main 一致
```

### 1.3 体检

```bash
uv run slrharness doctor
claude mcp list
# arxiv / tavily: Connected
tmux -V
```

### 1.4 清出干净 workspace（正片从这里开始）

```bash
# 若演示新主题，勿删已有成果；用新 theme
rm -rf workspaces/demo-agent-memory
```

---

## 2. 正片脚本（按镜头）

### 镜头 1：开场 — 项目结构（30s）

**画面：** 终端 + 简短 README/架构图

**口播要点：**
- SLRHarness = 可复现文献综述 harness  
- Scope 有审批门禁；正式研究按 research line 拆 topic  
- 最终产出机器校验过的 `SUMMARY.md`

---

### 镜头 2：启动 tmux 会话（20s）

```bash
tmux new -s demo
# 全程在此会话内操作；可演示断开 SSH 后 tmux attach 仍活着
```

---

### 镜头 3：Scope prepare（关键命令）

```bash
export PATH=/root/.local/bin:/root/.nvm/versions/node/v20.20.2/bin:$PATH
cd ~/JiahaoCAO/SLRHarness-v2/SLRHarness_supplementation

uv run slrharness prepare \
  --theme demo-agent-memory \
  --topic 'Persistent memory for LLM-based agents: write, consolidate, and retrieve loops inside agent harnesses' \
  --workspaces-dir workspaces \
  --agent-backend claude-code \
  --target-surveys 1 \
  --max-initial-candidates 2 \
  --scope-timeout 1800
```

**预期日志：**
- 创建 `workspaces/demo-agent-memory/`
- `slr-scoper` 开始检索（WebSearch / arXiv / Tavily）
- 约 5–15 分钟后：

```text
Scope status: AWAITING_SCOPE_APPROVAL
```

**视频演示：**
```bash
ls workspaces/demo-agent-memory
# INITIAL_TOPIC.md  SCOPE_PROPOSAL.md  SCOPE_SOURCES.md  SLR_STATE.json
python3 -c "import json;print(json.load(open('workspaces/demo-agent-memory/SLR_STATE.json'))['status'])"
# AWAITING_SCOPE_APPROVAL
```

**旁白：** 程序强制停在审批门禁，Agent 不能自己批准。

---

### 镜头 4：审阅 Scope（1–2 min）

```bash
uv run slrharness scope show --workspace workspaces/demo-agent-memory
# 或
sed -n '1,80p' workspaces/demo-agent-memory/SCOPE_PROPOSAL.md
grep -E '^## |^### ' workspaces/demo-agent-memory/SCOPE_PROPOSAL.md | head -40
```

**视频要点（选 2–3 处念）：**
- 主研究问题  
- In scope / Out of scope  
- Research lines 列表（数量适中，如 6–12 条）  

**预期：** 内容实质、可审批；`SCOPE_SOURCES.md` 记录检索与失败。

---

### 镜头 5：Approve

```bash
uv run slrharness scope approve \
  --workspace workspaces/demo-agent-memory \
  --revision 1
```

**预期：**
```text
Approved scope revision 1.
```
状态变为 `SCOPE_APPROVED`，出现 `SCOPE.md`、`SCOPE_ORIGINAL.md`。

```bash
python3 -c "import json;d=json.load(open('workspaces/demo-agent-memory/SLR_STATE.json'));print(d['status'], d['approved_revision'], d['formal_research_allowed'])"
# SCOPE_APPROVED 1 True
```

---

### 镜头 6：正式 run（tmux 持久化）

**说明：** 用小预算；若只想 demo 速度，可 `--max-rounds 1`（每轮 1 worker 只做 1 个 topic）。

```bash
uv run slrharness run \
  --workspace workspaces/demo-agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator \
  --max-rounds 3 \
  --num-workers 1 \
  --worker-timeout 1800 \
  --coordinator-timeout 1800 \
  --target-papers 2 \
  --max-paper-candidates 4 \
  --target-technical-sources 1 \
  --max-technical-candidates 2 \
  --coordinator-retries 1 \
  --max-correction-rounds 1 \
  --max-prefinal-repair-rounds 0 \
  --finalizer-retries 1 \
  --allow-dirty
```

**视频中并行展示：**
```bash
# 另开 pane 或稍后回看
tmux ls
# demo / slr-manager / slr-topic-coordinators

watch -n 5 'python3 - <<'"'"'PY'"'"'
import json
from pathlib import Path
d=json.loads(Path("workspaces/demo-agent-memory/SLR_STATE.json").read_text())
tasks=(d.get("control") or {}).get("topic_tasks") or {}
from collections import Counter
print(Counter((t or {}).get("status") for t in tasks.values()))
for t in tasks.values():
    if isinstance(t,dict) and t.get("status")!="PENDING":
        print(t.get("topic_path"), t.get("status"), t.get("stage"))
PY'
```

**预期现象：**
1. Manager 写 `TASKS.md`（每条 research line ≈ 一个 topic）  
2. 出现 `topics/...` 目录与 `papers/*.md`  
3. 状态：PENDING → RUNNING → COMPLETE  
4. `artifacts/ISSUES.jsonl`、staging 有内容  

**旁白强调：**
- `--target-papers 2` 是**每 topic** 深度  
- `--max-rounds` × `--num-workers` 决定本轮能做完几个 topic  
- tmux 保证 SSH 断了 run 仍继续  

---

### 镜头 7：（可选）中断与恢复

```bash
# Ctrl+B 然后 D 断开 tmux；再
tmux attach -t demo
# 或杀 SSH 再 attach，展示任务仍在
```

若要展示 resume：结束一轮后再次执行相同 `run` 命令。

---

### 镜头 8：Finalize（有 COMPLETE topic 后即可）

**方式 A —** `run` 在 pending 为空时自动 finalize  

**方式 B —** 提前用已有完成 topic 收尾（demo 更可控）：

```bash
# 先停 run（若仍在跑）
# pkill -f 'slrharness run --workspace workspaces/demo-agent-memory'

uv run slrharness finalize \
  --workspace workspaces/demo-agent-memory \
  --agent-backend claude-code \
  --topic-execution-mode topic_coordinator \
  --worker-timeout 1800 \
  --coordinator-timeout 1800 \
  --finalizer-timeout 3600 \
  --max-prefinal-repair-rounds 0 \
  --allow-finalize-with-limitations \
  --allow-complete-with-warnings \
  --allow-dirty
```

**预期阶段日志 / 状态：**
```text
SOURCE_AGGREGATION
PREFINAL_AUDIT
READY_FOR_FINAL_SYNTHESIS
FINAL_SYNTHESIS_RUNNING
FINAL_VALIDATION
COMPLETE
```

**产物：**
```bash
ls workspaces/demo-agent-memory/artifacts
# SOURCE_REGISTRY.json  PAPER_LIST.md  REFERENCES.md  audits/  sections/
ls workspaces/demo-agent-memory/sections
# 01-terminology-scope.md ... 05-future-directions.md
wc -l workspaces/demo-agent-memory/SUMMARY.md
```

---

### 镜头 9：解读最终报告（2 min）

```bash
grep -E '^## ' workspaces/demo-agent-memory/SUMMARY.md
python3 - <<'PY'
import json
from pathlib import Path
r=json.loads(Path('workspaces/demo-agent-memory/artifacts/SOURCE_REGISTRY.json').read_text())
print('papers', len(r.get('papers') or []), 'technical', len(r.get('technical_sources') or []))
a=json.loads(Path('workspaces/demo-agent-memory/artifacts/audits/final_audit.json').read_text())
print('final_audit', a.get('status'), 'errors', a.get('errors'))
PY
```

**视频中打开 3 处：**
1. Section 1 术语表  
2. Section 3 research-line 优先级表  
3. Delivery Status / Coverage and Limitations  

**口播：**
- 报告由**分节模板写出 + 程序组装**  
- 引用 ID 必须来自 registry  
- 不完整处写入 limitations，而不是静默成功  

---

## 3. 预期结果一览（给导师的 checklist）

| 阶段 | 成功标志 |
|------|----------|
| prepare | `AWAITING_SCOPE_APPROVAL` + 两份 SCOPE_*.md |
| approve | `SCOPE_APPROVED`，`formal_research_allowed=true` |
| run | TASKS 被规划；≥1 topic COMPLETE；有 papers 笔记 |
| finalize | `finalization.phase=COMPLETE` |
| 报告 | `SUMMARY.md` 五段齐全；`final_audit` 无 error 或仅 warnings |
| 聚合 | `SOURCE_REGISTRY.json` 中 papers/technical 计数 > 0 |

---

## 4. 录制技巧

1. **字号放大**（≥16pt），tmux 状态栏可显示 session 名  
2. **先跑通一次**再正式录，避免 arXiv 429 占满时间  
3. Scope prepare 可 **提前录好 B-roll**，正片用剪辑  
4. 正式 run 展示 1 个 topic 从 RUNNING→COMPLETE 即可，不必等 10 个  
5. 旁白固定三句金句：  
   - 「Agent 不能自己批准 Scope」  
   - 「完成以 artifact 校验为准，不以 exit code 为准」  
   - 「限制会写进报告，而不是假装完美」  

---

## 5. 推荐 Demo 配置（最省时）

| 参数 | Demo 值 | 原因 |
|------|---------|------|
| theme | `demo-agent-memory` | 与正式结果隔离 |
| target-surveys | 1 | Scope 快 |
| max-rounds | 1–3 | 控制正式调研时长 |
| target-papers | 2 | 单 topic 可控 |
| prefinal repair | 0 | 避免 gap repair 再拖长 |
| finalize | 显式 B 方式 | 不依赖 pending 清空 |

**若必须 15 分钟内出最终报告：**  
Scope 用已有 `agent-memory` 的 SCOPE 只做 approve 演示；正式 run 只做 1 topic；然后 finalize。叙事上仍完整。

---

## 6. 应急台词（现场卡住时）

| 现象 | 可说 |
|------|------|
| arXiv 429 / 超时 | 「检索降级：Tavily / WebSearch；失败会记入 SCOPE_SOURCES」 |
| topic PARTIAL | 「PARTIAL 是合法终态：证据不足会披露，不是失败掩盖」 |
| finalize 很慢 | 「五段报告 + 校验；可先看 sections/ 单节文件」 |
| SSH 断开 | 「任务在 tmux 里，attach 即可」 |

---

## 7. 一键命令清单（复制用）

```bash
# 0) 环境
export PATH=/root/.local/bin:/root/.nvm/versions/node/v20.20.2/bin:$PATH
cd ~/JiahaoCAO/SLRHarness-v2/SLRHarness_supplementation
tmux new -s demo
uv run slrharness doctor

# 1) Scope
uv run slrharness prepare --theme demo-agent-memory \
  --topic 'Persistent memory for LLM-based agents: write, consolidate, and retrieve loops inside agent harnesses' \
  --workspaces-dir workspaces --agent-backend claude-code \
  --target-surveys 1 --max-initial-candidates 2 --scope-timeout 1800

# 2) 审阅 + 批准
uv run slrharness scope show --workspace workspaces/demo-agent-memory
uv run slrharness scope approve --workspace workspaces/demo-agent-memory --revision 1

# 3) 正式调研
uv run slrharness run --workspace workspaces/demo-agent-memory \
  --agent-backend claude-code --topic-execution-mode topic_coordinator \
  --max-rounds 3 --num-workers 1 --worker-timeout 1800 --coordinator-timeout 1800 \
  --target-papers 2 --max-paper-candidates 4 \
  --target-technical-sources 1 --max-technical-candidates 2 \
  --coordinator-retries 1 --max-correction-rounds 1 \
  --max-prefinal-repair-rounds 0 --finalizer-retries 1 --allow-dirty

# 4) Finalize（有完成 topic 后）
uv run slrharness finalize --workspace workspaces/demo-agent-memory \
  --agent-backend claude-code --topic-execution-mode topic_coordinator \
  --finalizer-timeout 3600 --max-prefinal-repair-rounds 0 \
  --allow-finalize-with-limitations --allow-complete-with-warnings --allow-dirty

# 5) 展示结果
grep -E '^## ' workspaces/demo-agent-memory/SUMMARY.md
ls workspaces/demo-agent-memory/sections
```

---

*本方案基于 RSI / agent-memory 两轮真实跑通经验整理。*
