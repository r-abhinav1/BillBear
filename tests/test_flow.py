"""End-to-end room flow over HTTP against a real Postgres."""
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
