"""The three-tier score model: review_rating, rating, and the `low` verdict.

Two thresholds now sit between a rating and your phone:

    score <  review_rating   -> tracked only, verdict "low", never queued
    score >= review_rating   -> the review queue, verdict "promising"
    score >= rating          -> also a notification, verdict "notified"

Both keys resolve item-first, then marketplace, then 3, and a config whose
review threshold sits above its notify threshold is rejected outright.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterator

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.utils import CacheType
from ai_marketplace_monitor.webui.activity import build_activity, thresholds_from_config

BASE = """
[marketplace.facebook]
search_city = 'dallas'
username = 'u'
password = 'p'

[user.me]
pushbullet_token = 'x'

[item.iphone]
search_phrases = 'iphone'
"""

MARKETPLACE = "[marketplace.facebook]"
ITEM_PHRASES = "search_phrases = 'iphone'"


def _write(tmp_path: Path, body: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def temp_cache(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    yield cache
    cache.close()


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------
def test_defaults_to_three_when_unset(tmp_path: Path) -> None:
    _, notify_default, _, per_review, review_default = thresholds_from_config(
        [_write(tmp_path, BASE)]
    )
    assert notify_default == 3
    assert review_default == 3
    assert per_review["iphone"] == 3


def test_marketplace_review_rating_applies_to_every_item(tmp_path: Path) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 5\nreview_rating = 2")
    per_item, notify_default, _, per_review, review_default = thresholds_from_config(
        [_write(tmp_path, body)]
    )
    assert (notify_default, review_default) == (5, 2)
    assert per_item["iphone"] == 5
    assert per_review["iphone"] == 2


def test_item_overrides_marketplace(tmp_path: Path) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 5\nreview_rating = 2").replace(
        ITEM_PHRASES, ITEM_PHRASES + "\nreview_rating = 4"
    )
    _, _, _, per_review, review_default = thresholds_from_config([_write(tmp_path, body)])
    assert review_default == 2, "the marketplace default is unchanged"
    assert per_review["iphone"] == 4, "the item's own value wins"


def test_steady_state_value_of_a_two_element_list_wins(tmp_path: Path) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 5\nreview_rating = [1, 3]")
    _, _, _, per_review, _ = thresholds_from_config([_write(tmp_path, body)])
    assert per_review["iphone"] == 3


def test_review_threshold_is_clamped_to_the_notify_threshold(tmp_path: Path) -> None:
    """The TOML parser never validates, so a bad pair must not empty the queue."""
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 3\nreview_rating = 5")
    per_item, _, _, per_review, _ = thresholds_from_config([_write(tmp_path, body)])
    assert per_review["iphone"] == per_item["iphone"] == 3


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def test_review_rating_above_rating_is_rejected(tmp_path: Path) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 3\nreview_rating = 5")
    with pytest.raises(ValueError, match="review_rating"):
        Config([_write(tmp_path, body)])


def test_item_review_rating_above_inherited_marketplace_rating_is_rejected(
    tmp_path: Path,
) -> None:
    """The two keys resolve independently, so the effective pair is checked too."""
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 3").replace(
        ITEM_PHRASES, ITEM_PHRASES + "\nreview_rating = 4"
    )
    with pytest.raises(ValueError, match="review_rating"):
        Config([_write(tmp_path, body)])


def test_review_rating_equal_to_rating_is_accepted(tmp_path: Path) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 4\nreview_rating = 4")
    config = Config([_write(tmp_path, body)])
    assert config.marketplace["facebook"].review_rating == [4]


def test_review_rating_out_of_range_is_rejected(tmp_path: Path) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nreview_rating = 0")
    with pytest.raises(ValueError, match="review_rating"):
        Config([_write(tmp_path, body)])


def test_scalar_review_rating_normalizes_to_a_list(tmp_path: Path) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nreview_rating = 2")
    config = Config([_write(tmp_path, body)])
    assert config.marketplace["facebook"].review_rating == [2]


# --------------------------------------------------------------------------
# Verdicts and counts
# --------------------------------------------------------------------------
def _seed(cache: Cache, listing_id: str, score: int) -> None:
    listing = Listing(
        marketplace="facebook",
        name="",
        id=listing_id,
        title=f"iPhone scoring {score}",
        image="",
        price="$100",
        post_url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        location="Houston, TX",
        seller="Jane",
        condition="used_good",
        description="fine",
    )
    listing.to_cache(listing.post_url, local_cache=cache)
    cache.set(
        (CacheType.AI_BY_LISTING.value, "facebook", listing_id, "iphone"),
        {"score": score, "comment": "ok", "name": "ai"},
        tag=CacheType.AI_BY_LISTING.value,
    )


def _verdicts(cache: Cache, cfg: Path) -> Dict[int, str]:
    rows = build_activity(cache, [cfg])["listings"]
    return {row["score"]: row["verdict"] for row in rows}


def test_scores_one_to_five_against_review_3_notify_5(tmp_path: Path, temp_cache: Cache) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 5\nreview_rating = 3")
    cfg = _write(tmp_path, body)
    for score in (1, 2, 3, 4, 5):
        _seed(temp_cache, str(100 + score), score)
    assert _verdicts(temp_cache, cfg) == {
        1: "low",
        2: "low",
        3: "promising",
        4: "promising",
        5: "promising",
    }


def test_review_1_keeps_everything_reviewable(tmp_path: Path, temp_cache: Cache) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 4\nreview_rating = 1")
    cfg = _write(tmp_path, body)
    for score in (1, 2, 3, 4, 5):
        _seed(temp_cache, str(200 + score), score)
    assert set(_verdicts(temp_cache, cfg).values()) == {"promising"}


def test_rows_carry_both_thresholds(tmp_path: Path, temp_cache: Cache) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 5\nreview_rating = 2")
    cfg = _write(tmp_path, body)
    _seed(temp_cache, "301", 4)
    row = build_activity(temp_cache, [cfg])["listings"][0]
    assert (row["review_threshold"], row["threshold"]) == (2, 5)


def test_summary_counts_low_separately(tmp_path: Path, temp_cache: Cache) -> None:
    body = BASE.replace(MARKETPLACE, MARKETPLACE + "\nrating = 5\nreview_rating = 4")
    cfg = _write(tmp_path, body)
    for score in (1, 2, 3, 4, 5):
        _seed(temp_cache, str(400 + score), score)
    summary = build_activity(temp_cache, [cfg])["summary"][0]
    # `examined` is the AI-cost number and still counts every rating; the
    # queue-facing counts are the ones that must exclude the bottom tier.
    assert summary["examined"] == 5
    assert summary["low"] == 3
    assert summary["promising"] == 2
    assert summary["notified"] == 0
    assert summary["review_threshold"] == 4
