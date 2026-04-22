import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from .receipt_rules import build_image_receipt_prompt, build_text_receipt_prompt


class ProviderCallError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _provider_timeout_seconds() -> float:
    try:
        return float(os.getenv("OCR_PROVIDER_TIMEOUT_SECONDS", "8"))
    except ValueError:
        return 8.0


def _provider_max_attempts() -> int:
    try:
        return max(1, int(os.getenv("OCR_PROVIDER_MAX_ATTEMPTS", "1")))
    except ValueError:
        return 1


def _is_retryable_status_code(status_code: int) -> bool:
    return status_code in {408, 425, 429} or 500 <= status_code < 600


# ---------------------------------------------------------------------------
# Gemini circuit breaker
# Opens when Gemini hits a quota-exhausted 429, stays open for a configurable
# duration to avoid burning time on every request while quota is gone.
# ---------------------------------------------------------------------------
_GEMINI_CB_LOCK = threading.Lock()
_gemini_cb_open: bool = False
_gemini_cb_open_until: float = 0.0


def _gemini_circuit_open_seconds() -> float:
    try:
        return max(10.0, float(os.getenv("GEMINI_CIRCUIT_OPEN_SECONDS", "300")))
    except ValueError:
        return 300.0


def _is_gemini_circuit_open() -> bool:
    global _gemini_cb_open, _gemini_cb_open_until
    with _GEMINI_CB_LOCK:
        if _gemini_cb_open:
            if time.time() < _gemini_cb_open_until:
                return True
            # Circuit has expired — close it and allow a retry
            _gemini_cb_open = False
            print("[receipt_llm] Gemini circuit breaker closed — will retry Gemini")
        return False


def _open_gemini_circuit(reason: str) -> None:
    global _gemini_cb_open, _gemini_cb_open_until
    duration = _gemini_circuit_open_seconds()
    with _GEMINI_CB_LOCK:
        _gemini_cb_open = True
        _gemini_cb_open_until = time.time() + duration
    print(f"[receipt_llm] Gemini circuit breaker OPEN for {int(duration)}s — {reason}")


def _is_quota_exhausted_response(body: str) -> bool:
    """Return True when Gemini reports a hard quota exhaustion (limit: 0)."""
    return "limit: 0" in body or "quota_exceeded" in body.lower() or "free_tier" in body.lower()


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


def _collect_gemini_keys() -> List[str]:
    candidates = [
        os.getenv("GEMINI_API_KEYS", ""),
        os.getenv("GEMINI_API_KEY", ""),
        os.getenv("gemini-api-key", ""),
    ]
    keys: List[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        keys.extend([key.strip() for key in candidate.split(",") if key.strip()])
    return keys


def has_gemini_credentials() -> bool:
    return bool(_collect_gemini_keys())


def gemini_circuit_is_open() -> bool:
    """True when the circuit breaker is open and Gemini calls will be skipped."""
    return _is_gemini_circuit_open()


def has_openrouter_credentials() -> bool:
    return bool(os.getenv("OPENROUTER_API_KEY", "").strip())


def call_gemini_text_normalizer(raw_text: str) -> Dict[str, Any]:
    if _is_gemini_circuit_open():
        raise ProviderCallError("Gemini circuit breaker open — skipping to fallback", retryable=True)

    keys = _collect_gemini_keys()
    if not keys:
        raise ProviderCallError("Gemini API key not configured", retryable=False)

    model = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.0-flash")
    prompt = build_text_receipt_prompt(raw_text)
    timeout = _provider_timeout_seconds()
    # Try each key once; if only one key is configured, one attempt is sufficient
    # (the circuit breaker handles repeated failures at a higher level)
    attempts = max(1, len(keys))

    for attempt_index in range(attempts):
        key = keys[attempt_index % len(keys)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}

        print(f"[receipt_llm] Gemini attempt {attempt_index + 1}/{attempts} (model={model})")
        try:
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
        except requests.Timeout as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"Gemini timeout: {exc}", retryable=True) from exc
        except requests.RequestException as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"Gemini request failed: {exc}", retryable=True) from exc

        if response.status_code == 429:
            body = response.text
            if _is_quota_exhausted_response(body):
                # Hard quota exhaustion — open circuit breaker, no point retrying any key
                _open_gemini_circuit(f"quota exhausted (HTTP 429)")
                raise ProviderCallError(
                    f"Gemini quota exhausted — circuit breaker opened: {body[:200]}",
                    retryable=True,
                )
            # Temporary rate limit — retry with next key if available
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"Gemini rate limited (HTTP 429): {body[:200]}", retryable=True)

        if response.status_code >= 400:
            retryable = _is_retryable_status_code(response.status_code)
            message = f"Gemini HTTP {response.status_code}: {response.text[:300]}"
            if retryable and attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(message, retryable=retryable)

        try:
            payload_json = response.json()
            text = payload_json["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError(f"Gemini response parse failed: {exc}", retryable=False) from exc

        parsed_json = _parse_json_from_text(text)
        if parsed_json is None:
            raise ProviderCallError("Gemini returned non-JSON output", retryable=False)
        print("[receipt_llm] Gemini succeeded")
        return parsed_json

    raise ProviderCallError("Gemini request failed after retries", retryable=True)


def call_openrouter_text_normalizer(raw_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ProviderCallError("OpenRouter API key not configured", retryable=False)

    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    timeout = _provider_timeout_seconds()
    attempts = _provider_max_attempts()
    prompt = build_text_receipt_prompt(raw_text)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    site_url = os.getenv("OPENROUTER_SITE_URL", "").strip()
    site_name = os.getenv("OPENROUTER_SITE_NAME", "BillBear").strip()
    if site_url:
        headers["HTTP-Referer"] = site_url
    if site_name:
        headers["X-Title"] = site_name

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict JSON formatter for receipt data."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }

    for attempt_index in range(attempts):
        print(f"[receipt_llm] OpenRouter attempt {attempt_index + 1}/{attempts} (model={model})")
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except requests.Timeout as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"OpenRouter timeout: {exc}", retryable=True) from exc
        except requests.RequestException as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"OpenRouter request failed: {exc}", retryable=True) from exc

        if response.status_code >= 400:
            retryable = _is_retryable_status_code(response.status_code)
            message = f"OpenRouter HTTP {response.status_code}: {response.text[:300]}"
            if retryable and attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(message, retryable=retryable)

        try:
            payload_json = response.json()
            content = payload_json["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError(f"OpenRouter response parse failed: {exc}", retryable=False) from exc

        if isinstance(content, list):
            text = "".join(
                str(chunk.get("text", "")) for chunk in content if isinstance(chunk, dict)
            ).strip()
        else:
            text = str(content).strip()

        parsed_json = _parse_json_from_text(text)
        if parsed_json is None:
            raise ProviderCallError("OpenRouter returned non-JSON output", retryable=False)

        print("[receipt_llm] OpenRouter succeeded")
        return parsed_json

    raise ProviderCallError("OpenRouter request failed after retries", retryable=True)


# ---------------------------------------------------------------------------
# Vision LLM calls — accept base64-encoded image, return structured receipt
# ---------------------------------------------------------------------------


def call_gemini_vision(image_base64: str, mime_type: str = "image/jpeg") -> Dict[str, Any]:
    """Send an image to Gemini Vision and get structured receipt JSON back."""
    if _is_gemini_circuit_open():
        raise ProviderCallError("Gemini circuit breaker open — skipping to fallback", retryable=True)

    keys = _collect_gemini_keys()
    if not keys:
        raise ProviderCallError("Gemini API key not configured", retryable=False)

    model = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.0-flash")
    prompt = build_image_receipt_prompt()
    timeout = _provider_timeout_seconds()
    attempts = max(1, len(keys))

    for attempt_index in range(attempts):
        key = keys[attempt_index % len(keys)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": image_base64}},
                ]
            }]
        }

        print(f"[receipt_llm] Gemini Vision attempt {attempt_index + 1}/{attempts} (model={model})")
        try:
            response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
        except requests.Timeout as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"Gemini Vision timeout: {exc}", retryable=True) from exc
        except requests.RequestException as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"Gemini Vision request failed: {exc}", retryable=True) from exc

        if response.status_code == 429:
            body = response.text
            if _is_quota_exhausted_response(body):
                _open_gemini_circuit("quota exhausted (HTTP 429)")
                raise ProviderCallError(
                    f"Gemini quota exhausted — circuit breaker opened: {body[:200]}",
                    retryable=True,
                )
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"Gemini Vision rate limited (HTTP 429): {body[:200]}", retryable=True)

        if response.status_code >= 400:
            retryable = _is_retryable_status_code(response.status_code)
            message = f"Gemini Vision HTTP {response.status_code}: {response.text[:300]}"
            if retryable and attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(message, retryable=retryable)

        try:
            payload_json = response.json()
            text = payload_json["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError(f"Gemini Vision response parse failed: {exc}", retryable=False) from exc

        parsed_json = _parse_json_from_text(text)
        if parsed_json is None:
            raise ProviderCallError("Gemini Vision returned non-JSON output", retryable=False)
        print("[receipt_llm] Gemini Vision succeeded")
        return parsed_json

    raise ProviderCallError("Gemini Vision failed after retries", retryable=True)


def call_openrouter_vision(image_base64: str, mime_type: str = "image/jpeg") -> Dict[str, Any]:
    """Send an image to OpenRouter vision model and get structured receipt JSON back."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ProviderCallError("OpenRouter API key not configured", retryable=False)

    model = os.getenv("OPENROUTER_VISION_MODEL", "nvidia/nemotron-nano-12b-v2-vl:free")
    # Vision models on free tier can be slow; use a longer timeout
    timeout = max(_provider_timeout_seconds(), 60.0)
    attempts = _provider_max_attempts()
    prompt = build_image_receipt_prompt()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    site_url = os.getenv("OPENROUTER_SITE_URL", "").strip()
    site_name = os.getenv("OPENROUTER_SITE_NAME", "BillBear").strip()
    if site_url:
        headers["HTTP-Referer"] = site_url
    if site_name:
        headers["X-Title"] = site_name

    data_url = f"data:{mime_type};base64,{image_base64}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict JSON formatter for receipt data."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0,
    }

    for attempt_index in range(attempts):
        print(f"[receipt_llm] OpenRouter Vision attempt {attempt_index + 1}/{attempts} (model={model})")
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except requests.Timeout as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"OpenRouter Vision timeout: {exc}", retryable=True) from exc
        except requests.RequestException as exc:
            if attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(f"OpenRouter Vision request failed: {exc}", retryable=True) from exc

        if response.status_code >= 400:
            retryable = _is_retryable_status_code(response.status_code)
            message = f"OpenRouter Vision HTTP {response.status_code}: {response.text[:300]}"
            if retryable and attempt_index < attempts - 1:
                time.sleep(1.5 * (attempt_index + 1))
                continue
            raise ProviderCallError(message, retryable=retryable)

        try:
            payload_json = response.json()
            content = payload_json["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError(f"OpenRouter Vision response parse failed: {exc}", retryable=False) from exc

        if isinstance(content, list):
            text = "".join(
                str(chunk.get("text", "")) for chunk in content if isinstance(chunk, dict)
            ).strip()
        else:
            text = str(content).strip()

        parsed_json = _parse_json_from_text(text)
        if parsed_json is None:
            raise ProviderCallError("OpenRouter Vision returned non-JSON output", retryable=False)

        print("[receipt_llm] OpenRouter Vision succeeded")
        return parsed_json

    raise ProviderCallError("OpenRouter Vision failed after retries", retryable=True)
