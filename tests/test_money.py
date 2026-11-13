"""Currencies, charge rows, exact rounding and the currency question (no database needed)."""
from decimal import Decimal

import pytest

from utils.bill_audit import audit_bill
from utils.bill_split import calculate_bill_split
from utils.bill_totals import bill_signature, clean_charges, read_bill
from utils.money import TooManyDecimals, allocate, format_money, get_currency, parse_amount, to_minor
from utils.receipt_contracts import normalize_receipt_payload

INR, JPY, CHF = get_currency("INR"), get_currency("JPY"), get_currency("CHF")


# --- reading and writing amounts ---------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("₹1,234.50", "1234.50"), ("1,23,456.50", "123456.50"), ("Rs. 500/-", "500"),
    ("¥1,200", "1200"), ("1200円", "1200"), ("CHF 12.50", "12.50"),
    ("12,50", "12.50"), ("1.234,50", "1234.50"), ("1 234,50", "1234.50"), ("1'234.50", "1234.50"),
    ("1,234", "1234"), ("-50", "-50"), ("₹-50", "-50"), (12.5, "12.5"),
])
def test_parse_amount_reads_what_people_and_bills_write(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", "N/A", None])
def test_parse_amount_missing(raw):
    assert parse_amount(raw) is None


@pytest.mark.parametrize("raw", ["abc", "12.3.4", "1e5", "NaN", "99999999"])
def test_parse_amount_rejects(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_formatting_follows_the_currency():
    assert format_money(2574, JPY) == "¥2,574"
    assert format_money(1234567.5, INR) == "₹12,34,567.50"      # Indian grouping
    assert format_money(12.5, CHF) == "CHF 12.50"
    assert format_money(-70, INR) == "-₹70.00"
    # The PDF's built-in fonts have no ₹ or ¥.
    assert format_money(1234.5, INR, pdf=True) == "Rs. 1,234.50"
    assert format_money(2574, JPY, pdf=True) == "JPY 2,574"


def test_yen_has_no_decimals_when_typed_by_the_host():
    assert to_minor(Decimal("500.00"), JPY, strict=True) == 500
    with pytest.raises(TooManyDecimals, match="whole numbers"):
        to_minor(Decimal("500.50"), JPY, strict=True)
    assert to_minor(Decimal("500.50"), JPY) == 501               # a model's reading is rounded


def test_unknown_currency_falls_back_to_rupees():
    assert get_currency("XYZ").code == get_currency(None).code == "INR"


# --- exact rounding -----------------------------------------------------------

def test_allocate_hands_leftover_units_to_the_biggest_rounding_loss():
    assert allocate([333.33, 333.33, 333.33], 1000) == [334, 333, 333]   # tie: the first one
    assert allocate([3333.33, 3333.33, 3333.34], 10000) == [3333, 3333, 3334]
    assert allocate([0.0, 50.5, 49.5], 100) == [0, 51, 49]
    assert allocate([], 0) == []


def _room(items, picks, ocr):
    return {
        "items": [{"name": n, "price": str(p), "quantity": 1} for n, p in items],
        "selections": picks,
        "ocr_data": ocr,
    }


def test_yen_shares_are_whole_and_add_up_to_the_bill():
    room = _room([("Sukiyaki", 1000)], {"Asha": ["Sukiyaki"], "Ben": ["Sukiyaki"], "Chen": ["Sukiyaki"]},
                 {"currency": "JPY", "charges": []})
    people = calculate_bill_split(room)["user_breakdown"]
    assert [people[u]["final_amount"] for u in ("Asha", "Ben", "Chen")] == [334, 333, 333]
    assert people["Asha"]["item_details"][0]["your_share"] == 334


def test_rupee_shares_add_up_to_the_paisa():
    room = _room([("Thali", 100)], {"A": ["Thali"], "B": ["Thali"], "C": ["Thali"]}, {})
    split = calculate_bill_split(room)
    finals = [split["user_breakdown"][u]["final_amount"] for u in "ABC"]
    assert finals == [33.34, 33.33, 33.33]
    assert split["totals"]["grand_total"] == 100.0


def test_tokyo_dinner_with_tax_and_discount_rows():
    """The example from the plan: ¥2,600 of food, 10% tax, ¥260 off, ¥2,574 total."""
    room = _room(
        [("Ramen", 1200), ("Gyoza", 600), ("Beer", 800)],
        {"Asha": ["Ramen"], "Ben": ["Gyoza"], "Chen": ["Beer"]},
        {"currency": "JPY", "subtotal": 2600, "total": 2574, "charges": [
            {"label": "消費税", "amount": 234, "kind": "charge"},
            {"label": "Discount", "amount": 260, "kind": "discount"},
        ]},
    )
    split = calculate_bill_split(room)
    people = split["user_breakdown"]
    assert split["totals"]["grand_total"] == 2574
    for d in people.values():
        assert d["final_amount"] == int(d["final_amount"])           # whole yen
        lines = [i["your_share"] for i in d["item_details"]]
        lines += [c["amount"] * (-1 if c["kind"] == "discount" else 1) for c in d["charges"]]
        assert sum(lines) == d["final_amount"]                         # each stub adds up
    assert people["Asha"]["final_amount"] == 1188                      # 1200 + 108 tax − 120 off
    assert [c["label"] for c in people["Asha"]["charges"]] == ["消費税", "Discount"]
    titles = [w["title"] for w in audit_bill(room, split)]
    assert "Split total doesn't match bill total" not in titles
    assert any("¥" in w["detail"] or w["severity"] == "ok" for w in audit_bill(room, split))


def test_round_off_tolerance_is_the_currencys_cash_step():
    room = _room([("Rösti", "30.00")], {"A": ["Rösti"]}, {"currency": "CHF", "total": "30.05"})
    titles = [w["title"] for w in audit_bill(room, calculate_bill_split(room))]
    assert "Split total doesn't match bill total" not in titles          # 5 Rappen: the bill's rounding
    room["ocr_data"]["total"] = "30.10"
    titles = [w["title"] for w in audit_bill(room, calculate_bill_split(room))]
    assert "Split total doesn't match bill total" in titles


# --- charge rows --------------------------------------------------------------

def test_clean_charges_drops_empty_rows_and_reads_signs_and_percent():
    rows = [
        {"label": "CGST", "amount": "", "kind": "charge"},            # an untouched starter row
        {"label": "GST", "amount": "5%", "kind": "charge"},
        {"label": "", "amount": "-70", "kind": "charge"},             # negative = a discount
        {"label": "Tip", "amount": "0", "kind": "charge"},
    ]
    assert clean_charges(rows, INR, strict=True, percent_base=Decimal("700")) == [
        {"label": "GST", "amount": Decimal("35.00"), "kind": "charge"},
        {"label": "Discount", "amount": Decimal("70.00"), "kind": "discount"},
    ]


def test_clean_charges_strict_errors_name_the_row():
    with pytest.raises(ValueError, match="Tip: Japanese Yen amounts are whole numbers"):
        clean_charges([{"label": "Tip", "amount": "500.50", "kind": "charge"}], JPY, strict=True)
    with pytest.raises(ValueError, match="at most 12"):
        clean_charges([{"label": f"R{i}", "amount": "1"} for i in range(13)], INR, strict=True)
    # A model's rows are cleaned, never refused.
    assert len(clean_charges([{"label": f"R{i}", "amount": "1"} for i in range(13)], INR)) == 12


def test_old_fixed_fields_read_as_rows():
    legacy = {"serviceCharge": "₹40.00", "cgst": "₹20.00", "sgst": "₹20.00", "igst": "N/A",
              "discount": "₹10.00", "subtotal": "₹400.00", "total": "₹470.00"}
    bill = read_bill(legacy)
    assert bill["currency"].code == "INR"
    assert [(c["label"], c["amount"], c["kind"]) for c in bill["charges"]] == [
        ("Service charge", 40.0, "charge"), ("CGST", 20.0, "charge"), ("SGST", 20.0, "charge"),
        ("Discount", 10.0, "discount"),
    ]
    # The same bill saved in the new shape counts as unchanged (nobody reconfirms).
    new = {"currency": "INR", "subtotal": 400.0, "total": 470.0, "charges": [
        {"label": "Service charge", "amount": 40.0, "kind": "charge"},
        {"label": "CGST", "amount": 20.0, "kind": "charge"},
        {"label": "SGST", "amount": 20.0, "kind": "charge"},
        {"label": "Discount", "amount": 10.0, "kind": "discount"},
    ]}
    assert bill_signature(legacy) == bill_signature(new)


# --- the currency question ------------------------------------------------------

def _currency(payload, trust=False):
    return normalize_receipt_payload(payload, trust_confirmation=trust)["currency"]


def test_gst_lines_settle_rupees_without_asking():
    c = _currency({"items": [{"name": "Dal", "price": 200}],
                   "charges": [{"label": "CGST @2.5%", "amount": 5}, {"label": "SGST @2.5%", "amount": 5}]})
    assert c == {"code": "INR", "confirmed": True, "reason": "", "alternatives": []}


@pytest.mark.parametrize("evidence,confirmed", [
    (["symbol"], True), (["address"], True), (["tax_labels"], True),
    (["language"], False),          # Hindi is also written in Nepal; English everywhere
    ([], False),
])
def test_only_printed_facts_settle_rupees(evidence, confirmed):
    assert _currency({"currency": {"code": "INR", "evidence": evidence}})["confirmed"] is confirmed


def test_any_other_currency_needs_a_tap_and_keeps_the_models_guesses():
    c = _currency({"currency": {"code": "jpy", "evidence": ["symbol", "language"],
                                "reason": "¥ symbol, Japanese text", "alternatives": ["CNY", "JPY", "XXX", "INR"]}})
    assert c == {"code": "JPY", "confirmed": False, "reason": "¥ symbol, Japanese text", "alternatives": ["CNY", "INR"]}


def test_host_confirmation_and_old_saved_scans_are_trusted():
    assert _currency({"currency": {"code": "JPY", "confirmed": True}}, trust=True)["confirmed"] is True
    assert _currency({"currency": {"code": "JPY", "confirmed": True}})["confirmed"] is False
    # A saved scan from before currencies existed is a rupee bill.
    old = normalize_receipt_payload({"items": [{"name": "Tea", "price": "₹30.00"}], "cgst": "₹1.50",
                                     "total": "₹31.50"}, trust_confirmation=True)
    assert old["currency"]["code"] == "INR" and old["currency"]["confirmed"]
    assert old["charges"] == [{"label": "CGST", "amount": 1.5, "kind": "charge"}]
    assert old["items"][0]["price"] == 30.0 and old["total"] == 31.5


def test_receipt_amounts_follow_the_receipts_currency():
    r = normalize_receipt_payload({"currency": {"code": "JPY"}, "items": [{"name": "Ramen", "price": "1,200"}],
                                   "charges": [{"label": "Tax", "amount": 120.4}], "total": "1320"})
    assert r["items"][0]["price"] == 1200.0
    assert r["charges"] == [{"label": "Tax", "amount": 120.0, "kind": "charge"}]


def _total_warning(room):
    found = [w for w in audit_bill(room, calculate_bill_split(room)) if w["title"] == "Split total doesn't match bill total"]
    return found[0]["detail"] if found else None


def test_total_warning_names_charges_the_bill_total_leaves_out():
    """The host added tax, packing and a tip by hand; the bill total is still the dishes alone."""
    room = _room([("Ramen", 7535)], {"A": ["Ramen"]}, {"currency": "JPY", "total": 7535, "charges": [
        {"label": "Tax", "amount": 25, "kind": "charge"}, {"label": "Tax", "amount": 25, "kind": "charge"},
        {"label": "Packing", "amount": 400, "kind": "charge"}, {"label": "Tip", "amount": 500, "kind": "charge"},
    ]})
    detail = _total_warning(room)
    assert "¥8,485, but the bill total is ¥7,535: ¥950 more than the bill" in detail
    assert "exactly the taxes and charges (¥950)" in detail and "added them by hand" in detail


def test_total_warning_when_charges_explain_only_part_of_the_gap():
    room = _room([("Ramen", 1000)], {"A": ["Ramen"]}, {"currency": "JPY", "total": 900, "charges": [
        {"label": "Tip", "amount": 300, "kind": "charge"}]})
    assert "includes ¥300 of taxes and charges" in _total_warning(room)


def test_total_warning_when_the_split_is_short_of_the_bill():
    room = _room([("Ramen", 1000), ("Beer", 500)], {"A": ["Ramen"]}, {"currency": "JPY", "total": 1500})
    detail = _total_warning(room)
    assert "¥500 less than the bill" in detail and "weren't picked" in detail


def test_printed_subtotal_and_total_dont_count_as_a_change():
    a = {"currency": "INR", "subtotal": 700, "total": 700, "charges": [{"label": "Tip", "amount": 50}]}
    b = dict(a, subtotal=650, total=742)
    assert bill_signature(a) == bill_signature(b)
    assert bill_signature(a) != bill_signature(dict(a, charges=[{"label": "Tip", "amount": 60}]))
    assert bill_signature(a) != bill_signature(dict(a, currency="USD"))
