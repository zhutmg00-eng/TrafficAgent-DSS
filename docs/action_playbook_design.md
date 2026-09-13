# Use-Case Design Note: Action Playbook Emission

## Problem
Users complained: "大多数方案和话语不明所以 — 我用AI帮助决策后仍然不知道需要做什么."
The original report section 三 was vague jargon (空域分流/时域扩容/激波回传) and never told
the operator **what to do, in what order, when, and with what expected effect**.

## Solution Overview

### 1. `formulate_action_plan()` — new method
- Signature: `formulate_action_plan(self, diagnosis, strategies, rollout) -> Dict[str, Any]`
- Returns `{"plain_summary": str, "steps": [...]}`
- Steps are **parameterised from real computed values** (cycle_length, green_split, offsets,
  reroute_ratio, vms_advisory, risk_warning) — nothing is invented.
- 7 steps ordered by urgency:
  1. VMS 分流发布 / 待命
  2. Webster 信号配时下發
  3. 绿波协调启用 (with 5-word plain-language explanation)
  4. 上游管控与回溢防护
  5. 成效监测与验证
  6. 恢复与回落
  7. 兜底预案 (远端截流 + 路侧引导)
- Handles `rollout={}` or `rollout=None` gracefully.

### 2. Wiring into pipeline
- `formulate_candidate_strategies()` now calls `formulate_action_plan()` and includes the
  result as the `"action_plan"` key in its return dict.
- `generate_decision_report()` extracts `plain_summary` and `steps` from
  `strategies["action_plan"]` and renders:
  - **Section 三**: brief parameterised instruction table (cycle, green, reroute, green-wave)
  - **Section 四** (NEW): "行动指令清单 (Action Playbook)" with full step-by-step table

### 3. Jargon reduction
- `_diagnose_deterministic()` CoT labels changed:
  - 态势感知 → **现状**
  - 空间排队 → **排队**
  - 成因归因 → **原因**
  - 蔓延风险 → **蔓延风险** (kept)
  - 旁路核查 → **旁路核查** (kept)
- Added `"plain_diagnosis"` key to diagnosis dict (1-2 sentence plain Chinese summary).

### 4. Honesty rules preserved
- LLM/narrative layers still cannot invent numbers.
- Missing values produce `—"`, not fabricated data.
- Report explicitly notes fallback modes.
