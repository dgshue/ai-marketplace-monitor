"""Search-activity view: what was searched, what was dismissed, what looks promising.

The log pane answers "what is the monitor doing right now"; it does not answer
"what has it actually found, and was any of it worth my time". This module
answers the second question by reconstructing a reviewable record from the
on-disk cache.

Read-only. It never scrapes, never calls an AI backend, and never writes to the
cache -- it only joins three namespaces the monitor already populates:

  LISTING_DETAILS  (tag, post_url-without-query)                -> asdict(Listing)
  AI_INQUIRY       (tag, item_hash, marketplace_hash, l_hash)   -> asdict(AIResponse)
  USER_NOTIFIED    (tag, marketplace, listing_id, user)         -> (date, hash, price)

The join key is ``Listing.hash``, which is derived from the listing's own fields
rather than stored alongside them. So a details value has to be rehydrated into
a ``Listing`` and re-hashed before it can be matched to an AI_INQUIRY row. That
is why this cannot be a plain key lookup.

Memory: ratings are collected first (one small dict per rated listing), then
LISTING_DETAILS values are loaded one at a time and dropped unless their hash
was rated. The full details namespace is never held at once.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from diskcache import Cache  # type: ignore

from ..listing import Listing
from ..utils import CacheType
from .geo import Coordinates, distance_from, resolve

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - legacy runtimes
    import tomli as tomllib

logger = logging.getLogger(__name__)

# Mirrors AIResponse.conclusion. Duplicated rather than imported because the
# cache stores asdict(AIResponse) -- plain fields, no properties -- so the
# conclusion has to be recomputed from the score on the way back out.
CONCLUSIONS: Dict[int, str] = {
    1: "No match",
    2: "Potential match",
    3: "Poor match",
    4: "Good match",
    5: "Great deal",
}

# monitor.py's fallback when neither the item nor the marketplace sets `rating`.
DEFAULT_THRESHOLD = 3

VERDICT_NOTIFIED = "notified"
VERDICT_PROMISING = "promising"
VERDICT_DISMISSED = "dismissed"


def _rating_floor(value: Any) -> Optional[int]:
    """Normalize a `rating` config value to its steady-state threshold.

    monitor.py reads ``rating[0]`` on an item's first-ever search and
    ``rating[-1]`` on every search after that. The latter is the one worth
    showing, since it governs all ongoing runs.
    """
    if isinstance(value, bool):  # bool subclasses int; it is not a rating
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value:
        last = value[-1]
        return last if isinstance(last, int) and not isinstance(last, bool) else None
    return None


def thresholds_from_config(
    config_files: Iterable[Path],
) -> Tuple[Dict[str, int], int, set]:
    """Return (threshold per item name, marketplace-wide default).

    Every configured item gets an entry, not only those that set `rating` --
    build_activity also uses the key set as the candidate item names for the
    hash join, so an item missing here is an item whose listings go unmatched.

    Parsed straight from the TOML rather than through the Config loader: this
    runs on every activity request, must not raise on a half-edited file, and
    needs neither validation nor ${...} expansion to read an integer.
    """
    per_item: Dict[str, int] = {}
    explicit: Dict[str, int] = {}
    disabled: set = set()
    fallback = DEFAULT_THRESHOLD
    for path in config_files:
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        marketplaces = data.get("marketplace")
        if isinstance(marketplaces, dict):
            for section in marketplaces.values():
                if isinstance(section, dict):
                    floor = _rating_floor(section.get("rating"))
                    if floor is not None:
                        fallback = floor
        items = data.get("item")
        if isinstance(items, dict):
            for name, section in items.items():
                per_item[str(name)] = DEFAULT_THRESHOLD
                if isinstance(section, dict):
                    floor = _rating_floor(section.get("rating"))
                    if floor is not None:
                        explicit[str(name)] = floor
                    if section.get("enabled") is False:
                        disabled.add(str(name))
    for name in per_item:
        per_item[name] = explicit.get(name, fallback)
    return per_item, fallback, disabled


def home_from_config(config_files: Iterable[Path]) -> Optional[Coordinates]:
    """Resolve `home_location` from any [marketplace.*] section, if set."""
    for path in config_files:
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        marketplaces = data.get("marketplace")
        if not isinstance(marketplaces, dict):
            continue
        for section in marketplaces.values():
            if not isinstance(section, dict):
                continue
            home = section.get("home_location")
            if isinstance(home, str) and home.strip():
                found = resolve(home)
                if found:
                    return found
                logger.debug("home_location %r did not resolve", home)
    return None


def _collect_ratings(local_cache: Cache) -> Dict[str, Dict[str, Any]]:
    """listing_hash -> {score, comment, name} for every AI evaluation on record."""
    ratings: Dict[str, Dict[str, Any]] = {}
    for key in local_cache.iterkeys():
        if not isinstance(key, tuple) or len(key) < 4:
            continue
        if key[0] != CacheType.AI_INQUIRY.value:
            continue
        try:
            value = local_cache.get(key)
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.debug("Skipping unreadable AI_INQUIRY entry %r", key, exc_info=True)
            continue
        if isinstance(value, dict) and "score" in value:
            # One listing can be evaluated against several items. Last write
            # wins, which is also what the monitor itself acted on.
            ratings[key[3]] = value
    return ratings


def _collect_notified(local_cache: Cache) -> Dict[Tuple[str, str], str]:
    """(marketplace, listing_id) -> date, for listings a user was told about."""
    notified: Dict[Tuple[str, str], str] = {}
    for key in local_cache.iterkeys():
        if not isinstance(key, tuple) or len(key) < 4:
            continue
        if key[0] != CacheType.USER_NOTIFIED.value:
            continue
        try:
            value = local_cache.get(key)
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.debug("Skipping unreadable USER_NOTIFIED entry %r", key, exc_info=True)
            continue
        date = value if isinstance(value, str) else ""
        if isinstance(value, (tuple, list)) and value:
            date = value[0] or ""
        notified[(key[1], key[2])] = date
    return notified


def _collect_by_listing(local_cache: Cache) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """(marketplace, listing_id, item) -> AIResponse dict, the drift-proof join."""
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for key in local_cache.iterkeys():
        if not isinstance(key, tuple) or len(key) < 4:
            continue
        if key[0] != CacheType.AI_BY_LISTING.value:
            continue
        try:
            value = local_cache.get(key)
        except KeyboardInterrupt:
            raise
        except Exception:
            continue
        if isinstance(value, dict) and "score" in value:
            out[(key[1], key[2], key[3])] = value
    return out


def _collect_flags(local_cache: Cache) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(marketplace, listing_id) -> user flags: my_rank and hidden."""
    flags: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for key in local_cache.iterkeys():
        if not isinstance(key, tuple) or len(key) < 3:
            continue
        if key[0] != CacheType.USER_FLAGS.value:
            continue
        try:
            value = local_cache.get(key)
        except KeyboardInterrupt:
            raise
        except Exception:
            continue
        if isinstance(value, dict):
            flags[(key[1], key[2])] = value
    return flags


def _rehydrate(value: Any) -> Optional[Listing]:
    """Turn a cached LISTING_DETAILS value back into a Listing, or None.

    Cached rows predate field changes and may be missing or gaining keys, so a
    failure here is expected traffic rather than an error worth logging loudly.
    """
    if not isinstance(value, dict):
        return None
    try:
        return Listing(**value)
    except KeyboardInterrupt:
        raise
    except Exception:
        return None


def build_activity(
    local_cache: Cache,
    config_files: Iterable[Path],
    limit: int = 500,
) -> Dict[str, Any]:
    """Join the cache into per-listing review rows plus per-item totals."""
    config_files = list(config_files)
    per_item_threshold, default_threshold, disabled_items = thresholds_from_config(config_files)
    home = home_from_config(config_files)
    ratings = _collect_ratings(local_cache)
    by_listing = _collect_by_listing(local_cache)
    notified = _collect_notified(local_cache)
    user_flags = _collect_flags(local_cache)

    rows: List[Dict[str, Any]] = []
    seen_hashes: Set[str] = set()
    # Item names to try when re-hashing a cached listing, each with the
    # threshold that applies to it.
    candidates: List[Tuple[str, int]] = sorted(per_item_threshold.items())

    for key in local_cache.iterkeys():
        if not isinstance(key, tuple) or not key:
            continue
        if key[0] != CacheType.LISTING_DETAILS.value:
            continue
        try:
            listing = _rehydrate(local_cache.get(key))
        except KeyboardInterrupt:
            raise
        except Exception:
            continue
        if listing is None:
            continue

        # ``Listing.hash`` covers `name`, which is the item the listing was
        # searched for -- but LISTING_DETAILS is written during the detail
        # fetch, before that name is attached, so it is cached empty. Hashing
        # the cached row as-is therefore never matches the AI_INQUIRY row that
        # the monitor wrote while rating it. Re-hash once per configured item
        # name to find the pairing, which also recovers the item attribution
        # that the cached row lost.
        details = asdict(listing)
        for item, threshold in candidates:
            ident = (listing.marketplace, listing.id, item)
            if ident in seen_hashes:
                continue
            # Identity join first (cannot drift); hash reconstruction only for
            # ratings written before the by-listing mirror existed.
            rating = by_listing.get(ident)
            listing_hash = ident
            if rating is None:
                probe = (
                    listing
                    if details.get("name") == item
                    else Listing(**{**details, "name": item})
                )
                legacy_hash = probe.hash
                if legacy_hash in seen_hashes:
                    continue
                rating = ratings.get(legacy_hash)
                listing_hash = legacy_hash
                if rating is None:
                    continue
            score = rating.get("score")
            if not isinstance(score, int):
                continue
            seen_hashes.add(listing_hash)

            if (listing.marketplace, listing.id) in notified:
                verdict = VERDICT_NOTIFIED
            elif score >= threshold:
                verdict = VERDICT_PROMISING
            else:
                verdict = VERDICT_DISMISSED

            rows.append(
                {
                    "item": item,
                    "marketplace": listing.marketplace,
                    "id": listing.id,
                    "title": listing.title,
                    "price": listing.price,
                    "location": listing.location,
                    "image": listing.image or "",
                    # Item coordinates for the pickup map; None when the
                    # location string does not geocode.
                    "coords": (lambda c: list(c) if c else None)(resolve(listing.location or "")),
                    "distance_mi": distance_from(home, listing.location or ""),
                    "seller": listing.seller,
                    "condition": listing.condition,
                    "url": listing.post_url,
                    "score": score,
                    "conclusion": CONCLUSIONS.get(score, ""),
                    "comment": rating.get("comment", "") or "",
                    "ai_name": rating.get("name", "") or "",
                    "threshold": threshold,
                    "verdict": verdict,
                    "notified_at": notified.get((listing.marketplace, listing.id), ""),
                    # The user's own read on the listing, orthogonal to the AI's.
                    # False when the item is paused OR no longer in the config
                    # at all — either way it is off the active radar, and the
                    # default Deals view hides it while keeping it reachable.
                    "item_active": item in per_item_threshold and item not in disabled_items,
                    "my_rank": user_flags.get((listing.marketplace, listing.id), {}).get(
                        "my_rank"
                    ),
                    "hidden": bool(
                        user_flags.get((listing.marketplace, listing.id), {}).get("hidden")
                    ),
                }
            )

    # Best first, so the reason you opened the page is at the top.
    rows.sort(key=lambda row: (-row["score"], row["item"], row["title"]))

    summary: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket = summary.setdefault(
            row["item"],
            {
                "item": row["item"],
                "examined": 0,
                "dismissed": 0,
                "promising": 0,
                "notified": 0,
                "threshold": row["threshold"],
                "best_score": 0,
                "active": row["item_active"],
            },
        )
        bucket["examined"] += 1
        bucket[row["verdict"]] += 1
        bucket["best_score"] = max(bucket["best_score"], row["score"])

    return {
        "home": list(home) if home else None,
        "summary": sorted(summary.values(), key=lambda entry: entry["item"]),
        "listings": rows[:limit],
        "total": len(rows),
        "truncated": len(rows) > limit,
    }
