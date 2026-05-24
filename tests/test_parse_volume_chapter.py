"""Tests for _parse_volume_chapter — volume/chapter extraction from titles and URLs."""
from mangoteka.web.jobs import _parse_volume_chapter


def test_manga_in_ua_url():
    url = "https://manga.in.ua/manga/tokyo-ghoul/-tom-4/-rozdil-25/"
    vol, num = _parse_volume_chapter("", url)
    assert vol == "4"
    assert num == "25"


def test_faust_url_slash_tom():
    # Faust URLs have /tom-N/ (slash before tom, not dash) — the old regex missed this
    url = "https://faust-web.com/manga/berserk/tom-4/rozdil-25"
    vol, num = _parse_volume_chapter("", url)
    assert vol == "4"


def test_faust_url_no_false_positive():
    # "autom" or other words containing "tom" should not match
    url = "https://faust-web.com/manga/phantomhive/chapter-1"
    vol, num = _parse_volume_chapter("", url)
    assert vol is None


def test_title_tom_rozdil():
    vol, num = _parse_volume_chapter("Том 3. Розділ 12", "")
    assert vol == "3"
    assert num == "12"


def test_title_only_tom():
    vol, num = _parse_volume_chapter("Том 7", "")
    assert vol == "7"
    assert num == "0"


def test_title_only_rozdil():
    vol, num = _parse_volume_chapter("Розділ 99", "")
    assert vol is None
    assert num == "99"


def test_empty():
    vol, num = _parse_volume_chapter("", "")
    assert vol is None
    assert num == "0"
