import os
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from logging import Logger
from typing import Any, Callable, Dict, Generator, Generic, List, Tuple, Type, TypeVar

from playwright.sync_api import Browser, ElementHandle, Locator, Page  # type: ignore

from .listing import Listing
from .utils import (
    BaseConfig,
    Currency,
    KeyboardMonitor,
    MonitorConfig,
    Translator,
    browser_state_file,
    convert_to_seconds,
    hilight,
)

# Randomized pause between two page loads against the same site, in seconds.
# Two independent sources converge on a 5-15s uniform range for Facebook:
#   * kevinzg/facebook-scraper issue #548 uses `time.sleep(randint(5, 15))`
#     between requests (https://github.com/kevinzg/facebook-scraper/issues/548)
#   * the 2026 "Scrape Facebook Public Data Without the Graph API" writeup
#     defaults its rate limiter to `delay_range=(5, 15)` and sleeps
#     `random.uniform(*delay_range)` before every request.
# The floor is raised to 6s here because the flat 5s this replaced was itself
# enough to earn a temporary block on a busy config.
DEFAULT_REQUEST_DELAY: Tuple[int, int] = (6, 15)

# How long to stop touching a marketplace after it signals a block. The only
# concrete number the ecosystem offers is "at least an hour" before retrying a
# temporarily-banned Facebook account (kevinzg/facebook-scraper issue #390);
# 2h doubles that, since a monitor has no deadline and a second strike costs
# far more than a late listing.
DEFAULT_BLOCK_COOLDOWN: int = 2 * 60 * 60


class MarketPlace(Enum):
    FACEBOOK = "facebook"
    EBAY = "ebay"
    DEPOP = "depop"
    POSHMARK = "poshmark"


class MarketplaceBlockedError(RuntimeError):
    """Raised when a marketplace answers with a block / rate-limit page.

    Carries the human-readable signal that triggered it so the log line and
    the notification can say *why* the monitor backed off.
    """

    def __init__(self: "MarketplaceBlockedError", marketplace: str, reason: str) -> None:
        super().__init__(f"{marketplace} is blocking requests: {reason}")
        self.marketplace = marketplace
        self.reason = reason


@dataclass
class BlockState:
    """A marketplace that is currently sitting out a cooldown.

    Timestamps are plain unix floats so this serializes to the web UI without
    any timezone guessing on either side.
    """

    marketplace: str
    reason: str
    detected_at: float
    until: float
    # How many blocks in a row have been seen without an intervening clear.
    strikes: int = 1

    def remaining(self: "BlockState", now: float | None = None) -> float:
        """Seconds left on the cooldown, never negative."""
        return max(0.0, self.until - (time.time() if now is None else now))

    def is_active(self: "BlockState", now: float | None = None) -> bool:
        return self.remaining(now) > 0

    def as_dict(self: "BlockState", now: float | None = None) -> Dict[str, Any]:
        return {
            "marketplace": self.marketplace,
            "reason": self.reason,
            "detected_at": self.detected_at,
            "until": self.until,
            "remaining": self.remaining(now),
            "strikes": self.strikes,
        }


def block_cooldown_for(base_cooldown: int, strikes: int, max_multiplier: int = 4) -> int:
    """Cooldown length for the `strikes`-th consecutive block.

    Doubles per repeat and then flattens: a site that keeps saying no after a
    two-hour wait is not going to relent in another two, but an unbounded
    backoff would silently retire the marketplace.
    """
    if strikes < 1:
        strikes = 1
    return int(base_cooldown * min(2 ** (strikes - 1), max_multiplier))


class BlockTracker:
    """Per-marketplace block state, shared between the monitor and web threads.

    Every mutation replaces a whole ``BlockState``; the lock only keeps the
    dict itself consistent for the web thread's snapshot.
    """

    def __init__(self: "BlockTracker") -> None:
        self._states: Dict[str, BlockState] = {}
        self._lock = threading.Lock()

    def block(
        self: "BlockTracker",
        marketplace: str,
        reason: str,
        base_cooldown: int = DEFAULT_BLOCK_COOLDOWN,
        now: float | None = None,
    ) -> BlockState:
        """Record a block and start (or escalate) its cooldown."""
        now = time.time() if now is None else now
        with self._lock:
            previous = self._states.get(marketplace)
            strikes = previous.strikes + 1 if previous is not None else 1
            state = BlockState(
                marketplace=marketplace,
                reason=reason,
                detected_at=now,
                until=now + block_cooldown_for(base_cooldown, strikes),
                strikes=strikes,
            )
            self._states[marketplace] = state
        return state

    def active(
        self: "BlockTracker", marketplace: str, now: float | None = None
    ) -> BlockState | None:
        """The live block for a marketplace, or None once the cooldown lapses.

        An elapsed cooldown is kept, not dropped: the strike count is what
        makes a second block back off further than the first.
        """
        with self._lock:
            state = self._states.get(marketplace)
        return state if state is not None and state.is_active(now) else None

    def clear(self: "BlockTracker", marketplace: str | None = None) -> List[str]:
        """Forget block state. Returns the marketplace names actually cleared."""
        with self._lock:
            if marketplace is None:
                cleared = sorted(self._states)
                self._states.clear()
            else:
                cleared = [marketplace] if marketplace in self._states else []
                self._states.pop(marketplace, None)
        return cleared

    def to_dict(self: "BlockTracker") -> Dict[str, Dict[str, Any]]:
        """Every state, expired ones included, for writing to disk."""
        with self._lock:
            states = list(self._states.values())
        return {s.marketplace: s.as_dict() for s in states}

    def restore(self: "BlockTracker", data: Dict[str, Any] | None) -> None:
        """Reload state written by ``to_dict``, ignoring anything malformed.

        A cooldown that expired while the process was down restores as an
        inactive state: it no longer blocks searches, but its strike count
        still makes the next block back off further.
        """
        if not isinstance(data, dict):
            return
        for name, raw in data.items():
            if not isinstance(raw, dict):
                continue
            try:
                state = BlockState(
                    marketplace=str(raw.get("marketplace") or name),
                    reason=str(raw.get("reason") or "unknown"),
                    detected_at=float(raw.get("detected_at") or 0.0),
                    until=float(raw.get("until") or 0.0),
                    strikes=int(raw.get("strikes") or 1),
                )
            except KeyboardInterrupt:
                raise
            except (TypeError, ValueError):
                continue
            with self._lock:
                self._states[state.marketplace] = state

    def snapshot(self: "BlockTracker", now: float | None = None) -> Dict[str, Dict[str, Any]]:
        """Only the still-active blocks, ready for JSON."""
        with self._lock:
            states = list(self._states.values())
        return {s.marketplace: s.as_dict(now) for s in states if s.is_active(now)}


@dataclass
class MarketItemCommonConfig(BaseConfig):
    """Item options that can be specified in market (non-marketplace specifc)

    This class defines and processes options that can be specified
    in both marketplace and item sections, generic to all marketplaces
    """

    ai: List[str] | None = None
    exclude_sellers: List[str] | None = None
    notify: List[str] | None = None
    search_city: List[str] | None = None
    city_name: List[str] | None = None
    # radius must be processed after search_city
    radius: List[int] | None = None
    currency: List[str] | None = None
    search_interval: int | None = None
    max_search_interval: int | None = None
    start_at: List[str] | None = None
    # [min, max] seconds to pause between two page loads. Randomized per
    # request; see DEFAULT_REQUEST_DELAY for where the range comes from.
    request_delay: List[int] | None = None
    search_region: List[str] | None = None
    max_price: str | None = None
    min_price: str | None = None
    rating: List[int] | None = None
    prompt: str | None = None
    extra_prompt: str | None = None
    rating_prompt: str | None = None

    def handle_ai(self: "MarketItemCommonConfig") -> None:
        if self.ai is None:
            return

        if isinstance(self.ai, str):
            self.ai = [self.ai]
        if not all(isinstance(x, str) for x in self.ai):
            raise ValueError(f"Item {hilight(self.name)} ai must be a string or list.")

    def handle_exclude_sellers(self: "MarketItemCommonConfig") -> None:
        if self.exclude_sellers is None:
            return

        if isinstance(self.exclude_sellers, str):
            self.exclude_sellers = [self.exclude_sellers]
        if not isinstance(self.exclude_sellers, list) or not all(
            isinstance(x, str) for x in self.exclude_sellers
        ):
            raise ValueError(f"Item {hilight(self.name)} exclude_sellers must be a list.")

    def handle_max_search_interval(self: "MarketItemCommonConfig") -> None:
        if self.max_search_interval is None:
            return

        if isinstance(self.max_search_interval, str):
            try:
                self.max_search_interval = convert_to_seconds(self.max_search_interval)
            except Exception as e:
                raise ValueError(
                    f"Marketplace {self.name} max_search_interval {self.max_search_interval} is not recognized."
                ) from e
        if not isinstance(self.max_search_interval, int) or self.max_search_interval < 1:
            raise ValueError(
                f"Item {hilight(self.name)} max_search_interval must be at least 1 second."
            )

    def handle_notify(self: "MarketItemCommonConfig") -> None:
        if self.notify is None:
            return

        if isinstance(self.notify, str):
            self.notify = [self.notify]
        if not all(isinstance(x, str) for x in self.notify):
            raise ValueError(
                f"Item {hilight(self.name)} notify must be a string or list of string."
            )

    def handle_radius(self: "MarketItemCommonConfig") -> None:
        if self.radius is None:
            return

        if self.search_city is None:
            raise ValueError(
                f"Item {hilight(self.name)} radius must be None if search_city is None."
            )

        if isinstance(self.radius, int):
            self.radius = [self.radius]

        if not all(isinstance(x, int) for x in self.radius):
            raise ValueError(
                f"Item {hilight(self.name)} radius must be one or a list of integers."
            )

        if len(self.radius) != len(self.search_city):
            raise ValueError(
                f"Item {hilight(self.name)} radius must be the same length as search_city."
            )

    def handle_search_city(self: "MarketItemCommonConfig") -> None:
        if self.search_city is None:
            return

        if isinstance(self.search_city, str):
            self.search_city = [self.search_city]

        if not isinstance(self.search_city, list) or not all(
            isinstance(x, str) for x in self.search_city
        ):
            raise ValueError(
                f"Item {hilight(self.name)} search_city must be a string or list of string."
            )

        # Validate format of each search_city entry
        for city in self.search_city:
            # Check if the city contains only lowercase letters and numbers
            if not city.replace("_", "").replace("-", "").isalnum() or any(
                c.isupper() for c in city
            ):
                # Provide helpful guidance on obtaining the correct format
                raise ValueError(
                    f"Item {hilight(self.name)} search_city '{city}' has incorrect format.\n"
                    f"Expected: lowercase letters and numbers only (e.g., 'sanfrancisco', 'newyork', 'toronto').\n"
                    f"To get the correct value:\n"
                    f"  1. Visit Facebook Marketplace\n"
                    f"  2. Perform a search in your desired location\n"
                    f"  3. Look at the URL: https://www.facebook.com/marketplace/XXXXX/search?query=...\n"
                    f"  4. Use the XXXXX value (the text after 'marketplace/') as your search_city\n"
                    f"Example: If URL is https://www.facebook.com/marketplace/sanfrancisco/search?query=item\n"
                    f"         Then search_city = 'sanfrancisco'"
                )

    def handle_city_name(self: "MarketItemCommonConfig") -> None:
        if self.city_name is None:
            if self.search_city is None:
                return
            self.city_name = [x.capitalize() for x in self.search_city]
            return

        if self.search_city is None:
            raise ValueError(
                f"Item {hilight(self.name)} city_name must be None if search_city is None."
            )
        if isinstance(self.city_name, str):
            self.city_name = [self.city_name]
        # check if city_name is a list of strings
        if not isinstance(self.city_name, list) or not all(
            isinstance(x, str) for x in self.city_name
        ):
            raise ValueError(f"Region {self.name} city_name must be a list of strings.")

        if len(self.city_name) != len(self.search_city):
            raise ValueError(
                f"Region {self.name} city_name ({self.city_name}) must be the same length as search_city ({self.search_city})."
            )

    def handle_currency(self: "MarketItemCommonConfig") -> None:
        if self.currency is None:
            return

        if self.search_city is None:
            raise ValueError(
                f"Item {hilight(self.name)} currency must be None if search_city is None."
            )

        if isinstance(self.currency, str):
            self.currency = [self.currency] * len(self.search_city)

        if not all(isinstance(x, str) for x in self.currency):
            raise ValueError(
                f"Item {hilight(self.name)} currency must be one or a list of strings."
            )

        for currency in self.currency:
            try:
                Currency(currency)
            except ValueError as e:
                raise ValueError(
                    f"Item {hilight(self.name)} currency {currency} is not recognized."
                ) from e

        if len(self.currency) != len(self.search_city):
            raise ValueError(
                f"Region {self.name} city_name ({self.city_name}) must be the same length as search_city ({self.search_city})."
            )

    def handle_search_interval(self: "MarketItemCommonConfig") -> None:
        if self.search_interval is None:
            return

        if isinstance(self.search_interval, str):
            try:
                self.search_interval = convert_to_seconds(self.search_interval)
            except Exception as e:
                raise ValueError(
                    f"Marketplace {self.name} search_interval {self.search_interval} is not recognized."
                ) from e
        if not isinstance(self.search_interval, int) or self.search_interval < 1:
            raise ValueError(
                f"Item {hilight(self.name)} search_interval must be at least 1 second."
            )

    def handle_request_delay(self: "MarketItemCommonConfig") -> None:
        """Normalize request_delay to a [min, max] pair of whole seconds.

        Accepts a single number ("pause exactly this long"), a duration string
        ('10s'), or a two-element list of either.
        """
        if self.request_delay is None:
            return

        def as_seconds(value: Any) -> int:
            if isinstance(value, bool):
                raise ValueError("request_delay must be a number of seconds.")
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str):
                text = value.strip()
                if text.isdigit():
                    return int(text)
                # convert_to_seconds falls back to "now" (i.e. 0) for text it
                # cannot read, which would silently disable pacing entirely.
                seconds = convert_to_seconds(text)
                if seconds <= 0:
                    raise ValueError(f"{value!r} is not a duration.")
                return seconds
            raise ValueError("request_delay must be a number or duration string.")

        values = (
            self.request_delay
            if isinstance(self.request_delay, (list, tuple))
            else [self.request_delay]
        )
        try:
            seconds = [as_seconds(x) for x in values]
        except KeyboardInterrupt:
            raise
        except Exception as e:
            raise ValueError(
                f"Item {hilight(self.name)} request_delay {self.request_delay} is not recognized."
            ) from e

        if len(seconds) == 1:
            seconds = seconds * 2
        if len(seconds) != 2:
            raise ValueError(
                f"Item {hilight(self.name)} request_delay must be one or two values, "
                "e.g. request_delay = [6, 15]."
            )
        if any(x < 0 for x in seconds):
            raise ValueError(f"Item {hilight(self.name)} request_delay must not be negative.")
        if seconds[0] > seconds[1]:
            raise ValueError(
                f"Item {hilight(self.name)} request_delay minimum {seconds[0]} is larger "
                f"than its maximum {seconds[1]}."
            )
        self.request_delay = seconds

    def handle_search_region(self: "MarketItemCommonConfig") -> None:
        if self.search_region is None:
            return

        if isinstance(self.search_region, str):
            self.search_region = [self.search_region]

        if not isinstance(self.search_region, list) or not all(
            isinstance(x, str) for x in self.search_region
        ):
            raise ValueError(
                f"Item {hilight(self.name)} search_region must be one or a list of string."
            )

    def handle_max_price(self: "MarketItemCommonConfig") -> None:
        if self.max_price is None:
            return

        if isinstance(self.max_price, int):
            self.max_price = str(self.max_price)

        # the price should be a number followed by currency name (e.g. 100 USD)
        if not isinstance(self.max_price, str):
            raise ValueError(f"Item {hilight(self.name)} max_price must be a string.")

        if " " in self.max_price:
            price, currency = self.max_price.split(" ", 1)
            if not price.isdigit():
                raise ValueError(
                    f"Item {hilight(self.name)} max_price must be a number followed by currency name."
                )
            try:
                Currency(currency)
            except ValueError as e:
                raise ValueError(
                    f"Item {hilight(self.name)} max_price currency {currency} is not recognized."
                ) from e
        elif not self.max_price.isdigit():
            raise ValueError(
                f"Item {hilight(self.name)} max_price must be a number followed by currency name."
            )

    def handle_min_price(self: "MarketItemCommonConfig") -> None:
        if self.min_price is None:
            return

        if isinstance(self.min_price, int):
            self.min_price = str(self.min_price)

        # the price should be a number followed by currency name (e.g. 100 USD)
        if not isinstance(self.min_price, str):
            raise ValueError(f"Item {hilight(self.name)} min_price must be a string.")

        if " " in self.min_price:
            price, currency = self.min_price.split(" ", 1)
            if not price.isdigit():
                raise ValueError(
                    f"Item {hilight(self.name)} min_price must be a number followed by currency name."
                )
            try:
                Currency(currency)
            except ValueError as e:
                raise ValueError(
                    f"Item {hilight(self.name)} min_price currency {currency} is not recognized."
                ) from e
        elif not self.min_price.isdigit():
            raise ValueError(
                f"Item {hilight(self.name)} min_price must be a number followed by currency name."
            )

    def handle_start_at(self: "MarketItemCommonConfig") -> None:
        if self.start_at is None:
            return

        if isinstance(self.start_at, str):
            self.start_at = [self.start_at]

        if not isinstance(self.start_at, list) or not all(
            isinstance(x, str) for x in self.start_at
        ):
            raise ValueError(
                f"Item {hilight(self.name)} start_at must be a string or list of string."
            )

        # start_at should be in one of the format of
        # HH:MM:SS, HH:MM, *:MM:SS, or *:MM, or *:*:SS
        # where HH, MM, SS are hour, minutes and seconds
        # and * can be any number
        # if not, raise ValueError
        for val in self.start_at:
            if (
                val.count(":") not in (1, 2)
                or val.count("*") == 3
                or not all(x == "*" or (x.isdigit() and len(x) == 2) for x in val.split(":"))
            ):
                raise ValueError(f"Item {hilight(self.name)} start_at {val} is not recognized.")
            #
            acceptable = False
            for pattern in ["%H:%M:%S", "%H:%M", "*:%M:%S", "*:%M", "*:*:%S"]:
                try:
                    time.strptime(val, pattern)
                    acceptable = True
                    break
                except ValueError:
                    pass
            if not acceptable:
                raise ValueError(f"Item {hilight(self.name)} start_at {val} is not recognized.")

    def handle_rating(self: "MarketItemCommonConfig") -> None:
        if self.rating is None:
            return
        if isinstance(self.rating, int):
            self.rating = [self.rating]

        if not all(isinstance(x, int) and x >= 1 and x <= 5 for x in self.rating):
            raise ValueError(
                f"Item {hilight(self.name)} rating must be one or a list of integers between 1 and 5 inclusive."
            )

    def handle_prompt(self: "MarketItemCommonConfig") -> None:
        if self.prompt is None:
            return
        if not isinstance(self.prompt, str):
            raise ValueError(f"Item {hilight(self.name)} requires a string prompt, if specified.")

    def handle_extra_prompt(self: "MarketItemCommonConfig") -> None:
        if self.extra_prompt is None:
            return
        if not isinstance(self.extra_prompt, str):
            raise ValueError(
                f"Item {hilight(self.name)} requires a string extra_prompt, if specified."
            )

    def handle_rating_prompt(self: "MarketItemCommonConfig") -> None:
        if self.rating_prompt is None:
            return
        if not isinstance(self.rating_prompt, str):
            raise ValueError(
                f"Item {hilight(self.name)} requires a string rating_prompt, if specified."
            )


@dataclass
class MarketplaceConfig(MarketItemCommonConfig):
    """Generic marketplace config"""

    # name of market, right now facebook is the only supported one
    market_type: str | None = MarketPlace.FACEBOOK.value
    language: str | None = None
    monitor_config: MonitorConfig | None = None
    # Where "you" are, for the distance shown next to each listing in the web
    # UI's activity view. `search_city` cannot serve this: Facebook expects its
    # own city slug or a numeric place id, neither of which geocodes.
    # Accepts "City, ST" or a bare "lat, lon" pair.
    home_location: str | None = None
    # How long to stop searching this marketplace once it signals a block.
    # A human duration ('2h', '30m') or a number of seconds.
    block_cooldown: int | None = None

    def handle_block_cooldown(self: "MarketplaceConfig") -> None:
        if self.block_cooldown is None:
            return
        if isinstance(self.block_cooldown, str):
            try:
                self.block_cooldown = convert_to_seconds(self.block_cooldown)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                raise ValueError(
                    f"Marketplace {hilight(self.name)} block_cooldown "
                    f"{self.block_cooldown} is not recognized."
                ) from e
        if isinstance(self.block_cooldown, bool) or not isinstance(self.block_cooldown, int):
            raise ValueError(
                f"Marketplace {hilight(self.name)} block_cooldown must be a duration."
            )
        if self.block_cooldown < 60:
            raise ValueError(
                f"Marketplace {hilight(self.name)} block_cooldown must be at least 1 minute."
            )

    def handle_market_type(self: "MarketplaceConfig") -> None:
        if self.market_type is None:
            return
        if not isinstance(self.market_type, str):
            raise ValueError(f"Marketplace {hilight(self.market_type)} market must be a string.")
        if self.market_type.lower() != MarketPlace.FACEBOOK.value:
            raise ValueError(
                f"Marketplace {hilight(self.market_type)} market must be {MarketPlace.FACEBOOK.value}."
            )

    def handle_home_location(self: "MarketplaceConfig") -> None:
        if self.home_location is None:
            return
        if not isinstance(self.home_location, str) or not self.home_location.strip():
            raise ValueError(
                f"Marketplace {hilight(self.name)} home_location must be a non-empty string."
            )
        self.home_location = self.home_location.strip()

    def handle_language(self: "MarketplaceConfig") -> None:
        if self.language is None:
            return
        if not isinstance(self.language, str):
            raise ValueError(
                f"Marketplace {hilight(self.market_type)} language, if specified, must be a string."
            )


@dataclass
class ItemConfig(MarketItemCommonConfig):
    """This class defined options that can only be specified for items."""

    # the number of times that this item has been searched
    searched_count: int = 0

    # keywords is required, all others are optional
    search_phrases: List[str] = field(default_factory=list)
    keywords: List[str] | None = None
    antikeywords: List[str] | None = None
    description: str | None = None
    # Which sources to search this item on. A bare string is still accepted --
    # every config written before multi-source support used one -- and is
    # normalized to a list. None means "every enabled marketplace", which stays
    # the default so existing configs behave exactly as they did.
    marketplace: List[str] | None = None

    def handle_marketplace(self: "ItemConfig") -> None:
        if self.marketplace is None:
            return
        if isinstance(self.marketplace, str):
            self.marketplace = [self.marketplace]
        if not isinstance(self.marketplace, list) or not all(
            isinstance(x, str) for x in self.marketplace
        ):
            raise ValueError(
                f"Item {hilight(self.name)} marketplace must be a name or list of names."
            )
        if not self.marketplace:
            raise ValueError(
                f"Item {hilight(self.name)} marketplace list is empty. Remove the key to search "
                "every marketplace, or name at least one."
            )

    def searches_on(self: "ItemConfig", marketplace_name: str) -> bool:
        """True when this item should be searched on the named marketplace."""
        return self.marketplace is None or marketplace_name in self.marketplace

    def handle_search_phrases(self: "ItemConfig") -> None:
        if isinstance(self.search_phrases, str):
            self.search_phrases = [self.search_phrases]

        if not isinstance(self.search_phrases, list) or not all(
            isinstance(x, str) for x in self.search_phrases
        ):
            raise ValueError(f"Item {hilight(self.name)} search_phrases must be a list.")
        if len(self.search_phrases) == 0:
            raise ValueError(f"Item {hilight(self.name)} search_phrases list is empty.")

    def handle_antikeywords(self: "ItemConfig") -> None:
        if self.antikeywords is None:
            return

        if isinstance(self.antikeywords, str):
            self.antikeywords = [self.antikeywords]

        if not isinstance(self.antikeywords, list) or not all(
            isinstance(x, str) for x in self.antikeywords
        ):
            raise ValueError(f"Item {hilight(self.name)} antikeywords must be a list of strings.")

    def handle_keywords(self: "ItemConfig") -> None:
        if self.keywords is None:
            return

        if isinstance(self.keywords, str):
            self.keywords = [self.keywords]

        if not isinstance(self.keywords, list) or not all(
            isinstance(x, str) for x in self.keywords
        ):
            raise ValueError(f"Item {hilight(self.name)} keywords must be a list.")

    def handle_description(self: "ItemConfig") -> None:
        if self.description is None:
            return
        if not isinstance(self.description, str):
            raise ValueError(f"Item {hilight(self.name)} description must be a string.")


TMarketplaceConfig = TypeVar("TMarketplaceConfig", bound=MarketplaceConfig)
TItemConfig = TypeVar("TItemConfig", bound=ItemConfig)


class Marketplace(Generic[TMarketplaceConfig, TItemConfig]):
    # Whether a search needs a geographic origin. True for site-scraping
    # backends like Facebook, whose search URL is built around a city; false
    # for API backends that search a whole catalogue.
    requires_search_city = True
    # Whether this backend can drive a Playwright browser at all. This is the
    # class-level *maximum*: a backend whose configuration decides (eBay, whose
    # `mode` picks between the REST API and a scrape) leaves this True and
    # narrows it per instance in needs_browser().
    requires_browser = True

    def __init__(
        self: "Marketplace",
        name: str,
        browser: Browser | None,
        keyboard_monitor: KeyboardMonitor | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.name = name
        self.browser = browser
        self.keyboard_monitor = keyboard_monitor
        self.translator = Translator()
        self.logger = logger
        self.page: Page | None = None

    @classmethod
    def get_config(cls: Type["Marketplace"], **kwargs: Any) -> TMarketplaceConfig:
        raise NotImplementedError("get_config method must be implemented by subclasses.")

    @classmethod
    def get_item_config(cls: Type["Marketplace"], **kwargs: Any) -> TItemConfig:
        raise NotImplementedError("get_config method must be implemented by subclasses.")

    def needs_browser(self: "Marketplace") -> bool:
        """Whether THIS configured instance needs a browser.

        Callers deciding whether to launch Playwright must ask the instance,
        not the class: eBay answers True or False depending on its `mode`, and
        the class attribute can only state the worst case.
        """
        return self.requires_browser

    def configure(
        self: "Marketplace", config: TMarketplaceConfig, translator: Translator | None = None
    ) -> None:
        self.config = config
        if translator is not None:
            self.translator = translator

    def set_browser(self: "Marketplace", browser: Browser | None = None) -> None:
        if browser is not None:
            self.browser = browser
            self.page = None

    def stop(self: "Marketplace") -> None:
        if self.browser is not None:
            # stop closing the browser since Ctrl-C will kill playwright,
            # leaving browser in a dysfunctional status.
            # see
            #   https://github.com/microsoft/playwright-python/issues/1170
            # for details.
            # self.browser.close()
            self.browser = None
            self.page = None

    def create_page(self: "Marketplace", swap_proxy: bool = False) -> Page:
        assert self.browser is not None

        # if there is an existing page, asked to swap_proxy, and there is an proxy_server
        # setting with multiple proxies
        if (
            self.page
            and swap_proxy
            and self.config.monitor_config is not None
            and isinstance(self.config.monitor_config.proxy_server, list)
            and len(self.config.monitor_config.proxy_server) > 1
        ):
            self.page.close()
            self.page = None

        if self.page is None:
            # A persistent profile arrives as a BrowserContext (no new_context
            # attribute): pages open directly in it and every kind of state
            # persists on disk, so the storage_state restore below is only for
            # the ephemeral-Browser path. Proxy swapping needs per-context
            # proxies, which a persistent profile cannot do -- ignored there.
            if not hasattr(self.browser, "new_context"):
                self.page = self.browser.new_page()
                return self.page
            proxy = (
                None
                if self.config.monitor_config is None
                else self.config.monitor_config.get_proxy_options()
            )
            # Restore the saved session so a restart does not land back on the
            # login page. A state file written by an older Playwright, or one
            # truncated by a hard kill, makes new_context raise -- fall back to
            # a blank context rather than refusing to start.
            state = str(browser_state_file) if browser_state_file.exists() else None
            try:
                context = self.browser.new_context(storage_state=state, proxy=proxy)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if state is None:
                    raise
                if self.logger:
                    self.logger.warning(
                        f"""{hilight("[Login]", "fail")} Ignoring unreadable browser state: {e!s}"""
                    )
                context = self.browser.new_context(proxy=proxy)
            self.page = context.new_page()
        return self.page

    def save_browser_state(self: "Marketplace") -> None:
        """Persist cookies and localStorage so the next run starts logged in."""
        if self.page is None:
            return
        try:
            # Write then chmod, not the reverse: storage_state creates the file
            # itself, so tightening it beforehand would be undone.
            self.page.context.storage_state(path=str(browser_state_file))
            os.chmod(browser_state_file, 0o600)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # A missing session only costs a re-login; never fail a search over it.
            if self.logger:
                self.logger.debug(f"Could not save browser state: {e!s}")

    def goto_url(self: "Marketplace", url: str, attempt: int = 0) -> None:
        try:
            assert self.page is not None
            if self.logger:
                self.logger.debug(f"{hilight('[Retrieve]', 'info')} Navigating to {url}")
            self.page.goto(url, timeout=0)
            self.page.wait_for_load_state("domcontentloaded")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if attempt == 10:
                raise RuntimeError(f"Failed to navigate to {url} after 10 attempts. {e}") from e
            time.sleep(5)
            self.goto_url(url, attempt + 1)

    def request_delay_range(
        self: "Marketplace", item_config: TItemConfig | None = None
    ) -> Tuple[int, int]:
        """The [min, max] pause to use between page loads, item first."""
        delay = getattr(item_config, "request_delay", None) or getattr(
            getattr(self, "config", None), "request_delay", None
        )
        if not delay:
            return DEFAULT_REQUEST_DELAY
        return int(delay[0]), int(delay[-1])

    def pace(
        self: "Marketplace", item_config: TItemConfig | None = None, reason: str = ""
    ) -> float:
        """Sleep a random interval before the next request. Returns the delay.

        Uniform rather than fixed on purpose: a metronome is the easiest
        automation signature there is, and the delay this replaced was a flat
        five seconds on every page.
        """
        low, high = self.request_delay_range(item_config)
        if high <= 0:
            return 0.0
        delay = random.uniform(low, high)
        if self.logger:
            self.logger.debug(
                f"""{hilight("[Pace]", "info")} Waiting {delay:.1f}s before the next """
                f"""{self.name} request{f" ({reason})" if reason else ""}."""
            )
        time.sleep(delay)
        return delay

    def search(self: "Marketplace", item: TItemConfig) -> Generator[Listing, None, None]:
        raise NotImplementedError("Search method must be implemented by subclasses.")


class WebPage:
    def __init__(
        self: "WebPage",
        page: Page,
        translator: Translator | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.page = page
        self.translator: Translator = Translator() if translator is None else translator
        self.logger = logger

    def _parent_with_cond(
        self: "WebPage",
        element: Locator | ElementHandle | None,
        cond: Callable,
        ret: Callable | int,
    ) -> str:
        """Finding a parent element

        Starting from `element`, finding its parents, until `cond` matches, then return the `ret`th children,
        or a callable.
        """
        if element is None:
            return ""
        # get up at the DOM level, testing the children elements with cond,
        # apply the res callable to return a string
        parent: ElementHandle | None = (
            element.element_handle() if isinstance(element, Locator) else element
        )
        # look for parent of approximate_element until it has two children and the first child is the heading
        while parent:
            children = parent.query_selector_all(":scope > *")
            if cond(children):
                if isinstance(ret, int):
                    return children[ret].text_content() or self.translator("**unspecified**")
                else:
                    return ret(children)
            parent = parent.query_selector("xpath=..")
        raise ValueError("Could not find parent element with condition.")

    def _children_with_cond(
        self: "WebPage",
        element: Locator | ElementHandle | None,
        cond: Callable,
        ret: Callable | int,
    ) -> str:
        if element is None:
            return ""
        # Getting the children of an element, test condition, return the `index` or apply res
        # on the children element if the condition is met. Otherwise locate the first child and repeat the process.
        child: ElementHandle | None = (
            element.element_handle() if isinstance(element, Locator) else element
        )
        # look for parent of approximate_element until it has two children and the first child is the heading
        while child:
            children = child.query_selector_all(":scope > *")
            if cond(children):
                if isinstance(ret, int):
                    return children[ret].text_content() or self.translator("**unspecified**")
                return ret(children)
            if not children:
                raise ValueError("Could not find child element with condition.")
            # or we could use query_selector("./*[1]")
            child = children[0]
        raise ValueError("Could not find child element with condition.")
