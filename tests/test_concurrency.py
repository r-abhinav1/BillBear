"""Races that the old read-modify-write blob lost. One test client per thread."""
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import item_ids, join, make_room, needs_db, new_client, post_json, room, submit

pytestmark = needs_db

N = 8


def test_simultaneous_submits_all_persist():
    host = new_client()
    code = make_room(host, num_people=N)
    guests = [(new_client(), f"Guest{i}") for i in range(N - 1)]
    for c, name in guests:
        assert join(c, code, name).status_code == 302
    everyone = [(host, "Alice")] + guests

    with ThreadPoolExecutor(max_workers=N) as pool:
        results = list(pool.map(lambda cn: submit(cn[0], code, cn[1], ["Pasta", "Coke"]).status_code, everyone))
    assert results == [200] * N
    r = room(code)
    assert r["submitted_users"] == {name for _, name in everyone}
    assert all(r["selections"][name] == ["Pasta", "Coke"] for _, name in everyone)


def test_joins_never_exceed_capacity():
    host = new_client()
    code = make_room(host, num_people=4)
    clients = [(new_client(), f"P{i}") for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(lambda cn: join(cn[0], code, cn[1]).status_code, clients))
    assert statuses.count(302) == 3 and statuses.count(409) == 3
    assert len(room(code)["users"]) == 4


def test_same_name_concurrent_join_has_one_winner():
    host = new_client()
    code = make_room(host, num_people=10)
    clients = [new_client() for _ in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(lambda c: join(c, code, "Sam").status_code, clients))
    assert statuses.count(302) == 1
    assert room(code)["users"].count("Sam") == 1


def test_double_submit_from_one_user_is_consistent():
    host = new_client()
    code = make_room(host, num_people=1)
    picks = [["Pasta"], ["Pizza"], ["Coke"], ["Pasta", "Pizza"]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda p: submit(host, code, "Alice", p).status_code, picks))
    r = room(code)
    assert r["selections"]["Alice"] in picks
    assert len(r["claim_ids"]["Alice"]) == len(r["selections"]["Alice"])


def test_finalize_racing_revoke_has_one_outcome():
    for _ in range(5):
        host = new_client()
        code = make_room(host, num_people=2)
        bob = new_client()
        join(bob, code, "Bob")
        submit(host, code, "Alice", ["Pasta"])
        submit(bob, code, "Bob", ["Pizza"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            fin = pool.submit(lambda: post_json(host, f"/room/{code}/finalize").status_code)
            rev = pool.submit(lambda: post_json(bob, f"/room/{code}/user/Bob/revoke").status_code)
            fin_status, rev_status = fin.result(), rev.result()

        r = room(code)
        if fin_status == 200:
            assert rev_status == 409 and r["finalized"] and "Bob" in r["submitted_users"]
        else:
            assert fin_status == 409 and rev_status == 200
            assert not r["finalized"] and "Bob" not in r["submitted_users"]


def test_item_edit_racing_submit_keeps_valid_claims():
    host = new_client()
    code = make_room(host, num_people=2)
    bob = new_client()
    join(bob, code, "Bob")
    ids = [str(i["id"]) for i in room(code)["items"]]

    def edit():
        return host.post(f"/edit/{code}", data={
            "item_id": ids[:2], "item_name": ["Pasta", "Pizza"], "item_price": ["200", "400"], "total": "600",
        }).status_code

    def pick():
        return submit(bob, code, "Bob", ["Pasta", "Coke"]).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        e, s = pool.submit(edit), pool.submit(pick)
        assert e.result() == 302
        assert s.result() in (200, 409)
    r = room(code)
    valid = {i["id"] for i in r["items"]}
    assert set(r["claim_ids"]["Bob"]) <= valid
    assert item_ids(code, "Pasta")  # Pasta kept its id
