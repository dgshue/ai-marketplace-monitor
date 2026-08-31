"""eBay marketplace backend, built on the official Browse API.

Deliberately not a scraper. eBay publishes a documented REST API for exactly
this, free, with OAuth2 client-credentials auth and roughly 5,000 calls/day at
the application level (raisable through eBay's free Application Growth Check).
Against that, browser automation would be slower, more fragile, and against
eBay's terms -- there is no upside.

That makes this the first backend needing no browser at all: `requires_browser`
is False and `search()` never touches `self.browser`. The Listing objects it
yields are indistinguishable from the Facebook ones, so AI evaluation, rating
thresholds, notification, caching and de-duplication all work unchanged.

Credentials come from https://developer.ebay.com (create an application key
set). Keep them out of config.toml with ${EBAY_CLIENT_ID} / ${EBAY_CLIENT_SECRET}.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Type

import requests  # type: ignore

from .facebook import FacebookMarketItemCommonConfig
from .listing import Listing
from .marketplace import ItemConfig, Marketplace, MarketplaceConfig
from .utils import CounterItem, counter, hilight

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
class EbayItemConfig(ItemConfig, FacebookMarketItemCommonConfig):
    """Accepts the Facebook-specific item keys as well as the generic ones.

    An item that names no marketplace is searched everywhere, and config.py
    builds exactly one item object for it -- whichever backend it iterates
    last. So an item carrying `condition` or `date_listed` would fail to
    construct here, and one built here would be missing attributes Facebook's
    search reads. Sharing the field set makes the object usable by either
    backend regardless of iteration order; eBay maps `condition` and ignores
    the rest."""


@dataclass
class EbayMarketplaceConfig(MarketplaceConfig):
    """eBay-specific options. Everything else is inherited and behaves as it
    does for Facebook -- search_interval, rating, notify, keywords, prices."""

    client_id: str | None = None
    client_secret: str | None = None
    # Which eBay site to search. EBAY_US, EBAY_GB, EBAY_DE, ...
    marketplace_id: str | None = None
    # Restrict to items that will actually ship to you.
    delivery_country: str | None = None
    buying_options: List[str] | None = None

    def handle_client_id(self: "EbayMarketplaceConfig") -> None:
        if self.client_id is None:
            return
        if not isinstance(self.client_id, str) or not self.client_id.strip():
            raise ValueError(f"Marketplace {hilight(self.name)} client_id must be a string.")
        self.client_id = self.client_id.strip()

    def handle_client_secret(self: "EbayMarketplaceConfig") -> None:
        if self.client_secret is None:
            return
        if not isinstance(self.client_secret, str) or not self.client_secret.strip():
            raise ValueError(f"Marketplace {hilight(self.name)} client_secret must be a string.")
        self.client_secret = self.client_secret.strip()

    def handle_marketplace_id(self: "EbayMarketplaceConfig") -> None:
        if self.marketplace_id is None:
            self.marketplace_id = "EBAY_US"
            return
        if not isinstance(self.marketplace_id, str) or not self.marketplace_id.startswith("EBAY_"):
            raise ValueError(
                f"Marketplace {hilight(self.name)} marketplace_id must look like 'EBAY_US'."
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


class EbayMarketplace(Marketplace):
    # No browser, no login, no 2FA. The whole reason to prefer the API.
    requires_browser = False
    # Browse searches the whole catalogue; location is a delivery filter, not
    # an origin, so demanding a search_city here would be meaningless.
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

    @classmethod
    def get_config(cls: Type["EbayMarketplace"], **kwargs: Any) -> EbayMarketplaceConfig:
        return EbayMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: Type["EbayMarketplace"], **kwargs: Any) -> EbayItemConfig:
        return EbayItemConfig(**kwargs)

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
                f"Marketplace {hilight(self.name)} needs client_id and client_secret. "
                "Create an application key set at https://developer.ebay.com."
            )

        basic = base64.b64encode(
            f"{config.client_id}:{config.client_secret}".encode()
        ).decode()
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
            raise ValueError(
                f"eBay OAuth failed ({response.status_code}): {response.text[:300]}"
            )
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
        )

    def search(
        self: "EbayMarketplace", item_config: EbayItemConfig
    ) -> Generator[Listing, None, None]:
        config: EbayMarketplaceConfig = self.config  # type: ignore[assignment]
        token = self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": config.marketplace_id or "EBAY_US",
            "Accept": "application/json",
        }
        filters = self._filters(item_config)

        for phrase in item_config.search_phrases or []:
            params: Dict[str, Any] = {
                "q": phrase,
                "limit": MAX_LIMIT,
                # Newest first: a monitor cares about what appeared since the
                # last pass, not about eBay's relevance ranking.
                "sort": "newlyListed",
            }
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
                    self.logger.error(f"""{hilight("[Search]", "fail")} eBay request failed: {e}""")
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
            for summary in summaries:
                listing = self._to_listing(summary)
                if listing is None:
                    continue
                counter.increment(CounterItem.LISTING_EXAMINED, item_config.name)
                yield listing
