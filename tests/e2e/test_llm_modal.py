from playwright.sync_api import Page, expect


def test_llm_modal_open_and_close(page: Page, base_url: str):
    """Verify opening and closing the LLM settings modal dialog."""
    page.goto(base_url)

    modal = page.locator("#llmConfigModal")
    expect(modal).not_to_be_visible()

    # Open modal
    page.locator("#openLlmModalBtn").click()
    expect(modal).to_be_visible()

    # Close via close button (x)
    page.locator("#closeLlmModalBtn").click()
    expect(modal).not_to_be_visible()

    # Open again and close via cancel button
    page.locator("#openLlmModalBtn").click()
    expect(modal).to_be_visible()
    page.locator("#cancelLlmModalBtn").click()
    expect(modal).not_to_be_visible()


def test_llm_modal_provider_presets(page: Page, base_url: str):
    """Verify clicking quick provider tags autofills the API Base URL input."""
    page.goto(base_url)
    page.locator("#openLlmModalBtn").click()

    url_input = page.locator("#llmBaseUrlInput")

    # 1. 百度千帆
    page.locator('.provider-tag:has-text("百度千帆")').click()
    expect(url_input).to_have_value("https://qianfan.baidubce.com/v2")

    # 2. DeepSeek
    page.locator('.provider-tag:has-text("DeepSeek 官方")').click()
    expect(url_input).to_have_value("https://api.deepseek.com/v1")

    # 3. 硅基流动
    page.locator('.provider-tag:has-text("硅基流动")').click()
    expect(url_input).to_have_value("https://api.siliconflow.cn/v1")

    # 4. Ollama 本地
    page.locator('.provider-tag:has-text("Ollama 本地")').click()
    expect(url_input).to_have_value("http://localhost:11434/v1")


def test_llm_api_key_visibility_toggle(page: Page, base_url: str):
    """Verify toggling password mask for API key input."""
    page.goto(base_url)
    page.locator("#openLlmModalBtn").click()

    key_input = page.locator("#llmApiKeyInput")
    expect(key_input).to_have_attribute("type", "password")

    eye_btn = page.locator("#toggleApiKeyVisibilityBtn")
    eye_btn.click()
    expect(key_input).to_have_attribute("type", "text")

    eye_btn.click()
    expect(key_input).to_have_attribute("type", "password")
