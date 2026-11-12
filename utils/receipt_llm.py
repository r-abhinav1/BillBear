"""
Vision LLM provider calls. Each call is a *single* HTTP attempt with an
explicit timeout; retry/fallback policy lives in receipt_pipeline so it can
respect one overall deadline.

API keys go in headers (never URLs), so they can't leak through exception
text or request logs.
"""
import json
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from .receipt_rules import build_image_receipt_prompt


class ProviderCallError(Exception):
    def __init__(self, message: str, retryable: bool = False, billing: bool = False):
        super().__init__(message)
        self.retryable = retryable
        # True when the provider account can't pay (402 / credits depleted):
        # an operator problem, not a bad photo.
        self.billing = billing


def _is_retryable_status_code(status_code: int) -> bool:
    return status_code in {408, 425, 429} or 500 <= status_code < 600


# ---------------------------------------------------------------------------
# Gemini circuit breaker (per instance): opens on hard quota exhaustion.
# ---------------------------------------------------------------------------
_GEMINI_CB_LOCK = threading.Lock()
_gemini_cb_open_until: float = 0.0


def _gemini_circuit_open_seconds() -> float:
    try:
        return max(10.0, float(os.getenv("GEMINI_CIRCUIT_OPEN_SECONDS", "300")))
    except ValueError:
        return 300.0


def gemini_circuit_is_open() -> bool:
    with _GEMINI_CB_LOCK:
        return time.time() < _gemini_cb_open_until


def _open_gemini_circuit(reason: str) -> None:
    global _gemini_cb_open_until
    duration = _gemini_circuit_open_seconds()
    with _GEMINI_CB_LOCK:
        _gemini_cb_open_until = time.time() + duration
    print(f"[receipt_llm] Gemini circuit breaker OPEN for {int(duration)}s — {reason}")


def _is_quota_exhausted_response(body: str) -> bool:
    lowered = body.lower()
    return "limit: 0" in body or "quota_exceeded" in lowered or "free_tier" in lowered


# ---------------------------------------------------------------------------
# JSON extraction from model text
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    normalized = text.strip().strip("`").strip()
    if normalized.startswith("json"):
        normalized = normalized[4:].strip()
    try:
        parsed = json.loads(normalized)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    json_object = _extract_json_object(normalized)
    if not json_object:
        return None
    try:
        parsed = json.loads(json_object)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def gemini_keys() -> List[str]:
    keys: List[str] = []
    for candidate in (os.getenv("GEMINI_API_KEYS", ""), os.getenv("GEMINI_API_KEY", "")):
        keys.extend(k.strip() for k in candidate.split(",") if k.strip())
    # de-dupe, keep order
    return list(dict.fromkeys(keys))


def shuffled_gemini_keys() -> List[str]:
    keys = gemini_keys()
    random.shuffle(keys)
    return keys


def has_gemini_credentials() -> bool:
    return bool(gemini_keys())


def has_openrouter_credentials() -> bool:
    return bool(os.getenv("OPENROUTER_API_KEY", "").strip())


# ---------------------------------------------------------------------------
# Provider calls (single attempt each)
# ---------------------------------------------------------------------------

DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-lite"


def gemini_generation_config() -> Dict[str, Any]:
    """
    Gemini 3 guidance: keep temperature at its default (1.0) and let
    Flash-Lite use its default "minimal" thinking (thinking tokens are billed
    as output). GEMINI_THINKING_LEVEL (minimal/low/medium/high) overrides it.
    """
    config: Dict[str, Any] = {"responseMimeType": "application/json"}
    level = os.getenv("GEMINI_THINKING_LEVEL", "").strip().lower()
    if level:
        config["thinkingConfig"] = {"thinkingLevel": level}
    return config


def _log_gemini_usage(model: str, body: Dict[str, Any]) -> None:
    usage = body.get("usageMetadata") or {}
    print(
        f"[receipt_llm] gemini usage model={model} "
        f"in={usage.get('promptTokenCount')} out={usage.get('candidatesTokenCount')} "
        f"thinking={usage.get('thoughtsTokenCount', 0)} total={usage.get('totalTokenCount')}"
    )


def call_gemini_vision(image_base64: str, mime_type: str, api_key: str, timeout: float) -> Dict[str, Any]:
    model = os.getenv("GEMINI_IMAGE_MODEL", "").strip() or DEFAULT_GEMINI_IMAGE_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{
            "parts": [
                {"text": build_image_receipt_prompt()},
                {"inline_data": {"mime_type": mime_type, "data": image_base64}},
            ]
        }],
        "generationConfig": gemini_generation_config(),
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.Timeout as exc:
        raise ProviderCallError("Gemini Vision timed out", retryable=True) from exc
    except requests.RequestException as exc:
        raise ProviderCallError(f"Gemini Vision request failed: {type(exc).__name__}", retryable=True) from exc

    if response.status_code == 402 or (
        response.status_code == 403 and "billing" in response.text.lower()
    ):
        _open_gemini_circuit(f"billing problem (HTTP {response.status_code})")
        raise ProviderCallError(
            f"Gemini billing error HTTP {response.status_code}: {response.text[:200]}",
            retryable=False,
            billing=True,
        )
    if response.status_code == 429:
        if _is_quota_exhausted_response(response.text):
            _open_gemini_circuit("quota exhausted (HTTP 429)")
            raise ProviderCallError("Gemini quota exhausted", retryable=False)
        raise ProviderCallError("Gemini Vision rate limited (HTTP 429)", retryable=True)
    if response.status_code >= 400:
        raise ProviderCallError(
            f"Gemini Vision HTTP {response.status_code}: {response.text[:200]}",
            retryable=_is_retryable_status_code(response.status_code),
        )
    try:
        body = response.json()
        parts = body["candidates"][0]["content"]["parts"]
        # Skip thought parts; join the answer text.
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderCallError("Gemini Vision response parse failed", retryable=False) from exc
    _log_gemini_usage(model, body)
    parsed = _parse_json_from_text(text)
    if parsed is None:
        raise ProviderCallError("Gemini Vision returned non-JSON output", retryable=False)
    return parsed


def call_openrouter_vision(image_base64: str, mime_type: str, timeout: float) -> Dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ProviderCallError("OpenRouter API key not configured", retryable=False)
    model = os.getenv("OPENROUTER_VISION_MODEL", "nvidia/nemotron-nano-12b-v2-vl:free")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    site_url = os.getenv("OPENROUTER_SITE_URL", "").strip()
    if site_url:
        headers["HTTP-Referer"] = site_url
    headers["X-Title"] = os.getenv("OPENROUTER_SITE_NAME", "BillBear").strip() or "BillBear"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict JSON formatter for receipt data."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_image_receipt_prompt()},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
                ],
            },
        ],
        "temperature": 0,
    }
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers, timeout=timeout
        )
    except requests.Timeout as exc:
        raise ProviderCallError("OpenRouter Vision timed out", retryable=True) from exc
    except requests.RequestException as exc:
        raise ProviderCallError(f"OpenRouter Vision request failed: {type(exc).__name__}", retryable=True) from exc
    if response.status_code >= 400:
        raise ProviderCallError(
            f"OpenRouter Vision HTTP {response.status_code}: {response.text[:200]}",
            retryable=_is_retryable_status_code(response.status_code),
        )
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderCallError("OpenRouter Vision response parse failed", retryable=False) from exc
    if isinstance(content, list):
        text = "".join(str(c.get("text", "")) for c in content if isinstance(c, dict)).strip()
    else:
        text = str(content).strip()
    parsed = _parse_json_from_text(text)
    if parsed is None:
        raise ProviderCallError("OpenRouter Vision returned non-JSON output", retryable=False)
    return parsed
