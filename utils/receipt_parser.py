import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .receipt_contracts import (
    MISSING_VALUE,
    format_currency,
    is_missing_value,
    normalize_receipt_payload,
    parse_numeric_amount,
)

DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
)
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?(?:AM|PM|am|pm))?\b")
AMOUNT_TOKEN_PATTERN = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?")
ITEM_LINE_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 '&().,+/\-]{1,}?)\s+"
    r"(?:(?P<qty>\d+(?:\.\d+)?)\s+)?"
    r"(?P<amount>-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)$"
)

NOISE_KEYWORDS = {
    "duplicate",
    "print",
    "table",
    "bill",
    "date",
    "time",
    "reprint",
    "customer",
    "mobile",
    "covers",
    "gstin",
    "fssai",
    "feedback",
    "powered",
    "mail",
    "type",
    "phone",
    "ph:",
    "subtotal",
    "total",
    "round off",
    "service",
    "discount",
    "cgst",
    "sgst",
    "igst",
    "qty",
}


def parse_receipt_text(
    raw_text: str,
    lines: Optional[Iterable[str]] = None,
    ocr_confidence: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    prepared_lines = _prepare_lines(raw_text, lines)

    parsed: Dict[str, Any] = {
        "restaurant": _extract_restaurant_name(prepared_lines),
        "date": _extract_date(prepared_lines),
        "time": _extract_time(prepared_lines),
        "items": _extract_items(prepared_lines),
        "subtotal": MISSING_VALUE,
        "serviceCharge": MISSING_VALUE,
        "discount": MISSING_VALUE,
        "cgst": MISSING_VALUE,
        "sgst": MISSING_VALUE,
        "igst": MISSING_VALUE,
        "total": MISSING_VALUE,
    }
    parsed.update(_extract_financials(prepared_lines))

    if is_missing_value(parsed.get("subtotal")) and parsed["items"]:
        item_total = sum(parse_numeric_amount(item.get("price")) or 0.0 for item in parsed["items"])
        parsed["subtotal"] = format_currency(item_total)

    if is_missing_value(parsed.get("total")):
        total_guess = _estimate_total_from_components(parsed)
        if total_guess is not None:
            parsed["total"] = format_currency(total_guess)

    normalized = normalize_receipt_payload(parsed)
    confidence, unresolved = _score_parser_result(normalized, ocr_confidence)
    parser_meta: Dict[str, Any] = {
        "source": "deterministic_parser",
        "confidence": confidence,
        "unresolved_fields": unresolved,
        "line_count": len(prepared_lines),
    }
    if metadata:
        parser_meta["input_metadata"] = metadata

    normalized["parser_meta"] = parser_meta
    return normalized


def _prepare_lines(raw_text: str, lines: Optional[Iterable[str]]) -> List[str]:
    candidate_lines: List[str] = []
    if lines:
        for line in lines:
            if isinstance(line, str):
                candidate_lines.append(line)

    if raw_text:
        candidate_lines.extend(raw_text.splitlines())

    cleaned_lines: List[str] = []
    for raw_line in candidate_lines:
        cleaned = (
            raw_line.replace("|", " ")
            .replace("₹", " ")
            .replace("\t", " ")
            .replace("—", "-")
            .strip()
        )
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned:
            cleaned_lines.append(cleaned)
    return cleaned_lines


def _extract_restaurant_name(lines: Sequence[str]) -> str:
    for line in lines[:12]:
        lowered = line.lower()
        if any(keyword in lowered for keyword in ("duplicate print", "bill no", "table", "gstin", "fssai")):
            continue
        alpha_count = sum(ch.isalpha() for ch in line)
        digit_count = sum(ch.isdigit() for ch in line)
        if alpha_count < 3:
            continue
        if digit_count > alpha_count:
            continue
        return line.strip()
    return MISSING_VALUE


def _extract_date(lines: Sequence[str]) -> str:
    for line in lines:
        for pattern in DATE_PATTERNS:
            match = pattern.search(line)
            if match:
                return match.group(0)
    return MISSING_VALUE


def _extract_time(lines: Sequence[str]) -> str:
    for line in lines:
        match = TIME_PATTERN.search(line)
        if match:
            return match.group(0)
    return MISSING_VALUE


def _extract_amount_from_line(line: str) -> Optional[float]:
    matches = AMOUNT_TOKEN_PATTERN.findall(line)
    if not matches:
        return None
    try:
        return float(matches[-1].replace(",", ""))
    except ValueError:
        return None


def _extract_financials(lines: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {
        "subtotal": MISSING_VALUE,
        "serviceCharge": MISSING_VALUE,
        "discount": MISSING_VALUE,
        "cgst": MISSING_VALUE,
        "sgst": MISSING_VALUE,
        "igst": MISSING_VALUE,
        "total": MISSING_VALUE,
    }
    total_candidates: List[Tuple[int, int, float]] = []

    for index, line in enumerate(lines):
        lowered = line.lower()
        amount = _extract_amount_from_line(line)
        if amount is None:
            continue

        if ("sub total" in lowered or "subtotal" in lowered) and "qty" not in lowered:
            result["subtotal"] = format_currency(amount)
            continue

        if "service charge" in lowered or "service tax" in lowered:
            result["serviceCharge"] = format_currency(amount)
            continue

        if "discount" in lowered:
            result["discount"] = format_currency(abs(amount))
            continue

        if "cgst" in lowered:
            result["cgst"] = format_currency(amount)
            continue

        if "sgst" in lowered:
            result["sgst"] = format_currency(amount)
            continue

        if "igst" in lowered:
            result["igst"] = format_currency(amount)
            continue

        if "gst" in lowered and all(token not in lowered for token in ("cgst", "sgst", "igst")):
            result["igst"] = format_currency(amount)
            continue

        total_priority = _total_line_priority(lowered)
        if total_priority > 0:
            total_candidates.append((total_priority, index, amount))

    if total_candidates:
        best = sorted(total_candidates, key=lambda value: (value[0], value[1]))[-1]
        result["total"] = format_currency(best[2])

    return result


def _total_line_priority(line: str) -> int:
    if "subtotal" in line or "sub total" in line:
        return 0
    if "pay" in line:
        return 4
    if "total invoice value" in line:
        return 3
    if "grand total" in line:
        return 2
    if "total" in line:
        return 1
    return 0


def _should_ignore_item_candidate(name: str) -> bool:
    lowered = name.lower()
    return any(keyword in lowered for keyword in NOISE_KEYWORDS)


def _is_potential_item_prefix(line: str) -> bool:
    if any(char.isdigit() for char in line):
        return False
    lowered = line.lower()
    if _should_ignore_item_candidate(lowered):
        return False
    words = line.split()
    return 1 <= len(words) <= 4 and all(len(word) > 1 for word in words)


def _extract_items(lines: Sequence[str]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    pending_prefix = ""
    item_section_started = False

    for line in lines:
        lowered = line.lower()
        if "item" in lowered and ("qty" in lowered or "amt" in lowered or "amount" in lowered):
            item_section_started = True
            pending_prefix = ""
            continue

        line_match = ITEM_LINE_PATTERN.match(line)
        if line_match:
            raw_name = line_match.group("name").strip(" -:")
            if pending_prefix and (item_section_started or items):
                raw_name = f"{pending_prefix} {raw_name}".strip()
                pending_prefix = ""

            if _should_ignore_item_candidate(raw_name):
                continue

            amount = parse_numeric_amount(line_match.group("amount"))
            if amount is None or amount <= 0:
                continue

            items.append({"name": raw_name, "price": format_currency(amount)})
            item_section_started = True
            continue

        if _is_potential_item_prefix(line) and (item_section_started or items):
            pending_prefix = f"{pending_prefix} {line}".strip() if pending_prefix else line
        else:
            pending_prefix = ""

    # Remove duplicate items caused by noisy OCR repeated lines.
    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        key = (item["name"].lower(), item["price"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def _estimate_total_from_components(receipt: Dict[str, Any]) -> Optional[float]:
    subtotal = parse_numeric_amount(receipt.get("subtotal"))
    if subtotal is None:
        return None

    service_charge = parse_numeric_amount(receipt.get("serviceCharge")) or 0.0
    discount = parse_numeric_amount(receipt.get("discount")) or 0.0
    cgst = parse_numeric_amount(receipt.get("cgst")) or 0.0
    sgst = parse_numeric_amount(receipt.get("sgst")) or 0.0
    igst = parse_numeric_amount(receipt.get("igst")) or 0.0

    estimated_total = subtotal + service_charge + cgst + sgst + igst - discount
    return max(estimated_total, 0.0)


def _score_parser_result(receipt: Dict[str, Any], ocr_confidence: Optional[float]) -> Tuple[float, List[str]]:
    unresolved_fields: List[str] = []
    score = 0.0

    items = receipt.get("items", [])
    if items:
        score += min(len(items), 8) / 8 * 0.45
    else:
        unresolved_fields.append("items")

    subtotal = parse_numeric_amount(receipt.get("subtotal"))
    total = parse_numeric_amount(receipt.get("total"))
    service_charge = parse_numeric_amount(receipt.get("serviceCharge"))
    discount = parse_numeric_amount(receipt.get("discount"))
    cgst = parse_numeric_amount(receipt.get("cgst"))
    sgst = parse_numeric_amount(receipt.get("sgst"))
    igst = parse_numeric_amount(receipt.get("igst"))

    if subtotal is not None:
        score += 0.15
    else:
        unresolved_fields.append("subtotal")

    if total is not None:
        score += 0.20
    else:
        unresolved_fields.append("total")

    if any(value is not None for value in (service_charge, discount, cgst, sgst, igst)):
        score += 0.10

    item_sum = sum(parse_numeric_amount(item.get("price")) or 0.0 for item in items)
    if subtotal is not None and item_sum > 0:
        if abs(item_sum - subtotal) <= max(8.0, subtotal * 0.08):
            score += 0.10

    if total is not None and subtotal is not None:
        implied_total = subtotal + (service_charge or 0.0) + (cgst or 0.0) + (sgst or 0.0) + (igst or 0.0) - (discount or 0.0)
        if abs(implied_total - total) <= max(10.0, total * 0.08):
            score += 0.10

    if ocr_confidence is not None:
        bounded_confidence = max(0.0, min(float(ocr_confidence), 100.0)) / 100.0
        score = (score * 0.8) + (bounded_confidence * 0.2)

    return round(max(0.0, min(score, 1.0)), 2), unresolved_fields
