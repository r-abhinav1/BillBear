"""
Bill-math audit warnings shown on the results page.

These are not blocking errors — they're explanations for the host/guests when
the computed split won't equal the bill's printed total, usually because the
group collectively didn't assign every item to someone.
"""
from typing import Any, Dict, List

from .bill_split import item_amount
from .bill_totals import read_bill
from .money import format_money


def round_off_tolerance(currency) -> float:
    """
    Bills round the grand total to the currency's cash step ("round off": up
    to ₹1, 5 Rappen in Switzerland). The split's shares add up exactly, so
    that rounding is the only difference not worth reporting.
    """
    return float(currency.step)


def audit_bill(room: Dict[str, Any], bill_split: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Return a list of {severity, title, detail} dicts. Severities:
      - "note"  : worth a look, but the money still adds up
      - "info"  : FYI
      - "warn"  : user probably wants to fix this
      - "error" : math is clearly off
    """
    warnings: List[Dict[str, str]] = []

    items = room.get("items", [])
    selections = room.get("selections", {}) or {}
    bill = read_bill(room.get("ocr_data"))
    currency = bill["currency"]

    def money(value: float) -> str:
        return format_money(value, currency)

    bill_subtotal = bill["subtotal"] or 0.0
    bill_total = bill["total"] or 0.0
    reported_grand = float(bill_split.get("totals", {}).get("grand_total", 0.0))

    # --- Items nobody selected ----------------------------------------------
    item_sharing = bill_split.get("item_sharing", {})
    unassigned = [name for name, count in item_sharing.items() if not count]
    if unassigned:
        orphan_total = sum(item_amount(i) for i in items if i.get("name") in unassigned)
        warnings.append({
            "severity": "warn",
            "title": f"{len(unassigned)} item(s) not picked by anyone",
            "detail": (
                f"Nobody selected: {', '.join(unassigned[:6])}"
                + ("…" if len(unassigned) > 6 else "")
                + f" (~{money(orphan_total)}). That amount is not included in anyone's share."
            ),
        })

    # --- Units of a several-of dish that nobody owns --------------------------
    for miss in room.get("quantity_mismatches") or []:
        left = miss["quantity"] - miss["claimed"]
        warnings.append({
            "severity": "warn",
            "title": f"{miss['name']}: {miss['claimed']} of {miss['quantity']} claimed",
            "detail": (
                f"Nobody claimed the other {left} or chose to share {'it' if left == 1 else 'them'} "
                f"(~{money(miss.get('unowned', 0))}). That amount is not included in anyone's share."
            ),
        })

    # --- Ordered subtotal vs printed bill subtotal --------------------------
    unowned = sum(float(m.get("unowned") or 0) for m in room.get("quantity_mismatches") or [])
    ordered_subtotal = sum(
        item_amount(i)
        for i in items
        if item_sharing.get(i.get("name"), 0) > 0
    ) - unowned
    if bill_subtotal > 0 and round(abs(ordered_subtotal - bill_subtotal), currency.decimals) > 0.0:
        warnings.append({
            "severity": "info",
            "title": "Items paid for < bill subtotal",
            "detail": (
                f"The group covered {money(ordered_subtotal)} worth of items, "
                f"but the bill subtotal is {money(bill_subtotal)}. "
                f"Difference: {money(bill_subtotal - ordered_subtotal)} (what nobody picked or claimed)."
            ),
        })

    # --- Grand total vs printed bill total ----------------------------------
    tolerance = round_off_tolerance(currency)
    if bill_total > 0 and round(abs(reported_grand - bill_total), currency.decimals) > tolerance:
        over = reported_grand - bill_total
        extras = sum(c["amount"] for c in bill["charges"] if c["kind"] == "charge")
        if over > 0:
            # More than the bill: the rows (or prices) add something its total doesn't have.
            if extras and abs(over - extras) <= tolerance:
                why = (
                    f"That's exactly the taxes and charges ({money(extras)}), so the bill total doesn't "
                    "include them. If the host added them by hand, the bill total needs them too."
                )
            elif extras:
                why = (
                    f"The split includes {money(extras)} of taxes and charges. This usually means the host "
                    "added or changed a charge or a price without updating the bill total."
                )
            else:
                why = "This usually means a price was changed without updating the bill total."
            fix = "If the shares are right, the host can tap Fix the bill below to correct the total. Nobody has to pick again."
            detail = (
                f"The shares add up to {money(reported_grand)}, but the bill total is {money(bill_total)}: "
                f"{money(over)} more than the bill. {why} {fix}"
            )
        else:
            detail = (
                f"The shares add up to {money(reported_grand)}, but the bill total is {money(bill_total)}: "
                f"{money(-over)} less than the bill. Usually some dishes weren't picked, or the bill has a "
                "tax or charge that isn't in the rows yet. The host can reopen the picks to assign the "
                "missing dishes, or tap Fix the bill below to add the charge."
            )
        warnings.append({
            "severity": "info",
            "title": "Split total doesn't match bill total",
            "detail": detail,
        })

    # --- Users with no selection (paying nothing, for clarity) --------------
    all_users = list(selections.keys())
    noselect = [u for u in all_users if not selections.get(u)]
    if noselect:
        warnings.append({
            "severity": "info",
            "title": f"{len(noselect)} user(s) didn't pick anything",
            "detail": (
                f"{', '.join(noselect)} owe {money(0)}: they didn't select any items, "
                "so they're not included in the split of taxes and charges."
            ),
        })

    # --- All clean ----------------------------------------------------------
    if all(w["severity"] == "note" for w in warnings):
        warnings.append({
            "severity": "ok",
            "title": "✅ Math reconciled",
            "detail": "Every item is picked, and the split sums to the bill's total.",
        })

    return warnings
