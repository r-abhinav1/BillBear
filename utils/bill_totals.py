"""
The money part of a bill other than its dishes: currency, subtotal, total,
and a list of extra rows (taxes, service, packing, tip, discounts).

Stored in rooms.ocr_data as:

    {"currency": "JPY", "subtotal": 2600.0, "total": 2574.0,
     "charges": [{"label": "消費税", "amount": 234.0, "kind": "charge"},
                 {"label": "Discount", "amount": 260.0, "kind": "discount"}],
     "taxSplit": "proportional", "restaurant": ..., "date": ..., "time": ...}

Rooms and saved scan files from before this had fixed fields instead
(serviceCharge, cgst, sgst, igst, discount, all "₹12.34" strings) and no
currency. `read_bill` turns either shape into the same thing, so nothing else
has to care which one it got.
"""
import re
import unicodedata
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from .money import Currency, amount_or_zero, get_currency, parse_amount, parse_money, to_minor

CHARGE = "charge"
DISCOUNT = "discount"
KINDS = (CHARGE, DISCOUNT)
MAX_CHARGES = 12
LABEL_MAX = 40

# The fixed fields older rooms and saved scans have, as rows.
LEGACY_FIELDS = (
    ("serviceCharge", "Service charge", CHARGE),
    ("cgst", "CGST", CHARGE),
    ("sgst", "SGST", CHARGE),
    ("igst", "IGST", CHARGE),
    ("discount", "Discount", DISCOUNT),
)

# Rows an empty edit screen starts with (left empty, they are not saved).
STARTER_ROWS = {
    "INR": [("CGST", CHARGE), ("SGST", CHARGE), ("Service charge", CHARGE)],
    "*": [("Tax", CHARGE)],
}

# CGST / SGST / IGST / UTGST / GST, and the GSTIN registration number.
_GST = re.compile(r"(?<![A-Z])(?:[CSI]|UT)?GST(?:IN)?(?![A-Z])", re.IGNORECASE)
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def starter_rows(currency_code: str) -> List[Dict[str, str]]:
    rows = STARTER_ROWS.get(currency_code, STARTER_ROWS["*"])
    return [{"label": label, "kind": kind} for label, kind in rows]


def has_gst_marker(labels: Iterable[str]) -> bool:
    return any(_GST.search(label or "") for label in labels)


def normalize_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    return DISCOUNT if text in {"discount", "-", "minus", "less", "offer"} else CHARGE


def clean_label(value: Any, kind: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _WS.sub(" ", _CONTROL.sub("", text)).strip()[:LABEL_MAX].strip()
    if not text or text.upper() == "N/A":
        return "Discount" if kind == DISCOUNT else "Charge"
    return text


def _percent_of(raw: Any, base: Optional[Decimal], currency: Currency) -> Any:
    """ "5%" -> 5% of base, rounded to the currency. Anything else is returned unchanged."""
    if not isinstance(raw, str) or not raw.strip().endswith("%"):
        return raw
    rate = parse_amount(raw.strip()[:-1])
    if rate is None or base is None:
        raise ValueError(f"'{raw}' needs the dishes to be filled in first.")
    return to_minor(base * rate / 100, currency)


def clean_charges(
    rows: Any, currency: Currency, strict: bool = False, percent_base: Optional[Decimal] = None
) -> List[Dict[str, Any]]:
    """
    Validate [{label, amount, kind}] for one currency. Returns
    [{label, amount: Decimal, kind}].

    Rows without an amount, or with 0, are dropped (that's how the empty
    starter rows go away). A negative amount on a charge row is read as a
    discount of that size. With strict (the host's own form), an unreadable
    amount, one with too many decimals for the currency, or too many rows
    raises ValueError; otherwise (model output) such rows are dropped.
    An amount like "5%" is that share of percent_base (the subtotal).
    """
    if not isinstance(rows, list):
        return []
    cleaned = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        kind = normalize_kind(raw.get("kind"))
        try:
            amount = parse_money(_percent_of(raw.get("amount"), percent_base, currency), currency, strict=strict)
        except ValueError as exc:
            if strict:
                label = clean_label(raw.get("label"), kind)
                raise ValueError(f"{label}: {exc}")
            continue
        if amount is None or amount == 0:
            continue
        if amount < 0:
            amount, kind = -amount, DISCOUNT
        cleaned.append({"label": clean_label(raw.get("label"), kind), "amount": amount, "kind": kind})
    if len(cleaned) > MAX_CHARGES:
        if strict:
            raise ValueError(f"A bill can have at most {MAX_CHARGES} taxes, charges and discounts.")
        cleaned = cleaned[:MAX_CHARGES]
    return cleaned


def legacy_charges(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rows for the old fixed fields: "₹40.00" service charge -> a Service charge row."""
    rows = []
    for field, label, kind in LEGACY_FIELDS:
        amount = amount_or_zero(data.get(field))
        if amount:
            rows.append({"label": label, "amount": abs(amount), "kind": kind})
    return rows


def _number_or_none(value: Any) -> Optional[float]:
    try:
        amount = parse_amount(value)
    except ValueError:
        return None
    return None if amount is None else float(amount)


def read_bill(ocr_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Either stored shape -> {currency: Currency, subtotal, total (float or None),
    charges: [{label, amount: float, kind}], tax_split}.
    """
    data = ocr_data or {}
    currency = get_currency(data.get("currency"))
    if isinstance(data.get("charges"), list):
        charges = []
        for row in data["charges"]:
            if not isinstance(row, dict):
                continue
            amount = amount_or_zero(row.get("amount"))
            if not amount:
                continue
            kind = normalize_kind(row.get("kind"))
            if amount < 0:
                amount, kind = -amount, DISCOUNT
            charges.append({"label": clean_label(row.get("label"), kind), "amount": amount, "kind": kind})
    else:
        charges = legacy_charges(data)
    return {
        "currency": currency,
        "subtotal": _number_or_none(data.get("subtotal")),
        "total": _number_or_none(data.get("total")),
        "charges": charges,
        "tax_split": "equal" if data.get("taxSplit") == "equal" else "proportional",
    }


def stored_charges(charges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cleaned rows (Decimal amounts) -> what goes in ocr_data (JSON numbers)."""
    return [{"label": c["label"], "amount": float(c["amount"]), "kind": c["kind"]} for c in charges]


def as_number(amount: Any) -> Optional[float]:
    return None if amount is None else float(amount)


def bill_signature(ocr_data: Optional[Dict[str, Any]]) -> tuple:
    """
    What people's shares depend on, apart from the dishes and the tax-split
    switch: the currency and the charge rows. The printed subtotal and total
    are only checked against, never charged, so fixing them changes nobody's
    share and nobody is sent back to pick again. The same bill in the old or
    new stored shape gives the same value.
    """
    bill = read_bill(ocr_data)
    return (
        bill["currency"].code,
        tuple((c["label"], round(c["amount"], 6), c["kind"]) for c in bill["charges"]),
    )


def legacy_field_names() -> List[str]:
    return [field for field, _label, _kind in LEGACY_FIELDS]
