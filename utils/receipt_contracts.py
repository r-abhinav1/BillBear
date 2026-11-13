import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .bill_totals import clean_charges, has_gst_marker, legacy_charges, stored_charges
from .money import DEFAULT_CURRENCY, get_currency, is_supported, parse_money

MISSING_VALUE = "N/A"

# Dietary class of a line item. "other" covers non-food lines (cover charge,
# board games) and anything the scanner could not tell.
ITEM_CATEGORIES = ("veg", "nonveg", "egg", "alcohol", "other")
DEFAULT_ITEM_CATEGORY = "other"
_CATEGORY_ALIASES = {
    "vegetarian": "veg", "vegan": "veg", "jain": "veg",
    "nonvegetarian": "nonveg", "meat": "nonveg", "chicken": "nonveg", "fish": "nonveg", "seafood": "nonveg",
    "eggetarian": "egg", "eggs": "egg",
    "alcoholic": "alcohol", "liquor": "alcohol", "beer": "alcohol", "wine": "alcohol",
    "unknown": "other", "na": "other", "none": "other",
}


def normalize_item_quantity(value: Any) -> int:
    """Quantity printed on the bill for a line; 1 when missing or unreadable."""
    if isinstance(value, bool) or value is None:
        return 1
    match = re.search(r"\d+", str(value))
    if not match:
        return 1
    return min(max(int(match.group()), 1), 99)


def normalize_item_category(value: Any) -> str:
    """Map whatever a model or form sent ("Non-Veg", "ALCOHOLIC", None) to an ITEM_CATEGORIES value."""
    if not isinstance(value, str):
        return DEFAULT_ITEM_CATEGORY
    key = re.sub(r"[^a-z]", "", value.lower())
    key = _CATEGORY_ALIASES.get(key, key)
    return key if key in ITEM_CATEGORIES else DEFAULT_ITEM_CATEGORY


def is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return text == "" or text.upper() == MISSING_VALUE
    return False


def _normalize_text_field(value: Any) -> str:
    if is_missing_value(value):
        return MISSING_VALUE
    return str(value).strip() or MISSING_VALUE


# Kinds of clue a model may say it used for the currency. Only the ones that
# are printed facts (a symbol or code, tax labels, an address or phone number)
# can settle "rupees" without asking; language alone can't (Hindi is also
# written in Nepal, English everywhere).
CURRENCY_EVIDENCE = ("symbol", "tax_labels", "language", "address")
_SETTLES_INR = {"symbol", "tax_labels", "address"}
REASON_MAX = 120


def _clean_reason(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", "", value)).strip()[:REASON_MAX]


def normalize_currency(raw: Any, charges: List[Dict[str, Any]], trust_confirmation: bool) -> Dict[str, Any]:
    """
    {code, confirmed, reason, alternatives} for a receipt.

    `confirmed` says whether the host still has to say yes to the currency
    before the room is created. Rupees are settled without asking when the
    bill shows GST lines, or the model saw a printed rupee sign, tax labels
    or an Indian address. Any other currency, or rupees on a guess, needs one
    tap. What the model says about its own confidence is not used.

    With trust_confirmation (the host's own browser posting a receipt it
    already confirmed, or a saved scan file), a "confirmed" flag is kept, and
    a receipt with no currency at all is an older rupee one.
    """
    if raw is None and trust_confirmation:
        return {"code": DEFAULT_CURRENCY, "confirmed": True, "reason": "", "alternatives": []}
    if isinstance(raw, str):
        raw = {"code": raw}
    if not isinstance(raw, dict):
        raw = {}
    code = raw.get("code")
    code = code.strip().upper() if is_supported(code) else DEFAULT_CURRENCY
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    evidence = {e.strip().lower() for e in evidence if isinstance(e, str)}
    alternatives: List[str] = []
    for alt in raw.get("alternatives") if isinstance(raw.get("alternatives"), list) else []:
        if is_supported(alt) and alt.strip().upper() not in alternatives + [code]:
            alternatives.append(alt.strip().upper())
    if trust_confirmation and raw.get("confirmed") is True:
        confirmed = True
    else:
        confirmed = code == DEFAULT_CURRENCY and (
            bool(evidence & _SETTLES_INR) or has_gst_marker(c["label"] for c in charges)
        )
    return {
        "code": code,
        "confirmed": confirmed,
        "reason": _clean_reason(raw.get("reason")),
        "alternatives": alternatives[:2],
    }


def _plain_number(amount: Optional[Decimal]) -> Optional[float]:
    return None if amount is None else float(amount)


def normalize_receipt_payload(payload: Optional[Dict[str, Any]], trust_confirmation: bool = False) -> Dict[str, Any]:
    """
    Whatever a model returned, or a saved scan file holds (new or old shape),
    -> one receipt shape:

        {restaurant, date, time, currency: {code, confirmed, reason, alternatives},
         items: [{name, price: number|None, category, quantity}],
         subtotal, total: number|None, charges: [{label, amount, kind}]}

    Amounts are rounded to the receipt's currency. Older payloads with the
    fixed serviceCharge/cgst/sgst/igst/discount fields become charge rows.
    """
    source = payload or {}
    normalized: Dict[str, Any] = {
        "restaurant": _normalize_text_field(source.get("restaurant")),
        "date": _normalize_text_field(source.get("date")),
        "time": _normalize_text_field(source.get("time")),
    }

    raw_charges = source.get("charges") if isinstance(source.get("charges"), list) else legacy_charges(source)
    # Code first (to know the decimals), then whether it is settled (which may
    # depend on the charge labels).
    raw_currency = source.get("currency")
    cur = get_currency(raw_currency.get("code") if isinstance(raw_currency, dict) else raw_currency)
    charges = clean_charges(raw_charges, cur)
    normalized["currency"] = normalize_currency(raw_currency, charges, trust_confirmation)

    items: List[Dict[str, Any]] = []
    raw_items = source.get("items", [])
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if isinstance(raw_item, dict):
                name = _normalize_text_field(raw_item.get("name"))
                raw_price = raw_item.get("price")
                category = normalize_item_category(raw_item.get("category"))
                quantity = normalize_item_quantity(raw_item.get("quantity"))
            else:
                name = _normalize_text_field(raw_item)
                raw_price = None
                category = DEFAULT_ITEM_CATEGORY
                quantity = 1
            if name == MISSING_VALUE:
                continue
            try:
                price = _plain_number(parse_money(raw_price, cur))
            except ValueError:
                price = None
            items.append({"name": name, "price": price, "category": category, "quantity": quantity})
    normalized["items"] = items

    for field in ("subtotal", "total"):
        try:
            normalized[field] = _plain_number(parse_money(source.get(field), cur))
        except ValueError:
            normalized[field] = None
    normalized["charges"] = stored_charges(charges)

    if source.get("taxSplit") in ("equal", "proportional"):
        normalized["taxSplit"] = source["taxSplit"]

    parser_meta = source.get("parser_meta")
    if isinstance(parser_meta, dict):
        normalized["parser_meta"] = parser_meta

    extraction_meta = source.get("extraction_meta")
    if isinstance(extraction_meta, dict):
        normalized["extraction_meta"] = extraction_meta

    return normalized
