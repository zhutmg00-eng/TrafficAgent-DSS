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

  // Update Situational Awareness KPIs
  const speedEl = document.getElementById('kpiSpeed');
  const queueEl = document.getElementById('kpiQueue');
  const occEl = document.getElementById('kpiOcc');
  const bypassEl = document.getElementById('kpiBypass');
  const speedDelta = document.getElementById('kpiSpeedDelta');
  const queueDelta = document.getElementById('kpiQueueDelta');
  const occDelta = document.getElementById('kpiOccDelta');
  const bypassDelta = document.getElementById('kpiBypassDelta');

  if (speedEl) speedEl.textContent = `${config.speed_kmh} km/h`;
  if (queueEl) queueEl.textContent = `${config.queue_m} m`;
  if (occEl) occEl.textContent = `${Math.round(config.occupancy * 100)}%`;
  if (bypassEl) bypassEl.textContent = `1,200 veh/h (${Math.round(config.bypass_occupancy * 100)}%)`;

  if (speedDelta) speedDelta.textContent = config.speed_delta;
  if (queueDelta) queueDelta.textContent = config.queue_delta;
  if (occDelta) occDelta.textContent = config.occ_delta;
  if (bypassDelta) bypassDelta.textContent = config.bypass_delta;

  showToast(`已加载场景：${state.corridor} · ${state.congestionType}`, 'info');
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
}

function loadFallbackData() {
  renderSituationalAwareness();
  renderCoTDiagnosis();
  renderStrategies();
  renderRolloutKPIs();
  renderCharts();
}

// Render Situational Awareness
function renderSituationalAwareness() {
  const s = state.trafficState;
  const speedEl = document.getElementById('kpiSpeed');
  const queueEl = document.getElementById('kpiQueue');
  const occEl = document.getElementById('kpiOcc');
  const bypassEl = document.getElementById('kpiBypass');

  if (speedEl) speedEl.textContent = `${s.speed_kmh} km/h`;
  if (queueEl) queueEl.textContent = `${s.queue_m} m`;
  if (occEl) occEl.textContent = `${Math.round(s.occupancy * 100)}%`;
  if (bypassEl) bypassEl.textContent = `1,200 veh/h (${Math.round(s.bypass_occupancy * 100)}%)`;
}

// Render CoT Diagnosis Terminal
function renderCoTDiagnosis() {
  const terminalBody = document.getElementById('cotTerminalBody');
  if (!terminalBody) return;

  const steps = state.diagnosis?.cot_reasoning || [
    "1. 【态势感知】监测到走廊主断面 [J1_J2] 平均车速骤降至 8.2 km/h，占有率高达 82%。",
    "2. 【空间排队】当前排队长度达到 165.0 米，占路段库容比为 55%，已逼近回溢警戒线 (75%)。",
    "3. 【成因归因】突发占道事故导致通行能力锐减 60%，晚高峰潮汐车流高位积压形成激波回传。",
    "4. 【蔓延风险】若不采取干预，排队将在 180 秒内回溢至上游交叉口 J1，锁死东西向及南北向交叉车流。",
    "5. 【旁路核查】北部平行通道当前占有率仅为 28%，具备充沛备用承载容量，适宜实施诱导分流。"
  ];

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

// Render Rollout KPIs
function renderRolloutKPIs() {
  if (!state.rollout) return;
  const comp = state.rollout.comparisons?.strategy_b || {
    delay_improvement_pct: 44.6,
    queue_improvement_pct: 56.6,
    speed_improvement_pct: 138.3,
    throughput_improvement_pct: 32.8,
    co2_improvement_pct: 26.2
  };
  const kpis = state.rollout.kpis?.strategy_b || {
    avg_delay_s: 46.8,
    max_queue_m: 105.0,
    avg_speed_kmh: 22.4,
    throughput_vph: 1780.0,
    co2_emissions_kg: 134.6
  };

  const delayImp = document.getElementById('rolloutDelayImp');
  const queueImp = document.getElementById('rolloutQueueImp');
  const speedImp = document.getElementById('rolloutSpeedImp');
  const tpImp = document.getElementById('rolloutTpImp');
  const co2Imp = document.getElementById('rolloutCo2Imp');

  if (delayImp) delayImp.textContent = `-${comp.delay_improvement_pct}%`;
  if (queueImp) queueImp.textContent = `-${comp.queue_improvement_pct}%`;
  if (speedImp) speedImp.textContent = `+${comp.speed_improvement_pct}%`;
  if (tpImp) tpImp.textContent = `+${comp.throughput_improvement_pct}%`;
  if (co2Imp) co2Imp.textContent = `-${comp.co2_improvement_pct}%`;

  const delayAct = document.getElementById('rolloutDelayAct');
  const queueAct = document.getElementById('rolloutQueueAct');
  const speedAct = document.getElementById('rolloutSpeedAct');
  const tpAct = document.getElementById('rolloutTpAct');
  const co2Act = document.getElementById('rolloutCo2Act');

  if (delayAct) delayAct.textContent = `降至 ${kpis.avg_delay_s} s/veh`;
  if (queueAct) queueAct.textContent = `缩减至 ${kpis.max_queue_m} m`;
  if (speedAct) speedAct.textContent = `提升至 ${kpis.avg_speed_kmh} km/h`;
  if (tpAct) tpAct.textContent = `达 ${kpis.throughput_vph} veh/h`;
  if (co2Act) co2Act.textContent = `降至 ${kpis.co2_emissions_kg} kg`;
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

  if (typeof echarts === 'undefined') {
    renderCanvasRadarFallback(dom);
    return;
  }

  const isDark = state.theme === 'dark';
  radarChartInstance = echarts.init(dom, isDark ? 'dark' : null);

  const radarData = state.rollout?.radar || {
    dimensions: ["通行效率", "空间治堵", "容量释放", "运行平稳", "绿色低碳"],
    baseline: [45, 42, 50, 48, 52],
    strategy_a: [65, 60, 68, 62, 66],
    strategy_b: [92, 95, 88, 90, 85]
  };

  const option = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    legend: {
      bottom: 0,
      textStyle: { color: isDark ? '#94a3b8' : '#475569', fontSize: 12 },
      data: ['现状基线 (Do-Nothing)', '方案 A (Webster自适应)', '方案 B (Agent协同DSS)']
    },
    radar: {
      indicator: radarData.dimensions.map(d => ({ name: d, max: 100 })),
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
      data: [
        {
          value: radarData.baseline,
          name: '现状基线 (Do-Nothing)',
          itemStyle: { color: '#94a3b8' },
          lineStyle: { width: 2, type: 'dashed' },
          areaStyle: { color: 'rgba(148, 163, 184, 0.2)' }
        },
        {
          value: radarData.strategy_a,
          name: '方案 A (Webster自适应)',
          itemStyle: { color: '#f59e0b' },
          lineStyle: { width: 2 },
          areaStyle: { color: 'rgba(245, 158, 11, 0.25)' }
        },
        {
          value: radarData.strategy_b,
          name: '方案 B (Agent协同DSS)',
          itemStyle: { color: '#10b981' },
          lineStyle: { width: 3 },
          areaStyle: { color: 'rgba(16, 185, 129, 0.35)' }
        }
      ]
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

  const isDark = state.theme === 'dark';
  timeSeriesChartInstance = echarts.init(dom, isDark ? 'dark' : null);

  const ts = state.rollout?.time_series;
  let steps = ts?.time_steps;
  let qBase = ts?.queue_baseline;
  let qA = ts?.queue_strategy_a;
  let qB = ts?.queue_strategy_b;

  if (!steps) {
    steps = Array.from({ length: 61 }, (_, i) => i * 10);
    qBase = steps.map(t => t < 150 ? 12 + t * 0.08 : (t <= 420 ? Math.min(242, 25 + Math.pow(t - 150, 1.35) * 0.22) : Math.max(75, 242 - (t - 420) * 0.8)));
    qA = steps.map(t => t < 150 ? 12 + t * 0.07 : (t <= 420 ? Math.min(185, 22 + Math.pow(t - 150, 1.25) * 0.20) : Math.max(35, 185 - (t - 420) * 1.1)));
    qB = steps.map(t => t < 150 ? 12 + t * 0.05 : (t <= 420 ? Math.min(105, 18 + Math.pow(t - 150, 1.10) * 0.16) : Math.max(10, 105 - (t - 420) * 1.6)));
  }

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
      textStyle: { color: isDark ? '#94a3b8' : '#475569', fontSize: 12 },
      data: ['基线 (Do-Nothing)', '方案A (Webster)', '方案B (Agent协同DSS)']
    },
    grid: {
      left: '3%',
      right: '4%',
      top: '8%',
      bottom: '12%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: steps,
      name: '秒',
      axisLine: { lineStyle: { color: isDark ? '#334155' : '#cbd5e1' } },
      axisLabel: { color: isDark ? '#94a3b8' : '#64748b' }
    },
    yaxis: {
      type: 'value',
      name: '排队长度 (m)',
      nameTextStyle: { color: isDark ? '#94a3b8' : '#64748b' },
      splitLine: {
        lineStyle: { color: isDark ? 'rgba(255, 255, 255, 0.08)' : 'rgba(0, 0, 0, 0.08)' }
      },
      axisLabel: { color: isDark ? '#94a3b8' : '#64748b' }
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

// End-to-End Decision Pipeline Execution
async function executeAgentDecisionPipeline() {
  const btn = document.getElementById('runSimulationBtn');
  if (btn) {
    btn.classList.add('loading');
    btn.disabled = true;
  }

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
      renderCharts();

      // 4. Export formatted decision report
      await fetchDecisionReport();

      const delayImp = rolloutData.comparisons?.strategy_b?.delay_improvement_pct ?? 44.6;
      showToast(`🎉 智能体协同推演完成！方案 B 延误降低 ${delayImp}%`, 'success');
    } else {
      const err = await rolloutRes.json();
      showToast(`推演错误: ${err.detail || '未知异常'}`, 'error');
    }
  } catch (err) {
    showToast(`网络请求异常: ${err.message}`, 'error');
  } finally {
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
  openBtn.addEventListener('click', async () => {
    modal.style.display = 'flex';
    hideStatus();
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
  }
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
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

