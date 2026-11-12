"""
Vision-only receipt extraction under one overall deadline.

Policy:
  1. Gemini with one random key.
  2. A second Gemini key only on a retryable failure (429/5xx/timeout) and
     only if at least MIN_ATTEMPT_SECONDS remain.
  3. OpenRouter vision (best effort) with whatever budget is left.
  4. Otherwise raise VisionFailed; the UI offers Retry / enter items manually.
"""
import os
import time
from typing import Any, Callable, Dict, List, Optional

from . import receipt_llm
from .receipt_contracts import normalize_receipt_payload

MIN_ATTEMPT_SECONDS = 3.0
SAFETY_MARGIN = 0.5


class VisionFailed(Exception):
    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or []


class VisionUnavailable(VisionFailed):
    """No provider can be used right now (not configured, or billing/quota problem)."""


def vision_budget_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("VISION_BUDGET_SECONDS", "25")))
    except ValueError:
        return 25.0


def _per_call_cap() -> float:
    try:
        return max(3.0, float(os.getenv("VISION_CALL_TIMEOUT_SECONDS", "15")))
    except ValueError:
        return 15.0


def _usable(receipt: Dict[str, Any]) -> bool:
    return bool(receipt.get("items"))


def extract_receipt_from_vision(
    image_base64: str,
    mime_type: str = "image/jpeg",
    budget_seconds: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    start = clock()
    deadline = start + (budget_seconds if budget_seconds is not None else vision_budget_seconds())
    errors: List[str] = []
    billing_failure = False
    got_model_answer = False

    def remaining() -> float:
        return deadline - clock()

    def finish(raw: Dict[str, Any], provider: str) -> Dict[str, Any]:
        normalized = normalize_receipt_payload(raw)
        normalized["extraction_meta"] = {"mode": "vision", "source": "vision_llm", "provider": provider}
        normalized["parser_meta"] = {
            "source": "vision_llm",
            "provider": provider,
            "latency_ms": round((clock() - start) * 1000),
            "errors": errors or None,
        }
        return normalized

    has_gemini = receipt_llm.has_gemini_credentials()
    has_openrouter = receipt_llm.has_openrouter_credentials()
    if not has_gemini and not has_openrouter:
        raise VisionUnavailable("Receipt scanning is not configured on this server.")

    if has_gemini and not receipt_llm.gemini_circuit_is_open():
        keys = receipt_llm.shuffled_gemini_keys()[:2]
        for attempt, key in enumerate(keys):
            left = remaining() - SAFETY_MARGIN
            if attempt > 0 and left < MIN_ATTEMPT_SECONDS:
                break
            if left <= 0:
                break
            try:
                raw = receipt_llm.call_gemini_vision(image_base64, mime_type, key, timeout=min(left, _per_call_cap()))
                got_model_answer = True
                result = finish(raw, "gemini_vision")
                if _usable(result):
                    return result
                errors.append("Gemini found no line items")
                break  # a different key won't see a different image
            except receipt_llm.ProviderCallError as exc:
                errors.append(str(exc))
                billing_failure = billing_failure or exc.billing
                if not exc.retryable:
                    break
    elif has_gemini:
        errors.append("Gemini skipped: circuit open (quota/billing)")
        billing_failure = True

    if has_openrouter:
        left = remaining() - SAFETY_MARGIN
        if left >= MIN_ATTEMPT_SECONDS:
            try:
                raw = receipt_llm.call_openrouter_vision(image_base64, mime_type, timeout=left)
                got_model_answer = True
                result = finish(raw, "openrouter_vision")
                if _usable(result):
                    return result
                errors.append("OpenRouter found no line items")
            except receipt_llm.ProviderCallError as exc:
                errors.append(str(exc))
        else:
            errors.append("OpenRouter skipped: out of time")

    print(f"[receipt_pipeline] vision failed: {errors}")
    if billing_failure and not got_model_answer:
        # Every provider refused for account reasons: say so instead of blaming the photo.
        print("[receipt_pipeline] ACTION NEEDED: vision provider billing problem (top up / fix billing in AI Studio)")
        raise VisionUnavailable("Bill scanning is temporarily unavailable.", errors)
    raise VisionFailed("We couldn't read items from that photo.", errors)
