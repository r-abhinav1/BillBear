import os
import time
from typing import Any, Dict, List, Tuple

from .receipt_contracts import normalize_receipt_payload, validate_receipt_payload
from .receipt_llm import (
    ProviderCallError,
    call_gemini_text_normalizer,
    call_gemini_vision,
    call_openrouter_text_normalizer,
    call_openrouter_vision,
    gemini_circuit_is_open,
    has_gemini_credentials,
    has_openrouter_credentials,
)
from .receipt_parser import parse_receipt_text

ESSENTIAL_FIELDS = {"items", "subtotal", "total"}


def validate_ocr_text_payload(payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return False, ["OCR payload must be a JSON object"]

    raw_text = payload.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        errors.append("Field 'raw_text' is required and must be non-empty text")

    lines = payload.get("lines")
    if lines is not None and not isinstance(lines, list):
        errors.append("Field 'lines' must be a string list when provided")

    return len(errors) == 0, errors


def _fallback_threshold() -> float:
    try:
        return max(0.0, min(float(os.getenv("PARSER_CONFIDENCE_THRESHOLD", "0.72")), 1.0))
    except ValueError:
        return 0.72


def _always_use_llm() -> bool:
    value = os.getenv("ALWAYS_USE_LLM_NORMALIZATION", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _needs_fallback(parser_result: Dict[str, Any]) -> bool:
    if _always_use_llm():
        return True

    parser_meta = parser_result.get("parser_meta", {})
    confidence = float(parser_meta.get("confidence", 0.0))
    unresolved_fields = set(parser_meta.get("unresolved_fields", []))

    if confidence < _fallback_threshold():
        return True
    if unresolved_fields.intersection(ESSENTIAL_FIELDS):
        return True
    return False


def _enrich_meta(
    receipt: Dict[str, Any],
    source: str,
    used_fallback: bool,
    fallback_provider: str = "none",
    fallback_errors: List[str] = None,
    base_parser_meta: Dict[str, Any] = None,
) -> Dict[str, Any]:
    enriched = normalize_receipt_payload(receipt)
    parser_meta = dict(base_parser_meta or enriched.get("parser_meta", {}))
    parser_meta["used_fallback"] = used_fallback
    parser_meta["fallback_provider"] = fallback_provider
    if fallback_errors:
        parser_meta["fallback_errors"] = fallback_errors
    enriched["parser_meta"] = parser_meta
    enriched["extraction_meta"] = {
        "mode": "ocr_text",
        "source": source,
        "provider": fallback_provider if used_fallback else "none",
    }
    return enriched


def llm_fallback_if_needed(raw_text: str, parser_result: Dict[str, Any]) -> Dict[str, Any]:
    base_parser_meta = dict(parser_result.get("parser_meta", {}))
    if not _needs_fallback(parser_result):
        return _enrich_meta(
            parser_result,
            source="deterministic_parser",
            used_fallback=False,
            base_parser_meta=base_parser_meta,
        )

    fallback_errors: List[str] = []
    gemini_available = has_gemini_credentials()
    openrouter_available = has_openrouter_credentials()

    if not gemini_available and not openrouter_available:
        return _enrich_meta(
            parser_result,
            source="deterministic_parser",
            used_fallback=False,
            fallback_errors=["LLM fallback skipped: no provider credentials configured"],
            base_parser_meta=base_parser_meta,
        )

    if gemini_available:
        if gemini_circuit_is_open():
            fallback_errors.append("Gemini skipped: circuit breaker open (quota exhausted) — using OpenRouter directly")
            print("[receipt_pipeline] Gemini circuit breaker open — going directly to OpenRouter")
        else:
            try:
                gemini_result = call_gemini_text_normalizer(raw_text)
                normalized = normalize_receipt_payload(gemini_result)
                valid, validation_errors = validate_receipt_payload(normalized)
                if valid:
                    return _enrich_meta(
                        normalized,
                        source="llm_fallback",
                        used_fallback=True,
                        fallback_provider="gemini",
                        base_parser_meta=base_parser_meta,
                    )
                fallback_errors.extend([f"Gemini validation: {error}" for error in validation_errors])
            except ProviderCallError as exc:
                fallback_errors.append(f"Gemini: {exc}")
    else:
        fallback_errors.append("Gemini skipped: credentials not configured")

    if openrouter_available:
        print("[receipt_pipeline] Trying OpenRouter fallback")
        try:
            openrouter_result = call_openrouter_text_normalizer(raw_text)
            normalized = normalize_receipt_payload(openrouter_result)
            valid, validation_errors = validate_receipt_payload(normalized)
            if valid:
                return _enrich_meta(
                    normalized,
                    source="llm_fallback",
                    used_fallback=True,
                    fallback_provider="openrouter",
                    fallback_errors=fallback_errors,
                    base_parser_meta=base_parser_meta,
                )
            fallback_errors.extend([f"OpenRouter validation: {error}" for error in validation_errors])
            print(f"[receipt_pipeline] OpenRouter result failed validation: {validation_errors}")
        except ProviderCallError as exc:
            fallback_errors.append(f"OpenRouter: {exc}")
            print(f"[receipt_pipeline] OpenRouter call failed: {exc}")
    else:
        fallback_errors.append("OpenRouter skipped: credentials not configured")

    return _enrich_meta(
        parser_result,
        source="deterministic_parser",
        used_fallback=False,
        fallback_errors=fallback_errors,
        base_parser_meta=base_parser_meta,
    )


def extract_receipt_from_text_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    # If the payload contains a pre-extracted vision receipt, use it directly
    vision_receipt = payload.get("_vision_receipt")
    if isinstance(vision_receipt, dict) and vision_receipt.get("items"):
        normalized = normalize_receipt_payload(vision_receipt)
        valid, errors = validate_receipt_payload(normalized)
        if valid:
            # Preserve meta from the vision call
            for key in ("parser_meta", "extraction_meta"):
                if key in vision_receipt:
                    normalized[key] = vision_receipt[key]
            return normalized

    valid, errors = validate_ocr_text_payload(payload)
    if not valid:
        raise ValueError("; ".join(errors))

    t0 = time.monotonic()

    raw_text = payload["raw_text"].strip()
    lines = payload.get("lines")
    confidence = payload.get("confidence")
    metadata = payload.get("metadata")

    t_parse = time.monotonic()
    parser_result = parse_receipt_text(raw_text, lines=lines, ocr_confidence=confidence, metadata=metadata)
    parse_ms = round((time.monotonic() - t_parse) * 1000)

    t_llm = time.monotonic()
    result = llm_fallback_if_needed(raw_text, parser_result)
    llm_ms = round((time.monotonic() - t_llm) * 1000)

    total_ms = round((time.monotonic() - t0) * 1000)

    # Attach server-side latency info
    result.setdefault("parser_meta", {})
    result["parser_meta"]["latency"] = {
        "parse_ms": parse_ms,
        "llm_ms": llm_ms,
        "total_server_ms": total_ms,
    }
    # Carry forward client timings if present
    client_timings = (metadata or {}).get("timings")
    if client_timings:
        result["parser_meta"]["latency"]["client_timings"] = client_timings

    print(f"[receipt_pipeline] Latency: parse={parse_ms}ms llm={llm_ms}ms total_server={total_ms}ms")
    return result


def extract_receipt_from_vision(image_base64: str, mime_type: str = "image/jpeg") -> Dict[str, Any]:
    """Vision LLM fallback — send image directly to a vision model when OCR fails."""
    t0 = time.monotonic()
    fallback_errors: List[str] = []
    gemini_available = has_gemini_credentials()
    openrouter_available = has_openrouter_credentials()

    if not gemini_available and not openrouter_available:
        raise ValueError("No vision provider credentials configured")

    # Try Gemini Vision first
    if gemini_available:
        if gemini_circuit_is_open():
            fallback_errors.append("Gemini Vision skipped: circuit breaker open")
            print("[receipt_pipeline] Gemini circuit breaker open — skipping to OpenRouter Vision")
        else:
            try:
                result = call_gemini_vision(image_base64, mime_type)
                normalized = normalize_receipt_payload(result)
                valid, validation_errors = validate_receipt_payload(normalized)
                if valid:
                    total_ms = round((time.monotonic() - t0) * 1000)
                    normalized["parser_meta"] = {
                        "source": "vision_llm",
                        "used_fallback": True,
                        "fallback_provider": "gemini_vision",
                        "latency": {"total_server_ms": total_ms},
                    }
                    normalized["extraction_meta"] = {
                        "mode": "vision",
                        "source": "vision_llm",
                        "provider": "gemini_vision",
                    }
                    print(f"[receipt_pipeline] Gemini Vision succeeded ({total_ms}ms)")
                    return normalized
                fallback_errors.extend([f"Gemini Vision validation: {e}" for e in validation_errors])
            except ProviderCallError as exc:
                fallback_errors.append(f"Gemini Vision: {exc}")
    else:
        fallback_errors.append("Gemini Vision skipped: credentials not configured")

    # Try OpenRouter Vision
    if openrouter_available:
        print("[receipt_pipeline] Trying OpenRouter Vision fallback")
        try:
            result = call_openrouter_vision(image_base64, mime_type)
            normalized = normalize_receipt_payload(result)
            valid, validation_errors = validate_receipt_payload(normalized)
            if valid:
                total_ms = round((time.monotonic() - t0) * 1000)
                normalized["parser_meta"] = {
                    "source": "vision_llm",
                    "used_fallback": True,
                    "fallback_provider": "openrouter_vision",
                    "fallback_errors": fallback_errors if fallback_errors else None,
                    "latency": {"total_server_ms": total_ms},
                }
                normalized["extraction_meta"] = {
                    "mode": "vision",
                    "source": "vision_llm",
                    "provider": "openrouter_vision",
                }
                print(f"[receipt_pipeline] OpenRouter Vision succeeded ({total_ms}ms)")
                return normalized
            fallback_errors.extend([f"OpenRouter Vision validation: {e}" for e in validation_errors])
        except ProviderCallError as exc:
            fallback_errors.append(f"OpenRouter Vision: {exc}")
    else:
        fallback_errors.append("OpenRouter Vision skipped: credentials not configured")

    raise ValueError(f"Vision extraction failed: {'; '.join(fallback_errors)}")
