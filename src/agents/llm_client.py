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
from typing import Any, Dict, Optional, Tuple


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
            self.timeout = float(os.getenv("LLM_TIMEOUT", str(timeout)))
        except ValueError:
            self.timeout = timeout
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #
    @property
    def is_configured(self) -> bool:
        """True only when an API key is present (package check is lazy)."""
        return bool(self.api_key)

    def describe(self) -> Dict[str, Any]:
        """Non-sensitive description for health/status endpoints."""
        return {
            "configured": self.is_configured,
            "model": self.model if self.is_configured else None,
            "base_url": self.base_url or "https://api.openai.com/v1",
            "sdk_installed": self._sdk() is not None,
            "last_error": self.last_error,
        }

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

        fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
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
