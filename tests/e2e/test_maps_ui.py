from playwright.sync_api import Page, expect


def test_baidu_map_section_structure(page: Page, base_url: str):
    """Verify Section 1B Baidu Map container, toolbar buttons, and status."""
    page.goto(base_url)

    # 1. Map container exists
    map_container = page.locator("#baiduMapContainer")
    expect(map_container).to_be_visible()

    # 2. Traffic layer toggle exists and responds
    traffic_toggle = page.locator("#baiduTrafficToggle")
    expect(traffic_toggle).to_be_visible()
    initial_checked = traffic_toggle.is_checked()
    traffic_toggle.click()
    assert traffic_toggle.is_checked() != initial_checked
    traffic_toggle.click()
    assert traffic_toggle.is_checked() == initial_checked

    # 3. Route compare button exists
    route_btn = page.locator("#baiduRouteCompareBtn")
    expect(route_btn).to_be_visible()
    expect(route_btn).to_contain_text("真实绕行路线对比")

    # 4. Status badge
    status_badge = page.locator("#baiduMapStatusBadge")
    expect(status_badge).to_be_visible()


def test_osm_svg_digital_twin_map(page: Page, base_url: str):
    """Verify Section 6 OSM SVG vector map renders links and sidebar updates on link click."""
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto(base_url)

    # 1. SVG container is rendered
    svg = page.locator("#roadNetworkSvg")
    expect(svg).to_be_visible()

    # 2. Wait for link polylines to be attached to DOM from /api/network
    page.wait_for_selector("#roadNetworkSvg polyline.road-edge", state="attached", timeout=12000)
    links = page.locator("#roadNetworkSvg polyline.road-edge")
    link_count = links.count()
    assert link_count > 50, f"Expected > 50 links, found {link_count}"

    # 3. Sidebar starts with placeholder or link data
    sidebar_content = page.locator("#mapSidebarContent")
    expect(sidebar_content).to_be_visible()

    # 4. Click first link via dispatch_event and verify inspector updates
    first_link = links.first
    first_link.dispatch_event("click")
    expect(sidebar_content).not_to_contain_text("点击路段查看详细数据")
    expect(sidebar_content).to_contain_text("路段:")

    # 5. Test view toggle button (Baseline / Strategy)
    toggle_btn = page.locator("#mapToggleBtn")
    expect(toggle_btn).to_be_visible()
    label = page.locator("#mapToggleLabel")
    initial_label = label.inner_text()
    toggle_btn.click()
    expect(label).not_to_have_text(initial_label)

    assert len(errors) == 0, f"Unexpected browser errors: {errors}"
