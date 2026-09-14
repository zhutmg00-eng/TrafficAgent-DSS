/**
 * TrafficAgent-DSS: Modern Interactive Dashboard Controller
 * Asynchronous decoupled frontend interacting with FastAPI RESTful backend.
 * Features:
 *   - Real-time situational awareness & dynamic scenario switching
 *   - Live Chain-of-Thought (CoT) terminal reflection
 *   - What-If simulation rollout with interactive ECharts analytics
 *   - In-browser markdown report rendering, clipboard copy & direct download
 *   - Dark/Light dual-theme support with automatic local storage persistence
 */

// Scenario Presets Database
const SCENARIOS = {
  corridor_arterial: {
    name: '典型城市交通走廊 (主干道-瓶颈合流段)',
    incident_peak: {
      edge: 'J1_J2 (主干线合流段)',
      link_length_m: 300.0,
      speed_kmh: 8.2,
      queue_m: 165.0,
      occupancy: 0.82,
      bypass_occupancy: 0.28,
      speed_delta: '▼ -72.6% (严重阻滞)',
      queue_delta: '▲ +110m (逼近回溢线)',
      occ_delta: '▲ 超出容量设计阈值',
      bypass_delta: '● 备用容量充沛，适宜分流',
      alert_title: '系统警报：走廊瓶颈段发生突发占道拥堵',
      alert_desc: '核心节点 J1_J2 排队已达 165 米 (库容比 55%)。若无干预，将在 180 秒内引发 J1 交叉口全面回溢死锁！',
      alert_badge: '死锁高风险 (Level 4)'
    },
    overflow: {
      edge: 'J1_J2 (主干线合流段)',
      link_length_m: 300.0,
      speed_kmh: 11.5,
      queue_m: 140.0,
      occupancy: 0.76,
      bypass_occupancy: 0.32,
      speed_delta: '▼ -61.2% (失衡溢流)',
      queue_delta: '▲ +85m (排队回退)',
      occ_delta: '▲ 接近容量饱和线',
      bypass_delta: '● 备用容量良好，可分流',
      alert_title: '系统警报：常态化高峰交叉口相交车流失衡溢流',
      alert_desc: '关键进口道车流积压，下游放行受阻，出现渐进式排队向上游回溯现象！',
      alert_badge: '溢流预警 (Level 3)'
    },
    bottleneck_reduce: {
      edge: 'J1_J2 (主干线施工缩减段)',
      link_length_m: 300.0,
      speed_kmh: 5.8,
      queue_m: 210.0,
      occupancy: 0.89,
      bypass_occupancy: 0.25,
      speed_delta: '▼ -80.5% (极端降速)',
      queue_delta: '▲ +155m (严重排队)',
      occ_delta: '▲ 占道瓶颈严重失衡',
      bypass_delta: '● 备用容量充沛，急需分流',
      alert_title: '系统警报：占道施工车道骤减致瓶颈通行能力腰斩',
      alert_desc: '3车道汇流至单车道，瓶颈通过能力骤降65%，排队长度迅速突破200米！',
      alert_badge: '死锁极高风险 (Level 4+)'
    }
  },
  corridor_2nd_ring: {
    name: '城市快速路合流区 (西二环瓶颈合流走廊)',
    incident_peak: {
      edge: '西二环主路合流区 (西直门-官园段)',
      link_length_m: 400.0,
      speed_kmh: 6.5,
      queue_m: 235.0,
      occupancy: 0.91,
      bypass_occupancy: 0.34,
      speed_delta: '▼ -78.3% (快速路瘫痪)',
      queue_delta: '▲ +170m (合流带积压)',
      occ_delta: '▲ 严重超载 (91%)',
      bypass_delta: '● 辅路与平行通道尚有余量',
      alert_title: '系统警报：西二环快速路主线严重车流交织滞留',
      alert_desc: '西直门至官园合流区排队已达 235 米 (库容比 59%)，匝道汇流冲突加剧主线拥堵激波！',
      alert_badge: '重度拥堵 (Level 4+)'
    },
    overflow: {
      edge: '西二环主路合流区 (西直门-官园段)',
      link_length_m: 400.0,
      speed_kmh: 9.8,
      queue_m: 185.0,
      occupancy: 0.84,
      bypass_occupancy: 0.38,
      speed_delta: '▼ -67.3% (间歇交织停滞)',
      queue_delta: '▲ +120m (主线持续长排队)',
      occ_delta: '▲ 临界饱和',
      bypass_delta: '● 辅路通行顺畅',
      alert_title: '系统警报：快速路出入口交织区常态高峰溢流',
      alert_desc: '连续进出匝道车流反复强行并线导致交通激波反向传播，主线车速大幅降低！',
      alert_badge: '重度预警 (Level 3+)'
    },
    bottleneck_reduce: {
      edge: '西二环主路养护施工区',
      link_length_m: 400.0,
      speed_kmh: 4.8,
      queue_m: 290.0,
      occupancy: 0.95,
      bypass_occupancy: 0.30,
      speed_delta: '▼ -84.0% (局部完全锁死)',
      queue_delta: '▲ +220m (逼近上游立交)',
      occ_delta: '▲ 95% 极端拥堵',
      bypass_delta: '● 辅路及远端绕行通道顺畅',
      alert_title: '系统警报：快速路主线临时施工封道引发大面积缓行',
      alert_desc: '主线两车道封闭养护，通行能力骤减70%，排队迅速蔓延逼近上游立交！',
      alert_badge: '系统级紧急 (Level 5)'
    }
  }
};

// Corridor design parameter used for the bypass-capacity label. It is a property of the
// modelled corridor (spare capacity of the parallel bypass), NOT a live detector reading —
// named explicitly so it cannot be mistaken for measured data.
const CORRIDOR_BYPASS_SPARE_CAPACITY_VPH = 1200;

// Application Global State
const state = {
  theme: localStorage.getItem('traffic_dss_theme') || 'dark',
  corridor: '北京典型交通走廊 (学院南路-交大东路瓶颈干线)',
  congestionType: '晚高峰潮汐高负荷 + 突发交通事故 (占道停靠)',
  duration: 600,
  incidentStart: 150,
  incidentEnd: 420,
  useRerouting: true,
  useGreenWave: true,
  useWebster: true,
  runPhysicalSandbox: false,
  // Selected SCENARIOS preset (supplies the KPI delta captions). Null until a scenario is
  // chosen; renderSituationalAwareness falls back to the default preset.
  scenarioConfig: null,
  
  trafficState: {
    bottleneck_edge: "J1_J2 (主干线合流段)",
    queue_m: 165.0,
    link_length_m: 300.0,
    speed_kmh: 8.2,
    occupancy: 0.82,
    bypass_occupancy: 0.28
  },
  diagnosis: null,
  strategies: null,
  rollout: null,
  reportMarkdown: ''
};

// ECharts instances
let radarChartInstance = null;
let timeSeriesChartInstance = null;

// DOM Ready Hook
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  bindEventHandlers();
  await loadInitialData();
  window.addEventListener('resize', handleResize);
});

// Theme Management
function initTheme() {
  document.documentElement.setAttribute('data-theme', state.theme);
  updateThemeIcon();
}

function toggleTheme() {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', state.theme);
  localStorage.setItem('traffic_dss_theme', state.theme);
  updateThemeIcon();
  rebuildCharts();
}

function updateThemeIcon() {
  const btn = document.getElementById('themeToggleBtn');
  if (btn) {
    btn.innerHTML = state.theme === 'dark' ? '☀️' : '🌙';
  }
}

// Scenario Switching
function onScenarioChanged() {
  const corridorSelect = document.getElementById('corridorSelect');
  const congestionSelect = document.getElementById('congestionSelect');

  const corridorKey = corridorSelect?.value || 'corridor_arterial';
  const congestionKey = congestionSelect?.value || 'incident_peak';

  const corridorData = SCENARIOS[corridorKey] || SCENARIOS.corridor_arterial;
  const config = corridorData[congestionKey] || corridorData.incident_peak;

  state.corridor = corridorData.name;
  state.congestionType = congestionSelect?.selectedOptions[0]?.text || '晚高峰潮汐高负荷 + 突发事故 (占道停靠)';

  state.trafficState = {
    bottleneck_edge: config.edge,
    queue_m: config.queue_m,
    link_length_m: config.link_length_m,
    speed_kmh: config.speed_kmh,
    occupancy: config.occupancy,
    bypass_occupancy: config.bypass_occupancy
  };

  // Update Alert Banner
  const alertTitle = document.getElementById('alertTitle');
  const alertDesc = document.getElementById('alertDesc');
  const alertBadge = document.getElementById('alertBadge');
  if (alertTitle) alertTitle.textContent = config.alert_title;
  if (alertDesc) alertDesc.textContent = config.alert_desc;
  if (alertBadge) alertBadge.textContent = config.alert_badge;

  // Update Situational Awareness KPIs (single implementation, shared with initial load —
  // the values used to be maintained in three places: the HTML markup, the SCENARIOS
  // presets and state.trafficState, which inevitably drifted apart).
  state.scenarioConfig = config;
  renderSituationalAwareness();

  showToast(`已加载场景：${state.corridor} · ${state.congestionType}`, 'info');
  loadRoadNetwork();
}

// Bind Event Handlers
function bindEventHandlers() {
  // Theme Toggle
  const themeBtn = document.getElementById('themeToggleBtn');
  if (themeBtn) themeBtn.addEventListener('click', toggleTheme);

  // Scenario Dropdowns
  const corridorSelect = document.getElementById('corridorSelect');
  if (corridorSelect) corridorSelect.addEventListener('change', onScenarioChanged);

  const congestionSelect = document.getElementById('congestionSelect');
  if (congestionSelect) congestionSelect.addEventListener('change', onScenarioChanged);

  // Sliders with Dynamic Bounding
  const durationSlider = document.getElementById('durationSlider');
  const durationVal = document.getElementById('durationVal');
  const incidentStartSlider = document.getElementById('incidentStartSlider');
  const incidentStartVal = document.getElementById('incidentStartVal');
  const incidentEndSlider = document.getElementById('incidentEndSlider');
  const incidentEndVal = document.getElementById('incidentEndVal');

  if (durationSlider && durationVal) {
    durationSlider.addEventListener('input', (e) => {
      state.duration = parseInt(e.target.value);
      durationVal.textContent = `${state.duration}s`;
      if (incidentEndSlider) {
        incidentEndSlider.max = state.duration;
        if (state.incidentEnd > state.duration) {
          state.incidentEnd = state.duration;
          incidentEndSlider.value = state.incidentEnd;
          if (incidentEndVal) incidentEndVal.textContent = `${state.incidentEnd}s`;
        }
      }
    });
  }

  if (incidentStartSlider && incidentStartVal) {
    incidentStartSlider.addEventListener('input', (e) => {
      state.incidentStart = parseInt(e.target.value);
      incidentStartVal.textContent = `${state.incidentStart}s`;
      if (incidentEndSlider && state.incidentEnd <= state.incidentStart) {
        state.incidentEnd = Math.min(state.duration, state.incidentStart + 30);
        incidentEndSlider.value = state.incidentEnd;
        if (incidentEndVal) incidentEndVal.textContent = `${state.incidentEnd}s`;
      }
    });
  }

  if (incidentEndSlider && incidentEndVal) {
    incidentEndSlider.addEventListener('input', (e) => {
      let val = parseInt(e.target.value);
      if (val <= state.incidentStart) {
        val = state.incidentStart + 30;
        e.target.value = val;
      }
      state.incidentEnd = val;
      incidentEndVal.textContent = `${state.incidentEnd}s`;
    });
  }

  // Governance Component Toggles
  const rerouteToggle = document.getElementById('toggleReroute');
  if (rerouteToggle) {
    rerouteToggle.addEventListener('change', (e) => state.useRerouting = e.target.checked);
  }

  const greenWaveToggle = document.getElementById('toggleGreenWave');
  if (greenWaveToggle) {
    greenWaveToggle.addEventListener('change', (e) => state.useGreenWave = e.target.checked);
  }

  const websterToggle = document.getElementById('toggleWebster');
  if (websterToggle) {
    websterToggle.addEventListener('change', (e) => state.useWebster = e.target.checked);
  }

  const sandboxToggle = document.getElementById('toggleSandbox');
  if (sandboxToggle) {
    sandboxToggle.addEventListener('change', (e) => state.runPhysicalSandbox = e.target.checked);
  }

  // Run Simulation Button
  const runBtn = document.getElementById('runSimulationBtn');
  if (runBtn) {
    runBtn.addEventListener('click', executeAgentDecisionPipeline);
  }

  // Report Export Actions
  const copyBtn = document.getElementById('copyReportBtn');
  if (copyBtn) copyBtn.addEventListener('click', copyReportToClipboard);

  const downloadBtn = document.getElementById('downloadReportBtn');
  if (downloadBtn) downloadBtn.addEventListener('click', downloadReportMarkdown);

  // Initialize LLM Switcher Modal
  initLlmModal();

  // Initialize Map Toggle
  initMapToggle();
}

// Initial Data Load
async function loadInitialData() {
  // Check active LLM status
  fetchLlmStatus();

  try {
    const res = await fetch('/api/baseline-data');
    if (res.ok) {
      const data = await res.json();
      state.trafficState = data.traffic_state;
      state.diagnosis = data.diagnosis;
      state.strategies = data.strategies;
      state.rollout = data.rollout;

      renderSituationalAwareness();
      renderCoTDiagnosis();
      renderStrategies();
      renderRolloutKPIs();
      renderExecutionModeNotice();
      renderCharts();
      await fetchDecisionReport();
    } else {
      console.warn('API baseline fetch failed, using fallback data');
      loadFallbackData();
    }
  } catch (err) {
    console.warn('Network issue fetching baseline, using client fallback:', err);
    loadFallbackData();
  }

  // Load road network on startup
  await loadRoadNetwork();
  // After baseline loaded, render action checklist
  loadActionPlan();
}

function loadFallbackData() {
  renderSituationalAwareness();
  renderCoTDiagnosis();
  renderStrategies();
  renderRolloutKPIs();
  renderExecutionModeNotice();
  renderCharts();
}

// Render Situational Awareness. Both the numbers and the deltas come from real sources:
// the values from state.trafficState (the loaded baseline / selected scenario), the deltas
// from the matching SCENARIOS preset. Nothing here is a hard-coded showcase figure.
function renderSituationalAwareness() {
  const s = state.trafficState || {};
  const cfg = state.scenarioConfig || SCENARIOS.corridor_arterial.incident_peak;

  const speedEl = document.getElementById('kpiSpeed');
  const queueEl = document.getElementById('kpiQueue');
  const occEl = document.getElementById('kpiOcc');
  const bypassEl = document.getElementById('kpiBypass');
  const speedDelta = document.getElementById('kpiSpeedDelta');
  const queueDelta = document.getElementById('kpiQueueDelta');
  const occDelta = document.getElementById('kpiOccDelta');
  const bypassDelta = document.getElementById('kpiBypassDelta');

  if (speedEl) speedEl.textContent = s.speed_kmh === undefined || s.speed_kmh === null ? NO_DATA : `${s.speed_kmh} km/h`;
  if (queueEl) queueEl.textContent = s.queue_m === undefined || s.queue_m === null ? NO_DATA : `${s.queue_m} m`;
  if (occEl) occEl.textContent = s.occupancy === undefined || s.occupancy === null ? NO_DATA : `${Math.round(s.occupancy * 100)}%`;
  if (bypassEl) {
    bypassEl.textContent = s.bypass_occupancy === undefined || s.bypass_occupancy === null
      ? NO_DATA
      : `${CORRIDOR_BYPASS_SPARE_CAPACITY_VPH.toLocaleString('en-US')} veh/h (${Math.round(s.bypass_occupancy * 100)}%)`;
  }

  if (speedDelta) speedDelta.textContent = cfg.speed_delta || NO_DATA;
  if (queueDelta) queueDelta.textContent = cfg.queue_delta || NO_DATA;
  if (occDelta) occDelta.textContent = cfg.occ_delta || NO_DATA;
  if (bypassDelta) bypassDelta.textContent = cfg.bypass_delta || NO_DATA;
}

// Render CoT Diagnosis Terminal
function renderCoTDiagnosis() {
  const terminalBody = document.getElementById('cotTerminalBody');
  if (!terminalBody) return;

  // No fabricated reasoning. This panel used to fall back to a five-step chain-of-thought
  // that quoted specific detector readings (8.2 km/h, 82%, 165 m) even when no diagnosis had
  // been produced, i.e. it presented invented analysis as the agent's output.
  const steps = state.diagnosis?.cot_reasoning || [];

  if (!steps.length) {
    terminalBody.innerHTML =
      '<div class="cot-step"><span class="cot-step-text">' +
      '尚未获得诊断结果 —— 请先执行态势诊断，或点击「一键协同决策」运行完整流水线。' +
      '</span></div>';
    return;
  }

  terminalBody.innerHTML = steps.map(step => {
    const match = step.match(/^(\d+\.\s*【[^】]+】)(.*)$/);
    if (match) {
      return `<div class="cot-step"><span class="cot-step-tag">${match[1]}</span><span class="cot-step-text">${match[2]}</span></div>`;
    }
    return `<div class="cot-step"><span class="cot-step-text">${step}</span></div>`;
  }).join('');
}

// Render Candidate Strategies
function renderStrategies() {
  const stratA = state.strategies?.strategy_a;
  const stratB = state.strategies?.strategy_b;

  const cycleA = document.getElementById('stratACycle');
  if (cycleA && stratA) cycleA.textContent = `${stratA.cycle_length}s`;

  const cycleB = document.getElementById('stratBCycle');
  if (cycleB && stratB) cycleB.textContent = `${stratB.cycle_length}s`;

  const rerouteB = document.getElementById('stratBReroute');
  if (rerouteB && stratB) rerouteB.textContent = `${Math.round(stratB.reroute_ratio * 100)}%`;
}

// Shared "no data" token for every KPI surface. The dashboard must never fall back to
// plausible-looking constants: if the backend did not return a value, the card shows this
// instead of a number nobody computed. (An earlier revision carried hard-coded showcase
// figures such as 44.6% / 138.3% and a [92,95,88,90,85] radar, which meant a failed request
// still rendered a full, confident-looking result.)
const NO_DATA = '—';

// Single source for every ECharts font size. Chart labels at the ECharts default (12px) were
// noticeably undersized next to the rest of the UI on 1366px laptop panels; centralising the
// values here keeps radar and time-series axes consistent and easy to tune.
const CHART_FONT = { axis: 13, name: 13, legend: 13 };

function numOrNull(v) {
  if (v === null || v === undefined || v === '' ) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// Render Rollout KPIs
function renderRolloutKPIs() {
  if (!state.rollout) return;
  const comp = state.rollout.comparisons?.strategy_b || null;
  const kpis = state.rollout.kpis?.strategy_b || null;

  const delayV = numOrNull(comp?.delay_improvement_pct);
  const queueV = numOrNull(comp?.queue_improvement_pct);
  const speedV = numOrNull(comp?.speed_improvement_pct);
  const tpV = numOrNull(comp?.throughput_improvement_pct);
  const co2V = numOrNull(comp?.co2_improvement_pct);

  const delayImp = document.getElementById('rolloutDelayImp');
  const queueImp = document.getElementById('rolloutQueueImp');
  const speedImp = document.getElementById('rolloutSpeedImp');
  const tpImp = document.getElementById('rolloutTpImp');
  const co2Imp = document.getElementById('rolloutCo2Imp');

  if (delayImp) delayImp.textContent = delayV === null ? NO_DATA : `-${delayV}%`;
  if (queueImp) queueImp.textContent = queueV === null ? NO_DATA : `-${queueV}%`;
  if (speedImp) speedImp.textContent = speedV === null ? NO_DATA : `${speedV >= 0 ? '+' : ''}${speedV}%`;
  if (tpImp) tpImp.textContent = tpV === null ? NO_DATA : `${tpV >= 0 ? '+' : ''}${tpV}%`;
  if (co2Imp) co2Imp.textContent = co2V === null ? NO_DATA : `-${co2V}%`;

  const delayAct = document.getElementById('rolloutDelayAct');
  const queueAct = document.getElementById('rolloutQueueAct');
  const speedAct = document.getElementById('rolloutSpeedAct');
  const tpAct = document.getElementById('rolloutTpAct');
  const co2Act = document.getElementById('rolloutCo2Act');

  const delayS = numOrNull(kpis?.avg_delay_s);
  const queueM = numOrNull(kpis?.max_queue_m);
  const speedK = numOrNull(kpis?.avg_speed_kmh);
  const tput = numOrNull(kpis?.throughput_vph);
  const co2K = numOrNull(kpis?.co2_emissions_kg);

  if (delayAct) delayAct.textContent = delayS === null ? NO_DATA : `降至 ${delayS} s/veh`;
  if (queueAct) queueAct.textContent = queueM === null ? NO_DATA : `缩减至 ${queueM} m`;
  if (speedAct) speedAct.textContent = speedK === null ? NO_DATA : `至 ${speedK} km/h`;
  if (tpAct) tpAct.textContent = tput === null ? NO_DATA : `达 ${tput} veh/h`;
  if (co2Act) co2Act.textContent = co2K === null ? NO_DATA : `降至 ${co2K} kg`;

  // Overall effectiveness grade (F3). Was previously never written by any code path, so the
  // header kept the static "综合评级：待推演评估" even after a successful run. The grade is
  // Nonesafe: when the backend cannot compute it (e.g. no delay baseline) we fall back to an
  // explicit "不可用" label rather than inventing a letter.
  const ratingEl = document.getElementById('rolloutOverallRating');
  if (ratingEl) {
    const grade = comp?.overall_effectiveness_grade;
    if (grade) {
      const g = String(grade);
      const glyph = g.startsWith('A') ? '🟢' : (g.startsWith('B') ? '🟡' : (g.startsWith('C') ? '🟠' : '🔴'));
      ratingEl.textContent = `综合评级：${glyph} ${g}`;
    } else if (comp) {
      ratingEl.textContent = '综合评级：不可用（缺少基线对比指标）';
    } else {
      ratingEl.textContent = '综合评级：待推演评估';
    }
  }

  renderHeroConclusion(comp, kpis);
}

// Hero conclusion card (F4). One plain-language sentence + the headline number, sourced only
// from the payload the backend actually returned. Never invents a saving: if the comparison is
// missing the card says so and the number stays as the NO_DATA dash.
function renderHeroConclusion(comp, kpis) {
  const heroValue = document.getElementById('heroValue');
  const heroHeadline = document.getElementById('heroHeadline');
  const heroSub = document.getElementById('heroSub');
  if (!heroValue || !heroHeadline || !heroSub) return;

  const delayV = numOrNull(comp?.delay_improvement_pct);
  const delayS = numOrNull(kpis?.avg_delay_s);
  const mode = state.rollout?.execution_mode || '';
  const isLive = state.rollout?.degraded !== true && !mode.startsWith('calibrated');

  if (delayV === null) {
    heroValue.textContent = NO_DATA;
    heroHeadline.textContent = '尚未获得方案 B 的对比结论 —— 请运行推演或检查执行模式与控制证据。';
    heroSub.textContent = '本卡片只展示由工具或仿真实际计算出的数值；无数据时保持空态。';
    return;
  }

  heroValue.textContent = `-${delayV}%`;
  const delayPart = delayS === null ? '' : `，方案 B 实测车均延误降至 ${delayS} s/veh`;
  heroHeadline.textContent = `启用智能体协同方案后，车均延误较基线降低 ${delayV}%${delayPart}。`;
  heroSub.textContent = isLive
    ? '数据来源：SUMO 微观物理推演（三方案同参数对照）。'
    : '数据来源：标定经验模型（未运行物理沙盒），结论仅供方向性参考。';
}

// Show a prominent banner whenever the rollout payload is NOT a live SUMO measurement
// (calibrated empirical data or a fallback after a sandbox failure). The banner is the
// visual guarantee that downgraded data can never pass as measured output on screen.
function renderExecutionModeNotice() {
  const banner = document.getElementById('executionModeBanner');
  const degradedBanner = document.getElementById('degradedNoticeBanner');
  const rollout = state.rollout;
  const mode = rollout?.execution_mode || '';
  const degraded = rollout?.degraded === true || mode.startsWith('calibrated');
  const reason = rollout?.fallback_reason || '';

  if (banner) {
    if (degraded) {
      banner.textContent = reason
        ? `⚠ 当前展示为降级/标定数据（非 SUMO 微观仿真实测结果）：${reason}`
        : '⚠ 当前展示为标定经验数据（非 SUMO 微观仿真实测结果）。请在控制面板勾选「微观 SUMO 进程推演」后重新运行，以获得实测指标。';
      banner.classList.add('visible');
    } else {
      banner.classList.remove('visible');
    }
  }

  if (degradedBanner) {
    if (degraded && reason) {
      degradedBanner.textContent = `⚠️ 系统推演降级提示：${reason}`;
      degradedBanner.style.display = 'block';
    } else {
      degradedBanner.style.display = 'none';
    }
  }
}

// Charts Rendering via ECharts
function renderCharts() {
  renderRadarChart();
  renderTimeSeriesChart();
}

function rebuildCharts() {
  if (radarChartInstance) {
    radarChartInstance.dispose();
    radarChartInstance = null;
  }
  if (timeSeriesChartInstance) {
    timeSeriesChartInstance.dispose();
    timeSeriesChartInstance = null;
  }
  renderCharts();
}

function handleResize() {
  if (radarChartInstance) radarChartInstance.resize();
  if (timeSeriesChartInstance) timeSeriesChartInstance.resize();
}

function renderRadarChart() {
  const dom = document.getElementById('radarChartContainer');
  if (!dom) return;

  const isDark = state.theme === 'dark';

  // Series are drawn only from scores the evaluator actually produced. The previous
  // fallback hard-coded [92,95,88,90,85] / [65,60,68,62,66], so an empty result set still
  // rendered a favourable-looking radar — a fabricated visual conclusion.
  const radar = state.rollout?.radar || null;
  const dimensions = (radar && radar.dimensions) || ["通行效率", "空间治堵", "容量释放", "运行平稳", "绿色低碳"];

  const SERIES_META = [
    { key: 'baseline',   name: '现状基线 (Do-Nothing)',   color: '#94a3b8', width: 2, dashed: true,  area: 'rgba(148, 163, 184, 0.2)' },
    { key: 'strategy_a', name: '方案 A (Webster自适应)',  color: '#f59e0b', width: 2, dashed: false, area: 'rgba(245, 158, 11, 0.25)' },
    { key: 'strategy_b', name: '方案 B (Agent协同DSS)',   color: '#10b981', width: 3, dashed: false, area: 'rgba(16, 185, 129, 0.35)' }
  ];

  const present = SERIES_META.filter(
    m => radar && Array.isArray(radar[m.key]) && radar[m.key].length === dimensions.length
  );

  if (!present.length) {
    if (radarChartInstance) {
      radarChartInstance.dispose();
      radarChartInstance = null;
    }
    dom.innerHTML = '<div class="chart-empty">暂无可用的评估分值（尚未获得 A/B 推演对比数据）。</div>';
    return;
  }

  if (typeof echarts === 'undefined') {
    renderCanvasRadarFallback(dom);
    return;
  }

  radarChartInstance = echarts.init(dom, isDark ? 'dark' : null);

  const option = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    legend: {
      bottom: 0,
      textStyle: { color: isDark ? '#94a3b8' : '#475569', fontSize: 12 },
      data: present.map(m => m.name)
    },
    radar: {
      indicator: dimensions.map(d => ({ name: d, max: 100 })),
      splitNumber: 4,
      axisName: {
        color: isDark ? '#cbd5e1' : '#334155',
        fontSize: 12,
        fontWeight: 600
      },
      splitLine: {
        lineStyle: { color: isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)' }
      },
      splitArea: {
        show: true,
        areaStyle: {
          color: isDark ? ['rgba(255,255,255,0.02)', 'rgba(255,255,255,0.05)'] : ['rgba(0,0,0,0.01)', 'rgba(0,0,0,0.03)']
        }
      },
      axisLine: {
        lineStyle: { color: isDark ? 'rgba(255, 255, 255, 0.15)' : 'rgba(0, 0, 0, 0.15)' }
      }
    },
    series: [{
      type: 'radar',
      data: present.map(m => ({
        value: radar[m.key],
        name: m.name,
        itemStyle: { color: m.color },
        lineStyle: m.dashed ? { width: m.width, type: 'dashed' } : { width: m.width },
        areaStyle: { color: m.area }
      }))
    }]
  };

  radarChartInstance.setOption(option);
}

function renderTimeSeriesChart() {
  const dom = document.getElementById('timeSeriesChartContainer');
  if (!dom) return;

  if (typeof echarts === 'undefined') {
    renderCanvasTimeSeriesFallback(dom);
    return;
  }

  const ts = state.rollout?.time_series;
  const steps = ts?.time_steps;
  const qBase = ts?.queue_baseline;
  const qA = ts?.queue_strategy_a;
  const qB = ts?.queue_strategy_b;

  // No synthesised curve. This block used to fabricate 61 points with Math.pow whenever the
  // rollout carried no time series, drawing a convincing "queue dissipation" chart that no
  // simulation had produced — directly at odds with the NO_DATA policy used everywhere else,
  // and the kind of "plausible output" that a reviewer would rightly call out.
  if (!Array.isArray(steps) || steps.length === 0 || !Array.isArray(qB)) {
    if (timeSeriesChartInstance) {
      timeSeriesChartInstance.dispose();
      timeSeriesChartInstance = null;
    }
    dom.innerHTML = '<div class="chart-empty">暂无推演时序数据（尚未运行推演，或本次推演未返回时间序列）。</div>';
    return;
  }

  // Re-init cleanly: without disposing, every re-render (theme toggle, second run) stacked
  // another ECharts instance on the same element.
  if (timeSeriesChartInstance) {
    timeSeriesChartInstance.dispose();
    timeSeriesChartInstance = null;
  }
  dom.innerHTML = '';
  const isDark = state.theme === 'dark';
  timeSeriesChartInstance = echarts.init(dom, isDark ? 'dark' : null);

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      formatter: function (params) {
        let text = `<b>推演时间: ${params[0].axisValue} 秒</b><br/>`;
        params.forEach(p => {
          text += `${p.marker} ${p.seriesName}: <b>${p.value} 米</b><br/>`;
        });
        return text;
      }
    },
    legend: {
      bottom: 0,
      textStyle: { color: isDark ? '#94a3b8' : '#475569', fontSize: CHART_FONT.legend },
      data: ['基线 (Do-Nothing)', '方案A (Webster)', '方案B (Agent协同DSS)']
    },
    grid: {
      left: '3%',
      right: '4%',
      top: '10%',
      bottom: '12%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: steps,
      name: '秒',
      nameTextStyle: { color: isDark ? '#94a3b8' : '#64748b', fontSize: CHART_FONT.name },
      axisLine: { lineStyle: { color: isDark ? '#334155' : '#cbd5e1' } },
      axisLabel: { color: isDark ? '#94a3b8' : '#64748b', fontSize: CHART_FONT.axis }
    },
    yAxis: {
      type: 'value',
      name: '排队长度 (m)',
      nameTextStyle: { color: isDark ? '#94a3b8' : '#64748b', fontSize: CHART_FONT.name },
      splitLine: {
        lineStyle: { color: isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.08)' }
      },
      axisLabel: { color: isDark ? '#94a3b8' : '#64748b', fontSize: CHART_FONT.axis }
    },
    series: [
      {
        name: '基线 (Do-Nothing)',
        type: 'line',
        data: qBase,
        smooth: true,
        lineStyle: { width: 2, color: '#ef4444', type: 'dashed' },
        itemStyle: { color: '#ef4444' }
      },
      {
        name: '方案A (Webster)',
        type: 'line',
        data: qA,
        smooth: true,
        lineStyle: { width: 2, color: '#f59e0b' },
        itemStyle: { color: '#f59e0b' }
      },
      {
        name: '方案B (Agent协同DSS)',
        type: 'line',
        data: qB,
        smooth: true,
        lineStyle: { width: 3, color: '#10b981' },
        itemStyle: { color: '#10b981' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(16, 185, 129, 0.3)' },
            { offset: 1, color: 'rgba(16, 185, 129, 0.02)' }
          ])
        },
        markPoint: {
          data: [
            { type: 'max', name: '峰值排队', symbolSize: 45 }
          ]
        }
      }
    ]
  };

  timeSeriesChartInstance.setOption(option);
}

// Fallback HTML5 Canvas renderers for offline support
function renderCanvasRadarFallback(dom) {
  dom.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:13px;">ECharts CDN 离线，已启用原生轻量雷达视图</div>';
}

function renderCanvasTimeSeriesFallback(dom) {
  dom.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-muted);font-size:13px;">ECharts CDN 离线，已启用原生轻量时序视图</div>';
}

// Progress veil (F13) + chain-of-thought status text (F2) helpers.
// A full four-stage pipeline (diagnose → strategies → mirror SUMO rollout → report) can take
// tens of seconds with no intermediate feedback beyond a toast, which reads as "frozen". The
// veil shows the current stage and an elapsed timer; cotStatusText keeps the terminal header
// honest (IDLE → RUNNING → CONVERGED/FAILED) instead of a permanently static label.
const TASK_VEIL_STEPS = [
  { at: 0,    fill: 8,  text: '解析路网态势并执行思维链诊断…' },
  { at: 1200, fill: 28, text: '调用交通工程工具合成候选策略…' },
  { at: 3000, fill: 55, text: '并行执行多方案数字孪生沙盒推演…' },
  { at: 9000, fill: 78, text: 'A/B 量化评估与导出决策报告…' },
  { at: 20000, fill: 92, text: '仍在计算中（SUMO 微观推演耗时较长）…' }
];
let taskVeilTimerId = null;
let taskVeilStart = 0;

function setCotStatus(text, kind) {
  const el = document.getElementById('cotStatusText');
  if (!el) return;
  el.textContent = text;
  el.classList.remove('status-idle', 'status-running', 'status-done', 'status-failed');
  if (kind) el.classList.add(`status-${kind}`);
}

function showTaskVeil() {
  const veil = document.getElementById('taskVeil');
  if (!veil) return;
  const stepEl = document.getElementById('taskVeilStep');
  const timerEl = document.getElementById('taskVeilTimer');
  const fillEl = document.getElementById('taskVeilFill');
  veil.classList.add('visible');
  taskVeilStart = Date.now();
  if (taskVeilTimerId) clearInterval(taskVeilTimerId);
  taskVeilTimerId = setInterval(() => {
    const elapsed = Date.now() - taskVeilStart;
    if (timerEl) timerEl.textContent = `${(elapsed / 1000).toFixed(1)}s`;
    let current = TASK_VEIL_STEPS[0];
    for (const s of TASK_VEIL_STEPS) {
      if (elapsed >= s.at) current = s;
    }
    if (stepEl && stepEl.textContent !== current.text) stepEl.textContent = current.text;
    if (fillEl) fillEl.style.width = `${current.fill}%`;
  }, 200);
}

function hideTaskVeil() {
  const veil = document.getElementById('taskVeil');
  if (taskVeilTimerId) {
    clearInterval(taskVeilTimerId);
    taskVeilTimerId = null;
  }
  if (veil) veil.classList.remove('visible');
  const fillEl = document.getElementById('taskVeilFill');
  if (fillEl) fillEl.style.width = '100%';
}

// End-to-End Decision Pipeline Execution
async function executeAgentDecisionPipeline() {
  const btn = document.getElementById('runSimulationBtn');
  if (btn) {
    btn.classList.add('loading');
    btn.disabled = true;
  }

  setCotStatus('STATUS: RUNNING', 'running');
  showTaskVeil();

  try {
    showToast('🧠 智能体正在解析路网态势并执行思维链诊断...', 'info');

    // 1. Diagnose Bottleneck with current traffic state
    const diagRes = await fetch('/api/diagnose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state.trafficState)
    });

    if (diagRes.ok) {
      const diagData = await diagRes.json();
      state.diagnosis = diagData.diagnosis;
      renderCoTDiagnosis();
    }

    // 2. Synthesize Candidate Strategies via traffic engineering tools
    const stratRes = await fetch('/api/strategies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ diagnosis: state.diagnosis })
    });

    if (stratRes.ok) {
      const stratData = await stratRes.json();
      state.strategies = stratData.strategies;
      renderStrategies();
    }

    // 3. What-If Parallel Simulation Rollout
    const rolloutPayload = {
      corridor_choice: state.corridor,
      congestion_type: state.congestionType,
      duration: state.duration,
      incident_start: state.incidentStart,
      incident_end: state.incidentEnd,
      use_rerouting: state.useRerouting,
      use_green_wave: state.useGreenWave,
      use_webster: state.useWebster,
      run_physical_sandbox: state.runPhysicalSandbox
    };

    showToast('🚀 正在执行多方案数字孪生沙盒推演与 A/B 量化评估...', 'info');

    const rolloutRes = await fetch('/api/rollout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rolloutPayload)
    });

    if (rolloutRes.ok) {
      const rolloutData = await rolloutRes.json();
      state.rollout = rolloutData;

      renderRolloutKPIs();
      renderExecutionModeNotice();
      renderCharts();

      // Render detector table from rollout
      const detectors = rolloutData.detectors;
      const engine = rolloutData.engine;
      if (detectors || engine) {
        renderDetectorTable(detectors, engine);
      }

      // Render action checklist
      loadActionPlan();

      // If rollout includes network data, update map
      if (rolloutData.network) {
        currentNetworkData = rolloutData.network;
        renderRoadNetwork();
      }

      // If rollout includes map_snapshot, update map view
      if (rolloutData.map_snapshot) {
        if (!currentNetworkData) currentNetworkData = {};
        currentNetworkData.map_snapshot = rolloutData.map_snapshot;
        if (mapViewState === 'strategy') {
          renderRoadNetwork();
        }
      }

      // 4. Export formatted decision report
      await fetchDecisionReport();

      const delayImp = numOrNull(rolloutData.comparisons?.strategy_b?.delay_improvement_pct);
      setCotStatus('STATUS: CONVERGED', 'done');
      showToast(
        delayImp === null
          ? '推演完成，但未返回方案 B 的对比指标，请检查执行模式与控制证据。'
          : `🎉 智能体协同推演完成！方案 B 延误降低 ${delayImp}%`,
        delayImp === null ? 'info' : 'success'
      );
    } else {
      const err = await rolloutRes.json();
      setCotStatus('STATUS: FAILED', 'failed');
      showToast(`推演错误: ${err.detail || '未知异常'}`, 'error');
    }
  } catch (err) {
    setCotStatus('STATUS: FAILED', 'failed');
    showToast(`网络请求异常: ${err.message}`, 'error');
  } finally {
    hideTaskVeil();
    if (btn) {
      btn.classList.remove('loading');
      btn.disabled = false;
    }
  }
}

// Markdown to Semantic HTML Parser
function renderMarkdownToHtml(markdown) {
  if (!markdown) return '';

  const lines = markdown.split('\n');
  let html = '';
  let inTable = false;
  let tableRows = [];

  const flushTable = () => {
    if (tableRows.length === 0) return '';
    let tHtml = '<div class="table-responsive"><table class="report-table"><thead><tr>';
    const headerCols = tableRows[0];
    headerCols.forEach(c => {
      tHtml += `<th>${c}</th>`;
    });
    tHtml += '</tr></thead><tbody>';
    for (let r = 1; r < tableRows.length; r++) {
      tHtml += '<tr>';
      tableRows[r].forEach(c => {
        tHtml += `<td>${c}</td>`;
      });
      tHtml += '</tr>';
    }
    tHtml += '</tbody></table></div>';
    tableRows = [];
    inTable = false;
    return tHtml;
  };

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i];
    const trimmed = rawLine.trim();

    // Check table row
    if (trimmed.startsWith('|') && trimmed.endsWith('|')) {
      const cols = trimmed.slice(1, -1).split('|').map(c => c.trim());
      // Skip markdown divider row: | :--- | :--- |
      if (cols.every(c => /^:?-+:?$/.test(c))) {
        continue;
      }
      tableRows.push(cols.map(c => parseInlineMarkdown(c)));
      inTable = true;
      continue;
    } else if (inTable) {
      html += flushTable();
    }

    if (trimmed === '') continue;

    if (trimmed === '---') {
      html += '<hr class="report-divider"/>';
      continue;
    }

    if (trimmed.startsWith('# ')) {
      html += `<h1>${parseInlineMarkdown(trimmed.slice(2))}</h1>`;
      continue;
    }
    if (trimmed.startsWith('## ')) {
      html += `<h2>${parseInlineMarkdown(trimmed.slice(3))}</h2>`;
      continue;
    }
    if (trimmed.startsWith('### ')) {
      html += `<h3>${parseInlineMarkdown(trimmed.slice(4))}</h3>`;
      continue;
    }

    if (trimmed.startsWith('> ')) {
      html += `<blockquote class="report-quote">${parseInlineMarkdown(trimmed.slice(2))}</blockquote>`;
      continue;
    }

    if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      html += `<ul><li>${parseInlineMarkdown(trimmed.slice(2))}</li></ul>`;
      continue;
    }

    const numMatch = trimmed.match(/^(\d+)\.\s+(.*)$/);
    if (numMatch) {
      html += `<ol start="${numMatch[1]}"><li>${parseInlineMarkdown(numMatch[2])}</li></ol>`;
      continue;
    }

    html += `<p>${parseInlineMarkdown(trimmed)}</p>`;
  }

  if (inTable) {
    html += flushTable();
  }

  return html;
}

function parseInlineMarkdown(text) {
  let s = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  s = s.replace(/`([^`]+)`/g, '<code class="report-code">$1</code>');
  return s;
}

// Decision Report Management
async function fetchDecisionReport() {
  try {
    const res = await fetch('/api/report/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        diagnosis: state.diagnosis,
        strategies: state.strategies,
        rollout_data: state.rollout
      })
    });

    if (res.ok) {
      const data = await res.json();
      state.reportMarkdown = data.report_markdown;
      const reportBox = document.getElementById('reportMarkdownContent');
      if (reportBox) {
        reportBox.innerHTML = renderMarkdownToHtml(state.reportMarkdown);
      }
    }
  } catch (err) {
    console.error('Failed to fetch report:', err);
  }
}

function copyReportToClipboard() {
  if (!state.reportMarkdown) {
    showToast('暂无决策简报内容可复制', 'error');
    return;
  }
  navigator.clipboard.writeText(state.reportMarkdown).then(() => {
    showToast('✅ 决策支持简报已成功复制到剪贴板！', 'success');
  }).catch(() => {
    showToast('复制失败，请手动选取复制', 'error');
  });
}

function downloadReportMarkdown() {
  if (!state.reportMarkdown) {
    showToast('暂无决策简报可供下载', 'error');
    return;
  }
  const blob = new Blob([state.reportMarkdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'TrafficAgent_Decision_Briefing.md';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast('✅ 决策支持简报已成功下载！', 'success');
}

// Toast Notifications
function showToast(message, type = 'success') {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast ${type === 'error' ? 'error' : ''}`;
  toast.innerHTML = `<span>${type === 'error' ? '⚠️' : '🚦'}</span> <span>${message}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// ==========================================================================
// Section 6: Real Road-Network Digital Twin Map
// ==========================================================================

let mapViewState = 'baseline'; // 'baseline' | 'strategy'
let currentNetworkData = null;
let pinnedEdge = null;
let mapScale = { min: 0, max: 0, dmin: 0, dmax: 0 };

async function loadRoadNetwork() {
  try {
    const res = await fetch('/api/network');
    if (!res.ok) {
      console.warn('GET /api/network failed, road map will not render');
      return null;
    }
    const data = await res.json();
    currentNetworkData = data;
    renderRoadNetwork();
    return data;
  } catch (err) {
    console.warn('Network error loading road network:', err);
    return null;
  }
}

function renderRoadNetwork() {
  const svg = document.getElementById('roadNetworkSvg');
  if (!svg || !currentNetworkData) return;

  const data = currentNetworkData;
  const { bounds, edges, nodes, bottleneck_edge, bottleneck_name, live } = data;

  // Use map_snapshot for strategy view if available
  let renderEdges = edges;
  let renderLive = live;
  if (mapViewState === 'strategy' && data.map_snapshot) {
    renderEdges = data.map_snapshot.edges || edges;
    renderLive = data.map_snapshot.live || live;
  }

  // Compute bounds for SVG viewBox
  const mnX = bounds?.min_x ?? 0, mxX = bounds?.max_x ?? 1;
  const mnY = bounds?.min_y ?? 0, mxY = bounds?.max_y ?? 1;
  const pad = 40;
  const svgW = svg.clientWidth || 800;
  const svgH = svg.clientHeight || 400;

  const xRange = mxX - mnX || 1;
  const yRange = mxY - mnY || 1;
  const scaleX = (svgW - pad * 2) / xRange;
  const scaleY = (svgH - pad * 2) / yRange;
  const scale = Math.min(scaleX, scaleY);

  const offX = (svgW - xRange * scale) / 2;
  const offY = (svgH - yRange * scale) / 2;

  const toSvgX = (x) => (x - mnX) * scale + offX;
  const toSvgY = (y) => svgH - ((y - mnY) * scale + offY); // flip Y

  // Level colors
  const levelColor = (level) => {
    if (!level || level === 'unknown') return '#64748b';
    const map = { free: '#22c55e', moderate: '#eab308', congested: '#f97316', severe: '#dc2626' };
    return map[level] || '#64748b';
  };

  // Line width by highway class
  const lineWidth = (highway) => {
    if (!highway) return 2;
    const cls = String(highway).toLowerCase();
    if (cls.includes('motorway') || cls.includes('trunk')) return 5;
    if (cls.includes('primary')) return 4;
    if (cls.includes('secondary')) return 3;
    return 2;
  };

  // Build SVG content
  let svgContent = '';

  // Defs for pulse filter
  svgContent += `<defs>
    <filter id="bottleneckGlow">
      <feGaussianBlur stdDeviation="3" result="blur"/>
      <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>`;

  // Render nodes
  if (nodes) {
    nodes.forEach(n => {
      const sx = toSvgX(n.x);
      const sy = toSvgY(n.y);
      const r = n.kind === 'junction' ? 5 : 3;
      const fill = n.kind === 'junction' ? '#94a3b8' : '#64748b';
      svgContent += `<circle class="road-node" cx="${sx}" cy="${sy}" r="${r}" fill="${fill}" opacity="0.7"/>
`;
    });
  }

  // Render edges
  if (renderEdges) {
    renderEdges.forEach(e => {
      const geom = e.geometry;
      if (!geom || geom.length < 2) return;

      const coords = geom.map(p => `${toSvgX(p[0])},${toSvgY(p[1])}`).join(' ');
      const isBottleneck = e.id === bottleneck_edge;
      const level = renderLive?.[e.id]?.level || e.level;
      const color = levelColor(level);
      const width = lineWidth(e.highway);

      let cls = 'road-edge';
      let style = '';
      if (isBottleneck) {
        cls += ' bottleneck-edge';
        style = 'filter:url(#bottleneckGlow);';
      }

      const d = `M ${coords}`;
      svgContent += `<polyline class="${cls}" d="${d}" stroke="${color}" stroke-width="${width}" style="${style}" stroke-opacity="${isBottleneck ? 1 : 0.85}" data-edge-id="${e.id}" data-edge-name="${e.name || ''}" data-edge-speed="${renderLive?.[e.id]?.speed_kmh ?? e.speed_kmh ?? ''}" data-edge-queue="${renderLive?.[e.id]?.queue_m ?? ''}" data-edge-occupancy="${renderLive?.[e.id]?.occupancy ?? ''}" data-edge-flow="${renderLive?.[e.id]?.flow_vph ?? ''}" data-edge-level="${level}"/>
`;

      // Bottleneck label
      if (isBottleneck) {
        const midIdx = Math.floor(geom.length / 2);
        const lx = toSvgX(geom[midIdx][0]);
        const ly = toSvgY(geom[midIdx][1]);
        svgContent += `<text x="${lx}" y="${ly - 10}" fill="#dc2626" font-size="12" font-weight="700" text-anchor="middle">瓶颈: <tspan fill="#fff">${bottleneck_name || ''}</tspan></text>`;
      }
    });
  }

  svg.innerHTML = svgContent;

  // Attach event listeners
  attachMapEdgeEvents(svg, renderLive, renderEdges, bottleneck_name);
}

function attachMapEdgeEvents(svg, liveData, edgeData, bottleneckName) {
  const tooltip = document.getElementById('mapTooltip');
  if (!tooltip) return;

  const edges = svg.querySelectorAll('.road-edge');
  edges.forEach(el => {
    const edgeId = el.getAttribute('data-edge-id');
    const edgeName = el.getAttribute('data-edge-name') || '未命名路段';
    const speed = el.getAttribute('data-edge-speed');
    const queue = el.getAttribute('data-edge-queue');
    const occupancy = el.getAttribute('data-edge-occupancy');
    const flow = el.getAttribute('data-edge-flow');
    const level = el.getAttribute('data-edge-level') || 'unknown';

    el.addEventListener('mouseenter', (e) => {
      const levelLabel = { free: '畅通', moderate: '轻度拥堵', congested: '中度拥堵', severe: '严重拥堵', unknown: '未知' };
      tooltip.innerHTML = `
        <div class="tt-name">${edgeName}</div>
        <div class="tt-row"><span class="tt-label">拥堵等级</span><span class="tt-val">${levelLabel[level] || level}</span></div>
        ${speed ? `<div class="tt-row"><span class="tt-label">车速</span><span class="tt-val">${speed} km/h</span></div>` : ''}
        ${queue ? `<div class="tt-row"><span class="tt-label">排队</span><span class="tt-val">${queue} m</span></div>` : ''}
        ${occupancy ? `<div class="tt-row"><span class="tt-label">占有率</span><span class="tt-val">${Math.round(occupancy * 100)}%</span></div>` : ''}
        ${flow ? `<div class="tt-row"><span class="tt-label">流量</span><span class="tt-val">${flow} veh/h</span></div>` : ''}
      `;
      tooltip.classList.add('visible');
    });

    el.addEventListener('mousemove', (e) => {
      const rect = svg.parentElement.getBoundingClientRect();
      let tx = e.clientX - rect.left + 16;
      let ty = e.clientY - rect.top - 10;
      // Keep tooltip in bounds
      const tw = tooltip.offsetWidth;
      if (tx + tw > rect.width) tx = tx - tw - 32;
      tooltip.style.left = tx + 'px';
      tooltip.style.top = ty + 'px';
    });

    el.addEventListener('mouseleave', () => {
      tooltip.classList.remove('visible');
    });

    el.addEventListener('click', () => {
      pinnedEdge = edgeId;
      const sb = document.getElementById('mapSidebarContent');
      if (sb) {
        const levelLabel = { free: '畅通', moderate: '轻度拥堵', congested: '中度拥堵', severe: '严重拥堵', unknown: '未知' };
        sb.innerHTML = `
          <div style="margin-bottom:10px;"><span class="det-key">路段:</span> <span class="det-val">${edgeName}</span></div>
          <div style="margin-bottom:6px;"><span class="det-key">拥堵等级:</span> <span class="det-val">${levelLabel[level] || level}</span></div>
          ${speed ? `<div style="margin-bottom:6px;"><span class="det-key">车速:</span> <span class="det-val">${speed} km/h</span></div>` : ''}
          ${queue ? `<div style="margin-bottom:6px;"><span class="det-key">排队长度:</span> <span class="det-val">${queue} m</span></div>` : ''}
          ${occupancy ? `<div style="margin-bottom:6px;"><span class="det-key">占有率:</span> <span class="det-val">${Math.round(occupancy * 100)}%</span></div>` : ''}
          ${flow ? `<div style="margin-bottom:6px;"><span class="det-key">流量:</span> <span class="det-val">${flow} veh/h</span></div>` : ''}
        `;
      }
    });
  });
}

// Map view toggle
function initMapToggle() {
  const btn = document.getElementById('mapToggleBtn');
  const label = document.getElementById('mapToggleLabel');
  if (!btn || !label) return;

  btn.addEventListener('click', () => {
    if (mapViewState === 'baseline') {
      mapViewState = 'strategy';
      label.textContent = '策略视图';
      btn.classList.add('map-toggle-active');
    } else {
      mapViewState = 'baseline';
      label.textContent = '基线视图';
      btn.classList.remove('map-toggle-active');
    }
    renderRoadNetwork();
  });
}

// ==========================================================================
// Section 7: Action Checklist
// ==========================================================================

async function loadActionPlan() {
  if (!state.diagnosis && !state.strategies && !state.rollout) return;

  try {
    const res = await fetch('/api/action-plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        diagnosis: state.diagnosis,
        strategies: state.strategies,
        rollout: state.rollout
      })
    });

    if (!res.ok) {
      console.warn('POST /api/action-plan failed');
      return;
    }

    const data = await res.json();
    renderActionChecklist(data);
  } catch (err) {
    console.warn('Error loading action plan:', err);
  }
}

function renderActionChecklist(data) {
  const summaryEl = document.getElementById('actionSummary');
  const stepsEl = document.getElementById('actionSteps');
  if (!summaryEl || !stepsEl) return;

  // Render plain summary
  if (data.plain_summary) {
    summaryEl.innerHTML = `<strong>📌 行动概要：</strong>${data.plain_summary}`;
    summaryEl.style.display = 'block';
  } else {
    summaryEl.style.display = 'none';
  }

  // Render steps
  const steps = data.steps || [];
  if (steps.length === 0) {
    stepsEl.innerHTML = '<div class="detector-placeholder">暂无行动指令</div>';
    return;
  }

  stepsEl.innerHTML = steps.map(s => {
    const phase = s.phase ?? 0;
    return `
      <div class="action-step-card phase-${phase}">
        <div class="action-step-header">
          <span class="action-step-num">${s.n ?? (steps.indexOf(s) + 1)}</span>
          <span class="action-step-title">${s.title || '未命名行动'}</span>
        </div>
        <div class="action-step-detail">
          ${s.action ? `<div class="action-detail-item"><span class="action-detail-label">行动内容</span><span class="action-detail-value">${s.action}</span></div>` : ''}
          ${s.where ? `<div class="action-detail-item"><span class="action-detail-label">📍 位置</span><span class="action-detail-value">${s.where}</span></div>` : ''}
          ${s.when ? `<div class="action-detail-item"><span class="action-detail-label">⏰ 时机</span><span class="action-detail-value">${s.when}</span></div>` : ''}
          ${s.expected ? `<div class="action-detail-item"><span class="action-detail-label">🎯 预期效果</span><span class="action-detail-value">${s.expected}</span></div>` : ''}
          ${s.owner ? `<div class="action-detail-item"><span class="action-detail-label">👤 责任</span><span class="action-detail-value">${s.owner}</span></div>` : ''}
          ${s.verified !== undefined ? `<div class="action-detail-item"><span class="action-detail-label">✅ 验证</span><span class="action-detail-value">${s.verified ? '已验证' : '待验证'}</span></div>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

// ==========================================================================
// Section 8: Per-Link Detector Table
// ==========================================================================

function renderDetectorTable(detectors, engine) {
  const tbody = document.getElementById('detectorTableBody');
  const engineLabel = document.getElementById('engineLabel');
  if (!tbody) return;

  // Engine label
  if (engineLabel && engine) {
    engineLabel.textContent = `引擎: ${engine}`;
    engineLabel.style.display = 'inline-block';
  } else if (engineLabel) {
    engineLabel.style.display = 'none';
  }

  // Sort by severity (severe > congested > moderate > free > unknown)
  if (!detectors || detectors.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="detector-placeholder">运行推演后显示检测器数据</td></tr>';
    return;
  }

  // Slice top 15
  const rows = detectors.slice(0, 15);

  tbody.innerHTML = rows.map(d => {
    const level = d.level || 'unknown';
    const levelLabel = { free: '畅通', moderate: '轻度拥堵', congested: '中度拥堵', severe: '严重拥堵', unknown: '未知' };
    return `
      <tr>
        <td><span class="detector-level-chip level-${level}"><span class="level-dot"></span>${levelLabel[level] || level}</span></td>
        <td>${d.edge_id || '-'}</td>
        <td>${d.name || '-'}</td>
        <td>${d.highway || '-'}</td>
        <td>${d.peak_queue_m != null ? d.peak_queue_m : '-'}</td>
        <td>${d.avg_speed_kmh != null ? d.avg_speed_kmh : '-'}</td>
        <td>${d.min_speed_kmh != null ? d.min_speed_kmh : '-'}</td>
        <td>${d.peak_flow_vph != null ? d.peak_flow_vph : '-'}</td>
        <td>${d.peak_occupancy != null ? Math.round(d.peak_occupancy * 100) + '%' : '-'}</td>
        <td>${d.avg_delay_s != null ? d.avg_delay_s : '-'}</td>
      </tr>
    `;
  }).join('');

  // Attach sort handlers
  attachDetectorSortHandlers();
}

// Detector-table sorting (F7). Two defects lived here:
//   1. attachDetectorSortHandlers() was called from inside renderDetectorTable(), so every
//      rollout added one more click listener per header — after N runs a single click ran the
//      sort N times (toggle direction visibly flickered). Binding is now done ONCE, and the
//      table body itself is replaced on each render, so delegation survives re-renders.
//   2. The guard `!rows[0].querySelector('.detector-placeholder') === false` was inverted by
//      operator precedence: it evaluated as `(!x) === false`, entering the sort branch for the
//      placeholder row and skipping it for real rows — i.e. sorting never worked.
function attachDetectorSortHandlers() {
  const table = document.getElementById('detectorTable');
  if (!table || table.dataset.sortBound === '1') return;
  table.dataset.sortBound = '1';

  const thead = table.querySelector('thead');
  if (!thead) return;

  thead.addEventListener('click', (ev) => {
    const th = ev.target.closest('th[data-sort]');
    if (!th) return;
    const key = th.getAttribute('data-sort');
    const tbody = document.getElementById('detectorTableBody');
    if (!tbody) return;

    const rows = Array.from(tbody.querySelectorAll('tr'));
    // Nothing to sort: empty body, or the single placeholder row.
    if (rows.length === 0 || rows[0].querySelector('.detector-placeholder')) {
      return;
    }

    const isAsc = !th.classList.contains('sorted-asc');
    Array.from(thead.querySelectorAll('th[data-sort]')).forEach(h => {
      h.classList.remove('sorted-asc', 'sorted-desc');
      h.removeAttribute('aria-sort');
    });
    th.classList.add(isAsc ? 'sorted-asc' : 'sorted-desc');
    th.setAttribute('aria-sort', isAsc ? 'ascending' : 'descending');

    const colIdx = Array.from(th.parentNode.children).indexOf(th);

    rows.sort((a, b) => {
      const aVal = a.children[colIdx]?.textContent?.trim?.() ?? '';
      const bVal = b.children[colIdx]?.textContent?.trim?.() ?? '';
      const aNum = parseFloat(aVal);
      const bNum = parseFloat(bVal);
      if (!isNaN(aNum) && !isNaN(bNum)) {
        return isAsc ? aNum - bNum : bNum - aNum;
      }
      return isAsc
        ? String(aVal).localeCompare(String(bVal), 'zh')
        : String(bVal).localeCompare(String(aVal), 'zh');
    });
    rows.forEach(r => tbody.appendChild(r));
  });
}


// ==========================================================================
// LLM Switcher (ccSwitch Style Model Detection & Hot-Swap)
// ==========================================================================

async function fetchLlmStatus() {
  try {
    const res = await fetch('/api/llm/config');
    if (res.ok) {
      const data = await res.json();
      const llm = data.llm || {};
      const brainText = document.getElementById('agentBrainStatusText');
      if (brainText) {
        if (llm.configured) {
          brainText.textContent = `智能体大脑：在线 (${llm.model || 'LLM'})`;
        } else {
          brainText.textContent = '智能体大脑：在线 (规则降级模板)';
        }
      }
    }
  } catch (err) {
    console.debug('Failed to fetch LLM status:', err);
  }
}

function initLlmModal() {
  const openBtn = document.getElementById('openLlmModalBtn');
  const modal = document.getElementById('llmConfigModal');
  const closeBtn = document.getElementById('closeLlmModalBtn');
  const cancelBtn = document.getElementById('cancelLlmModalBtn');
  const detectBtn = document.getElementById('detectModelsBtn');
  const detectSpinner = document.getElementById('detectSpinner');
  const detectBtnText = document.getElementById('detectBtnText');
  const saveBtn = document.getElementById('saveLlmConfigBtn');
  const baseUrlInput = document.getElementById('llmBaseUrlInput');
  const apiKeyInput = document.getElementById('llmApiKeyInput');
  const eyeBtn = document.getElementById('toggleApiKeyVisibilityBtn');
  const modelSelect = document.getElementById('llmModelSelect');
  const customModelInput = document.getElementById('llmCustomModelInput');
  const statusAlert = document.getElementById('llmStatusAlert');
  const providerTags = document.querySelectorAll('.provider-tag');

  if (!openBtn || !modal) return;

  function showStatus(msg, type = 'info') {
    if (!statusAlert) return;
    statusAlert.textContent = msg;
    statusAlert.className = `llm-status-alert ${type}`;
    statusAlert.style.display = 'block';
  }

  function hideStatus() {
    if (statusAlert) statusAlert.style.display = 'none';
  }

  // Open modal & prefill current values
  let lastFocused = null;

  openBtn.addEventListener('click', async () => {
    lastFocused = document.activeElement;
    modal.style.display = 'flex';
    hideStatus();
    // Move focus into the dialog so keyboard/screen-reader users land inside it (F14).
    const firstField = baseUrlInput || closeBtn;
    if (firstField) setTimeout(() => firstField.focus(), 30);
    try {
      const res = await fetch('/api/llm/config');
      if (res.ok) {
        const data = await res.json();
        const llm = data.llm || {};
        if (baseUrlInput && !baseUrlInput.value) {
          baseUrlInput.value = llm.base_url || 'https://api.deepseek.com/v1';
        }
        if (customModelInput && !customModelInput.value && llm.model) {
          customModelInput.value = llm.model;
        }
        if (llm.masked_key && apiKeyInput && !apiKeyInput.value) {
          apiKeyInput.placeholder = `已配置: ${llm.masked_key}`;
        }
      }
    } catch (e) {
      console.warn('Failed to prefill LLM config:', e);
    }
  });

  // Close modal
  function closeModal() {
    modal.style.display = 'none';
    hideStatus();
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  }
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  // Esc to close + focus trap while the dialog is open (F14). Without the trap, Tab walked
  // straight out of the dialog into the dimmed page behind it.
  modal.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeModal();
      return;
    }
    if (e.key !== 'Tab') return;
    const focusables = Array.from(
      modal.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter(el => el.offsetParent !== null);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  // Quick provider tags click
  providerTags.forEach(tag => {
    tag.addEventListener('click', () => {
      const url = tag.getAttribute('data-url');
      if (url && baseUrlInput) {
        baseUrlInput.value = url;
        showToast(`已填入 ${tag.textContent} 端点地址`, 'info');
      }
    });
  });

  // Eye toggle for password visibility
  if (eyeBtn && apiKeyInput) {
    eyeBtn.addEventListener('click', () => {
      const isPwd = apiKeyInput.type === 'password';
      apiKeyInput.type = isPwd ? 'text' : 'password';
      eyeBtn.textContent = isPwd ? '🔒' : '👁️';
    });
  }

  // Auto-detect models (like ccSwitch)
  if (detectBtn) {
    detectBtn.addEventListener('click', async () => {
      const base_url = baseUrlInput ? baseUrlInput.value.trim() : '';
      const api_key = apiKeyInput ? apiKeyInput.value.trim() : '';

      detectBtn.disabled = true;
      if (detectSpinner) detectSpinner.style.display = 'inline-block';
      if (detectBtnText) detectBtnText.textContent = '正在探测服务商可用模型...';
      hideStatus();

      try {
        const res = await fetch('/api/llm/detect-models', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ base_url, api_key })
        });

        const data = await res.json();
        if (data.success && data.models && data.models.length > 0) {
          if (modelSelect) {
            modelSelect.innerHTML = '';
            data.models.forEach(m => {
              const opt = document.createElement('option');
              opt.value = m;
              opt.textContent = m;
              if (m === data.current_model || (customModelInput && m === customModelInput.value)) {
                opt.selected = true;
              }
              modelSelect.appendChild(opt);
            });
            if (customModelInput) {
              customModelInput.value = modelSelect.value;
            }
          }
          showStatus(`✅ 成功探测到 ${data.count} 个可用模型！已自动加载至下拉选单。`, 'success');
          showToast(`成功探测到 ${data.count} 个模型`, 'success');
        } else {
          showStatus(`❌ 模型自动识别失败：${data.error || '未返回可用模型列表，请核对 Base URL 与 Key'}`, 'error');
          showToast(data.error || '未能探测到模型', 'error');
        }
      } catch (err) {
        showStatus(`❌ 网络请求异常：${err.message}`, 'error');
        showToast('请求探测失败', 'error');
      } finally {
        detectBtn.disabled = false;
        if (detectSpinner) detectSpinner.style.display = 'none';
        if (detectBtnText) detectBtnText.textContent = '🔍 自动识别可用模型 (Auto-detect Models)';
      }
    });
  }

  // Model select change -> update customModelInput
  if (modelSelect && customModelInput) {
    modelSelect.addEventListener('change', () => {
      if (modelSelect.value) {
        customModelInput.value = modelSelect.value;
      }
    });
  }

  // Save config & hot reload
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      const base_url = baseUrlInput ? baseUrlInput.value.trim() : '';
      const api_key = apiKeyInput ? apiKeyInput.value.trim() : '';
      const model = customModelInput ? customModelInput.value.trim() : (modelSelect ? modelSelect.value.trim() : '');

      saveBtn.disabled = true;
      saveBtn.textContent = '保存中...';

      try {
        const res = await fetch('/api/llm/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ base_url, api_key, model })
        });

        if (res.ok) {
          const data = await res.json();
          showStatus(`✅ 配置已热更新并立即生效！当前模型：${data.llm.model}`, 'success');
          showToast(`大模型已切换为：${data.llm.model}`, 'success');

          const brainText = document.getElementById('agentBrainStatusText');
          if (brainText) {
            brainText.textContent = `智能体大脑：在线 (${data.llm.model})`;
          }

          setTimeout(() => {
            closeModal();
          }, 600);
        } else {
          showStatus('保存配置失败，请检查输入参数', 'error');
          showToast('保存配置失败', 'error');
        }
      } catch (err) {
        showStatus(`保存异常：${err.message}`, 'error');
      } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = '💾 保存并立即生效';
      }
    });
  }
}

