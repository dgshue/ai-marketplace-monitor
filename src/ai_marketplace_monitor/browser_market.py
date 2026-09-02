"""Shared base for browser-scraped marketplaces with no login (Depop, Poshmark).

Extraction approach ported from secondhand-mcp (MIT, github.com/jlsookiki/
secondhand-mcp): key off the stable listing-URL pattern, the tile's <img>, and
a price regex — never off hashed CSS-module class names, which change on every
deploy. Their implementation rotates residential proxy IPs to survive
Cloudflare; this one runs in a headed Chromium with a persistent profile on a
residential connection, which is a stronger fingerprint than any rotation, so
a block is treated as "log and skip this pass" rather than retried through
proxies we don't have.

Search tiles carry no seller, condition, or description, so Listings from
these backends have those fields empty — the AI judges on title and price.
That is weaker than Facebook's full-description evaluation and is said out
loud in the form schema rather than discovered by surprise.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Tuple, Type

from .facebook import FacebookMarketItemCommonConfig
from .listing import Listing
from .marketplace import ItemConfig, Marketplace, MarketplaceConfig
from .utils import CounterItem, counter, hilight

NAV_TIMEOUT_MS = 25_000
TILE_WAIT_MS = 8_000
# One reload on a soft failure. A hard block is only retried by backends that
# set block_retry_delay; otherwise the next scheduled pass gets a fresh chance.
SOFT_RETRIES = 1

# How many listings one search phrase may hand to the AI when the config says
# nothing. These backends return a whole search page at once -- eBay serves up
# to 240 tiles per phrase -- and every tile costs a full AI rating, about 25s
# against a local Ollama. Four phrases x 240 tiles is a five-hour pass, which
# is what a `car` item actually did. 60 is eBay's own default page size and
# about 25 minutes of rating per phrase: enough to see everything new on a
# newest-first search, bounded enough that a pass finishes.
DEFAULT_MAX_LISTINGS = 60

_PRICE_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def price_number(text: str | None) -> float | None:
    if not text:
        return None
    match = _PRICE_NUM.search(text.replace(",", ""))
    return float(match.group(1)) if match else None


@dataclass
class BrowserItemConfig(ItemConfig, FacebookMarketItemCommonConfig):
    """Shared item field set for cross-backend items.

    An unbound item constructed by any backend must be usable by every other
    one (see EbayItemConfig for the full rationale).
    """


@dataclass
class BrowserMarketplaceConfig(MarketplaceConfig):
    def handle_market_type(self: "BrowserMarketplaceConfig") -> None:
        # The base class pins market_type to facebook; these are not that.
        return


class BrowserTileMarketplace(Marketplace):
    """Search-page tile scraper.

    Subclasses define the URL, the anchor pattern, and how a raw tile becomes
    a Listing.
    """

    requires_search_city = False
    requires_browser = True

    # Subclass contract -------------------------------------------------
    display_name = ""
    anchor_selector = ""  # e.g. 'a[href*="/products/"]'
    extract_js = ""  # page-side extraction, returns a list of tile dicts
    # Lower-cased fragments of <title> that mean "we were served an
    # interstitial, not results". Subclasses extend rather than replace, so a
    # site-specific block page (eBay's "Pardon our interruption") is still
    # recognised alongside the generic Cloudflare ones.
    block_title_markers: Tuple[str, ...] = (
        "just a moment",
        "forbidden",
        "access denied",
        "attention required",
    )
    # Seconds to wait before each search page after the first. Sites that
    # throttle bursts (measured on eBay: several back-to-back loads from one IP
    # get an interstitial, the same URL a minute later does not) set this so a
    # multi-phrase item does not look like a scrape.
    phrase_delay = 0.0
    # Seconds to wait before re-trying a page that came back as a block. Zero
    # means "give up for this pass", which is right for a Cloudflare challenge
    # that will not clear on its own.
    block_retry_delay = 0.0

    def search_url(
        self: "BrowserTileMarketplace", phrase: str, item_config: "BrowserItemConfig"
    ) -> str:
        raise NotImplementedError

    def tile_to_listing(self: "BrowserTileMarketplace", tile: Dict[str, Any]) -> Listing | None:
        raise NotImplementedError

    # -------------------------------------------------------------------

    @classmethod
    def get_config(cls: Type["BrowserTileMarketplace"], **kwargs: Any) -> BrowserMarketplaceConfig:
        return BrowserMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: Type["BrowserTileMarketplace"], **kwargs: Any) -> BrowserItemConfig:
        return BrowserItemConfig(**kwargs)

    def _fetch_tiles(self: "BrowserTileMarketplace", url: str) -> List[Dict[str, Any]] | None:
        assert self.page is not None
        for attempt in range(SOFT_RETRIES + 1):
            try:
                self.page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                title = self.page.title() or ""
                lowered = title.lower()
                if any(marker in lowered for marker in self.block_title_markers):
                    if attempt < SOFT_RETRIES and self.block_retry_delay:
                        # A burst-throttle interstitial clears on its own; the
                        # identical URL succeeds after a pause. Worth one wait
                        # rather than losing the phrase for a whole cycle.
                        if self.logger:
                            self.logger.debug(
                                f"{self.display_name} served {title!r}; "
                                f"retrying in {self.block_retry_delay:g}s."
                            )
                        time.sleep(self.block_retry_delay)
                        continue
                    if self.logger:
                        self.logger.warning(
                            f"""{hilight("[Search]", "fail")} {self.display_name} blocked this pass ({title!r}); will try again next cycle."""
                        )
                    return None
                try:
                    self.page.wait_for_selector(self.anchor_selector, timeout=TILE_WAIT_MS)
                except Exception:  # noqa: S110 — SSR content may already be present
                    pass
                tiles = self.page.evaluate(self.extract_js)
                if isinstance(tiles, list):
                    return tiles
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if attempt >= SOFT_RETRIES:
                    if self.logger:
                        self.logger.error(
                            f"""{hilight("[Search]", "fail")} {self.display_name} navigation failed: {e}"""
                        )
                    return None
                time.sleep(2)
        return None

    def search(
        self: "BrowserTileMarketplace", item_config: BrowserItemConfig
    ) -> Generator[Listing, None, None]:
        if not self.page:
            self.page = self.create_page()

        config = self.config
        low = price_number(item_config.min_price or config.min_price)
        high = price_number(item_config.max_price or config.max_price)
        # Item beats marketplace beats the built-in default.
        max_listings = item_config.max_listings or config.max_listings or DEFAULT_MAX_LISTINGS

        for index, phrase in enumerate(item_config.search_phrases or []):
            if index and self.phrase_delay:
                time.sleep(self.phrase_delay)
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Search]", "info")} Searching {self.display_name} for {hilight(phrase)}"""
                )
            tiles = self._fetch_tiles(self.search_url(phrase, item_config))
            if tiles is None:
                continue
            counter.increment(CounterItem.SEARCH_PERFORMED, item_config.name)
            yielded = 0
            for tile in tiles:
                listing = self.tile_to_listing(tile)
                if listing is None:
                    continue
                # These search pages don't reliably honor price params in the
                # URL, so bounds are applied over the rendered results — same
                # call the upstream implementation makes.
                numeric = price_number(listing.price)
                if numeric is not None:
                    if low is not None and numeric < low:
                        continue
                    if high is not None and numeric > high:
                        continue
                counter.increment(CounterItem.LISTING_EXAMINED, item_config.name)
                yield listing
                yielded += 1
                if yielded >= max_listings:
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Search]", "info")} Stopping at the """
                            f"""{max_listings}-listing cap for {hilight(phrase)} on """
                            f"""{self.display_name}; raise max_listings to see more."""
                        )
                    break
