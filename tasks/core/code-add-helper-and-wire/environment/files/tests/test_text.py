from jotter.text import truncate


def test_short_text_is_untouched():
    assert truncate("hello", 10) == "hello"


def test_long_text_is_cut_with_ellipsis():
    assert truncate("abcdefghij", 5) == "abcd…"
