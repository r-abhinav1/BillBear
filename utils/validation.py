"""
Input validation for anything a user (or an LLM) can put into the database.

Names show up in URLs, HTML, JS and PDFs, so they are restricted hard:
NFKC-normalized, whitespace-collapsed, 1-30 chars of letters/digits/space and
a little punctuation, at least one letter or digit, and no mixing of Latin
with Cyrillic/Greek (look-alike spoofing such as Cyrillic "Аlex").
"""
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, List, Optional

NAME_MAX = 30
ROOM_NAME_MAX = 60
ITEM_NAME_MAX = 80
MAX_ITEMS = 200
PRICE_LIMIT = Decimal("10000000")

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


def parse_price(value: Any) -> Optional[Decimal]:
    """
    Parse "₹1,234.50", "450", 12.5, "-50" -> Decimal(2dp). Missing/N/A -> None.
    Raises ValueError for garbage, NaN/Infinity or out-of-range values.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        text = str(value)
    else:
        text = str(value).strip()
        if not text or text.upper() in {"N/A", "NA", "-"}:
            return None
        text = text.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace(",", "").strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        raise ValueError(f"'{value}' is not a valid amount")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"'{value}' is not a valid amount") from exc
    if not amount.is_finite() or abs(amount) >= PRICE_LIMIT:
        raise ValueError(f"'{value}' is out of range")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_price(amount: Optional[Decimal]) -> str:
    if amount is None:
        return "N/A"
    return f"₹{amount:.2f}"


def clean_items(raw_items: Any, strict_prices: bool = False) -> List[dict]:
    """
    Normalize a list of {name, price[, id]} into [{id|None, name, price: Decimal|None}].
    Drops nameless rows. With strict_prices, an unparsable price raises ValueError;
    otherwise it becomes None (used for LLM output, which the host reviews).
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
            price = parse_price(raw.get("price"))
        except ValueError:
            if strict_prices:
                raise ValueError(f"Item '{name}': invalid price '{raw.get('price')}'")
            price = None
        item_id = raw.get("id")
        try:
            item_id = int(item_id) if item_id not in (None, "") else None
        except (TypeError, ValueError):
            item_id = None
        cleaned.append({"id": item_id, "name": name, "price": price})
        if len(cleaned) > MAX_ITEMS:
            raise ValueError(f"A bill can have at most {MAX_ITEMS} items.")
    return cleaned
