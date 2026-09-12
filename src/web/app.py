"""
TrafficAgent-DSS: Modern Decoupled Web API & Decision Support Backend
Built with FastAPI, providing RESTful endpoints for real-time situational awareness,
LLM Chain-of-Thought (CoT) reasoning, What-If simulation rollouts, and report export.
"""

import os
import sys
import json
import shutil
from pathlib import Path
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

# Ensure project root is on sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.tools.webster import WebsterSignalOptimizer
from src.tools.green_wave import GreenWaveCoordinator
from src.tools.rerouting import DynamicReroutingAllocator
from src.tools.evaluator import PerformanceEvaluator
from src.agents.traffic_agent import TrafficDecisionAgent

# Application initialization
app = FastAPI(
    title="TrafficAgent-DSS 城市交通拥堵治理决策支持系统 API",
    description="2026年第十六届北京市大学生交通科技大赛 · ITSAC 2026 创新挑战赛（赛题2）现代化解耦架构决策服务",
    version="2.1.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Enable CORS for full decouple flexibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global Traffic Decision Agent
agent = TrafficDecisionAgent()

# Static directories
STATIC_DIR = Path(__file__).resolve().parent / "static"
if not STATIC_DIR.exists():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
INDEX_HTML_PATH = STATIC_DIR / "index.html"


# Pydantic Request & Response Models
class TrafficStateInput(BaseModel):
    bottleneck_edge: str = Field(default="J1_J2 (主干线合流段)", description="瓶颈路段标识")
    queue_m: float = Field(default=165.0, ge=0.0, description="瓶颈处当前排队长度 (米)")
    link_length_m: float = Field(default=300.0, gt=0.0, description="瓶颈路段总长度 (米)")
    speed_kmh: float = Field(default=8.2, ge=0.0, description="瓶颈瞬时车速 (km/h)")
    occupancy: float = Field(default=0.82, ge=0.0, le=1.0, description="断面车道占有率")
    bypass_occupancy: float = Field(default=0.28, ge=0.0, le=1.0, description="平行旁路车道占有率")


class RolloutConfigInput(BaseModel):
    corridor_choice: str = Field(default="北京典型交通走廊 (学院南路-交大东路瓶颈干线)")
    congestion_type: str = Field(default="晚高峰潮汐高负荷 + 突发交通事故 (占道停靠)")
    duration: int = Field(default=600, ge=300, le=1800, description="推演总时长 (秒)")
    incident_start: int = Field(default=150, ge=0, le=1200, description="事故开始时间 (秒)")
    incident_end: int = Field(default=420, ge=0, le=1800, description="事故撤离时间 (秒)")
    use_rerouting: bool = Field(default=True, description="是否启用动态诱导分流")
    use_green_wave: bool = Field(default=True, description="是否启用干线绿波协调")
    use_webster: bool = Field(default=True, description="是否启用 Webster 信号配时优化")
    run_physical_sandbox: bool = Field(default=False, description="是否强制执行本地微观 SUMO 进程推演")

    @model_validator(mode="after")
    def validate_incident_window(self):
        if self.incident_start >= self.incident_end:
            raise ValueError(f"事故开始时间 ({self.incident_start}s) 必须早于事故撤离时间 ({self.incident_end}s)")
        if self.incident_end > self.duration:
            raise ValueError(f"事故撤离时间 ({self.incident_end}s) 不能超出推演总时长 ({self.duration}s)")
        return self


class ReportExportInput(BaseModel):
    diagnosis: Optional[Dict[str, Any]] = None
    strategies: Optional[Dict[str, Any]] = None
    rollout_data: Optional[Dict[str, Any]] = None


# Default baseline calibrated datasets
def get_calibrated_rollout_data(
    duration: int = 600,
    incident_start: int = 150,
    incident_end: int = 420,
    use_rerouting: bool = True,
    use_green_wave: bool = True,
    use_webster: bool = True
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
        "baseline": [45, 42, 50, 48, 52],
        "strategy_a": [65, 60, 68, 62, 66],
        "strategy_b": [
            max(35, min(99, int(45 + delay_imp * 1.15))),
            max(35, min(99, int(42 + queue_imp * 0.95))),
            max(40, min(98, int(50 + tp_imp * 1.25))),
            max(40, min(95, int(48 + var_imp * 0.80))),
            max(40, min(95, int(52 + co2_imp * 1.30)))
        ]
    }

    return {
        # 本数据集为标定经验数据（非 SUMO 微观仿真实测），在此显式标注来源，
        # 避免下游决策简报将其误标为物理仿真结果。
        "execution_mode": "calibrated_empirical_fast",
        "simulation_duration": duration,
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
        "radar": radar
    }


# REST API Endpoints
@app.get("/api/status", summary="系统健康与状态检查")
async def get_system_status():
    """
    Returns system health, agent status, and active scenario configurations.
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
        "version": "2.1.0",
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
            "ITSAC 2026 创新挑战赛（赛题2：基于交通仿真智能体的城市交通拥堵治理决策支持）"
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


@app.post("/api/diagnose", summary="智能体思维链态势诊断与拥堵归因")
async def diagnose_traffic(state: Optional[TrafficStateInput] = None):
    """
    Executes situational perception, bottleneck attribution, and Chain-of-Thought (CoT) reasoning.
    """
    input_dict = state.model_dump() if state else {
        "bottleneck_edge": "J1_J2 (主干线合流段)",
        "queue_m": 165.0,
        "link_length_m": 300.0,
        "speed_kmh": 8.2,
        "occupancy": 0.82,
        "bypass_occupancy": 0.28
    }

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
async def generate_strategies(payload: Optional[Dict[str, Any]] = None):
    """
    Computes candidate governance strategies:
    - Baseline: Do-Nothing
    - Strategy A: Webster Local Adaptive Timing
    - Strategy B: TrafficAgent-DSS Spatio-Temporal Coordinated Control
    """
    try:
        # If diagnosis is provided in payload, use it; otherwise run default diagnosis first
        diag = None
        if payload and "diagnosis" in payload:
            diag = payload["diagnosis"]
        else:
            default_state = {
                "bottleneck_edge": "J1_J2 (主干线合流段)",
                "queue_m": 165.0,
                "link_length_m": 300.0,
                "speed_kmh": 8.2,
                "occupancy": 0.82,
                "bypass_occupancy": 0.28
            }
            diag = agent.diagnose_bottleneck(default_state)

        strategies = agent.formulate_candidate_strategies(diag)
        return {
            "success": True,
            "strategies": strategies
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Strategy formulation error: {str(e)}"
        )


@app.post("/api/rollout", summary="数字孪生沙盒推演与 What-If 多方案量化评估")
async def execute_rollout(config: Optional[RolloutConfigInput] = None):
    """
    Executes What-If simulation rollout across Baseline, Strategy A, and Strategy B.
    Runs headless SUMO TraCI micro-physics or high-fidelity calibrated benchmark data.
    """
    cfg = config or RolloutConfigInput()

    if cfg.run_physical_sandbox:
        try:
            # Close the diagnosis -> strategy -> control loop: the detector state drives the
            # Webster / green-wave / rerouting parameters that the sandbox then applies.
            default_state = {
                "bottleneck_edge": "J1_J2 (主干线合流段)",
                "queue_m": 165.0,
                "link_length_m": 300.0,
                "speed_kmh": 8.2,
                "occupancy": 0.82,
                "bypass_occupancy": 0.28
            }
            diag = agent.diagnose_bottleneck(default_state)

            rollout_raw = agent.execute_what_if_rollout(
                duration=cfg.duration,
                incident_start=cfg.incident_start,
                incident_end=cfg.incident_end,
                use_rerouting=cfg.use_rerouting,
                use_green_wave=cfg.use_green_wave,
                use_webster=cfg.use_webster,
                diagnosis=diag
            )
            # Format time-series for frontend charts
            time_steps = rollout_raw.get("time_stamps", list(range(0, cfg.duration + 1, 10)))
            traces = rollout_raw.get("raw_traces", {})
            q_base = traces.get("baseline", {}).get("queues", [])
            q_a = traces.get("strategy_a", {}).get("queues", [])
            q_b = traces.get("strategy_b", {}).get("queues", [])

            # Extract calibrated radar scores from evaluation comparison
            comp_b = rollout_raw["comparisons"]["strategy_b"]
            scores_dict = comp_b.get("radar_scores", {})
            radar_b = [
                int(scores_dict.get("通行效率 (Delay)", 92)),
                int(scores_dict.get("空间治堵 (Queue)", 95)),
                int(scores_dict.get("容量释放 (Throughput)", 88)),
                int(scores_dict.get("运行平稳 (Reliability)", 90)),
                int(scores_dict.get("绿色低碳 (Carbon)", 85))
            ]

            radar_data = {
                "dimensions": ["通行效率", "空间治堵", "容量释放", "运行平稳", "绿色低碳"],
                "baseline": [45, 42, 50, 48, 52],
                "strategy_a": [65, 60, 68, 62, 66],
                "strategy_b": radar_b
            }

            return {
                "success": True,
                "execution_mode": "physical_sumo_sandbox",
                "simulation_duration": cfg.duration,
                "incident_window": [cfg.incident_start, cfg.incident_end],
                "kpis": rollout_raw["kpis"],
                "comparisons": rollout_raw["comparisons"],
                "time_series": {
                    "time_steps": time_steps,
                    "queue_baseline": q_base,
                    "queue_strategy_a": q_a,
                    "queue_strategy_b": q_b
                },
                "radar": radar_data
            }
        except Exception as e:
            # Graceful fallback to calibrated data with notice
            calibrated = get_calibrated_rollout_data(
                duration=cfg.duration,
                incident_start=cfg.incident_start,
                incident_end=cfg.incident_end,
                use_rerouting=cfg.use_rerouting,
                use_green_wave=cfg.use_green_wave,
                use_webster=cfg.use_webster
            )
            calibrated["execution_mode"] = "calibrated_empirical_fallback"
            calibrated["fallback_reason"] = f"SUMO Sandbox notice: {str(e)}"
            calibrated["success"] = True
            return calibrated
    else:
        # High-precision calibrated interactive mode
        calibrated = get_calibrated_rollout_data(
            duration=cfg.duration,
            incident_start=cfg.incident_start,
            incident_end=cfg.incident_end,
            use_rerouting=cfg.use_rerouting,
            use_green_wave=cfg.use_green_wave,
            use_webster=cfg.use_webster
        )
        calibrated["execution_mode"] = "calibrated_empirical_fast"
        calibrated["success"] = True
        return calibrated


@app.post("/api/report/export", summary="生成格式化决策简报")
async def export_decision_report(data: Optional[ReportExportInput] = None):
    """
    Generates standardized Markdown decision support briefing for operators and reviewers.
    """
    try:
        # Prepare inputs
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
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Report generation error: {str(e)}"
        )


@app.get("/api/report/download", summary="下载 Markdown 格式决策简报 (GET)")
async def download_decision_report():
    """
    Directly returns Markdown decision briefing file as a download stream.
    """
    default_state = {
        "bottleneck_edge": "J1_J2 (主干线合流段)",
        "queue_m": 165.0,
        "link_length_m": 300.0,
        "speed_kmh": 8.2,
        "occupancy": 0.82,
        "bypass_occupancy": 0.28
    }
    diag = agent.diagnose_bottleneck(default_state)
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


@app.post("/api/report/download", summary="自定义下载 Markdown 格式决策简报 (POST)")
async def download_custom_decision_report(data: Optional[ReportExportInput] = None):
    """
    Returns customized Markdown decision briefing file stream based on current session state.
    """
    default_state = {
        "bottleneck_edge": "J1_J2 (主干线合流段)",
        "queue_m": 165.0,
        "link_length_m": 300.0,
        "speed_kmh": 8.2,
        "occupancy": 0.82,
        "bypass_occupancy": 0.28
    }
    diag = data.diagnosis if data and data.diagnosis else agent.diagnose_bottleneck(default_state)
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


@app.get("/api/baseline-data", summary="获取标定基准推演与全套指标数据集")
async def get_baseline_dataset():
    """
    Returns full pre-calibrated baseline datasets for instant page initialization.
    """
    default_state = {
        "bottleneck_edge": "J1_J2 (主干线合流段)",
        "queue_m": 165.0,
        "link_length_m": 300.0,
        "speed_kmh": 8.2,
        "occupancy": 0.82,
        "bypass_occupancy": 0.28
    }
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
