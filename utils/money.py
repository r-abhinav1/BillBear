"""
Currencies, amount parsing and formatting, and exact share allocation.

Every amount a person types, a model reads off a bill, or the app shows goes
through here, so the rules live in one place:

- `CURRENCIES` lists what the app supports: symbol, decimal places, and the
  smallest step a bill is rounded to (its "round off").
- `parse_amount` turns "₹1,234.50", "¥1200", "12,50 €" or 12.5 into a Decimal.
- `format_money` / `format_amount` render an amount for one currency.
- `allocate` rounds a list of shares to whole minor units (paise, yen) so they
  add up exactly to a target, instead of each being rounded on its own.

Changing a room's currency only changes how amounts are shown and checked.
Nothing is ever converted.
"""
import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_CURRENCY = "INR"
# Items are stored as numeric(12,2) with a ±1e7 check; totals share the limit.
AMOUNT_LIMIT = Decimal("10000000")


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    symbol: str
    decimals: int
    # Smallest step the bill total is rounded to (cash rounding / "round off").
    step: Decimal
    # What the PDF prints: its built-in fonts have no ₹, ¥, ฿ and so on.
    pdf_prefix: str

    @property
    def minor(self) -> Decimal:
        return Decimal(1).scaleb(-self.decimals)

    def public(self) -> Dict[str, Any]:
        """The fields the browser needs to format and check amounts."""
        return {
            "code": self.code,
            "name": self.name,
            "symbol": self.symbol,
            "decimals": self.decimals,
            "step": float(self.step),
        }


def _c(code, name, symbol, decimals=2, step="0.01", pdf=None):
    return Currency(code, name, symbol, decimals, Decimal(step), pdf or code)


# Order is the order of the picker. Three-decimal currencies (KWD, BHD, OMR) are
# left out on purpose: item prices are stored with two decimals.
CURRENCIES: Dict[str, Currency] = {c.code: c for c in (
    _c("INR", "Indian Rupee", "₹", step="1", pdf="Rs."),
    _c("USD", "US Dollar", "$"),
    _c("EUR", "Euro", "€"),
    _c("GBP", "British Pound", "£"),
    _c("AED", "UAE Dirham", "AED"),
    _c("SGD", "Singapore Dollar", "S$", step="0.05"),
    _c("THB", "Thai Baht", "฿"),
    _c("JPY", "Japanese Yen", "¥", decimals=0, step="1"),
    _c("AUD", "Australian Dollar", "A$", step="0.05"),
    _c("CAD", "Canadian Dollar", "C$", step="0.05"),
    _c("CHF", "Swiss Franc", "CHF", step="0.05"),
    _c("NZD", "New Zealand Dollar", "NZ$", step="0.10"),
    _c("HKD", "Hong Kong Dollar", "HK$"),
    _c("MYR", "Malaysian Ringgit", "RM", step="0.05"),
    _c("IDR", "Indonesian Rupiah", "Rp", decimals=0, step="100"),
    _c("LKR", "Sri Lankan Rupee", "LKR"),
    _c("NPR", "Nepalese Rupee", "NPR"),
    _c("BDT", "Bangladeshi Taka", "৳"),
    _c("SAR", "Saudi Riyal", "SAR"),
    _c("QAR", "Qatari Riyal", "QAR"),
    _c("CNY", "Chinese Yuan", "CN¥"),
    _c("KRW", "South Korean Won", "₩", decimals=0, step="1"),
    _c("PHP", "Philippine Peso", "₱"),
    _c("ZAR", "South African Rand", "R"),
)}


def get_currency(code: Any) -> Currency:
    """The currency for a code; anything unknown or missing is the default (rupees)."""
    if isinstance(code, str):
        found = CURRENCIES.get(code.strip().upper())
        if found:
            return found
    return CURRENCIES[DEFAULT_CURRENCY]


def is_supported(code: Any) -> bool:
    return isinstance(code, str) and code.strip().upper() in CURRENCIES


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

MISSING_WORDS = {"", "N/A", "NA", "-", "NONE", "NULL"}
_GROUPING = re.compile(r"[\s  '’]")          # 1 234,50 / 1'234.50
# 1,234.50 and Indian 1,23,456.50 (commas group, the dot is the decimal point)
_DOT_DECIMAL = re.compile(r"\d+(?:,\d{2,3})*(?:\.\d+)?")
# 12,50 and 1.234,50 (one comma, then one or two digits: a decimal comma)
_COMMA_DECIMAL = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{1,2}|\d+,\d{1,2}")


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().upper() in MISSING_WORDS


def parse_amount(value: Any) -> Optional[Decimal]:
    """
    Read an amount the way people and receipts write it. Returns None for
    missing values ("", "N/A", None) and raises ValueError for anything else
    that isn't one clear number.

    Symbols and codes around the number are ignored ("Rs. 500/-", "1200円",
    "CHF 12.50"). Commas are thousands separators ("1,23,456.50"), except a
    lone comma followed by one or two digits, which is a decimal comma
    ("12,50", "1.234,50").
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"'{value}' is not a valid amount") from exc
        return _checked(amount, value)

    text = str(value).strip()
    if text.upper() in MISSING_WORDS:
        return None
    digits = [i for i, ch in enumerate(text) if ch.isdigit()]
    if not digits:
        raise ValueError(f"'{value}' is not a valid amount")
    head, body = text[: digits[0]], text[digits[0]: digits[-1] + 1]
    negative = "-" in head or "−" in head
    body = _GROUPING.sub("", body)
    if _COMMA_DECIMAL.fullmatch(body):
        number = body.replace(".", "").replace(",", ".")
    elif _DOT_DECIMAL.fullmatch(body):
        number = body.replace(",", "")
    else:
        raise ValueError(f"'{value}' is not a valid amount")
    amount = Decimal(number)
    return _checked(-amount if negative else amount, value)


def _checked(amount: Decimal, original: Any) -> Decimal:
    if not amount.is_finite() or abs(amount) >= AMOUNT_LIMIT:
        raise ValueError(f"'{original}' is out of range")
    return amount


class TooManyDecimals(ValueError):
    """An amount the currency can't hold exactly (¥500.50, ₹1.005)."""


def to_minor(amount: Decimal, currency: Currency, strict: bool = False) -> Decimal:
    """
    Round to the currency's decimals. With strict, refuse amounts that have
    more decimals than the currency allows (¥500.50) instead of rounding them.
    """
    rounded = amount.quantize(currency.minor, rounding=ROUND_HALF_UP)
    if strict and rounded != amount:
        if currency.decimals == 0:
            raise TooManyDecimals(f"{currency.name} amounts are whole numbers, so '{_plain(amount)}' can't be used.")
        raise TooManyDecimals(f"{currency.name} amounts have at most {currency.decimals} decimals: '{_plain(amount)}'.")
    return rounded


def parse_money(value: Any, currency: Currency, strict: bool = False) -> Optional[Decimal]:
    """parse_amount + rounding to the currency. None stays None."""
    amount = parse_amount(value)
    return None if amount is None else to_minor(amount, currency, strict)


def amount_or_zero(value: Any) -> float:
    """Lenient read for stored data: anything unreadable counts as 0."""
    try:
        amount = parse_amount(value)
    except ValueError:
        return 0.0
    return float(amount) if amount is not None else 0.0


def _plain(amount: Decimal) -> str:
    return format(amount, "f")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _group_indian(whole: str) -> str:
    """1234567 -> 12,34,567"""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join(pairs + [tail])


def format_amount(amount: Any, currency: Currency, grouping: bool = True) -> str:
    """The number alone, with the currency's decimals: 1,234.50 / 1,200 / 12,34,567.00."""
    try:
        value = Decimal(str(amount if amount is not None else 0))
    except InvalidOperation:
        value = Decimal(0)
    value = value.quantize(currency.minor, rounding=ROUND_HALF_UP)
    negative = value < 0
    text = format(abs(value), "f")
    whole, _, frac = text.partition(".")
    if grouping:
        whole = _group_indian(whole) if currency.code == "INR" else f"{int(whole):,}"
    result = whole + (f".{frac}" if frac else "")
    return f"-{result}" if negative else result


def format_money(amount: Any, currency: Currency, pdf: bool = False) -> str:
    """With the symbol: ₹1,234.50, ¥1,200, CHF 12.50. The PDF uses its plain-font prefix."""
    if amount is None:
        return "N/A"
    number = format_amount(amount, currency)
    negative = number.startswith("-")
    number = number.lstrip("-")
    prefix = currency.pdf_prefix if pdf else currency.symbol
    sep = " " if (pdf or prefix[-1].isalpha()) else ""
    return f"{'-' if negative else ''}{prefix}{sep}{number}"


# ---------------------------------------------------------------------------
# Exact allocation
# ---------------------------------------------------------------------------

def to_units(value: float, currency: Currency) -> float:
    """A float amount in minor units (paise, yen), without float noise (0.29 * 100)."""
    return round(value * (10 ** currency.decimals), 6)


def from_units(units: int, currency: Currency) -> float:
    return round(units / (10 ** currency.decimals), currency.decimals)


def allocate(values: Sequence[float], target_units: int) -> List[int]:
    """
    Round each value (already in minor units) down to a whole unit, then hand
    the units still missing to the values that lost the most in rounding, the
    earliest one first on a tie. The result always adds up to target_units,
    which must be within one unit of sum(values) for every share to stay
    within one unit of its exact value.

    allocate([333.33, 333.33, 333.33], 1000) == [334, 333, 333]
    """
    if not values:
        return []
    result = [math.floor(v) for v in values]
    missing = target_units - sum(result)
    # Most rounded-off first; the earliest wins a tie.
    order = sorted(range(len(values)), key=lambda i: (-(values[i] - result[i]), i))
    # Normally 0 <= missing <= len(values). Wrap round (or take back from the
    # least rounded-off) so that any target still adds up.
    for n in range(abs(missing)):
        if missing > 0:
            result[order[n % len(order)]] += 1
        else:
            result[order[-1 - n % len(order)]] -= 1
    return result


def currency_choices() -> List[Dict[str, Any]]:
    return [c.public() for c in CURRENCIES.values()]
