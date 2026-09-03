"""The cache-boundary price merge for Facebook listings.

Vehicle detail pages carry no price element, so the PDP scraper falls back to
the first "$..." in the seller's description -- on a dealer listing that is the
down payment, not the price ("$450" under a "$3,000 | $4,200" tile). The value
is plausible, so the junk-artifact test in ``_prefer_tile`` never fires and the
wrong number is what gets cached and shown. The tile price is also what the AI
was rated against, so it wins outright whenever the tile has one.
"""

from __future__ import annotations

from ai_marketplace_monitor.facebook import FacebookMarketplace


def _fb() -> FacebookMarketplace:
    """A bare instance: _merge_price touches no browser and no config."""
    return FacebookMarketplace.__new__(FacebookMarketplace)


def test_tile_price_beats_a_plausible_but_wrong_pdp_price() -> None:
    assert _fb()._merge_price("$450", "$3,000 | $4,200") == "$3,000 | $4,200"


def test_tile_price_beats_a_clipped_down_payment() -> None:
    assert _fb()._merge_price("$550", "$5,500") == "$5,500"


def test_pdp_price_is_kept_when_the_tile_has_none() -> None:
    assert _fb()._merge_price("$450", None) == "$450"
    assert _fb()._merge_price("$450", "") == "$450"


def test_junk_pdp_price_without_a_tile_price_becomes_empty() -> None:
    assert _fb()._merge_price("**unspecified**", None) == ""


def test_the_other_fields_still_prefer_the_pdp() -> None:
    """Only price changed; title/location/seller keep the junk-only override."""
    fb = _fb()
    assert fb._prefer_tile("Honda Civic", "Civic 2012") == "Honda Civic"
    assert fb._prefer_tile("Seller's description", "Winston-Salem, NC") == "Winston-Salem, NC"
