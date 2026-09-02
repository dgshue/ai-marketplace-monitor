"""eBay's two backends and the rule that picks between them."""

from typing import Any

import pytest

from ai_marketplace_monitor.browser_market import DEFAULT_MAX_LISTINGS
from ai_marketplace_monitor.ebay import (
    EbayBrowserMarketplace,
    EbayItemConfig,
    EbayMarketplace,
    EbayMarketplaceConfig,
)


def config(**kwargs: Any) -> EbayMarketplaceConfig:
    return EbayMarketplaceConfig(name="ebay", **kwargs)


# ---------------------------------------------------------------- mode


def test_no_credentials_defaults_to_browser() -> None:
    assert config().resolved_mode == "browser"


def test_credentials_default_to_api() -> None:
    assert config(client_id="id", client_secret="secret").resolved_mode == "api"


def test_half_a_credential_is_not_enough() -> None:
    assert config(client_id="id").resolved_mode == "browser"
    assert config(client_secret="secret").resolved_mode == "browser"


def test_blank_credentials_read_as_absent() -> None:
    # This is how an unset ${EBAY_CLIENT_ID} arrives from a compose file.
    assert config(client_id="  ", client_secret="").resolved_mode == "browser"


def test_explicit_mode_beats_credentials() -> None:
    assert (
        config(client_id="id", client_secret="secret", mode="browser").resolved_mode == "browser"
    )
    assert config(mode="api").resolved_mode == "api"


def test_mode_is_normalized() -> None:
    assert config(mode="  BROWSER ").mode == "browser"


def test_blank_mode_means_automatic() -> None:
    cfg = config(mode="", client_id="id", client_secret="secret")
    assert cfg.mode is None
    assert cfg.resolved_mode == "api"


@pytest.mark.parametrize("bad", ["scrape", "rest", "Api2", 7])
def test_invalid_mode_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        config(mode=bad)


def test_needs_browser_is_per_instance() -> None:
    market = EbayMarketplace("ebay", None, None, None)
    market.configure(config(client_id="id", client_secret="secret"))
    assert market.mode == "api"
    assert market.needs_browser() is False

    market.configure(config())
    assert market.mode == "browser"
    assert market.needs_browser() is True


# ------------------------------------------------------------- browser


def scraper(**kwargs: Any) -> EbayBrowserMarketplace:
    market = EbayBrowserMarketplace("ebay", None, None, None)
    market.configure(config(**kwargs))
    return market


def test_search_url_is_newest_first() -> None:
    url = scraper().search_url("gopro hero 11", EbayItemConfig(name="i", search_phrases=["x"]))
    assert url.startswith("https://www.ebay.com/sch/i.html?")
    assert "_nkw=gopro+hero+11" in url
    assert "_sop=10" in url


def test_search_url_carries_price_bounds_and_condition() -> None:
    item = EbayItemConfig(
        name="i",
        search_phrases=["x"],
        min_price="100",
        max_price="300 USD",
        condition=["used_good"],
    )
    url = scraper().search_url("cam", item)
    assert "_udlo=100" in url
    assert "_udhi=300" in url
    assert "LH_ItemCondition=3000" in url


def test_search_url_follows_the_marketplace_id() -> None:
    url = scraper(marketplace_id="EBAY_GB").search_url(
        "cam", EbayItemConfig(name="i", search_phrases=["x"])
    )
    assert url.startswith("https://www.ebay.co.uk/sch/")


TILE = {
    "id": "227501449913",
    "title": "NEW LISTINGGoPro HERO11 Black Bundle Opens in a new window or tab",
    "price": "$230.00",
    "img": "https://i.ebayimg.com/images/g/x/s-l500.webp",
    "subtitles": ["*NO Battery or SD Card* | Missing Lens", "Pre-Owned"],
    "attrs": ["$230.00", "0 bids", "Located in United States", "Sep-1 13:52"],
}


def test_tile_becomes_a_listing() -> None:
    listing = scraper().tile_to_listing(TILE)
    assert listing is not None
    assert listing.id == "227501449913"
    # The "NEW LISTING" flag and the screen-reader hint are not part of the title.
    assert listing.title == "GoPro HERO11 Black Bundle"
    assert listing.price == "$230.00"
    # The condition is eBay's vocabulary, not the seller's free-text note.
    assert listing.condition == "Pre-Owned"
    assert listing.location == "United States"
    assert listing.post_url == "https://www.ebay.com/itm/227501449913"
    assert listing.marketplace == "ebay"
    # Search tiles carry neither; empty is honest.
    assert listing.seller == ""
    assert listing.description == ""


def test_house_ad_tile_is_dropped() -> None:
    assert scraper().tile_to_listing({"id": "123456", "title": "Shop on eBay"}) is None


def test_tile_without_an_id_is_dropped() -> None:
    assert scraper().tile_to_listing({"id": "", "title": "Real listing"}) is None


def test_ebay_block_pages_are_recognized() -> None:
    markers = EbayBrowserMarketplace.block_title_markers
    assert any(m in "pardon our interruption..." for m in markers)
    assert any(m in "error page | ebay" for m in markers)
    # The inherited Cloudflare markers must survive.
    assert "just a moment" in markers


# --------------------------------------------------------- result cap


def item(**kwargs: Any) -> EbayItemConfig:
    kwargs.setdefault("search_phrases", ["x"])
    return EbayItemConfig(name="i", **kwargs)


def test_page_size_follows_the_cap() -> None:
    """No point downloading 240 tiles to rate 60 of them."""
    assert "_ipg=60" in scraper().search_url("cam", item())
    assert "_ipg=60" in scraper().search_url("cam", item(max_listings=60))
    assert "_ipg=120" in scraper().search_url("cam", item(max_listings=61))
    assert "_ipg=240" in scraper().search_url("cam", item(max_listings=240))
    # Beyond the largest page eBay offers, ask for that page.
    assert "_ipg=240" in scraper().search_url("cam", item(max_listings=1000))


def test_item_cap_beats_marketplace_cap() -> None:
    url = scraper(max_listings=200).search_url("cam", item(max_listings=30))
    assert "_ipg=60" in url


def test_browser_search_stops_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    market = scraper()
    market.page = object()  # type: ignore[assignment]
    tiles = [
        {"id": str(1000 + i), "title": f"cam {i}", "price": "$10", "subtitles": [], "attrs": []}
        for i in range(200)
    ]
    monkeypatch.setattr(market, "_fetch_tiles", lambda url: tiles)

    assert len(list(market.search(item(max_listings=7)))) == 7
    # And the default when nothing is configured, which is what an uncapped
    # `car` item needed: 240 tiles per phrase was five hours of AI ratings.
    assert len(list(market.search(item()))) == DEFAULT_MAX_LISTINGS


def test_default_cap_is_the_browser_tile_default() -> None:
    assert DEFAULT_MAX_LISTINGS == 60


@pytest.mark.parametrize("bad", [0, -5, "many", True])
def test_invalid_max_listings_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        item(max_listings=bad)


def test_quoted_max_listings_is_accepted() -> None:
    assert item(max_listings="25").max_listings == 25


# ---------------------------------------------------------- category


def test_search_url_carries_the_category() -> None:
    url = scraper(category="6001").search_url("toyota", item())
    assert "_sacat=6001" in url


def test_no_category_means_no_sacat() -> None:
    assert "_sacat" not in scraper().search_url("toyota", item())


def test_numeric_category_from_toml_is_accepted() -> None:
    assert config(category=6001).category == "6001"


def test_blank_category_reads_as_absent() -> None:
    assert config(category="  ").category is None


@pytest.mark.parametrize("bad", ["cars", "60 01", "6001a"])
def test_non_numeric_category_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        config(category=bad)


# ------------------------------------------------------------- api mode


class FakeResponse:
    status_code = 200

    def __init__(self: "FakeResponse", count: int) -> None:
        self.count = count

    def json(self: "FakeResponse") -> Any:
        return {
            "itemSummaries": [
                {
                    "itemId": f"v1|{i}|0",
                    "title": f"item {i}",
                    "price": {"value": "10.00", "currency": "USD"},
                    "itemWebUrl": f"https://www.ebay.com/itm/{i}",
                }
                for i in range(self.count)
            ]
        }


def api_market(monkeypatch: pytest.MonkeyPatch, **cfg: Any) -> tuple:
    market = EbayMarketplace("ebay", None, None, None)
    market.configure(config(client_id="id", client_secret="secret", **cfg))
    monkeypatch.setattr(market, "_access_token", lambda: "token")
    seen: dict = {}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        seen.update(kwargs.get("params") or {})
        return FakeResponse(200)

    monkeypatch.setattr("ai_marketplace_monitor.ebay.requests.get", fake_get)
    return market, seen


def test_api_mode_honors_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    market, seen = api_market(monkeypatch)
    listings = list(market.search(item(max_listings=12)))
    # Browse takes a limit, so the cap is asked for rather than filtered.
    assert seen["limit"] == 12
    assert len(listings) == 12


def test_api_mode_without_a_cap_keeps_the_full_page(monkeypatch: pytest.MonkeyPatch) -> None:
    market, seen = api_market(monkeypatch)
    assert len(list(market.search(item()))) == 200
    assert seen["limit"] == 200


def test_api_mode_passes_the_category(monkeypatch: pytest.MonkeyPatch) -> None:
    market, seen = api_market(monkeypatch, category="6001")
    list(market.search(item(max_listings=1)))
    assert seen["category_ids"] == "6001"
