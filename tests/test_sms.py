import pytest

from git_sms.sms import MAX_PAGES, is_gsm7, normalize, paginate, segments


def test_normalize_maps_typography_to_gsm7():
    text = normalize(chr(0x201C) + "Fix it" + chr(0x201D) + " " + chr(0x2014) + " now" + chr(0x2026) + chr(0x200B))
    assert text == '"Fix it" - now...'
    assert is_gsm7(text)


def test_normalize_strips_controls_and_collapses_space():
    assert normalize("a\x00b\x07c   d\n\n\n\ne\t f") == "abc d\n\ne f"


def test_non_latin_text_survives_as_ucs2():
    text = normalize("Привет 👋")
    assert text == "Привет 👋"
    assert not is_gsm7(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 1),
        ("a" * 160, 1),
        ("a" * 161, 2),
        ("a" * 306, 2),
        ("a" * 307, 3),
        ("[" * 80, 1),
        ("[" * 81, 2),
        ("é" * 160, 1),
        ("Ж" * 70, 1),
        ("Ж" * 71, 2),
        ("👋" * 35, 1),
        ("👋" * 36, 2),
    ],
)
def test_segment_counting(text, expected):
    assert segments(text) == expected


def test_short_text_is_one_unlabelled_page():
    assert paginate("hello", 3) == ["hello"]


@pytest.mark.parametrize("max_segments", [1, 2, 3])
@pytest.mark.parametrize("text", ["word " * 400, "x" * 2000, "line\n" * 300, "Ж " * 500])
def test_every_page_fits_the_segment_budget(text, max_segments):
    pages = paginate(text, max_segments)
    assert 1 < len(pages) <= MAX_PAGES
    assert all(segments(page) <= max_segments for page in pages)
    assert pages[0].endswith(f"[1/{len(pages)} MORE]")
    assert pages[-1].endswith(f"[{len(pages)}/{len(pages)}]")


def test_pages_break_on_word_boundaries_and_keep_content():
    text = " ".join(f"w{i}" for i in range(300))
    pages = paginate(text, 2)
    rebuilt = " ".join(page.rsplit(" [", 1)[0] for page in pages)
    assert rebuilt == text


def test_overlong_text_is_truncated_at_max_pages():
    pages = paginate("z " * 10_000, 1)
    assert len(pages) == MAX_PAGES
    assert pages[-1].endswith(f"... [{MAX_PAGES}/{MAX_PAGES}]")
    assert all(segments(page) == 1 for page in pages)
