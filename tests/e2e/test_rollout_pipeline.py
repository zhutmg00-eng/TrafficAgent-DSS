from playwright.sync_api import Page, expect

import pytest

pytestmark = pytest.mark.e2e



def test_rollout_pipeline_execution_and_charts(page: Page, base_url: str):
    """Test full decision pipeline trigger, KPI updates, ECharts canvas mounting, and report generation."""
    page.goto(base_url)

    # 1. Uncheck micro SUMO sandbox toggle via evaluate to use the fast mesoscopic engine for E2E
    page.evaluate(
        "() => { const el = document.getElementById('toggleSandbox'); if (el && el.checked) { el.checked = false; el.dispatchEvent(new Event('change')); } }"
    )

    # 2. Click Run Simulation button
    run_btn = page.locator("#runSimulationBtn")
    expect(run_btn).to_be_visible()
    run_btn.click()

    # 3. Wait for rollout completion (button re-enabled and KPIs updated)
    page.wait_for_selector("#rolloutDelayImp:not(:text('—'))", timeout=15000)

    # 4. Verify KPI values updated to percentages
    delay_text = page.locator("#rolloutDelayImp").inner_text()
    assert "%" in delay_text, f"Expected percentage in delay improvement, got: {delay_text}"

    queue_text = page.locator("#rolloutQueueImp").inner_text()
    assert "%" in queue_text, f"Expected percentage in queue improvement, got: {queue_text}"

    # 5. Verify ECharts containers contain rendered <canvas> elements
    radar_canvas = page.locator("#radarChartContainer canvas")
    expect(radar_canvas).to_be_visible()

    time_series_canvas = page.locator("#timeSeriesChartContainer canvas")
    expect(time_series_canvas).to_be_visible()

    # 6. Verify Decision Briefing Report is rendered
    report_content = page.locator("#reportMarkdownContent")
    expect(report_content).not_to_have_text("正在生成专业决策简报...")
    expect(report_content).to_contain_text("Decision Briefing")


def test_degraded_notice_banner_display(page: Page, base_url: str):
    """Verify that when a rollout returns degraded=true, the degraded notice banner is displayed."""
    page.goto(base_url)

    # Mock /api/rollout to return degraded: true and fallback_reason
    def handle_mock_rollout(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            json={
                "degraded": True,
                "fallback_reason": "SUMO 微观物理沙盒环境未就绪，已安全降级至确定性中观推演引擎",
                "execution_mode": "mesoscopic_fallback",
                "corridor_choice": "corridor_arterial",
                "comparisons": {
                    "strategy_b": {
                        "delay_improvement_pct": 18.5,
                        "queue_improvement_pct": 26.0,
                        "throughput_improvement_pct": 14.0,
                        "speed_improvement_pct": 22.0,
                        "co2_improvement_pct": 12.0,
                    }
                },
                "strategy_b": {
                    "kpi": {
                        "avg_delay": 42.0,
                        "max_queue": 110.0,
                        "bottleneck_speed": 18.0,
                        "throughput": 2300,
                        "co2_emissions": 85.0,
                    },
                    "time_series": {
                        "timestamps": [0, 60, 120, 180, 240, 300, 360, 420, 480, 540, 600],
                        "queue_lengths": [10, 25, 45, 65, 80, 75, 60, 40, 25, 15, 10],
                    },
                },
                "baseline": {
                    "kpi": {
                        "avg_delay": 52.0,
                        "max_queue": 150.0,
                        "bottleneck_speed": 14.0,
                        "throughput": 2000,
                        "co2_emissions": 98.0,
                    },
                    "time_series": {
                        "timestamps": [0, 60, 120, 180, 240, 300, 360, 420, 480, 540, 600],
                        "queue_lengths": [10, 30, 60, 90, 120, 140, 150, 145, 130, 120, 110],
                    },
                },
            },
        )

    page.route("**/api/rollout", handle_mock_rollout)

    # Click run simulation
    page.locator("#runSimulationBtn").click()

    # Verify execution mode banner and degraded notice banner appear
    exec_banner = page.locator("#executionModeBanner")
    expect(exec_banner).to_be_visible()

    degraded_banner = page.locator("#degradedNoticeBanner")
    page.wait_for_selector("#degradedNoticeBanner", timeout=10000)
    expect(degraded_banner).to_be_visible()
    expect(degraded_banner).to_contain_text("降级")
