"""Behavioral regressions found during the repository review."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_map_contains_drawable_polylines(page, base_url):
    page.goto(base_url)
    edge = page.locator('polyline.road-edge').first
    expect(edge).to_be_attached()
    assert edge.evaluate('(el) => el.points.numberOfItems') >= 2
    assert edge.evaluate('(el) => el.getTotalLength()') > 0


def test_report_markup_is_inert(page, base_url):
    page.goto(base_url)
    result = page.evaluate('''() => {
      const host = document.createElement('div');
      document.body.appendChild(host);
      host.innerHTML = renderMarkdownToHtml('<img src=x onerror="window.compromised=true">');
      return {images: host.querySelectorAll('img').length, text: host.textContent};
    }''')
    assert result['images'] == 0
    assert '<img' in result['text']


def test_negative_improvement_is_displayed_as_increase(page, base_url):
    page.goto(base_url)
    page.evaluate('''() => {
      state.rollout = {execution_mode: 'mesoscopic_network'};
      renderHeroConclusion({delay_improvement_pct: -12}, {avg_delay_s: 42});
    }''')
    expect(page.locator('#heroValue')).to_have_text('+12%')
    expect(page.locator('#heroHeadline')).to_contain_text('增加')
    expect(page.locator('#heroSub')).to_contain_text('中观')
