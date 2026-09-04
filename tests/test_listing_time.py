"""Found vs listed: the two clocks a review row now carries, and canonical URLs.

`listed_at` is when the seller posted the listing, resolved from the page's
relative wording at scrape time so it cannot rot in the cache. `first_seen` is
when the monitor first wrote the listing's details down, and it must survive
every re-cache or an old listing would keep looking like a new find.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor.listing import Listing, canonical_url
from ai_marketplace_monitor.utils import (
    CacheType,
    Translator,
    parse_relative_time,
    relative_time_phrase,
)
from ai_marketplace_monitor.webui.activity import build_activity

# A fixed reference so "3 days ago" is an exact number of seconds rather than
# whatever the test runner's clock happens to say.
NOW = datetime.datetime(2026, 9, 3, 12, 0, 0).timestamp()

MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY


@pytest.fixture
def cache_dir(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    yield cache
    cache.close()


# ---------------------------------------------------------------------------
# The relative-time parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds_ago"),
    [
        # The shapes Facebook actually renders on a listing page.
        ("Listed 3 days ago in High Point, NC", 3 * DAY),
        ("Listed 2 hours ago", 2 * HOUR),
        ("Listed a week ago", WEEK),
        ("Listed an hour ago", HOUR),
        ("Listed one minute ago", MINUTE),
        ("Listed 45 minutes ago", 45 * MINUTE),
        ("Listed 30 seconds ago", 30.0),
        ("Listed over 2 weeks ago", 2 * WEEK),
        ("Listed about 5 minutes ago", 5 * MINUTE),
        ("Listed almost 3 days ago", 3 * DAY),
        ("Listed more than 6 hours ago", 6 * HOUR),
        # Boundaries: one of each unit, and the largest plausible count.
        ("1 minute ago", MINUTE),
        ("1 hour ago", HOUR),
        ("1 day ago", DAY),
        ("1 week ago", WEEK),
        ("59 minutes ago", 59 * MINUTE),
        ("24 hours ago", 24 * HOUR),
        # Abbreviations, as tiles write them.
        ("Listed 2 hrs ago", 2 * HOUR),
        ("Listed 10 mins ago", 10 * MINUTE),
        # No number at all.
        ("Listed just now", 0.0),
        ("Listed a few seconds ago", 0.0),
        ("Listed moments ago", 0.0),
        # Facebook duplicates the phrase into a visually hidden twin; the
        # parser searches rather than splits, so it reads the first one.
        ("Listed a week agoa week ago in High Point, NC", WEEK),
    ],
)
def test_relative_forms(text: str, seconds_ago: float) -> None:
    stamp = parse_relative_time(text, now=NOW)
    assert stamp is not None, text
    assert abs((NOW - stamp) - seconds_ago) < 1.5, text


def test_yesterday_lands_on_yesterday() -> None:
    """Date-only wording: the day is certain, the hour is not."""
    stamp = parse_relative_time("Listed yesterday", now=NOW)
    assert stamp is not None
    assert (
        datetime.datetime.fromtimestamp(stamp).date()
        == (datetime.datetime.fromtimestamp(NOW) - datetime.timedelta(days=1)).date()
    )


def test_months_and_years_use_real_calendar_lengths() -> None:
    """Delegated to parsedatetime, so February is 28 days and not 30."""
    stamp = parse_relative_time("Listed a month ago", now=NOW)
    assert stamp is not None
    assert datetime.datetime.fromtimestamp(stamp).month == 8
    stamp = parse_relative_time("Listed 2 years ago", now=NOW)
    assert stamp is not None
    assert datetime.datetime.fromtimestamp(stamp).year == 2024


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        # The Facebook layout that shows a location and no time at all.
        "Listed  in High Point, NC",
        # A location that reads like a date must not become one.
        "March, PA",
        "May, OK",
        # A rental blurb full of numbers and nouns.
        "2 bedrooms 1 bathroom",
        "3 days left",
        # Forward-looking text is a misread, never a listing time.
        "in 3 days",
        "Ends in 2 hours",
        # A unit with no quantity and no "ago".
        "Listed week",
        "Condition: Used - Good",
    ],
)
def test_unknown_is_none(text: str) -> None:
    assert parse_relative_time(text, now=NOW) is None
    assert relative_time_phrase(text) == ("", "")


def test_phrase_keeps_the_page_wording() -> None:
    """`listed_text` is what a human would have read, not our normalization."""
    assert relative_time_phrase("Listed over 2 weeks ago in Denver, CO")[1] == "over 2 weeks ago"
    assert relative_time_phrase("Listed a week ago")[1] == "a week ago"
    assert relative_time_phrase("Listed just now")[1] == "just now"


def test_translated_vocabulary() -> None:
    """A locale that declares its words in [translation.*] parses too."""
    spanish = Translator(
        "Spanish",
        {
            "day": "día",
            "days": "días",
            "hour": "hora",
            "hours": "horas",
            "ago": "hace",
            "a": "un",
            "yesterday": "ayer",
        },
    )
    stamp = parse_relative_time("Publicado hace 3 días en Madrid", now=NOW, translator=spanish)
    assert stamp is not None and abs((NOW - stamp) - 3 * DAY) < 1.5
    stamp = parse_relative_time("hace un día", now=NOW, translator=spanish)
    assert stamp is not None and abs((NOW - stamp) - DAY) < 1.5
    # Without the dictionary the same text is simply unknown -- never a guess.
    assert parse_relative_time("Publicado hace 3 días en Madrid", now=NOW) is None


# ---------------------------------------------------------------------------
# first_seen
# ---------------------------------------------------------------------------


def _listing(**overrides: Any) -> Listing:
    base: Dict[str, Any] = {
        "marketplace": "facebook",
        "name": "",
        "id": "1234567890",
        "title": "A thing",
        "image": "",
        "price": "$10",
        "post_url": "https://www.facebook.com/marketplace/item/1234567890/",
        "location": "Houston, TX",
        "seller": "someone",
        "condition": "Used",
        "description": "a thing",
    }
    base.update(overrides)
    return Listing(**base)  # type: ignore[arg-type]


def test_first_seen_is_stamped_on_the_first_write(cache_dir: Cache) -> None:
    before = time.time()
    listing = _listing()
    assert listing.first_seen is None
    listing.to_cache(listing.post_url, local_cache=cache_dir)
    assert listing.first_seen is not None
    assert before <= listing.first_seen <= time.time()
    assert Listing.from_cache(listing.post_url, local_cache=cache_dir).first_seen == (
        listing.first_seen
    )


def test_first_seen_survives_a_recache(cache_dir: Cache) -> None:
    """A price change, a title change or a photo backfill must not reset it."""
    original = _listing()
    original.to_cache(original.post_url, local_cache=cache_dir)
    first = original.first_seen
    assert first is not None

    # A fresh scrape of the same listing, with no idea it has been seen before.
    later = _listing(price="$8", first_seen=time.time() + 500)
    later.to_cache(later.post_url, local_cache=cache_dir)
    assert later.first_seen == first

    reloaded = Listing.from_cache(later.post_url, local_cache=cache_dir)
    assert reloaded is not None
    assert reloaded.first_seen == first
    assert reloaded.price == "$8"


def test_first_seen_ignores_junk_already_on_disk(cache_dir: Cache) -> None:
    listing = _listing()
    key = (CacheType.LISTING_DETAILS.value, listing.post_url)
    cache_dir.set(key, {"first_seen": "not a number"}, tag=CacheType.LISTING_DETAILS.value)
    listing.to_cache(listing.post_url, local_cache=cache_dir)
    assert isinstance(listing.first_seen, float)


def test_timestamps_stay_out_of_the_hash() -> None:
    """The hash joins a listing to its AI rating; new fields must not move it."""
    plain = _listing()
    stamped = _listing(first_seen=123.0, listed_at=456.0, listed_text="a week ago")
    assert plain.hash == stamped.hash


def test_cached_rows_without_the_new_keys_still_load(cache_dir: Cache) -> None:
    """Everything written before this feature has to keep rehydrating."""
    url = "https://www.facebook.com/marketplace/item/999/"
    cache_dir.set(
        (CacheType.LISTING_DETAILS.value, url),
        {
            "marketplace": "facebook",
            "name": "",
            "id": "999",
            "title": "Old row",
            "image": "",
            "price": "$1",
            "post_url": url,
            "location": "",
            "seller": "",
            "condition": "",
            "description": "",
        },
        tag=CacheType.LISTING_DETAILS.value,
    )
    listing = Listing.from_cache(url, local_cache=cache_dir)
    assert listing is not None
    assert listing.first_seen is None
    assert listing.listed_at is None
    assert listing.listed_text == ""


# ---------------------------------------------------------------------------
# Canonical URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marketplace", "url", "listing_id", "want"),
    [
        (
            "facebook",
            "https://www.facebook.com/marketplace/item/123/?ref=search&__tn__=!%3AD",
            "123",
            "https://www.facebook.com/marketplace/item/123/",
        ),
        (
            "facebook",
            "https://www.facebook.com/marketplace/item/123",
            "123",
            "https://www.facebook.com/marketplace/item/123/",
        ),
        (
            "ebay",
            "https://www.ebay.com/itm/4455?_trkparms=abc",
            "4455",
            "https://www.ebay.com/itm/4455",
        ),
        # A non-US site keeps its own domain.
        (
            "ebay",
            "https://www.ebay.co.uk/itm/4455?hash=x",
            "4455",
            "https://www.ebay.co.uk/itm/4455",
        ),
        # No usable host: fall back to the main site rather than inventing one.
        ("ebay", "", "4455", "https://www.ebay.com/itm/4455"),
        (
            "depop",
            "https://www.depop.com/products/some-slug?utm_source=share",
            "some-slug",
            "https://www.depop.com/products/some-slug",
        ),
        (
            "poshmark",
            "https://poshmark.com/listing/thing-123abc#tracking",
            "123abc",
            "https://poshmark.com/listing/thing-123abc",
        ),
        # An unknown marketplace, and a listing with no id, both degrade to
        # "strip the query" rather than to nothing.
        ("mystery", "https://example.com/x?y=1", "7", "https://example.com/x"),
        (
            "facebook",
            "https://www.facebook.com/marketplace/item/9/?ref=x",
            "",
            "https://www.facebook.com/marketplace/item/9/",
        ),
    ],
)
def test_canonical_url(marketplace: str, url: str, listing_id: str, want: str) -> None:
    assert canonical_url(marketplace, url, listing_id) == want


def test_canonical_url_property_matches_the_function() -> None:
    listing = _listing(post_url="https://www.facebook.com/marketplace/item/1234567890/?ref=x")
    assert listing.canonical_url == "https://www.facebook.com/marketplace/item/1234567890/"
    assert "__tn__" not in listing.canonical_url


# ---------------------------------------------------------------------------
# The activity rows the web UI renders
# ---------------------------------------------------------------------------


def _seed(cache: Cache, listing: Listing, score: int = 4, rated_at: float | None = None) -> None:
    listing.to_cache(listing.post_url, local_cache=cache)
    cache.set(
        (CacheType.AI_BY_LISTING.value, listing.marketplace, listing.id, "thing"),
        {"score": score, "comment": "ok", "name": "test", "rated_at": rated_at or time.time()},
        tag=CacheType.AI_BY_LISTING.value,
    )


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "[marketplace.facebook]\nsearch_city = 'dallas'\n\n"
        "[item.thing]\nsearch_phrases = 'thing'\n",
        encoding="utf-8",
    )
    return path


def test_rows_carry_both_clocks(cache_dir: Cache, tmp_path: Path) -> None:
    listed = NOW - 3 * DAY
    listing = _listing(listed_at=listed, listed_text="3 days ago")
    _seed(cache_dir, listing)

    rows = build_activity(cache_dir, [_config(tmp_path)])["listings"]
    assert len(rows) == 1
    row = rows[0]
    assert row["listed_at"] == listed
    assert row["listed_text"] == "3 days ago"
    assert isinstance(row["first_seen"], float)
    assert row["first_seen"] >= listed


def test_rows_predating_first_seen_fall_back_to_rated_at(cache_dir: Cache, tmp_path: Path) -> None:
    """An old cached row has no stamp; the rating's own is close enough."""
    url = "https://www.facebook.com/marketplace/item/777/"
    cache_dir.set(
        (CacheType.LISTING_DETAILS.value, url),
        {
            "marketplace": "facebook",
            "name": "",
            "id": "777",
            "title": "Old row",
            "image": "",
            "price": "$1",
            "post_url": url,
            "location": "",
            "seller": "",
            "condition": "",
            "description": "",
        },
        tag=CacheType.LISTING_DETAILS.value,
    )
    cache_dir.set(
        (CacheType.AI_BY_LISTING.value, "facebook", "777", "thing"),
        {"score": 4, "comment": "ok", "name": "test", "rated_at": NOW},
        tag=CacheType.AI_BY_LISTING.value,
    )
    row = build_activity(cache_dir, [_config(tmp_path)])["listings"][0]
    assert row["first_seen"] == NOW
    assert row["listed_at"] is None
    assert row["listed_text"] == ""


def test_rows_expose_a_clean_url_and_keep_the_raw_one(cache_dir: Cache, tmp_path: Path) -> None:
    listing = _listing(
        post_url="https://www.facebook.com/marketplace/item/1234567890/?ref=search&__tn__=!%3AD"
    )
    _seed(cache_dir, listing)
    row = build_activity(cache_dir, [_config(tmp_path)])["listings"][0]
    assert row["url"] == "https://www.facebook.com/marketplace/item/1234567890/"
    assert "__tn__" not in row["url"]
    # The photo proxy keys on the raw URL, so it has to survive.
    assert row["raw_url"] == listing.post_url


# ---------------------------------------------------------------------------
# eBay's own two sources for the same fact
# ---------------------------------------------------------------------------


def test_ebay_api_creation_date() -> None:
    """`itemCreationDate` is ISO 8601 in UTC; anything else is unknown."""
    from ai_marketplace_monitor.ebay import _parse_item_creation_date

    stamp = _parse_item_creation_date("2026-08-30T14:05:00.000Z")
    assert stamp is not None
    assert (
        datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat()
        == "2026-08-30T14:05:00+00:00"
    )
    assert _parse_item_creation_date(None) is None
    assert _parse_item_creation_date("") is None
    assert _parse_item_creation_date("last tuesday") is None


def test_ebay_tile_dates() -> None:
    """Newest-first tiles carry a date; relevance-sorted ones carry nothing."""
    from ai_marketplace_monitor.ebay import _parse_tile_date

    # eBay's short form has no year: read as this one, rolled back when that
    # would put the listing in the future.
    stamp = _parse_tile_date("Sep-01 08:23", now=NOW)
    assert stamp is not None
    assert datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M") == "2026-09-01 08:23"
    stamp = _parse_tile_date("Dec-20 08:23", now=NOW)
    assert stamp is not None
    assert datetime.datetime.fromtimestamp(stamp).year == 2025

    stamp = _parse_tile_date("Nov 12, 2025", now=NOW)
    assert stamp is not None
    assert datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") == "2025-11-12"

    # The shared relative path still applies when a tile words it that way.
    stamp = _parse_tile_date("2 days ago", now=NOW)
    assert stamp is not None and abs((NOW - stamp) - 2 * DAY) < 1.5

    # Auction time-left, free shipping, and an empty cell are all "unknown".
    assert _parse_tile_date("", now=NOW) is None
    assert _parse_tile_date("Free shipping", now=NOW) is None
    assert _parse_tile_date("Buy It Now", now=NOW) is None


def test_ebay_browser_tile_to_listing_carries_the_date() -> None:
    from ai_marketplace_monitor.ebay import EbayBrowserMarketplace

    backend = EbayBrowserMarketplace.__new__(EbayBrowserMarketplace)
    # tile_to_listing only reaches into the config for the site host.
    backend.config = SimpleNamespace(marketplace_id="EBAY_US")  # type: ignore[assignment]
    tile = {
        "id": "334455",
        "title": "A thing",
        "price": "$25.00",
        "img": "https://i.ebayimg.com/x.jpg",
        "subtitles": [],
        "attrs": [],
        "dates": ["Listed Nov 12, 2025"],
    }
    listing = backend.tile_to_listing(tile)
    assert listing is not None
    assert listing.listed_text == "Nov 12, 2025"
    assert listing.listed_at is not None

    tile["dates"] = ["Free shipping"]
    listing = backend.tile_to_listing(tile)
    assert listing is not None
    assert listing.listed_at is None
    assert listing.listed_text == ""
