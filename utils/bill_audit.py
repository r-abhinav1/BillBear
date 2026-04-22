"""
Bill-math audit warnings shown on the results page.

These are not blocking errors — they're explanations for the host/guests when
the computed split won't equal the bill's printed total, usually because the
group collectively didn't assign every item to someone.
"""
from typing import Any, Dict, List


def _parse_amount(value: Any) -> float:
    if value is None or value == "N/A":
        return 0.0
    try:
        return float(str(value).replace("₹", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def audit_bill(room: Dict[str, Any], bill_split: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Return a list of {severity, title, detail} dicts. Severities:
      - "info"  : FYI
      - "warn"  : user probably wants to fix this
      - "error" : math is clearly off
    """
    warnings: List[Dict[str, str]] = []

    items = room.get("items", [])
    selections = room.get("selections", {}) or {}
    ocr_data = room.get("ocr_data", {}) or {}

    bill_subtotal = _parse_amount(ocr_data.get("subtotal"))
    bill_total = _parse_amount(ocr_data.get("total"))
    reported_grand = float(bill_split.get("totals", {}).get("grand_total", 0.0))

    # --- Items nobody selected ----------------------------------------------
    item_sharing = bill_split.get("item_sharing", {})
    unassigned = [name for name, count in item_sharing.items() if not count]
    if unassigned:
        orphan_total = sum(_parse_amount(i.get("price")) for i in items if i.get("name") in unassigned)
        warnings.append({
            "severity": "warn",
            "title": f"{len(unassigned)} item(s) not picked by anyone",
            "detail": (
                f"Nobody selected: {', '.join(unassigned[:6])}"
                + ("…" if len(unassigned) > 6 else "")
                + f" (~₹{orphan_total:.2f}). That amount is not included in anyone's share."
            ),
        })

    # --- Ordered subtotal vs printed bill subtotal --------------------------
    ordered_subtotal = sum(
        _parse_amount(i.get("price"))
        for i in items
        if item_sharing.get(i.get("name"), 0) > 0
    )
    if bill_subtotal > 0 and abs(ordered_subtotal - bill_subtotal) > 0.01:
        warnings.append({
            "severity": "info",
            "title": "Items paid for < bill subtotal",
            "detail": (
                f"The group covered ₹{ordered_subtotal:.2f} worth of items, "
                f"but the bill subtotal is ₹{bill_subtotal:.2f}. "
                f"Difference: ₹{bill_subtotal - ordered_subtotal:.2f} (the unpicked items)."
            ),
        })

    # --- Grand total vs printed bill total ----------------------------------
    if bill_total > 0 and abs(reported_grand - bill_total) > 0.01:
        delta = bill_total - reported_grand
        sign = "higher" if delta > 0 else "lower"
        warnings.append({
            "severity": "info",
            "title": "Split total doesn't match bill total",
            "detail": (
                f"Split sums to ₹{reported_grand:.2f}; bill total is ₹{bill_total:.2f} "
                f"(₹{abs(delta):.2f} {sign}). "
                "Usually caused by unpicked items or rounding. "
                "Ask the host to revert and re-assign the missing items if it matters."
            ),
        })

    # --- Users with no selection (paying nothing, for clarity) --------------
    all_users = list(selections.keys())
    noselect = [u for u in all_users if not selections.get(u)]
    if noselect:
        warnings.append({
            "severity": "info",
            "title": f"{len(noselect)} user(s) didn't pick anything",
            "detail": (
                f"{', '.join(noselect)} owe ₹0.00 — they didn't select any items, "
                "so they're not included in the tax/service split."
            ),
        })

    # --- All clean ----------------------------------------------------------
    if not warnings:
        warnings.append({
            "severity": "ok",
            "title": "✅ Math reconciled",
            "detail": "Every item is picked, and the split sums to the bill's total.",
        })

    return warnings
