"""Non-Latin names in the PDF: which font each run of text gets."""
from utils.pdf_text import families_in, font_face_css, font_path, install_shaping, pdf_text, runs


def test_runs_split_by_script_and_keep_latin_in_helvetica():
    assert runs("Butter Chicken (बटर चिकन)") == [(None, "Butter Chicken ("), ("bbdeva", "बटर"), (None, " "),
                                                 ("bbdeva", "चिकन"), (None, ")")]
    assert runs("消費税 10%") == [("bbcjk", "消費税"), (None, " 10%")]
    assert runs("Paneer Tikka") == [(None, "Paneer Tikka")]
    # A zero-width joiner shapes the letters around it, so it stays in their run.
    assert runs("क्‍ष") == [("bbdeva", "क्‍ष")]


def test_pdf_text_escapes_and_wraps_only_non_latin():
    assert pdf_text("Tea <b>") == "Tea &lt;b&gt;"
    assert pdf_text("தோசை") == '<span style="font-family: bbtaml">தோசை</span>'
    assert pdf_text(None) == ""


def test_only_the_fonts_a_room_needs_are_loaded():
    assert families_in(["Paneer", "Dal", "₹40"]) == {"bbdeva"}          # ₹ comes from the Devanagari font
    assert families_in(["Paneer", "Dal"]) == set()
    assert families_in(["醤油ラーメン", "মাছ", "ಮಸಾಲೆ"]) == {"bbcjk", "bbbeng", "bbknda"}
    css = font_face_css({"bbdeva"})
    assert css == "@font-face { font-family: bbdeva; src: url('bbfont/NotoSansDevanagari-Regular.ttf'); }"


def test_font_urls_only_reach_bundled_files():
    assert font_path("NotoSansTamil-Regular.ttf").endswith("static/fonts/pdf/NotoSansTamil-Regular.ttf")
    assert font_path("../../app.py") is None and font_path("") is None


def test_every_bundled_font_file_exists():
    from utils.pdf_text import SCRIPT_FONTS
    import os

    for _family, filename, _ranges in SCRIPT_FONTS:
        assert os.path.isfile(font_path(filename)), filename


def test_shaping_is_available():
    assert install_shaping() is True and install_shaping() is True      # idempotent
