"""
Input validation for anything a user (or an LLM) can put into the database.

Names show up in URLs, HTML, JS and PDFs, so they are restricted hard:
NFKC-normalized, whitespace-collapsed, 1-30 chars of letters/digits/space and
a little punctuation, at least one letter or digit, and no mixing of Latin
with Cyrillic/Greek (look-alike spoofing such as Cyrillic "Аlex").
"""
import re
import unicodedata
from decimal import Decimal
from typing import Any, List, Optional

from .money import Currency, TooManyDecimals, format_money, get_currency, parse_money
from .receipt_contracts import normalize_item_category

NAME_MAX = 30
ROOM_NAME_MAX = 60
ITEM_NAME_MAX = 80
MAX_ITEMS = 200
MAX_QUANTITY = 99

_NAME_PUNCT = set(" .'-_")
_CONFUSABLE_SCRIPTS = {"LATIN", "CYRILLIC", "GREEK"}
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def _script_of(ch: str) -> Optional[str]:
    try:
        return unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return None


def _clean_text(raw: Any) -> str:
    text = unicodedata.normalize("NFKC", str(raw or ""))
    text = _CONTROL.sub("", text)
    return _WS.sub(" ", text).strip()


def normalize_person_name(raw: Any) -> str:
    """Return a cleaned display name or raise ValueError with a user-facing message."""
    name = _clean_text(raw)
    if not name:
        raise ValueError("Please enter your name.")
    if len(name) > NAME_MAX:
        raise ValueError(f"Name must be at most {NAME_MAX} characters.")

    has_alnum = False
    scripts = set()
    for ch in name:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            has_alnum = True
            script = _script_of(ch)
            if script in _CONFUSABLE_SCRIPTS:
                scripts.add(script)
        elif ch in _NAME_PUNCT or cat[0] == "M":  # combining marks (Devanagari matras etc.)
            continue
        else:
            raise ValueError("Names can only contain letters, numbers, spaces and . ' - _")
    if not has_alnum:
        raise ValueError("Name must contain at least one letter or number.")
    if len(scripts) > 1:
        raise ValueError("Name mixes alphabets (e.g. Latin and Cyrillic). Please retype it.")
    return name


def name_key(name: str) -> str:
    """Case-insensitive key used for uniqueness inside a room."""
    return name.casefold()


def normalize_room_name(raw: Any) -> str:
    name = _clean_text(raw)
    if not name:
        raise ValueError("Please enter a room name.")
    return name[:ROOM_NAME_MAX]


def normalize_item_name(raw: Any) -> str:
    return _clean_text(raw)[:ITEM_NAME_MAX]


def parse_price(value: Any, currency: Optional[Currency] = None, strict: bool = False) -> Optional[Decimal]:
    """
    Parse "₹1,234.50", "450", 12.5, "-50", "¥1,200" -> Decimal rounded to the
    currency's decimals (rupees unless given). Missing/N/A -> None. Raises
    ValueError for garbage, NaN/Infinity or out-of-range values, and with
    strict, for more decimals than the currency has (¥12.50).
    """
    return parse_money(value, currency or get_currency(None), strict=strict)


def format_price(amount: Optional[Decimal], currency: Optional[Currency] = None) -> str:
    return format_money(amount, currency or get_currency(None))


def normalize_quantity(raw: Any) -> int:
    """How many of a dish the bill lists: "5", 5, "5.0", "x2" -> 1..99. Anything else -> 1."""
    if isinstance(raw, bool) or raw is None:
        return 1
    match = re.search(r"\d+", str(raw))
    if not match:
        return 1
    try:
        value = int(match.group())
    except ValueError:
        return 1
    return min(max(value, 1), MAX_QUANTITY)


def clean_items(raw_items: Any, strict_prices: bool = False, currency: Optional[Currency] = None) -> List[dict]:
    """
    Normalize a list of {name, price[, id, category, quantity]} into
    [{id|None, name, price: Decimal|None, category, quantity}].
    Drops nameless rows. Prices are rounded to the currency's decimals. With
    strict_prices, an unparsable price (or one with too many decimals) raises
    ValueError; otherwise it becomes None (LLM output, which the host reviews).
    """
    if not isinstance(raw_items, list):
        return []
    cleaned = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = normalize_item_name(raw.get("name"))
        if not name or name.upper() == "N/A":
            continue
        try:
            price = parse_price(raw.get("price"), currency, strict=strict_prices)
        except ValueError as exc:
            if strict_prices:
                detail = str(exc) if isinstance(exc, TooManyDecimals) else f"invalid price '{raw.get('price')}'"
                raise ValueError(f"Item '{name}': {detail}")
            price = None
        item_id = raw.get("id")
        try:
            item_id = int(item_id) if item_id not in (None, "") else None
        except (TypeError, ValueError):
            item_id = None
        cleaned.append({
            "id": item_id,
            "name": name,
            "price": price,
            "category": normalize_item_category(raw.get("category")),
            "quantity": normalize_quantity(raw.get("quantity")),
        })
        if len(cleaned) > MAX_ITEMS:
            raise ValueError(f"A bill can have at most {MAX_ITEMS} items.")
    return cleaned
