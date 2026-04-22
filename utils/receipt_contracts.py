import re
from typing import Any, Dict, List, Optional, Tuple

CURRENCY_SYMBOL = "₹"
MISSING_VALUE = "N/A"
AMOUNT_FIELDS = ("subtotal", "serviceCharge", "discount", "cgst", "sgst", "igst", "total")

BASE_RECEIPT_TEMPLATE: Dict[str, Any] = {
    "restaurant": MISSING_VALUE,
    "date": MISSING_VALUE,
    "time": MISSING_VALUE,
    "items": [],
    "subtotal": MISSING_VALUE,
    "serviceCharge": MISSING_VALUE,
    "discount": MISSING_VALUE,
    "cgst": MISSING_VALUE,
    "sgst": MISSING_VALUE,
    "igst": MISSING_VALUE,
    "total": MISSING_VALUE,
}

AMOUNT_PATTERN = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return text == "" or text.upper() == MISSING_VALUE
    return False


def parse_numeric_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(CURRENCY_SYMBOL, "").replace(",", "").strip()
        if not cleaned:
            return None
        matches = AMOUNT_PATTERN.findall(cleaned)
        if not matches:
            return None
        try:
            return float(matches[-1].replace(",", ""))
        except ValueError:
            return None
    return None


def format_currency(amount: float) -> str:
    return f"{CURRENCY_SYMBOL}{amount:.2f}"


def _normalize_text_field(value: Any) -> str:
    if is_missing_value(value):
        return MISSING_VALUE
    return str(value).strip() or MISSING_VALUE


def normalize_receipt_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    source = payload or {}
    normalized = dict(BASE_RECEIPT_TEMPLATE)

    normalized["restaurant"] = _normalize_text_field(source.get("restaurant"))
    normalized["date"] = _normalize_text_field(source.get("date"))
    normalized["time"] = _normalize_text_field(source.get("time"))

    items: List[Dict[str, str]] = []
    raw_items = source.get("items", [])
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if isinstance(raw_item, dict):
                name = _normalize_text_field(raw_item.get("name"))
                raw_price = raw_item.get("price")
            else:
                name = _normalize_text_field(raw_item)
                raw_price = None

            if name == MISSING_VALUE:
                continue

            numeric_price = parse_numeric_amount(raw_price)
            if numeric_price is None:
                if is_missing_value(raw_price):
                    price = MISSING_VALUE
                else:
                    maybe_number = parse_numeric_amount(str(raw_price))
                    price = format_currency(maybe_number) if maybe_number is not None else MISSING_VALUE
            else:
                price = format_currency(numeric_price)

            items.append({"name": name, "price": price})

    normalized["items"] = items

    for field in AMOUNT_FIELDS:
        numeric_value = parse_numeric_amount(source.get(field))
        normalized[field] = format_currency(numeric_value) if numeric_value is not None else MISSING_VALUE

    parser_meta = source.get("parser_meta")
    if isinstance(parser_meta, dict):
        normalized["parser_meta"] = parser_meta

    extraction_meta = source.get("extraction_meta")
    if isinstance(extraction_meta, dict):
        normalized["extraction_meta"] = extraction_meta

    return normalized


def validate_receipt_payload(payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
    receipt = normalize_receipt_payload(payload)
    errors: List[str] = []

    if not receipt["items"]:
        errors.append("No bill items were extracted")
    else:
        for index, item in enumerate(receipt["items"], start=1):
            if is_missing_value(item.get("name")):
                errors.append(f"Item #{index} is missing a name")
            if is_missing_value(item.get("price")) or parse_numeric_amount(item.get("price")) is None:
                errors.append(f"Item '{item.get('name', f'#{index}')}' is missing a valid price")

    if is_missing_value(receipt["subtotal"]):
        errors.append("Subtotal is missing")
    if is_missing_value(receipt["total"]):
        errors.append("Total is missing")

    return len(errors) == 0, errors
