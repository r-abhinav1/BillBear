"""
Pure rows -> room-dict adapter.

`utils/bill_split.py`, `utils/bill_audit.py` and the templates were written
against the old Redis JSON blob (`users`, `selections`, `submitted_users`,
`items`, `ocr_data`, ...). This module rebuilds that shape from normalized
rows so those consumers keep working unchanged.

Selections are keyed by item *display name*, so duplicate item names
("Naan", "Naan") get a read-time suffix ("Naan (2)") to keep them distinct.
"""
from collections import defaultdict
from typing import Any, Dict, Iterable, List

from .money import format_money, get_currency


def unique_display_names(names: List[str]) -> List[str]:
    taken = set(names)
    seen: Dict[str, int] = defaultdict(int)
    result = []
    for name in names:
        seen[name] += 1
        if seen[name] == 1:
            result.append(name)
            continue
        n = seen[name]
        candidate = f"{name} ({n})"
        while candidate in taken:
            n += 1
            candidate = f"{name} ({n})"
        taken.add(candidate)
        result.append(candidate)
    return result


def assemble_room(
    room_row: Dict[str, Any],
    participant_rows: Iterable[Dict[str, Any]],
    item_rows: Iterable[Dict[str, Any]],
    claim_rows: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    currency = get_currency((room_row.get("ocr_data") or {}).get("currency"))
    participants = sorted(participant_rows, key=lambda p: (not p["is_host"], p["joined_at"], p["id"]))
    items_sorted = sorted(item_rows, key=lambda i: (i["position"], i["id"]))

    display = unique_display_names([i["name"] for i in items_sorted])
    items = [
        {
            "id": row["id"],
            "name": display[idx],
            "raw_name": row["name"],
            "price": format_money(row["price"], currency),
            "amount": row["price"],
            "category": row.get("category") or "other",
            # How many of this dish the bill lists; `price` is for all of them.
            "quantity": int(row.get("quantity") or 1),
        }
        for idx, row in enumerate(items_sorted)
    ]
    item_name_by_id = {item["id"]: item["name"] for item in items}
    quantity_by_id = {item["id"]: item["quantity"] for item in items}

    # participant id -> {item id: portions claimed}
    claims_by_pid: Dict[int, Dict[int, int]] = defaultdict(dict)
    for claim in claim_rows:
        if claim["item_id"] in item_name_by_id:
            # Several-of dishes: whole units claimed, or 0 for "sharing what's left".
            # Rows from before portions existed, and single dishes, count as 1.
            raw = claim.get("portions")
            several = quantity_by_id[claim["item_id"]] > 1
            claims_by_pid[claim["participant_id"]][claim["item_id"]] = (
                max(0, int(raw)) if several and raw is not None else 1
            )

    position_by_id = {item["id"]: idx for idx, item in enumerate(items)}
    host = next((p for p in participants if p["is_host"]), None)

    users, submitted, selections, portions, claim_ids, people = [], set(), {}, {}, {}, []
    for p in participants:
        name = p["name"]
        mine = claims_by_pid.get(p["id"], {})
        ids = sorted(mine, key=lambda i: position_by_id[i])
        users.append(name)
        claim_ids[name] = ids
        is_submitted = p["submitted_at"] is not None
        if is_submitted:
            submitted.add(name)
            selections[name] = [item_name_by_id[i] for i in ids]
            portions[name] = {item_name_by_id[i]: mine[i] for i in ids}
        people.append({
            "id": p["id"],
            "name": name,
            "is_host": p["is_host"],
            "submitted": is_submitted,
            "submission_id": p.get("submission_id") or "",
            "has_device": p.get("user_id_hash") is not None,
            "claim_ids": ids,
            "claim_portions": {i: mine[i] for i in ids},
        })

    claimed_by_submitted = {i for p in people if p["submitted"] for i in p["claim_ids"]}
    unclaimed = [item["name"] for item in items if item["id"] not in claimed_by_submitted]

    # For dishes the bill lists several of: who holds what. `portions` is whole
    # units claimed, or 0 for someone sharing whatever is left. `taken` also
    # counts people who reopened their picks: what they last sent stays on hold
    # until they send again. `sent` is only what is currently submitted.
    several_ids = {item["id"] for item in items if item["quantity"] > 1}
    taken: Dict[int, List[Dict[str, Any]]] = {item_id: [] for item_id in several_ids}
    sent: Dict[int, List[Dict[str, Any]]] = {item_id: [] for item_id in several_ids}
    for p in people:
        for item_id, count in p["claim_portions"].items():
            if item_id in several_ids:
                taken[item_id].append({"name": p["name"], "portions": count})
                if p["submitted"]:
                    sent[item_id].append({"name": p["name"], "portions": count})

    # Units that someone should own but nobody does: the dish was picked, not
    # every unit was claimed, and nobody is sharing the rest. Those units are
    # charged to no one, so the host is asked to check before splitting.
    quantity_mismatches = []
    for item in items:
        held = sent.get(item["id"]) or []
        if not held or any(h["portions"] == 0 for h in held):
            continue
        claimed = sum(h["portions"] for h in held)
        if claimed < item["quantity"]:
            quantity_mismatches.append({
                "name": item["name"],
                "quantity": item["quantity"],
                "claimed": claimed,
                "by": held,
                "unowned": float(item["amount"] or 0) * (item["quantity"] - claimed) / item["quantity"],
            })

    ocr_data = dict(room_row.get("ocr_data") or {})
    ocr_data["items"] = [
        {"name": i["name"], "price": i["price"], "category": i["category"], "quantity": i["quantity"]}
        for i in items
    ]

    num_people = int(room_row["num_people"])
    still_editing = [p["name"] for p in people if not p["submitted"]]
    waiting_for = max(0, num_people - len(people))

    return {
        "code": room_row["code"],
        "room_name": room_row["room_name"],
        "host_name": host["name"] if host else "",
        "host_participant_id": host["id"] if host else None,
        "num_people": num_people,
        "items": items,
        "ocr_data": ocr_data,
        # {code, name, symbol, decimals, step}; amounts are shown in it, never converted.
        "currency": currency.public(),
        "ocr_source": room_row.get("ocr_source") or "manual",
        "users": users,
        "submitted_users": submitted,
        "selections": selections,
        "portions": portions,
        "quantity_mismatches": quantity_mismatches,
        "taken": taken,
        "claim_ids": claim_ids,
        "participants": people,
        "unclaimed_items": unclaimed,
        "still_editing": still_editing,
        "waiting_for": waiting_for,
        "can_finalize": not still_editing and waiting_for == 0 and bool(people),
        "finalized": room_row.get("finalized_at") is not None,
        "finalized_at": room_row.get("finalized_at"),
        "created_at": room_row.get("created_at"),
        "last_activity_at": room_row.get("last_activity_at"),
    }
