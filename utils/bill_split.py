import math
from typing import Any, Dict, List

from .bill_totals import CHARGE, DISCOUNT, read_bill
from .money import allocate, amount_or_zero, from_units, to_units


def item_amount(item: Dict[str, Any]) -> float:
    """An item's line price: the stored number when there is one, else its display string."""
    if item.get("amount") is not None:
        return float(item["amount"])
    return amount_or_zero(item.get("price"))


def calculate_bill_split(room: Dict[str, Any]) -> Dict[str, Any]:
    """
    Split the bill among users based on their item selections.

    A dish is divided equally among the users who selected it. Where the bill
    lists several of a dish (5 x Masala Dosa), each person either claims whole
    units, paying the per-unit price for each, or shares: the units nobody
    claimed are divided equally among the sharers. Units that are neither
    claimed nor shared are charged to nobody (the audit reports them).

    Every extra row on the bill (tax, service, packing, tip) is split in
    proportion to what each person's dishes cost (the default), or equally
    between everyone who picked something when the host has set
    ocr_data["taxSplit"] to "equal". Discount rows follow the food: each
    person gets the same fraction off their dishes as the bill took off its
    whole subtotal.

    Amounts are rounded to the room's currency (paise, whole yen) so that
    they add up exactly: the shares add up to the total, and each person's
    lines add up to what they pay. See money.allocate.
    """
    items = room.get("items", [])
    selections = room.get("selections", {})
    portions = room.get("portions") or {}
    bill = read_bill(room.get("ocr_data"))
    currency = bill["currency"]
    charges = [c for c in bill["charges"] if c["kind"] == CHARGE]
    discounts = [c for c in bill["charges"] if c["kind"] == DISCOUNT]

    # Build item-name → price / quantity maps
    item_prices: Dict[str, float] = {}
    item_quantity: Dict[str, int] = {}
    for item in items:
        item_prices[item["name"]] = item_amount(item)
        try:
            item_quantity[item["name"]] = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            item_quantity[item["name"]] = 1

    def units_of(user: str, item_name: str) -> int:
        """Whole units of a several-of dish this user claimed; 0 means they are sharing."""
        try:
            return max(0, int((portions.get(user) or {}).get(item_name, 0)))
        except (TypeError, ValueError):
            return 0

    # Count how many users selected each item
    item_selection_count: Dict[str, int] = {
        name: sum(1 for user_selections in selections.values() if name in user_selections)
        for name in item_prices
    }

    # For each item, what each picker pays and how that came about.
    item_shares: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for name, price in item_prices.items():
        pickers = [user for user, picks in selections.items() if name in picks]
        quantity = item_quantity[name]
        shares: Dict[str, Dict[str, Any]] = {}
        if quantity <= 1:
            for user in pickers:
                shares[user] = {"mode": "plain", "amount": price / len(pickers), "units": 1}
        else:
            claimed = sum(units_of(user, name) for user in pickers)
            sharers = [user for user in pickers if units_of(user, name) == 0]
            # More units claimed than the bill lists should not happen (sends are
            # checked); if it does, the claimers split the whole line by units.
            unit_price = price / max(quantity, claimed)
            unclaimed = max(quantity - claimed, 0)
            for user in pickers:
                units = units_of(user, name)
                if units:
                    shares[user] = {"mode": "units", "amount": unit_price * units, "units": units}
                else:
                    shares[user] = {"mode": "shared", "amount": unit_price * unclaimed / len(sharers), "units": 0}
            for share in shares.values():
                share.update({"sharers": len(sharers), "unclaimed": unclaimed, "unit_price": unit_price})
        item_shares[name] = shares

    # Calculate each user's item subtotal
    all_users = list(selections.keys())
    user_item_totals: Dict[str, float] = {}
    for user in all_users:
        user_item_totals[user] = sum(
            item_shares[item_name][user]["amount"]
            for item_name in selections.get(user, [])
            if item_name in item_shares
        )

    subtotal = sum(item_prices.values())
    # Users who actually picked something split the charges among themselves.
    # A user with an empty selection (e.g. force-completed) doesn't owe any,
    # because they didn't order anything: if you picked nothing you pay nothing.
    paying_users = [u for u, picks in selections.items() if picks]
    num_paying = len(paying_users)

    # Each paying user's fraction of the charges. Proportional falls back to
    # equal when the dishes people picked add up to nothing.
    paying_total = sum(user_item_totals[u] for u in paying_users)
    proportional = bill["tax_split"] != "equal"
    by_consumption = proportional and paying_total > 0

    def charge_fraction(user: str) -> float:
        if user not in paying_users:
            return 0.0
        if by_consumption:
            return max(user_item_totals[user], 0.0) / paying_total
        return 1.0 / num_paying

    # A discount is proportional to each user's share of the FULL bill subtotal
    # (not the "ordered" subtotal). This mirrors how the bill itself applied
    # the discount to every item, so unselected items "carry" their share.
    def discount_share(user: str, row: Dict[str, Any]) -> float:
        if user not in paying_users or subtotal <= 0:
            return 0.0
        return user_item_totals[user] * row["amount"] / subtotal

    # Exact (unrounded) lines per person: dishes, then charges, then discounts
    # (negative). Rounded together below.
    exact_lines: Dict[str, List[float]] = {}
    for user in all_users:
        lines = [item_shares[name][user]["amount"] for name in selections.get(user, []) if name in item_shares]
        lines += [row["amount"] * charge_fraction(user) for row in charges]
        lines += [-discount_share(user, row) for row in discounts]
        exact_lines[user] = lines if user in paying_users else [0.0] * len(lines)

    # Round to whole minor units so every share is a payable amount and the
    # shares add up exactly to the (rounded) total.
    exact_finals = [to_units(sum(exact_lines[u]), currency) for u in all_users]
    final_units = dict(zip(all_users, allocate(exact_finals, math.floor(sum(exact_finals) + 0.5))))

    user_breakdown: Dict[str, Any] = {}
    for user in all_users:
        is_paying = user in paying_users
        line_units = allocate([to_units(v, currency) for v in exact_lines[user]], final_units[user])
        lines = [from_units(u, currency) for u in line_units]
        n_items = len([n for n in selections.get(user, []) if n in item_shares])
        item_lines, charge_lines, discount_lines = (
            lines[:n_items], lines[n_items:n_items + len(charges)], lines[n_items + len(charges):]
        )

        # Per-item split detail — the item's full price, how many people picked
        # it, and what this user's part works out to. For a several-of dish:
        # `mode` "units" (claimed `portions` of `quantity`) or "shared" (split
        # the `unclaimed` units with `sharers` people in all).
        item_details = []
        picked = [n for n in selections.get(user, []) if n in item_shares]
        for item_name, your_share in zip(picked, item_lines):
            full_price = item_prices.get(item_name, 0.0)
            share = item_shares[item_name][user]
            item_details.append({
                "name": item_name,
                "mode": share["mode"],
                "portions": share["units"],
                "quantity": item_quantity.get(item_name, 1),
                "sharers": share.get("sharers", 0),
                "unclaimed": share.get("unclaimed", 0),
                "unit_price": round(share.get("unit_price", full_price), currency.decimals),
                "full_price": round(full_price, currency.decimals),
                "shared_by": item_selection_count.get(item_name, 1),
                "your_share": your_share,
            })

        # What this person pays of each extra row on the bill, in bill order.
        # Discounts are positive amounts here, shown with a minus.
        user_charges = (
            [{"label": row["label"], "kind": CHARGE, "amount": amount} for row, amount in zip(charges, charge_lines)]
            + [{"label": row["label"], "kind": DISCOUNT, "amount": -amount} for row, amount in zip(discounts, discount_lines)]
        )

        user_breakdown[user] = {
            "selected_items": selections.get(user, []),
            "item_details": item_details,
            "item_total": round(user_item_totals[user], currency.decimals),
            "percentage": round((user_item_totals[user] / subtotal) * 100, 1) if subtotal > 0 else 0,
            "charges": [c for c in user_charges if c["amount"]] if is_paying else [],
            "discount": round(-sum(discount_lines), currency.decimals) if is_paying else 0.0,
            "final_amount": from_units(final_units[user], currency),
        }

    return {
        "user_breakdown": user_breakdown,
        "totals": {
            "currency": currency.code,
            "subtotal": round(sum(user_item_totals.values()), currency.decimals),
            # The bill's own rows, as printed on it.
            "charges": [dict(row) for row in charges] + [dict(row) for row in discounts],
            "has_charges": bool(charges),
            "grand_total": from_units(sum(final_units.values()), currency),
            "tax_split": "proportional" if proportional else "equal",
        },
        "item_sharing": item_selection_count,
    }
