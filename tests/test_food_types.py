"""Splitting by food type: the rate card (host counts) and everyone picking their types."""
import json
import re

import psycopg
import pytest

from billbear.bill.food_types import clean_counts, clean_types, pot_lines, rate_card, stored_counts
from billbear.bill.split import calculate_bill_split
from tests.conftest import TEST_DATABASE_URL, join, needs_db, new_client, post_json, room, submit

# --------------------------------------------------------------------------
# The maths, without a database
# --------------------------------------------------------------------------

# The example bill from the design: 10 people, dishes in four types plus water.
BILL = [
    ("Paneer tikka", 420, "veg", 1), ("Dal makhani", 380, "veg", 1), ("Veg biryani", 640, "veg", 2),
    ("Butter naan", 480, "veg", 8), ("Jeera rice", 260, "veg", 1), ("Gobi manchurian", 300, "veg", 1),
    ("Chicken 65", 460, "nonveg", 1), ("Mutton rogan josh", 620, "nonveg", 1),
    ("Chicken biryani", 1050, "nonveg", 3), ("Fish tikka", 540, "nonveg", 1), ("Prawn ghee roast", 720, "nonveg", 1),
    ("Egg curry", 280, "egg", 1), ("Masala omelette", 240, "egg", 2),
    ("Kingfisher pint", 1800, "alcohol", 6), ("Old Monk", 1120, "alcohol", 4), ("Sula red", 1900, "alcohol", 1),
    ("Mineral water", 160, "other", 4),
]
CHARGES = [
    {"label": "CGST", "amount": 284.25, "kind": "charge"},
    {"label": "SGST", "amount": 284.25, "kind": "charge"},
    {"label": "Service charge", "amount": 568.5, "kind": "charge"},
]


def _room(items=BILL, charges=CHARGES, tax_split=None, currency="INR"):
    ocr = {"currency": currency, "charges": charges}
    if tax_split:
        ocr["taxSplit"] = tax_split
    return {
        "items": [
            {"id": n, "name": name, "amount": price, "price": str(price), "category": cat, "quantity": qty}
            for n, (name, price, cat, qty) in enumerate(items)
        ],
        "ocr_data": ocr,
    }


COUNTS = {"veg": 10, "nonveg": 6, "egg": 3, "alcohol": 4}


def test_rate_card_gives_each_type_its_part_of_the_taxes():
    card = rate_card(_room(), COUNTS, 10)
    each = {r["key"]: r["each"] for r in card["rows"]}
    # Charges are exactly 10% of the dishes on this bill.
    assert each == {"veg": 272.8, "nonveg": 621.5, "egg": 190.67, "alcohol": 1325.5}
    assert card["everyone"]["each"] == 17.6          # water, 160 + 10%, 10 ways
    assert card["bill_total"] == 12507.0
    # Egg doesn't divide by 3: the card says how far the rounded shares are off.
    assert card["shares_total"] == 12507.01 and card["difference"] == 0.01
    assert card["examples"][0] == {"keys": ["veg"], "labels": ["Veg"], "amount": 290.4}


def test_rate_card_with_taxes_split_equally_puts_them_on_the_everyone_line():
    card = rate_card(_room(tax_split="equal"), COUNTS, 10)
    each = {r["key"]: r["each"] for r in card["rows"]}
    assert each["veg"] == 248.0 and each["alcohol"] == 1205.0
    assert card["everyone"]["each"] == round(16 + 1137 / 10, 2)
    assert card["everyone"]["charges_in"] is True
    assert abs(card["difference"]) < 0.05


def test_rate_card_without_shared_dishes_or_charges_has_no_everyone_line():
    items = [("Dal", 300, "veg", 1), ("Chicken", 600, "nonveg", 1)]
    card = rate_card(_room(items, charges=[]), {"veg": 3, "nonveg": 2}, 3)
    assert card["everyone"] is None
    assert [r["each"] for r in card["rows"]] == [100.0, 300.0]
    assert card["difference"] == 0


def test_rate_card_discounts_follow_the_food():
    items = [("Dal", 600, "veg", 1), ("Chicken", 400, "nonveg", 1)]
    charges = [{"label": "Happy hour", "amount": 100, "kind": "discount"}]
    card = rate_card(_room(items, charges), {"veg": 2, "nonveg": 1}, 2)
    assert [r["each"] for r in card["rows"]] == [270.0, 360.0]    # 10% off each pot
    assert card["bill_total"] == 900.0 and card["difference"] == 0


def test_rate_card_in_yen_rounds_to_whole_yen():
    items = [("Ramen", 1000, "nonveg", 1), ("Gyoza", 500, "nonveg", 1), ("Beer", 700, "alcohol", 1)]
    card = rate_card(_room(items, charges=[], currency="JPY"), {"nonveg": 3, "alcohol": 2}, 3)
    assert [r["each"] for r in card["rows"]] == [500.0, 350.0]


def test_clean_counts_refuses_what_the_host_cannot_mean():
    present = ["veg", "alcohol"]
    assert clean_counts({"veg": 4, "alcohol": "2", "egg": 9}, present, 5) == {"veg": 4, "alcohol": 2}
    for bad, words in [
        ({"veg": 0, "alcohol": 1}, "at least 1"),
        ({"veg": 6, "alcohol": 1}, "only 5"),
        ({"veg": True, "alcohol": 1}, "how many"),
        ({"veg": 2.5, "alcohol": 1}, "how many"),
        ({"veg": "two", "alcohol": 1}, "whole number"),
        ({"alcohol": 1}, "how many"),
        ([], "each type"),
    ]:
        with pytest.raises(ValueError, match=words):
            clean_counts(bad, present, 5)


def test_stored_counts_fit_the_bill_as_it_is_now():
    # A new type starts at everyone; a count above the head count comes down.
    assert stored_counts({"veg": 8, "gone": 3}, ["veg", "egg"], 6) == {"veg": 6, "egg": 6}
    assert stored_counts(None, ["veg"], 4) == {"veg": 4}


def test_clean_types_keeps_only_types_on_the_bill():
    assert clean_types(["alcohol", "VEG", "veg", "other", "meat", 3, None], ["veg", "nonveg", "alcohol"]) == [
        "veg", "alcohol"]


# --------------------------------------------------------------------------
# Rooms, end to end
# --------------------------------------------------------------------------

TYPED = [
    ("Dal", "300", "veg", 1), ("Naan", "200", "veg", 4), ("Chicken", "600", "nonveg", 1),
    ("Beer", "500", "alcohol", 2), ("Water", "100", "other", 1),
]


def make_typed_room(host, num_people=3, items=TYPED):
    receipt = {
        "items": [{"name": n, "price": p, "category": c, "quantity": q} for n, p, c, q in items],
        "extraction_meta": {"source": "vision_llm"},
    }
    resp = host.post("/create", data={
        "host_name": "Alice", "room_name": "Dinner", "num_people": str(num_people),
        "receipt_json": json.dumps(receipt),
    })
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    return re.search(r"/edit/([A-Z0-9]{6})", resp.headers["Location"]).group(1)


def edit_form(code, **changes):
    r = room(code)
    form = {
        "item_id": [str(i["id"]) for i in r["items"]],
        "item_name": [i["raw_name"] for i in r["items"]],
        "item_price": [str(i["amount"]) for i in r["items"]],
        "item_category": [i["category"] for i in r["items"]],
        "item_quantity": [str(i["quantity"]) for i in r["items"]],
        "tax_split_set": "1", "tax_proportional": "1",
    }
    form.update(changes)
    return form


def send_types(client, code, name, types, submission_id=""):
    return client.post(
        f"/room/{code}/user/{name}/select-items",
        data={"split_mode": "types", "selected_types": types, "submission_id": submission_id},
        headers={"X-Requested-With": "fetch"},
    )


def typed_table(mode="types"):
    host = new_client()
    code = make_typed_room(host)
    bob, cara = new_client(), new_client()
    join(bob, code, "Bob")
    join(cara, code, "Cara")
    resp = host.post(f"/edit/{code}", data=edit_form(code, next_mode=mode))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    return code, host, bob, cara


@needs_db
def test_check_the_bill_offers_the_food_type_link_and_saving_with_it_switches():
    host = new_client()
    code = make_typed_room(host)
    page = host.get(f"/edit/{code}").get_data(as_text=True)
    assert "Big bill? Split by food type instead" in page
    assert "Save and open the room" in page

    resp = host.post(f"/edit/{code}", data=edit_form(code, next_mode="types"))
    assert resp.headers["Location"].endswith(f"/room/{code}")
    assert room(code)["split_mode"] == "types"
    assert "Change how to split" in host.get(f"/edit/{code}").get_data(as_text=True)

    resp = host.post(f"/edit/{code}", data=edit_form(code, next_mode="counts"))
    assert resp.headers["Location"].endswith(f"/room/{code}/rate-card")
    assert room(code)["split_mode"] == "counts"
    # A plain Save keeps the way the room splits.
    host.post(f"/edit/{code}", data=edit_form(code))
    assert room(code)["split_mode"] == "counts"
    # Anything else in the field is ignored.
    host.post(f"/edit/{code}", data=edit_form(code, next_mode="'; drop table rooms; --"))
    assert room(code)["split_mode"] == "counts"


@needs_db
def test_everyone_picks_types_and_the_split_adds_up():
    code, host, bob, cara = typed_table()
    assert "Pick my food types" in bob.get(f"/room/{code}").get_data(as_text=True)
    page = bob.get(f"/room/{code}/user/Bob/select-items").get_data(as_text=True)
    assert 'name="selected_types" value="veg"' in page and 'value="other"' not in page

    assert send_types(host, code, "Alice", ["veg", "nonveg"]).status_code == 200
    assert send_types(bob, code, "Bob", ["veg", "alcohol"]).status_code == 200
    assert send_types(cara, code, "Cara", ["alcohol"]).status_code == 200
    r = room(code)
    assert r["selections"]["Cara"] == ["Beer", "Water"]          # her type, plus what everyone shares
    assert r["portions"]["Bob"]["Naan"] == 0                    # several-of: shared, never claimed
    assert r["quantity_mismatches"] == [] and r["unclaimed_items"] == []

    assert post_json(host, f"/room/{code}/finalize").status_code == 200
    split = calculate_bill_split(room(code))
    pays = {u: d["final_amount"] for u, d in split["user_breakdown"].items()}
    # Veg 500 / 2, chicken 600 / 1, beer 500 / 2, water 100 / 3.
    exact = {"Alice": 250 + 600 + 100 / 3, "Bob": 250 + 250 + 100 / 3, "Cara": 250 + 100 / 3}
    assert all(abs(pays[u] - exact[u]) <= 0.01 for u in exact), pays
    # The paisa the thirds leave over goes to someone, so the shares add up exactly.
    assert round(sum(pays.values()), 2) == 1700.0

    page = bob.get(f"/room/{code}/results").get_data(as_text=True)
    assert "All square." in page and "split 2 ways" in page and "What was in your food types" in page
    lines = pot_lines(room(code), split)
    assert [p["key"] for p in lines["Bob"]] == ["veg", "alcohol", "other"]
    pdf = bob.get(f"/room/{code}/download")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"


@needs_db
def test_picks_from_a_page_for_the_other_way_are_refused():
    code, host, bob, cara = typed_table()
    resp = submit(bob, code, "Bob", ["Dal"])                     # an old dish-by-dish page
    assert resp.status_code == 409 and resp.get_json()["code"] == "MODE_CHANGED"
    assert "food type" in resp.get_json()["message"]
    assert "Bob" not in room(code)["submitted_users"]

    post_json(host, f"/room/{code}/host/split-mode", {"mode": "items"})
    resp = send_types(bob, code, "Bob", ["veg"])                 # a food-type page left open
    assert resp.status_code == 409 and resp.get_json()["code"] == "MODE_CHANGED"


@needs_db
def test_made_up_types_are_dropped_and_the_database_refuses_them_too():
    code, host, bob, cara = typed_table()
    assert send_types(bob, code, "Bob", ["veg", "meat", "other", "veg", "<script>"]).status_code == 200
    bob_row = next(p for p in room(code)["participants"] if p["name"] == "Bob")
    assert bob_row["categories"] == ["veg"]
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE participants SET categories = ARRAY['meat'] WHERE name = 'Bob'")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE rooms SET split_mode = 'free' WHERE code = %s", (code,))


@needs_db
def test_sending_types_twice_with_the_same_id_is_one_send():
    code, host, bob, cara = typed_table()
    assert send_types(bob, code, "Bob", ["veg"], "sid-1").get_json()["idempotent"] is False
    assert send_types(bob, code, "Bob", ["alcohol"], "sid-1").get_json()["idempotent"] is True
    bob_row = next(p for p in room(code)["participants"] if p["name"] == "Bob")
    assert bob_row["categories"] == ["veg"]


@needs_db
def test_switching_after_picks_needs_everyone_sent_back():
    code, host, bob, cara = typed_table()
    send_types(bob, code, "Bob", ["veg"])
    resp = post_json(host, f"/room/{code}/host/split-mode", {"mode": "items"})
    assert resp.status_code == 409 and resp.get_json()["code"] == "PICKS_SENT"
    assert room(code)["split_mode"] == "types" and "Bob" in room(code)["submitted_users"]

    resp = post_json(host, f"/room/{code}/host/split-mode", {"mode": "items", "reopen": True})
    assert resp.status_code == 200 and resp.get_json()["reopened"] == 1
    r = room(code)
    assert r["split_mode"] == "items" and r["submitted_users"] == set()
    # What Bob tapped is kept, for switching back.
    post_json(host, f"/room/{code}/host/split-mode", {"mode": "types"})
    assert next(p for p in room(code)["participants"] if p["name"] == "Bob")["categories"] == ["veg"]


@needs_db
def test_only_the_host_switches_and_only_to_known_ways():
    code, host, bob, cara = typed_table()
    resp = post_json(bob, f"/room/{code}/host/split-mode", {"mode": "items"})
    assert resp.status_code == 403
    assert post_json(new_client(), f"/room/{code}/host/split-mode", {"mode": "items"}).status_code == 403
    for body in ({"mode": "everything"}, {"mode": ["items"]}, {}):
        assert post_json(host, f"/room/{code}/host/split-mode", body).status_code == 400
    assert room(code)["split_mode"] == "types"


@needs_db
def test_cannot_split_by_type_when_no_dish_has_a_type():
    host = new_client()
    code = make_typed_room(host, items=[("Water", "100", "other", 1), ("Tip jar", "50", "other", 1)])
    resp = post_json(host, f"/room/{code}/host/split-mode", {"mode": "types"})
    assert resp.status_code == 409 and resp.get_json()["code"] == "NO_FOOD_TYPES"


@needs_db
def test_the_host_can_still_make_someone_pick_again():
    code, host, bob, cara = typed_table()
    send_types(bob, code, "Bob", ["veg", "alcohol"])
    assert post_json(host, f"/room/{code}/host/revoke/Bob").status_code == 200
    assert "Bob" not in room(code)["submitted_users"]
    page = bob.get(f"/room/{code}/user/Bob/select-items").get_data(as_text=True)
    # Their last types are ticked, ready to change or send again.
    assert re.search(r'value="veg" checked', page) and re.search(r'value="alcohol" checked', page)
    assert not re.search(r'value="nonveg" checked', page)


@needs_db
def test_retyping_a_dish_sends_people_back_but_a_new_quantity_does_not():
    code, host, bob, cara = typed_table()
    send_types(bob, code, "Bob", ["veg"])
    r = room(code)
    quantities = [str(i["quantity"]) for i in r["items"]]
    quantities[1] = "6"                                            # Naan x4 -> x6, same price
    host.post(f"/edit/{code}", data=edit_form(code, item_quantity=quantities))
    assert "Bob" in room(code)["submitted_users"]

    categories = [i["category"] for i in room(code)["items"]]
    categories[0] = "nonveg"                                       # Dal moves to another pot
    host.post(f"/edit/{code}", data=edit_form(code, item_category=categories))
    assert "Bob" not in room(code)["submitted_users"]


@needs_db
def test_a_type_nobody_picked_warns_the_host_only():
    code, host, bob, cara = typed_table()
    send_types(host, code, "Alice", ["veg"])
    send_types(bob, code, "Bob", ["veg"])
    send_types(cara, code, "Cara", ["veg", "nonveg"])
    hs = host.get(f"/room/{code}/status").get_json()
    assert [p["key"] for p in hs["unpicked_types"]] == ["alcohol"]
    assert hs["type_pickers"] == {"veg": 3, "nonveg": 1, "alcohol": 0}
    assert hs["picks"]["Cara"] == ["Veg", "Non-veg"]
    bs = bob.get(f"/room/{code}/status").get_json()
    assert bs["unpicked_types"] == [] and bs["picks"] is None
    assert bs["my_types"] == ["veg"] and bs["my_estimate"] > 0
    assert post_json(host, f"/room/{code}/finalize").status_code == 200
    page = host.get(f"/room/{code}/results").get_data(as_text=True)
    assert "Nobody picked alcohol" in page


@needs_db
def test_rate_card_counts_are_saved_and_checked():
    code, host, bob, cara = typed_table("counts")
    page = host.get(f"/room/{code}/rate-card")
    assert page.status_code == 200 and "How many had each?" in page.get_data(as_text=True)

    resp = post_json(host, f"/room/{code}/rate-card", {"head": 4, "counts": {"veg": 4, "nonveg": 2, "alcohol": 2}})
    data = resp.get_json()
    assert resp.status_code == 200, data
    assert data["counts"] == {"veg": 4, "nonveg": 2, "alcohol": 2}
    assert data["each"] == {"veg": "₹125.00", "nonveg": "₹300.00", "alcohol": "₹250.00"}
    assert "Everyone also pays" in data["html"] and "Veg: ₹125.00" in data["share_text"]
    assert room(code)["num_people"] == 4

    for body in (
        {"head": 4, "counts": {"veg": 5, "nonveg": 2, "alcohol": 2}},     # more than at the table
        {"head": 4, "counts": {"veg": 0, "nonveg": 2, "alcohol": 2}},
        {"head": 4, "counts": {"veg": 2, "nonveg": 2}},                   # a type left out
        {"head": 2, "counts": {"veg": 2, "nonveg": 2, "alcohol": 2}},     # three have joined
        {"head": 99, "counts": {"veg": 2, "nonveg": 2, "alcohol": 2}},
        {"head": True, "counts": {"veg": 1, "nonveg": 1, "alcohol": 1}},
        {"head": "4", "counts": {"veg": 1, "nonveg": 1, "alcohol": 1}},
    ):
        assert post_json(host, f"/room/{code}/rate-card", body).status_code == 400, body
    assert room(code)["type_counts"] == {"veg": 4, "nonveg": 2, "alcohol": 2}

    pdf = host.get(f"/room/{code}/rate-card/download")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    assert "rate_card.pdf" in pdf.headers["Content-Disposition"]


@needs_db
def test_rate_card_is_the_hosts_alone():
    code, host, bob, cara = typed_table("counts")
    assert bob.get(f"/room/{code}/rate-card").status_code == 302
    assert bob.get(f"/room/{code}/rate-card/download").status_code == 302
    assert new_client().get(f"/room/{code}/rate-card").status_code == 403
    resp = post_json(bob, f"/room/{code}/rate-card", {"head": 3, "counts": {"veg": 1, "nonveg": 1, "alcohol": 1}})
    assert resp.status_code == 403
    # Guests have nothing to pick, and the host has nothing to split.
    assert bob.get(f"/room/{code}/user/Bob/select-items").headers["Location"].endswith(f"/room/{code}")
    assert "nothing to pick" in bob.get(f"/room/{code}").get_data(as_text=True)
    resp = send_types(bob, code, "Bob", ["veg"])
    assert resp.status_code == 409 and resp.get_json()["code"] == "MODE_CHANGED"
    resp = post_json(host, f"/room/{code}/finalize")
    assert resp.status_code == 409 and resp.get_json()["code"] == "RATE_CARD"


@needs_db
def test_rate_card_page_only_for_a_rate_card_room():
    code, host, bob, cara = typed_table("types")
    assert host.get(f"/room/{code}/rate-card").status_code == 302
    resp = post_json(host, f"/room/{code}/rate-card", {"head": 3, "counts": {"veg": 1, "nonveg": 1, "alcohol": 1}})
    assert resp.status_code == 409 and resp.get_json()["code"] == "MODE_CHANGED"


@needs_db
def test_nudge_sits_in_each_guests_manage_sheet():
    code, host, bob, cara = typed_table("items")
    page = host.get(f"/room/{code}/waiting/Alice").get_data(as_text=True)
    assert 'id="sheetNudge"' in page and 'id="nudgeBtn"' not in page
    assert 'id="sheetNudge"' not in bob.get(f"/room/{code}/waiting/Bob").get_data(as_text=True)


def test_help_explains_splitting_by_food_type(client):
    page = client.get("/help/food-type").get_data(as_text=True)
    assert "By food type" in page and "I know how many had what" in page and "Let everyone pick" in page
    assert "₹3,000.00" in page
    assert 'href="/help/food-type"' in client.get("/help").get_data(as_text=True)
    host = client.get("/help/host").get_data(as_text=True)
    assert "Nudge them" in host and 'href="/help/food-type"' in host
