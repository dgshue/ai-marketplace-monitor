"""The review queue's user decisions: kept / hidden / my_rank and reviewed_at.

The Triage UI derives Queue vs Reviewed purely from these flags, so the
endpoint has to stamp ``reviewed_at`` when a decision lands and drop it when
the last decision is cleared (that is what an undo does).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from diskcache import Cache  # type: ignore
from fastapi.testclient import TestClient

from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.utils import CacheType
from ai_marketplace_monitor.webui import server as webui_server
from ai_marketplace_monitor.webui.activity import build_activity
from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app

CONFIG = (
    "[marketplace.facebook]\nsearch_city = 'dallas'\n\n[item.iphone]\nsearch_phrases = 'iphone'\n"
)


@pytest.fixture
def temp_cache(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    yield cache
    cache.close()


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


def _seed_listing(cache: Cache, listing_id: str = "42") -> Listing:
    listing = Listing(
        marketplace="facebook",
        name="",
        id=listing_id,
        title="iPhone 13",
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
        {"score": 4, "comment": "ok", "name": "ai"},
        tag=CacheType.AI_BY_LISTING.value,
    )
    return listing


def test_keep_stamps_reviewed_at_and_undo_clears_it(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, temp_cache, monkeypatch)
    body = {"marketplace": "facebook", "id": "42"}

    resp = client.post("/api/listing/flag", json={**body, "kept": True})
    flags = resp.json()["flags"]
    assert flags["kept"] is True
    assert flags["reviewed_at"] > 0
    first_stamp = flags["reviewed_at"]

    # A later rating keeps the ORIGINAL review time: the listing was decided once.
    flags = client.post("/api/listing/flag", json={**body, "my_rank": 3}).json()["flags"]
    assert flags["reviewed_at"] == first_stamp
    assert flags["my_rank"] == 3

    # Undo everything -> no decision left -> back in the queue.
    flags = client.post(
        "/api/listing/flag", json={**body, "kept": False, "hidden": False, "my_rank": None}
    ).json()["flags"]
    assert flags["kept"] is False
    assert "my_rank" not in flags
    assert "reviewed_at" not in flags


def test_dismiss_alone_counts_as_reviewed(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, temp_cache, monkeypatch)
    flags = client.post(
        "/api/listing/flag", json={"marketplace": "facebook", "id": "7", "hidden": True}
    ).json()["flags"]
    assert flags["hidden"] is True
    assert "reviewed_at" in flags


def test_activity_rows_expose_kept_and_reviewed_at(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_listing(temp_cache, "42")
    client = _client(tmp_path, temp_cache, monkeypatch)
    cfg = tmp_path / "config.toml"

    rows = build_activity(temp_cache, [cfg])["listings"]
    assert len(rows) == 1
    assert rows[0]["kept"] is False
    assert rows[0]["reviewed_at"] is None

    client.post("/api/listing/flag", json={"marketplace": "facebook", "id": "42", "kept": True})
    rows = client.get("/api/activity").json()["listings"]
    assert rows[0]["kept"] is True
    assert isinstance(rows[0]["reviewed_at"], float)
    assert rows[0]["hidden"] is False


def test_flag_rejects_bad_rank(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, temp_cache, monkeypatch)
    resp = client.post(
        "/api/listing/flag", json={"marketplace": "facebook", "id": "1", "my_rank": 9}
    )
    assert resp.status_code == 400
