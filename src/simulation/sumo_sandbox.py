"""
TrafficAgent-DSS: SUMO Simulation Sandbox Controller
Manages headless SUMO execution, TraCI interaction, incident injection, and What-If evaluation.
"""

import os
import sys
import time
import shutil
import zlib
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
    ) -> Dict[str, Any]:
        """
        Runs a complete headless simulation run.

        Schemes:
          - 'baseline': Fixed-time signal control, no rerouting, no intervention.
          - 'webster': Adaptive signal timing based on Webster's method.
          - 'agent_dss': Coordinated Agent strategy (Webster + Arterial Green Wave + Dynamic VMS Rerouting).

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

        # Arterial progression plan (computed by the green-wave tool and forwarded here).
        gw_offsets = [float(o) for o in (control_params.get("green_wave_offsets") or [])]
        cycle_length = max(1.0, float(control_params.get("cycle_length") or 90.0))
        arterial_green = max(1.0, float(control_params.get("arterial_green") or 45.0))
        arterial_phase = int(control_params.get("arterial_phase", 0))

        # Control-actuation accounting: proves which controls actually reached the simulator.
        signal_commands = 0
        reroute_commands = 0

        # Generate unique connection label to allow concurrent runs
        self.port_counter += 1
        label = f"sim_{scheme}_{self.port_counter}_{int(time.time())}"

        cmd = [
            self.sumo_bin,
            "-c", str(self.cfg_file),
            "--no-step-log", "true",
            "--time-to-teleport", "-1",
            "--collision.action", "none",
            "--waiting-time-memory", "1000",
        ]

        traci.start(cmd, label=label)
        conn = traci.getConnection(label)

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
            tls_list = conn.trafficlight.getIDList()
            all_edges = conn.edge.getIDList()
            bottleneck_edge = "J1_J2"
            incident_lanes = (f"{bottleneck_edge}_0", f"{bottleneck_edge}_1")

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

                # 2. Dynamic Signal Control
                #    (a) green_wave_on -> arterial progression: every junction's arterial
                #        green begins at t ≡ offset (mod C), which forms the coordinated band.
                #    (b) webster_on alone -> single-point adaptive green timing.
                if step > 60 and (green_wave_on or webster_on):
                    for idx, tl_id in enumerate(("J1", "J2", "J3")):
                        if tl_id not in tls_list:
                            continue
                        try:
                            if green_wave_on and gw_offsets:
                                offset = gw_offsets[idx % len(gw_offsets)]
                                phase_pos = (step - int(round(offset))) % int(round(cycle_length))
                                if phase_pos == 0:
                                    conn.trafficlight.setPhase(tl_id, arterial_phase)
                                    conn.trafficlight.setPhaseDuration(tl_id, arterial_green)
                                    signal_commands += 1
                            elif webster_on and step % 30 == 0:
                                if conn.trafficlight.getPhase(tl_id) == arterial_phase:
                                    conn.trafficlight.setPhaseDuration(tl_id, arterial_green)
                                    signal_commands += 1
                        except Exception:
                            pass

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

            completed_vehicles = conn.simulation.getArrivedNumber()

        finally:
            try:
                conn.close()
            except Exception:
                pass

        return {
            "scheme": scheme,
            "simulation_duration": duration,
            "time_stamps": time_stamps,
            "queue_lengths": bottleneck_queues,
            "vehicle_speeds": [s / 3.6 for s in bottleneck_speeds],  # in m/s for evaluator
            "bottleneck_speeds_kmh": bottleneck_speeds,
            "vehicle_delays": network_delays,
            "delay_metric": "mean_vehicle_time_loss_s_per_veh (SUMO tripinfo `timeLoss` definition)",
            "completed_trips": completed_vehicles,
            "total_co2_mg": total_co2,
            "total_fuel_ml": total_fuel,
            "control_evidence": {
                "scheme": scheme,
                "webster_applied": webster_on,
                "green_wave_applied": bool(green_wave_on and gw_offsets),
                "green_wave_offsets": gw_offsets,
                "cycle_length": cycle_length,
                "arterial_green": arterial_green,
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
