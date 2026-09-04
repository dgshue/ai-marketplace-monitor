"""eBay backend, with two interchangeable ways to search.

`mode = "api"` talks to eBay's official Browse API: fast, richer (seller,
condition, description, exact item location), roughly 5,000 calls/day, and it
needs no browser at all. The price is a developer account -- an application key
set from https://developer.ebay.com -- which is a real barrier for someone who
just wants deal alerts.

`mode = "browser"` scrapes the ordinary ebay.com search page in the same headed
Chromium the Depop and Poshmark backends already use. No account, no keys, no
OAuth; the trade is fewer fields (no seller, no description) and the usual
fragility of markup that changes without notice.

Neither is written into the config by default. `mode` left unset resolves to
"api" when both credentials are present and "browser" otherwise, so eBay works
out of the box and silently upgrades itself the moment keys appear.

Keep credentials out of config.toml with ${EBAY_CLIENT_ID} / ${EBAY_CLIENT_SECRET}.
"""

from __future__ import annotations

import base64
import datetime
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Tuple, Type
from urllib.parse import urlencode

import requests  # type: ignore

from .browser_market import DEFAULT_MAX_LISTINGS, BrowserItemConfig, BrowserTileMarketplace
from .listing import Listing
from .marketplace import MARKETPLACE_DISPLAY_NAMES, MarketPlace, Marketplace, MarketplaceConfig
from .utils import CounterItem, counter, hilight, parse_relative_time

# Production endpoints. The sandbox equivalents return synthetic inventory that
# is useless for deal-hunting, so they are not offered as an option.
OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# The only scope client-credentials grants can obtain, and all Browse needs.
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay caps item_summary/search at 200 per page.
MAX_LIMIT = 200

# Refresh a little before the token actually lapses, so a long search cannot
# expire mid-flight.
TOKEN_EXPIRY_MARGIN = 60

VALID_BUYING_OPTIONS = {"FIXED_PRICE", "AUCTION", "BEST_OFFER", "CLASSIFIED_AD"}

# How a search is performed. "api" needs credentials; "browser" needs the
# shared Chromium and nothing else.
VALID_MODES = ("api", "browser")

# How eBay writes a listing's date on a search tile, newest-first. Both forms
# appear: the short one on items listed inside the current year, the long one
# once they roll over. Anything else (a relative phrase, an auction's
# "time left") is handed to the shared relative-time parser instead.
TILE_DATE_FORMATS = ("%b-%d %H:%M", "%b %d, %Y", "%b-%d-%Y %H:%M")


def _parse_tile_date(text: str, now: float | None = None) -> float | None:
    """A search tile's listing date as an epoch, or None.

    Tries the relative wording first (it is the shared, translated path), then
    eBay's own absolute formats. The short format carries no year, so it is
    read as the current one and rolled back a year if that would put the
    listing in the future.
    """
    reference = time.time() if now is None else float(now)
    stamp = parse_relative_time(text, now=reference)
    if stamp is not None:
        return stamp
    candidate = " ".join((text or "").split())
    if not candidate:
        return None
    today = datetime.datetime.fromtimestamp(reference)
    for fmt in TILE_DATE_FORMATS:
        try:
            parsed = datetime.datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            parsed = parsed.replace(year=today.year)
            if parsed.timestamp() > reference + 86400:
                parsed = parsed.replace(year=today.year - 1)
        value = parsed.timestamp()
        return None if value > reference + 86400 else value
    return None


def _parse_item_creation_date(value: Any) -> float | None:
    """`itemCreationDate` from the Browse API: ISO 8601, always UTC ("...Z")."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Browser mode
#
# Selectors were verified against a live ebay.com search on 2026-09-01 (headed
# Chromium in this app's own container: 62 `li.s-card` tiles for "gopro hero
# 11", zero `li.s-item`) and cross-checked against scraper-bank's Playwright
# eBay search scraper, regenerated 2026-01-17:
#   github.com/scraper-bank/eBay.com-Scrapers
#   node/playwright/product_search/scraper/ebay.com_scraper_product_search_v1.js
# It keys off `ul.srp-results li.s-card` and reads `.s-card__title`,
# `.s-card__price`, `img.s-card__image` and `a.s-card__link`. eBay replaced the
# older `li.s-item` markup with `li.s-card` during 2025 but still serves the old
# one in places, so both are matched -- the same dual-layout handling every
# current scraper has converged on.
#
# secondhand-mcp, the reference the Depop and Poshmark backends were ported
# from, deliberately has no eBay scraper: its ebay.ts calls the Browse API,
# which is what this file's API mode already does.
# ---------------------------------------------------------------------------
BROWSER_SEARCH_HOSTS = {
    "EBAY_US": "www.ebay.com",
    "EBAY_GB": "www.ebay.co.uk",
    "EBAY_CA": "www.ebay.ca",
    "EBAY_DE": "www.ebay.de",
    "EBAY_AU": "www.ebay.com.au",
}
DEFAULT_BROWSER_HOST = "www.ebay.com"

# _sop=10 is "Time: newly listed". A monitor wants what appeared since the last
# pass, not eBay's relevance ranking -- the same choice API mode makes with
# sort=newlyListed.
SORT_NEWLY_LISTED = "10"
# Page sizes the search UI offers, smallest first. One page per phrase per pass
# is plenty for a newest-first monitor; asking for 240 tiles when the cap is 60
# just downloads 180 tiles to throw away.
ITEMS_PER_PAGE_CHOICES = (60, 120, 240)


def _items_per_page(max_listings: int) -> str:
    """Smallest offered page size that still covers the cap."""
    for size in ITEMS_PER_PAGE_CHOICES:
        if max_listings <= size:
            return str(size)
    return str(ITEMS_PER_PAGE_CHOICES[-1])


# LH_ItemCondition ids, from eBay's own condition-id table:
# https://developer.ebay.com/api-docs/sell/static/metadata/condition-id-values.html
# Keys are the vocabulary users already write for Facebook, so one `condition`
# line works across backends.
BROWSER_CONDITION_IDS = {
    "new": "1000",
    "open_box": "1500",
    "refurbished": "2000",
    "certified_refurbished": "2000",
    "seller_refurbished": "2500",
    "used_like_new": "2750",
    "used": "3000",
    "used_good": "3000",
    "used_fair": "3000",
    "for_parts": "7000",
}

# Every eBay search page opens with a house-ad card that looks exactly like a
# listing but points at /itm/123456 and is titled "Shop on eBay".
HOUSE_AD_ITEM_ID = "123456"

# Tiles prefix new listings with a "NEW LISTING" flag and append a screen-reader
# hint to the link text; neither belongs in the title the AI reads.
_TITLE_PREFIX = re.compile(r"^(?:new listing|sponsored)\s*", re.IGNORECASE)
_TITLE_SUFFIX = re.compile(r"\s*opens in a new window or tab\s*$", re.IGNORECASE)
# "Located in United States" (s-card) / "from United States" (s-item).
_LOCATION_ROW = re.compile(r"^(?:located in|from)\s+(.+)$", re.IGNORECASE)
# A tile can carry two subtitles: eBay's condition vocabulary and the seller's
# own free-text note ("*NO Battery or SD Card* | Missing Lens"). Only the first
# kind is a condition, so match the vocabulary rather than taking subtitle[0].
_CONDITION_ROW = re.compile(
    r"^(?:brand new|new|open box|pre-owned|certified|refurbished|used|"
    r"parts only|for parts|like new|very good|good|acceptable)\b",
    re.IGNORECASE,
)

# Browse returns eBay's condition vocabulary; these are the values a user is
# likely to write in config.
CONDITION_ALIASES = {
    "new": "NEW",
    "used": "USED",
    "refurbished": "CERTIFIED_REFURBISHED",
    "certified_refurbished": "CERTIFIED_REFURBISHED",
    "seller_refurbished": "SELLER_REFURBISHED",
    "for_parts": "FOR_PARTS_OR_NOT_WORKING",
    "used_like_new": "USED",
    "used_good": "USED",
    "used_fair": "USED",
}


def _numeric_price(value: str | None) -> str | None:
    """Pull a bare number out of '300' or '300 USD' for the price filter."""
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return match.group(0) if match else None


@dataclass
class EbayItemConfig(BrowserItemConfig):
    """Accepts the Facebook-specific item keys as well as the generic ones.

    An item that names no marketplace is searched everywhere, and config.py
    builds exactly one item object for it -- whichever backend it iterates
    last. So an item carrying `condition` or `date_listed` would fail to
    construct here, and one built here would be missing attributes Facebook's
    search reads. Sharing the field set makes the object usable by either
    backend regardless of iteration order; eBay maps `condition` and ignores
    the rest.

    Deriving from BrowserItemConfig (itself ItemConfig + the Facebook mixin,
    so the field set is unchanged) rather than repeating those bases is what
    lets browser mode hand the very same object to the tile scraper.
    """


@dataclass
class EbayMarketplaceConfig(MarketplaceConfig):
    """eBay-specific options.

    Everything else is inherited and behaves as it does for Facebook --
    search_interval, rating, notify, keywords, prices.
    """

    # "api", "browser", or unset -- see resolved_mode.
    mode: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    # Which eBay site to search. EBAY_US, EBAY_GB, EBAY_DE, ...
    marketplace_id: str | None = None
    # Restrict to items that will actually ship to you.
    delivery_country: str | None = None
    buying_options: List[str] | None = None
    # eBay category id, e.g. 6001 for Cars & Trucks. Narrows a search that a
    # phrase alone cannot: "toyota" matches a whole catalogue of floor mats
    # before it matches a car. Declared here rather than on the item config
    # because `category` already exists there as Facebook's own vocabulary
    # (FacebookMarketItemCommonConfig.handle_category), and an eBay numeric id
    # would fail that validator. Browser mode passes it as _sacat, API mode as
    # category_ids.
    category: str | None = None

    def handle_mode(self: "EbayMarketplaceConfig") -> None:
        if self.mode is None:
            return
        if not isinstance(self.mode, str):
            raise ValueError(f"Marketplace {hilight(self.name)} mode must be a string.")
        # Same "unset arrives as empty string" story as the credentials below:
        # a blank select in the web UI must mean "decide for me", not "invalid".
        self.mode = self.mode.strip().lower() or None
        if self.mode is not None and self.mode not in VALID_MODES:
            raise ValueError(
                f"Marketplace {hilight(self.name)} mode must be one of "
                f"{', '.join(VALID_MODES)}, not {hilight(self.mode)}."
            )

    @property
    def resolved_mode(self: "EbayMarketplaceConfig") -> str:
        """Which backend actually runs.

        An explicit `mode` always wins. Otherwise credentials decide: with a
        key set the API is strictly better, and without one the scraper is the
        only thing that can return anything at all. Resolving lazily (rather
        than in handle_mode) keeps this independent of the order __post_init__
        happens to run the field handlers in.
        """
        if self.mode:
            return self.mode
        return "api" if (self.client_id and self.client_secret) else "browser"

    def handle_client_id(self: "EbayMarketplaceConfig") -> None:
        if self.client_id is None:
            return
        if not isinstance(self.client_id, str):
            raise ValueError(f"Marketplace {hilight(self.name)} client_id must be a string.")
        # An empty value is how "not configured yet" arrives in practice: the
        # compose file passes EBAY_CLIENT_ID through unconditionally, so an
        # unset stack variable reaches ${EBAY_CLIENT_ID} as "". Rejecting that
        # at parse time made the whole config invalid before the user ever had
        # credentials; treat it as absent and fall back to browser mode.
        self.client_id = self.client_id.strip() or None

    def handle_client_secret(self: "EbayMarketplaceConfig") -> None:
        if self.client_secret is None:
            return
        if not isinstance(self.client_secret, str):
            raise ValueError(f"Marketplace {hilight(self.name)} client_secret must be a string.")
        self.client_secret = self.client_secret.strip() or None

    def handle_marketplace_id(self: "EbayMarketplaceConfig") -> None:
        if self.marketplace_id is None:
            self.marketplace_id = "EBAY_US"
            return
        if not isinstance(self.marketplace_id, str) or not self.marketplace_id.startswith("EBAY_"):
            raise ValueError(
                f"Marketplace {hilight(self.name)} marketplace_id must look like 'EBAY_US'."
            )

    def handle_category(self: "EbayMarketplaceConfig") -> None:
        if self.category is None:
            return
        # TOML gives an int for `category = 6001`; both spellings mean the
        # same id and both end up in a URL as a string.
        if isinstance(self.category, bool) or not isinstance(self.category, (int, str)):
            raise ValueError(
                f"Marketplace {hilight(self.name)} category must be an eBay category id."
            )
        self.category = str(self.category).strip()
        if not self.category:
            self.category = None
            return
        if not self.category.isdigit():
            raise ValueError(
                f"Marketplace {hilight(self.name)} category must be a numeric eBay "
                f"category id (e.g. 6001 for Cars & Trucks), not "
                f"{hilight(self.category)}."
            )

    def handle_delivery_country(self: "EbayMarketplaceConfig") -> None:
        if self.delivery_country is None:
            return
        if not isinstance(self.delivery_country, str) or len(self.delivery_country) != 2:
            raise ValueError(
                f"Marketplace {hilight(self.name)} delivery_country must be a 2-letter code."
            )
        self.delivery_country = self.delivery_country.upper()

    def handle_buying_options(self: "EbayMarketplaceConfig") -> None:
        if self.buying_options is None:
            return
        if isinstance(self.buying_options, str):
            self.buying_options = [self.buying_options]
        self.buying_options = [str(x).upper() for x in self.buying_options]
        invalid = [x for x in self.buying_options if x not in VALID_BUYING_OPTIONS]
        if invalid:
            raise ValueError(
                f"Marketplace {hilight(self.name)} buying_options {invalid} invalid. "
                f"Valid: {', '.join(sorted(VALID_BUYING_OPTIONS))}."
            )

    # market_type on the base class hard-codes Facebook; eBay is not that.
    def handle_market_type(self: "EbayMarketplaceConfig") -> None:
        return


class EbayBrowserMarketplace(BrowserTileMarketplace):
    """Search-page scraper for browser mode.

    Never registered as a marketplace of its own: `[marketplace.ebay]` stays a
    single section and a single registry entry, and EbayMarketplace drives this
    object when its mode resolves to "browser". It therefore never builds its
    own config -- the eBay config is handed to it by its owner.
    """

    display_name = MARKETPLACE_DISPLAY_NAMES[MarketPlace.EBAY.value]
    anchor_selector = "li.s-card, li.s-item"
    # eBay's bot wall renders as "Error Page | eBay" or "Pardon Our
    # Interruption"; a headless launch reliably trips it, a headed one does not.
    block_title_markers: Tuple[str, ...] = (
        *BrowserTileMarketplace.block_title_markers,
        "pardon our interruption",
        "error page",
        "security measure",
        "are you a robot",
    )
    # Measured 2026-09-01: four ebay.com search loads inside a minute from one
    # IP got the interstitial three times; the same URL a minute later returned
    # 242 tiles. So pace the phrases and give one blocked page a second chance
    # rather than dropping the phrase for the whole cycle.
    phrase_delay = 6.0
    block_retry_delay = 25.0

    # Both layouts read the same way: find the /itm/ link, then pull the four
    # fields that exist in either markup. Text is returned raw and parsed in
    # tile_to_listing, which keeps the page-side half small and lets the messy
    # half be unit-tested without a browser.
    extract_js = """
    (() => {
      const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      const seen = new Set();
      const out = [];
      document.querySelectorAll('li.s-card, li.s-item').forEach((li) => {
        const a = li.querySelector('a.s-card__link, a.s-item__link, a[href*="/itm/"]');
        if (!a) return;
        const m = (a.getAttribute('href') || '').match(/\\/itm\\/(\\d+)/);
        if (!m) return;
        const id = m[1];
        if (seen.has(id)) return;
        seen.add(id);
        const price = li.querySelector('.s-card__price, .s-item__price');
        const title = li.querySelector('.s-card__title, .s-item__title');
        const img = li.querySelector('img.s-card__image, img.s-item__image-img, img');
        out.push({
          id: id,
          title: clean(title && title.innerText),
          price: clean(price && price.innerText),
          img: (img && (img.getAttribute('src') || img.getAttribute('data-src'))) || '',
          subtitles: Array.from(
            li.querySelectorAll('.s-card__subtitle, .s-item__subtitle, .SECONDARY_INFO')
          ).map((e) => clean(e.innerText)),
          attrs: Array.from(
            li.querySelectorAll(
              '.s-card__attribute-row, .s-item__location, .s-item__itemLocation'
            )
          ).map((e) => clean(e.innerText)),
          // Newest-first results carry the listing date; relevance-sorted
          // ones often do not, and auctions show time-left here instead.
          // Collected raw and sorted out in Python.
          dates: Array.from(
            li.querySelectorAll(
              '.s-item__listingDate, .s-card__listingDate, .s-item__dynamic, .s-card__caption'
            )
          ).map((e) => clean(e.innerText)),
        });
      });
      return out;
    })()
    """

    def _host(self: "EbayBrowserMarketplace") -> str:
        config: EbayMarketplaceConfig = self.config  # type: ignore[assignment]
        return BROWSER_SEARCH_HOSTS.get(config.marketplace_id or "EBAY_US", DEFAULT_BROWSER_HOST)

    def search_url(
        self: "EbayBrowserMarketplace", phrase: str, item_config: BrowserItemConfig
    ) -> str:
        config: EbayMarketplaceConfig = self.config  # type: ignore[assignment]
        max_listings = item_config.max_listings or config.max_listings or DEFAULT_MAX_LISTINGS
        params: Dict[str, str] = {
            "_nkw": phrase,
            "_sop": SORT_NEWLY_LISTED,
            "_ipg": _items_per_page(max_listings),
        }
        if config.category:
            # eBay's own category filter. Far cheaper than letting the AI
            # reject 200 car parts one 25-second rating at a time.
            params["_sacat"] = config.category
        # Pushing the bounds into the URL is not just politeness: one page is
        # all we fetch, so an unfiltered page of 240 can be entirely out of
        # range. BrowserTileMarketplace still re-checks every price it gets
        # back, because eBay applies _udlo/_udhi to the item price and ignores
        # shipping.
        low = _numeric_price(item_config.min_price or config.min_price)
        high = _numeric_price(item_config.max_price or config.max_price)
        if low:
            params["_udlo"] = low
        if high:
            params["_udhi"] = high

        # `condition` lives on the Facebook config classes, so read it
        # defensively -- the item may have been built by another backend.
        conditions = getattr(item_config, "condition", None) or getattr(config, "condition", None)
        if conditions:
            ids = sorted(
                {
                    BROWSER_CONDITION_IDS[str(c).lower()]
                    for c in conditions
                    if str(c).lower() in BROWSER_CONDITION_IDS
                }
            )
            if ids:
                # eBay takes several condition ids pipe-separated.
                params["LH_ItemCondition"] = "|".join(ids)

        return f"https://{self._host()}/sch/i.html?{urlencode(params)}"

    def tile_to_listing(self: "EbayBrowserMarketplace", tile: Dict[str, Any]) -> Listing | None:
        item_id = str(tile.get("id") or "").strip()
        if not item_id or item_id == HOUSE_AD_ITEM_ID:
            return None

        title = str(tile.get("title") or "").strip()
        title = _TITLE_SUFFIX.sub("", _TITLE_PREFIX.sub("", title)).strip()
        if not title or title.lower() == "shop on ebay":
            return None

        condition = ""
        for subtitle in tile.get("subtitles") or []:
            text = str(subtitle).strip()
            if text and _CONDITION_ROW.match(text):
                condition = text
                break

        location = ""
        for attr in tile.get("attrs") or []:
            match = _LOCATION_ROW.match(str(attr).strip())
            if match:
                location = match.group(1).strip()
                break

        # Only newest-first pages reliably render a date, so this is often
        # empty -- "unknown" is the honest answer, not a guess from the sort
        # position.
        listed_at: float | None = None
        listed_text = ""
        for raw in tile.get("dates") or []:
            text = re.sub(r"(?i)^listed\s+", "", str(raw).strip())
            stamp = _parse_tile_date(text)
            if stamp is not None:
                listed_at, listed_text = stamp, text
                break

        return Listing(
            marketplace="ebay",
            # Left empty to match every other backend: the item name is
            # attached later, and the cache key depends on this being uniform.
            name="",
            id=item_id,
            title=title,
            image=str(tile.get("img") or ""),
            price=str(tile.get("price") or "").strip(),
            # The href on the tile carries a page-sized tracking query string
            # that changes on every load; the canonical form is stable and is
            # what the cache and the de-duplicator key on.
            post_url=f"https://{self._host()}/itm/{item_id}",
            location=location,
            # Search tiles carry neither, and saying so beats inventing it.
            seller="",
            condition=condition,
            description="",
            listed_at=listed_at,
            listed_text=listed_text,
        )


class EbayMarketplace(Marketplace):
    display_name = MARKETPLACE_DISPLAY_NAMES[MarketPlace.EBAY.value]
    # The class-level worst case. Which of the two backends actually runs is a
    # per-instance question -- see needs_browser().
    requires_browser = True
    # Browse searches the whole catalogue and the search page ships nationwide;
    # location is a delivery filter, not an origin, so demanding a search_city
    # here would be meaningless in either mode.
    requires_search_city = False

    def __init__(
        self: "EbayMarketplace",
        name: str,
        browser: Any = None,
        keyboard_monitor: Any = None,
        logger: Any = None,
    ) -> None:
        super().__init__(name, browser, keyboard_monitor, logger)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Built on first use in browser mode, so an API-mode user never pays
        # for a second Marketplace object.
        self._scraper: EbayBrowserMarketplace | None = None

    @classmethod
    def get_config(cls: Type["EbayMarketplace"], **kwargs: Any) -> EbayMarketplaceConfig:
        return EbayMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: Type["EbayMarketplace"], **kwargs: Any) -> EbayItemConfig:
        return EbayItemConfig(**kwargs)

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    @property
    def mode(self: "EbayMarketplace") -> str:
        config: EbayMarketplaceConfig | None = getattr(self, "config", None)
        # Unconfigured instances exist briefly between construction and
        # configure(); browser mode is the answer that needs no credentials.
        return "browser" if config is None else config.resolved_mode

    def needs_browser(self: "EbayMarketplace") -> bool:
        return self.mode == "browser"

    def _browser_backend(self: "EbayMarketplace") -> EbayBrowserMarketplace:
        if self._scraper is None:
            self._scraper = EbayBrowserMarketplace(
                self.name, self.browser, self.keyboard_monitor, self.logger
            )
        self._scraper.set_browser(self.browser)
        self._scraper.configure(self.config, self.translator)
        return self._scraper

    def set_browser(self: "EbayMarketplace", browser: Any = None) -> None:
        super().set_browser(browser)
        if self._scraper is not None:
            self._scraper.set_browser(browser)

    def stop(self: "EbayMarketplace") -> None:
        if self._scraper is not None:
            self._scraper.stop()
        super().stop()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _access_token(self: "EbayMarketplace") -> str:
        """Return a cached application token, fetching a new one when stale.

        Client-credentials tokens last two hours, so caching matters: minting
        one per search would spend a meaningful share of the daily call budget
        on authentication alone.
        """
        now = time.time()
        if self._token and now < self._token_expires_at - TOKEN_EXPIRY_MARGIN:
            return self._token

        config: EbayMarketplaceConfig = self.config  # type: ignore[assignment]
        if not config.client_id or not config.client_secret:
            raise ValueError(
                f'Marketplace {hilight(self.name)} is set to mode = "api", which needs '
                "client_id and client_secret from an application key set at "
                'https://developer.ebay.com. Set mode = "browser" to search without one.'
            )

        basic = base64.b64encode(f"{config.client_id}:{config.client_secret}".encode()).decode()
        response = requests.post(
            OAUTH_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": OAUTH_SCOPE},
            timeout=30,
        )
        if response.status_code != 200:
            # The body carries eBay's actual reason (bad key, wrong environment,
            # unaccepted agreement); a bare status code sends people hunting.
            raise ValueError(f"eBay OAuth failed ({response.status_code}): {response.text[:300]}")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = now + float(payload.get("expires_in", 7200))
        if self.logger:
            self.logger.debug(f"{hilight('[eBay]', 'succ')} Obtained application token.")
        assert self._token is not None
        return self._token

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _filters(self: "EbayMarketplace", item_config: EbayItemConfig) -> str:
        config: EbayMarketplaceConfig = self.config  # type: ignore[assignment]
        filters: List[str] = []

        low = _numeric_price(item_config.min_price or config.min_price)
        high = _numeric_price(item_config.max_price or config.max_price)
        if low and high:
            filters.append(f"price:[{low}..{high}]")
        elif low:
            filters.append(f"price:[{low}]")
        elif high:
            filters.append(f"price:[..{high}]")
        if low or high:
            # eBay rejects a price filter that does not say which currency.
            filters.append("priceCurrency:USD")

        # `condition` lives on the Facebook config classes, not the generic
        # base, and an item may have been built by either backend -- so read it
        # defensively rather than assuming the attribute exists.
        conditions = getattr(item_config, "condition", None) or getattr(config, "condition", None)
        if conditions:
            mapped = {CONDITION_ALIASES.get(str(c).lower(), str(c).upper()) for c in conditions}
            filters.append(f"conditions:{{{'|'.join(sorted(mapped))}}}")

        if config.buying_options:
            filters.append(f"buyingOptions:{{{'|'.join(config.buying_options)}}}")

        if config.delivery_country:
            filters.append(f"deliveryCountry:{config.delivery_country}")

        return ",".join(filters)

    def _to_listing(self: "EbayMarketplace", item: Dict[str, Any]) -> Listing | None:
        item_id = str(item.get("itemId") or "").strip()
        title = str(item.get("title") or "").strip()
        if not item_id or not title:
            return None

        price = item.get("price") or {}
        value = price.get("value")
        currency = price.get("currency") or "USD"
        price_text = f"${value}" if currency == "USD" and value else f"{value} {currency}".strip()

        location = item.get("itemLocation") or {}
        city = location.get("city") or ""
        region = location.get("stateOrProvince") or ""
        # "Asheboro, NC" -- the shape the activity view's geocoder expects.
        where = ", ".join([p for p in (city, region) if p])

        seller = item.get("seller") or {}
        image = (item.get("image") or {}).get("imageUrl") or ""

        return Listing(
            marketplace="ebay",
            # Left empty to match the Facebook backend: the item name is
            # attached later, and the cache key depends on this being uniform.
            name="",
            id=item_id,
            title=title,
            image=image,
            price=price_text,
            post_url=str(item.get("itemWebUrl") or ""),
            location=where,
            seller=str(seller.get("username") or ""),
            condition=str(item.get("condition") or ""),
            description=str(item.get("shortDescription") or ""),
            # Browse returns the seller's posting time outright -- an exact
            # ISO timestamp, no relative wording to unpick.
            listed_at=_parse_item_creation_date(item.get("itemCreationDate")),
        )

    def search(
        self: "EbayMarketplace", item_config: EbayItemConfig
    ) -> Generator[Listing, None, None]:
        if self.mode == "browser":
            if self.browser is None:
                # Only reachable if a caller decided no browser was needed;
                # never take the monitor loop down over it.
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[Search]", "fail")} eBay is in browser mode but no browser """
                        """is running; skipping this pass."""
                    )
                return
            yield from self._browser_backend().search(item_config)
            return

        yield from self._search_api(item_config)

    def _search_api(
        self: "EbayMarketplace", item_config: EbayItemConfig
    ) -> Generator[Listing, None, None]:
        config: EbayMarketplaceConfig = self.config  # type: ignore[assignment]
        try:
            token = self._access_token()
        except ValueError as e:
            # Missing/invalid credentials must not take the monitor loop down;
            # an api-mode section without keys logs and skips the pass.
            if self.logger:
                self.logger.error(f"""{hilight("[Search]", "fail")} {e}""")
            return
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": config.marketplace_id or "EBAY_US",
            "Accept": "application/json",
        }
        filters = self._filters(item_config)
        # An explicit cap is honored here too -- Browse takes a `limit`, so
        # asking for fewer items is strictly cheaper than fetching 200 and
        # throwing most away. Left unset the API keeps its old full page: it
        # returns descriptions and sellers, so its ratings are worth more than
        # a bare tile's, and it is not what burned five hours.
        max_listings = item_config.max_listings or config.max_listings

        for phrase in item_config.search_phrases or []:
            params: Dict[str, Any] = {
                "q": phrase,
                "limit": min(max_listings, MAX_LIMIT) if max_listings else MAX_LIMIT,
                # Newest first: a monitor cares about what appeared since the
                # last pass, not about eBay's relevance ranking.
                "sort": "newlyListed",
            }
            if config.category:
                params["category_ids"] = config.category
            if filters:
                params["filter"] = filters

            if self.logger:
                self.logger.info(
                    f"""{hilight("[Search]", "info")} Searching eBay for {hilight(phrase)}"""
                )
            try:
                response = requests.get(
                    BROWSE_SEARCH_URL, headers=headers, params=params, timeout=45
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[Search]", "fail")} eBay request failed: {e}"""
                    )
                continue

            if response.status_code == 429:
                # The daily cap is application-wide; burning through it just
                # produces errors, so stop rather than hammer.
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[Search]", "fail")} eBay rate limit reached; skipping remaining phrases."""
                    )
                return
            if response.status_code != 200:
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[Search]", "fail")} eBay search failed """
                        f"""({response.status_code}): {response.text[:300]}"""
                    )
                continue

            summaries = response.json().get("itemSummaries") or []
            counter.increment(CounterItem.SEARCH_PERFORMED, item_config.name)
            yielded = 0
            for summary in summaries:
                listing = self._to_listing(summary)
                if listing is None:
                    continue
                counter.increment(CounterItem.LISTING_EXAMINED, item_config.name)
                yield listing
                yielded += 1
                if max_listings and yielded >= max_listings:
                    break
