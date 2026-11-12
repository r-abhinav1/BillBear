from typing import Any, Dict


def _parse_amount(value: Any) -> float:
    """Convert a currency string or number to a float, returning 0.0 on failure."""
    if not value or value == "N/A":
        return 0.0
    try:
        return float(str(value).replace("₹", "").replace(",", ""))
    except ValueError:
        return 0.0


def calculate_bill_split(room: Dict[str, Any]) -> Dict[str, Any]:
    """
    Split the bill among users based on their item selections.

    A dish is divided equally among the users who selected it. Where the bill
    lists several of a dish (5 x Masala Dosa), each person either claims whole
    units, paying the per-unit price for each, or shares: the units nobody
    claimed are divided equally among the sharers. Units that are neither
    claimed nor shared are charged to nobody (the audit reports them).
    Taxes and service charge are split in proportion to what each person's
    dishes cost (the default), or equally between everyone who picked
    something when the host has set ocr_data["taxSplit"] to "equal".
    Discounts are applied proportionally to each user's item share.
    """
    items = room.get("items", [])
    selections = room.get("selections", {})
    ocr_data = room.get("ocr_data", {})
    portions = room.get("portions") or {}

    # Build item-name → price / quantity maps
    item_prices: Dict[str, float] = {}
    item_quantity: Dict[str, int] = {}
    for item in items:
        price = _parse_amount(item.get("price", "₹0.00"))
        item_prices[item["name"]] = price
        try:
            item_quantity[item["name"]] = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            item_quantity[item["name"]] = 1

    # Additional charges from OCR data
    service_charge = _parse_amount(ocr_data.get("serviceCharge", 0))
    discount = _parse_amount(ocr_data.get("discount", 0))
    cgst = _parse_amount(ocr_data.get("cgst", 0))
    sgst = _parse_amount(ocr_data.get("sgst", 0))
    igst = _parse_amount(ocr_data.get("igst", 0))

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
    # Users who actually picked something split the tax/service among themselves.
    # A user with an empty selection (e.g. force-completed) doesn't owe tax,
    # because they didn't order anything. This keeps the math intuitive: if you
    # picked nothing you pay nothing.
    paying_users = [u for u, picks in selections.items() if picks]
    num_paying = len(paying_users)

    # Each paying user's fraction of the taxes and service charge. Proportional
    # falls back to equal when the dishes people picked add up to nothing.
    paying_total = sum(user_item_totals[u] for u in paying_users)
    proportional = ocr_data.get("taxSplit") != "equal"
    by_consumption = proportional and paying_total > 0

    def charge_fraction(user: str) -> float:
        if user not in paying_users:
            return 0.0
        if by_consumption:
            return max(user_item_totals[user], 0.0) / paying_total
        return 1.0 / num_paying

    # Discount is proportional to each user's share of the FULL bill subtotal
    # (not the "ordered" subtotal). This mirrors how the bill itself applied
    # the discount to every item, so unselected items "carry" their share.
    discount_rate = (discount / subtotal) if subtotal > 0 else 0.0

    user_breakdown: Dict[str, Any] = {}
    grand_total = 0.0

    for user, item_total in user_item_totals.items():
        is_paying = user in paying_users
        fraction = charge_fraction(user)
        service_charge_per_user = service_charge * fraction
        cgst_per_user = cgst * fraction
        sgst_per_user = sgst * fraction
        igst_per_user = igst * fraction
        user_discount = item_total * discount_rate
        if is_paying:
            final_amount = (
                item_total
                + service_charge_per_user
                + cgst_per_user
                + sgst_per_user
                + igst_per_user
                - user_discount
            )
        else:
            # No selection → owe nothing. Keep the row so they appear in the
            # breakdown (shows "No items selected" + ₹0.00).
            final_amount = 0.0
            user_discount = 0.0

        # Per-item split detail — the item's full price, how many people picked
        # it, and what this user's part works out to. For a several-of dish:
        # `mode` "units" (claimed `portions` of `quantity`) or "shared" (split
        # the `unclaimed` units with `sharers` people in all).
        item_details = []
        for item_name in selections.get(user, []):
            full_price = item_prices.get(item_name, 0.0)
            share = item_shares.get(item_name, {}).get(user, {"mode": "plain", "amount": full_price, "units": 1})
            item_details.append({
                "name": item_name,
                "mode": share["mode"],
                "portions": share["units"],
                "quantity": item_quantity.get(item_name, 1),
                "sharers": share.get("sharers", 0),
                "unclaimed": share.get("unclaimed", 0),
                "unit_price": round(share.get("unit_price", full_price), 2),
                "full_price": round(full_price, 2),
                "shared_by": item_selection_count.get(item_name, 1),
                "your_share": round(share["amount"], 2),
            })

        user_breakdown[user] = {
            "selected_items": selections.get(user, []),
            "item_details": item_details,
            "item_total": round(item_total, 2),
            "percentage": round((item_total / subtotal) * 100, 1) if subtotal > 0 else 0,
            "service_charge": round(service_charge_per_user if is_paying else 0.0, 2),
            "discount": round(user_discount, 2),
            "cgst": round(cgst_per_user if is_paying else 0.0, 2),
            "sgst": round(sgst_per_user if is_paying else 0.0, 2),
            "igst": round(igst_per_user if is_paying else 0.0, 2),
            "final_amount": round(final_amount, 2),
        }

        grand_total += final_amount

    return {
        "user_breakdown": user_breakdown,
        "totals": {
            "subtotal": round(sum(user_item_totals.values()), 2),
            "service_charge": round(service_charge, 2),
            "discount": round(discount, 2),
            "cgst": round(cgst, 2),
            "sgst": round(sgst, 2),
            "igst": round(igst, 2),
            "grand_total": round(grand_total, 2),
            "tax_split": "proportional" if proportional else "equal",
        },
        "item_sharing": item_selection_count,
    }
