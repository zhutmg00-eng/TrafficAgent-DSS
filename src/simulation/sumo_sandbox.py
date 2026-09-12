"""
TrafficAgent-DSS: SUMO Simulation Sandbox Controller
Manages headless SUMO execution, TraCI interaction, incident injection, and What-If evaluation.
"""

import os
import sys
import time
import shutil
import zlib
import uuid
from pathlib import Path
from typing import Dict, List, Any, Optional
import numpy as np


def _stable_bucket(text: str) -> int:
    """
    Deterministic hash for reproducible simulation.

    The built-in ``hash()`` is salted per process (PYTHONHASHSEED), so using it for
    vehicle-diversion decisions made every run produce different traffic assignment.
    CRC32 is stable across processes and interpreters, guaranteeing that the same
    scenario yields the same result every time.
    """
    return zlib.crc32(text.encode("utf-8"))

# Ensure SUMO tools are importable
def setup_sumo_env():
    py_dir = Path(sys.executable).parent
    sumo_data_bin = py_dir / "Lib" / "site-packages" / "sumo_data" / "bin"
    scripts_dir = py_dir / "Scripts"
    sumo_data_home = py_dir / "Lib" / "site-packages" / "sumo_data"

    paths_to_add = []
    if sumo_data_bin.exists():
        paths_to_add.append(str(sumo_data_bin))
    if scripts_dir.exists():
        paths_to_add.append(str(scripts_dir))

    current_path = os.environ.get("PATH", "")
    for p in paths_to_add:
        if p not in current_path:
            current_path = p + os.pathsep + current_path
    os.environ["PATH"] = current_path

    if sumo_data_home.exists() and not os.environ.get("SUMO_HOME"):
        os.environ["SUMO_HOME"] = str(sumo_data_home)


setup_sumo_env()

try:
    import traci
    import sumolib
except ImportError:
    traci = None
    sumolib = None

# TraCI classes needed to deploy a complete signal program (Logic/Phase).
# Import paths differ across SUMO releases, so probe the known locations.
_TLSLogic = None
_TLSPhase = None
if traci is not None:
    try:
        from traci._trafficlight import Logic as _TLSLogic  # SUMO >= 1.20 layout
    except ImportError:
        try:
            from traci.trafficlight import Logic as _TLSLogic  # legacy layout
        except ImportError:
            _TLSLogic = None
    try:
        from sumolib.net import Phase as _TLSPhase
    except ImportError:
        _TLSPhase = None


def _compute_phase_alignment(
    cycle_length: float,
    phase_durations: List[float],
    first_green_start: float,
) -> "tuple[int, float]":
    """
    Fast-forwards a cycle-based signal program so that its arterial green (phase 0)
    is due to *start* exactly ``first_green_start`` seconds after the simulation begins.

    This is how a green-wave offset is realised without TraCI ``offset`` support:
    at t=0 the program is moved to the point of its cycle it would occupy had it been
    running with that offset all along.

    Example: C=45, durations=[24, 4, 13, 4], first_green_start=21.6
      -> the program must sit at 45-21.6 = 23.4 s of its cycle at t=0,
         i.e. inside phase 0 with 24-23.4 = 0.6 s of green left to run;
         the next arterial green starts 0.6+4+13+4 = 21.6 s later.

    Returns (phase_index, remaining_seconds_of_that_phase).
    """
    if cycle_length <= 0 or not phase_durations:
        return 0, 0.0
    pos = (cycle_length - (first_green_start % cycle_length)) % cycle_length
    acc = 0.0
    for idx, dur in enumerate(phase_durations):
        if pos < acc + dur:
            rem = acc + dur - pos
            if rem < 0.01:
                next_idx = (idx + 1) % len(phase_durations)
                return next_idx, float(phase_durations[next_idx])
            return idx, round(rem, 2)
        acc += dur
    # Floating-point edge (pos == cycle end): restart from phase 0.
    return 0, float(phase_durations[0])


def _deploy_signal_program(
    conn,
    tl_ids,
    tls_list,
    signal_program: Dict[str, Any],
) -> "tuple[int, List[Dict[str, Any]], List[str]]":
    """
    Deploys the arterial signal program exactly ONCE, before the simulation steps.

    * The program replaces the network default, so the control the strategy claims is the
      control that actually runs; the phase states (link semantics) are taken verbatim
      from the network so connections stay valid.
    * ``type`` selects the controller: ``"actuated"`` keeps the adaptive min/max-green
      behaviour (required for the incident scenario, where demand shifts sharply between
      approaches) while ``"static"`` runs the fixed Webster plan.
    * The green-wave offset is realised by phase alignment (see
      :func:`_compute_phase_alignment`) — a one-shot fast-forward, not a runtime hack.

    Returns (deployed_count, evidence_rows, error_messages).
    """
    deployed = 0
    evidence: List[Dict[str, Any]] = []
    errors: List[str] = []

    if _TLSLogic is None or _TLSPhase is None:
        return 0, evidence, ["TraCI Logic/Phase classes unavailable in this environment"]

    green_main = float(signal_program.get("green_main") or 0.0)
    green_cross = float(signal_program.get("green_cross") or 0.0)
    yellow = float(signal_program.get("yellow") or 4.0)
    first_green_starts = [float(s) for s in (signal_program.get("first_green_start") or [])]
    prog_type = str(signal_program.get("type") or "static").lower()
    logic_type = 3 if prog_type == "actuated" else 0  # 0=static, 3=actuated

    # Actuated controllers bound each green by min/max duration; absent bounds mean the
    # phase duration is also its min and max (i.e. a fixed green).
    def _bound(key: str, fallback: float) -> float:
        value = signal_program.get(key)
        return float(value) if value else float(fallback)

    program_id = "dss_signal"
    cycle = green_main + 2.0 * yellow + green_cross

    for idx, tl_id in enumerate(tl_ids):
        if tl_id not in tls_list:
            errors.append(f"{tl_id}: not present in the network")
            continue
        try:
            base_logics = conn.trafficlight.getAllProgramLogics(tl_id)
            if not base_logics or len(base_logics[0].phases) < 4:
                errors.append(f"{tl_id}: signal program has no 4-phase structure to map onto")
                continue
            base = base_logics[0]

            # Keep the network's own link-state strings; replace only the durations.
            new_phases = [
                _TLSPhase(
                    green_main,
                    base.phases[0].state,  # arterial green
                    _bound("min_green_main", green_main),
                    _bound("max_green_main", green_main),
                ),
                _TLSPhase(yellow, base.phases[1].state),  # yellow
                _TLSPhase(
                    green_cross,
                    base.phases[2].state,  # cross-street green
                    _bound("min_green_cross", green_cross),
                    _bound("max_green_cross", green_cross),
                ),
                _TLSPhase(yellow, base.phases[3].state),  # yellow
            ]

            logic = _TLSLogic(program_id, logic_type, 0, new_phases)
            conn.trafficlight.setProgramLogic(tl_id, logic)
            conn.trafficlight.setProgram(tl_id, program_id)

            t_start = first_green_starts[idx] if idx < len(first_green_starts) else 0.0
            phase_idx, remaining = _compute_phase_alignment(
                cycle, [p.duration for p in new_phases], t_start
            )
            conn.trafficlight.setPhase(tl_id, phase_idx)
            conn.trafficlight.setPhaseDuration(tl_id, remaining)

            deployed += 1
            evidence.append({
                "tls": tl_id,
                "program_id": program_id,
                "program_type": prog_type,
                "cycle_length": round(cycle, 1),
                "green_main": round(green_main, 1),
                "green_cross": round(green_cross, 1),
                "yellow": round(yellow, 1),
                "min_green_main": round(_bound("min_green_main", green_main), 1),
                "max_green_main": round(_bound("max_green_main", green_main), 1),
                "min_green_cross": round(_bound("min_green_cross", green_cross), 1),
                "max_green_cross": round(_bound("max_green_cross", green_cross), 1),
                "arterial_green_first_start_s": round(t_start, 1),
                "aligned_phase_index": phase_idx,
                "aligned_phase_remaining_s": round(remaining, 1),
            })
        except Exception as exc:  # pragma: no cover - reported through evidence
            errors.append(f"{tl_id}: {type(exc).__name__}: {exc}")

    return deployed, evidence, errors


class SumoSimulationSandbox:
    """
    High-performance TraCI sandbox for traffic simulation and What-If evaluation.
    """

    def __init__(self, scenario_dir: Optional[str] = None):
        if scenario_dir is None:
            self.scenario_dir = Path(__file__).resolve().parent.parent.parent / "scenarios"
        else:
            self.scenario_dir = Path(scenario_dir)

        self.net_file = self.scenario_dir / "corridor.net.xml"
        self.rou_file = self.scenario_dir / "corridor.rou.xml"
        self.cfg_file = self.scenario_dir / "corridor.sumocfg"

        self.sumo_bin = self._find_sumo()
        self.port_counter = 8813

    def _find_sumo(self) -> str:
        """Locates sumo executable."""
        sumo_home = os.environ.get("SUMO_HOME")
        if sumo_home:
            cand = Path(sumo_home) / "bin" / "sumo.exe"
            if cand.exists():
                return str(cand)

        which_sumo = shutil.which("sumo")
        if which_sumo:
            return which_sumo

        py_dir = Path(sys.executable).parent
        candidates = [
            py_dir / "Scripts" / "sumo.exe",
            py_dir / "Lib" / "site-packages" / "eclipse_sumo" / "bin" / "sumo.exe",
            py_dir / "site-packages" / "eclipse_sumo" / "bin" / "sumo.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)

        return "sumo"

    def run_simulation(
        self,
        scheme: str = "baseline",
        duration: int = 600,
        incident_start: int = 150,
        incident_end: int = 420,
        control_params: Optional[Dict[str, Any]] = None,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Runs a complete headless simulation run.

        Schemes:
          - 'baseline': Fixed-time signal control, no rerouting, no intervention.
          - 'webster': Adaptive signal timing based on Webster's method.
          - 'agent_dss': Coordinated Agent strategy (Webster + Arterial Green Wave + Dynamic VMS Rerouting).

        Args:
          seed: Optional random seed for reproducible multi-seed stochastic evaluation.

        Returns:
          Dict containing time-series traces and aggregate performance metrics.
        """
        if traci is None:
            raise RuntimeError("TraCI is not installed or importable.")

        if control_params is None:
            control_params = {}

        reroute_ratio = float(
            control_params.get("reroute_ratio", 0.25 if scheme == "agent_dss" else 0.0)
        )
        reroute_ratio = max(0.0, min(1.0, reroute_ratio))
        webster_on = bool(control_params.get("webster", scheme in ("webster", "agent_dss")))
        green_wave_on = bool(control_params.get("green_wave", scheme == "agent_dss"))

        # ---- VMS diversion setup ------------------------------------------------- #
        # Vehicles may only be rerouted while they are still on the shared upstream
        # approach edge. `entry_J1` and `entry_div` both branch off node `entry_W`, so a
        # vehicle that has already entered `entry_J1` has physically passed the fork and
        # can no longer be sent onto the bypass.
        approach_edge = "approach_W"
        bypass_route_edges = ["approach_W", "entry_div", "div_byp", "byp_mer", "mer_exit"]
        reroute_threshold = max(0, min(100, int(round(reroute_ratio * 100))))
        reroute_errors: List[str] = []
        diversion_decided: set = set()

        # Arterial signal program (Webster timing + optional green-wave alignment),
        # deployed ONCE before the stepping loop starts.
        #
        # NOTE on the previous failure mode: an earlier implementation mutated the
        # duration of a single phase from inside the stepping loop (`setPhaseDuration`
        # every 30 s). For an `actuated` logic that repeatedly resets the phase timer
        # and corrupts the right-of-way — which is why the Webster-only strategy used
        # to perform *worse* than the fixed-time baseline. The program is now deployed
        # as a complete static plan, exactly once.
        signal_program = control_params.get("signal_program") or {}
        sp_green_main = float(signal_program.get("green_main") or 0.0)
        sp_green_cross = float(signal_program.get("green_cross") or 0.0)
        sp_yellow = float(signal_program.get("yellow") or 4.0)
        sp_first_green_starts = [float(s) for s in (signal_program.get("first_green_start") or [])]

        # Control-actuation accounting: proves which controls actually reached the simulator.
        signal_commands = 0
        reroute_commands = 0

        # Check SUMO executable existence
        resolved_bin = shutil.which(self.sumo_bin) or (self.sumo_bin if Path(self.sumo_bin).exists() else None)
        if not resolved_bin:
            raise FileNotFoundError(f"SUMO executable not found: '{self.sumo_bin}'. Please verify SUMO installation or SUMO_HOME.")

        # Generate unique connection label to prevent collisions under concurrency
        self.port_counter += 1
        label = f"sim_{scheme}_{os.getpid()}_{self.port_counter}_{uuid.uuid4().hex[:8]}"

        cmd = [
            self.sumo_bin,
            "-c", str(self.cfg_file),
            "--no-step-log", "true",
            "--time-to-teleport", "-1",
            "--collision.action", "none",
            "--waiting-time-memory", "1000",
        ]
        safe_seed = None
        if seed is not None:
            try:
                safe_seed = int(seed)
                if safe_seed < 0:
                    raise ValueError(f"seed must be non-negative, got {seed}")
            except (ValueError, TypeError) as err:
                raise ValueError(f"Invalid random seed: {seed} ({err})")
            cmd.extend(["--seed", str(safe_seed)])

        conn = None
        started = False

        # Time-series collection
        time_stamps = []
        bottleneck_queues = []
        bottleneck_speeds = []
        network_delays = []
        completed_vehicles = 0
        total_co2 = 0.0
        total_fuel = 0.0

        incident_injected = False
        incident_cleared = False
        # Speed limits captured at incident-injection time, so the clearance step can restore
        # the road exactly instead of forcing every scenario back to a hard-coded 60 km/h.
        original_lane_speeds: Dict[str, float] = {}

        try:
            try:
                traci.start(cmd, label=label)
                started = True
                conn = traci.getConnection(label)
            except FileNotFoundError:
                raise
            except Exception as start_err:
                raise RuntimeError(f"Failed to start SUMO TraCI process: {start_err}") from start_err

            tls_list = conn.trafficlight.getIDList()
            all_edges = conn.edge.getIDList()
            bottleneck_edge = "J1_J2"
            incident_lanes = (f"{bottleneck_edge}_0", f"{bottleneck_edge}_1")

            # ---- One-shot signal program deployment (before the first step) -------- #
            # The Webster-based plan replaces the network default; when the green wave is
            # active each junction's cycle is fast-forwarded so the arterial green starts
            # at the coordinated offset. Nothing modifies the signals from inside the loop.
            sp_deployed = 0
            sp_evidence: List[Dict[str, Any]] = []
            sp_errors: List[str] = []
            if webster_on and sp_green_main > 0 and sp_green_cross > 0:
                sp_deployed, sp_evidence, sp_errors = _deploy_signal_program(
                    conn,
                    ("J1", "J2", "J3"),
                    tls_list,
                    signal_program,
                )
                signal_commands = sp_deployed

            for step in range(duration):
                # 1. Inject Incident (Bottleneck collision/blockage on J1_J2)
                if not incident_injected and step >= incident_start:
                    # Block the affected lanes, remembering each lane's original speed limit.
                    for lane_id in incident_lanes:
                        try:
                            if lane_id not in original_lane_speeds:
                                original_lane_speeds[lane_id] = conn.lane.getMaxSpeed(lane_id)
                            conn.lane.setMaxSpeed(lane_id, 0.5)
                        except Exception:
                            pass
                    incident_injected = True

                # Clear Incident: restore the ORIGINAL speed limits (not a hard-coded value)
                if incident_injected and not incident_cleared and step >= incident_end:
                    for lane_id, original_speed in original_lane_speeds.items():
                        try:
                            conn.lane.setMaxSpeed(lane_id, original_speed)
                        except Exception:
                            pass
                    incident_cleared = True

                # 2. Signal control: nothing here by design.
                #    The Webster program (and the green-wave phase alignment) was deployed
                #    once before the loop began. Mutating phase durations from inside the
                #    stepping loop is exactly the defect that used to corrupt an actuated
                #    signal's right-of-way; the control evidence below records what was
                #    actually deployed, not what was attempted.

                # 3. Dynamic Rerouting via VMS (deterministic, reproducible assignment)
                if reroute_threshold > 0 and step >= incident_start:
                    try:
                        approach_vehs = conn.edge.getLastStepVehicleIDs(approach_edge)
                        for vid in approach_vehs:
                            # Decide each vehicle exactly once, while it is still on the
                            # approach. Re-deciding every step would both inflate the command
                            # count and multiply the diversion probability per vehicle.
                            if vid in diversion_decided:
                                continue
                            diversion_decided.add(vid)
                            if list(conn.vehicle.getRoute(vid)) == bypass_route_edges:
                                # Already routed onto the bypass (natural diversion demand):
                                # this is not a VMS action and must not be counted as one.
                                continue
                            # The diversion decision is bound to the VEHICLE, not to the
                            # (vehicle, step) pair. Per-step sampling would divert roughly
                            # 1-(1-r)^n of all vehicles (n = steps spent on the approach),
                            # i.e. a "25% diversion" would in practice divert almost everyone.
                            if _stable_bucket(vid) % 100 < reroute_threshold:
                                conn.vehicle.setRoute(vid, bypass_route_edges)
                                reroute_commands += 1
                    except Exception as exc:
                        # Never swallow control failures silently: they are reported through
                        # control_evidence so a non-acting control cannot masquerade as an
                        # applied one.
                        if len(reroute_errors) < 5:
                            reroute_errors.append(f"{type(exc).__name__}: {exc}")

                # Step the simulation
                conn.simulationStep()

                # `getArrivedNumber()` reports arrivals during the *last* step only, so the
                # total must be accumulated step by step. Sampling it once after the loop
                # (as an earlier revision did) returned the final step's count — usually 0 —
                # which made the throughput KPI meaningless (4 "completed trips" reported
                # while the tripinfo file held 977).
                completed_vehicles += conn.simulation.getArrivedNumber()

                # Collect metrics every 5 seconds
                if step % 5 == 0:
                    # Bottleneck metrics
                    halting_veh = conn.edge.getLastStepHaltingNumber(bottleneck_edge)
                    q_length_m = halting_veh * 7.5  # average effective vehicle space
                    mean_speed = conn.edge.getLastStepMeanSpeed(bottleneck_edge)
                    mean_speed_kmh = max(0.0, mean_speed * 3.6)

                    # Network totals
                    co2_step = sum(conn.edge.getCO2Emission(e) for e in all_edges)
                    fuel_step = sum(conn.edge.getFuelConsumption(e) for e in all_edges)
                    total_co2 += co2_step * 5.0
                    total_fuel += fuel_step * 5.0

                    # Average trip time loss (s/veh).
                    # NOTE: `edge.getWaitingTime()` only accumulates time spent below 0.1 m/s
                    # and is NOT trip delay. TraCI's per-vehicle `getTimeLoss()` uses the same
                    # definition as SUMO's tripinfo `timeLoss`, which is the metric the
                    # evaluator and the decision brief must report.
                    active_veh_ids = conn.vehicle.getIDList()
                    if active_veh_ids:
                        losses = []
                        for vid in active_veh_ids:
                            try:
                                losses.append(conn.vehicle.getTimeLoss(vid))
                            except Exception:
                                pass
                        avg_delay = sum(losses) / len(losses) if losses else 0.0
                    else:
                        avg_delay = 0.0

                    time_stamps.append(step)
                    bottleneck_queues.append(round(q_length_m, 1))
                    bottleneck_speeds.append(round(mean_speed_kmh, 1))
                    network_delays.append(round(avg_delay, 1))

        finally:
            if conn is not None:
                try:
                    conn.close(wait=False)
                    proc = getattr(conn, "_process", None)
                    if proc is not None:
                        try:
                            proc.wait(timeout=5.0)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
            elif started:
                try:
                    if hasattr(traci, "connection") and traci.connection.has(label):
                        c = traci.getConnection(label)
                        c.close(wait=False)
                        proc = getattr(c, "_process", None)
                        if proc is not None:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                except Exception:
                    pass

        return {
            "scheme": scheme,
            "simulation_duration": duration,
            "seed": safe_seed,
            "time_stamps": time_stamps,
            "queue_lengths": bottleneck_queues,
            "vehicle_speeds": [s / 3.6 for s in bottleneck_speeds],  # in m/s for evaluator
            "bottleneck_speeds_kmh": bottleneck_speeds,
            "vehicle_delays": network_delays,
            "delay_metric": "mean_vehicle_time_loss_s_per_veh (SUMO tripinfo `timeLoss` definition)",
            "completed_trips": completed_vehicles,
            "total_co2_mg": total_co2,
            "total_fuel_mg": total_fuel,
            "total_fuel_ml": total_fuel,  # legacy compatibility alias
            "control_evidence": {
                "scheme": scheme,
                "seed": safe_seed,
                "webster_applied": bool(webster_on and sp_deployed > 0),
                "webster_program_deployed": sp_deployed,
                "webster_program_detail": sp_evidence,
                "webster_program_errors": sp_errors,
                "green_wave_applied": bool(green_wave_on and sp_first_green_starts),
                "green_wave_first_green_start_s": sp_first_green_starts,
                "cycle_length": round(sp_green_main + 2 * sp_yellow + sp_green_cross, 1),
                "arterial_green": sp_green_main,
                "cross_green": sp_green_cross,
                "yellow": sp_yellow,
                "reroute_ratio_applied": reroute_ratio,
                "reroute_source_edge": approach_edge,
                "signal_commands": signal_commands,
                "reroute_commands": reroute_commands,
                "reroute_errors": reroute_errors,
                "incident_injected": incident_injected,
                "incident_cleared": incident_cleared,
                "lane_speeds_restored": {k: round(v, 2) for k, v in original_lane_speeds.items()},
            },
        }
