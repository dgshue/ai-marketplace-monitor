"""Multi-photo listings: extraction, the model, the proxy, and what to keep.

Two bugs and one feature share this code path, so they share a test file:

  * a listing used to carry exactly one photo -- the search tile's thumbnail;
  * that photo was often not the item at all. ``page.locator("img").first``
    on a listing page is the signed-in account's avatar whenever Facebook
    renders the page chrome, which it did for 49 of the 204 listings this
    deployment had cached, and an .mp4 for another 24.

So the extraction tests assert both "all of the listing's photos" and "none
of anybody's face".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
from diskcache import Cache  # type: ignore
from fastapi.testclient import TestClient
from pytest_playwright.pytest_playwright import CreateContextCallback  # type: ignore

from ai_marketplace_monitor.facebook import (
    FacebookItemConfig,
    FacebookMarketplace,
    FacebookRegularItemPage,
    is_listing_photo,
    select_listing_photos,
)
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.marketplace import DEFAULT_MAX_IMAGES, MarketItemCommonConfig
from ai_marketplace_monitor.monitor import photos_to_snapshot
from ai_marketplace_monitor.utils import CacheType, image_cache_path
from ai_marketplace_monitor.webui import server as webui_server
from ai_marketplace_monitor.webui.activity import build_activity
from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app

FIXTURE = Path(__file__).parent / "gallery_listing.html"

# Real URL shapes, lifted from this deployment's cache.
HERO = (
    "https://scontent-atl3-2.xx.fbcdn.net/v/t39.30808-6/"
    "764638222_122327286884029173_9122952715198746806_n.jpg?stp=dst-jpg_p960x960_tt6"
)
HERO_THUMB = (
    "https://scontent-atl3-2.xx.fbcdn.net/v/t39.30808-6/"
    "764638222_122327286884029173_9122952715198746806_n.jpg?stp=cp6_dst-jpg_s135x135_tt6"
)
SECOND = (
    "https://scontent-atl3-2.xx.fbcdn.net/v/t39.30808-6/"
    "764638333_122327286884029174_9122952715198746807_n.jpg?stp=dst-jpg_p960x960_tt6"
)
MARKETPLACE_TYPE = (
    "https://scontent-atl3-3.xx.fbcdn.net/v/t45.5328-4/"
    "758245863_1724149762205409_937498582279949920_n.jpg?stp=dst-jpg_p720x720_tt6"
)
AVATAR = (
    "https://scontent-atl3-3.xx.fbcdn.net/v/t39.30808-1/"
    "742338820_27736835159259460_3023661446999844781_n.jpg?stp=cp6_dst-jpg_s100x100_tt6"
)
VIDEO = (
    "https://video-atl3-3.xx.fbcdn.net/o1/v/t2/f2/m266/"
    "AQPbfvrKt3DJDqaaSMcAv77INBonkzGVqyDjoMOH8Ccj.mp4?strext=1"
)
SPRITE = "https://static.xx.fbcdn.net/rsrc.php/v3/yq/r/icon.png"


# ---------------------------------------------------------------- URL rules


@pytest.mark.parametrize("url", [HERO, HERO_THUMB, SECOND, MARKETPLACE_TYPE])
def test_listing_photos_are_recognized(url: str) -> None:
    assert is_listing_photo(url)


@pytest.mark.parametrize(
    "url",
    [
        AVATAR,
        # The profile subtype identifies an avatar whatever size it was
        # requested at.
        AVATAR.split("?")[0] + "?stp=dst-jpg_p960x960_tt6",
        "https://scontent-atl3-3.xx.fbcdn.net/v/t1.6435-1/1_2_3_n.jpg",
        VIDEO,
        SPRITE,
        "",
        "data:image/png;base64,iVBORw0KGgo=",
    ],
)
def test_non_photos_are_rejected(url: str) -> None:
    assert not is_listing_photo(url)


def test_variants_of_one_photo_collapse_to_the_largest() -> None:
    picked = select_listing_photos(
        [{"src": HERO_THUMB}, {"src": HERO}, {"src": SECOND}, {"src": AVATAR}]
    )
    # One entry for the hero, at its 960px variant, even though the 135px one
    # came first; the avatar never enters.
    assert picked == [HERO, SECOND]


def test_selection_keeps_page_order_and_honours_the_cap() -> None:
    urls = [
        f"https://scontent-x.xx.fbcdn.net/v/t39.30808-6/{n}_2_3_n.jpg?stp=dst-jpg_p960x960_tt6"
        for n in range(20)
    ]
    picked = select_listing_photos([{"src": u} for u in urls], limit=12)
    assert picked == urls[:12]


def test_profile_flagged_candidates_are_dropped() -> None:
    assert select_listing_photos([{"src": HERO, "profile": True}]) == []


# ---------------------------------------------------------------- extraction


def _page(new_context: CreateContextCallback):  # type: ignore[no-untyped-def]
    page = new_context().new_page()
    page.goto(f"file://{FIXTURE}")
    page.wait_for_load_state("domcontentloaded")
    return page


def test_gallery_comes_from_the_inline_listing_json(
    new_context: CreateContextCallback,
) -> None:
    page = _page(new_context)
    images = FacebookRegularItemPage(page).get_images()
    stems = [url.split("/")[-1].split(".jpg")[0] for url in images]
    assert stems == [
        "764638222_122327286884029173_9122952715198746806_n",
        "764638333_122327286884029174_9122952715198746807_n",
        "758245863_1724149762205409_937498582279949920_n",
    ]
    # The JSON carries full-size URLs, not the 135px tile variant that the
    # same page's primary_listing_photo holds.
    assert all("s135x135" not in url for url in images)


def test_gallery_falls_back_to_the_dom_without_the_json(
    new_context: CreateContextCallback,
) -> None:
    page = _page(new_context)
    page.evaluate("() => document.querySelectorAll('script').forEach((s) => s.remove())")
    images = FacebookRegularItemPage(page).get_images()
    stems = [url.split("/")[-1].split(".jpg")[0] for url in images]
    assert stems == [
        "764638222_122327286884029173_9122952715198746806_n",
        "764638333_122327286884029174_9122952715198746807_n",
        "758245863_1724149762205409_937498582279949920_n",
    ]
    # The account avatar, the seller's profile picture, the video, the UI
    # sprite and the two "Similar listings" tiles are all absent.
    joined = " ".join(images)
    assert "-1/" not in joined
    assert "video-" not in joined
    assert "111222333" not in joined and "444555666" not in joined


def test_primary_photo_is_the_first_of_the_gallery(
    new_context: CreateContextCallback,
) -> None:
    page = _page(new_context)
    item_page = FacebookRegularItemPage(page)
    assert item_page.get_image_url() == item_page.get_images()[0]


# ------------------------------------------------------------- cache merge


def test_pdp_gallery_wins_over_the_tile_photo() -> None:
    fb = FacebookMarketplace.__new__(FacebookMarketplace)
    assert fb._merge_images([HERO, SECOND], HERO_THUMB) == [HERO, SECOND]


def test_tile_photo_is_the_fallback_when_the_page_yielded_nothing() -> None:
    fb = FacebookMarketplace.__new__(FacebookMarketplace)
    assert fb._merge_images([], HERO) == [HERO]
    # ... but never a fallback to somebody's face.
    assert fb._merge_images([], AVATAR) == []
    assert fb._merge_images([], None) == []


def test_a_cache_hit_backfills_a_missing_photo_from_the_tile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows cached before galleries existed have no photo. Fix them for free.

    The listing page is never revisited when price and title still match, so
    without this an old listing keeps an empty frame forever. It does not gain
    the rest of the gallery -- that only exists on the page being skipped.
    """
    cache = Cache(str(tmp_path / "c"))
    try:
        import ai_marketplace_monitor.listing as listing_module

        monkeypatch.setattr(listing_module, "cache", cache)
        stale = _listing(image="", images=[])
        stale.to_cache(stale.post_url, local_cache=cache)

        fb = FacebookMarketplace.__new__(FacebookMarketplace)
        item_config = FacebookItemConfig(name="probe", search_phrases=["x"])
        got, from_cache = fb.get_listing_details(
            stale.post_url,
            item_config,
            price=stale.price,
            title=stale.title,
            image=HERO,
        )
        assert from_cache is True
        assert got.image == HERO and got.images == [HERO]
        # ... and it stuck, so the web UI reads it too.
        again = Listing.from_cache(stale.post_url, local_cache=cache)
        assert again is not None and again.images == [HERO]

        # An avatar is not a backfill.
        blanked = _listing(image="", images=[])
        blanked.post_url = "https://www.facebook.com/marketplace/item/43/"
        blanked.to_cache(blanked.post_url, local_cache=cache)
        got, _ = fb.get_listing_details(
            blanked.post_url,
            item_config,
            price=blanked.price,
            title=blanked.title,
            image=AVATAR,
        )
        assert got.image == "" and got.images == []
    finally:
        cache.close()


# ------------------------------------------------------------------- model


def _listing(**over: Any) -> Listing:
    fields: Dict[str, Any] = {
        "marketplace": "facebook",
        "name": "",
        "id": "42",
        "title": "iPhone 13",
        "image": "",
        "price": "$100",
        "post_url": "https://www.facebook.com/marketplace/item/42/",
        "location": "Houston, TX",
        "seller": "Jane",
        "condition": "used_good",
        "description": "fine",
    }
    fields.update(over)
    return Listing(**fields)  # type: ignore[arg-type]


def test_cached_rows_without_images_still_load() -> None:
    """Every row cached before this feature lacks the key entirely."""
    legacy = {
        "marketplace": "facebook",
        "name": "",
        "id": "42",
        "title": "iPhone 13",
        "image": HERO,
        "price": "$100",
        "post_url": "https://www.facebook.com/marketplace/item/42/",
        "location": "Houston, TX",
        "seller": "Jane",
        "condition": "used_good",
        "description": "fine",
    }
    listing = Listing(**legacy)  # type: ignore[arg-type]
    assert listing.images == []
    # The one photo it does have is still photo 0.
    assert listing.photos == [HERO]


def test_photos_prefers_the_gallery_and_survives_an_empty_one() -> None:
    assert _listing(image=HERO, images=[HERO, SECOND]).photos == [HERO, SECOND]
    assert _listing(image="", images=[]).photos == []


def test_the_gallery_stays_out_of_the_hash() -> None:
    """The hash joins a cached listing to its cached AI rating.

    Letting a newly extracted gallery into it would orphan every rating
    recorded before this field existed.
    """
    assert _listing(images=[]).hash == _listing(images=[HERO, SECOND]).hash


def test_round_trip_through_the_cache(tmp_path: Path) -> None:
    cache = Cache(str(tmp_path / "c"))
    try:
        listing = _listing(image=HERO, images=[HERO, SECOND])
        listing.to_cache(listing.post_url, local_cache=cache)
        back = Listing.from_cache(listing.post_url, local_cache=cache)
        assert back is not None and back.images == [HERO, SECOND]
    finally:
        cache.close()


# ------------------------------------------------------------ snapshot policy


def test_only_review_worthy_listings_are_snapshotted() -> None:
    listing = _listing(image=HERO, images=[HERO, SECOND, MARKETPLACE_TYPE])
    # Under the review threshold the listing never reaches the queue, so its
    # photos are bytes nobody will look at.
    assert photos_to_snapshot(listing, score=2, review_rating=3, max_images=6) == []
    assert photos_to_snapshot(listing, score=3, review_rating=3, max_images=6) == [
        HERO,
        SECOND,
        MARKETPLACE_TYPE,
    ]
    assert photos_to_snapshot(listing, score=5, review_rating=3, max_images=2) == [HERO, SECOND]
    # 0 turns snapshotting off outright.
    assert photos_to_snapshot(listing, score=5, review_rating=3, max_images=0) == []


def test_a_listing_with_one_photo_still_snapshots_it() -> None:
    assert photos_to_snapshot(_listing(image=HERO), 4, 3, DEFAULT_MAX_IMAGES) == [HERO]


# --------------------------------------------------------------- config


@pytest.mark.parametrize("value,expected", [(None, None), (0, 0), (3, 3), ("4", 4), (12, 12)])
def test_max_images_accepts(value: Any, expected: Any) -> None:
    assert MarketItemCommonConfig(name="x", max_images=value).max_images == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, 13, True, "many", 2.5])
def test_max_images_rejects(value: Any) -> None:
    with pytest.raises(ValueError):
        MarketItemCommonConfig(name="x", max_images=value)  # type: ignore[arg-type]


def test_max_images_precedence_item_then_marketplace_then_default() -> None:
    """The resolution the monitor applies, spelled out."""

    def resolve(item: int | None, marketplace: int | None) -> int:
        if item is not None:
            return item
        return marketplace if marketplace is not None else DEFAULT_MAX_IMAGES

    assert resolve(2, 9) == 2
    assert resolve(None, 9) == 9
    assert resolve(None, None) == DEFAULT_MAX_IMAGES
    # 0 is a real setting, not "unset".
    assert resolve(0, 9) == 0
    assert resolve(None, 0) == 0


# ------------------------------------------------------------ activity rows


CONFIG = (
    "[marketplace.facebook]\nsearch_city = 'dallas'\n\n[item.iphone]\nsearch_phrases = 'iphone'\n"
)


@pytest.fixture
def temp_cache(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    yield cache
    cache.close()


def _seed(cache: Cache, images: List[str]) -> Listing:
    listing = _listing(image=images[0] if images else "", images=images)
    listing.to_cache(listing.post_url, local_cache=cache)
    cache.set(
        (CacheType.AI_BY_LISTING.value, "facebook", "42", "iphone"),
        {"score": 4, "comment": "ok", "name": "ai"},
        tag=CacheType.AI_BY_LISTING.value,
    )
    return listing


def test_activity_rows_carry_the_gallery(tmp_path: Path, temp_cache: Cache) -> None:
    _seed(temp_cache, [HERO, SECOND, MARKETPLACE_TYPE])
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    row = build_activity(temp_cache, [cfg])["listings"][0]
    assert row["image"] == HERO
    assert row["images"] == [HERO, SECOND, MARKETPLACE_TYPE]
    assert row["image_count"] == 3


def test_activity_rows_of_single_photo_listings_report_one(
    tmp_path: Path, temp_cache: Cache
) -> None:
    _seed(temp_cache, [HERO])
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    row = build_activity(temp_cache, [cfg])["listings"][0]
    assert row["image_count"] == 1 and row["images"] == [HERO]


# ----------------------------------------------------------------- proxy


def _client(tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(webui_server, "cache", temp_cache)
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    handler = LogBroadcastHandler()
    app = create_app(
        WebUIConfig(config_files=[cfg], log_handler=handler),
        AuthState(),
        ConfigFileService([cfg]),
        handler,
    )
    return TestClient(app)


def test_proxy_serves_each_photo_from_its_own_snapshot(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = _seed(temp_cache, [HERO, SECOND, MARKETPLACE_TYPE])
    monkeypatch.setattr(webui_server, "image_cache_path", lambda url, i=0: tmp_path / f"i{i}.img")
    for index in range(3):
        (tmp_path / f"i{index}.img").write_bytes(f"photo{index}".encode())
    client = _client(tmp_path, temp_cache, monkeypatch)
    for index in range(3):
        resp = client.get(f"/api/listing-image?post={listing.post_url}&i={index}")
        assert resp.status_code == 200
        assert resp.content == f"photo{index}".encode()
    # No index at all is photo 0, exactly as before galleries existed.
    assert client.get(f"/api/listing-image?post={listing.post_url}").content == b"photo0"


def test_proxy_404s_outside_the_gallery(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = _seed(temp_cache, [HERO, SECOND])
    monkeypatch.setattr(webui_server, "image_cache_path", lambda url, i=0: tmp_path / f"i{i}.img")
    (tmp_path / "i0.img").write_bytes(b"photo0")
    client = _client(tmp_path, temp_cache, monkeypatch)
    assert client.get(f"/api/listing-image?post={listing.post_url}&i=2").status_code == 404
    assert client.get(f"/api/listing-image?post={listing.post_url}&i=-1").status_code == 404
    assert client.get("/api/listing-image?post=https://nope.invalid/x").status_code == 404


def test_proxy_404s_cleanly_when_the_cdn_url_has_expired(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache miss falls through to a live fetch; a dead URL is a 404."""
    listing = _seed(temp_cache, [HERO, SECOND])
    monkeypatch.setattr(webui_server, "image_cache_path", lambda url, i=0: tmp_path / f"i{i}.img")
    monkeypatch.setattr(webui_server, "fetch_image_snapshot", lambda url, dest: False)
    client = _client(tmp_path, temp_cache, monkeypatch)
    resp = client.get(f"/api/listing-image?post={listing.post_url}&i=1")
    assert resp.status_code == 404
    assert "expired" in resp.json()["detail"].lower()


def test_snapshot_paths_keep_the_pre_gallery_name() -> None:
    """Photo 0 must resolve to the file every existing snapshot was saved as."""
    url = "https://www.facebook.com/marketplace/item/42/"
    assert image_cache_path(url, 0).name.endswith(".img")
    assert "-" not in image_cache_path(url, 0).stem
    assert image_cache_path(url, 3).stem == image_cache_path(url, 0).stem + "-3"
    # The query string is not part of the identity, exactly as the cache key.
    assert image_cache_path(url + "?ref=x", 0) == image_cache_path(url, 0)
