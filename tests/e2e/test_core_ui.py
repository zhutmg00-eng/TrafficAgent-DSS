import os
from playwright.sync_api import Page, expect


def test_dashboard_initial_rendering(page: Page, base_url: str):
    """Verify that the dashboard loads completely with brand headers and correct initial state."""
    page.goto(base_url)

    # 1. Title and Header branding
    expect(page).to_have_title("TrafficAgent-DSS 城市交通拥堵治理数字孪生决策支持系统")
    brand_title = page.locator(".brand-title")
    expect(brand_title).to_have_text("TrafficAgent-DSS")

    # 2. Status Badges
    sandbox_badge = page.locator("#sandboxStatusBadge")
    expect(sandbox_badge).to_be_visible()
    agent_badge = page.locator("#agentBrainStatusBadge")
    expect(agent_badge).to_be_visible()

    # 3. Macro KPI Section 1 values
    expect(page.locator("#kpiSpeed")).to_contain_text("km/h")
    expect(page.locator("#kpiQueue")).to_contain_text("m")
    expect(page.locator("#kpiOcc")).to_contain_text("%")

    # 4. Rollout KPI elements exist and are rendered
    delay_imp = page.locator("#rolloutDelayImp").inner_text()
    assert delay_imp == "—" or "%" in delay_imp, f"Expected placeholder or percentage, got {delay_imp}"

    overall_rating = page.locator("#rolloutOverallRating")
    expect(overall_rating).to_be_visible()


def test_theme_toggle_interaction(page: Page, base_url: str):
    """Verify switching between light and dark themes updates DOM and persistence."""
    page.goto(base_url)

    html = page.locator("html")
    initial_theme = html.get_attribute("data-theme")
    assert initial_theme in ["light", "dark"]

    theme_btn = page.locator("#themeToggleBtn")
    expect(theme_btn).to_be_visible()

    # Click theme toggle button
    theme_btn.click()

    expected_theme = "dark" if initial_theme == "light" else "light"
    expect(html).to_have_attribute("data-theme", expected_theme)

    # Click again to revert
    theme_btn.click()
    expect(html).to_have_attribute("data-theme", initial_theme)


def test_dashboard_visual_screenshot(page: Page, base_url: str):
    """Capture full-page screenshot of the dashboard as visual test artifact."""
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    screenshot_dir = os.path.join(os.path.dirname(__file__), "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_path = os.path.join(screenshot_dir, "dashboard_initial_1080p.png")

    page.screenshot(path=screenshot_path, full_page=True)
    assert os.path.exists(screenshot_path)
    assert os.path.getsize(screenshot_path) > 10000
