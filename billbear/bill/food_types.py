"""
Splitting by food type: every food type on the bill is a pot, and each pot is
split equally between the people who had some of it.

Two ways in, chosen by the host (rooms.split_mode):
  - "types":  everyone taps the types they had. Tapping a type counts as
              sharing every dish of that type, so the dish-by-dish split
              (bill/split.py) does the maths unchanged; see assembler.py.
  - "counts": the host types how many people had each type and gets a rate
              card (rate_card below). Nobody picks and the bill is never
              "split" in the room.

Dishes typed "other" (water, packing, anything not marked) are never a pot to
pick: everyone at the table shares them.

No Flask and no database here.
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence

from billbear.bill.money import from_units, to_units
from billbear.bill.totals import CHARGE, DISCOUNT, read_bill

ITEMS, TYPES, COUNTS = "items", "types", "counts"
SPLIT_MODES = (ITEMS, TYPES, COUNTS)

# In the order they are shown everywhere.
FOOD_TYPES = ("veg", "nonveg", "egg", "alcohol")
TYPE_LABELS = {"veg": "Veg", "nonveg": "Non-veg", "egg": "Egg", "alcohol": "Alcohol", "other": "Everyone shares"}
EVERYONE = "other"


def split_mode(value: Any) -> str:
    """A stored or posted split mode; anything unknown is the dish-by-dish default."""
    return value if value in SPLIT_MODES else ITEMS


def _amount(item: Dict[str, Any]) -> float:
    value = item.get("amount")
    return float(value) if value is not None else 0.0


def present_types(items: Iterable[Dict[str, Any]]) -> List[str]:
    """The food types this bill has a dish of, in display order ("other" is never one)."""
    found = {item.get("category") for item in items}
    return [t for t in FOOD_TYPES if t in found]


def pots(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The bill's dishes grouped by type: one pot per type it has, then the dishes
    everyone shares (only if there are any). Each pot is
    {key, label, total, dishes: [item, ...]}.
    """
    result = []
    for key in list(present_types(items)) + [EVERYONE]:
        dishes = [i for i in items if (i.get("category") if i.get("category") in FOOD_TYPES else EVERYONE) == key]
        if dishes:
            result.append({
                "key": key,
                "label": TYPE_LABELS[key],
                "total": sum(_amount(i) for i in dishes),
                "dishes": dishes,
            })
    return result


def clean_types(values: Iterable[Any], present: Sequence[str]) -> List[str]:
    """What someone tapped, kept to the types this bill has, once each, in display order."""
    wanted = {str(v).strip().lower() for v in values or [] if isinstance(v, str)}
    return [t for t in present if t in wanted]


def clean_counts(raw: Any, present: Sequence[str], head: int) -> Dict[str, int]:
    """
    The host's "how many had each type" -> {type: count} for every type the bill
    has. Raises ValueError, with a message for the host, on anything it can't use.
    """
    if not isinstance(raw, dict):
        raise ValueError("Say how many people had each type.")
    counts = {}
    for key in present:
        value = raw.get(key)
        # bool is an int in Python; "true" people is not a count.
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"Say how many people had {TYPE_LABELS[key].lower()}.")
        try:
            n = int(str(value).strip())
        except ValueError:
            raise ValueError(f"The number for {TYPE_LABELS[key].lower()} must be a whole number.")
        if n < 1:
            raise ValueError(f"The bill has {TYPE_LABELS[key].lower()} dishes, so at least 1 person had some.")
        if n > head:
            raise ValueError(
                f"{n} people had {TYPE_LABELS[key].lower()}, but only {head} are at the table."
                " Raise the number at the table first."
            )
        counts[key] = n
    return counts


def stored_counts(raw: Any, present: Sequence[str], head: int) -> Dict[str, int]:
    """
    The counts saved on a room, made to fit the bill as it is now: a type with
    no saved count (the host added its first dish later) starts at everyone at
    the table, and a count above the head count comes down to it.
    """
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for key in present:
        value = raw.get(key)
        n = value if isinstance(value, int) and not isinstance(value, bool) else head
        result[key] = min(max(n, 1), head)
    return result


def _combos(present: Sequence[str]) -> List[List[str]]:
    """A few combinations to show as examples on the rate card."""
    if not present:
        return []
    first, rest = present[0], list(present[1:])
    combos = [[first]] + [[first, other] for other in rest]
    if len(present) > 2:
        combos.append(list(present))
    return combos[:5]


def rate_card(room: Dict[str, Any], counts: Dict[str, int], head: int) -> Dict[str, Any]:
    """
    What one person pays for each food type they had, taxes in.

    Each type's pot is split equally between the `counts[type]` people who had
    it; the dishes everyone shares are split between all `head` people. Taxes,
    service and other charges follow the bill's tax-split switch, as in the
    dish-by-dish split: by default each pot carries its part of them (its share
    of the dishes); with "equal" they are split evenly between everyone at the
    table instead, on the Everyone line. Discounts always follow the food.

    Amounts are per head, rounded to the currency's minor unit, so the shares
    can add up to a few paise more or less than the bill: `difference` says by
    how much (in currency, positive = more than the bill).
    """
    bill = read_bill(room.get("ocr_data"))
    currency = bill["currency"]
    items = room.get("items", [])
    charges = sum(c["amount"] for c in bill["charges"] if c["kind"] == CHARGE)
    discounts = sum(c["amount"] for c in bill["charges"] if c["kind"] == DISCOUNT)
    subtotal = sum(_amount(i) for i in items)
    # Same fall-back as split.py: with no dishes priced, charges are split evenly.
    proportional = bill["tax_split"] != "equal" and subtotal > 0

    def fraction(pot_total: float) -> float:
        return pot_total / subtotal if subtotal > 0 else 0.0

    def per_head(units: float, people: int) -> int:
        return int(round(units / people)) if people else 0

    rows, everyone = [], None
    for pot in pots(items):
        key = pot["key"]
        people = head if key == EVERYONE else counts.get(key, 0)
        food = pot["total"]
        tax = charges * fraction(food) if proportional else 0.0
        off = discounts * fraction(food)
        each_units = per_head(to_units(food + tax - off, currency), people)
        row = {
            "key": key,
            "label": pot["label"],
            "people": people,
            "total": food,
            "dishes": pot["dishes"],
            "each": from_units(each_units, currency),
            "_units": each_units,
        }
        if key == EVERYONE:
            everyone = row
        else:
            rows.append(row)

    # The Everyone line: shared dishes, plus every charge when they are split evenly.
    even_charges = 0.0 if proportional else charges
    if everyone is None and even_charges:
        everyone = {"key": EVERYONE, "label": TYPE_LABELS[EVERYONE], "people": head, "total": 0.0,
                    "dishes": [], "each": 0.0, "_units": 0}
    if everyone is not None:
        shared_food = everyone["total"]
        exact = to_units(
            shared_food + (charges * fraction(shared_food) if proportional else charges)
            - discounts * fraction(shared_food),
            currency,
        )
        everyone["_units"] = per_head(exact, head)
        everyone["each"] = from_units(everyone["_units"], currency)
        everyone["charges_in"] = bool(even_charges)

    bill_units = int(round(to_units(subtotal + charges - discounts, currency)))
    shares_units = sum(r["_units"] * r["people"] for r in rows) + (everyone["_units"] * head if everyone else 0)

    each_by_key = {r["key"]: r["_units"] for r in rows}
    base = everyone["_units"] if everyone else 0
    examples = [
        {"keys": combo, "labels": [TYPE_LABELS[k] for k in combo],
         "amount": from_units(base + sum(each_by_key[k] for k in combo), currency)}
        for combo in _combos([r["key"] for r in rows])
    ]
    for row in rows + ([everyone] if everyone else []):
        row.pop("_units", None)
    return {
        "currency": currency.code,
        "head": head,
        "rows": rows,
        "everyone": everyone,
        "examples": examples,
        "proportional": proportional,
        "has_charges": bool(charges),
        "bill_total": from_units(bill_units, currency),
        "shares_total": from_units(shares_units, currency),
        "difference": from_units(shares_units - bill_units, currency),
    }


def pot_lines(room: Dict[str, Any], bill_split: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    For a room split by type: each person's dishes from the split, added up by
    pot. {name: [{key, label, total, ways, share, dishes: [name, ...]}]}, pots in
    display order. `ways` is how many people the pot was split between.
    """
    category = {i["name"]: (i["category"] if i["category"] in FOOD_TYPES else EVERYONE) for i in room["items"]}
    sharing = bill_split.get("item_sharing", {})
    order = {key: n for n, key in enumerate(FOOD_TYPES + (EVERYONE,))}
    pot_totals = {p["key"]: p["total"] for p in pots(room["items"])}
    result = {}
    for user, d in bill_split["user_breakdown"].items():
        by_pot: Dict[str, Dict[str, Any]] = {}
        for detail in d["item_details"]:
            key = category.get(detail["name"], EVERYONE)
            pot = by_pot.setdefault(key, {
                "key": key, "label": TYPE_LABELS[key], "total": pot_totals.get(key, 0.0),
                "ways": sharing.get(detail["name"], 1), "share": 0.0, "dishes": [],
            })
            pot["share"] += detail["your_share"]
            pot["dishes"].append(detail["name"])
        lines = sorted(by_pot.values(), key=lambda p: order[p["key"]])
        for line in lines:
            line["share"] = round(line["share"], 6)
        result[user] = lines
    return result


def type_pickers(room: Dict[str, Any]) -> Dict[str, int]:
    """How many of the people who have sent tapped each type the bill has."""
    present = present_types(room.get("items", []))
    counts = {t: 0 for t in present}
    for person in room.get("participants", []):
        if person.get("submitted"):
            for t in person.get("categories") or []:
                if t in counts:
                    counts[t] += 1
    return counts


def unpicked_pots(room: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pots with dishes that nobody who has sent tapped: [{key, label, total}]."""
    picked = type_pickers(room)
    return [
        {"key": p["key"], "label": p["label"], "total": p["total"]}
        for p in pots(room.get("items", []))
        if p["key"] != EVERYONE and not picked.get(p["key"])
    ]


def type_labels(keys: Optional[Iterable[str]]) -> List[str]:
    return [TYPE_LABELS[k] for k in keys or [] if k in TYPE_LABELS and k != EVERYONE]


def public_pots(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """pots() with plain values only, for a page's script."""
    return [
        {
            "key": p["key"],
            "label": p["label"],
            "total": float(p["total"]),
            "dishes": [{"name": d["name"], "price": d.get("price") or "", "quantity": d.get("quantity") or 1}
                       for d in p["dishes"]],
        }
        for p in pots(items)
    ]
