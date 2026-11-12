"""Pure-function tests (no database needed)."""
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from utils import receipt_llm
from utils.bill_audit import audit_bill
from utils.bill_split import calculate_bill_split
from utils.image_prep import ImageRejected, prepare_receipt_image
from utils.receipt_pipeline import VisionFailed, VisionUnavailable, extract_receipt_from_vision
from utils.room_assembler import assemble_room, unique_display_names
from utils.validation import clean_items, normalize_person_name, parse_price

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("₹1,234.50", Decimal("1234.50")), ("450", Decimal("450.00")), (12.5, Decimal("12.50")),
    ("-50", Decimal("-50.00")), ("N/A", None), ("", None), (None, None),
])
def test_parse_price_ok(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", ["45.5.0", "abc", "NaN", "Infinity", "1e5", "99999999"])
def test_parse_price_rejects(raw):
    with pytest.raises(ValueError):
        parse_price(raw)


def test_clean_items_lenient_vs_strict():
    rows = [{"name": "  Tea  ", "price": "x"}, {"name": "", "price": "1"}, {"name": "Dal", "price": "₹10"}]
    assert clean_items(rows) == [
        {"id": None, "name": "Tea", "price": None, "category": "other", "quantity": 1},
        {"id": None, "name": "Dal", "price": Decimal("10.00"), "category": "other", "quantity": 1},
    ]
    with pytest.raises(ValueError):
        clean_items(rows, strict_prices=True)


def test_clean_items_caps_count():
    with pytest.raises(ValueError):
        clean_items([{"name": f"i{n}", "price": "1"} for n in range(201)])


def test_person_name_normalization():
    assert normalize_person_name("  Mary   Jane ") == "Mary Jane"
    assert normalize_person_name("Ｂｏｂ") == "Bob"  # NFKC fullwidth
    assert normalize_person_name("राहुल") == "राहुल"
    for bad in ["", "   ", "<b>", "a/b", "x" * 31, "--", "Pаul"]:
        with pytest.raises(ValueError):
            normalize_person_name(bad)


# --- assembler --------------------------------------------------------------

def test_unique_display_names():
    assert unique_display_names(["A", "A", "A (2)", "B"]) == ["A", "A (3)", "A (2)", "B"]


def _rows():
    room = {"code": "ABCDEF", "room_name": "R", "num_people": 3, "ocr_data": {"total": "₹300.00"},
            "ocr_source": "vision_llm", "finalized_at": None, "created_at": T0, "last_activity_at": T0}
    people = [
        {"id": 2, "name": "Bob", "is_host": False, "joined_at": T0 + timedelta(seconds=5),
         "submitted_at": T0, "submission_id": "s", "user_id_hash": "h2"},
        {"id": 1, "name": "Alice", "is_host": True, "joined_at": T0,
         "submitted_at": None, "submission_id": None, "user_id_hash": "h1"},
    ]
    items = [
        {"id": 11, "position": 1, "name": "Naan", "price": Decimal("50")},
        {"id": 10, "position": 0, "name": "Naan", "price": Decimal("250")},
    ]
    claims = [{"participant_id": 2, "item_id": 11}, {"participant_id": 1, "item_id": 10},
              {"participant_id": 2, "item_id": 999}]
    return room, people, items, claims


def test_assemble_room_shape():
    r = assemble_room(*_rows())
    assert r["users"] == ["Alice", "Bob"] and r["host_name"] == "Alice"
    assert [i["name"] for i in r["items"]] == ["Naan", "Naan (2)"]
    assert r["submitted_users"] == {"Bob"}
    assert r["selections"] == {"Bob": ["Naan (2)"]}     # only submitted users, unknown item dropped
    assert r["claim_ids"]["Alice"] == [10]              # draft kept
    assert r["still_editing"] == ["Alice"] and r["waiting_for"] == 1
    assert r["can_finalize"] is False
    assert r["unclaimed_items"] == ["Naan"]             # Alice's draft doesn't count
    split = calculate_bill_split(r)
    assert split["user_breakdown"]["Bob"]["item_total"] == 50.0
    assert audit_bill(r, split)


# --- vision pipeline --------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_pipeline_second_key_only_on_retryable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = []

    def fake(img, mime, key, timeout):
        calls.append(key)
        if len(calls) == 1:
            raise receipt_llm.ProviderCallError("429", retryable=True)
        return {"items": [{"name": "Tea", "price": "₹10"}]}

    monkeypatch.setattr(receipt_llm, "call_gemini_vision", fake)
    result = extract_receipt_from_vision("b64", budget_seconds=20, clock=FakeClock())
    assert len(calls) == 2 and result["items"][0]["name"] == "Tea"


def test_pipeline_no_second_key_on_hard_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = []

    def fake(img, mime, key, timeout):
        calls.append(key)
        raise receipt_llm.ProviderCallError("400 bad", retryable=False)

    monkeypatch.setattr(receipt_llm, "call_gemini_vision", fake)
    with pytest.raises(VisionFailed):
        extract_receipt_from_vision("b64", budget_seconds=20, clock=FakeClock())
    assert len(calls) == 1


def test_pipeline_respects_deadline(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    clock = FakeClock()
    timeouts = []

    def slow_gemini(img, mime, key, timeout):
        timeouts.append(timeout)
        clock.t += 7.0  # burns most of the budget, then times out
        raise receipt_llm.ProviderCallError("timeout", retryable=True)

    def openrouter(img, mime, timeout):
        raise AssertionError("should be out of time")

    monkeypatch.setattr(receipt_llm, "call_gemini_vision", slow_gemini)
    monkeypatch.setattr(receipt_llm, "call_openrouter_vision", openrouter)
    with pytest.raises(VisionFailed):
        extract_receipt_from_vision("b64", budget_seconds=9, clock=clock)
    assert len(timeouts) == 1 and timeouts[0] <= 8.5


def test_pipeline_unconfigured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(VisionUnavailable):
        extract_receipt_from_vision("b64")


def test_gemini_key_sent_in_header_not_url(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"items": []}'}]}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        return Resp()

    monkeypatch.setattr(receipt_llm.requests, "post", fake_post)
    receipt_llm.call_gemini_vision("b64", "image/jpeg", "SECRET-KEY", timeout=5)
    assert "SECRET-KEY" not in seen["url"] and seen["headers"]["x-goog-api-key"] == "SECRET-KEY"


# --- image prep -------------------------------------------------------------

def _img(fmt="JPEG", size=(100, 100), mode="RGB"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, size, "white").save(buf, format=fmt)
    return buf.getvalue()


def test_image_prep_downscales_width_keeps_tall():
    import base64

    from PIL import Image

    b64, mime = prepare_receipt_image(_img(size=(3200, 6000)))
    out = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert mime == "image/jpeg" and out.width <= 1600 and out.height <= 4000
    small_b64, _ = prepare_receipt_image(_img(size=(300, 900)))
    assert Image.open(io.BytesIO(base64.b64decode(small_b64))).size == (300, 900)  # never upscales


def test_image_prep_accepts_png_rgba():
    _b64, mime = prepare_receipt_image(_img("PNG", mode="RGBA"))
    assert mime == "image/jpeg"


def test_image_prep_rejects_garbage_and_gif():
    with pytest.raises(ImageRejected):
        prepare_receipt_image(b"not an image at all")
    with pytest.raises(ImageRejected):
        prepare_receipt_image(_img("GIF", mode="P"))
    with pytest.raises(ImageRejected):
        prepare_receipt_image(b"")


def test_image_prep_heic_passthrough():
    fake_heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100
    _b64, mime = prepare_receipt_image(fake_heic)
    assert mime in ("image/heic", "image/jpeg")


def test_gemini_request_uses_31_flash_lite_defaults(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "candidates": [{"content": {"parts": [
                    {"text": "thinking...", "thought": True},
                    {"text": '{"items": [{"name": "Tea", "price": "₹10"}]}'},
                ]}}],
                "usageMetadata": {"promptTokenCount": 1500, "candidatesTokenCount": 300},
            }

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"], seen["payload"] = url, json
        return Resp()

    monkeypatch.delenv("GEMINI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_THINKING_LEVEL", raising=False)
    monkeypatch.setattr(receipt_llm.requests, "post", fake_post)
    out = receipt_llm.call_gemini_vision("b64", "image/jpeg", "k", timeout=5)
    assert "/models/gemini-3.1-flash-lite:generateContent" in seen["url"]
    config = seen["payload"]["generationConfig"]
    assert "temperature" not in config and "thinkingConfig" not in config
    assert out["items"][0]["name"] == "Tea"  # thought part skipped

    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "low")
    receipt_llm.call_gemini_vision("b64", "image/jpeg", "k", timeout=5)
    assert seen["payload"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


def test_gemini_402_billing_reports_unavailable(monkeypatch):
    class Resp:
        status_code = 402
        text = '{"error": {"code": 402, "message": "Your prepayment credits are depleted."}}'

    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(receipt_llm, "_gemini_cb_open_until", 0.0)
    calls = []
    monkeypatch.setattr(receipt_llm.requests, "post", lambda *a, **k: calls.append(1) or Resp())
    with pytest.raises(VisionUnavailable):
        extract_receipt_from_vision("b64", budget_seconds=20, clock=FakeClock())
    assert len(calls) == 1                     # no point trying the second key
    assert receipt_llm.gemini_circuit_is_open()  # stop hammering for a few minutes
    monkeypatch.setattr(receipt_llm, "_gemini_cb_open_until", 0.0)


def test_audit_ignores_round_off_but_flags_real_gaps():
    room = {"items": [{"name": "Thali", "price": "₹833.35"}],
            "selections": {"A": ["Thali"]},
            "ocr_data": {"subtotal": "₹833.35", "cgst": "₹20.83", "sgst": "₹20.83", "total": "₹875.00"}}
    split = calculate_bill_split(room)          # 875.01 vs printed 875.00
    titles = [w["title"] for w in audit_bill(room, split)]
    assert "Split total doesn't match bill total" not in titles
    room["ocr_data"]["total"] = "₹880.00"          # ₹5 off: real problem
    titles = [w["title"] for w in audit_bill(room, calculate_bill_split(room))]
    assert "Split total doesn't match bill total" in titles


def test_item_category_normalization():
    from utils.receipt_contracts import normalize_item_category, normalize_receipt_payload

    assert normalize_item_category("Non-Veg") == "nonveg"
    assert normalize_item_category(" ALCOHOLIC ") == "alcohol"
    assert normalize_item_category("eggetarian") == "egg"
    assert normalize_item_category("vegetarian") == "veg"
    for junk in (None, "", 7, "<script>", "dessert"):
        assert normalize_item_category(junk) == "other"

    receipt = normalize_receipt_payload({"items": [
        {"name": "Paneer Tikka", "price": "₹320", "category": "veg"},
        {"name": "Kingfisher", "price": "₹250", "category": "Alcohol"},
        {"name": "Cover charge", "price": "₹100"},
    ]})
    assert [i["category"] for i in receipt["items"]] == ["veg", "alcohol", "other"]
    assert [i["category"] for i in clean_items(receipt["items"])] == ["veg", "alcohol", "other"]


def test_scan_prompt_asks_for_category():
    from utils.receipt_rules import build_image_receipt_prompt

    prompt = build_image_receipt_prompt()
    assert '"category":"[veg|nonveg|egg|alcohol|other]"' in prompt
    for word in ("nonveg", "egg", "alcohol", "other"):
        assert f'"{word}"' in prompt


# --- quantities and portions ------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (5, 5), ("5", 5), ("x2", 2), ("3 Nos", 3), ("2.0", 2),
    (None, 1), ("", 1), ("lots", 1), (0, 1), (True, 1), (500, 99),
])
def test_quantity_normalization(raw, expected):
    from utils.receipt_contracts import normalize_item_quantity
    from utils.validation import normalize_quantity

    assert normalize_quantity(raw) == expected
    assert normalize_item_quantity(raw) == expected


def test_scan_prompt_asks_for_quantity():
    from utils.receipt_rules import build_image_receipt_prompt

    prompt = build_image_receipt_prompt()
    assert '"quantity":[whole number]' in prompt
    assert "never the price of one" in prompt


def _dosa_room(claims):
    """Five dosas for 500 and one coffee; `claims` is [(participant id, item id, portions)]."""
    room = {"code": "ABCDEF", "room_name": "R", "num_people": 4, "ocr_data": {"total": "₹560.00"},
            "ocr_source": "manual", "finalized_at": None, "created_at": T0, "last_activity_at": T0}
    people = [
        {"id": n, "name": name, "is_host": n == 1, "joined_at": T0 + timedelta(seconds=n),
         "submitted_at": T0, "submission_id": "s", "user_id_hash": f"h{n}"}
        for n, name in enumerate(["Asha", "Bala", "Chitra", "Dev"], start=1)
    ]
    items = [
        {"id": 10, "position": 0, "name": "Masala Dosa", "price": Decimal("500"), "quantity": 5},
        {"id": 11, "position": 1, "name": "Coffee", "price": Decimal("60"), "quantity": 1},
    ]
    rows = [{"participant_id": p, "item_id": i, "portions": n} for p, i, n in claims]
    return assemble_room(room, people, items, rows)


def test_units_are_paid_per_unit_and_sharers_split_the_rest():
    # Five dosas for 500: Asha had two, Bala one, and Chitra and Dev shared the other two.
    r = _dosa_room([(1, 10, 2), (2, 10, 1), (3, 10, 0), (4, 10, 0), (1, 11, 1), (2, 11, 1)])
    assert r["items"][0]["quantity"] == 5
    assert r["portions"]["Asha"] == {"Masala Dosa": 2, "Coffee": 1}
    assert r["portions"]["Dev"] == {"Masala Dosa": 0}
    assert r["quantity_mismatches"] == []
    split = calculate_bill_split(r)
    people = split["user_breakdown"]
    assert people["Asha"]["item_total"] == 230.0       # two dosas, half the coffee
    assert people["Bala"]["item_total"] == 130.0
    assert people["Chitra"]["item_total"] == people["Dev"]["item_total"] == 100.0
    assert split["totals"]["subtotal"] == 560.0
    mine = people["Asha"]["item_details"][0]
    assert (mine["mode"], mine["portions"], mine["quantity"], mine["your_share"]) == ("units", 2, 5, 200.0)
    shared = people["Dev"]["item_details"][0]
    assert (shared["mode"], shared["unclaimed"], shared["sharers"], shared["your_share"]) == ("shared", 2, 2, 100.0)
    assert [w["severity"] for w in audit_bill(r, split)] == ["ok"]


def test_everyone_sharing_splits_the_whole_line():
    # Nobody claims a unit: five dosas between four people is a quarter each.
    r = _dosa_room([(n, 10, 0) for n in (1, 2, 3, 4)])
    split = calculate_bill_split(r)
    assert {d["item_total"] for d in split["user_breakdown"].values()} == {125.0}
    assert r["quantity_mismatches"] == []


def test_one_sharer_pays_for_everything_unclaimed():
    r = _dosa_room([(1, 10, 1), (2, 10, 0)])
    people = calculate_bill_split(r)["user_breakdown"]
    assert people["Asha"]["item_total"] == 100.0 and people["Bala"]["item_total"] == 400.0


def test_units_nobody_owns_are_not_charged_and_are_flagged():
    # Four of the five dosas claimed and nobody sharing: one dosa has no owner.
    r = _dosa_room([(1, 10, 1), (2, 10, 1), (3, 10, 1), (4, 10, 1), (1, 11, 1)])
    miss = r["quantity_mismatches"]
    assert [(m["name"], m["quantity"], m["claimed"], m["unowned"]) for m in miss] == [("Masala Dosa", 5, 4, 100.0)]
    assert miss[0]["by"][0] == {"name": "Asha", "portions": 1}
    split = calculate_bill_split(r)
    assert split["user_breakdown"]["Bala"]["item_total"] == 100.0
    assert split["totals"]["subtotal"] == 460.0
    audit = audit_bill(r, split)
    assert audit[0]["title"] == "Masala Dosa: 4 of 5 claimed" and audit[0]["severity"] == "warn"
    assert "ok" not in [w["severity"] for w in audit]


def test_rows_without_portions_count_as_one_each():
    r = assemble_room(*_rows())
    assert r["portions"] == {"Bob": {"Naan (2)": 1}}
    assert r["items"][0]["quantity"] == 1 and r["quantity_mismatches"] == []


# --- how taxes and service charge are divided --------------------------------

def _taxed_room(tax_split=None):
    """Asha ordered 300 of food, Bala 100; the bill adds 40 service charge and 20 + 20 GST."""
    ocr = {"serviceCharge": "₹40.00", "cgst": "₹20.00", "sgst": "₹20.00", "total": "₹480.00"}
    if tax_split:
        ocr["taxSplit"] = tax_split
    return {
        "items": [{"name": "Biryani", "price": "₹300.00", "quantity": 1},
                  {"name": "Dal", "price": "₹100.00", "quantity": 1}],
        "selections": {"Asha": ["Biryani"], "Bala": ["Dal"], "Chitra": []},
        "ocr_data": ocr,
    }


def test_taxes_follow_what_each_person_had_by_default():
    split = calculate_bill_split(_taxed_room())
    people = split["user_breakdown"]
    assert split["totals"]["tax_split"] == "proportional"
    assert (people["Asha"]["service_charge"], people["Asha"]["cgst"], people["Asha"]["sgst"]) == (30.0, 15.0, 15.0)
    assert (people["Bala"]["service_charge"], people["Bala"]["cgst"], people["Bala"]["sgst"]) == (10.0, 5.0, 5.0)
    assert people["Asha"]["final_amount"] == 360.0 and people["Bala"]["final_amount"] == 120.0
    assert people["Chitra"]["final_amount"] == 0.0          # picked nothing, owes nothing
    assert split["totals"]["grand_total"] == 480.0


def test_host_can_split_taxes_equally_instead():
    split = calculate_bill_split(_taxed_room("equal"))
    people = split["user_breakdown"]
    assert split["totals"]["tax_split"] == "equal"
    assert people["Asha"]["final_amount"] == 340.0 and people["Bala"]["final_amount"] == 140.0
    assert people["Chitra"]["final_amount"] == 0.0
    assert split["totals"]["grand_total"] == 480.0


def test_proportional_taxes_fall_back_to_equal_when_dishes_cost_nothing():
    room = _taxed_room()
    for item in room["items"]:
        item["price"] = "₹0.00"
    people = calculate_bill_split(room)["user_breakdown"]
    assert people["Asha"]["final_amount"] == people["Bala"]["final_amount"] == 40.0
