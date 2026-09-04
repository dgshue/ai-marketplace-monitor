"""One notification per listing: the payload, the auth, and the fan-out.

The digest era is over -- every push backend sends one message per listing --
so what is worth pinning down is the shape of that message (does it carry the
price, the verdict, the threshold, the distance, both links, the photo?), the
two ways an ntfy token can reach the server, and the fact that the bookkeeping
that decides "have I already told you about this one" still runs per listing.
"""

import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from ai_marketplace_monitor.ai import AIResponse
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.marketplace import ItemConfig
from ai_marketplace_monitor.notification import (
    NotificationStatus,
    NotifyContext,
    PushNotificationConfig,
    app_deep_link,
    build_listing_notice,
)
from ai_marketplace_monitor.ntfy import NtfyNotificationConfig
from ai_marketplace_monitor.pushbullet import PushbulletNotificationConfig
from ai_marketplace_monitor.pushover import PushoverNotificationConfig

APP_URL = "https://aimm.example.com"


def make_listing(**overrides: Any) -> Listing:
    fields: Dict[str, Any] = {
        "marketplace": "facebook",
        "name": "car",
        "id": "123456",
        "title": "2014 Acura RLX SH-AWD",
        "image": "https://cdn.example.com/photo0.jpg",
        "price": "$4,500",
        "post_url": "https://www.facebook.com/marketplace/item/123456/?ref=search",
        "location": "Asheboro, NC",
        "seller": "Someone",
        "condition": "Used - good",
        "description": "Runs great, clean title, new tires all around.",
        "images": ["https://cdn.example.com/photo0.jpg", "https://cdn.example.com/photo1.jpg"],
    }
    fields.update(overrides)
    return Listing(**fields)


def rating(score: int = 5, comment: str = "Priced $2k under comparable listings.") -> AIResponse:
    return AIResponse(score=score, comment=comment, name="qa")


def item_config(name: str = "qaitem") -> ItemConfig:
    """A real item config -- ``User.notify`` is generic over one.

    Only ``name`` is read on the notification path (the counter is keyed by
    it), but a stand-in namespace is not a type mypy accepts.
    """
    return ItemConfig(name=name, search_phrases=["qa"])


# ---------------------------------------------------------------------------
# The notice itself
# ---------------------------------------------------------------------------
class TestBuildListingNotice:
    def test_full_notice(self: "TestBuildListingNotice") -> None:
        listing = make_listing(listed_at=time.time() - 3 * 86400)
        notice = build_listing_notice(
            listing,
            rating(),
            NotificationStatus.NOT_NOTIFIED,
            app_url=APP_URL,
            notify_threshold=4,
            distance_mi=12.4,
        )
        assert notice.title == "$4,500 · 2014 Acura RLX SH-AWD"
        lines = notice.message.split("\n")
        assert lines[0] == "5/5 Great deal · notify ≥ 4"
        assert lines[1] == "12.4 mi · Asheboro, NC · listed 3d ago"
        assert lines[2] == "Priced $2k under comparable listings."
        # The shareable address, not the one the search page handed us.
        assert notice.listing_url == "https://www.facebook.com/marketplace/item/123456/"
        assert notice.app_link == f"{APP_URL}/#listing/facebook/123456"
        assert notice.photo_url == "https://cdn.example.com/photo0.jpg"
        assert notice.priority == 4
        assert notice.tags == ["car", "facebook"]

    def test_no_photo_no_distance_no_app_url(self: "TestBuildListingNotice") -> None:
        listing = make_listing(image="", images=[])
        notice = build_listing_notice(listing, rating(score=4, comment="Decent."))
        assert notice.photo_url is None
        assert notice.app_link is None
        # No threshold to quote and no distance to give: the verdict is still
        # there, the meta line is just the location.
        assert notice.message.split("\n")[0] == "4/5 Good match"
        assert notice.message.split("\n")[1] == "Asheboro, NC"
        # Priority stays at the default for anything under a 5.
        assert notice.priority == 3
        assert notice.links == ["https://www.facebook.com/marketplace/item/123456/"]

    def test_unspecified_price_is_not_printed(self: "TestBuildListingNotice") -> None:
        notice = build_listing_notice(make_listing(price="**unspecified**"), rating())
        assert notice.title == "2014 Acura RLX SH-AWD"

    def test_long_title_is_truncated(self: "TestBuildListingNotice") -> None:
        listing = make_listing(title="2014 Acura RLX SH-AWD " + "w/Advance Package " * 8)
        notice = build_listing_notice(listing, rating())
        assert notice.title.startswith("$4,500 · 2014 Acura RLX SH-AWD")
        assert notice.title.endswith("…")
        assert len(notice.title) <= len("$4,500 · ") + 64

    def test_status_prefixes_a_repeat_notification(self: "TestBuildListingNotice") -> None:
        notice = build_listing_notice(
            make_listing(), rating(), NotificationStatus.LISTING_DISCOUNTED
        )
        assert notice.title == "Price drop · $4,500 · 2014 Acura RLX SH-AWD"
        again = build_listing_notice(make_listing(), rating(), NotificationStatus.EXPIRED)
        assert again.title.startswith("Still listed · ")

    def test_unrated_listing_has_no_verdict_line(self: "TestBuildListingNotice") -> None:
        notice = build_listing_notice(
            make_listing(), AIResponse(score=3, comment=AIResponse.NOT_EVALUATED)
        )
        assert notice.message == "Asheboro, NC"
        assert notice.priority == 3

    def test_whole_miles_lose_the_trailing_zero(self: "TestBuildListingNotice") -> None:
        notice = build_listing_notice(make_listing(), rating(), distance_mi=12.0)
        assert "12 mi" in notice.message

    def test_deep_link_needs_an_app_url(self: "TestBuildListingNotice") -> None:
        assert app_deep_link(None, "facebook", "1") is None
        assert app_deep_link("https://x.test/", "ebay", "9") == "https://x.test/#listing/ebay/9"


# ---------------------------------------------------------------------------
# ntfy: JSON publish, and the two ways a token gets there
# ---------------------------------------------------------------------------
class TestNtfyPublish:
    def config(self: "TestNtfyPublish", **kw: Any) -> NtfyNotificationConfig:
        fields: Dict[str, Any] = {
            "name": "ntfy",
            "ntfy_server": "https://ntfy.example.com",
            "ntfy_topic": "listings",
            "app_url": APP_URL,
        }
        fields.update(kw)
        return NtfyNotificationConfig(**fields)

    def test_bearer_token_header(self: "TestNtfyPublish") -> None:
        cfg = self.config(ntfy_token="tk_secret")
        url, topic, headers = cfg._endpoint()
        assert url == "https://ntfy.example.com/"
        assert topic == "listings"
        assert headers["Authorization"] == "Bearer tk_secret"

    def test_legacy_auth_query_on_the_topic_still_works(self: "TestNtfyPublish") -> None:
        """The pre-header deployment put ?auth= on NTFY_TOPIC; keep it publishing."""
        cfg = self.config(ntfy_topic="listings?auth=QmVhcmVyIHRrX3NlY3JldA")
        url, topic, headers = cfg._endpoint()
        assert url == "https://ntfy.example.com/?auth=QmVhcmVyIHRrX3NlY3JldA"
        assert topic == "listings"
        assert "Authorization" not in headers

    def test_legacy_auth_query_on_the_server_still_works(self: "TestNtfyPublish") -> None:
        cfg = self.config(ntfy_server="https://ntfy.example.com/?auth=QUJD")
        url, topic, _ = cfg._endpoint()
        assert url == "https://ntfy.example.com/?auth=QUJD"
        assert topic == "listings"

    def test_listing_payload(self: "TestNtfyPublish") -> None:
        cfg = self.config(ntfy_token="tk_secret")
        notice = build_listing_notice(
            make_listing(), rating(), app_url=APP_URL, notify_threshold=4, distance_mi=12.4
        )
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            assert cfg.send_listing(notice) is True
        body = post.call_args.kwargs["json"]
        assert body["topic"] == "listings"
        assert body["title"] == notice.title
        assert body["priority"] == 4
        assert body["tags"] == ["car", "facebook"]
        assert body["click"] == notice.app_link
        assert body["attach"] == body["icon"] == "https://cdn.example.com/photo0.jpg"
        assert [a["label"] for a in body["actions"]] == ["Open listing", "Open in AIMM"]
        assert body["actions"][0]["url"] == notice.listing_url
        assert body["actions"][1]["url"] == notice.app_link
        assert all(a["action"] == "view" for a in body["actions"])
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tk_secret"

    def test_payload_without_photo_or_app_url(self: "TestNtfyPublish") -> None:
        cfg = self.config(app_url=None)
        notice = build_listing_notice(make_listing(image="", images=[]), rating())
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            cfg.send_listing(notice)
        body = post.call_args.kwargs["json"]
        assert "attach" not in body and "icon" not in body
        assert [a["label"] for a in body["actions"]] == ["Open listing"]
        # Nowhere else to send a tap: the marketplace it is.
        assert body["click"] == notice.listing_url

    def test_alert_clicks_through_to_status(self: "TestNtfyPublish") -> None:
        cfg = self.config()
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            cfg.send_message("facebook is blocked", "Retrying at 14:20")
        body = post.call_args.kwargs["json"]
        assert body["click"] == f"{APP_URL}/#status"
        assert body["title"] == "facebook is blocked"

    def test_a_rejected_publish_is_not_reported_as_sent(self: "TestNtfyPublish") -> None:
        cfg = self.config()
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("401 Unauthorized")
        with patch("ai_marketplace_monitor.ntfy.requests.post", return_value=response):
            with pytest.raises(RuntimeError):
                cfg.send_message("t", "m")


# ---------------------------------------------------------------------------
# Fan-out: one send per listing, and only for listings that need one
# ---------------------------------------------------------------------------
class TestFanOut:
    def listings(self: "TestFanOut", n: int) -> List[Listing]:
        return [make_listing(id=str(1000 + i), title=f"Listing {i}") for i in range(n)]

    def test_one_send_per_listing(self: "TestFanOut") -> None:
        cfg = NtfyNotificationConfig(
            name="ntfy",
            ntfy_server="https://ntfy.example.com",
            ntfy_topic="listings",
            app_url=APP_URL,
        )
        listings = self.listings(3)
        statuses = [NotificationStatus.NOT_NOTIFIED] * 3
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            assert cfg.notify(listings, [rating()] * 3, statuses, context=NotifyContext()) is True
        assert post.call_count == 3
        titles = [call.kwargs["json"]["title"] for call in post.call_args_list]
        assert titles == [f"$4,500 · Listing {i}" for i in range(3)]

    def test_already_notified_listings_are_skipped(self: "TestFanOut") -> None:
        cfg = NtfyNotificationConfig(
            name="ntfy", ntfy_server="https://ntfy.example.com", ntfy_topic="listings"
        )
        listings = self.listings(3)
        statuses = [
            NotificationStatus.NOT_NOTIFIED,
            NotificationStatus.NOTIFIED,
            NotificationStatus.EXPIRED,
        ]
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            cfg.notify(listings, [rating()] * 3, statuses)
        assert post.call_count == 2
        # The reminder keeps its own wording, per listing.
        assert post.call_args_list[1].kwargs["json"]["title"].startswith("Still listed · ")

    def test_force_resends_everything(self: "TestFanOut") -> None:
        cfg = NtfyNotificationConfig(
            name="ntfy", ntfy_server="https://ntfy.example.com", ntfy_topic="listings"
        )
        statuses = [NotificationStatus.NOTIFIED] * 2
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            cfg.notify(self.listings(2), [rating()] * 2, statuses, force=True)
        assert post.call_count == 2

    def test_nothing_to_send_returns_false(self: "TestFanOut") -> None:
        cfg = NtfyNotificationConfig(
            name="ntfy", ntfy_server="https://ntfy.example.com", ntfy_topic="listings"
        )
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            assert cfg.notify(self.listings(1), [rating()], [NotificationStatus.NOTIFIED]) is False
        assert post.call_count == 0

    def test_a_dead_backend_stops_the_batch_but_keeps_what_got_through(
        self: "TestFanOut",
    ) -> None:
        """Exhausting the retries once means the server is down, not unlucky.

        On the defaults that is five minutes of sleeping for one listing;
        repeating it per listing would stall the search loop for as many
        minutes again. What did get through is reported, and only that is
        recorded (see the context's `sent` set).
        """
        cfg = NtfyNotificationConfig(
            name="ntfy",
            ntfy_server="https://ntfy.example.com",
            ntfy_topic="listings",
            max_retries=1,
        )
        ok = MagicMock(status_code=200)
        bad = MagicMock()
        bad.raise_for_status.side_effect = RuntimeError("500")
        ctx = NotifyContext()
        listings = self.listings(3)
        with patch("ai_marketplace_monitor.ntfy.requests.post", side_effect=[ok, bad, ok]) as post:
            assert (
                cfg.notify(
                    listings, [rating()] * 3, [NotificationStatus.NOT_NOTIFIED] * 3, context=ctx
                )
                is True
            )
        assert post.call_count == 2
        assert ctx.sent == {("facebook", "1000")}

    def test_distance_comes_from_the_context(self: "TestFanOut") -> None:
        cfg = NtfyNotificationConfig(
            name="ntfy", ntfy_server="https://ntfy.example.com", ntfy_topic="listings"
        )
        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            cfg.notify(
                [make_listing()],
                [rating()],
                [NotificationStatus.NOT_NOTIFIED],
                context=NotifyContext(notify_threshold=4, home_location="35.5, -79.5"),
            )
        message = post.call_args.kwargs["json"]["message"]
        assert "notify ≥ 4" in message
        # Asheboro NC is about 30 miles from that point; the exact number is
        # geonamescache's business, the presence of one is this test's.
        assert " mi · Asheboro, NC" in message


# ---------------------------------------------------------------------------
# The other backends
# ---------------------------------------------------------------------------
class TestOtherBackends:
    def test_pushover_puts_the_listing_in_url_and_the_app_in_the_body(
        self: "TestOtherBackends",
    ) -> None:
        cfg = PushoverNotificationConfig(
            name="pushover",
            pushover_user_key="u" * 30,
            pushover_api_token="a" * 30,
            app_url=APP_URL,
        )
        notice = build_listing_notice(make_listing(), rating(), app_url=APP_URL)
        with patch.object(cfg, "_post", return_value=True) as post:
            assert cfg.send_listing(notice) is True
        params = post.call_args.args[0]
        assert params["url"] == notice.listing_url
        assert params["url_title"] == "Open listing"
        assert f'<a href="{notice.app_link}">Open in AIMM</a>' in params["message"]
        assert "<br>" in params["message"]
        assert params["priority"] == 1  # a 5/5 breaks through quiet hours

    def test_pushbullet_sends_a_link_push(self: "TestOtherBackends") -> None:
        cfg = PushbulletNotificationConfig(
            name="pushbullet", pushbullet_token="o.abcdefgh", app_url=APP_URL
        )
        notice = build_listing_notice(make_listing(), rating(), app_url=APP_URL)
        client = MagicMock()
        with patch.object(cfg, "_client", return_value=client):
            assert cfg.send_listing(notice) is True
        title, url, body = client.push_link.call_args.args
        assert title == notice.title
        assert url == notice.listing_url
        assert notice.app_link in body

    def test_default_backend_spells_both_links_out(self: "TestOtherBackends") -> None:
        """Telegram and anything added later still get one message per listing."""
        cfg = PushNotificationConfig(name="generic", app_url=APP_URL)
        notice = build_listing_notice(make_listing(), rating(), app_url=APP_URL)
        with patch.object(cfg, "send_message", return_value=True) as send:
            cfg.send_listing(notice)
        body = send.call_args.kwargs["message"]
        assert body.endswith(f"{notice.listing_url}\n{notice.app_link}")

    def test_description_is_off_unless_asked_for(self: "TestOtherBackends") -> None:
        listing = make_listing()
        assert PushNotificationConfig(name="g").description_for(listing) == ""
        assert (
            PushNotificationConfig(name="g", with_description=True).description_for(listing)
            == listing.description
        )
        clipped = PushNotificationConfig(name="g", with_description=10).description_for(listing)
        assert clipped == listing.description[:10] + "..."


# ---------------------------------------------------------------------------
# The bookkeeping that decides "have I already told you about this one"
# ---------------------------------------------------------------------------
class TestUserBookkeeping:
    def test_every_listing_is_recorded_separately(
        self: "TestUserBookkeeping", tmp_path: Any
    ) -> None:
        """Fan-out must not cost the per-listing USER_NOTIFIED rows.

        The reminder logic (`remind`) reads one cache row per (marketplace,
        id, user); a batch that recorded one row for the whole search would
        make every listing share a reminder clock.
        """
        from diskcache import Cache  # type: ignore

        from ai_marketplace_monitor.user import User, UserConfig

        local = Cache(str(tmp_path / "cache"))
        user = User(
            UserConfig(
                name="me",
                ntfy_server="https://ntfy.example.com",
                ntfy_topic="listings",
                app_url=APP_URL,
            )
        )
        listings = [make_listing(id=str(2000 + i)) for i in range(3)]
        for listing in listings:
            assert user.notification_status(listing, local) == NotificationStatus.NOT_NOTIFIED

        with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
            post.return_value = MagicMock(status_code=200)
            user.notify(
                listings,
                [rating()] * 3,
                item_config(),
                local_cache=local,
                context=NotifyContext(notify_threshold=4),
            )

        assert post.call_count == 3
        for listing in listings:
            assert user.notification_status(listing, local) == NotificationStatus.NOTIFIED
            assert local.get(user.notified_key(listing)) is not None
        local.close()


def test_markdown_format_gets_hard_line_breaks() -> None:
    """A lone newline is not a line break in Markdown; the message needs both."""
    cfg = NtfyNotificationConfig(
        name="ntfy",
        ntfy_server="https://ntfy.example.com",
        ntfy_topic="listings",
        message_format="markdown",
    )
    notice = build_listing_notice(make_listing(), rating(), distance_mi=12.4)
    with patch("ai_marketplace_monitor.ntfy.requests.post") as post:
        post.return_value = MagicMock(status_code=200)
        cfg.send_listing(notice)
    body = post.call_args.kwargs["json"]
    assert body["markdown"] is True
    assert "  \n" in body["message"]
    assert body["message"].replace("  \n", "\n") == notice.message


def test_a_listing_no_backend_could_send_is_not_recorded(tmp_path: Any) -> None:
    """The bookkeeping follows the sends, not the batch.

    Before, one successful message marked the whole search notified; a listing
    that never made it would then never be retried.
    """
    from diskcache import Cache  # type: ignore

    from ai_marketplace_monitor.user import User, UserConfig

    local = Cache(str(tmp_path / "cache"))
    user = User(
        UserConfig(
            name="me",
            ntfy_server="https://ntfy.example.com",
            ntfy_topic="listings",
            max_retries=1,
        )
    )
    listings = [make_listing(id="3001"), make_listing(id="3002")]
    ok = MagicMock(status_code=200)
    bad = MagicMock()
    bad.raise_for_status.side_effect = RuntimeError("500")
    with patch("ai_marketplace_monitor.ntfy.requests.post", side_effect=[ok, bad]):
        user.notify(listings, [rating()] * 2, item_config(), local_cache=local)

    assert user.notification_status(listings[0], local) == NotificationStatus.NOTIFIED
    assert user.notification_status(listings[1], local) == NotificationStatus.NOT_NOTIFIED
    local.close()
