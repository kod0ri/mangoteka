"""Tests for ComickClient — langList parsing, fetch_meta structure."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from mangoteka.comick_scraper import ComickClient, CDN


def _make_comic_response(lang_list=None, genres=None):
    return {
        "comic": {
            "hid": "abc123",
            "title": "Tokyo Ghoul:re",
            "desc": "A story about ghouls.",
            "year": 2014,
            "status": 2,
            "md_covers": [{"b2key": "covers/tgre.jpg"}],
            "md_titles": [{"title": "Tokyo Ghoul:re", "lang": "en"}],
            "genres": genres or [{"name": "Action"}, {"name": "Horror"}],
        },
        "langList": lang_list if lang_list is not None else ["en", "uk", "ru"],
        "artists": [],
        "authors": [],
    }


@pytest.mark.asyncio
async def test_fetch_meta_langlist_as_strings():
    """langList from API is a list of strings, not dicts."""
    fake_r = MagicMock()
    fake_r.status_code = 200
    fake_r.json.return_value = _make_comic_response(lang_list=["en", "uk"])

    client = ComickClient()
    client._session = MagicMock()
    client._sem = AsyncMock()
    client._sem.__aenter__ = AsyncMock(return_value=None)
    client._sem.__aexit__ = AsyncMock(return_value=None)
    client._session.get = AsyncMock(return_value=fake_r)

    meta = await client.fetch_meta("https://comick.io/comic/tokyo-ghoul-re")
    assert meta.title == "Tokyo Ghoul:re"
    assert "en" in meta.available_langs
    assert "uk" in meta.available_langs


@pytest.mark.asyncio
async def test_fetch_meta_langlist_empty():
    fake_r = MagicMock()
    fake_r.status_code = 200
    fake_r.json.return_value = _make_comic_response(lang_list=[])

    client = ComickClient()
    client._session = MagicMock()
    client._sem = AsyncMock()
    client._sem.__aenter__ = AsyncMock(return_value=None)
    client._sem.__aexit__ = AsyncMock(return_value=None)
    client._session.get = AsyncMock(return_value=fake_r)

    meta = await client.fetch_meta("https://comick.io/comic/tokyo-ghoul-re")
    assert meta.available_langs == []


@pytest.mark.asyncio
async def test_fetch_meta_cover_url():
    fake_r = MagicMock()
    fake_r.status_code = 200
    fake_r.json.return_value = _make_comic_response()

    client = ComickClient()
    client._session = MagicMock()
    client._sem = AsyncMock()
    client._sem.__aenter__ = AsyncMock(return_value=None)
    client._sem.__aexit__ = AsyncMock(return_value=None)
    client._session.get = AsyncMock(return_value=fake_r)

    meta = await client.fetch_meta("https://comick.io/comic/tokyo-ghoul-re")
    assert meta.cover_url == f"{CDN}/covers/tgre.jpg"


@pytest.mark.asyncio
async def test_fetch_meta_status_map():
    fake_r = MagicMock()
    fake_r.status_code = 200
    fake_r.json.return_value = _make_comic_response()  # status=2 → completed

    client = ComickClient()
    client._session = MagicMock()
    client._sem = AsyncMock()
    client._sem.__aenter__ = AsyncMock(return_value=None)
    client._sem.__aexit__ = AsyncMock(return_value=None)
    client._session.get = AsyncMock(return_value=fake_r)

    meta = await client.fetch_meta("https://comick.io/comic/tokyo-ghoul-re")
    assert meta.status == "completed"
