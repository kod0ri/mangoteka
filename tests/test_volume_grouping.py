"""Tests for volume grouping logic in jobs._run_volume_mode path."""
from mangoteka.web.jobs import _vol_sort_key, _parse_volume_chapter


def test_vol_sort_key_numeric():
    assert _vol_sort_key("1") < _vol_sort_key("2") < _vol_sort_key("10")


def test_vol_sort_key_zero():
    assert _vol_sort_key("0") == (0, 0)


def test_vol_sort_key_non_numeric_last():
    # Non-numeric keys sort after numeric
    assert _vol_sort_key("—") > _vol_sort_key("99")


def test_vol_sort_key_ordering():
    vols = ["10", "2", "—", "1", "0"]
    assert sorted(vols, key=_vol_sort_key) == ["0", "1", "2", "10", "—"]


def test_parse_volume_chapter_faust_tom4():
    # Faust: /tom-4/ in the path
    url = "https://faust-web.com/manga/berserk/tom-4/rozdil-100"
    vol, num = _parse_volume_chapter("", url)
    assert vol == "4", f"expected vol=4, got {vol!r}"


def test_parse_volume_chapter_miu_tom():
    # manga.in.ua: -tom-2 in the path
    url = "https://manga.in.ua/manga/one-piece/-tom-2/-rozdil-10/"
    vol, num = _parse_volume_chapter("", url)
    assert vol == "2"
    assert num == "10"


def test_parse_volume_chapter_no_vol_in_url():
    url = "https://manga.in.ua/manga/some/-rozdil-5/"
    vol, num = _parse_volume_chapter("", url)
    assert vol is None
    assert num == "5"


def test_parse_volume_chapter_prefers_title():
    # Title takes precedence over URL
    url = "https://faust-web.com/manga/x/tom-3/chapter-5"
    vol, num = _parse_volume_chapter("Том 7. Розділ 42", url)
    assert vol == "7"
    assert num == "42"
