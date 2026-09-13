import os
import socket
import threading
import time
import pytest
import uvicorn
from src.web.app import app


def find_free_port() -> int:
    """Find an available TCP port for the live FastAPI test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server_url():
    """Spin up a dedicated, isolated FastAPI uvicorn server for E2E tests."""
    port = find_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Wait for server readiness
    max_retries = 60
    ready = False
    for _ in range(max_retries):
        try:
            import urllib.request
            with urllib.request.urlopen(f"{base_url}/api/status", timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)

    if not ready:
        raise RuntimeError(f"Live FastAPI server failed to start on {base_url}")

    yield base_url

    # Teardown
    server.should_exit = True
    thread.join(timeout=3)


@pytest.fixture(scope="session")
def base_url(live_server_url):
    """Integrate with pytest-playwright and pytest-base-url."""
    return live_server_url
