"""
TrafficAgent-DSS: LLM Reasoning Client
OpenAI-compatible chat client wrapper with explicit, auditable degradation.

设计契约（重要）：
  1. 本客户端**只负责推理与解释**，绝不产出任何性能指标数值（延误/排队/碳排等）。
     所有数值一律由交通工程工具与 SUMO 仿真计算得出，避免模型"编数字"。
  2. 若模型未配置或调用失败，**不静默伪造结果**，而是返回明确的降级标记
     (reasoning_mode)，由调用方决定回落到确定性模板，并在结论中如实标注。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple


def _load_dotenv_if_available() -> None:
    """
    Load a local `.env` file when python-dotenv is installed (see `.env.example`).

    Optional by design: credentials may equally be injected through real environment
    variables, so a missing package must never break the import.
    """
    try:
        from dotenv import load_dotenv  # noqa: WPS433 (optional dependency)
    except Exception:
        return
    try:
        load_dotenv(override=False)
    except Exception:
        pass


# Populate LLM_API_KEY / LLM_BASE_URL / LLM_MODEL from .env once, at import time.
_load_dotenv_if_available()


class LLMReasoningClient:
    """Thin wrapper over any OpenAI-compatible chat completion endpoint."""

    MODE_LLM = "llm"
    MODE_UNCONFIGURED = "template_fallback_unconfigured"
    MODE_ERROR = "template_fallback_llm_error"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.api_key = (
            api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        ).strip()
        self.base_url = (
            base_url or os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or ""
        ).strip() or None
        self.model = (model or os.getenv("LLM_MODEL") or "gpt-4o-mini").strip()
        try:
            self.timeout = max(1.0, float(os.getenv("LLM_TIMEOUT", str(timeout))))
        except ValueError:
            self.timeout = max(1.0, float(timeout))
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Status & Configuration helpers
    # ------------------------------------------------------------------ #
    @property
    def is_configured(self) -> bool:
        """True only when an API key is present (package check is lazy)."""
        return bool(self.api_key)

    def describe(self) -> Dict[str, Any]:
        """Non-sensitive description for health/status endpoints."""
        masked_key = None
        if self.api_key:
            if len(self.api_key) <= 8:
                masked_key = "sk-***"
            else:
                masked_key = f"{self.api_key[:3]}***{self.api_key[-4:]}"

        return {
            "configured": self.is_configured,
            "model": self.model,
            "base_url": self.base_url or "https://api.openai.com/v1",
            "sdk_installed": self._sdk() is not None,
            "last_error": self.last_error,
            "masked_key": masked_key,
        }

    def update_config(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Hot-updates the LLM client credentials and active model at runtime.
        Returns the updated describe() dictionary.
        """
        if api_key is not None:
            self.api_key = api_key.strip()
        if base_url is not None:
            b = base_url.strip().rstrip("/")
            self.base_url = b if b else None
        if model is not None and model.strip():
            self.model = model.strip()
        if timeout is not None:
            try:
                self.timeout = max(1.0, float(timeout))
            except (ValueError, TypeError):
                pass
        self.last_error = None
        return self.describe()

    # ------------------------------------------------------------------ #
    # Model Auto-Discovery (ccSwitch-style /v1/models detection)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_model_ids_from_dict(data: Any) -> List[str]:
        """Extracts model IDs from OpenAI, Ollama, or custom /v1/models response formats."""
        if not data:
            return []

        ids: List[str] = []
        # Format 1: Standard OpenAI {"data": [{"id": "gpt-4o", ...}, ...]}
        if isinstance(data, dict):
            items = data.get("data") or data.get("models") or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        m_id = item.get("id") or item.get("name") or item.get("model")
                        if m_id:
                            ids.append(str(m_id).strip())
                    elif isinstance(item, str) and item.strip():
                        ids.append(item.strip())
            # Format 2: Direct model dict or array
            elif isinstance(data.get("model"), str):
                ids.append(data["model"].strip())
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    m_id = item.get("id") or item.get("name")
                    if m_id:
                        ids.append(str(m_id).strip())
                elif isinstance(item, str) and item.strip():
                    ids.append(item.strip())

        return ids

    @classmethod
    def _sort_and_filter_models(cls, models: List[str]) -> List[str]:
        """
        Deduplicates and sorts models intelligently:
        Prioritizes chat / instruction / reasoning models at the top.
        """
        seen = set()
        deduped = []
        for m in models:
            if m and m not in seen:
                seen.add(m)
                deduped.append(m)

        chat_keywords = (
            "chat", "reasoner", "deepseek", "gpt-4", "gpt-3.5", "qwen", "claude",
            "llama", "glm", "mistral", "yi-", "kimi", "moonshot", "instruct", "o1"
        )
        non_chat_keywords = ("embed", "embedding", "tts", "whisper", "dall-e", "moderation", "rerank")

        primary_chat = []
        secondary = []
        auxiliary = []

        for m in deduped:
            m_lower = m.lower()
            if any(k in m_lower for k in non_chat_keywords):
                auxiliary.append(m)
            elif any(k in m_lower for k in chat_keywords):
                primary_chat.append(m)
            else:
                secondary.append(m)

        primary_chat.sort()
        secondary.sort()
        auxiliary.sort()

        return primary_chat + secondary + auxiliary

    @classmethod
    def list_available_models(
        cls,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 12.0,
    ) -> Tuple[List[str], Optional[str]]:
        """
        Queries an OpenAI-compatible /v1/models endpoint to auto-detect available models.
        Dual-track implementation: attempts OpenAI SDK first, gracefully falls back to raw HTTP.

        Returns:
            (model_ids: List[str], error: Optional[str])
        """
        key = (api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        url = (base_url or os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
        if not url:
            url = "https://api.openai.com/v1"

        # Track 1: Try OpenAI official SDK if available and key is present
        openai_cls = cls._sdk()
        if openai_cls is not None and key:
            try:
                sdk_client = openai_cls(api_key=key, base_url=url, timeout=timeout)
                resp = sdk_client.models.list()
                raw_list = []
                data_attr = getattr(resp, "data", None)
                if data_attr and isinstance(data_attr, list):
                    for item in data_attr:
                        m_id = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else str(item))
                        if m_id:
                            raw_list.append(str(m_id).strip())
                if raw_list:
                    return cls._sort_and_filter_models(raw_list), None
            except Exception:
                # SDK failed or endpoint returned non-standard format; fall through to HTTP
                pass

        # Track 2: Raw HTTP probe using httpx or urllib
        # Probe candidate endpoints (e.g. handle case where user omitted /v1)
        if url.endswith("/models"):
            probe_urls = [url]
        elif url.endswith("/v1"):
            probe_urls = [f"{url}/models"]
        else:
            probe_urls = [f"{url}/v1/models", f"{url}/models"]

        headers = {
            "User-Agent": "TrafficAgent-DSS/2.1 (ccSwitch-ModelDetector)",
            "Accept": "application/json",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"

        last_http_err = None

        # Try httpx if available
        try:
            import httpx
            for p_url in probe_urls:
                try:
                    with httpx.Client(timeout=timeout, verify=True) as client:
                        resp = client.get(p_url, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            extracted = cls._extract_model_ids_from_dict(data)
                            if extracted:
                                return cls._sort_and_filter_models(extracted), None
                        elif resp.status_code in (401, 403):
                            return [], f"API 认证失败 (HTTP {resp.status_code})：请核对 API Key 是否正确或具有访问权限。"
                        else:
                            last_http_err = f"HTTP {resp.status_code}: {resp.text[:120]}"
                except Exception as exc:
                    last_http_err = f"{type(exc).__name__}: {exc}"
        except ImportError:
            # Fallback to standard library urllib
            import urllib.request
            import urllib.error
            for p_url in probe_urls:
                try:
                    req = urllib.request.Request(p_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status == 200:
                            raw_body = resp.read().decode("utf-8")
                            data = json.loads(raw_body)
                            extracted = cls._extract_model_ids_from_dict(data)
                            if extracted:
                                return cls._sort_and_filter_models(extracted), None
                except urllib.error.HTTPError as he:
                    if he.code in (401, 403):
                        return [], f"API 认证失败 (HTTP {he.code})：请核对 API Key 是否正确或具有访问权限。"
                    last_http_err = f"HTTP {he.code}: {he.reason}"
                except Exception as exc:
                    last_http_err = f"{type(exc).__name__}: {exc}"

        if not key:
            return [], "未提供 API Key，且目标端点需要鉴权后方可查询模型列表。"

        return [], f"未能从端点自动识别可用模型 ({last_http_err or '未返回有效模型数据'})，请检查 Base URL 与网络连接。"

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sdk():
        """Lazily import the openai SDK so the system still boots without it."""
        try:
            from openai import OpenAI  # noqa: WPS433 (intentional lazy import)
            return OpenAI
        except Exception:
            return None

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """Best-effort JSON extraction from a model response."""
        if not text:
            return None

        raw = text.strip()

        fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL | re.IGNORECASE)
        if fenced:
            raw = fenced.group(1).strip()

        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass

        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
        """
        Requests a JSON object from the model.

        Returns:
            (payload, mode, error)
              payload : parsed dict, or None when the model was not used
              mode    : one of MODE_LLM / MODE_UNCONFIGURED / MODE_ERROR
              error   : human-readable reason, or None on success
        """
        if not self.is_configured:
            self.last_error = "LLM_API_KEY / OPENAI_API_KEY is not set"
            return None, self.MODE_UNCONFIGURED, self.last_error

        openai_cls = self._sdk()
        if openai_cls is None:
            self.last_error = "openai package is not installed"
            return None, self.MODE_UNCONFIGURED, self.last_error

        try:
            kwargs: Dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout}
            if self.base_url:
                kwargs["base_url"] = self.base_url

            client = openai_cls(**kwargs)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )

            content = ""
            if getattr(response, "choices", None):
                content = response.choices[0].message.content or ""

            payload = self._extract_json(content)
            if payload is None:
                self.last_error = "model response did not contain a valid JSON object"
                return None, self.MODE_ERROR, self.last_error

            self.last_error = None
            return payload, self.MODE_LLM, None

        except Exception as exc:  # network / auth / rate-limit / timeout ...
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None, self.MODE_ERROR, self.last_error
