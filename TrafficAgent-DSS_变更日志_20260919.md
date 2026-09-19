# 变更日志 · 决策大屏「软件化」（手机 / 电脑可直接使用）

- 日期：2026-09-19
- 主题：把原先只能在开发机上用命令行启动的决策大屏，做成**双击即用的软件**——电脑端以独立窗口运行，手机端扫码打开并可加到主屏幕
- 代码基线：本地工作区对应远端 `4d615a4`（v2.2.1）之后的 Signal Console 前端；当时 `src/web/static/index.html` 仍带未解决的合并冲突标记
- 影响面：新增 13 个文件、修改 2 个文件；不改动任何仿真、控制与评价逻辑

---

## 1. 修复（P0）· 前端残留合并冲突标记

**文件**：`src/web/static/index.html`（删除 7 行冲突痕迹：3 行标记 `<<<<<<<` / `=======` / `>>>>>>>` 加 4 行 HEAD 侧旧文案；保留 8 行 Signal Console 侧内容）

- **现象**：第 312–326 行存在 `<<<<<<< HEAD` / `=======` / `>>>>>>> 2dd3103` 三段冲突标记。浏览器把它们当普通文本渲染，第 2 节（智能体思维链推理与拥堵归因）标题区直接显示裸文本 `<<<<<<< HEAD`，且该区块布局塌陷。任何人拉取都会拿到这个坏版本，打开大屏即可见。
- **成因**：Signal Console 视觉系统（`1e5efa1` / `2dd3103`）并入 main 时冲突未解决。远端已于 `38a17d84` 修复，而本地工作区仍是修复前状态（远端 blob `5c432c59` ≠ 本地 `df3e3371`）。
- **处置**：按远端同一策略解决——保留 Signal Console 新视觉侧（`.section-header-left` / `.section-title-en` / `.section-chip`），emoji 改为已有 SVG 图标（`#i-cpu`），两侧「LLM Reasoning Layer」文案均保留，信息无损失。
- **验证**：修复前服务端返回的页面含 1 处冲突标记，修复后为 **0 处**（`grep -c -E '^<{7}|^>{7}'` 实测）。

---

## 2. 新增 · 可安装 Web 应用（PWA）

| 文件 | 说明 |
|---|---|
| `src/web/static/manifest.json` | 应用清单：名称、图标、`standalone` 独立窗口、主题色、启动范围。用 `.json` 而非 `.webmanifest`，因为前者的 MIME 由标准库保证为 `application/json`，后者可能被当成 `application/octet-stream` 导致清单被静默忽略 |
| `src/web/static/sw.js` | 离线外壳。缓存策略按数据性质分级：`/api/**` **永不缓存**（避免把旧仿真数字当成新结果展示）；导航请求网络优先、失败回落缓存壳；`/static/**` 走 stale-while-revalidate。预缓存用 `Promise.allSettled`，单个文件 404 不会让整个 install 失败 |
| `src/web/static/js/pwa.js` | 注册 Service Worker；手机端引导「添加到主屏幕」（Android 用 `beforeinstallprompt`，iOS 给出分享菜单文字指引）；状态栏配色跟随页面主题切换。不触碰 `dashboard.js` 的任何状态或 DOM id，保持可合并性 |
| `src/web/static/pwa/` 下 5 个文件 | 主屏与桌面图标：`icon-192.png` / `icon-512.png` / `apple-touch-icon-180.png` / `maskable-512.png` / `app.ico`（256px，供 Windows 快捷方式），图案与 `favicon.svg` 一致 |
| `scripts/make_pwa_icons.py` | 图标生成器。**零第三方依赖**：自写 PNG 编码（`zlib` + `struct`），抗锯齿用有符号距离场逐像素算覆盖率。整批产出（4 张 PNG + 1 个 ICO）生成耗时约 1.6 秒。换配色只需改常量重跑 |

---

## 3. 新增 · 手机端适配

**文件**：`src/web/static/css/mobile.css`（追加在 `style.css` 之后，只覆盖手机需要的部分）

- iOS 刘海与底部指示条安全区（`env(safe-area-inset-*)`，配合 `viewport-fit=cover`）
- 820px 以下：侧栏取消吸顶、快速导航改为可横滑的标签行、KPI 卡片两列、分区标题换行不挤压、关闭毛玻璃提升中端机流畅度
- 420px 以下：KPI 单列
- 触摸设备：可点元素最小 44px，输入框字号 16px（避免 iOS 聚焦时自动放大）
- 宽表格在自身容器内横向滚动，不撑破整页；`min-width:0` 修掉 flex/grid 子项默认不收窄导致的横向溢出
- 独立窗口模式下禁止下拉橡皮筋滚动

## 4. 修改 · 新增 Service Worker 路由

**文件**：`src/web/app.py`（仅新增一条只读路由，未改动既有逻辑）

```python
@app.get("/sw.js", include_in_schema=False)
def serve_service_worker():
    ...
    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"}
```

原因：Service Worker 只能控制其自身 URL 及其下级路径。若把文件放在 `/static/sw.js`，作用域会被限制在 `/static/**`，永远拦截不到页面导航。`Cache-Control: no-cache` 避免浏览器长期钉住旧 worker。

---

## 5. 新增 · 一键启动器（电脑双击 / 手机扫码）

| 文件 | 说明 |
|---|---|
| `scripts/launch_app.py` | 启动器：自动把 `SUMO_HOME` 指向项目自带的 SUMO；绑定 `0.0.0.0`（原 `start_local.bat` 绑的是 `127.0.0.1`，手机永远连不上）；探测局域网 IP；端口被占用自动顺延；打印二维码（PNG + 终端 ASCII 两种）；用 Chrome/Edge 的 `--app` 开独立窗口，找不到再退回默认浏览器；浏览器在服务就绪后才打开（守护线程轮询端口，不用固定 sleep） |
| `start_app.bat` | 双击入口。`.bat` 只用 ASCII 字符，中文提示由 Python 打印，避免 Windows 控制台代码页乱码；已设置 `chcp 65001` / `PYTHONIOENCODING` / `PYTHONUTF8` |

二维码与图标一样是**零依赖**实现：`qrcode` 包默认用 Pillow 渲染 PNG，而项目依赖里没有 Pillow，因此改用 `qr.get_matrix()` 取模块矩阵后自行编码 PNG（32 行代码）。

---

## 6. 验证（实测证据）

| 项目 | 结果 |
|---|---|
| 页面与资源端点 | `/`、`/sw.js`、`/static/manifest.json`、`/static/css/mobile.css`、`/static/js/pwa.js`、`/static/pwa/*.png`、`/static/pwa/app.ico` 全部 `200`，MIME 正确（`application/javascript` / `application/json` / `text/css` / `image/png` / `image/x-icon`） |
| 冲突标记 | 服务端返回页面中 **0 处**（修复前 1 处） |
| 业务接口 | `POST /api/diagnose` → `200`，耗时 **0.1 s**；`GET /api/detectors` → 引擎 `mesoscopic_network`、20 个检测器；`GET /api/status` → 版本 `2.3.0` |
| SUMO | 正确识别 `E:\TrafficAgent-DSS\.sumo\sumo-1.21.0\bin\sumo.exe` |
| 启动器 | `start_app.bat` 实跑通过：中文横幅正常、`SUMO_HOME` 自动设置、8000 被占用时自动顺延到 8001 |
| 图标与二维码 | 图标渲染已肉眼确认；二维码 PNG 生成成功并已目视确认为有效码 |

---

## 7. 已知限制

1. **手机端拿不到完整 PWA 能力**：手机是通过局域网 `http://192.168.x.x` 访问的，浏览器规定只有 https 或 localhost 才算安全上下文，因此手机上 Service Worker（离线缓存）与 Android 自动安装横幅不生效。iPhone 仍可通过「添加到主屏幕」得到图标与独立窗口，功能一致。要做到手机上也是完整 PWA，需要部署到 https 域名。
2. **手机必须与电脑连同一个 Wi-Fi**，且电脑保持开机、服务窗口不关闭。
3. **Windows 防火墙**：首次启动必须允许 `python.exe` 访问专用网络，否则手机连不上。
4. **百度地图未接入**：`.env` 中 `BAIDU_MAP_AK` 与 `BAIDU_MAP_SERVER_AK` 均为空，大屏显示「LBS 未接入」。
5. **LLM 密钥无效**：`.env` 的 `LLM_API_KEY` 已填但调用返回 `401`，叙事层降级为规则模板；数值链路不受影响。
6. **独立窗口依赖浏览器**：需要已安装 Chrome 或 Edge，否则退回默认浏览器的普通标签页。

## 8. 后续

- 若确定走公网 https 部署，可补齐第 7 条第 1 点的完整 PWA 能力（前提：作品所有者同意公开，且云端能安装 `requirements.txt` 依赖；SUMO 二进制装不上，仿真会走中观引擎）
- 本文件为独立变更日志；正式并库时可合并进仓库根部的 `CHANGELOG.md`

---

## 附：文件清单

**新增（13）**：`src/web/static/manifest.json`、`src/web/static/sw.js`、`src/web/static/js/pwa.js`、`src/web/static/css/mobile.css`、`src/web/static/pwa/icon-192.png`、`src/web/static/pwa/icon-512.png`、`src/web/static/pwa/apple-touch-icon-180.png`、`src/web/static/pwa/maskable-512.png`、`src/web/static/pwa/app.ico`、`scripts/make_pwa_icons.py`、`scripts/launch_app.py`、`start_app.bat`、`docs/RUN_AS_APP.md`

**修改（2）**：`src/web/static/index.html`（清冲突标记 + 注入 PWA 标签）、`src/web/app.py`（新增 `GET /sw.js` 路由）
