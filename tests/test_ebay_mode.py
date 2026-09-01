"""eBay's two backends and the rule that picks between them."""

import pytest

from ai_marketplace_monitor.ebay import (
    EbayBrowserMarketplace,
    EbayItemConfig,
    EbayMarketplace,
    EbayMarketplaceConfig,
)


def config(**kwargs: object) -> EbayMarketplaceConfig:
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
def test_invalid_mode_rejected(bad: object) -> None:
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


def scraper(**kwargs: object) -> EbayBrowserMarketplace:
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
