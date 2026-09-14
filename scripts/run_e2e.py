"""
Run Playwright E2E Test Suite for TrafficAgent-DSS.
Executes pytest with Playwright against a live headless Chromium instance.

Note on `-m e2e`: pytest.ini excludes the `e2e` marker by default so that a plain
`pytest` stays a fast core regression run. Passing `-m e2e` on the command line
overrides that default and re-enables this suite.
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
        "-m", "e2e",
        "-v",
        "--tb=short",
        "-p", "no:cacheprovider",
    ]
    print(f"[E2E Runner] Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(root))
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
