/**
 * TrafficAgent-DSS × 百度地图 LBS 接入层
 *
 * 职责（对应百度地图开发者创作大赛「须以百度地图 API/SDK 作为核心功能」）：
 *  1. 真实路网数字孪生底座：百度地图 JS API GL + 实时路况图层 (TrafficLayer)；
 *  2. 真实绕行对比：经后端代理 /api/baidu/route 调用百度驾车路径规划 REST API，
 *     展示真实距离/耗时/拥堵比例，替换纯仿真口径之外的现实参照；
 *  3. 诚实状态：未配置 AK 时显式说明"未配置"，调用失败时如实报错——
 *     本模块绝不生成任何替代地图数据。
 */
(function () {
  'use strict';

  const state = {
    ak: null,
    center: { lng: 116.337, lat: 39.965 },
    map: null,
    trafficLayer: null,
    overlays: [],
    scriptLoading: false,
    scriptLoaded: false
  };

  // 走廊演示 OD（BD-09 坐标，北京学院南路-西直门一带），用于真实绕行对比
  const DEMO_OD = {
    origin: { lng: 116.3295, lat: 39.9612, name: '瓶颈上游 · 学院南路东段' },
    dest: { lng: 116.3521, lat: 39.9723, name: '瓶颈下游 · 西直门桥北' }
  };

  function el(id) { return document.getElementById(id); }

  function toast(msg, type) {
    if (typeof window.showToast === 'function') window.showToast(msg, type || 'info');
  }

  // Status colour goes through CSS custom properties, not literal hex. The literals here used
  // to be the *dark* steps (#f87171 / #34d399 / #fbbf24); in light mode those are pale tints
  // on a near-white header, so "LBS: 未配置" measured 2.52:1. Tokens resolve per theme.
  function setStatusBadge(text, token) {
    const badge = el('baiduMapStatusBadge');
    if (badge) {
      badge.textContent = text;
      badge.style.color = token || 'var(--text-muted)';
    }
  }

  function showUnconfigured(message) {
    const container = el('baiduMapContainer');
    if (container) {
      container.innerHTML = '<div class="map-unconfigured">' +
        '<div class="map-unconfigured-icon">🗺️</div>' +
        '<div class="map-unconfigured-title">百度地图 LBS 未接入</div>' +
        '<div class="map-unconfigured-desc">' + message + '</div>' +
        '<div class="map-unconfigured-desc">配置方法：复制 <code>.env.example</code> 为 <code>.env</code>，' +
        '在 <a href="https://lbs.baidu.com" target="_blank" rel="noopener">百度地图开放平台</a> 创建「浏览器端」应用后填入 <code>BAIDU_MAP_AK</code>。</div>' +
        '</div>';
    }
    setStatusBadge('LBS: 未配置', 'var(--state-bad-text)');
    const btn = el('baiduRouteCompareBtn');
    if (btn) btn.disabled = true;
  }

  function loadBMapGL(ak) {
    if (state.scriptLoaded) return Promise.resolve();
    if (state.scriptLoading) return state.scriptLoading;
    state.scriptLoading = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://api.map.baidu.com/api?v=1.0&type=webgl&ak=' + encodeURIComponent(ak) + '&callback=__bmapReady';
      script.async = true;
      script.onerror = () => reject(new Error('百度地图 JS API GL 脚本加载失败（检查网络或 AK）'));
      window.__bmapReady = () => { state.scriptLoaded = true; resolve(); };
      document.head.appendChild(script);
    });
    return state.scriptLoading;
  }

  function clearOverlays() {
    state.overlays.forEach(o => { try { state.map.removeOverlay(o); } catch (e) { /* noop */ } });
    state.overlays = [];
  }

  function setupMap() {
    const container = el('baiduMapContainer');
    if (!container || !window.BMapGL) return;
    state.map = new BMapGL.Map(container, { enableMapClick: false });
    state.map.centerAndZoom(new BMapGL.Point(state.center.lng, state.center.lat), 15);
    state.map.enableScrollWheelZoom(true);

    if (el('baiduTrafficToggle')?.checked) addTrafficLayer();

    // 走廊抽象模型与真实路网的对照锚点：J1-J3 三个信号交叉口
    const junctions = [
      { lng: state.center.lng - 0.0042, lat: state.center.lat, label: 'J1' },
      { lng: state.center.lng, lat: state.center.lat, label: 'J2 (瓶颈)' },
      { lng: state.center.lng + 0.0042, lat: state.center.lat, label: 'J3' }
    ];
    junctions.forEach(j => {
      const point = new BMapGL.Point(j.lng, j.lat);
      const marker = new BMapGL.Marker(point, { title: j.label });
      const label = new BMapGL.Label(j.label, {
        position: point,
        offset: new BMapGL.Size(8, -20)
      });
      label.setStyle({
        color: '#e2e8f0',
        backgroundColor: 'rgba(15,23,42,0.82)',
        border: '1px solid rgba(56,189,248,0.55)',
        borderRadius: '4px',
        fontSize: '12px',
        padding: '2px 6px'
      });
      state.map.addOverlay(marker);
      state.map.addOverlay(label);
      state.overlays.push(marker, label);
    });

    setStatusBadge('LBS: 已接入 (JS API GL · 实时路况)', 'var(--state-ok-text)');
  }

  function addTrafficLayer() {
    if (!state.map || state.trafficLayer) return;
    state.trafficLayer = new BMapGL.TrafficLayer();
    state.map.addTileLayer(state.trafficLayer);
  }

  function removeTrafficLayer() {
    if (state.map && state.trafficLayer) {
      state.map.removeTileLayer(state.trafficLayer);
      state.trafficLayer = null;
    }
  }

  function fmtNumber(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Number(v).toFixed(digits);
  }

  function renderRouteCards(routes, sourceLabel) {
    const wrap = el('baiduRouteCards');
    if (!wrap) return;
    const src = sourceLabel || '百度地图实时路径规划';
    wrap.innerHTML = routes.map((r, i) => {
      const color = i === 0 ? '#38bdf8' : '#f59e0b';
      const badge = i === 0 ? '推荐路线' : '备选 ' + i;
      const cong = r.congestion_summary;
      const congText = cong && cong.congested_ratio !== null && cong.congested_ratio !== undefined
        ? ` · 拥堵里程占比 ${(cong.congested_ratio * 100).toFixed(0)}% (${cong.label})`
        : '';
      return '<div class="route-card" style="border-left:3px solid ' + color + ';">' +
        '<div class="route-card-head"><span class="route-badge" style="color:' + color + '">' + badge + '</span>' +
        '<span class="route-km">' + fmtNumber(r.distance_km, 2) + ' km · ' +
        fmtNumber(r.duration_min, 1) + ' 分钟</span></div>' +
        '<div class="route-card-sub">' + escapeHtml(src) + escapeHtml(congText) + '</div>' +
        '</div>';
    }).join('');
  }

  // ---------------------------------------------------------------------------
  // 浏览器端路线规划兜底（BMapGL.DrivingRoute）
  //
  // 为什么需要它：后端 /api/baidu/route 走的是百度「服务端」Web 服务 API，需要单独
  // 申请一个走 IP 白名单校验的服务端 AK。该 AK 未配置或白名单未放行时（百度返回
  // status=240/210），后端会如实回 502，此前本模块只能整体报错、绕行对比不可用。
  //
  // 但浏览器端 AK 本身具备 JS API 驾车路线规划能力（通道 qt=drct），因此这里用同一把
  // 浏览器端 AK 兜底，保证「真实绕行路线对比」在答辩等关键场景下不会因服务端配额/
  // 白名单问题而整体失效。
  //
  // 红线不变：这里拿到的是百度实时返回的真实路线，不是伪造数据；卡片下方会明确标注
  // 本次结果走的是哪条通道（服务端代理 / 浏览器端 JS API）。
  // ---------------------------------------------------------------------------

  const METERS_PER_KM = 1000;

  // BMapGL 的距离/耗时是带单位的字符串（如 "6.6公里" / "23分钟"）。
  // 若个别版本返回纯数字，则按百度惯例视为「米」与「秒」。
  function parseKm(value) {
    if (value === null || value === undefined) return null;
    if (typeof value === 'number') return value / METERS_PER_KM;
    const s = String(value).trim();
    const m = s.match(/([\d.]+)\s*(公里|千米|km|米|m)/i);
    if (m) {
      const v = parseFloat(m[1]);
      const unit = m[2].toLowerCase();
      return (unit === '米' || unit === 'm') ? v / METERS_PER_KM : v;
    }
    const n = parseFloat(s);
    return isNaN(n) ? null : n / METERS_PER_KM;
  }

  function parseMinutes(value) {
    if (value === null || value === undefined) return null;
    if (typeof value === 'number') return value / 60;
    const s = String(value).trim();
    let total = 0;
    let matched = false;
    const h = s.match(/([\d.]+)\s*(小时|时|h)/i);
    if (h) { total += parseFloat(h[1]) * 60; matched = true; }
    const m = s.match(/([\d.]+)\s*(分钟|分|min)/i);
    if (m) { total += parseFloat(m[1]); matched = true; }
    if (matched) return total;
    const n = parseFloat(s);
    return isNaN(n) ? null : n / 60;
  }

  // 把一个 DrivingRoutePlan 转成本模块统一的 route 结构（与后端返回同形）
  function planToRoute(plan, index) {
    const points = [];
    try {
      const n = plan.getNumRoutes ? plan.getNumRoutes() : 0;
      for (let k = 0; k < n; k++) {
        const seg = plan.getRoute(k);
        const path = seg && seg.getPath ? seg.getPath() : null;
        if (path && path.length) points.push.apply(points, path);
      }
    } catch (e) { /* 取不到路径点只影响画线，不影响距离/耗时 */ }
    return {
      index: index,
      distance_km: parseKm(plan.getDistance ? plan.getDistance() : null),
      duration_min: parseMinutes(plan.getDuration ? plan.getDuration() : null),
      // 浏览器端 JS API 不提供后端那套拥堵里程统计 —— 如实留空，不编一个出来
      congestion_summary: null,
      path_lnglat: points.map(p => [p.lng, p.lat])
    };
  }

  function searchRouteViaJsApi() {
    return new Promise((resolve, reject) => {
      if (!state.map || !window.BMapGL || typeof BMapGL.DrivingRoute !== 'function') {
        reject(new Error('百度地图 JS API 驾车路线规划不可用'));
        return;
      }
      let settled = false;
      const done = (fn, arg) => { if (!settled) { settled = true; fn(arg); } };
      const timer = setTimeout(() => done(reject, new Error('浏览器端路线规划超时（15s）')), 15000);

      let drv;
      try {
        drv = new BMapGL.DrivingRoute(state.map, {
          // 不接管渲染：路线由本模块用自己的配色画，与后端结果保持同一观感
          renderOptions: { map: null, autoViewport: false },
          onSearchComplete: () => {
            clearTimeout(timer);
            try {
              const status = drv.getStatus();
              if (status !== 0) {
                done(reject, new Error('百度 JS API 返回状态 ' + status));
                return;
              }
              const results = drv.getResults();
              const n = results && results.getNumPlans ? results.getNumPlans() : 0;
              const routes = [];
              for (let i = 0; i < n; i++) routes.push(planToRoute(results.getPlan(i), i));
              if (!routes.length) { done(reject, new Error('未返回可用路线')); return; }
              done(resolve, routes);
            } catch (e) { done(reject, e); }
          }
        });
      } catch (e) { clearTimeout(timer); done(reject, e); return; }

      drv.search(
        new BMapGL.Point(DEMO_OD.origin.lng, DEMO_OD.origin.lat),
        new BMapGL.Point(DEMO_OD.dest.lng, DEMO_OD.dest.lat)
      );
    });
  }

  async function compareRoutes() {
    const btn = el('baiduRouteCompareBtn');
    const note = el('baiduRouteNote');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 规划中…'; }

    let routes = null;
    let backendDetail = '';
    let cached = false;

    // ① 优先后端代理：走服务端 AK，带短期缓存与调用审计
    try {
      const res = await fetch('/api/baidu/route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin_lng: DEMO_OD.origin.lng, origin_lat: DEMO_OD.origin.lat,
          dest_lng: DEMO_OD.dest.lng, dest_lat: DEMO_OD.dest.lat
        })
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data && data.success && (data.routes || []).length) {
        routes = data.routes;
        cached = !!data.cached;
      } else {
        backendDetail = (data && data.detail) || ('HTTP ' + res.status);
      }
    } catch (err) {
      backendDetail = err.message;
    }

    // ② 后端不可用 → 用同一把浏览器端 AK 走 JS API 兜底
    let viaJsApi = false;
    if (!routes) {
      if (note) {
        note.textContent = '服务端路径规划不可用（' + backendDetail + '），正在改用浏览器端 JS API 规划…';
      }
      try {
        routes = await searchRouteViaJsApi();
        viaJsApi = true;
      } catch (err) {
        const msg = '服务端：' + backendDetail + '；浏览器端：' + err.message;
        if (note) note.textContent = '⚠ 两条通道均失败 —— ' + msg;
        toast('百度路径规划失败：' + msg, 'error');
        if (btn) { btn.disabled = false; btn.textContent = '🚗 真实绕行路线对比'; }
        return;
      }
    }

    // ③ 绘制：两条通道拿到的是同构数据，画法完全一致
    try {
      clearOverlays();
      const colors = ['#38bdf8', '#f59e0b', '#a78bfa'];
      routes.slice(0, 3).forEach((r, i) => {
        const pts = (r.path_lnglat || []).map(pair => new BMapGL.Point(pair[0], pair[1]));
        if (pts.length < 2) return;
        const polyline = new BMapGL.Polyline(pts, {
          strokeColor: colors[i % colors.length],
          strokeWeight: i === 0 ? 6 : 4,
          strokeOpacity: i === 0 ? 0.9 : 0.7
        });
        state.map.addOverlay(polyline);
        state.overlays.push(polyline);
      });
      renderRouteCards(routes, viaJsApi
        ? '百度地图 JS API GL 驾车路线规划（浏览器端兜底）'
        : '百度地图驾车路径规划 API（后端服务端代理）');
      if (note) {
        note.textContent = (viaJsApi
          ? '真实数据源：百度地图 JS API GL 驾车路线规划（浏览器端）'
          : '真实数据源：百度地图驾车路径规划 API（后端服务端代理）' + (cached ? '（缓存结果）' : '')) +
          ' · OD：' + DEMO_OD.origin.name + ' → ' + DEMO_OD.dest.name +
          (viaJsApi ? ' · 服务端通道不可用：' + backendDetail : '');
      }
      toast('已获取 ' + routes.length + ' 条真实规划路线' +
        (viaJsApi ? '（浏览器端 JS API 兜底）' : ''), 'success');
    } catch (err) {
      if (note) note.textContent = '⚠ 路线绘制异常：' + err.message;
      toast('路线绘制异常：' + err.message, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '🚗 真实绕行路线对比'; }
    }
  }

  async function init() {
    try {
      const res = await fetch('/api/baidu/config');
      const data = await res.json();
      if (!data.configured || !data.ak) {
        showUnconfigured(data.note || '未配置 BAIDU_MAP_AK。');
        return;
      }
      state.ak = data.ak;
      state.center = data.center || state.center;
      setStatusBadge('LBS: 加载中…', 'var(--state-warn-text)');
      await loadBMapGL(state.ak);
      setupMap();
    } catch (err) {
      showUnconfigured('接入检测失败：' + err.message);
    }
  }

  function bindControls() {
    const toggle = el('baiduTrafficToggle');
    if (toggle) {
      toggle.addEventListener('change', (e) => {
        if (e.target.checked) addTrafficLayer(); else removeTrafficLayer();
      });
    }
    const btn = el('baiduRouteCompareBtn');
    if (btn) btn.addEventListener('click', compareRoutes);
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindControls();
    init();
  });

  // 暴露给 console 调试与后续模块联动
  window.BaiduMapBridge = { state, compareRoutes, DEMO_OD };
})();
