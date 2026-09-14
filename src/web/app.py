"""
TrafficAgent-DSS: Modern Decoupled Web API & Decision Support Backend
Built with FastAPI, providing RESTful endpoints for real-time situational awareness,
LLM Chain-of-Thought (CoT) reasoning, What-If simulation rollouts, and report export.
"""

import mimetypes
import os
import sys
import json
import shutil
import threading
import time
import secrets
from pathlib import Path
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response, status, Depends, Header
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

# Ensure project root is on sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

# ---------------------------------------------------------------------------
# .env loading (must happen BEFORE any module reads os.environ).
#
# `python-dotenv` was already a dependency and `.env.example` told operators to
# copy it to `.env`, but nothing ever called `load_dotenv()` — so a correctly
# filled `.env` had NO effect: BAIDU_MAP_AK stayed empty (the dashboard kept
# reporting "LBS 未接入") and LLM_API_KEY was ignored. Silent misconfiguration is
# the worst kind, because the operator believes they configured it.
#
# `override=False` keeps real environment variables winning over the file, which
# is what CI and container deployments expect. The path is resolved from the
# project root rather than the CWD, because uvicorn is often launched from
# elsewhere. A missing python-dotenv degrades to plain os.environ instead of
# crashing the server.
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv

    _ENV_FILE = root_dir / ".env"
    _DOTENV_LOADED = bool(_load_dotenv(dotenv_path=_ENV_FILE, override=False))
    _DOTENV_STATUS = f"loaded {_ENV_FILE}" if _DOTENV_LOADED else f"no readable {_ENV_FILE}"
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    _DOTENV_STATUS = "python-dotenv not installed; using process environment only"
except Exception as _exc:  # pragma: no cover - never let config loading kill startup
    _DOTENV_STATUS = f"failed to load .env: {_exc}"

from src.tools.webster import WebsterSignalOptimizer
from src.tools.green_wave import GreenWaveCoordinator
from src.tools.rerouting import DynamicReroutingAllocator
from src.tools.evaluator import PerformanceEvaluator
from src.agents.traffic_agent import TrafficDecisionAgent
from src.agents.llm_client import LLMReasoningClient

# ---------------------------------------------------------------------------
# Static-resource MIME hardening.
#
# Python's `mimetypes` reads the Windows registry on this platform, and a third-party
# installer can rewrite HKCR\.css to an unofficial `application/x-css`. Starlette's
# StaticFiles then serves style.css with that Content-Type, Chrome enforces strict MIME
# checking on stylesheets and refuses to apply it, and the whole dashboard renders
# unstyled (measured: 0 CSS rules parsed, 257 when served as text/css).
#
# The mapping must never depend on the host OS, so register the core web types
# explicitly at import time. This is a no-op on Linux/macOS and a fix on polluted
# Windows machines (e.g. a competition demo laptop).
for _ext, _type in (
    (".css", "text/css"),
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".json", "application/json"),
    (".svg", "image/svg+xml"),
    (".woff", "font/woff"),
    (".woff2", "font/woff2"),
):
    mimetypes.add_type(_type, _ext)

APP_VERSION = "2.3.0"
DEFAULT_DASHBOARD_PORT = 8501

# Application initialization
app = FastAPI(
    title="TrafficAgent-DSS 城市交通拥堵治理决策支持系统 API",
    description="2026年第十六届北京市大学生交通科技大赛 · ITSAC 2026 创新挑战赛（赛题2）现代化解耦架构决策服务",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Enable CORS for full decouple flexibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("TRAFFIC_ALLOWED_ORIGINS", "").split(",")
                   if origin.strip() and origin.strip() != "*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

# Global Traffic Decision Agent
agent = TrafficDecisionAgent()

# Static directories
from src.web import network_api

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Real road-network APIs: topology/live map, detectors, action plan.
app.include_router(network_api.router)
if not STATIC_DIR.exists():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
INDEX_HTML_PATH = STATIC_DIR / "index.html"


# Pydantic Request & Response Models
class TrafficStateInput(BaseModel):
    bottleneck_edge: str = Field(default="J1_J2 (主干线合流段)", description="瓶颈路段标识")
    queue_m: float = Field(default=165.0, ge=0.0, le=10000.0, description="瓶颈处当前排队长度 (米)")
    link_length_m: float = Field(default=300.0, gt=0.0, le=10000.0, description="瓶颈路段总长度 (米)")
    speed_kmh: float = Field(default=8.2, ge=0.0, le=200.0, description="瓶颈瞬时车速 (km/h)")
    occupancy: float = Field(default=0.82, ge=0.0, le=1.0, description="断面车道占有率")
    bypass_occupancy: float = Field(default=0.28, ge=0.0, le=1.0, description="平行旁路车道占有率")


class StrategyFormulationInput(BaseModel):
    diagnosis: Optional[Dict[str, Any]] = Field(default=None, description="智能体态势感知与归因诊断输出")


class RolloutConfigInput(BaseModel):
    corridor_choice: str = Field(default="北京典型交通走廊 (学院南路-交大东路瓶颈干线)")
    congestion_type: str = Field(default="晚高峰潮汐高负荷 + 突发交通事故 (占道停靠)")
    duration: int = Field(default=600, ge=300, le=1800, description="推演总时长 (秒)")
    incident_start: int = Field(default=150, ge=0, le=1200, description="事故开始时间 (秒)")
    incident_end: int = Field(default=420, ge=0, le=1800, description="事故撤离时间 (秒)")
    use_rerouting: bool = Field(default=True, description="是否启用动态诱导分流")
    use_green_wave: bool = Field(default=True, description="是否启用干线绿波协调")
    use_webster: bool = Field(default=True, description="是否启用 Webster 信号配时优化")
    run_physical_sandbox: bool = Field(default=True, description="是否执行本地微观 SUMO 进程推演（默认开启；关闭时仅返回标定经验数据且不提供统计推断）")
    seed: Optional[int] = Field(default=None, ge=0, description="随机种子 (用于可复现仿真)")

    @model_validator(mode="after")
    def validate_incident_window(self):
        if self.incident_start >= self.incident_end:
            raise ValueError(f"事故开始时间 ({self.incident_start}s) 必须早于事故撤离时间 ({self.incident_end}s)")
        if self.incident_end > self.duration:
            raise ValueError(f"事故撤离时间 ({self.incident_end}s) 不能超出推演总时长 ({self.duration}s)")
        return self


def _scenario_parameters(corridor: str, congestion: str) -> Dict[str, float]:
    """Map UI scenario labels to explicit mesoscopic demand/capacity parameters."""
    text = f"{corridor} {congestion}"
    params = {"demand_multiplier": 1.25, "capacity_multiplier": 0.38}
    if "西二环" in text or "快速路" in text:
        params.update(demand_multiplier=1.35, capacity_multiplier=0.32)
    if "施工" in text or "缩减" in text or "封道" in text:
        params.update(demand_multiplier=1.45, capacity_multiplier=0.25)
    elif "溢流" in text or "失衡" in text:
        params.update(demand_multiplier=1.30, capacity_multiplier=0.45)
    return params


class MultiSeedEvaluationInput(BaseModel):
    seeds: Optional[List[int]] = Field(default=[42, 101, 2024, 777, 999], description="评估随机种子列表")
    duration: int = Field(default=600, ge=30, le=1800, description="单次推演时长 (秒)")
    incident_start: int = Field(default=150, ge=0, le=1200, description="事故开始时间 (秒)")
    incident_end: int = Field(default=420, ge=0, le=1800, description="事故撤离时间 (秒)")
    run_physical_sandbox: bool = Field(default=True, description="是否调用本地微观 SUMO 进行全量仿真推演（默认开启；置信区间与显著性检验仅在物理模式下提供）")

    @model_validator(mode="after")
    def validate_batch_window(self):
        if self.seeds is not None:
            if len(self.seeds) == 0:
                raise ValueError("seeds 列表不能为空")
            if len(self.seeds) > 100:
                raise ValueError("seeds 列表最多允许 100 个随机种子")
            for s in self.seeds:
                if s is None or s < 0:
                    raise ValueError(f"种子必须为非负整数，收到: {s}")
        if self.incident_start >= self.incident_end:
            raise ValueError(f"事故开始时间 ({self.incident_start}s) 必须早于事故撤离时间 ({self.incident_end}s)")
        if self.incident_end > self.duration:
            raise ValueError(f"事故撤离时间 ({self.incident_end}s) 不能超出推演总时长 ({self.duration}s)")
        return self


class ReportExportInput(BaseModel):
    diagnosis: Optional[Dict[str, Any]] = None
    strategies: Optional[Dict[str, Any]] = None
    rollout_data: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_structures(self):
        if self.rollout_data is not None:
            if not isinstance(self.rollout_data, dict):
                raise ValueError("rollout_data 必须为字典结构")
            kpis = self.rollout_data.get("kpis")
            if kpis is not None and not isinstance(kpis, dict):
                raise ValueError("rollout_data 中的 kpis 必须为字典结构")
        return self


class DecisionPipelineInput(BaseModel):
    traffic_state: Optional[TrafficStateInput] = None
    rollout_config: Optional[RolloutConfigInput] = None


class ClosedLoopOptimizationInput(BaseModel):
    """
    Input for the LLM-in-the-loop control optimisation endpoint.

    `rounds` counts **model** decision rounds. 0 means "run the deterministic rule
    chain only" — useful for getting the reference result without calling a model at all.
    """
    traffic_state: Optional[TrafficStateInput] = None
    rounds: int = Field(default=2, ge=0, le=4, description="大模型闭环决策轮数（0 = 仅确定性规则链，不调用大模型）")
    duration: int = Field(default=600, ge=300, le=1200, description="单次推演时长 (秒)")
    incident_start: int = Field(default=150, ge=0, le=1200, description="事故开始时间 (秒)")
    incident_end: int = Field(default=420, ge=0, le=1800, description="事故撤离时间 (秒)")
    seed: Optional[int] = Field(default=None, ge=0, description="随机种子 (用于可复现仿真)")
    use_rerouting: bool = Field(default=True, description="是否允许闭环策略使用动态诱导分流")
    use_webster: bool = Field(default=True, description="是否允许闭环策略使用 Webster 信号配时")

    @model_validator(mode="after")
    def validate_window(self):
        if self.incident_start >= self.incident_end:
            raise ValueError(f"事故开始时间 ({self.incident_start}s) 必须早于事故撤离时间 ({self.incident_end}s)")
        if self.incident_end > self.duration:
            raise ValueError(f"事故撤离时间 ({self.incident_end}s) 不能超出推演总时长 ({self.duration}s)")
        return self


class LLMDetectModelsInput(BaseModel):
    api_key: Optional[str] = Field(default=None, description="API 密钥 (留空则使用当前已配置密钥)")
    base_url: Optional[str] = Field(default=None, description="API Base URL (如 https://api.deepseek.com/v1)")


class LLMConfigInput(BaseModel):
    api_key: Optional[str] = Field(default=None, description="API 密钥")
    base_url: Optional[str] = Field(default=None, description="API Base URL")
    model: Optional[str] = Field(default=None, description="目标大模型 ID")



# Default baseline calibrated datasets
def get_calibrated_rollout_data(
    duration: int = 600,
    incident_start: int = 150,
    incident_end: int = 420,
    use_rerouting: bool = True,
    use_green_wave: bool = True,
    use_webster: bool = True,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Returns calibrated empirical simulation benchmark data.
    Dynamically adjusts metrics if the operator disables specific control components.
    """
    # Factor adjustments based on enabled tools
    b_delay = 46.8
    b_queue = 105.0
    b_speed = 22.4
    b_throughput = 1780.0
    b_co2 = 134.6

    if not use_rerouting:
        b_delay += 10.5
        b_queue += 45.0
        b_speed -= 4.2
        b_throughput -= 140.0
        b_co2 += 12.0

    if not use_green_wave:
        b_delay += 6.2
        b_queue += 25.0
        b_speed -= 2.8
        b_throughput -= 80.0
        b_co2 += 7.5

    if not use_webster:
        b_delay += 12.0
        b_queue += 35.0
        b_speed -= 3.5
        b_throughput -= 120.0
        b_co2 += 14.0

    # Ensure bounds
    b_delay = round(max(38.0, min(80.0, b_delay)), 1)
    b_queue = round(max(70.0, min(220.0, b_queue)), 1)
    b_speed = round(max(10.5, min(28.0, b_speed)), 1)
    b_throughput = round(max(1400.0, min(1900.0, b_throughput)), 1)
    b_co2 = round(max(120.0, min(175.0, b_co2)), 1)

    base_kpi = {
        "avg_delay_s": 84.5,
        "max_queue_m": 242.0,
        "avg_speed_kmh": 9.4,
        "throughput_vph": 1340.0,
        "delay_variance": 480.2,
        "co2_emissions_kg": 182.4
    }

    strat_a_kpi = {
        "avg_delay_s": 65.2,
        "max_queue_m": 185.0,
        "avg_speed_kmh": 13.8,
        "throughput_vph": 1520.0,
        "delay_variance": 340.5,
        "co2_emissions_kg": 158.2
    }

    strat_b_kpi = {
        "avg_delay_s": b_delay,
        "max_queue_m": b_queue,
        "avg_speed_kmh": b_speed,
        "throughput_vph": b_throughput,
        "delay_variance": 195.0,
        "co2_emissions_kg": b_co2
    }

    # Relative improvement
    delay_imp = round(((base_kpi["avg_delay_s"] - strat_b_kpi["avg_delay_s"]) / base_kpi["avg_delay_s"]) * 100, 1)
    queue_imp = round(((base_kpi["max_queue_m"] - strat_b_kpi["max_queue_m"]) / base_kpi["max_queue_m"]) * 100, 1)
    speed_imp = round(((strat_b_kpi["avg_speed_kmh"] - base_kpi["avg_speed_kmh"]) / base_kpi["avg_speed_kmh"]) * 100, 1)
    tp_imp = round(((strat_b_kpi["throughput_vph"] - base_kpi["throughput_vph"]) / base_kpi["throughput_vph"]) * 100, 1)
    co2_imp = round(((base_kpi["co2_emissions_kg"] - strat_b_kpi["co2_emissions_kg"]) / base_kpi["co2_emissions_kg"]) * 100, 1)
    var_imp = 59.4 if (use_webster and use_green_wave) else (35.0 if use_webster else 15.0)

    # Calculate dissipation rate for strategy b based on active tools
    dissipation_rate_b = 0.8
    if use_webster:
        dissipation_rate_b += 0.3
    if use_green_wave:
        dissipation_rate_b += 0.25
    if use_rerouting:
        dissipation_rate_b += 0.25

    # Time series profile generation
    step = 10
    time_steps = list(range(0, duration + 1, step))
    q_base = []
    q_a = []
    q_b = []

    for t in time_steps:
        if t < incident_start:
            q_base.append(round(min(30.0, 12.0 + t * 0.08), 1))
            q_a.append(round(min(28.0, 12.0 + t * 0.07), 1))
            q_b.append(round(min(25.0, 12.0 + t * 0.05), 1))
        elif t <= incident_end:
            dt = t - incident_start
            q_base.append(round(min(base_kpi["max_queue_m"], 25.0 + (dt ** 1.35) * 0.22), 1))
            q_a.append(round(min(strat_a_kpi["max_queue_m"], 22.0 + (dt ** 1.25) * 0.20), 1))
            q_b.append(round(min(strat_b_kpi["max_queue_m"], 18.0 + (dt ** 1.10) * 0.16), 1))
        else:
            dt = t - incident_end
            q_base.append(round(max(75.0, base_kpi["max_queue_m"] - dt * 0.8), 1))
            q_a.append(round(max(35.0, strat_a_kpi["max_queue_m"] - dt * 1.1), 1))
            q_b.append(round(max(10.0, strat_b_kpi["max_queue_m"] - dt * dissipation_rate_b), 1))

    # Radar scores (0-100 normalized)
    radar = {
        "dimensions": ["通行效率", "空间治堵", "容量释放", "运行平稳", "绿色低碳"],
        "baseline": [50, 50, 50, 50, 50],
        "strategy_a": [65, 60, 68, 62, 66],
        "strategy_b": [
            max(35, min(99, int(50 + delay_imp * 1.15))),
            max(35, min(99, int(50 + queue_imp * 0.95))),
            max(40, min(98, int(50 + tp_imp * 1.25))),
            max(40, min(95, int(50 + var_imp * 0.80))),
            max(40, min(95, int(50 + co2_imp * 1.30)))
        ]
    }

    return {
        # 本数据集为标定经验数据（非 SUMO 微观仿真实测），在此显式标注来源，
        # 避免下游决策简报将其误标为物理仿真结果。
        "execution_mode": "calibrated_empirical_fast",
        "simulation_duration": duration,
        "seed": seed,
        "incident_window": [incident_start, incident_end],
        "kpis": {
            "baseline": base_kpi,
            "strategy_a": strat_a_kpi,
            "strategy_b": strat_b_kpi
        },
        "comparisons": {
            "strategy_b": {
                "delay_improvement_pct": delay_imp,
                "queue_improvement_pct": queue_imp,
                "speed_improvement_pct": speed_imp,
                "throughput_improvement_pct": tp_imp,
                "variance_improvement_pct": var_imp,
                "co2_improvement_pct": co2_imp,
                "overall_effectiveness_grade": "卓越 (Level A+)" if delay_imp >= 35 else ("良好 (Level A)" if delay_imp >= 20 else "一般 (Level B)")
            }
        },
        "time_series": {
            "time_steps": time_steps,
            "queue_baseline": q_base,
            "queue_strategy_a": q_a,
            "queue_strategy_b": q_b
        },
        "radar": radar,
        # Uniform response shape across execution modes: a client can always read these
        # keys. In calibrated mode nothing was ever pushed into a simulator, and saying so
        # explicitly is the honest answer.
        "control_evidence": {
            "physical_control_applied": False,
            "note": "本模式为标定数据集推演，未向 SUMO 仿真器下发任何控制指令。",
            "baseline": {},
            "strategy_a": {},
            "strategy_b": {},
        },
        "strategy_inputs": {},
        "scenario": {
            "simulated_corridor": "标定数据集（未运行路网）",
            "note": "标定经验数据，非实测；切换 run_physical_sandbox=true 可执行真实微观仿真。",
        },
    }


# REST API Endpoints
@app.get("/api/status", summary="系统健康与状态检查")
def get_system_status():
    """
    Returns system health, agent status, and active scenario configurations.

    Declared as a sync handler so FastAPI runs it in its threadpool: this endpoint
    must stay responsive while a long SUMO rollout occupies a worker thread.
    """
    has_sumo = False
    try:
        if agent.sandbox.sumo_bin:
            p = Path(agent.sandbox.sumo_bin)
            if p.exists() or shutil.which(agent.sandbox.sumo_bin):
                has_sumo = True
    except Exception:
        has_sumo = False

    return {
        "status": "online",
        "system_name": "TrafficAgent-DSS 城市交通拥堵治理决策支持系统",
        "version": APP_VERSION,
        "architecture": "Decoupled Modern RESTful API + Responsive Dashboard",
        "agent_brain": {
            "state": "ready",
            "model_type": "LLM Chain-of-Thought Decision Agent (OpenAI-compatible endpoint)",
            "llm": agent.llm.describe(),
            "reasoning_available": agent.llm.is_configured,
            "degradation_notice": (
                None if agent.llm.is_configured
                else "未配置 LLM_API_KEY：归因与方案叙事将降级为确定性规则模板，"
                     "API 响应中的 reasoning_mode / narrative_mode 会如实标注。"
            ),
            "tools": ["Webster Signal Optimizer", "Green Wave Coordinator", "Dynamic Rerouting Allocator", "5D Performance Evaluator"]
        },
        "simulation_engine": {
            "engine": "SUMO (Simulation of Urban MObility) / TraCI",
            "detected_sumo_binary": agent.sandbox.sumo_bin,
            "binary_exists": has_sumo
        },
        "competition_tracks": [
            "2026年第十六届北京市大学生交通科技大赛",
            "ITSAC 2026 创新挑战赛（赛题2：基于交通仿真智能体的城市交通拥堵治理决策支持）",
            "百度地图开发者创作大赛 (2026-09)"
        ],
        "default_state": {
            "bottleneck_edge": "J1_J2 (主干线合流段)",
            "queue_m": 165.0,
            "link_length_m": 300.0,
            "speed_kmh": 8.2,
            "occupancy": 0.82,
            "bypass_occupancy": 0.28
        }
    }


def require_admin(authorization: Optional[str] = Header(default=None)):
    token = os.getenv("TRAFFIC_ADMIN_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "模型管理未启用，请由管理员配置管理令牌。")
    if not secrets.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "管理令牌无效。", headers={"WWW-Authenticate": "Bearer"})


@app.post("/api/llm/detect-models", dependencies=[Depends(require_admin)], summary="自动识别与探测可用大模型列表 (ccSwitch 风格)")
def detect_llm_models(payload: Optional[LLMDetectModelsInput] = None):
    """
    Queries /v1/models endpoint from the provided or current base_url and api_key,
    auto-detecting all available model IDs.
    """
    input_payload = payload or LLMDetectModelsInput()
    key = input_payload.api_key or agent.llm.api_key
    url = input_payload.base_url or agent.llm.base_url or "https://api.openai.com/v1"

    models, err = LLMReasoningClient.list_available_models(api_key=key, base_url=url)
    return {
        "success": err is None,
        "models": models,
        "count": len(models),
        "current_model": agent.llm.model,
        "base_url": url,
        "error": err,
    }


@app.get("/api/llm/config", summary="获取当前大模型配置与状态")
async def get_llm_config():
    """
    Returns non-sensitive active LLM configuration and status.
    """
    desc = agent.llm.describe()
    return {
        "status": "success",
        "llm": desc,
    }


@app.post("/api/llm/config", dependencies=[Depends(require_admin)], summary="热更新并切换大模型配置")
def update_llm_config(payload: LLMConfigInput):
    """
    Hot-updates API key, base URL, and active model for the running agent.
    """
    desc = agent.update_llm_config(
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
    )
    return {
        "success": True,
        "llm": desc,
        "message": f"模型配置已实时更新为: {desc.get('model')}",
    }


# ---------------------------------------------------------------------------
# 百度地图 LBS 能力接入（地图开发者创作大赛核心功能底座）
# AK 通过 .env 的 BAIDU_MAP_AK 配置；浏览器端 AK 靠百度控制台的 Referer 白名单保护。
# ---------------------------------------------------------------------------
BAIDU_MAP_AK = os.environ.get("BAIDU_MAP_AK", "").strip()

# Baidu validates AKs differently depending on the *application type* chosen in the console:
#   · 浏览器端 AK  -> validated by Referer whitelist (safe to ship to the browser)
#   · 服务端 AK    -> validated by optional IP whitelist (must NEVER reach the browser)
# `/api/baidu/route` calls the driving-direction **Web 服务 API from the server**, so a
# browser-type AK can be rejected there ("Referer 校验失败"), because an httpx request
# carries no Referer. Allow a dedicated server AK; fall back to the browser AK so a single
# key still works on setups where Baidu accepts it.
BAIDU_MAP_SERVER_AK = (os.environ.get("BAIDU_MAP_SERVER_AK", "") or "").strip() or BAIDU_MAP_AK
_BAIDU_SERVER_AK_DEDICATED = bool(
    (os.environ.get("BAIDU_MAP_SERVER_AK", "") or "").strip()
)

BAIDU_MAP_CENTER_LNG = float(os.environ.get("BAIDU_MAP_CENTER_LNG", "116.337") or 116.337)
BAIDU_MAP_CENTER_LAT = float(os.environ.get("BAIDU_MAP_CENTER_LAT", "39.965") or 39.965)
_BAIDU_DIRECTION_URL = "https://api.map.baidu.com/direction/v2/driving"

# One-line startup disclosure of the *integration* state, so a misconfigured .env is
# obvious in the server log instead of only showing up as "未接入" on the dashboard.
# Only presence is reported — never the value of a key.
print(
    "[TrafficAgent-DSS] config: "
    f".env {_DOTENV_STATUS} | "
    f"BAIDU_MAP_AK {'SET' if BAIDU_MAP_AK else 'EMPTY (dashboard shows LBS 未接入)'} | "
    f"BAIDU_MAP_SERVER_AK "
    f"{'SET (dedicated)' if _BAIDU_SERVER_AK_DEDICATED else ('fallback -> BAIDU_MAP_AK' if BAIDU_MAP_AK else 'EMPTY (route proxy unavailable)')} | "
    f"LLM_API_KEY {'SET' if os.environ.get('LLM_API_KEY', '').strip() else 'EMPTY (rule-template fallback)'}",
    flush=True,
)
_BAIDU_ROUTE_CACHE: Dict[str, Dict[str, Any]] = {}
_BAIDU_ROUTE_CACHE_TTL_S = 120.0
_BAIDU_ROUTE_CACHE_MAX = 256
# Guards _BAIDU_ROUTE_CACHE: this handler is async (it awaits httpx), so concurrent
# requests can interleave between the lookup and the store.
_BAIDU_ROUTE_CACHE_LOCK = threading.Lock()


def _baidu_cache_get(key: str) -> Optional[Dict[str, Any]]:
    """Return a non-expired cache entry, sweeping expired ones when the map grows."""
    now = time.time()
    with _BAIDU_ROUTE_CACHE_LOCK:
        entry = _BAIDU_ROUTE_CACHE.get(key)
        if entry is not None and now - entry.get("_ts", 0.0) >= _BAIDU_ROUTE_CACHE_TTL_S:
            entry = None
        if len(_BAIDU_ROUTE_CACHE) > _BAIDU_ROUTE_CACHE_MAX:
            # Bounded growth: drop everything past its TTL, then the oldest half if needed.
            for k in [k for k, v in _BAIDU_ROUTE_CACHE.items()
                      if now - v.get("_ts", 0.0) >= _BAIDU_ROUTE_CACHE_TTL_S]:
                _BAIDU_ROUTE_CACHE.pop(k, None)
            if len(_BAIDU_ROUTE_CACHE) > _BAIDU_ROUTE_CACHE_MAX:
                for k, _ in sorted(_BAIDU_ROUTE_CACHE.items(), key=lambda kv: kv[1].get("_ts", 0.0))[
                    : len(_BAIDU_ROUTE_CACHE) - _BAIDU_ROUTE_CACHE_MAX
                ]:
                    _BAIDU_ROUTE_CACHE.pop(k, None)
    return entry


def _baidu_cache_put(key: str, value: Dict[str, Any]) -> None:
    with _BAIDU_ROUTE_CACHE_LOCK:
        _BAIDU_ROUTE_CACHE[key] = value


class BaiduRouteInput(BaseModel):
    """驾车路径规划请求（百度 BD-09 坐标）。"""

    origin_lng: float = Field(..., ge=73.0, le=136.0, description="起点经度 (BD-09)")
    origin_lat: float = Field(..., ge=15.0, le=55.0, description="起点纬度 (BD-09)")
    dest_lng: float = Field(..., ge=73.0, le=136.0, description="终点经度 (BD-09)")
    dest_lat: float = Field(..., ge=15.0, le=55.0, description="终点纬度 (BD-09)")


@app.get("/api/baidu/config", summary="百度地图前端接入配置 (AK 与地图中心)")
def get_baidu_config():
    """
    Returns the browser-side Baidu Map AK and default map center.
    An absent AK is reported honestly so the dashboard can show a real
    'not configured' state instead of a fabricated map.
    """
    return {
        "success": True,
        "configured": bool(BAIDU_MAP_AK),
        "ak": BAIDU_MAP_AK,
        "center": {"lng": BAIDU_MAP_CENTER_LNG, "lat": BAIDU_MAP_CENTER_LAT},
        "gl_api": "https://api.map.baidu.com/api?v=1.0&type=webgl&ak=",
        "note": (
            "浏览器端 AK 属公开凭据，请务必在百度地图开放平台控制台为该 AK 配置 "
            f"Referer 白名单（默认服务端口为 http://127.0.0.1:{DEFAULT_DASHBOARD_PORT}/*，"
            "如用 PORT 环境变量改过端口请以实际地址为准），防止盗用。"
        ) if BAIDU_MAP_AK else "未配置 BAIDU_MAP_AK：真实路网视图与路径规划不可用，界面将显式标注未配置状态。",
    }


@app.post("/api/baidu/route", summary="百度驾车路径规划代理 (真实绕行对比)")
async def baidu_driving_route(payload: BaiduRouteInput):
    """
    Server-side proxy for the Baidu Map driving-direction REST API.
    Returns the real planned distance/duration so the dashboard can compare the
    congested corridor against real alternative routes. Responses are cached briefly
    (per OD pair) to respect the daily quota; failures are reported as failures —
    no synthetic route data is ever produced.
    """
    if not BAIDU_MAP_SERVER_AK:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="未配置 BAIDU_MAP_AK / BAIDU_MAP_SERVER_AK：无法调用百度路径规划。请在 .env 中配置后重启服务。",
        )

    cache_key = f"{payload.origin_lng:.5f},{payload.origin_lat:.5f}->{payload.dest_lng:.5f},{payload.dest_lat:.5f}"

    cached = _baidu_cache_get(cache_key)
    if cached:
        return {**cached, "cached": True}

    now = time.time()
    import httpx

    params = {
        "origin": f"{payload.origin_lat:.6f},{payload.origin_lng:.6f}",
        "destination": f"{payload.dest_lat:.6f},{payload.dest_lng:.6f}",
        "ak": BAIDU_MAP_SERVER_AK,
        "alternatives": 1,  # 返回备选路线，供绕行对比
        "extensions_info": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_BAIDU_DIRECTION_URL, params=params)
            resp.raise_for_status()
            body = resp.json()
    except Exception:
        # Never echo provider exception text: httpx errors may include the full URL,
        # including the server-side Baidu AK query parameter.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="百度路径规划调用失败，请稍后重试。",
        )

    if body.get("status") != 0:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"百度路径规划返回错误 status={body.get('status')}: {body.get('message', '未知错误')}",
        )

    result = body.get("result") or {}
    routes = []
    for idx, r in enumerate(result.get("routes") or []):
        routes.append({
            "index": idx,
            "distance_km": round(r.get("distance", 0) / 1000.0, 2),
            "duration_min": round(r.get("duration", 0) / 60.0, 1),
            "congestion_summary": _baidu_congestion_summary(r),
            "path_lnglat": _route_path_lnglat(r, max_points=240),
        })

    payload_out = {
        "success": True,
        "origin": {"lng": payload.origin_lng, "lat": payload.origin_lat},
        "destination": {"lng": payload.dest_lng, "lat": payload.dest_lat},
        "routes": routes,
        "cached": False,
        "note": "数据来源：百度地图驾车路径规划 API 实时返回，未经任何人工修饰。",
    }
    _baidu_cache_put(cache_key, {**payload_out, "_ts": now})
    return payload_out


def _route_path_lnglat(route: Dict[str, Any], max_points: int = 240) -> list:
    """
    Flatten a Baidu route's per-step `path` strings into a decimated [[lng, lat], ...]
    polyline for map drawing. Coordinates stay in Baidu BD-09 as returned by the API.
    """
    pts: list = []
    for s in route.get("steps") or []:
        raw = s.get("path") or ""
        for pair in raw.split(";"):
            if not pair:
                continue
            parts = pair.split(",")
            try:
                lng, lat = float(parts[0]), float(parts[1])
            except (ValueError, IndexError, TypeError):
                continue
            pts.append([round(lng, 6), round(lat, 6)])
    if len(pts) > max_points:
        step = len(pts) / float(max_points)
        pts = [pts[int(i * step)] for i in range(max_points - 1)] + [pts[-1]]
    return pts


def _baidu_congestion_summary(route: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate per-step congestion annotation from the Baidu route if present."""
    steps = route.get("steps") or []
    congested_m = 0
    total_m = 0
    for s in steps:
        dist = float(s.get("distance", 0) or 0)
        total_m += dist
        # congestion: 0畅通 1缓行 2拥堵 3严重拥堵 (百度路况枚举)
        if int(s.get("congestion", 0) or 0) >= 2:
            congested_m += dist
    ratio = round(congested_m / total_m, 3) if total_m > 0 else None
    return {
        "total_distance_m": total_m,
        "congested_distance_m": congested_m,
        "congested_ratio": ratio,
        "label": (
            "严重拥堵路段" if (ratio or 0) >= 0.5
            else ("拥堵路段" if (ratio or 0) >= 0.25 else "通行状况尚可")
        ) if ratio is not None else None,
    }


@app.post("/api/diagnose", summary="智能体思维链态势诊断与拥堵归因")
def diagnose_traffic(state: Optional[TrafficStateInput] = None):
    """
    Executes situational perception, bottleneck attribution, and Chain-of-Thought (CoT) reasoning.
    """
    input_dict = state.model_dump() if state else TrafficStateInput().model_dump()

    try:
        diagnosis = agent.diagnose_bottleneck(input_dict)
        return {
            "success": True,
            "traffic_state": input_dict,
            "diagnosis": diagnosis
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Agent diagnosis error: {str(e)}"
        )


@app.post("/api/strategies", summary="候选治理预案构想与交通工程工具求解")
async def generate_strategies(payload: Optional[StrategyFormulationInput] = None):
    """
    Computes candidate governance strategies:
    - Baseline: Do-Nothing
    - Strategy A: Webster Local Adaptive Timing
    - Strategy B: TrafficAgent-DSS Spatio-Temporal Coordinated Control
    """
    diag = payload.diagnosis if payload else None
    if diag is not None and not isinstance(diag, dict):
        raise HTTPException(
            status_code=422,
            detail="diagnosis 字段必须为合法的字典对象"
        )
    try:
        if not diag:
            diag = agent.diagnose_bottleneck(TrafficStateInput().model_dump())

        strategies = agent.formulate_candidate_strategies(diag)
        return {
            "success": True,
            "strategies": strategies
        }
    except HTTPException:
        raise
    except (ValueError, TypeError) as ve:
        raise HTTPException(
            status_code=422,
            detail=f"参数校验失败: {str(ve)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Strategy formulation error: {str(e)}"
        )


def _build_physical_rollout_payload(cfg: "RolloutConfigInput", rollout_raw: Dict[str, Any]) -> Dict[str, Any]:
    """Shapes a successful SUMO run into the public rollout response."""
    # Format time-series for frontend charts
    time_steps = rollout_raw.get("time_stamps", list(range(0, cfg.duration + 1, 10)))
    traces = rollout_raw.get("raw_traces", {})
    q_base = traces.get("baseline", {}).get("queues", [])
    q_a = traces.get("strategy_a", {}).get("queues", [])
    q_b = traces.get("strategy_b", {}).get("queues", [])

    # Radar dimension scores are taken verbatim from the evaluator's comparison.
    # They must NOT be defaulted to showcase constants: if a scheme produced no
    # scores, the dashboard has to show a missing state rather than plausible-looking
    # numbers that were never computed.
    RADAR_KEYS = [
        "通行效率 (Delay)",
        "空间治堵 (Queue)",
        "容量释放 (Throughput)",
        "运行平稳 (Reliability)",
        "绿色低碳 (Carbon)",
    ]

    def _radar_from(comp: Optional[dict]):
        scores = (comp or {}).get("radar_scores") or {}
        if not scores:
            return None
        values = []
        for k in RADAR_KEYS:
            v = scores.get(k)
            if v is None:
                return None
            values.append(int(round(float(v))))
        return values

    comp_b = rollout_raw["comparisons"]["strategy_b"]
    comp_a = rollout_raw["comparisons"].get("strategy_a", {})
    radar_a = _radar_from(comp_a)
    radar_b = _radar_from(comp_b)

    radar_data = {
        "dimensions": ["通行效率", "空间治堵", "容量释放", "运行平稳", "绿色低碳"],
        "baseline": [50, 50, 50, 50, 50],
        "strategy_a": radar_a,
        "strategy_b": radar_b
    }

    return {
        "success": True,
        "execution_mode": "physical_sumo_sandbox",
        "simulation_duration": cfg.duration,
        "seed": cfg.seed,
        "incident_window": [cfg.incident_start, cfg.incident_end],
        # Echo the requested scenario labels together with the corridor that was
        # actually simulated. `corridor_choice` / `congestion_type` are descriptive
        # labels: the physical sandbox always runs the bundled corridor and incident
        # profile, so surfacing both makes that explicit instead of leaving the
        # caller to assume the labels selected something.
        "scenario": {
            "requested_corridor_choice": cfg.corridor_choice,
            "requested_congestion_type": cfg.congestion_type,
            "simulated_corridor": "scenarios/corridor.net.xml (J1-J3 bundled corridor)",
            "note": "物理沙盒固定运行内置走廊与事故工况；上述标签仅为展示用途，不改变路网。",
        },
        "kpis": rollout_raw["kpis"],
        "comparisons": rollout_raw["comparisons"],
        # Audit trail: which controls actually reached the simulator, and which
        # detector inputs drove the plan. Promised by CHANGELOG ("说做了 vs 真做了
        # 可对照") but previously dropped here, so no API client could verify it.
        "control_evidence": rollout_raw.get("control_evidence", {}),
        "strategy_inputs": rollout_raw.get("strategy_inputs", {}),
        "time_series": {
            "time_steps": time_steps,
            "queue_baseline": q_base,
            "queue_strategy_a": q_a,
            "queue_strategy_b": q_b
        },
        "radar": radar_data
    }


def _degrade_rollout(cfg: "RolloutConfigInput", physical_error: BaseException) -> Dict[str, Any]:
    """
    Called only when the physical SUMO run itself failed. Tries the real-network
    mesoscopic engine first, then the calibrated dataset, and always records *why*
    plus which engine actually produced the numbers.
    """
    try:
        result = network_api.run_mesoscopic_rollout(
            duration=cfg.duration,
            incident_start=cfg.incident_start,
            incident_end=cfg.incident_end,
            use_rerouting=cfg.use_rerouting,
            use_green_wave=cfg.use_green_wave,
            use_webster=cfg.use_webster,
            seed=cfg.seed,
        )
        result["fallback_reason"] = f"SUMO 不可用，已平滑降级至真实路网中观排队推演引擎: {physical_error}"
        result["degraded"] = True
        return result
    except Exception as meso_error:
        calibrated = get_calibrated_rollout_data(
            duration=cfg.duration,
            incident_start=cfg.incident_start,
            incident_end=cfg.incident_end,
            use_rerouting=cfg.use_rerouting,
            use_green_wave=cfg.use_green_wave,
            use_webster=cfg.use_webster,
            seed=cfg.seed
        )
        calibrated["execution_mode"] = "calibrated_empirical_fallback"
        calibrated["fallback_reason"] = (
            f"SUMO 与中观引擎均不可用，回退至标定数据: {physical_error} / {meso_error}"
        )
        calibrated["degraded"] = True
        calibrated["success"] = True
        return calibrated


def _run_rollout(cfg: "RolloutConfigInput", state_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Runs the What-If rollout for one traffic state.

    `state_dict` is the detector reading that drives the Webster / green-wave / rerouting
    parameters. It is threaded in explicitly so the one-stop /api/decide pipeline can run
    the simulation against the *same* state it diagnosed, instead of silently reverting
    to the hard-coded corridor defaults (which made the brief internally inconsistent).
    """
    state = state_dict or TrafficStateInput().model_dump()
    scenario = _scenario_parameters(cfg.corridor_choice, cfg.congestion_type)

    if not cfg.run_physical_sandbox:
        # Caller explicitly requested the non-physical fast path.
        try:
            result = network_api.run_mesoscopic_rollout(
                duration=cfg.duration,
                incident_start=cfg.incident_start,
                incident_end=cfg.incident_end,
                use_rerouting=cfg.use_rerouting,
                use_green_wave=cfg.use_green_wave,
                use_webster=cfg.use_webster,
                seed=cfg.seed,
                scenario=scenario,
            )
            result["degraded"] = True
            result["fallback_reason"] = "已选非微观仿真模式，运行真实路网中观排队物理推演引擎。"
            result.setdefault("scenario", {}).update({
                "requested_corridor_choice": cfg.corridor_choice,
                "requested_congestion_type": cfg.congestion_type,
                "parameters": scenario,
            })
            return result
        except Exception as e:
            calibrated = get_calibrated_rollout_data(
                duration=cfg.duration,
                incident_start=cfg.incident_start,
                incident_end=cfg.incident_end,
                use_rerouting=cfg.use_rerouting,
                use_green_wave=cfg.use_green_wave,
                use_webster=cfg.use_webster,
                seed=cfg.seed
            )
            calibrated["execution_mode"] = "calibrated_empirical_fast"
            calibrated["fallback_reason"] = f"mesoscopic network unavailable: {e}"
            calibrated["degraded"] = True
            calibrated["success"] = True
            return calibrated

    # Close the diagnosis -> strategy -> control loop: the detector state drives the
    # Webster / green-wave / rerouting parameters that the sandbox then applies.
    diag = agent.diagnose_bottleneck(state)

    try:
        rollout_raw = agent.execute_what_if_rollout(
            duration=cfg.duration,
            incident_start=cfg.incident_start,
            incident_end=cfg.incident_end,
            use_rerouting=cfg.use_rerouting,
            use_green_wave=cfg.use_green_wave,
            use_webster=cfg.use_webster,
            seed=cfg.seed,
            diagnosis=diag
        )
    except Exception as physical_error:
        # Only the simulator invocation justifies degrading. See the comment on the
        # shaping step below for why post-processing errors must NOT land here.
        return _degrade_rollout(cfg, physical_error)

    # The physical run succeeded. Any failure past this point would be a bug in our own
    # response shaping — previously it was caught by the same broad `except` and reported
    # as "SUMO 不可用", which pointed investigations at the simulator while the real
    # error was a formatting defect. Let it surface as a 500 instead.
    return _build_physical_rollout_payload(cfg, rollout_raw)


@app.post("/api/rollout", summary="数字孪生沙盒推演与 What-If 多方案量化评估")
def execute_rollout(config: Optional[RolloutConfigInput] = None):
    """
    Executes What-If simulation rollout across Baseline, Strategy A, and Strategy B.
    Runs headless SUMO TraCI micro-physics or high-fidelity calibrated benchmark data.

    Declared sync so FastAPI dispatches it to a worker thread: three SUMO runs can take
    tens of seconds, and running them on the event loop used to freeze /api/status and
    the map polling for the whole duration.
    """
    cfg = config or RolloutConfigInput()
    return _run_rollout(cfg, None)


@app.post("/api/evaluate/multi-seed", summary="多随机种子蒙特卡洛/批次推演评估")
def evaluate_multi_seed(payload: Optional[MultiSeedEvaluationInput] = None):
    """
    Executes multi-seed evaluation to quantify statistical significance and confidence intervals.
    Supports physical SUMO sandbox runs or empirical calibrated evaluations.
    """
    import numpy as np

    inp = payload or MultiSeedEvaluationInput()
    seeds = inp.seeds if inp.seeds is not None else [42, 101, 2024, 777, 999]
    if not seeds:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="seeds 列表不能为空，必须包含至少一个非负整数随机种子"
        )
    if any(not isinstance(s, int) or s < 0 for s in seeds):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="seeds 列表中的种子必须均为非负整数"
        )

    try:
        diag = agent.diagnose_bottleneck(TrafficStateInput().model_dump())

        fallback_notice = None
        if inp.run_physical_sandbox:
            try:
                results = agent.run_multi_seed_evaluation(
                    seeds=seeds,
                    duration=inp.duration,
                    incident_start=inp.incident_start,
                    incident_end=inp.incident_end,
                    diagnosis=diag,
                )
                results["success"] = True
                results["execution_mode"] = "physical_sumo_sandbox"
                return results
            except Exception as e:
                # Graceful fallback when SUMO binaries or physical sandbox encounter errors
                fallback_notice = f"SUMO Sandbox notice: {str(e)}"

        # Calibrated mode: the empirical dataset is a single deterministic scenario, so it
        # cannot support statistical inference. Only point estimates are returned; SEM,
        # confidence intervals and significance testing require the physical sandbox
        # (run_physical_sandbox=true) where each seed is an independent SUMO run.
        schemes = ["baseline", "strategy_a", "strategy_b"]
        base_data = get_calibrated_rollout_data(
            duration=inp.duration,
            incident_start=inp.incident_start,
            incident_end=inp.incident_end,
        )
        base_kpis = base_data["kpis"]

        metric_keys = [
            "avg_delay_s", "max_queue_m", "avg_speed_kmh",
            "throughput_vph", "delay_variance", "co2_emissions_kg", "fuel_liters"
        ]
        summary = {
            sc: {
                m: ({"mean": round(float(base_kpis[sc][m]), 2)}
                    if base_kpis[sc].get(m) is not None else None)
                for m in metric_keys if m in base_kpis[sc]
            }
            for sc in schemes
        }

        comp_keys = [
            "delay_improvement_pct", "queue_improvement_pct",
            "speed_improvement_pct", "throughput_improvement_pct",
            "variance_improvement_pct", "co2_improvement_pct", "fuel_improvement_pct"
        ]
        comp_b = PerformanceEvaluator.compare_schemes(base_kpis["baseline"], base_kpis["strategy_b"])
        # A metric whose baseline is unavailable comes back as None; surface that as null
        # instead of crashing on float(None) (the calibrated dataset carries no fuel figure).
        b_improvements = {
            ck: ({"mean": round(float(comp_b[ck]), 2)} if comp_b.get(ck) is not None else None)
            for ck in comp_keys
        }

        response_payload = {
            "success": True,
            "execution_mode": "calibrated_fallback_no_sumo" if fallback_notice else "calibrated_empirical_fast",
            "seeds_tested": seeds,
            "sample_size": None,
            "summary_by_scheme": summary,
            "strategy_b_improvements": b_improvements,
            "statistically_significant": None,
            "statistical_notice": (
                "标定经验数据为单一确定性数据集，不支持统计推断：未输出标准误、置信区间与显著性结论。"
                "如需统计评估，请设置 run_physical_sandbox=true 运行物理 SUMO 多种子蒙特卡洛推演。"
            ),
        }
        if fallback_notice:
            response_payload["fallback_reason"] = fallback_notice
        return response_payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Multi-seed evaluation error: {str(e)}"
        )


@app.post("/api/report/export", summary="生成格式化决策简报")
async def export_decision_report(data: Optional[ReportExportInput] = None):
    """
    Generates standardized Markdown decision support briefing for operators and reviewers.
    """
    try:
        default_state = {
            "bottleneck_edge": "J1_J2 (主干线合流段)",
            "queue_m": 165.0,
            "link_length_m": 300.0,
            "speed_kmh": 8.2,
            "occupancy": 0.82,
            "bypass_occupancy": 0.28
        }
        diagnosis = data.diagnosis if data and data.diagnosis else agent.diagnose_bottleneck(default_state)
        strategies = data.strategies if data and data.strategies else agent.formulate_candidate_strategies(diagnosis)
        rollout_data = data.rollout_data if data and data.rollout_data else get_calibrated_rollout_data()

        report_md = agent.generate_decision_report(diagnosis, strategies, rollout_data)

        return {
            "success": True,
            "report_markdown": report_md,
            "filename": "TrafficAgent_Decision_Briefing.md",
            "length": len(report_md)
        }
    except HTTPException:
        raise
    except (ValueError, TypeError) as ve:
        raise HTTPException(
            status_code=422,
            detail=f"参数校验失败: {str(ve)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Report generation error: {str(e)}"
        )


@app.get("/api/report/download", summary="下载 Markdown 格式决策简报 (GET)")
def download_decision_report():
    """
    Directly returns Markdown decision briefing file as a download stream.
    """
    try:
        diag = agent.diagnose_bottleneck(TrafficStateInput().model_dump())
        strat = agent.formulate_candidate_strategies(diag)
        rollout = get_calibrated_rollout_data()
        report_md = agent.generate_decision_report(diag, strat, rollout)

        return Response(
            content=report_md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="TrafficAgent_Decision_Briefing.md"'
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"简报下载流生成失败: {str(e)}"
        )


@app.post("/api/report/download", summary="自定义下载 Markdown 格式决策简报 (POST)")
def download_custom_decision_report(data: Optional[ReportExportInput] = None):
    """
    Returns customized Markdown decision briefing file stream based on current session state.
    """
    try:
        diag = data.diagnosis if data and data.diagnosis else agent.diagnose_bottleneck(
            TrafficStateInput().model_dump()
        )
        strat = data.strategies if data and data.strategies else agent.formulate_candidate_strategies(diag)
        rollout = data.rollout_data if data and data.rollout_data else get_calibrated_rollout_data()
        report_md = agent.generate_decision_report(diag, strat, rollout)

        return Response(
            content=report_md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="TrafficAgent_Decision_Briefing.md"'
            }
        )
    except HTTPException:
        raise
    except (ValueError, TypeError) as ve:
        raise HTTPException(
            status_code=422,
            detail=f"参数校验失败: {str(ve)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"简报下载流生成失败: {str(e)}"
        )


@app.post("/api/decide", summary="一站式端到端协同决策流水线")
def execute_full_decision_pipeline(payload: Optional[DecisionPipelineInput] = None):
    """
    One-stop pipeline: diagnosis -> strategies -> rollout -> decision brief.
    Provides complete DSS result in a single request with graceful degradation.
    """
    try:
        t_state = payload.traffic_state if payload and payload.traffic_state else TrafficStateInput()
        state_dict = t_state.model_dump()
        diagnosis = agent.diagnose_bottleneck(state_dict)
        strategies = agent.formulate_candidate_strategies(diagnosis)

        r_cfg = payload.rollout_config if payload and payload.rollout_config else RolloutConfigInput()
        # Pass the SAME detector state into the rollout. Previously the simulation was
        # driven by the hard-coded 165 m / 0.82 corridor defaults regardless of what the
        # caller sent, so the action plan (from the caller's state) and the simulated KPIs
        # (from the defaults) described two different situations inside one report.
        rollout_res = _run_rollout(r_cfg, state_dict)

        report_md = agent.generate_decision_report(diagnosis, strategies, rollout_res)

        return {
            "success": True,
            "traffic_state": state_dict,
            "diagnosis": diagnosis,
            "strategies": strategies,
            "rollout": rollout_res,
            "report_markdown": report_md,
            "execution_mode": rollout_res.get("execution_mode", "calibrated_empirical_benchmark"),
        }
    except HTTPException:
        raise
    except (ValueError, TypeError) as ve:
        raise HTTPException(
            status_code=422,
            detail=f"流水线参数校验失败: {str(ve)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Decision pipeline error: {str(e)}"
        )


@app.post("/api/optimize/closed-loop", summary="大模型闭环控制策略寻优（LLM 进入决策回路）")
def optimize_closed_loop(payload: Optional[ClosedLoopOptimizationInput] = None):
    """
    Closed-loop control optimisation with the LLM *inside* the decision path.

    Each round the model receives the detector state, the deterministic toolchain
    baseline, and the measured KPI delta of its own previous decision, then returns a
    control parameter set (cycle / arterial green share / diversion ratio / progression
    speed / coordination flag). Every set is schema-validated and clipped into the
    corridor's feasible domain before deployment, and the effect is measured by SUMO —
    never predicted by the model.

    Degradation is explicit: with no usable model the loop does not run and the
    deterministic result is returned together with `decision_mode =
    deterministic_rule_chain` and the reason, rather than a fabricated "optimised" figure.

    Note this endpoint is a synchronous `def`, so FastAPI runs it in a worker thread —
    it never blocks the event loop while SUMO subprocesses are executing.
    """
    try:
        cfg = payload or ClosedLoopOptimizationInput()
        state_dict = (cfg.traffic_state or TrafficStateInput()).model_dump()
        diagnosis = agent.diagnose_bottleneck(state_dict)
        result = agent.optimize_control_policy_closed_loop(
            diagnosis,
            rounds=cfg.rounds,
            duration=cfg.duration,
            incident_start=cfg.incident_start,
            incident_end=cfg.incident_end,
            seed=cfg.seed,
            use_rerouting=cfg.use_rerouting,
            use_webster=cfg.use_webster,
        )
        return {
            "success": True,
            "traffic_state": state_dict,
            "diagnosis": {
                "bottleneck_location": diagnosis.get("bottleneck_location"),
                "severity_level": diagnosis.get("severity_level"),
                "root_causes": diagnosis.get("root_causes", []),
                "reasoning_mode": diagnosis.get("reasoning_mode"),
            },
            **result,
        }
    except HTTPException:
        raise
    except (ValueError, TypeError) as ve:
        raise HTTPException(status_code=422, detail=f"闭环寻优参数校验失败: {str(ve)}")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Closed-loop optimisation error: {str(e)}",
        )


@app.get("/api/baseline-data", summary="获取标定基准推演与全套指标数据集")
def get_baseline_dataset():
    """
    Returns full pre-calibrated baseline datasets for instant page initialization.
    """
    default_state = TrafficStateInput().model_dump()
    diagnosis = agent.diagnose_bottleneck(default_state)
    strategies = agent.formulate_candidate_strategies(diagnosis)
    rollout = get_calibrated_rollout_data()

    return {
        "success": True,
        "traffic_state": default_state,
        "diagnosis": diagnosis,
        "strategies": strategies,
        "rollout": rollout
    }


# Static and Single-Page Application (SPA) Serving
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

FAVICON_PATH = STATIC_DIR / "favicon.svg"


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon():
    """
    Browsers request /favicon.ico unconditionally; it used to 404 on every page load.
    Served with an explicit image/svg+xml type (browsers accept SVG here).
    """
    if FAVICON_PATH.exists():
        return FileResponse(str(FAVICON_PATH), media_type="image/svg+xml")
    return Response(status_code=204)


@app.head("/", include_in_schema=False)
@app.get("/", response_class=HTMLResponse, summary="决策支持大屏前端入口")
async def serve_dashboard():
    """
    Serves the modern TrafficAgent-DSS interactive dashboard single-page web app.
    """
    if INDEX_HTML_PATH.exists():
        return FileResponse(str(INDEX_HTML_PATH), media_type="text/html")
    return HTMLResponse(
        content="""
        <html>
            <head><title>TrafficAgent-DSS</title></head>
            <body style="font-family:sans-serif;padding:40px;background:#0f172a;color:#f8fafc;">
                <h1>🚦 TrafficAgent-DSS Web API Running</h1>
                <p>Modern decoupled backend is online. Static UI file is being initialized.</p>
                <p><a href="/docs" style="color:#38bdf8;">View Swagger API Documentation (/docs)</a></p>
            </body>
        </html>
        """
    )


if __name__ == "__main__":
    import uvicorn
    # Clean on-demand launcher
    port = int(os.environ.get("PORT", 8501))
    print(f"🚀 Starting TrafficAgent-DSS Web Dashboard at http://localhost:{port}")
    uvicorn.run("src.web.app:app", host="0.0.0.0", port=port, reload=True)
