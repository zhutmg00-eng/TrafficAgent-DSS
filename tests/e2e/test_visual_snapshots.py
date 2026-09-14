import os
from playwright.sync_api import Page

import pytest

pytestmark = pytest.mark.e2e



def test_capture_multi_resolution_and_theme_snapshots(page: Page, base_url: str):
    """Capture responsive multi-resolution and theme visual regression snapshots."""
    screenshot_dir = os.path.join(os.path.dirname(__file__), "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)

    resolutions = [
        ("1080p_fhd", 1920, 1080),
        ("768p_laptop", 1366, 768),
    ]

    for label, width, height in resolutions:
        page.set_viewport_size({"width": width, "height": height})
        page.goto(base_url)
        page.wait_for_load_state("networkidle")

        # 1. Capture current theme
        img_path = os.path.join(screenshot_dir, f"dashboard_{label}_theme1.png")
        page.screenshot(path=img_path, full_page=True)
        assert os.path.exists(img_path) and os.path.getsize(img_path) > 20000

        # 2. Toggle theme and capture alternate theme
        page.locator("#themeToggleBtn").click()
        img_theme2_path = os.path.join(screenshot_dir, f"dashboard_{label}_theme2.png")
        page.screenshot(path=img_theme2_path, full_page=True)
        assert os.path.exists(img_theme2_path) and os.path.getsize(img_theme2_path) > 20000


def test_capture_modal_dialog_snapshot(page: Page, base_url: str):
    """Capture snapshot of the LLM configuration dialog."""
    screenshot_dir = os.path.join(os.path.dirname(__file__), "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)

    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(base_url)
    page.locator("#openLlmModalBtn").click()

    # Select Baidu Qianfan preset
    page.locator('.provider-tag:has-text("百度千帆")').click()

    modal_img_path = os.path.join(screenshot_dir, "llm_modal_baidu_qianfan.png")
    page.screenshot(path=modal_img_path)
    assert os.path.exists(modal_img_path) and os.path.getsize(modal_img_path) > 20000
