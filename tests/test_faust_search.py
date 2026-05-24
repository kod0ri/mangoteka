"""Tests for FaustClient.search — API key mapping and SearchResult construction."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from mangoteka.faust_scraper import FaustClient


def _make_faust_item(**kwargs):
    base = {
        "slug": "berserk",
        "name": "Берсерк",
        "coverImageUrl": "https://cdn.faust-web.com/cover.jpg",
        "publicationStatus": "Ongoing",
        "tags": [{"name": "Фентезі"}, {"name": "Екшн"}],
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_search_uses_titles_key():
    """API returns {"titles": [...]}, not "items" or "data"."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"titles": [_make_faust_item()]}

    with patch.object(FaustClient, "_get", new_callable=AsyncMock, return_value=fake_response):
        with patch.object(FaustClient, "__aenter__", return_value=FaustClient()):
            client = FaustClient()
            client._get = AsyncMock(return_value=fake_response)
            client._session = MagicMock()

            from mangoteka.search import SearchResult
            from mangoteka.faust_scraper import FaustClient as FC

            with patch("mangoteka.faust_scraper.FaustClient.__aenter__", return_value=client), \
                 patch("mangoteka.faust_scraper.FaustClient.__aexit__", return_value=None):
                results = await FC.search("berserk", limit=5)

    assert len(results) == 1
    assert results[0].title == "Берсерк"
    assert results[0].url == "https://faust-web.com/manga/berserk"
    assert results[0].source == "faust"


@pytest.mark.asyncio
async def test_search_no_genres_field_on_searchresult():
    """SearchResult has no 'genres' field — passing it must not crash."""
    from mangoteka.search import SearchResult
    # Ensure SearchResult can be constructed without genres
    r = SearchResult(title="T", url="http://x", source="faust")
    assert not hasattr(r, "genres")


@pytest.mark.asyncio
async def test_search_falls_back_to_items_key():
    """If API switches back to 'items', we still handle it."""
    fake_response = MagicMock()
    fake_response.json.return_value = {"items": [_make_faust_item(slug="test", name="Test")]}

    client = FaustClient()
    client._get = AsyncMock(return_value=fake_response)
    client._session = MagicMock()

    with patch("mangoteka.faust_scraper.FaustClient.__aenter__", return_value=client), \
         patch("mangoteka.faust_scraper.FaustClient.__aexit__", return_value=None):
        results = await FaustClient.search("test")

    assert len(results) == 1
    assert results[0].title == "Test"


@pytest.mark.asyncio
async def test_search_skips_items_without_slug():
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "titles": [
            {"name": "No Slug"},
            _make_faust_item(),
        ]
    }

    client = FaustClient()
    client._get = AsyncMock(return_value=fake_response)
    client._session = MagicMock()

    with patch("mangoteka.faust_scraper.FaustClient.__aenter__", return_value=client), \
         patch("mangoteka.faust_scraper.FaustClient.__aexit__", return_value=None):
        results = await FaustClient.search("x")

    assert len(results) == 1
    assert results[0].title == "Берсерк"
