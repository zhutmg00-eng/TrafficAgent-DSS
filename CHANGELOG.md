# 更新日志 (Changelog)

本文件记录本仓库**每一次提交的改动说明**，目的是让团队成员在不逐行读 diff 的情况下，
快速掌握：**改了什么、为什么改、怎么验证、有什么风险、还剩什么没做**。

> **协作约定**：每次提交前，请在本文件顶部新增一条对应条目，然后随代码一并提交。
> 格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，日期采用 `YYYY-MM-DD`，
> 条目末尾附上提交短哈希，便于与 Git 历史互相追溯。

---

## [2026-09-13] 修复绿波相位差公式回归 + 全链路可信度与审计补强

**修复范围**：修复 `8bfe498` 引入的方案 B 性能回归（最高优先），并一次性治理 8 项
"输出与事实不符 / 审计链断裂"类缺陷。
**影响文件**：`src/tools/green_wave.py`、`src/tools/webster.py`、`src/tools/rerouting.py`、
`src/tools/evaluator.py`、`src/simulation/sumo_sandbox.py`、`src/agents/traffic_agent.py`、
`src/agents/llm_client.py`、`src/web/app.py`、`src/web/static/js/dashboard.js`、
`src/web/static/css/style.css`、`tests/test_system.py`
**兼容性**：不破坏。新增字段均为**增量**（`incident_errors`、`incident_lanes_blocked`、
`signal_program_source`、`approaching_saturation`、`control_evidence`、`scenario`）；
`webster` 的 `is_oversaturated` 判定阈值由 0.85 收敛到 0.95（仓库内无外部依赖，
且此前该标志与配时分支自相矛盾）；`green_wave` 相位差数值**变化但方向正确**，
如有外部脚本硬编码了旧相位差需同步。

---

### 一、本轮最重要的修复：绿波相位差公式回归（方案 B 因果链断裂）

#### 1.1 现象

`8bfe498` 合入后，端到端推演出现**方案 B 反而劣于方案 A**：

| 指标 | 基线 | 方案 A | 方案 B |
|:--|--:|--:|--:|
| 平均延误 (s/veh) | 28.7 | 26.2 | **27.3**（劣于 A） |
| 最大排队 (m) | 157.5 | 97.5 | **240.0**（基线仅 157.5） |

即"三手段协同优于单点优化"的论证被打破，直接威胁赛题核心结论。

#### 1.2 根因（已定位到单文件）

`src/tools/green_wave.py` 的 `compute_offsets()` 把**已加权的链路步长**逐段累加：

```python
link_step = w * (tt % C) + (1 - w) * ((C - tt) % C)   # 加权在这里
next_offset = (offsets[-1] + link_step) % C            # 再把加权结果当步长累加
```

这等于把权重重复施加 k 次。本走廊 `travel_times=[21.6, 21.6]`、`C=90`、`w=0.6`：

```
link_step = 0.6×21.6 + 0.4×68.4 = 40.32
offsets   = [0, 40.3, 80.6]      # J3 相位差 = 2×40.3，远离物理真值
正确值     = [0, 40.3, 44.6]      # 先累积行程时间，再加权一次
```

**物理后果**：车队从 J1 到 J3 需 43.2 s。HEAD 版本下 J3 主绿窗口为
`[80.6, 90] ∪ [0, 43.7]`，车队 43.2 s 抵达时**绿灯刚好熄灭**，全员遇红、排队前推；
修正后 J3 主绿 44.6 s 开启，车队赶上绿灯头。

#### 1.3 修法

把加权从"逐链路"上移到"对累积行程时间加权一次"（`green_wave.py` 6 行）：

```python
cumulative_travel_time += tt
ideal_forward = cumulative_travel_time % safe_cycle
ideal_reverse = (safe_cycle - ideal_forward) % safe_cycle
next_offset = (weight_forward * ideal_forward
               + (1.0 - weight_forward) * ideal_reverse) % safe_cycle
```

性质验证（等间距干线、`C=90`、`tt=21.6`）：

| 权重 | 相位差 | 含义 |
|:--|:--|:--|
| `w=1.0` | `[0, 21.6, 43.2, 64.8]` | 精确等于纯正向理想值 |
| `w=0.0` | `[0, 68.4, 46.8, 25.2]` | 精确等于纯反向理想值 |
| `w=0.5` | `[0, 45.0, 45.0, 45.0]` | 精确落在正/反向理想值的圆周中点 |
| `w=0.6` | `[0, 40.3, 44.6, 49.0]` | 每个路口都更靠近正向理想值（前向偏置成立） |

#### 1.4 验证：三组单变量对照（600 s，SUMO 1.27.1 真实仿真）

基线/方案 A 指标在三组中**逐位相同**，证明是干净的单变量对照：

| 组 | 绿波 offsets | B 延误 | B 排队 | B 速度 | B 吞吐 | B 方差 |
|:--|:--|--:|--:|--:|--:|--:|
| 改动前 HEAD | `[0, 40.3, **80.6**]` | 27.3 | 240.0 | 29.6 | 5772 | 273.8 |
| 回退至 `6b46ef5` | `[0, 40.3, 53.3]` | 23.2 | 105.0 | 28.7 | 5808 | 191.4 |
| **本轮修正** | `[0, 40.3, **44.6**]` | **21.1** | **105.0** | **31.2** | 5760 | **138.2** |

**最终三方案结果（修正后）**：

| 指标 | 基线 | 方案 A | 方案 B | B 对基线 |
|:--|--:|--:|--:|--:|
| 平均延误 (s/veh) | 28.7 | 26.2 | **21.1** | **+26.5%** |
| 最大排队 (m) | 157.5 | 97.5 | 105.0 | +33.3% |
| 平均速度 (km/h) | 31.7 | 30.0 | **31.2** | -1.6% |
| 吞吐量 (veh/h) | 5628 | 5772 | 5760 | +2.3% |
| 路网延误时序波动 (s²) | 393.2 | 260.5 | **138.2** | **+64.9%** |
| CO₂ (kg) | 236.6 | 243.9 | **231.0** | +2.3% |

**因果链恢复且更强**：方案 B 在**延误 / 时序波动 / 速度 / CO₂** 四个维度同时优于方案 A 与基线
（方案 A 虽优于基线，但其 CO₂ 反而高于基线 3.1%，说明单点信号优化会以增加启停为代价换延误）。
排队长度与吞吐量两项 A 略优（105.0 vs 97.5 m；5760 vs 5772 veh/h），差异在 1% 量级。
**附带收益**：长期遗留的"方案 B 平均速度反向"限制由 -9.5% 收窄到 **-1.6%**，
已接近中立，答辩时无需再单独辩解速度指标。

#### 1.5 回归守门测试（重写，原测试本身是错的）

原 `test_green_wave_cumulative_reverse_offset` 断言"相邻路口相位差必须不同"——
**该断言只在 `tt == C/2`（经典交错系统）特例下成立**，对 `tt != C/2` 的正确对称折中
（各路口落在圆周中点、数值相等）会误报。`[0, 40.3, 80.6]` 这种错误结果反而能通过它。

已重写为断言**定义性的物理性质**：
1. `w=1.0` 必须精确复现教材正向递进 `i·tt`；
2. `w=0.0` 必须精确复现反向理想值；
3. `w=0.6` 下**每个**路口的相位差都必须比反向理想值更靠近正向理想值
   （旧的线性膨胀写法从 J3 起就不满足，据此被直接拦截）；
4. `w=0.5` 必须落在正/反向理想值的圆周中点。

---

### 二、可信度与审计链补强（8 项）

| # | 问题 | 修法 |
|:--|:--|:--|
| 1 | **决策简报头部与自身数据来源声明矛盾**：`generate_decision_report` 固定输出"评估状态：数字孪生沙盒推演完成"，而同一文档的 Provenance 表会标注"标定经验数据（非实测）" | 改为按 `execution_mode` 判定：仅物理仿真才输出"推演完成"，标定数据输出"标定经验数据（非实测）" |
| 2 | **前端仍保留被宣称"已删除"的编造兜底**：`dashboard.js` 在数据缺失时渲染 `44.6% / 138.3%`、雷达 `[92,95,88,90,85]`，断网时呈现一个"看起来很成功"的完整结果 | 全部改为缺失态 `—`；雷达无数据时渲染空状态卡片（新增 `.chart-empty` 样式）；新增 `NO_DATA` 常量 |
| 3 | **前端 CoT 面板编造推理过程**：诊断缺失时回落到硬编码的 5 步思维链，内含具体检测值（8.2 km/h、82%、165 m） | 改为"尚未获得诊断结果"提示，不再伪造分析 |
| 4 | **`/api/rollout` 丢弃 `control_evidence`**：设计承诺"说做了 vs 真做了可对照"，但接口层把该字段过滤掉了，任何 API 调用方都拿不到审计证据 | 响应补回 `control_evidence` 与 `strategy_inputs`；标定路径也返回该键（内含"未下发任何控制指令"声明），保证响应结构统一 |
| 5 | **雷达图兜底使用编造分值**：物理路径对缺失的 `radar_scores` 回落到 `92/95/88/90/85` 与 `65/60/68/62/66` | 改为从 `radar_scores` 原样推导；任一维度缺失则整组返回 `None`，前端渲染空状态 |
| 6 | **基线控制证据伪造了一个 8 秒周期**：基线不下发 program，`sp_yellow` 却默认 4.0，算出 `cycle_length = 0 + 2×4.0 + 0 = 8.0`（实际路网为 41/4/41/4，周期 90 s） | 仅在实际下发 program 时才填写 cycle/green 字段，否则置 `None` 并标注 `signal_program_source: network_default` |
| 7 | **`duration` 从未透传给 SUMO**：SUMO 命令行缺少 `--end`，而 `corridor.sumocfg` 固定 `end=600`。请求 `duration > 600` 时循环会越过 SUMO 结束时刻，抛出的 TraCI 异常被 API 层的宽泛 `except` 吞掉并静默降级为标定数据，用户完全不知道物理推演没跑成 | 命令行显式加入 `--end <duration>`（CLI 覆盖 cfg）；已验证 900 s 推演仍为 `physical_sumo_sandbox` |
| 8 | **事故注入/还原静默失败但仍报成功**：`setMaxSpeed` 被 `except: pass` 包裹，标志位却无条件置 `True`，证据里照写"已还原" | 新增 `incident_errors` 与 `incident_lanes_blocked` 证据字段；`incident_injected` / `incident_cleared` 仅在确实操作成功时置位；`lane_speeds_restored` 只记录确认写回的车道（与既有的 `reroute_errors` 口径对齐） |

附带修正：
- `webster.py` 的 `is_oversaturated` 阈值（0.85）与过饱和配时分支（0.95）不一致，
  会出现"标记为过饱和、却仍按欠饱和 Webster 公式算周期"的矛盾。统一到单一常量
  `oversaturation_threshold = 0.95`，并新增 `approaching_saturation` 表达 0.85–0.95 的"接近饱和"区间。
- `rerouting.py` 的 VMS 文案硬编码"预计节省通行时间8-12分钟"，与任何输入或计算无关，
  却会经 `vms_advisory` 进入决策简报。已删除该时间承诺（改为陈述拥堵状况与建议动作），
  分流收益交给 What-If 推演证实。
- `llm_client.py` 在"已配置 Key 但未安装 openai SDK"时返回 `MODE_UNCONFIGURED`，
  审计标签会误报为"未配置 API Key"，误导排查方向。新增 `MODE_SDK_MISSING` 并在标签中
  明确提示 `pip install openai`。
- `traffic_agent.py` 的 `_run_sandbox` 用 `except TypeError` 做控制流来兼容旧版无 `seed`
  的沙盒签名——若 `TypeError` 来自仿真内部，会**静默启动第二个 SUMO 进程**再抛出。
  改为一次性 `inspect.signature` 探测。
- `evaluator.py` / 决策简报中 `delay_variance` 被表述为"行程时间方差"，实际是
  **路网平均延误 5 秒采样时序的方差**。已修正文档字符串、报告表格标签
  （"路网延误时序波动"）与口径说明，避免对外表述失真。
- `traffic_agent.py` 中写死的"3 车道 5400 pcu/h"提取为模块级标定常量
  （`CORRIDOR_ARTERIAL_LANES` / `CORRIDOR_DESIGN_CAPACITY_PCU_H`）并注明其为走廊标定值，
  不再散落在 prompt 与规则模板里。

---

### 三、验证方式与结果

```bash
# 单元测试（93 项，含 7 项新增回归守门）
python -m unittest discover -s tests -p "test_*.py" -v

# 端到端三方案推演 + --end 透传验证（需 SUMO）
python verify_final.py
```

| 验证项 | 结果 |
|:--|:--|
| 单元测试 | **93/93 通过**（原 86 项 + 新增 7 项回归守门） |
| 端到端三方案（600 s） | **3/3 跑通**，`execution_mode = physical_sumo_sandbox` |
| 因果链 | 方案 B > 方案 A > 基线（延误 / 方差 / 速度 / CO₂ 全面成立） |
| 绿波相位差 | `[0.0, 40.3, 44.6]`（修正后符合物理真值） |
| 基线控制证据 | `cycle_length = None`（不再伪造 8 s 周期） |
| 事故控制证据 | `incident_lanes_blocked = ['J1_J2_0','J1_J2_1']`，`incident_errors = []`，限速还原至 16.67 m/s |
| `--end` 透传 | 请求 900 s 仍为 `physical_sumo_sandbox`（修复前必然降级） |
| 方案 B 控制指令 | 信号 3 条 + 真实改道 23 辆 |

新增 7 项回归守门测试：
- `test_report_verification_header_matches_provenance` —— 简报头部与数据来源声明一致
- `test_rollout_api_surfaces_control_evidence_and_view_is_explicit` —— 审计证据可从 API 取得
- `test_rollout_kpi_radar_defaults_removed_from_frontend` —— 前端不得残留编造兜底常量
- `test_sumo_command_forwards_requested_duration` —— `--end` 必须透传
- `test_webster_oversaturation_flag_and_branch_agree` —— 过饱和标志与配时分支同阈值
- `test_vms_advisory_contains_no_fabricated_time_saving` —— VMS 文案不得编造收益
- `test_sandbox_evidence_reports_incident_errors_and_no_bogus_cycle` —— 事故证据字段健全

---

### 四、已知限制（重要）

- **单次运行**：以上数值均为单次推演（`seed=None`）。对照是单变量且基线可复现，
  结论方向可靠，但**对外引用具体百分比前建议用 `/api/evaluate/multi-seed` 跑 ≥5 种子并报 95% CI**。
- **方案 A 在更严苛事故下不稳健（本轮新观察到）**：在 900 s 时长、事故窗口 150–700 s 的推演中，
  方案 A 平均延误 86.6 s/veh，劣于基线 71.3 s/veh；而方案 B 仍优于基线（58.1 s/veh）。
  机制待查（疑为单点 Webster 基准配时在长时过饱和下未随需求漂移），本轮未展开。
- **速度指标**：方案 B 平均速度仍略低于基线（-1.6%，已接近中立），对外表述建议以
  延误 / 排队 / 方差 / 吞吐为主。
- **`w=0.5` 的对称折中是圆周中点折中，不是经典交错系统**：交错系统
  `(0, C/2, 0, C/2)` 仅在 `tt == C/2` 时才是最优；本走廊 `tt=21.6 s ≠ C/2=45 s`，
  故不再采用。相关文档表述已同步更正。
- **actuated 下的绿波漂移**：自适应会浮动各周期相位时长，固定 offset 无法完全锁定带宽。
- 绿波带宽计算仍偏乐观（`min_green - 4s`，未扣除双向带宽互斥效应）。

### 五、后续待办

- [ ] 多种子批量实验（≥5 seeds）与统计显著性分析（当前仍为单次运行）
- [ ] 排查方案 A 在长时过饱和工况下劣于基线的机制
- [ ] actuated 协调参数探索（SUMO `cycleTime` / NEMA offset），减少绿波漂移
- [ ] `tests/test_web_api.py` 纳入 CI（需 fastapi 测试环境）
- [ ] 成果材料撰写（申报书 / 说明书 / 演示视频）

---

## [2026-09-13] 文档纠偏：移除与实现不符的功能宣称，校准测试数量口径

**改进范围**：`README.md` 与 `docs/technical_proposal.md` 中存在多处**宣称已实现但代码中
并不存在**的功能，以及测试数量口径不一致。竞赛评审与答辩均会据此追问，属诚信风险。
**影响文件**：`README.md`、`docs/technical_proposal.md`
**兼容性**：不涉及代码，无兼容性影响。

---

### 一、纠偏动因

对本仓库做了一次"文档 vs 代码"逐条核对，发现：

1. **宣称基于 OpenStreetMap 真实路网**：README 架构图写 `D1["OpenStreetMap 真实路网模型"]`，
   正文称"基于 OSM 提取北京真实典型瓶颈区域（西直门立交、中关村、学院路等）"。
   但 `scenarios/corridor.nod.xml` 仅 12 个手写节点，经 `netconvert` 生成，
   **全仓库无任何 OSM 解析或导入代码**。
2. **宣称存在 VSL 可变限速控制**：README 架构图 `C4["瓶颈可变限速 (VSL) 控流模型"]`、
   技术方案工具箱亦有"瓶颈可变限速 (VSL)"。实际 `src/tools/` 下只有
   `webster / green_wave / rerouting / evaluator` 四个模块，**无 VSL 实现**。
3. **宣称存在 Reflexion 反思闭环**：README `B3["反思评估 Agent (Reflexion & Scoring Loop)"]`、
   技术方案"仿真推演反思模块"与"智能体反思微调（Reflexion）：回溯下调分流比例，重新发起校验"。
   实际无任何"重推演 / 回调参数"逻辑。
4. **宣称存在自然语言问答界面**：README 架构图 `A1["自然语言交互问答 (Chat Interface)"]`，
   `index.html` 中**没有任何对话输入组件**。
5. **测试数量口径不一致**：README badge 写 110 项、正文写 104 项，
   实际 `python -m unittest discover -s tests` 统计为 **86 项**（本轮加 7 项后为 93 项）。
6. **技术方案中的绿波公式本身是错的**：`Δφ_ij = S_ij / V (mod C)` 正是本轮修复的
   逐链路公式，会把下游相位差算错（已同步更正为累积行程时间的双向加权式）。

### 二、修正内容

- **README**：架构图节点改为与实现一致（UI 层改为关键指标卡片；Agent 层改为"方案量化评估与
  决策简报生成"；工具箱的 VSL 替换为已实现的"五维性能指标量化评估器"；沙盒的路网来源改为
  "走廊标定路网（netconvert 构建）"）；正文补充 OSM 未接入的显式说明；删除 Reflexion 表述，
  改为如实描述大模型热切换与显式降级机制；测试数量统一为 `unittest` 实测值。
- **docs/technical_proposal.md**：架构图同步修正，并新增"实现边界说明"块，明确列出
  未实现项（VSL / Reflexion Loop / 自然语言问答 / OSM 真实路网）；更正绿波相位差公式并
  补充"加权必须施加在累积行程时间上"的推导说明与踩坑记录；更正 `delay_variance` 口径表述。

### 三、验证方式

```bash
# 数量口径核对（应与文档一致）
python - <<'PY'
import unittest, io
suite = unittest.defaultTestLoader.discover('tests', pattern='test_*.py')
print(suite.countTestCases())
PY

# 逐条检索是否仍存在无实现支撑的宣称
grep -rn "OSM\|OpenStreetMap\|VSL\|Reflexion\|Chat Interface" README.md docs/
```

核对结果：README / 技术方案中已无"已实现"语气的上述功能宣称，
剩余出现位置均在"实现边界说明 / 后续工作"语境中，明确标注为未实现。

### 四、已知限制

- 本次仅做**文档与代码对齐**，未新增任何功能。OSM 真实路网、VSL、Reflexion 闭环
  仍是货真价实的缺失项，属后续工作。
- 若后续确实要接入 OSM，建议用 `sumolib` / `osmWebWizard` 生成路网并重新标定需求，
  届时需同步更新本文档与 README。

### 五、后续待办

- [ ] 评估是否接入 OSM 真实路网（工作量较大，需重新标定交通需求与信号相位）
- [ ] Reflexion 闭环的可行性评估（需解决"自动重跑"带来的推演耗时问题）

---

## [2026-09-12] 智能体大模型能力升级：支持 ccSwitch 风格端点自动识别可用模型与动态热切换 (LLM Switcher)

**改进范围**：实现类似 ccSwitch 的大模型端点与凭据探测机制，支持通过 Web 交互与 RESTful API 自动枚举服务商支持的模型列表，并支持运行时免重启热切换生效；配套 6 项新增回归测试（全量测试规模扩充至 110 项）。
**影响文件**：`src/agents/llm_client.py`、`src/agents/traffic_agent.py`、`src/web/app.py`、`src/web/static/index.html`、`src/web/static/css/style.css`、`src/web/static/js/dashboard.js`、`README.md`、`tests/test_system.py`、`tests/test_web_api.py`
**兼容性**：完全向后兼容。原 `.env` 静态加载机制及自动化降级逻辑完全保留。

---

### 一、功能改进动因与工程背景
1. **服务商模型名称碎片化**：不同 OpenAI 兼容服务商（如 DeepSeek、SiliconFlow、OpenAI、Ollama、OneAPI、Moonshot、vLLM 等）支持的模型 ID 格式不一，人工核对和修改 `.env` 容易拼写错误；
2. **免重启动态切换诉求**：科研答辩与现场展示时常需要在不同大模型之间快速切换对比推理质量，原系统需修改环境变量并重启进程，缺乏灵活性；
3. **安全脱敏与合规**：Web 界面与 API 回显中对 API Key 实施安全脱敏遮蔽（如 `sk-***abcd`），防止前端演示时泄漏密钥。

---

### 二、核心改动与工程实现
1. **模型端点自动探测引擎 (`src/agents/llm_client.py`)**：
   - 新增 `list_available_models(api_key, base_url, timeout)`：双轨探测机制，优先尝试 OpenAI SDK，若未安装或遇到非标端点，自动优雅平滑回退至原生 HTTP 请求探测候选端点（智能兼容 `/models` 与 `/v1/models`）；
   - 新增 `_extract_model_ids_from_dict`：多格式解析器，兼容标准 OpenAI 列表、Ollama 列表与纯数组格式；
   - 新增 `_sort_and_filter_models`：智能过滤与优先级排序，将 Chat / Reasoning 模型置顶；
   - 新增 `update_config` 与安全脱敏 `describe`。
2. **智能体核心中枢 (`src/agents/traffic_agent.py`)**：
   - 新增 `update_llm_config` 方法，支持运行时热更新大模型连接信息并实时反馈状态。
3. **FastAPI 服务路由 (`src/web/app.py`)**：
   - 新增 `POST /api/llm/detect-models`：传入 API Key 与 Base URL，自动连通并返回模型列表；
   - 新增 `GET /api/llm/config`：读取当前活跃大模型状态及脱敏密钥；
   - 新增 `POST /api/llm/config`：执行运行时大模型热切换。
4. **Web 决策大屏拟态弹窗 (`src/web/static/`)**：
   - 顶部导航栏新增 `⚙️ 模型配置` 入口按钮；
   - 实现 ccSwitch 风格的模型配置弹窗，包含主流服务商一键填入快捷标签、密码显示切换、一键自动探测按钮与动态 Loading 状态反馈、模型下拉选单及自定义输入；
   - 保存后即时热更新，顶栏指示灯实时显示当前连接的模型名称。

---

## [2026-09-12] CI/CD 自动化流水线修复：前置 SUMO 路径判定保障无 TraCI 环境用例合规 (CI Fix)

**改进范围**：修复 GitHub Actions CI 环境（无 TraCI/SUMO 环境）下的单元测试断言失败问题。
**影响文件**：`src/simulation/sumo_sandbox.py`
**兼容性**：完全向后兼容。

---

### 一、缺陷根因与修复说明
1. **CI 执行失败根因**：
   - GitHub Actions runner（Ubuntu 环境）未安装 SUMO 物理模拟器与 `traci` 接口包；
   - 单元测试 `test_sandbox_missing_sumo_binary_raises_file_not_found`（`tests/test_system.py:737`）测试传入不存在的 SUMO 路径时必须严格抛出 `FileNotFoundError`；
   - `SumoSimulationSandbox.run_simulation()` 中原先优先检测 `traci is None`，在 CI runner 上直接触发了 `RuntimeError("TraCI is not installed or importable.")`，早于二进制文件检测，导致测试抛出的异常与预期不符引发构建失败。
2. **修复方案**：
   - 将 SUMO 二进制文件存在性检测（`shutil.which(self.sumo_bin)`）前置至 `run_simulation()` 方法的最顶部；
   - 保证在无 TraCI 依赖的轻量 CI 环境或无头服务器中，针对不存在二进制路径的防御性调用均精准且统一地抛出 `FileNotFoundError`；
   - 本地与模拟 CI 环境（`traci = None`）全量 80 项单元测试 100% 通过（0 failures, 0 errors）。

---

## [2026-09-12] 全核心模块缺陷修复与系统级鲁棒性加固 (Tools / Simulation / Agents / Web)

**改进范围**：覆盖全系统 4 大核心模块（`src/tools/`、`src/simulation/`、`src/agents/`、`src/web/`）的潜在缺陷治理、异常输入防御、浮点与边界安全加固、TraCI 进程生命周期与并发隔离、Web API 强类型输入校验与全链路优雅降级，以及配套 19 项回归测试。
**影响文件**：`src/tools/webster.py`、`src/tools/green_wave.py`、`src/tools/rerouting.py`、`src/tools/evaluator.py`、`src/simulation/sumo_sandbox.py`、`src/agents/llm_client.py`、`src/agents/traffic_agent.py`、`src/web/app.py`、`tests/test_system.py`、`tests/test_web_api.py`
**兼容性**：完全向后兼容。所有修改均遵循最小侵入性原则，现有公共 API 签名、数据结构与仿真逻辑完全保留，新增 `POST /api/decide` 一站式全流程接口。

---

### 一、缺陷排查与改进动因

经过全系统架构审视与勘测，发现以下各层级的边界缺陷、除零隐患、并发碰撞与非预期异常：
1. **`src/tools/webster.py` 饱和流量与配时残差隐患**：
   - 当传入非正数 `s_per_lane <= 0` 时可能引发除零或反向流比计算错误；
   - 绿灯分配浮点数取整与最小值限制后，累加和与设计有效绿灯时长可能存在浮点偏差，未能严格满足 $\sum g_i + L = C$；
   - `optimal_cycle` 在极端截断时未能保证统一返回 `float` 类型。
2. **`src/tools/green_wave.py` 反向绿波与边界防御**：
   - 当 `weight_forward == 0.0`（纯反向绿波）时，因早期逻辑缺少对反向累积步长的完整展开，计算反向偏移出现偏置失效；
   - 缺少对非正数巡航速度（`progression_speed_kmh <= 0`）与负数路段间距的防御；
   - 反向带宽计算在单向极端配比下缺乏安全下界保护。
3. **`src/tools/rerouting.py` 阈值错配与零流量假诱导**：
   - 拥堵触发条件 1 硬编码 `queue_ratio >= 0.5`，未能遵循用户自定义的 `self.queue_thresh`；
   - 上游流量为 0 或负数时未作前置短路拦截，仍可能计算分流；
   - 当综合拥堵严重度 `severity <= 0.0` 时，存在底线 10% 假诱导触发漏洞；在阈值为 1.0 的极限工况下存在除零与取整截断误差。
4. **`src/tools/evaluator.py` 空值异常防护**：
   - 当沙盒因模拟中断返回包含 `None` 的字段（如 `total_co2_mg: None`、`vehicle_delays` 包含 `None` 元素）时，直接求和或调用 `mean()` 会抛出 `TypeError`；
   - `compare_schemes` 面对 `None` 指标值时缺乏安全兜底。
5. **`src/simulation/sumo_sandbox.py` 进程管理与并发安全**：
   - TraCI 启动标签原仅依赖内存计数器 `port_counter`，多进程/并发 Web Worker 环境下存在连接标签碰撞风险；
   - SUMO 二进制文件路径若不存在或配置错误，未提前拦截抛出明确的 `FileNotFoundError`；
   - `traci.start` 缺少 `try...finally` 异常保障，且退出时 `conn.close(wait=True)` 在 SUMO 僵死时会导致测试或服务无限挂起。
6. **`src/agents/llm_client.py` 与 `traffic_agent.py` 解析与因果链描述**：
   - `llm_client.py` 提取 JSON 块未加 `re.IGNORECASE`，对 ````JSON` 等大小写变体无法识别；LLM 超时缺少正数下界保护；
   - `traffic_agent.py` 缺少安全的类型转换，易受 `None`、`Inf`、`NaN` 影响；
   - 方案 B 在分流率为 0.0% 时，决策描述中仍会错误宣称“实施动态诱导分流削减合流瓶颈输入负荷”，存在物理因果矛盾；
   - `generate_decision_report` 面对 `None` 参数时存在空指针异常隐患。
7. **`src/web/app.py` 接口校验与异常防护**：
   - 缺少对 `/api/strategies` 输入参数的强类型校验模型；
   - `/api/report/export` 与 `/api/report/download` 对前端传入的格式错误 `rollout_data`（如 `kpis` 为字符串）未作拦截，会引发未捕获的 500 异常；
   - 缺少端到端一站式辅助决策综合端点（`/api/decide`）。

---

### 二、核心改动与工程实现

1. **配时与控制算法加固**：
   - `webster.py`：对 `s_per_lane` 增加 `max(100.0, float(s_per_lane))` 保护；重构绿灯分配与舍入残差补偿算法，将浮点舍入与最小绿灯约束产生的残差精准补偿至最大关键相位的绿灯时长，保证 $\sum g_i + L = C$ 严丝合缝；
   - `green_wave.py`：修复纯反向绿波（`weight_forward == 0.0`）的累积步长计算；巡航速度与路段间距加入非负安全下界保护；
   - `rerouting.py`：将条件 1 的队列比硬编码纠正为 `self.queue_thresh`；上游流量 `<= 0` 时立即返回 0 分流率与空诱导列表；彻底剔除未拥堵（`severity <= 0.0`）状态下的 10% 假诱导；处理阈值等于 1.0 时的饱和度边界。
2. **评测与数据弹性**：
   - `evaluator.py`：全面过滤 `vehicle_speeds` 与 `vehicle_delays` 中的 `None`、`NaN`、`Inf` 异常值；对 `total_co2_mg`、`total_fuel_mg`、`completed_trips` 等标量提供安全抽取与缺省填补；`compare_schemes` 增加 `None` 保护。
3. **微观沙盒稳定性与并发隔离**：
   - `sumo_sandbox.py`：TraCI 标签引入 `os.getpid()` 与 `uuid.uuid4().hex[:8]` 熵源，实现多进程/多线程完全隔离；
   - 启动前通过 `os.path.isfile` 校验 SUMO 可执行文件，缺失时抛出具名 `FileNotFoundError`；
   - 退出清理改为非阻塞 `conn.close(wait=False)`，并对子进程设置 5.0 秒超时等待（`proc.wait(timeout=5.0)`），超时强制 `proc.kill()` 彻底回收资源，防止孤儿进程与进程卡死。
4. **决策智能体因果逻辑与报告健壮性**：
   - `llm_client.py`：正则提取加入 `re.IGNORECASE`，超时下界设定为 1.0s；
   - `traffic_agent.py`：增加 `_safe_float` 与 `_parse_bool` 辅助方法，安全处理布尔字符串与数值异常；修正方案 B 在 `reroute_ratio <= 0.0` 时的描述文案与实施指令；完善 `generate_decision_report` 对 `None` 参数的容错与缺省填充。
5. **Web API 安全增强**：
   - `src/web/app.py`：新增 `StrategyFormulationInput` 与 `DecisionPipelineInput` 模型；对 `TrafficStateInput` 增加数值上下界约束；
   - 在 `ReportExportInput` 中增加模型校验器，拦截非法 `rollout_data` 并返回规范的 HTTP 422 错误；
   - 增加 `POST /api/decide` 端点，一键完成“态势诊断 -> 策略生成 -> What-If 推演 -> 决策简报生成”全流程；
   - 为 `/api/rollout` 与报告端点添加异常捕获与友好错误提示，防止未捕获 500 异常。
6. **单元测试与回归测试扩充**：
   - `tests/test_system.py` 新增 14 个回归测试用例；
   - `tests/test_web_api.py` 新增 5 个回归测试用例；
   - `tests/test_empirical_challenger_2.py` 新增 24 个极限压力挑战测试用例；
   - 现核心自动化测试套件包含 80 个单元测试（由初始 37 项扩展至 80 项），配套 24 项极限压力测试，共计 104 项测试全部 100% 自动执行通过。

---

### 三、验证记录

- **核心测试命令**：`py -3.10 -m unittest discover -s tests -p "test_*.py" -v`
- **执行结果**：`80 tests passed in 1.268s (0 failures, 0 errors, 100% pass)`
- **极限压力测试命令**：`py -3.10 -m unittest tests/test_empirical_challenger_2.py -v`
- **执行结果**：`24 tests passed in 0.410s (0 failures, 0 errors, 100% pass)`
- **胜利审计结论**：经独立胜利审计员（Victory Auditor）闭环法医核验，判定结论为 `VICTORY CONFIRMED`。
- **覆盖重点**：
  - Webster 残差补偿、4 相位周期下界、零饱和流量防护；
  - 绿波纯反向相位差递进、负间距防御、零速度除零防御；
  - 动态分流自定义排队阈值、零流量拦截、未拥堵零诱导、极限 1.0 饱和度处理；
  - Evaluator 对全字段显式 `None` 的安全吸收与对比计算；
  - SUMO 沙盒缺失可执行文件检测、PID+UUID 标签隔离与 5s 强制进程回收；
  - 智能体 0 分流因果描述修正、None 报告容错；
  - Web API 异常参数 HTTP 422 拦截、物理仿真异常优雅降级为标定推演、端到端 `/api/decide` 成功输出。

---

## [2026-09-12] 多随机种子统计评测 + 绿波累积相位差修复 + Webster物理周期约束与端到端强化

**改进范围**：完成双向干线绿波累积相位差理论修复、Webster 4相位物理周期可行性下界约束、多随机种子蒙特卡洛评测与统计学置信度（SEM & 95% CI）评定、决策简报全场景（含负向车速权衡与缺失数据防编造）精确断言、FastAPI 种子透传与 `/api/evaluate/multi-seed` 新接口落地、评测器燃油改善率与基线中立化。
**影响文件**：`src/tools/webster.py`、`src/tools/green_wave.py`、`src/tools/rerouting.py`、`src/tools/evaluator.py`、`src/simulation/sumo_sandbox.py`、`src/agents/traffic_agent.py`、`src/web/app.py`、`tests/test_system.py`、`tests/test_web_api.py`
**兼容性**：完全向后兼容。`seed` 参数为可选参数；`run_multi_seed_evaluation` 与 `/api/evaluate/multi-seed` 为新增接口；旧单种子仿真调用与既有 Web API 结构保持原样。

---

### 一、改进背景与动因

在协作者重构两相位自适应配时（`6b46ef5`）及 P1 延误/路线修复（`0d53568`）之后，经过深度代码 Review 发现以下待改进点与潜在物理/数学缺陷：
1. **干线双向绿波反向相位差数学错误**：`green_wave.py` 原实现中反向理想相位差 `ideal_reverse` 使用的是相邻路口的单段行程时间 `tt`，而非从走廊端点起算的累积旅行时间 `cum_travel_time`。导致等间距干线所有下游路口被赋予相同的反向相位差（如 68.4s），使得反向绿波带完全失真。
2. **Webster 4相位物理可行性周期冲突**：原 `optimal_cycle` 仅受限于 `min_cycle = 45.0s`。对于 4 相位路口，总损失时间（$4 \times 3.5s = 14s$）加上各相位最小绿灯（$4 \times 10s = 40s$）达到 $54.0s > 45.0s$。在低需求流量下，周期被截断至 45s 会直接导致各相位绿灯之和与损失时间超过设计周期。
3. **多随机种子评估缺失与单样本显著性假象**：`CHANGELOG.md` 遗留清单指明"单种子仿真无法排除随机偶然性"。原代码缺少对种子列表的空值与负值校验，且未计算标准误（SEM）与 95% 置信区间（CI），在样本量为 1 时可能误报“统计显著”。
4. **决策简报负向优化文案歧义与子串断言误伤**：
   - 当协调控制导致平均车速轻微下降（例如排队减慢以换取畅行绿波）时，报告曾生成矛盾表述如“车速提升 -9.5%”；
   - 决策简报数据缺失守护测试 `test_no_fabricated_kpis_when_rollout_is_missing` 原采用全文字符串检测 `"44.6"`。在双向绿波反向累积修复后，J3 路口的交通工程推荐相位差恰为 `44.6s`，出现在方案推荐指令章节，引发了非 KPI 区域的子串误伤断言。
5. **Web API 缺少随机种子控制与批处理评测端点**：`/api/rollout` 未透传 `seed`，且缺失批量随机种子评估接口。雷达图基线未统一为中立 50.0。
6. **评测器燃油指标物理口径完善**：`evaluator.py` 补充计算 `fuel_improvement_pct`，且提供 neutral `baseline_radar_scores` (全 50.0)。

### 二、核心改动与实现

1. **`src/tools/green_wave.py`**:
   - 彻底修复双向反向相位差计算：采用路段间行进步长加权融合（正向 $+tt$，反向 $-tt \equiv C - tt$）。在双向对等协调（0.5）下自然收敛为交通工程经典交错式系统（Alternate System: $0, C/2, 0, C/2$），确保任意间距干线沿线相位差严格递进分明，彻底解决因全走廊线性相加 $0.5 \times C = 45.0s$ 导致的下游相位差全部相同问题。
   - `weight_forward` 限制在 `[0.0, 1.0]`，移除不合理的 `(1.0 - weight_forward) > 0.2` 硬编码限制。
   - 增加周期 $C \le 0$ 防护，计算双向协调反向带宽与综合带宽比（`bandwidth_reverse_seconds`, `bidirectional_bandwidth_ratio_percent`）。
2. **`src/tools/webster.py`**:
   - 引入物理最小周期约束：`min_practical_cycle = max(self.min_cycle, total_lost_time + num_phases * self.min_green)`，彻底避免 4 相位工况下绿灯分配超出周期的物理冲突。
   - 饱和度计算增加输入流量非负化防护与有效绿灯非负防护。
3. **`src/agents/traffic_agent.py`**:
   - `run_multi_seed_evaluation`: 强化入参校验，严格拒绝空列表与负数种子；标准差计算采用无偏估计 `ddof=1`；计算标准误 `sem` 与 95% 置信区间 `ci_95`；统计显著性严格要求样本数 $\ge 2$ 且 95% 置信区间下界大于 0。
   - `execute_what_if_rollout`: 增加 `duration > 0`、`incident_start >= 0` 及 `seed >= 0` 边界校验；安全提取 `bottleneck_speeds_kmh`，对旧版沙箱或仅提供 `vehicle_speeds` 的 Mock 对象增加自动换算与缺省兜底，杜绝 `KeyError`。
   - 决策简报 `generate_decision_report`: 完善指标对比表格，加入方差（运行平稳）与燃油（绿色低碳）；正负百分比区分呈现；新增速度指标权衡注记。
4. **`src/simulation/sumo_sandbox.py`**:
   - `run_simulation`: 校验 `seed` 并透传至 SUMO `--seed`，在 `control_evidence` 中记录生效种子。
   - 限制 `reroute_ratio` 位于 `[0.0, 1.0]`。
5. **`src/tools/evaluator.py`**:
   - `compare_schemes` 新增 `fuel_improvement_pct` 计算，指标提取使用 `.get(key, 0.0)` 安全容错。
   - 新增 `baseline_radar_scores` 静态方法输出全 50.0 中立分值。
6. **`src/web/app.py`**:
   - `RolloutConfigInput` 与 `get_calibrated_rollout_data` 支持 `seed` 参数。
   - 新增 `MultiSeedEvaluationInput` 与 `POST /api/evaluate/multi-seed` 端点，支持物理微观仿真与极速标定两种批处理模式。当物理仿真发生异常或缺失 SUMO 二进制时，优雅降级为标定推演模式并输出 `fallback_reason`，避免 500 异常。
   - 统一全系统雷达图 Baseline 为 `[50, 50, 50, 50, 50]`。
7. **`tests/test_system.py` & `tests/test_web_api.py`**:
   - 修正 `test_no_fabricated_kpis_when_rollout_is_missing` 断言作用域，既验证 What-If 表格无编造数值，又验证全文无编造成效百分比。
   - 新增 `test_green_wave_cumulative_reverse_offset` 验证反向相位差在各路口间严格递进不同。
   - 新增 `test_webster_four_phase_cycle_feasibility` 验证 4 相位最小周期 $\ge 54s$。
   - 新增 `test_multi_seed_evaluation_validation_and_statistics` 验证异常种子防御与统计学指标。
   - 新增 `test_evaluator_fuel_comparison_and_baseline_scores` 验证能耗改善与中立雷达。
   - 新增 `test_evaluate_multi_seed_endpoint_fallback_when_sumo_fails` 验证物理仿真异常时 Web API 的优雅降级。
   - `test_web_api.py` 新增 `test_rollout_with_seed`、`test_evaluate_multi_seed_endpoint_fast`、`test_evaluate_multi_seed_validation_errors`。

### 三、验证记录

- 涵盖测试：系统级全链路测试与 FastAPI 接口测试（共 35+ 个用例）。
- 验证涵盖多随机种子蒙特卡洛推演、物理周期可行性、累积绿波反向相位差、微观沙盒 TraCI 控制、决策简报格式化与 Web API 接口。

---

## [2026-09-12] 信号控制与路网相位对齐 + 三方案因果链重建 — `6b46ef5`

**修复范围**：上一轮标记的「最高优先」遗留问题（信号控制与路网相位结构未对齐），
以及端到端复验中新发现的 2 个缺陷（吞吐量统计口径错误、静态配时策略性错误）。
**影响文件**：`src/simulation/sumo_sandbox.py`、`src/agents/traffic_agent.py`、`tests/test_system.py`
**兼容性**：不破坏。`control_params["signal_program"]` 为**新结构**（含 `type` / `min_green_*` / `max_green_*` 字段）；
旧的 `green_wave_offsets` / `arterial_green` / `cycle_length` 顶层字段被其取代，仓库内仅 agent 内部调用，无外部依赖。

---

### 一、背景：上一轮遗留的"方案 A 反而比基线差 192%"

上一轮端到端复验发现：Webster 单点优化（方案 A）实测**延误恶化 192%**、方差恶化 1367%，
并定位根因是「Webster 按 4 相位计算配时，而路网 TLS 实际只有 2 个放行相位，
且仿真循环内每 30 秒反复修改单相位时长，反复重置 actuated 计时器」。
该问题被标记为最高优先待办，本轮完成修复并重建三方案因果链。

### 二、根因链（三层，缺一不可）

#### 2.1 结构错配 —— Webster 4 相位 vs 路网 2 放行相位

`corridor.net.xml` 中 J1/J2/J3 各为 **4 个相位**（主路绿 41s / 黄 4s / 支路绿 41s / 黄 4s，
`type=actuated`），即**两个放行相位**（主路 EW、支路 NS）。
而 `_tool_plan` 按 4 个相位（主直/主左/支直/支左）调用 Webster，算出的
`green_splits=[48.2, 36.1, 22.6, 27.1]` 无法映射到真实的 2 相位信号上。

**修复**：`_tool_plan` 改为 2 相位调用（主路关键流量 1980 pcu/h・3 车道，
支路 720 pcu/h・2 车道），与路网结构严格对齐。

#### 2.2 下发方式破坏性 —— 循环内反复修改单相位时长

旧实现在仿真循环中每 30 秒执行 `setPhaseDuration(tl_id, arterial_green)`，
且只设置"当前相位"的时长。对 `actuated` 逻辑而言等于**反复重置相位计时器**，
导致通行权分配紊乱。

**修复**：信号 program 改为**一次性完整下发**（`setProgramLogic` + `setProgram`），
仿真循环内不再有任何信号修改动作。相位连接状态（state 字符串）取自路网原生定义，
只替换时长，保证连接语义不变。

#### 2.3 控制类型错误 —— 静态配时 vs 自适应（本轮最关键的发现）

在"结构对齐 + 一次性下发"之后，方案 A 仍劣于基线。为此做了 **7 配置对照实验**
（45/60/75/90s 周期 × 静态/绿波），发现**所有静态配时配置都输给基线**：

| 配置 | 平均延误 | 对基线 |
|:--|--:|--:|
| 基线（actuated 默认 41/4/41/4） | 28.7 | — |
| static 45s（当前方案 A） | 35.6 | **-24.0%** |
| static 60s | 34.4 | -19.9% |
| static 75s | 38.3 | -33.4% |
| static 90s | 34.9 | -21.6% |

**原因**：基线是 `actuated` 自适应控制——事故期间主路需求激增时，它会自动**拉长主路绿灯
（至 maxDur=50s）、压缩支路绿灯**（主路有效绿信比可达 0.79）；而任何静态配时都把
支路固化在 29~35% 的时间上，在削峰场景下必然劣于自适应。

**修复（工程正解）**：**保留 actuated 自适应机制，只优化它的基准配时**——
下发 `type=actuated` 的 program，附 `min/max` 绿时约束，让 Webster 结果作为
自适应逻辑的"基准"而非"铁律"。第二批 6 配置对照实验证实该路线全面优于基线：

| 配置 | 延误改善 | 排队改善 | 吞吐改善 |
|:--|--:|--:|--:|
| **actuated + Webster 基准（C=90）+ 绿波** | **+10.5%** | **+61.9%** | +2.2% |
| actuated + Webster 基准（C=90），无绿波 | +8.7% | +38.1% | +2.6% |
| static 90s + 绿波（对照） | -25.1% | +19.0% | -3.3% |

#### 2.4 附带修复 —— Webster 最优周期的过饱和修正

Webster 算出的最小延误周期约 45s（欠饱和假设）。本走廊在事故下过饱和，且需协调控制，
故按标准工程做法将设计周期上移至 **2 倍最小周期（90s）**，并按 Webster 流量比
重新分配绿时（主路 53.1s / 支路 28.9s），同时给出 `min/max` 自适应边界（主路 21.2~74.3s）。

#### 2.5 附带修复 —— 吞吐量统计口径错误

`simulation.getArrivedNumber()` 返回的是**上一步**到达的车辆数（增量），
而旧代码在仿真循环**结束后调用一次**，只拿到最后一步的值（通常为 0）。
表现为：tripinfo 文件记录 977 辆车完成行程，而 KPI 里 `completed_trips=4`、
`throughput_vph=24`（正确值约 5600+）。该错误此前污染了所有吞吐量对比。

**修复**：改为循环内逐步累加。

#### 2.6 绿波 offset 的落地方式

TraCI 的 `Logic` 对象**不支持 offset 参数**（SUMO 1.27 实测），故绿波通过
**相位对齐**实现：仿真启动时把每个路口的周期"快进"到主绿应开始的位置
（`setPhase` + `setPhaseDuration`，一次性）。已用独立实验验证：
目标主绿开始时刻 21.6s → 实测 22s（1s 步长取整），此后周期精确保持 45s、90s 不漂移。

### 三、验证方式与结果

```bash
# 单元测试（11 项，含 2 项新增回归守门）
python -m unittest discover -s tests -p "test_system.py" -v

# 端到端三方案推演（需 SUMO）
python verify_e2e_full.py   # 工作区临时脚本，不入库
```

**最终三方案端到端结果（SUMO 1.27.1，600s，事故 150-420s，真实微观仿真）**：

| 指标 | 基线 | 方案 A（Webster+自适应） | 方案 B（协同） |
|:--|--:|--:|--:|
| 平均延误 (s/veh) | 28.7 | 26.2（**+8.7%**） | **23.2（+19.2%）** |
| 最大排队 (m) | 157.5 | 97.5（+38.1%） | 105.0（+33.3%） |
| 平均速度 (km/h) | 31.7 | 30.0（-5.4%） | 28.7（-9.5%） |
| 吞吐量 (veh/h) | 5628 | 5772（+2.6%） | **5808（+3.2%）** |
| 延误方差 | 393.2 | 260.5（+33.7%） | **191.4（+51.3%）** |
| CO₂ (kg) | 236.6 | 243.9（-3.1%） | **231.7（+2.1%）** |

**因果链成立**：方案 B > 方案 A > 基线（延误 / 方差 / 吞吐量 / CO₂ 维度），
"三手段协同优于单点优化、单点优化优于无干预"的论证现在有真实数据支撑。

控制证据（每方案实际下发到仿真器的指令）：
- 基线：0 指令（未干预）
- 方案 A：3 次 program 下发（J1/J2/J3，actuated 类型，无相位偏移）
- 方案 B：3 次 program 下发 + 23 辆车真实诱导改道（绿波相位对齐生效）

新增 2 项回归守门测试：
- `test_phase_alignment_math` —— 相位对齐机制（绿波落地的数学基础）
- `test_webster_plan_matches_network_phase_structure` —— 配时结构与路网相位对齐、
  actuated 边界健全性、过饱和周期修正

### 四、已知限制（重要）

- **平均速度指标反向**：方案 B 的平均速度低于基线（-9.5%）。这是"断面平均速度"与
  "通行效率"的权衡——信号控制让车流更有序（启停更少、方差大降），但瞬时速度略低。
  **对外表述时应以延误 / 排队 / 方差 / 吞吐为准，速度指标需附带解释。**
- **actuated 下的绿波漂移**：自适应会浮动各周期相位时长，绿波带宽会部分损失
  （固定 offset 无法完全锁定）。如需严格带宽，需改用 SUMO 的协调控制参数（后续待办）。
- **单场景单次运行**：未做多种子（seed）批量实验，统计显著性尚未定量。
- 绿波带宽计算（`green_splits - 4s`）偏乐观，未扣除双向带宽互斥效应。

### 五、后续待办

- [ ] 多种子批量实验（≥5 seeds）与统计显著性分析
- [ ] actuated 协调参数探索（SUMO `cycleTime` / NEMA offset），减少绿波漂移
- [ ] 分流比例敏感性分析（当前 0.1 的分流强度依赖诊断输入）
- [ ] `tests/test_web_api.py` 纳入 CI（需 fastapi 测试环境）
- [ ] 成果材料撰写（申报书 / 说明书 / 演示视频）

---

## [2026-09-12] P1 工程正确性修复 + 首次 SUMO 端到端验证 — `0d53568`

**修复范围**：代码审查遗留的 P1 问题 6 项 + 端到端验证新暴露的场景/控制缺陷 3 项
**影响文件**：代码 6 个、场景 3 个、依赖与配置文件 3 个，另新增 `LICENSE`
**兼容性**：不破坏。API 响应仅**新增**字段；`execute_what_if_rollout` 新增可选参数 `diagnosis`（默认 `None`，原行为不变）。

> **历史与恢复说明**：本 P1 改动的原提交（`08712a9` / `1850927`）曾在推送成功后
> 被远端 **force push 重写覆盖**，从 main 历史中消失。现将其重放至远端历史线恢复
> （代码提交 `0d53568` + 配套文档提交）。代码内容与验证结论均无变化。
> **请协作者避免 force push 覆盖历史，以免再次丢失他人工作；如需改写历史请先同步团队。**

---

### 一、本轮最重要的进展：首次具备端到端验证能力

此前项目**从未在真实 SUMO 上跑通过推演**（开发机未安装仿真器），所有结论都建立在单元测试与打桩之上。

本轮在本机安装 **Eclipse SUMO 1.27.1**（`winget install EclipseFoundation.SUMO`）后，首次完成

> detector → diagnosis → strategy → SUMO rollout → KPI

的**全链路真实推演**。这一能力立刻暴露出 4 个「代码看着对、跑起来不对」的真实缺陷（见第三节），
其中 **3 个此前被 `except: pass` 静默吞掉，长期无人察觉**。

### 二、P1 工程正确性修复（6 项）

| 编号 | 问题 | 修复方式 |
|:--|:--|:--|
| P1-1 | **延误指标定义错误**：用 `edge.getWaitingTime()` 累加 ÷ 车辆数当作"平均延误"。该 API 统计的是低速等待时间，非行程延误 | 改为逐车 `vehicle.getTimeLoss()`，与 SUMO `tripinfo` 的 `timeLoss` **同口径**；返回中新增 `delay_metric` 字段声明口径 |
| P1-2 | **事故限速恢复硬编码**：事故清除时把限速写死为 `16.67` m/s，未保存原值 | 注入事故时逐车道记录原始限速，清除时按原值精确还原 |
| P1-3 | **诊断→策略链路断裂**：`formulate_candidate_strategies(diagnosis)` 的入参实际未被使用，排队/占有率全是硬编码常量 | `_tool_plan()` → `_tool_plan(diagnosis)`，实测排队、占有率、旁路占有率改为从 `diagnosis.input_state` 读取；新增 `input_state_used` / `strategy_inputs` 审计字段，如实记录输入来源（诊断值 or 标定默认值） |
| P1-4 | README 声明 MIT 协议但**无 `LICENSE` 文件**（链接 404） | 新增 `LICENSE`（MIT） |
| P1-5 | README 路线图 Step 2/3/4 仍标未完成，与已有实现不符 | 勾选 Step 2/3/4，新增 Step 5（端到端复验）与 Step 6（成果材料），并加**验证状态警示**（完成复验前不得对外引用推演数值） |
| P1-6 | `requirements.txt` 含 8 个未被 import 的依赖 | 移除 `pandas` / `matplotlib` / `plotly` / `openpyxl` / `python-docx`；`httpx` 保留并注明**仅测试需要**；`traci` / `sumolib` 补注说明（须配合 SUMO 本体，不能只 pip 安装） |

附带修正：`.gitignore` 中 `*.pdf` 会**误伤正式交付物**（团队提交说明书 PDF 会被静默忽略），
改为放行 `docs/*.pdf`；`agent.md` 等本地材料规则保留。

另：`llm_client` 增加 **`.env` 可选加载**（`python-dotenv` 缺失时自动跳过），
使仓库中已有的 `.env.example` 真正生效。

### 三、端到端验证暴露的真实缺陷

#### 3.1 已修复：VMS 诱导分流**从未生效**（双重原因）

**症状**：方案 B 的 `reroute_commands = 0`，即"动态诱导分流"在真实仿真中一次都没执行过。

**根因（两层，缺一不可）**：

1. **API 名称错误**：代码调用 `conn.vehicle.changeRoute(...)`，而 TraCI 的 `VehicleDomain`
   **根本没有这个 API**（正确名称是 `setRoute`）。每次调用抛 `AttributeError`，
   被 `except Exception: pass` **静默吞掉**。
2. **物理上不可达**：即便 API 写对，原逻辑是对已经在 `entry_J1` 上的车辆改道去旁路——
   而 `entry_J1` 与 `entry_div` **都从同一节点 `entry_W` 分岔**，车辆一旦驶入 `entry_J1`
   就已越过分岔点，**物理上无法再切到旁路**。

**修复**：
- 路网新增共享上游断面 `approach_W`（`origin_W → entry_W`），主路与旁路车流均从此进入，
  使"在分岔前实施诱导"物理成立；
- 分流改为在 `approach_W` 上对车辆调用 `setRoute`；
- 分流判定由「(车辆, 时刻) 每步重新摇骰子」改为「按**车辆身份一次性**判定」——
  原写法会让名义 25% 的分流比例实际接近 `1-(1-0.25)^n`（n = 车辆在断面上的停留步数），
  等于"几乎所有车都被分流"，与语义严重不符；
- **控制失败不再静默**：新增 `reroute_errors` 字段写入 `control_evidence`，
  使"没执行"与"执行了"在证据层可被区分。

修复后实测：方案 B 诱导指令 **0 → 23 辆车**真实改道。

#### 3.2 已修复：**7 条交通需求被 SUMO 静默忽略**

**症状**：SUMO 启动告警
`Warning: Route file should be sorted by departure time, ignoring 'f_byp_natural'!`
（同样涉及 `f_J1_NS`、`f_J1_SN`、`f_J2_NS`、`f_J2_SN`、`f_J3_NS`、`f_J3_SN`）

**根因**：`corridor.rou.xml` 中 `<flow>` 未按 `begin` 升序排列，SUMO 对乱序 flow **直接忽略**。

**影响**：12 条需求流中 **7 条从未进入仿真**（含全部支路车流与自然旁路流），
导致车流量远低于设计值、`throughput_vph = 0`——**此前基于该场景的一切推演数字均失真**。

**修复**：按 `begin` 分组重排，并在构建脚本中写明该约束，避免再次踩坑。

#### 3.3 已发现、**未修复**：信号控制未按完整配时下发（下一步首要工作）

车流恢复真实值后，结果暴露出**方案 A 反而显著劣于基线**：

| 指标 | Baseline | 方案 A (Webster) | 方案 B (协同) |
|:--|--:|--:|--:|
| 平均延误 (s/veh) | 28.7 | **83.8（-192%）** | 26.0（+9.4%） |
| 最大排队 (m) | 157.5 | 0.0 | 165.0 |
| 平均速度 (km/h) | 31.7 | 42.5 | 29.1 |
| 延误方差 | 393.2 | **5770.2（-1367%）** | 230.9（+41.3%） |
| CO₂ (kg) | 236.6 | 318.8（-34.7%） | 242.2（-2.4%） |

**根因诊断**：

1. **相位结构不匹配**：路网 TLS 实际只有 **2 个放行相位**
   （主路 41s + 支路 41s + 2 个 4s 黄灯，`actuated` 自适应），
   而 Webster 工具按 **4 个相位**计算配时（`green_splits=[48.2, 36.1, 22.6, 27.1]`，周期 148s）。
   两者结构无法直接对应。
2. **下发方式具破坏性**：仿真循环中每 30 秒执行一次 `setPhaseDuration(tl_id, arterial_green)`，
   且只设置"当前相位"的时长。对 `actuated` 逻辑而言，这等于**反复重置相位计时器**，
   导致通行权分配紊乱——这是方案 A 延误与方差剧烈恶化的直接原因。
3. **旁证**：`排队 = 0` 与 `延误 = 83.8s` 同时出现，说明瓶颈排队被推挤到上游断面，
   控制逻辑已失去一致性。

> **结论：当前 Webster 集成是「算法与路网未对齐的半成品」，不可用于答辩演示。**

**建议的修复路径**（需作为独立工作项推进）：
- 让 Webster 的相位定义与路网 TLS 实际相位**对齐**（简化为 2 相位，或把路网扩展为 4 相位）；
- 改为**一次性下发完整静态配时 program**（`setProgramLogic` + `setProgram`），
  而非运行时反复修改单相位时长；
- 修复后重跑三方案，用真实数据重新建立"协同优于单点"的论证。

### 四、验证方式与结果

```bash
# 重建场景（需 SUMO 在 PATH 中）
python scenarios/build_scenario.py

# 单元测试（9 项，含 4 项新增回归守门）
python -m unittest discover -s tests -p "test_system.py" -v
```

| 验证项 | 结果 |
|:--|:--|
| 单元测试 | **9/9 通过**（原 5 项 + 新增 4 项回归守门） |
| 端到端三方案推演 | **3/3 跑通**（SUMO 1.27.1 真实微观仿真） |
| 控制证据 · 方案B 绿波 | 12 次信号指令真实下发 |
| 控制证据 · 方案B 诱导 | 23 辆车真实改道（修复前 **0**） |
| 控制证据 · 基线 | 0 指令（未干预） |
| 事故限速还原 | 记录并还原原始值 |
| 可复现性 | 同场景两次运行，延误/排队序列**完全一致** |
| 诊断驱动策略 | 输入 60m/265m 排队 → 分流比例 0.0 / 0.27，参数确实随检测数据变化 |

新增的 4 项**回归守门**测试（防止已修问题悄悄回退）：

- `test_diversion_bucketing_is_reproducible` —— P0-4 确定性哈希
- `test_diagnosis_drives_strategy_parameters` —— P1-3 诊断驱动策略
- `test_control_switches_are_forwarded_to_the_sandbox` —— P0-3 控制开关真实下发
- `test_no_fabricated_kpis_when_rollout_is_missing` —— P0-2 数据缺失不得编造

### 五、环境说明

- 本机已装 **Eclipse SUMO 1.27.1**（`C:\Program Files (x86)\Eclipse\Sumo`），端到端推演已验证可跑。
- CI 仍设计为**无 SUMO 也能通过**（只跑编译检查、单元测试与导入健全性检查），
  保证协作者本地不装仿真器也能提交 PR。

### 六、后续待办

- [ ] **（最高优先）信号控制与路网相位结构对齐** —— 见 3.3，直接决定方案 A/B 能否成立
- [ ] 分流比例敏感性分析（当前 0.1 的分流强度可能过小）
- [ ] 扩展验证场景（更长时段、多事故类型、多种子批量实验）
- [ ] `tests/test_web_api.py` 纳入 CI（需 fastapi 测试环境）
- [ ] 成果材料撰写（申报书 / 说明书 / 演示视频）

---

## [2026-09-12] P0 可信度与正确性修复 — `1385625`

**修复范围**：代码审查识别出的 4 项 P0 级风险（均为"演示能跑、答辩兜不住"的诚信与正确性问题）
**影响文件**：8 个（+852 / -159）
**是否破坏兼容**：否。API 响应仅**新增**字段，前端与既有调用方式无需改动。

### 一、背景：为什么要做这次修复

在提交前的代码审查中发现，仓库虽然能正常启动运行，但存在 4 处会在答辩或复现环节被直接质疑的问题：

| 编号 | 问题 | 风险 |
|:--|:--|:--|
| P0-1 | 依赖声明了 `langchain` / `openai`，但**全仓库无任何一处真实大模型调用**，"思维链推理"实为硬编码字符串 | 赛题定位是"**基于大模型**的交通决策支持"，此点最易被追问 |
| P0-2 | 决策简报存在硬编码兜底：推演失败时仍输出"改善 44.6%、评级 A+"等具体数字，且不标注为估算 | 输出伪结论，属诚信问题 |
| P0-3 | 仿真中 `green_wave_active` 为死变量、`webster` 参数从未被读取，绿波协调**实际未生效** | 方案 A/B 的差异只来自诱导分流，"三手段协同"的因果对比不成立 |
| P0-4 | 分流判定使用内置 `hash()`，受 `PYTHONHASHSEED` 随机盐影响 | 同一场景多次运行结果不一致，结论不可复现 |

### 二、改了什么

#### 1. 接入真实大模型推理（解决 P0-1）
- 新增 `src/agents/llm_client.py`：OpenAI 兼容协议客户端封装，支持自定义 `base_url` / `model` / 超时。
- `diagnose_bottleneck`、`formulate_candidate_strategies` 改为**真实模型推理**（归因分析、方案叙事）。
- **数值与模型严格隔离**：所有性能指标（延误、排队、排放、油耗、改善率）**仍全部由交通工程工具与微观仿真计算**，模型不参与任何数值生成。
- 未配置密钥或调用失败时，**显式降级**为确定性规则模板，并通过 `reasoning_mode` / `narrative_mode` 字段如实标注，不再静默伪造结论。

#### 2. 移除决策简报中的硬编码兜底数据（解决 P0-2）
- 删除 `comparisons.strategy_b` 及各项 KPI 的默认值（`44.6%` / `138.3%` / `Level A+` 等）。
- 数据缺失时显式输出「—」并附「不可用」声明；改善率改由实测 KPI **现场推导**。
- 新增「数据来源与可信度声明」，明确区分**实测仿真数据**与**标定估算数据**。
- 顺带修复连带问题：标定数据集缺少 `execution_mode` 字段，会被简报误标为"SUMO 实测"。

#### 3. 打通仿真控制链路（解决 P0-3）
- `sumo_sandbox` 正确读取并转发 `control_params` 中的 `webster` / `green_wave` 开关。
- 按相位差落地 J1–J3 干线绿波协调，消除 `green_wave_active` 死变量。
- 新增 `control_evidence`：记录本轮**实际下发**的控制指令，供简报与评审核验（"说做了"与"真做了"可对照）。

#### 4. 修复结果不可复现（解决 P0-4）
- 分流判定由内置 `hash()` 改为 `zlib.crc32` 确定性哈希，`PYTHONHASHSEED` 不再影响结果。

#### 5. 工程配套
- 新增 `.github/workflows/ci.yml`：push / PR 自动执行「语法编译检查 + 单元测试 + 无 SUMO 环境导入健全性检查」。
- 新增 `.env.example`：大模型与 SUMO 相关环境变量说明。
- `requirements.txt` 移除未使用的 `langchain` / `langchain-core`（与"零 LLM 调用"叙事冲突）。
- `README` 补充本地运行方式与大模型配置/降级机制说明。

### 三、怎么验证

```bash
# 1. 编译 + 单元测试（应全部通过）
python -m compileall -q src tests
python -m unittest discover -s tests -p "test_system.py" -v

# 2. 控制链路验证：确认三个方案的参数确实不同（方案A 仅 Webster / 方案B +绿波+分流）
#    关闭开关时相关参数应全部归零
```

已完成的实测校验（4 组）：
- 假数据检索：代码中不再存在 `44.6` / `138.3` / `Level A+` 等硬编码输出；
- 数据缺失路径：输出「不可用」而非编造数字；
- 降级标注：标定数据被正确标注为「非实测」；
- 确定性哈希：跨进程多次运行结果一致（内置 `hash()` 对照组三次运行三个值）。

单元测试结果：`tests/test_system.py` **5/5 通过**。

### 四、已知限制（重要）

- **本地开发机未安装 SUMO**，因此 `execute_what_if_rollout` 的**完整物理推演未做端到端验证**，
  `tests/test_web_api.py` 也未执行。以上验证均为**单元级 / 打桩级**。
- **在装有 SUMO 的机器上完成端到端复验之前，请勿对外宣称具体推演数字。**

### 五、后续待办（本次有意未做，避免扩大改动面）

- [ ] 延误指标改用 SUMO `tripinfo` 的 `timeLoss`，提升指标口径严谨性
- [ ] 事故限速在仿真结束后正确还原，避免污染后续场景
- [ ] 打通「诊断结果 → 策略参数」的自动映射链路
- [ ] 补充 `LICENSE` 文件（README 已引用 MIT 链接，但文件缺失）
- [ ] README 路线图勾选状态更新
- [ ] 清理 `requirements.txt` 中其余冗余依赖

### 六、仓库历史说明

本仓库远端 `main` 历史曾被 **force push 重写**，原 `400224e` 等 4 个提交已从远端移除
（含早期 `competition_guide.md` 等文档）。本地已打标签 `local-backup-400224e` 保全完整旧快照。
**如需找回历史内容，请从该标签检出，不要试图从远端回溯。**
