"""
TrafficAgent-DSS 消融实验 (Ablation Study)
==========================================

以真实 SUMO 物理沙盒逐项开/关治理组件，量化每个组件对网络效能的边际贡献，
为《作品说明书》与答辩提供"技术深度"证据链：

    baseline            : 无干预 (Do-Nothing)
    webster_only        : 仅 Webster 自适应配时
    webster+greenwave   : 配时 + 干线绿波协调
    webster+greenwave+vms: 配时 + 绿波 + VMS 诱导分流 (完整方案 B)
    vms_only            : 仅 VMS 诱导分流 (可选对照)

每个配置 × 每个随机种子独立运行一次完整 SUMO 推演，输出均值、标准差与
95% 置信区间（t 分布，小样本严谨口径）。所有数字都来自实测仿真，
不存在任何合成数据。

用法（仓库根目录）：
    .venv/bin/python experiments/ablation.py --seeds 42 101 2024 --duration 600
    .venv/bin/python experiments/ablation.py --out experiments/ablation_result.md
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

from src.agents.traffic_agent import TrafficDecisionAgent  # noqa: E402

CONFIGS = [
    ("baseline", "无干预基线 (Do-Nothing)", dict(use_webster=False, use_green_wave=False, use_rerouting=False)),
    ("webster_only", "方案A：仅 Webster 配时", dict(use_webster=True, use_green_wave=False, use_rerouting=False)),
    ("webster_gw", "Webster + 干线绿波", dict(use_webster=True, use_green_wave=True, use_rerouting=False)),
    ("full_b", "完整方案B：Webster + 绿波 + VMS", dict(use_webster=True, use_green_wave=True, use_rerouting=True)),
    ("vms_only", "对照：仅 VMS 分流", dict(use_webster=False, use_green_wave=False, use_rerouting=True)),
]

METRICS = ["avg_delay_s", "max_queue_m", "throughput_vph", "co2_emissions_kg"]

# execute_what_if_rollout 每次调用固定返回 baseline/strategy_a/strategy_b 三套实测 KPI：
# strategy_a 的控制只随 use_webster 变化；strategy_b 随三个开关变化。
# 因此各消融配置从对应方案键取数（其余方案的运行结果仅作为同种子对照保留）。
KPI_KEY = {"baseline": "baseline", "webster_only": "strategy_a"}


def ci95_t(values):
    """小样本 95% 置信区间（t 分布），n>=2 时才有效。"""
    n = len(values)
    if n < 2:
        return None, None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    sem = math.sqrt(var / n)
    # t(0.975, df=n-1) 的常见值；n<=10 时覆盖绝大多数实验规模
    t_table = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
               6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}
    t = t_table.get(n - 1, 1.96)
    return mean - t * sem, mean + t * sem


def run(seeds, duration, incident_start, incident_end):
    agent = TrafficDecisionAgent()
    default_state = {
        "bottleneck_edge": "J1_J2 (主干线合流段)",
        "queue_m": 165.0,
        "link_length_m": 300.0,
        "speed_kmh": 8.2,
        "occupancy": 0.82,
        "bypass_occupancy": 0.28,
    }
    diag = agent.diagnose_bottleneck(default_state)

    results = {}
    for key, label, switches in CONFIGS:
        results[key] = {"label": label, "runs": []}
        for seed in seeds:
            t0 = time.time()
            out = agent.execute_what_if_rollout(
                duration=duration,
                incident_start=incident_start,
                incident_end=incident_end,
                seed=seed,
                diagnosis=diag,
                **switches,
            )
            if out.get("execution_mode") != "physical_sumo_sandbox":
                raise RuntimeError(
                    f"配置 {key} seed={seed} 未进入物理沙盒（{out.get('execution_mode')}），"
                    "消融实验必须基于真实 SUMO 实测，已中止以避免输出无实测依据的结论。"
                )
            results[key]["runs"].append({
                "seed": seed,
                "kpis": out["kpis"][KPI_KEY.get(key, "strategy_b")],
                "elapsed_s": round(time.time() - t0, 1),
            })
            print(f"[{key}] seed={seed} done in {results[key]['runs'][-1]['elapsed_s']}s", flush=True)
    return results


def summarize(results):
    summary = {}
    for key, block in results.items():
        summary[key] = {"label": block["label"], "metrics": {}}
        for m in METRICS:
            vals = [r["kpis"][m] for r in block["runs"]]
            n = len(vals)
            mean = sum(vals) / n
            std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
            lo, hi = ci95_t(vals)
            summary[key]["metrics"][m] = {
                "mean": round(mean, 2), "std": round(std, 2),
                "ci95": [None if lo is None else round(lo, 2),
                         None if hi is None else round(hi, 2)],
                "n": n,
            }
    return summary


def to_markdown(summary, seeds, duration, incident_start, incident_end):
    lines = [
        "# TrafficAgent-DSS 消融实验报告 (真实 SUMO 实测)",
        "",
        f"- 随机种子: {seeds}  |  推演时长: {duration}s  |  事故窗口: {incident_start}-{incident_end}s",
        "- 指标口径: SUMO 微观仿真实测（延误=tripinfo timeLoss 同口径，排队=halting×7.5m）",
        "- 置信区间: t 分布 95% CI",
        "",
    ]
    header = "| 配置 | " + " | ".join(METRICS) + " |"
    sep = "|---" * (len(METRICS) + 1) + "|"
    lines += [header, sep]
    for key in [c[0] for c in CONFIGS]:
        block = summary.get(key)
        if not block:
            continue
        cells = []
        for m in METRICS:
            st = block["metrics"][m]
            ci = st["ci95"]
            ci_txt = f" [{ci[0]}, {ci[1]}]" if ci[0] is not None else ""
            cells.append(f"{st['mean']}±{st['std']}{ci_txt}")
        lines.append(f"| {block['label']} | " + " | ".join(cells) + " |")

    base = summary.get("baseline", {}).get("metrics", {})
    full = summary.get("full_b", {}).get("metrics", {})
    if base and full:
        lines += ["", "## 完整方案B 相对基线的边际改善", ""]
        for m in METRICS:
            b, f = base.get(m, {}).get("mean"), full.get(m, {}).get("mean")
            if b in (None, 0) or f is None:
                continue
            pct = (b - f) / b * 100.0
            direction = "降低" if pct >= 0 else "上升"
            lines.append(f"- {m}: {direction} {abs(pct):.1f}% ({b} → {f})")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="TrafficAgent-DSS 消融实验")
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 101, 2024])
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--incident-start", type=int, default=150)
    parser.add_argument("--incident-end", type=int, default=420)
    parser.add_argument("--out", type=str, default=None, help="输出 markdown 路径")
    args = parser.parse_args()

    raw = run(args.seeds, args.duration, args.incident_start, args.incident_end)
    summary = summarize(raw)

    out_md = to_markdown(summary, args.seeds, args.duration, args.incident_start, args.incident_end)
    print("\n" + out_md)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else PROJ_ROOT / "experiments" / f"ablation_result_{stamp}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_md, encoding="utf-8")
    (out_path.parent / f"ablation_raw_{stamp}.json").write_text(
        json.dumps({"summary": summary, "raw": raw}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    print(f"[ablation] 结果已写入 {out_path}", flush=True)


if __name__ == "__main__":
    main()
