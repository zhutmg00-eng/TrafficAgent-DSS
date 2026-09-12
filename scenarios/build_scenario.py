"""
TrafficAgent-DSS: Scenario Builder
Compiles the SUMO road network and generates realistic traffic demand and incident files.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path


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


def find_sumo_binary(binary_name: str) -> str:
    """Finds SUMO executable (e.g. netconvert, sumo, sumo-gui)."""
    # 1. Check PATH
    which_bin = shutil.which(binary_name)
    if which_bin:
        return which_bin

    # 2. Check Scripts or site-packages
    py_dir = Path(sys.executable).parent
    candidates = [
        py_dir / "Scripts" / f"{binary_name}.exe",
        py_dir / "Lib" / "site-packages" / "sumo_data" / "bin" / f"{binary_name}.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    return binary_name


def build_network(scenario_dir: Path):
    """Compiles .nod.xml and .edg.xml into .net.xml."""
    nod_file = scenario_dir / "corridor.nod.xml"
    edg_file = scenario_dir / "corridor.edg.xml"
    net_file = scenario_dir / "corridor.net.xml"

    netconvert_bin = find_sumo_binary("netconvert")
    print(f"[Build] Using netconvert: {netconvert_bin}")

    cmd = [
        netconvert_bin,
        "--node-files", str(nod_file),
        "--edge-files", str(edg_file),
        "--output-file", str(net_file),
        "--no-turnarounds", "true",
        "--tls.guess", "true",
        "--tls.join", "true",
        "--tls.default-type", "actuated"
    ]
    print(f"[Build] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[Build] Error: {result.stderr}")
        raise RuntimeError(f"netconvert failed: {result.stderr}")
    print(f"[Build] Successfully generated {net_file}")


def generate_routes(scenario_dir: Path):
    """Generates demand routes with peak surge and dual paths."""
    rou_file = scenario_dir / "corridor.rou.xml"

    content = """<?xml version="1.0" encoding="UTF-8"?>
<routes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/routes_file.xsd">
    <!-- Vehicle Types -->
    <vType id="passenger" accel="2.6" decel="4.5" sigma="0.5" length="4.5" minGap="2.5" maxSpeed="16.67" color="yellow"/>
    <vType id="priority_car" accel="3.0" decel="4.5" sigma="0.2" length="4.8" minGap="2.0" maxSpeed="18.0" color="red"/>

    <!-- Key Route Definitions -->
    <!-- Main Arterial Eastbound (enters through the shared upstream approach) -->
    <route id="r_main_EB" edges="approach_W entry_J1 J1_J2 J2_J3 J3_exit"/>
    <!-- North Parallel Bypass Eastbound (same approach, diverted at entry_W) -->
    <route id="r_byp_EB" edges="approach_W entry_div div_byp byp_mer mer_exit"/>

    <!-- Main Arterial Westbound -->
    <route id="r_main_WB" edges="exit_J3 J3_J2 J2_J1 J1_entry"/>

    <!-- Cross Street Routes -->
    <route id="r_J1_NS" edges="N1_J1 J1_S1"/>
    <route id="r_J1_SN" edges="S1_J1 J1_N1"/>

    <route id="r_J2_NS" edges="N2_J2 J2_S2"/>
    <route id="r_J2_SN" edges="S2_J2 J2_N2"/>

    <route id="r_J3_NS" edges="N3_J3 J3_S3"/>
    <route id="r_J3_SN" edges="S3_J3 J3_N3"/>

    <!-- Traffic Flows (600s simulation horizon) -->
    <!-- NOTE: flows MUST be listed in non-decreasing `begin` order. SUMO silently IGNORES
         any flow that appears out of order ("Route file should be sorted by departure
         time, ignoring ..."), which previously dropped 7 of the 12 demand flows. -->

    <!-- begin = 0 -->
    <!-- 1. Off-peak background flows (0-100s, ~1200 veh/h on main) -->
    <flow id="f_bg_EB" route="r_main_EB" type="passenger" begin="0" end="100" probability="0.33" departLane="best"/>
    <flow id="f_bg_WB" route="r_main_WB" type="passenger" begin="0" end="600" probability="0.25" departLane="best"/>

    <!-- 4. Natural bypass flow (small fraction default) -->
    <flow id="f_byp_natural" route="r_byp_EB" type="passenger" begin="0" end="600" probability="0.08" departLane="best"/>

    <!-- 5. Cross street demands (600-800 veh/h each) -->
    <flow id="f_J1_NS" route="r_J1_NS" type="passenger" begin="0" end="600" probability="0.16" departLane="best"/>
    <flow id="f_J1_SN" route="r_J1_SN" type="passenger" begin="0" end="600" probability="0.16" departLane="best"/>

    <flow id="f_J2_NS" route="r_J2_NS" type="passenger" begin="0" end="600" probability="0.20" departLane="best"/>
    <flow id="f_J2_SN" route="r_J2_SN" type="passenger" begin="0" end="600" probability="0.20" departLane="best"/>

    <flow id="f_J3_NS" route="r_J3_NS" type="passenger" begin="0" end="600" probability="0.16" departLane="best"/>
    <flow id="f_J3_SN" route="r_J3_SN" type="passenger" begin="0" end="600" probability="0.16" departLane="best"/>

    <!-- begin = 100 -->
    <!-- 2. Peak surge flows on Main Arterial (100-500s, ~2000 veh/h, saturated) -->
    <flow id="f_peak_EB" route="r_main_EB" type="passenger" begin="100" end="500" probability="0.55" departLane="best"/>

    <!-- begin = 500 -->
    <!-- 3. Post-peak recovery flow (500-600s) -->
    <flow id="f_post_EB" route="r_main_EB" type="passenger" begin="500" end="600" probability="0.28" departLane="best"/>
</routes>
"""
    with open(rou_file, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[Build] Successfully generated {rou_file}")


def generate_sumocfg(scenario_dir: Path):
    """Generates the .sumocfg file."""
    cfg_file = scenario_dir / "corridor.sumocfg"
    content = """<?xml version="1.0" encoding="UTF-8"?>
<configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/sumoConfiguration.xsd">
    <input>
        <net-file value="corridor.net.xml"/>
        <route-files value="corridor.rou.xml"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="600"/>
        <step-length value="1.0"/>
    </time>
    <processing>
        <collision.action value="none"/>
        <time-to-teleport value="-1"/>
    </processing>
    <report>
        <verbose value="false"/>
        <no-step-log value="true"/>
    </report>
</configuration>
"""
    with open(cfg_file, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[Build] Successfully generated {cfg_file}")


if __name__ == "__main__":
    scenario_directory = Path(__file__).parent.resolve()
    print(f"Building scenario in: {scenario_directory}")
    generate_routes(scenario_directory)
    generate_sumocfg(scenario_directory)
    build_network(scenario_directory)
    print("[Build] Scenario build complete!")
