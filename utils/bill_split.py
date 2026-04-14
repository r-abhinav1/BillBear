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

    Each item's cost is divided equally among the users who selected it.
    Taxes and service charges are split equally across all users.
    Discounts are applied proportionally to each user's item share.
    """
    items = room.get("items", [])
    selections = room.get("selections", {})
    ocr_data = room.get("ocr_data", {})

    # Build item-name → price map
    item_prices: Dict[str, float] = {}
    for item in items:
        price = _parse_amount(item.get("price", "₹0.00"))
        item_prices[item["name"]] = price

    # Additional charges from OCR data
    service_charge = _parse_amount(ocr_data.get("serviceCharge", 0))
    discount = _parse_amount(ocr_data.get("discount", 0))
    cgst = _parse_amount(ocr_data.get("cgst", 0))
    sgst = _parse_amount(ocr_data.get("sgst", 0))
    igst = _parse_amount(ocr_data.get("igst", 0))

    # Count how many users selected each item
    item_selection_count: Dict[str, int] = {
        name: sum(1 for user_selections in selections.values() if name in user_selections)
        for name in item_prices
    }

    # Calculate each user's item subtotal (shared cost)
    all_users = list(selections.keys())
    user_item_totals: Dict[str, float] = {}
    for user in all_users:
        total = 0.0
        for item_name in selections.get(user, []):
            price = item_prices.get(item_name, 0.0)
            count = item_selection_count.get(item_name, 1)
            if count > 0:
                total += price / count
        user_item_totals[user] = total

    subtotal = sum(item_prices.values())
    num_users = len(all_users)

    # Split fixed charges equally
    cgst_per_user = cgst / num_users if num_users else 0.0
    sgst_per_user = sgst / num_users if num_users else 0.0
    igst_per_user = igst / num_users if num_users else 0.0
    service_charge_per_user = service_charge / num_users if num_users else 0.0

    # Discount is proportional to each user's share of the subtotal
    discount_rate = (discount / subtotal) if subtotal > 0 else 0.0

    user_breakdown: Dict[str, Any] = {}
    grand_total = 0.0

    for user, item_total in user_item_totals.items():
        user_discount = item_total * discount_rate
        final_amount = (
            item_total
            + service_charge_per_user
            + cgst_per_user
            + sgst_per_user
            + igst_per_user
            - user_discount
        )

        # Per-item split detail — shows each item's full price, how many people
        # share it, and what this user's portion works out to.
        item_details = []
        for item_name in selections.get(user, []):
            full_price = item_prices.get(item_name, 0.0)
            shared_by = item_selection_count.get(item_name, 1)
            your_share = full_price / shared_by if shared_by > 0 else full_price
            item_details.append({
                "name": item_name,
                "full_price": round(full_price, 2),
                "shared_by": shared_by,
                "your_share": round(your_share, 2),
            })

        user_breakdown[user] = {
            "selected_items": selections.get(user, []),
            "item_details": item_details,
            "item_total": round(item_total, 2),
            "percentage": round((item_total / subtotal) * 100, 1) if subtotal > 0 else 0,
            "service_charge": round(service_charge_per_user, 2),
            "discount": round(user_discount, 2),
            "cgst": round(cgst_per_user, 2),
            "sgst": round(sgst_per_user, 2),
            "igst": round(igst_per_user, 2),
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
        },
        "item_sharing": item_selection_count,
    }
