"""Distance between a listing's location and home.

Used by the activity view (every row carries a `distance_mi`) and by the
per-listing notifications, which say how far away a find is before you decide
whether to open it. It lives beside the monitor rather than inside `webui`
because importing `webui` pulls in the whole FastAPI application, and a
notification should not have to start a web server to measure a distance.

Marketplace gives a listing's location as free text -- "Asheboro, NC",
"Texas City, TX", sometimes "Ships to you" or a bare city with no state. To
turn "$950, 5/5" into something actionable you also need to know whether it is
twenty minutes away or four hours, so this resolves those strings to
coordinates and measures the distance.

Geocoding is offline, via geonamescache, which bundles the GeoNames city
extract as package data. Two reasons not to call a geocoding service here:
the activity view re-resolves every row on each request, and Nominatim's usage
policy caps you at one request per second -- a page of 200 listings would take
over three minutes and would still be rate-limited on refresh.

The tradeoff is coverage. The dataset is loaded at min_city_population=500, so
the smallest hamlets and neighbourhood names ("North Asheboro") can miss.
A miss yields None and the row simply shows no distance, which is the right
failure: a wrong distance is worse than an absent one.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MILES_PER_KM = 0.621371
EARTH_RADIUS_KM = 6371.0088  # mean radius, IUGG

Coordinates = Tuple[float, float]

# "35.7, -79.8" -- accepted so a user whose town is missing from the dataset,
# or who wants a precise origin, can bypass name lookup entirely.
_LATLON_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

# Built once on first use: (lowercase city, state code) -> coordinates, plus a
# (lowercase city) -> coordinates fallback for strings with no state.
_index: Optional[Dict[str, Coordinates]] = None
_index_failed = False


def _load_index() -> Dict[str, Coordinates]:
    """Build the lookup table from geonamescache, once per process."""
    global _index, _index_failed
    if _index is not None:
        return _index
    if _index_failed:
        return {}

    table: Dict[str, Coordinates] = {}
    try:
        import geonamescache  # type: ignore

        cities = geonamescache.GeonamesCache(min_city_population=500).get_cities()
    except KeyboardInterrupt:
        raise
    except Exception:
        # Missing or broken optional data: degrade to "no distances" rather
        # than breaking the whole activity view.
        logger.debug("geonamescache unavailable; distances disabled", exc_info=True)
        _index_failed = True
        return {}

    for city in cities.values():
        try:
            name = str(city["name"]).strip().lower()
            coords = (float(city["latitude"]), float(city["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        state = str(city.get("admin1code") or "").strip().lower()

        # GeoNames punctuates ("St. Louis") where Marketplace usually does not
        # ("St Louis"), so index a de-punctuated form alongside the original.
        names = {name}
        depunctuated = " ".join(name.replace(".", " ").split())
        if depunctuated:
            names.add(depunctuated)

        for variant in names:
            if state:
                table.setdefault(f"{variant}|{state}", coords)
            # Bare-name fallback, consulted only when the location string
            # carries no state at all to disambiguate with.
            table.setdefault(variant, coords)

    _index = table
    return table


# Marketplace writes place names the way people type them; GeoNames stores
# them expanded. Without this, "Mt Pleasant" and "St Louis" simply miss.
_ABBREVIATIONS = (
    ("mt ", "mount "),
    ("st ", "saint "),
    ("ft ", "fort "),
    ("n ", "north "),
    ("s ", "south "),
    ("e ", "east "),
    ("w ", "west "),
)


def _name_variants(city: str) -> Tuple[str, ...]:
    """The name as written, plus an expanded form if it starts with an abbreviation."""
    cleaned = city.replace(".", " ")
    cleaned = " ".join(cleaned.split())
    variants = [city]
    if cleaned != city:
        variants.append(cleaned)
    for short, full in _ABBREVIATIONS:
        if cleaned.startswith(short):
            variants.append(full + cleaned[len(short) :])
            break
    return tuple(dict.fromkeys(variants))


def parse_coordinates(text: str) -> Optional[Coordinates]:
    """Read an explicit "lat, lon" pair, or None if it is not one."""
    match = _LATLON_RE.match(text or "")
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return lat, lon
    return None


def resolve(location: str) -> Optional[Coordinates]:
    """Resolve a location string to coordinates, or None if unrecognised."""
    if not location:
        return None
    text = location.strip()
    if not text:
        return None

    explicit = parse_coordinates(text)
    if explicit:
        return explicit

    # Facebook uses these in place of a real location for shipped items.
    lowered = text.lower()
    if lowered in {"ships to you", "shipping", "**unspecified**", "unspecified"}:
        return None

    table = _load_index()
    if not table:
        return None

    parts = [p.strip().lower() for p in text.split(",") if p.strip()]
    if not parts:
        return None

    city = parts[0]
    if len(parts) >= 2:
        state = parts[1]
        for name in _name_variants(city):
            found = table.get(f"{name}|{state}") or table.get(f"{name}|{parts[-1]}")
            if found:
                return found
        # A state was given and nothing matched inside it. Do NOT fall back to
        # a bare-name lookup: "Mt Pleasant, SC" would then match a Mt Pleasant
        # on another continent and report a confidently wrong distance
        # (measured: 2356 mi for a town ~200 mi away). No distance is better.
        return None

    # No state to disambiguate with, so a bare name is the best available and
    # duplicates across states resolve to whichever the dataset yields first.
    for name in _name_variants(city):
        found = table.get(name)
        if found:
            return found
    return None


def haversine_miles(origin: Coordinates, target: Coordinates) -> float:
    """Great-circle distance in statute miles."""
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    lat2, lon2 = math.radians(target[0]), math.radians(target[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a)) * MILES_PER_KM


def distance_from(home: Optional[Coordinates], location: str) -> Optional[float]:
    """Miles from `home` to `location`, or None if either cannot be resolved."""
    if home is None:
        return None
    target = resolve(location)
    if target is None:
        return None
    return round(haversine_miles(home, target), 1)
