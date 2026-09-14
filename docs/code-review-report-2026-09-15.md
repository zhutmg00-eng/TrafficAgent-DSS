# TrafficAgent-DSS 代码整体审查报告

- 审查日期：2026-09-15
- 审查对象：当前工作库 `D:\交通 智能体`
- Git 基线：`main` @ `8ef1ebf`（提交说明为 `docs(v2.3.2)`）
- 审查方式：只读代码审查、接口探针、核心测试、Playwright E2E、Python/JavaScript 编译检查

## 结论

项目已经形成较完整的 Python/FastAPI + 交通工程工具 + SUMO/中观推演 + 浏览器大屏分层结构，回归测试和降级路径也有基础。当前 HEAD 不适合直接暴露到不受信任的网络，也不适合把界面展示和全部指标直接当作比赛或论文定稿依据：LLM 配置端点存在未鉴权 SSRF/密钥外发边界，CORS 与它叠加放大了风险；百度 AK 可能出现在异常详情；前端多处把模型或后端文本直接写入 `innerHTML`；默认 SUMO 开关、状态徽章、策略地图和场景选择与实际执行路径存在不一致。

核心测试在本机通过，说明现有实现具备可运行性，但测试主要守住当前输出形状和预期收益，尚未覆盖上述安全边界及 UI/后端一致性。建议先处理 P0/P1 项，再同步技术方案和版本信息，最后再冻结对外材料。

## 审查范围与验证结果

检查了 `src/agents`、`src/simulation`、`src/tools`、`src/data`、`src/web`、`tests`、`experiments`、CI 配置、README、CHANGELOG 与技术方案。执行结果如下：

| 检查 | 结果 |
|---|---|
| `py -3.10 -m pytest -q` | 169 passed，14 个 E2E 按默认配置排除，1 warning，43 个 subtests 通过 |
| `py -3.10 -m pytest tests/e2e -m e2e -q` | 14 passed，2 warnings |
| `py -3.10 -m compileall -q src scripts experiments scenarios` | 通过 |
| `node --check src/web/static/js/dashboard.js` | 通过 |
| 物理推演探针 | `execution_mode=physical_sumo_sandbox`，300 秒/种子 1 三方案均完成；本机检测到 SUMO 可执行文件 |
| 中观场景探针 | 任意 `corridor_choice`/`congestion_type` 输入得到相同 KPI 与 `Beijing Xizhimen real road network (OSM)` 标签；`map_snapshot` 只有 `baseline/strategy_a/strategy_b` |
| CORS 探针 | `OPTIONS /api/llm/config` 对 `https://evil.example` 返回 `200`、`access-control-allow-origin: https://evil.example`、`allow-credentials: true` |
| SSRF 探针 | `POST /api/llm/detect-models` 使用 `http://127.0.0.1:9` 返回连接错误结果，证明后端按用户输入发起了服务端请求 |
| 覆盖率 | 未执行；环境没有 `coverage` 命令 |

## 发现的问题

### P0：上线或对外演示前必须处理

#### P0-1：LLM 配置/模型探测未鉴权，并允许 SSRF 与服务端密钥外发

- 证据：`src/web/app.py:471-518` 的 `/api/llm/detect-models`、`/api/llm/config`；`src/agents/llm_client.py:209-257` 直接拼接并请求传入的 `base_url`。
- 现场验证：传入 `http://127.0.0.1:9` 后端返回连接失败，说明请求从服务端发出，而不是仅在浏览器校验。
- 影响：攻击者可探测内网服务、访问云元数据或内部管理端点；随后 `/api/diagnose`、`/api/strategies` 等可能把服务端保存的 API key 发送到攻击者控制的兼容端点。配置端点还允许任意调用方热写运行中的模型配置。
- 建议：配置与探测接口置于认证后的管理面；只允许预置 provider/host 白名单；拒绝 loopback、私有、link-local、保留地址和非 HTTP(S) scheme；解析 DNS 后再次校验目标地址并防 DNS rebinding；限制端口、超时、响应体大小，并禁止把用户 URL 作为任意出站目的地。

#### P0-2：CORS 使用通配来源并开启 credentials

- 证据：`src/web/app.py:96-103`：`allow_origins=["*"]`、`allow_credentials=True`、方法和请求头全开放。
- 现场验证：任意 Origin 的预检响应会回显该 Origin 并允许凭据。
- 影响：和 P0-1 的无鉴权配置接口叠加后，任意网站都可尝试跨域操纵本服务；也不符合最小权限原则。
- 建议：通过环境变量配置明确的前端 Origin 白名单；开发、本地和生产分别配置；管理接口单独加认证和 CSRF 防护，不依赖 CORS 作为访问控制。

#### P0-3：百度服务端 AK 可能通过异常详情泄露

- 证据：`src/web/app.py:642-658` 将 `BAIDU_MAP_SERVER_AK` 放入 query 参数，并在 `resp.raise_for_status()` 异常时把 `str(e)` 放入 `HTTPException.detail`。
- 影响：httpx 的异常字符串包含完整请求 URL，非 2xx 响应可能把 `ak=...` 返回给调用方或前端；服务端日志也可能记录同一 URL。
- 建议：对外只返回固定错误类别和 request id；日志记录采用密钥脱敏后的 URL；不要把第三方异常原文直接放进 API 响应。

### P1：发布前应修复的安全与行为一致性问题

#### P1-1：前端多处 HTML 注入/XSS sink

- 证据：`src/web/static/js/dashboard.js:416-422` 直接渲染 `cot_reasoning`；`996-1094` 的 Markdown 解析器未先 HTML escape；`1161-1164` 的 toast、`1328-1365` 的地图 tooltip/侧栏、`1428-1455` 的行动清单均直接拼接后端文本到 `innerHTML`。
- 影响：模型/provider 返回的恶意文本、用户提交的诊断字段或路网名称可能执行脚本，造成会话劫持或页面篡改。
- 建议：统一 `escapeHtml`，只对有限 Markdown 语法做白名单转换；优先使用 `textContent`/DOM API；对 SVG 属性、数值和枚举值做类型校验；增加 CSP，并用恶意 HTML 的 E2E 用例验证不会执行。

#### P1-2：SUMO 开关的视觉默认值与请求默认值相反

- 证据：`src/web/static/index.html:142-147` 的 checkbox 默认 `checked`；`dashboard.js:118-128` 的 `state.runPhysicalSandbox=false`；`dashboard.js:297-300` 只有发生 change 事件才同步。
- 影响：用户打开页面看到“微观 SUMO 进程推演”已勾选，直接运行时仍发送 `run_physical_sandbox=false`，实际走中观引擎。
- 建议：初始化时从 `checkbox.checked` 写入 state，或让 JS 默认值与 HTML 一致；提交前在页面显示本次实际 `execution_mode`。

#### P1-3：SUMO 状态徽章没有读取真实状态

- 证据：`src/web/static/index.html:45-48` 固定显示“微观沙盒：就绪 (SUMO / TraCI)”，未发现 `dashboard.js` 对 `sandboxStatusBadge` 的更新逻辑；真实状态来自 `/api/status` 的 `simulation_engine.binary_exists`。
- 影响：没有 SUMO 或 net.xml 的机器仍会显示“就绪”，用户无法判断当前运行是否会降级。
- 建议：页面初始化请求 `/api/status`，按 `binary_exists`、场景文件和可用性显示“就绪/不可用/中观降级”，并与推演返回的 `execution_mode` 联动。

#### P1-4：地图“策略视图”切换不改变数据

- 证据：后端 `src/web/network_api.py:219-223` 返回 `{baseline, strategy_a, strategy_b}`；前端 `dashboard.js:1206-1212` 却读取 `data.map_snapshot.edges/live`。
- 影响：点击基线/策略按钮只改变标签和样式，地图继续使用基线 `edges/live`，视觉上无法比较策略效果。
- 建议：前端根据当前视图选择 `map_snapshot.baseline` 或 `map_snapshot.strategy_b`（并处理 A/B），或者后端统一返回前端所需的 `{edges, live}` 结构；增加 E2E 断言切换后路段颜色/数据确实变化。

#### P1-5：场景选择没有驱动实际仿真（已修复）

- 证据：`RolloutConfigInput` 在 `src/web/app.py:134-144` 接收 `corridor_choice`/`congestion_type`，但 `network_api.run_mesoscopic_rollout`（`src/web/network_api.py:169-223`）不接收它们；SUMO 固定使用 `corridor.sumocfg` 与 `J1_J2`（`src/simulation/sumo_sandbox.py:256-258,421-424`）。物理响应还明确写出所选标签仅为展示用途（`src/web/app.py:844-854`）。
- 状态：中观推演现在将走廊/事故标签映射为显式需求与通行能力参数，并返回所选标签及实际参数；微观 SUMO 仍明确标注使用固定标定走廊。

#### P1-6：事故时间滑块可以生成后端拒绝的窗口

- 证据：`index.html:91-107` 的 duration 最小 300、start 最大 300、end 最小 300；`dashboard.js:242-279` 在 duration/start 变化时可能把 `incident_start` 与 `incident_end` 调成相等。
- 影响：提交 payload 时触发 Pydantic 的 `incident_start < incident_end` 校验，返回 422；用户只能看到运行失败提示。
- 建议：动态设置 start 的最大值为 `duration - 最小事故时长`，同步调整两个滑块，并在提交前统一校验 `0 <= start < end <= duration`。

#### P1-7：SUMO 延误统计口径已明确，但仍非逐车最终延误

- 证据：`src/simulation/sumo_sandbox.py:534-554` 每 5 秒对当前 active vehicles 调用累计 `getTimeLoss()`，再把样本序列交给 evaluator 求均值。
- 状态：字段已改名为 `active_vehicle_cumulative_time_loss_sample_mean_s` 并明确“非逐车最终均值”；若用于正式论文，仍建议后续采集 tripinfo 后按完成车辆聚合。

#### P1-8：推理型模型参数固定，可能直接失败并静默降级（已修复）

- 证据：`src/agents/llm_decision.py:422-424` 固定 `temperature=0.2,max_tokens=900`；`src/agents/llm_client.py:365-379` 原样传给 `chat.completions.create`。
- 状态：LLM 客户端按模型名识别 reasoner/o 系列与 thinking 模型，分别使用 `max_completion_tokens` 或传统 `temperature`/`max_tokens`；实际降级模式仍通过 API 状态返回。

### P2：维护性、统计语义与工程卫生

#### P2-1：API 版本号与仓库版本需明确区分

- 证据：`src/web/app.py:84` 为 `APP_VERSION = "2.3.0"`；CHANGELOG 已明确 v2.3.2 为文档/行为发布，而 2.3.0 是 API 兼容版本。
- 结论：这不是功能错误，但版本语义容易被误读；建议后续将产品发布版本与 API 兼容版本分开命名并由单一来源生成。

#### P2-2：技术方案曾有 OSM、HBEFA、方差和 Reflexion 口径漂移（已修复）

- 证据：`docs/technical_proposal.md:52-55` 仍写“尚未接入 OSM”；`101-106` 已承认 `delay_variance` 是 5 秒网络平均延误时序方差且 Reflexion 未实现；`114-120` 仍把指标写成“行程时间方差”和“SUMO HBEFA”。而 README `251`、`300-305` 已描述 OSM 中观网络和当前实测口径。
- 状态：已在 `docs/technical_proposal.md` 修正 OSM 双轨网络、延误时序方差与排放接口口径；未实现的 Reflexion 仍明确标注为后续工作。

#### P2-3：依赖未锁定，CI 不覆盖 E2E

- 证据：`requirements.txt` 主要使用 `>=`；`.github/workflows/ci.yml:29-40` 只安装少量核心包并运行单测，未安装 Playwright/Chromium，也没有 E2E job。E2E 需显式 `-m e2e` 才会执行，默认 `pytest.ini` 的注释仍写“113 项”而当前核心测试为 169 项。
- 影响：上游依赖升级可能改变结果；浏览器回归只能在本地发现，CI 绿灯不代表大屏可用。
- 建议：提交 constraints/lock 文件；在 CI 增加可控的 E2E job 或明确发布门禁；同步 pytest 配置与测试数量说明。

#### P2-4：部分测试把当前模型收益写成硬性结论

- 证据：`tests/test_web_api.py:113-117` 固定要求策略 B 延误/排队/速度改善超过阈值；`tests/test_network_mesoscopic.py:85-98` 固定要求策略优于基线。
- 影响：模型、随机种子或参数改变时，测试可能因“收益变小”失败，而不是捕捉逻辑错误；反过来也没有验证守恒、边界和控制是否真正到达仿真器。
- 建议：保留少量可解释的回归基准，同时把主要断言改为结构、非负、有限值、守恒、窗口约束、控制证据和统计置信区间等不变量。

#### P2-5：评测器缺失总量仍以 0 表示（已修复）

- 证据：`src/tools/evaluator.py:73-105` 在缺少 CO2、fuel、completed trips 或 duration 时默认 0/600，并返回 `co2_emissions_kg=0`、`fuel_liters=0`、`throughput_vph=0`。
- 状态：`PerformanceEvaluator` 现在将缺失或非有限总量返回为 `None`，实测零值仍保持为 0；新增完整性测试覆盖两种情况。

#### P2-6：Webster 工具没有防御 NaN/Inf

- 证据：`src/tools/webster.py:64-68,121-125` 使用 `max(0.0, float(q))` 等转换，但 NaN 仍可穿透并生成 NaN 结果。
- 影响：公开工具接口或实验输入一旦包含非有限值，可能污染控制计划和后续 JSON。
- 建议：对流量、车道数、损失时间、周期等统一做 `math.isfinite` 与范围校验，异常时返回明确 `ValueError`。

#### P2-7：多种子置信区间口径不统一

- 证据：`src/agents/traffic_agent.py:1244-1261` 用正态近似 `1.96 * SEM`；`experiments/ablation.py:52-67` 使用小样本 t 分布。README/CHANGELOG 对外强调小样本 t-CI。
- 影响：API 与实验报告的显著性结论可能不同，用户难以判断差异来自数据还是统计方法。
- 建议：统一统计函数和自由度处理，或在输出中明确标注 `normal_approx`/`t_interval` 及适用样本量。

## 已验证的优点

- 模块边界清楚：交通智能体、LLM 客户端、控制工具、SUMO 沙盒、中观网络、FastAPI 和前端资源分层明确。
- Pydantic 对推演时间窗、种子和批次规模有基础校验；同步的长时推演由 FastAPI worker thread 承载，避免阻塞事件循环。
- 物理 SUMO、中观真实路网、标定数据三层路径会返回 `execution_mode`/`degraded`/`fallback_reason`，降级意图总体比“伪装成仿真成功”更透明。
- `compileall`、核心单测、显式 E2E 和浏览器脚本语法检查均可通过；当前环境还实际完成了一次物理三方案推演。
- `README.md:300-305,356-375` 已较诚实地披露 M3 单一 VMS 方案优于 M4 全组合、闭环尚未证明优于规则链等限制，后续材料应沿用这套谨慎口径。
- `git ls-files` 未发现已跟踪的真实 `.env`、私钥、数据库或凭据文件；仍应继续避免把本地赛事附件和环境文件加入提交。

## 建议的修复顺序

1. 先关闭或保护 LLM 配置/探测端点，收紧 CORS，修正百度异常脱敏；部署前确认没有公网可达的管理 API。
2. 统一前端输入状态和后端响应结构：SUMO 开关、状态徽章、地图策略视图、场景映射、事故窗口校验。
3. 修复所有 `innerHTML` 注入点并增加 XSS E2E；同时适配 reasoning 模型参数并显示真实降级原因。
4. 重新定义/实现逐车延误统计，给缺失 KPI 使用 `None` 语义，补充 NaN/Inf 边界测试，统一多种子 CI 计算方法。
5. 统一版本来源，更新技术方案和测试数量说明；锁定依赖并把 E2E 纳入可执行的发布门禁。
6. 在上述修复完成后重新运行核心测试、E2E、物理 SUMO、多种子实验，并保存带版本、场景、种子和 `execution_mode` 的结果，作为对外材料唯一数据源。

## 限制与未验证项

本次审查没有修改源代码，没有进行压力测试、渗透测试、真实百度 AK 调用、真实第三方 LLM 调用或多浏览器矩阵测试；覆盖率工具在环境中不可用。网络部署前仍需在隔离环境验证认证、出站访问控制、DNS rebinding 防护、日志脱敏和 CSP。
