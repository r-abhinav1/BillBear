"""End-to-end room flow over HTTP against a real Postgres."""
import json

from tests.conftest import item_ids, join, make_room, needs_db, new_client, post_json, room, submit

pytestmark = needs_db


def _full_room():
    host = new_client()
    code = make_room(host, num_people=3)
    bob, cara = new_client(), new_client()
    assert join(bob, code, "Bob").status_code == 302
    assert join(cara, code, "Cara").status_code == 302
    return code, host, bob, cara


def test_create_sets_host_session_and_items(client):
    code = make_room(client)
    r = room(code)
    assert r["host_name"] == "Alice"
    assert [i["name"] for i in r["items"]] == ["Pasta", "Pizza", "Coke"]
    assert r["items"][0]["price"] == "₹200.00"
    assert client.get(f"/edit/{code}").status_code == 200


def test_create_manual_without_items(client):
    resp = client.post("/create", data={
        "host_name": "Alice", "room_name": "Lunch", "num_people": "2", "skip_ocr": "true",
    })
    assert resp.status_code == 302


def test_create_requires_receipt_or_manual(client):
    resp = client.post("/create", data={"host_name": "Alice", "room_name": "x", "num_people": "2"})
    assert resp.status_code == 400
    assert b"enter items manually" in resp.data.lower()


def test_create_rejects_bad_people_count(client):
    resp = client.post("/create", data={
        "host_name": "Alice", "room_name": "x", "num_people": "51", "skip_ocr": "true",
    })
    assert resp.status_code == 400


def test_room_get_is_read_only(client):
    code = make_room(client)
    before = room(code)["last_activity_at"]
    assert client.get(f"/room/{code}").status_code == 200
    assert room(code)["last_activity_at"] == before


def test_full_flow_finalize_and_results():
    code, host, bob, cara = _full_room()
    assert submit(host, code, "Alice", ["Pasta"]).status_code == 200
    assert submit(bob, code, "Bob", ["Pasta", "Pizza"]).status_code == 200

    # Cara still picking -> finalize refused, naming her
    resp = post_json(host, f"/room/{code}/finalize")
    assert resp.status_code == 409
    assert resp.get_json()["still_editing"] == ["Cara"]

    # Results gated before finalize
    assert host.get(f"/room/{code}/results").status_code == 302
    assert host.get(f"/room/{code}/download").status_code == 302

    assert submit(cara, code, "Cara", ["Coke"]).status_code == 200
    status = host.get(f"/room/{code}/status").get_json()
    assert status["can_finalize"] and status["unclaimed_items"] == []

    resp = post_json(host, f"/room/{code}/finalize")
    assert resp.status_code == 200
    # Idempotent second click
    assert post_json(host, f"/room/{code}/finalize").get_json()["already_finalized"] is True

    page = bob.get(f"/room/{code}/results")
    assert page.status_code == 200
    assert "Bob" in page.get_data(as_text=True)


def test_finalize_requires_party_full():
    host = new_client()
    code = make_room(host, num_people=3)
    submit(host, code, "Alice", ["Pasta"])
    resp = post_json(host, f"/room/{code}/finalize")
    assert resp.status_code == 409 and resp.get_json()["waiting_for"] == 2
    # Host lowers party size instead of force-completing
    assert post_json(host, f"/room/{code}/host/num-people", {"num_people": 1}).status_code == 200
    assert post_json(host, f"/room/{code}/finalize").status_code == 200


def test_solo_room_finalizes():
    host = new_client()
    code = make_room(host, num_people=1)
    submit(host, code, "Alice", ["Pasta", "Pizza", "Coke"])
    assert post_json(host, f"/room/{code}/finalize").status_code == 200


def test_writes_rejected_after_finalize_and_reopen_clears():
    code, host, bob, cara = _full_room()
    for c, n in ((host, "Alice"), (bob, "Bob"), (cara, "Cara")):
        submit(c, code, n, ["Pasta"])
    assert post_json(host, f"/room/{code}/finalize").status_code == 200

    assert submit(bob, code, "Bob", ["Pizza"]).status_code == 409
    assert post_json(bob, f"/room/{code}/user/Bob/revoke").status_code == 409
    assert post_json(host, f"/room/{code}/host/revoke/Bob").status_code == 409
    assert post_json(host, f"/room/{code}/host/remove-user", {"target_user": "Bob"}).status_code == 409
    assert post_json(host, f"/room/{code}/host/num-people", {"num_people": 4}).status_code == 409
    assert host.get(f"/edit/{code}").status_code == 409
    assert join(new_client(), code, "Dan").status_code == 409

    assert post_json(host, f"/room/{code}/host/reopen-selections").status_code == 200
    r = room(code)
    assert not r["finalized"] and r["submitted_users"] == set()
    # Picks survive as drafts
    assert r["claim_ids"]["Bob"] == [int(x) for x in item_ids(code, "Pasta")]
    # Results now redirect back to picking
    assert bob.get(f"/room/{code}/results").status_code == 302


def test_idempotent_submit_same_id():
    host = new_client()
    code = make_room(host, num_people=1)
    first = submit(host, code, "Alice", ["Pasta"], submission_id="sid-1")
    assert first.get_json()["idempotent"] is False
    again = submit(host, code, "Alice", ["Pizza"], submission_id="sid-1")
    assert again.get_json()["idempotent"] is True
    assert room(code)["selections"]["Alice"] == ["Pasta"]
    other = submit(host, code, "Alice", ["Pizza"], submission_id="sid-2")
    assert other.get_json()["idempotent"] is False
    assert room(code)["selections"]["Alice"] == ["Pizza"]


def test_submit_filters_foreign_item_ids():
    a, b = new_client(), new_client()
    code_a = make_room(a, num_people=1)
    code_b = make_room(b, num_people=1)
    foreign = item_ids(code_b, "Pasta")
    resp = a.post(
        f"/room/{code_a}/user/Alice/select-items",
        data={"selected_items": foreign + item_ids(code_a, "Coke")},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 200
    assert room(code_a)["selections"]["Alice"] == ["Coke"]


def test_legacy_name_checkbox_values_still_work():
    host = new_client()
    code = make_room(host, num_people=1)
    resp = host.post(
        f"/room/{code}/user/Alice/select-items",
        data={"selected_items": ["Pizza"]},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 200
    assert room(code)["selections"]["Alice"] == ["Pizza"]


def test_empty_selection_allowed():
    host = new_client()
    code = make_room(host, num_people=1)
    assert submit(host, code, "Alice", []).status_code == 200
    assert "Alice" in room(code)["submitted_users"]


def test_duplicate_item_names_are_distinct():
    host = new_client()
    code = make_room(host, num_people=1, items=[("Naan", "50"), ("Naan", "50"), ("Dal", "150")])
    names = [i["name"] for i in room(code)["items"]]
    assert names == ["Naan", "Naan (2)", "Dal"]
    submit(host, code, "Alice", ["Naan (2)"])
    assert room(code)["selections"]["Alice"] == ["Naan (2)"]
    # Ambiguous legacy name is rejected instead of guessed
    resp = host.post(
        f"/room/{code}/user/Alice/select-items",
        data={"selected_items": ["Naan"]},
        headers={"X-Requested-With": "fetch"},
    )
    assert resp.status_code == 409


def test_edit_items_diffs_by_id_and_resets_submissions():
    code, host, bob, cara = _full_room()
    submit(bob, code, "Bob", ["Pasta", "Coke"])
    r = room(code)
    pasta, pizza, coke = (i["id"] for i in r["items"])

    # Rename Pizza's price, drop Coke, add Garlic Bread
    resp = host.post(f"/edit/{code}", data={
        "item_id": [str(pasta), str(pizza), ""],
        "item_name": ["Pasta", "Pizza", "Garlic Bread"],
        "item_price": ["₹200.00", "450", "120"],
        "subtotal": "770", "total": "770",
    })
    assert resp.status_code == 302
    r = room(code)
    assert [i["id"] for i in r["items"]][:2] == [pasta, pizza]
    assert [i["name"] for i in r["items"]] == ["Pasta", "Pizza", "Garlic Bread"]
    assert r["items"][1]["price"] == "₹450.00"
    # Bob is back to picking; his Pasta draft survives, Coke claim cascaded away
    assert "Bob" not in r["submitted_users"]
    assert r["claim_ids"]["Bob"] == [pasta]


def test_edit_without_changes_keeps_submissions():
    code, host, bob, cara = _full_room()
    submit(bob, code, "Bob", ["Pasta"])
    r = room(code)
    ids = [str(i["id"]) for i in r["items"]]
    host.post(f"/edit/{code}", data={
        "item_id": ids,
        "item_name": ["Pasta", "Pizza", "Coke"],
        "item_price": ["200", "400", "100"],
        "subtotal": "₹700.00", "serviceCharge": "N/A", "discount": "N/A",
        "cgst": "N/A", "sgst": "N/A", "igst": "N/A", "total": "₹700.00",
    })
    assert "Bob" in room(code)["submitted_users"]


def test_edit_rejects_bad_price_and_keeps_input(client):
    code = make_room(client)
    resp = client.post(f"/edit/{code}", data={
        "item_id": [""], "item_name": ["Soup"], "item_price": ["12.3.4"], "total": "N/A",
    })
    assert resp.status_code == 400
    assert b"Soup" in resp.data


def test_unclaimed_items_reported():
    host = new_client()
    code = make_room(host, num_people=1)
    submit(host, code, "Alice", ["Pasta"])
    status = host.get(f"/room/{code}/status").get_json()
    assert status["unclaimed_items"] == ["Pizza", "Coke"]
    assert status["can_finalize"] is True


def test_remove_user_cascades_claims():
    code, host, bob, cara = _full_room()
    submit(bob, code, "Bob", ["Pizza"])
    assert post_json(host, f"/room/{code}/host/remove-user", {"target_user": "Bob"}).status_code == 200
    r = room(code)
    assert "Bob" not in r["users"]
    # Removed user's cookie no longer works
    assert submit(bob, code, "Bob", ["Pasta"]).status_code == 403
    assert bob.get(f"/room/{code}/status").get_json()["caller_name"] is None


def test_host_cannot_remove_self(client):
    code = make_room(client)
    assert post_json(client, f"/room/{code}/host/remove-user", {"target_user": "Alice"}).status_code == 400


def test_num_people_cannot_go_below_joined():
    code, host, _bob, _cara = _full_room()
    assert post_json(host, f"/room/{code}/host/num-people", {"num_people": 2}).status_code == 400


def test_join_name_rules():
    host = new_client()
    code = make_room(host, num_people=5)
    assert join(new_client(), code, "alice ").status_code == 409          # case/space-insensitive dup
    assert join(new_client(), code, "x');alert(1);//").status_code == 400  # punctuation
    assert join(new_client(), code, "Bob/../x").status_code == 400
    assert join(new_client(), code, "Аlice").status_code == 400            # Cyrillic А + Latin
    assert join(new_client(), code, "...").status_code == 400
    assert join(new_client(), code, "a" * 31).status_code == 400
    assert join(new_client(), code, "Zoë O'Neil").status_code == 302


def test_room_full():
    host = new_client()
    code = make_room(host, num_people=2)
    assert join(new_client(), code, "Bob").status_code == 302
    assert join(new_client(), code, "Cara").status_code == 409


def test_join_with_cookie_is_reclaim_not_duplicate():
    host = new_client()
    code = make_room(host, num_people=3)
    bob = new_client()
    join(bob, code, "Bob")
    again = join(bob, code, "Bobby")
    assert again.status_code == 302 and "/user/Bob" in again.headers["Location"]
    assert room(code)["users"] == ["Alice", "Bob"]


def test_reset_device_lets_guest_rejoin_and_keep_picks():
    code, host, bob, _cara = _full_room()
    submit(bob, code, "Bob", ["Pizza"])
    new_phone = new_client()
    assert join(new_phone, code, "Bob").status_code == 409
    assert post_json(host, f"/room/{code}/host/reset-device", {"target_user": "Bob"}).status_code == 200
    # Old phone is unlinked
    assert bob.get(f"/room/{code}/status").get_json()["caller_name"] is None
    assert join(new_phone, code, "bob").status_code == 302
    r = room(code)
    assert "Bob" in r["submitted_users"] and r["selections"]["Bob"] == ["Pizza"]
    assert new_phone.get(f"/room/{code}/status").get_json()["caller_name"] == "Bob"


def test_reset_device_reclaim_allowed_after_finalize():
    host = new_client()
    code = make_room(host, num_people=2)
    bob = new_client()
    join(bob, code, "Bob")
    submit(host, code, "Alice", ["Pasta"])
    submit(bob, code, "Bob", ["Pizza"])
    post_json(host, f"/room/{code}/finalize")
    post_json(host, f"/room/{code}/host/reset-device", {"target_user": "Bob"})
    phone = new_client()
    assert join(phone, code, "Bob").status_code == 302
    assert phone.get(f"/room/{code}/results").status_code == 200


def test_host_recovery_link():
    host = new_client()
    code = make_room(host)
    page = host.get(f"/room/{code}").get_data(as_text=True)
    import re

    link = re.search(r"/room/%s/host\?k=([A-Za-z0-9_\-]+)" % code, page)
    assert link, "host link shown to host"
    token = link.group(1)

    laptop = new_client()
    # GET only shows a confirm page (link-preview bots must not rotate the host)
    assert laptop.get(f"/room/{code}/host?k={token}").status_code == 200
    assert post_json(laptop, f"/room/{code}/finalize").status_code == 403
    resp = laptop.post(f"/room/{code}/host", data={"k": token})
    assert resp.status_code == 302
    assert laptop.get(f"/room/{code}/status").get_json()["caller_is_host"] is True
    # Old host device lost host rights
    assert host.get(f"/room/{code}/status").get_json()["caller_name"] is None

    assert new_client().post(f"/room/{code}/host", data={"k": token + "x"}).status_code == 403


def test_guest_never_sees_host_link():
    code, _host, bob, _cara = _full_room()
    assert b"/host?k=" not in bob.get(f"/room/{code}").data
    assert b"/host?k=" not in bob.get(f"/room/{code}/waiting/Bob").data


def test_expired_room_is_404_and_cleanup_deletes(client):
    import psycopg

    from tests.conftest import TEST_DATABASE_URL

    code = make_room(client)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE rooms SET last_activity_at = now() - interval '25 hours' WHERE code = %s", (code,))
    assert client.get(f"/room/{code}").status_code == 404
    assert client.get(f"/room/{code}/status").status_code == 404

    resp = client.get("/api/cron/cleanup", headers={"Authorization": "Bearer test-cron-secret"})
    assert resp.status_code == 200 and resp.get_json()["rooms_deleted"] == 1


def test_my_status_and_waiting_redirects():
    host = new_client()
    code = make_room(host, num_people=1)
    assert host.get(f"/api/room/{code}/my-status").get_json()["submitted"] is False
    submit(host, code, "Alice", ["Coke"], submission_id="abc")
    data = host.get(f"/api/room/{code}/my-status").get_json()
    assert data["submitted"] and data["submission_id"] == "abc" and data["selections"] == ["Coke"]
    # Waiting room canonicalizes the URL name to the cookie's identity
    resp = host.get(f"/room/{code}/waiting/Mallory")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/waiting/Alice")
    post_json(host, f"/room/{code}/finalize")
    assert host.get(f"/room/{code}/waiting/Alice").status_code == 302


def test_qr_link_uses_lan_address_when_opened_via_localhost(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    host = new_client()
    code = make_room(host)
    page = host.get(f"/room/{code}").get_data(as_text=True)
    assert f"http://192.168.1.50/join/{code}" in page
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://billbear.example")
    page = host.get(f"/room/{code}").get_data(as_text=True)
    assert f"https://billbear.example/join/{code}" in page


def test_share_links_prefer_ngrok_tunnel(monkeypatch):
    import app as app_module

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(app_module, "_ngrok_public_url", lambda: "https://abc.ngrok-free.app")
    host = new_client()
    code = make_room(host, num_people=1)
    assert f"https://abc.ngrok-free.app/join/{code}" in host.get(f"/room/{code}").get_data(as_text=True)
    submit(host, code, "Alice", ["Pasta"])
    post_json(host, f"/room/{code}/finalize")
    results = host.get(f"/room/{code}/results").get_data(as_text=True)
    assert "Copy Share Link" not in results and "/results</span>" not in results


def test_item_categories_survive_create_and_edit(client):
    receipt_items = [
        {"name": "Paneer Tikka", "price": "320", "category": "veg"},
        {"name": "Chicken 65", "price": "380", "category": "Non-Veg"},
        {"name": "Old Monk", "price": "250", "category": "alcohol"},
        {"name": "Cover charge", "price": "100"},
    ]
    resp = client.post("/create", data={
        "host_name": "Alice", "room_name": "Dinner", "num_people": "2",
        "receipt_json": json.dumps({"items": receipt_items, "subtotal": "1050", "total": "1050"}),
    })
    assert resp.status_code == 302
    code = resp.headers["Location"].rsplit("/", 1)[-1]
    r = room(code)
    assert [i["category"] for i in r["items"]] == ["veg", "nonveg", "alcohol", "other"]

    page = client.get(f"/edit/{code}").get_data(as_text=True)
    for value in ("veg", "nonveg", "alcohol"):
        assert page.count(f'<option value="{value}" selected>') == 1

    # Host corrects one category and adds an egg dish; a bogus value falls back to "other".
    ids = [str(i["id"]) for i in r["items"]]
    resp = client.post(f"/edit/{code}", data={
        "item_id": ids + [""],
        "item_name": ["Paneer Tikka", "Chicken 65", "Old Monk", "Cover charge", "Egg Bhurji"],
        "item_price": ["320", "380", "250", "100", "150"],
        "item_category": ["veg", "nonveg", "alcohol", "hacked", "egg"],
        "subtotal": "1200", "total": "1200",
    })
    assert resp.status_code == 302
    assert [i["category"] for i in room(code)["items"]] == ["veg", "nonveg", "alcohol", "other", "egg"]


def _dosa_room():
    host = new_client()
    resp = host.post("/create", data={
        "host_name": "Alice", "room_name": "Breakfast", "num_people": "2",
        "receipt_json": json.dumps({
            "items": [{"name": "Masala Dosa", "price": "500", "quantity": 5},
                      {"name": "Coffee", "price": "60"}],
            "subtotal": "560", "total": "560",
        }),
    })
    assert resp.status_code == 302
    code = resp.headers["Location"].rsplit("/", 1)[-1]
    bob = new_client()
    assert join(bob, code, "Bob").status_code == 302
    return code, host, bob


def _submit_portions(client, code, name, portions):
    """portions: {item name: how many}"""
    ids = dict(zip(portions, item_ids(code, *portions)))
    data = {"selected_items": list(ids.values()), "submission_id": ""}
    data.update({f"portions_{ids[item]}": str(count) for item, count in portions.items()})
    return client.post(f"/room/{code}/user/{name}/select-items", data=data,
                       headers={"X-Requested-With": "fetch"})


def test_quantity_survives_create_and_shows_stepper():
    code, host, _bob = _dosa_room()
    assert [i["quantity"] for i in room(code)["items"]] == [5, 1]
    page = host.get(f"/room/{code}/user/Alice/select-items").get_data(as_text=True)
    assert page.count('class="portions"') == 1          # only the dish there are several of
    assert "<output>Shared</output>" in page            # a plain tap means sharing
    assert 'name="item_quantity" value="5"' in host.get(f"/edit/{code}").get_data(as_text=True)


def test_plain_tap_on_a_several_of_dish_means_shared():
    code, host, bob = _dosa_room()
    assert submit(host, code, "Alice", ["Masala Dosa"]).status_code == 200
    assert submit(bob, code, "Bob", ["Masala Dosa", "Coffee"]).status_code == 200
    r = room(code)
    assert r["portions"] == {"Alice": {"Masala Dosa": 0}, "Bob": {"Masala Dosa": 0, "Coffee": 1}}
    assert r["quantity_mismatches"] == []
    assert post_json(host, f"/room/{code}/finalize").status_code == 200
    page = host.get(f"/room/{code}/results").get_data(as_text=True)
    assert "all 5 shared 2 ways" in page


def test_claimed_units_and_sharers_end_to_end():
    code, host, bob = _dosa_room()
    # Alice asks for 9 of 5 dosas: capped at the five there are.
    assert _submit_portions(host, code, "Alice", {"Masala Dosa": 9, "Coffee": 1}).status_code == 200
    assert room(code)["portions"]["Alice"] == {"Masala Dosa": 5, "Coffee": 1}

    # Every dosa is Alice's, so Bob can neither claim one nor share.
    resp = _submit_portions(bob, code, "Bob", {"Masala Dosa": 1})
    assert resp.status_code == 409 and resp.get_json()["code"] == "PORTIONS_TAKEN"
    assert resp.get_json()["message"] == "No Masala Dosa left to claim: Alice has 5."
    resp = _submit_portions(bob, code, "Bob", {"Masala Dosa": 0})
    assert resp.status_code == 409
    assert resp.get_json()["message"] == (
        "All 5 Masala Dosa are claimed (Alice has 5), so there is nothing left to share."
    )
    assert "Bob" not in room(code)["submitted_users"]      # nothing was saved

    # Alice corrects herself to three; Bob shares the other two.
    assert post_json(host, f"/room/{code}/user/Alice/revoke").status_code == 200
    assert _submit_portions(host, code, "Alice", {"Masala Dosa": 3, "Coffee": 1}).status_code == 200
    assert _submit_portions(bob, code, "Bob", {"Masala Dosa": 0}).status_code == 200
    r = room(code)
    assert r["portions"] == {"Alice": {"Masala Dosa": 3, "Coffee": 1}, "Bob": {"Masala Dosa": 0}}
    assert r["quantity_mismatches"] == []

    status = host.get(f"/room/{code}/status").get_json()
    assert status["picks"] == {"Alice": ["Masala Dosa ×3", "Coffee"], "Bob": ["Masala Dosa (sharing)"]}

    assert post_json(host, f"/room/{code}/finalize").status_code == 200
    page = host.get(f"/room/{code}/results").get_data(as_text=True)
    assert "3 of 5, ₹100.00 each" in page and "2 of 5 shared 1 way" in page
    pdf = host.get(f"/room/{code}/download")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"


def test_unowned_units_warn_the_host_only():
    code, host, bob = _dosa_room()
    assert _submit_portions(host, code, "Alice", {"Masala Dosa": 1}).status_code == 200
    assert _submit_portions(bob, code, "Bob", {"Masala Dosa": 2}).status_code == 200
    status = host.get(f"/room/{code}/status").get_json()
    assert [(m["name"], m["claimed"], m["quantity"]) for m in status["quantity_mismatches"]] == [("Masala Dosa", 3, 5)]
    assert bob.get(f"/room/{code}/status").get_json()["quantity_mismatches"] == []


def test_lowering_quantity_resets_and_trims_claims():
    code, host, bob = _dosa_room()
    assert _submit_portions(bob, code, "Bob", {"Masala Dosa": 4}).status_code == 200
    r = room(code)
    resp = host.post(f"/edit/{code}", data={
        "item_id": [str(i["id"]) for i in r["items"]],
        "item_name": ["Masala Dosa", "Coffee"],
        "item_price": ["500", "60"],
        "item_quantity": ["2", "1"],
        "item_category": ["veg", "veg"],
        "subtotal": "560", "total": "560",
    })
    assert resp.status_code == 302
    r = room(code)
    assert r["items"][0]["quantity"] == 2
    assert r["submitted_users"] == set()                # Bob has to confirm again
    bob_row = next(p for p in r["participants"] if p["name"] == "Bob")
    assert list(bob_row["claim_portions"].values()) == [2]


def test_second_sender_sees_what_is_taken_and_takes_what_is_left():
    code, host, bob = _dosa_room()
    assert _submit_portions(host, code, "Alice", {"Masala Dosa": 4}).status_code == 200
    # Bob, still picking, can see what Alice sent; Alice's own view leaves her out.
    dosa = item_ids(code, "Masala Dosa")[0]
    assert bob.get(f"/room/{code}/status").get_json()["taken"] == {dosa: [{"name": "Alice", "portions": 4}]}
    assert host.get(f"/room/{code}/status").get_json()["taken"] == {dosa: []}

    resp = _submit_portions(bob, code, "Bob", {"Masala Dosa": 2, "Coffee": 1})
    assert resp.status_code == 409
    assert resp.get_json()["message"] == "Only 1 of the 5 Masala Dosa can still be claimed: Alice has 4."
    assert _submit_portions(bob, code, "Bob", {"Masala Dosa": 1, "Coffee": 1}).status_code == 200
    assert room(code)["quantity_mismatches"] == []

    # Alice changing her own mind is not blocked by her earlier claim, but Bob's one is.
    assert post_json(host, f"/room/{code}/user/Alice/revoke").status_code == 200
    assert _submit_portions(host, code, "Alice", {"Masala Dosa": 4}).status_code == 200
    assert post_json(host, f"/room/{code}/user/Alice/revoke").status_code == 200
    assert _submit_portions(host, code, "Alice", {"Masala Dosa": 5}).status_code == 409


def test_single_dishes_are_shared_never_blocked():
    code, host, bob = _dosa_room()
    assert submit(host, code, "Alice", ["Coffee"]).status_code == 200
    assert submit(bob, code, "Bob", ["Coffee"]).status_code == 200


def _naan_room(people, quantity=2, price="90"):
    """`quantity` naans; `people` guests join besides the host Alice."""
    host = new_client()
    resp = host.post("/create", data={
        "host_name": "Alice", "room_name": "Dinner", "num_people": str(len(people) + 1),
        "receipt_json": json.dumps({"items": [{"name": "Naan", "price": price, "quantity": quantity}],
                                    "subtotal": price, "total": price}),
    })
    code = resp.headers["Location"].rsplit("/", 1)[-1]
    clients = {"Alice": host}
    for name in people:
        clients[name] = new_client()
        assert join(clients[name], code, name).status_code == 302
    return code, clients


def _item_totals(code):
    from utils.bill_split import calculate_bill_split

    return {n: d["item_total"] for n, d in calculate_bill_split(room(code))["user_breakdown"].items()}


def test_two_dishes_shared_between_four_people():
    code, c = _naan_room(["Bob", "Cara", "Dev"])
    for name in c:
        assert _submit_portions(c[name], code, name, {"Naan": 0}).status_code == 200, name
    assert room(code)["quantity_mismatches"] == []
    assert _item_totals(code) == {"Alice": 22.5, "Bob": 22.5, "Cara": 22.5, "Dev": 22.5}


def test_one_claims_a_unit_and_any_number_share_the_rest():
    # Three dosas for 300: Alice had one to herself, the other two go round the table.
    code, c = _naan_room(["Bob", "Cara", "Dev", "Esha"], quantity=3, price="300")
    assert _submit_portions(c["Alice"], code, "Alice", {"Naan": 1}).status_code == 200
    for name in ("Bob", "Cara", "Dev", "Esha"):
        assert _submit_portions(c[name], code, name, {"Naan": 0}).status_code == 200, name
    assert _item_totals(code) == {"Alice": 100.0, "Bob": 50.0, "Cara": 50.0, "Dev": 50.0, "Esha": 50.0}


def test_a_unit_stays_back_while_someone_is_sharing():
    # Three dosas: Bob is sharing, so Alice and Cara cannot claim all three between them.
    code, c = _naan_room(["Bob", "Cara"], quantity=3, price="300")
    assert _submit_portions(c["Bob"], code, "Bob", {"Naan": 0}).status_code == 200
    assert _submit_portions(c["Alice"], code, "Alice", {"Naan": 2}).status_code == 200
    resp = _submit_portions(c["Cara"], code, "Cara", {"Naan": 1})
    assert resp.status_code == 409
    assert resp.get_json()["message"] == (
        "No Naan left to claim: Alice has 2, Bob is sharing the rest. You can share instead."
    )
    assert _submit_portions(c["Cara"], code, "Cara", {"Naan": 0}).status_code == 200
    assert _item_totals(code) == {"Alice": 200.0, "Bob": 50.0, "Cara": 50.0}


def test_units_can_never_exceed_the_bill():
    # shlok 1, Jsoh 2 of three; levi can neither claim a fourth nor share nothing.
    host = new_client()
    resp = host.post("/create", data={
        "host_name": "shlok", "room_name": "Breakfast", "num_people": "3",
        "receipt_json": json.dumps({"items": [{"name": "MASALA DOSA", "price": "300", "quantity": 3}],
                                    "subtotal": "300", "total": "300"}),
    })
    code = resp.headers["Location"].rsplit("/", 1)[-1]
    jsoh, levi = new_client(), new_client()
    assert join(jsoh, code, "Jsoh").status_code == 302 and join(levi, code, "levi").status_code == 302
    assert _submit_portions(host, code, "shlok", {"MASALA DOSA": 1}).status_code == 200
    assert _submit_portions(jsoh, code, "Jsoh", {"MASALA DOSA": 2}).status_code == 200
    resp = _submit_portions(levi, code, "levi", {"MASALA DOSA": 1})
    assert resp.status_code == 409
    assert resp.get_json()["message"] == "No MASALA DOSA left to claim: shlok has 1, Jsoh has 2."
    assert _submit_portions(levi, code, "levi", {"MASALA DOSA": 0}).status_code == 409
    assert room(code)["quantity_mismatches"] == []


def test_tax_split_switch_is_on_by_default_and_can_be_turned_off():
    code, host, bob = _dosa_room()
    page = host.get(f"/edit/{code}").get_data(as_text=True)
    assert 'name="tax_proportional" value="1" checked' in page
    assert submit(bob, code, "Bob", ["Coffee"]).status_code == 200

    r = room(code)
    form = {
        "item_id": [str(i["id"]) for i in r["items"]],
        "item_name": ["Masala Dosa", "Coffee"], "item_price": ["500", "60"],
        "item_quantity": ["5", "1"], "item_category": ["other", "other"],
        "subtotal": "560", "total": "560", "tax_split_set": "1",
    }
    assert host.post(f"/edit/{code}", data=form).status_code == 302          # switch off
    r = room(code)
    assert r["ocr_data"]["taxSplit"] == "equal"
    assert r["submitted_users"] == {"Bob"}               # nobody is sent back to re-pick
    assert 'name="tax_proportional" value="1">' in host.get(f"/edit/{code}").get_data(as_text=True)

    assert host.post(f"/edit/{code}", data={**form, "tax_proportional": "1"}).status_code == 302
    assert room(code)["ocr_data"]["taxSplit"] == "proportional"
    # A form without the switch at all (an old cached page) leaves the choice alone.
    form.pop("tax_split_set")
    assert host.post(f"/edit/{code}", data=form).status_code == 302
    assert room(code)["ocr_data"]["taxSplit"] == "proportional"


def test_reopened_picks_stay_on_hold_for_their_owner():
    # Three dosas: Alice has 2, Bob has 1. Bob reopens his picks to change something else;
    # Cara, who joined late, must not be able to take or share Bob's dosa meanwhile.
    code, c = _naan_room(["Bob", "Cara"], quantity=3, price="300")
    assert _submit_portions(c["Alice"], code, "Alice", {"Naan": 2}).status_code == 200
    assert _submit_portions(c["Bob"], code, "Bob", {"Naan": 1}).status_code == 200
    assert post_json(c["Bob"], f"/room/{code}/user/Bob/revoke").status_code == 200

    naan = item_ids(code, "Naan")[0]
    seen = c["Cara"].get(f"/room/{code}/status").get_json()["taken"][naan]
    assert seen == [{"name": "Alice", "portions": 2}, {"name": "Bob", "portions": 1}]
    assert _submit_portions(c["Cara"], code, "Cara", {"Naan": 1}).status_code == 409
    resp = _submit_portions(c["Cara"], code, "Cara", {"Naan": 0})
    assert resp.status_code == 409
    assert "Alice has 2, Bob has 1" in resp.get_json()["message"]

    # Bob can send his own again, and can also give it up, which frees it for Cara.
    assert _submit_portions(c["Bob"], code, "Bob", {"Naan": 1}).status_code == 200
    assert post_json(c["Bob"], f"/room/{code}/user/Bob/revoke").status_code == 200
    assert submit(c["Bob"], code, "Bob", []).status_code == 200
    assert _submit_portions(c["Cara"], code, "Cara", {"Naan": 1}).status_code == 200


def test_help_pages_link_through_in_order(client):
    import app as app_module

    index = client.get("/help")
    assert index.status_code == 200
    slugs = [t["slug"] for t in app_module.HELP_TOPICS]
    for position, slug in enumerate(slugs):
        page = client.get(f"/help/{slug}")
        assert page.status_code == 200, slug
        text = page.get_data(as_text=True)
        assert f'href="/help/{slug}"' in index.get_data(as_text=True)
        if position + 1 < len(slugs):
            assert f'class="nextup" href="/help/{slugs[position + 1]}"' in text
        else:
            assert 'class="nextup" href="/create?mode=image"' in text
    assert client.get("/help/nope").status_code == 302
    about = client.get("/about")
    assert about.status_code == 301 and about.headers["Location"].endswith("/help")
    assert 'href="/help"' in client.get("/").get_data(as_text=True)


# --- currencies and charge rows ----------------------------------------------

def _edit(host, code, currency="INR", prices=("200", "400", "100"), charges=(), **extra):
    r = room(code)
    data = {
        "item_id": [str(i["id"]) for i in r["items"]],
        "item_name": [i["raw_name"] for i in r["items"]],
        "item_price": list(prices),
        "currency": currency,
        "charge_label": [c[0] for c in charges],
        "charge_amount": [c[1] for c in charges],
        "charge_kind": [c[2] for c in charges],
        "tax_split_set": "1", "tax_proportional": "1",
    }
    data.update(extra)
    return host.post(f"/edit/{code}", data=data)


def test_manual_room_starts_in_rupees_with_starter_rows(client):
    resp = client.post("/create", data={
        "host_name": "Alice", "room_name": "Lunch", "num_people": "2", "skip_ocr": "true",
    })
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert 'value="INR" selected' in page
    for label in ("CGST", "SGST", "Service charge"):
        assert f'data-starter="{label}"' in page
    assert "Only changes the symbol. Amounts aren&#39;t converted." in page or "Amounts aren't converted" in page


def test_created_from_a_confirmed_yen_scan(client):
    code = make_room(client, items=[("Ramen", "1200"), ("Gyoza", "600"), ("Beer", "800")], extra={
        "receipt_json": json.dumps({
            "currency": {"code": "JPY", "confirmed": True},
            "items": [{"name": "Ramen", "price": 1200}, {"name": "Gyoza", "price": 600}, {"name": "Beer", "price": 800}],
            "charges": [{"label": "消費税", "amount": 234, "kind": "charge"}],
            "total": 2834,
        }),
    })
    r = room(code)
    assert r["currency"]["code"] == "JPY"
    assert [i["price"] for i in r["items"]] == ["¥1,200", "¥600", "¥800"]
    assert r["ocr_data"]["charges"] == [{"label": "消費税", "amount": 234.0, "kind": "charge"}]
    page = client.get(f"/edit/{code}").get_data(as_text=True)
    assert 'value="JPY" selected' in page and 'value="消費税"' in page and "¥1,200" in page


def test_edit_rows_and_currency_then_results_and_pdf():
    code, host, bob, cara = _full_room()
    resp = _edit(host, code, "JPY", prices=("1,200", "600", "800"), charges=[
        ("消費税", "234", "charge"), ("Lunch set", "260", "discount"),
        ("CGST", "", "charge"),                       # empty starter row: not saved
    ], subtotal="2600", total="¥2,574")
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    ocr = room(code)["ocr_data"]
    assert ocr["currency"] == "JPY" and ocr["total"] == 2574.0
    assert ocr["charges"] == [
        {"label": "消費税", "amount": 234.0, "kind": "charge"},
        {"label": "Lunch set", "amount": 260.0, "kind": "discount"},
    ]

    submit(host, code, "Alice", ["Pasta"])
    submit(bob, code, "Bob", ["Pizza"])
    submit(cara, code, "Cara", ["Coke"])
    assert post_json(host, f"/room/{code}/finalize").status_code == 200

    page = bob.get(f"/room/{code}/results").get_data(as_text=True)
    assert "All square." in page and "¥2,574" in page
    assert "¥1,188" in page                          # Alice: 1200 + 108 tax − 120 off
    assert "消費税" in page and "Lunch set" in page and "₹" not in page
    assert "Japanese Yen (JPY)" in page

    pdf = bob.get(f"/room/{code}/download")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"


def test_edit_refuses_decimals_the_currency_cannot_hold(client):
    code = make_room(client)
    resp = _edit(client, code, "JPY", prices=("200", "400.50", "100"))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert "Japanese Yen amounts are whole numbers" in page
    assert 'value="JPY" selected' in page and "400.50" in page          # what was typed stays
    resp = _edit(client, code, "JPY", charges=[("Tip", "50.5", "charge")])
    assert resp.status_code == 400 and "Tip: Japanese Yen amounts are whole numbers" in resp.get_data(as_text=True)
    assert room(code)["currency"]["code"] == "INR"                      # nothing was saved


def test_changing_only_the_currency_asks_people_to_reconfirm():
    code, host, bob, cara = _full_room()
    submit(bob, code, "Bob", ["Pasta"])
    assert _edit(host, code, "INR", subtotal="700", total="700").status_code == 302
    assert "Bob" in room(code)["submitted_users"]                       # same bill: picks stay
    assert _edit(host, code, "USD", subtotal="700", total="700").status_code == 302
    assert "Bob" not in room(code)["submitted_users"]                   # amounts mean something else now


def test_help_page_for_currencies(client):
    page = client.get("/help/currency").get_data(as_text=True)
    assert "I never convert it" in page and "¥2,574" in page and 'role="radiogroup"' in page
    assert "Other currencies" in client.get("/help").get_data(as_text=True)


def test_pdf_prints_hindi_tamil_and_japanese_names():
    host = new_client()
    code = make_room(host, host="राहुल", num_people=2, extra={"receipt_json": json.dumps({
        "currency": {"code": "INR", "confirmed": True},
        "items": [{"name": "पनीर टिक्का", "price": 330}, {"name": "மசாலா தோசை", "price": 120},
                  {"name": "醤油ラーメン", "price": 500}],
        "charges": [{"label": "सेवा शुल्क", "amount": 50}],
    })})
    guest = new_client()
    join(guest, code, "Meera")
    submit(host, code, "राहुल", ["पनीर टिक्का", "醤油ラーメン"])
    submit(guest, code, "Meera", ["மசாலா தோசை"])
    assert post_json(host, f"/room/{code}/finalize").status_code == 200
    pdf = host.get(f"/room/{code}/download")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    for font in (b"NotoSansDevanagari", b"NotoSansTamil", b"DroidSansFallback"):
        assert font in pdf.data, font


def test_english_pdf_embeds_no_extra_fonts():
    code, host, bob, cara = _full_room()
    for client, name in ((host, "Alice"), (bob, "Bob"), (cara, "Cara")):
        submit(client, code, name, ["Pasta"])
    post_json(host, f"/room/{code}/finalize")
    pdf = host.get(f"/room/{code}/download").data
    assert b"NotoSans" not in pdf and b"DroidSans" not in pdf


# --- ways back ------------------------------------------------------------------

def test_fixing_only_the_bill_total_keeps_everyones_picks():
    code, host, bob, cara = _full_room()
    submit(bob, code, "Bob", ["Pasta"])
    assert _edit(host, code, "INR", subtotal="700", total="700").status_code == 302
    assert _edit(host, code, "INR", subtotal="650", total="742").status_code == 302   # printed figures only
    assert "Bob" in room(code)["submitted_users"]
    assert _edit(host, code, "INR", charges=[("Tip", "50", "charge")], total="742").status_code == 302
    assert "Bob" not in room(code)["submitted_users"]                                 # a charge changes shares


def test_fix_the_bill_reopens_the_split_without_resetting_picks():
    code, host, bob, cara = _full_room()
    for client, name in ((host, "Alice"), (bob, "Bob"), (cara, "Cara")):
        submit(client, code, name, ["Pasta"])
    assert post_json(host, f"/room/{code}/finalize").status_code == 200
    page = host.get(f"/room/{code}/results").get_data(as_text=True)
    assert "Fix the bill" in page and "Ask Alice to reopen" not in page
    assert "Picked wrong? Ask Alice to reopen the picks." in bob.get(f"/room/{code}/results").get_data(as_text=True)

    assert post_json(bob, f"/room/{code}/host/fix-bill").status_code == 403           # host only
    resp = post_json(host, f"/room/{code}/host/fix-bill")
    assert resp.status_code == 200 and resp.get_json()["redirect"] == f"/edit/{code}"
    r = room(code)
    assert not r["finalized"] and r["submitted_users"] == {"Alice", "Bob", "Cara"}
    assert host.get(f"/edit/{code}").status_code == 200                               # editable again
    # Fix the total only, then split again straight away.
    assert _edit(host, code, "INR", total="700").status_code == 302
    assert post_json(host, f"/room/{code}/finalize").status_code == 200


def test_refused_create_keeps_the_scan(client):
    receipt = json.dumps({"currency": {"code": "INR", "confirmed": True}, "items": [{"name": "Dal", "price": 200}]})
    resp = client.post("/create", data={"host_name": "Asha 🙂", "room_name": "Dinner", "num_people": "2",
                                        "receipt_json": receipt})
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert "Names can only contain" in page and "&#34;Dal&#34;" in page                # the scan comes back
    resp = client.post("/create", data={"host_name": "Asha 🙂", "room_name": "Dinner", "num_people": "2",
                                        "skip_ocr": "true", "receipt_json": receipt})
    assert 'name="receipt_json" value=""' in resp.get_data(as_text=True)              # manual: nothing to keep


def test_check_the_bill_has_a_way_back_to_the_room(client):
    code = make_room(client)
    page = client.get(f"/edit/{code}").get_data(as_text=True)
    assert f'class="backlink" href="/room/{code}"' in page and "data-live" in page
