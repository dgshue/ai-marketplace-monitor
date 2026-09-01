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
from typing import Any, Dict, Generator, List

from .facebook import FacebookMarketItemCommonConfig
from .listing import Listing
from .marketplace import ItemConfig, Marketplace, MarketplaceConfig
from .utils import CounterItem, counter, hilight

NAV_TIMEOUT_MS = 25_000
TILE_WAIT_MS = 8_000
# One reload on a soft failure; a hard block (Cloudflare interstitial) is not
# retried — the next scheduled pass gets a fresh chance.
SOFT_RETRIES = 1

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

    def search_url(self: "BrowserTileMarketplace", phrase: str) -> str:
        raise NotImplementedError

    def tile_to_listing(self: "BrowserTileMarketplace", tile: Dict[str, Any]) -> Listing | None:
        raise NotImplementedError

    # -------------------------------------------------------------------

    @classmethod
    def get_config(cls: type, **kwargs: Any) -> BrowserMarketplaceConfig:
        return BrowserMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: type, **kwargs: Any) -> BrowserItemConfig:
        return BrowserItemConfig(**kwargs)

    def _fetch_tiles(self: "BrowserTileMarketplace", url: str) -> List[Dict[str, Any]] | None:
        assert self.page is not None
        for attempt in range(SOFT_RETRIES + 1):
            try:
                self.page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                title = self.page.title() or ""
                if "Just a moment" in title or "Forbidden" in title or "Access denied" in title:
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

        for phrase in item_config.search_phrases or []:
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Search]", "info")} Searching {self.display_name} for {hilight(phrase)}"""
                )
            tiles = self._fetch_tiles(self.search_url(phrase))
            if tiles is None:
                continue
            counter.increment(CounterItem.SEARCH_PERFORMED, item_config.name)
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
