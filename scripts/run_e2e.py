"""
Run Playwright E2E Test Suite for TrafficAgent-DSS.
Executes pytest with Playwright against a live headless Chromium instance.
"""

import sys
import subprocess
from pathlib import Path

def main():
    root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(root / "tests" / "e2e"),
        "-v",
        "--tb=short"
    ]
    print(f"[E2E Runner] Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(root))
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
