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

  function setStatusBadge(text, color) {
    const badge = el('baiduMapStatusBadge');
    if (badge) {
      badge.textContent = text;
      badge.style.color = color || 'var(--text-muted)';
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
    setStatusBadge('LBS: 未配置', '#f87171');
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

    setStatusBadge('LBS: 已接入 (JS API GL · 实时路况)', '#34d399');
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

  function renderRouteCards(routes) {
    const wrap = el('baiduRouteCards');
    if (!wrap) return;
    wrap.innerHTML = routes.map((r, i) => {
      const color = i === 0 ? '#38bdf8' : '#f59e0b';
      const badge = i === 0 ? '推荐路线' : '备选 ' + i;
      const cong = r.congestion_summary;
      const congText = cong && cong.congested_ratio !== null && cong.congested_ratio !== undefined
        ? ` · 拥堵里程占比 ${(cong.congested_ratio * 100).toFixed(0)}% (${cong.label})`
        : '';
      return '<div class="route-card" style="border-left:3px solid ' + color + ';">' +
        '<div class="route-card-head"><span class="route-badge" style="color:' + color + '">' + badge + '</span>' +
        '<span class="route-km">' + r.distance_km + ' km · ' + r.duration_min + ' 分钟</span></div>' +
        '<div class="route-card-sub">百度地图实时路径规划' + congText + '</div>' +
        '</div>';
    }).join('');
  }

  async function compareRoutes() {
    const btn = el('baiduRouteCompareBtn');
    const note = el('baiduRouteNote');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 规划中…'; }
    try {
      const res = await fetch('/api/baidu/route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          origin_lng: DEMO_OD.origin.lng, origin_lat: DEMO_OD.origin.lat,
          dest_lng: DEMO_OD.dest.lng, dest_lat: DEMO_OD.dest.lat
        })
      });
      const data = await res.json();
      if (!res.ok || !data.success) {
        const msg = data.detail || ('路径规划失败 (HTTP ' + res.status + ')');
        if (note) note.textContent = '⚠ ' + msg;
        toast('百度路径规划失败：' + msg, 'error');
        return;
      }
      clearOverlays();
      const colors = ['#38bdf8', '#f59e0b', '#a78bfa'];
      (data.routes || []).slice(0, 3).forEach((r, i) => {
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
      renderRouteCards(data.routes || []);
      if (note) {
        note.textContent = (data.cached ? '（缓存结果）' : '') +
          '真实数据源：百度地图驾车路径规划 API · OD：' + DEMO_OD.origin.name + ' → ' + DEMO_OD.dest.name;
      }
      toast('已获取 ' + (data.routes || []).length + ' 条真实规划路线', 'success');
    } catch (err) {
      if (note) note.textContent = '⚠ 请求异常：' + err.message;
      toast('路径规划请求异常：' + err.message, 'error');
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
      setStatusBadge('LBS: 加载中…', '#fbbf24');
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
