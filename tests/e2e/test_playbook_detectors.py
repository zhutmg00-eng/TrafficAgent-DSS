from playwright.sync_api import Page, expect


def test_action_playbook_rendering(page: Page, base_url: str):
    """Verify Section 7 7-step Action Checklist loads and displays actionable instructions."""
    page.goto(base_url)

    # 1. Wait for action plan to load via /api/action-plan
    page.wait_for_selector(".action-step-card", timeout=12000)

    # 2. Check summary and steps
    summary = page.locator("#actionSummary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("行动概要")

    # 3. Verify step cards
    cards = page.locator(".action-step-card")
    count = cards.count()
    assert count >= 5, f"Expected at least 5 action steps, got {count}"

    # 4. Check first step card structure
    first_card = cards.first
    expect(first_card.locator(".action-step-num")).to_be_visible()
    expect(first_card.locator(".action-step-title")).to_be_visible()
    expect(first_card.locator(".action-step-detail")).to_be_visible()


def test_detector_table_structure(page: Page, base_url: str):
    """Verify Section 8 Detector Table has correct headers and sort attributes."""
    page.goto(base_url)

    table = page.locator("#detectorTable")
    expect(table).to_be_visible()

    # Verify table headers
    headers = page.locator("#detectorTable th")
    assert headers.count() == 10

    header_texts = [headers.nth(i).inner_text() for i in range(headers.count())]
    assert any("拥堵等级" in h for h in header_texts)
    assert any("路段编号" in h for h in header_texts)
    assert any("名称" in h for h in header_texts)
    assert any("峰值排队" in h for h in header_texts)
