"""Pause must interrupt a search that is already running.

`web_paused` used to be consulted only at the top of `search_item`, so a pass
that was half-way through a few hundred listings kept calling the AI for hours
after the user pressed Pause -- about 25 seconds per listing on a local Ollama.
The same held for a marketplace that started blocking mid-pass.
"""

from __future__ import annotations

import threading
from typing import Any, ClassVar, Generator, List

import pytest

from ai_marketplace_monitor import monitor as monitor_module
from ai_marketplace_monitor.ai import AIResponse
from ai_marketplace_monitor.ebay import EbayItemConfig, EbayMarketplaceConfig
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.marketplace import BlockTracker
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.notification import NotificationStatus


def make_listing(index: int) -> Listing:
    return Listing(
        marketplace="ebay",
        name="",
        id=str(index),
        title=f"listing {index}",
        image="",
        price="$100",
        post_url=f"https://www.ebay.com/itm/{index}",
        location="US",
        seller="",
        condition="Pre-Owned",
        description="",
    )


class FakeMarketplace:
    """Yields a fixed list of listings and records the session save."""

    def __init__(self: "FakeMarketplace", count: int) -> None:
        self.listings = [make_listing(i) for i in range(count)]
        self.saved = 0

    def search(self: "FakeMarketplace", item_config: Any) -> Generator[Listing, None, None]:
        yield from self.listings

    def save_browser_state(self: "FakeMarketplace") -> None:
        self.saved += 1


class FakeAgentConfig:
    name = "fake-ai"


class FakeAgent:
    """Counts evaluations and can trip the pause the way a user would."""

    def __init__(self: "FakeAgent", after_first: Any = None) -> None:
        self.config = FakeAgentConfig()
        self.calls: List[str] = []
        self.after_first = after_first

    def evaluate(self: "FakeAgent", listing: Listing, *args: Any, **kwargs: Any) -> AIResponse:
        self.calls.append(listing.id)
        if self.after_first is not None and len(self.calls) == 1:
            self.after_first()
        return AIResponse(5, "great", "fake-ai")


class FakeUser:
    """Never-notified, never-notifies: keeps the cache and the network out."""

    notified: ClassVar[List[Any]] = []

    def __init__(self: "FakeUser", config: Any, logger: Any = None) -> None:
        pass

    def notification_status(self: "FakeUser", listing: Listing) -> NotificationStatus:
        return NotificationStatus.NOT_NOTIFIED

    def notify(self: "FakeUser", listings: List[Listing], *args: Any, **kwargs: Any) -> None:
        FakeUser.notified.extend(listings)


class FakeConfig:
    def __init__(self: "FakeConfig") -> None:
        self.user = {"u1": object()}


def build_monitor() -> MarketplaceMonitor:
    """A monitor without Playwright: __init__ would start a browser driver."""
    mon = MarketplaceMonitor.__new__(MarketplaceMonitor)
    mon.config = FakeConfig()  # type: ignore[assignment]
    mon.logger = None
    mon.web_paused = threading.Event()
    mon.block_tracker = BlockTracker()
    mon.ai_agents = []
    return mon


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end of a normal pass sleeps 5s; tests do not need to."""
    monkeypatch.setattr(monitor_module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(monitor_module, "User", FakeUser)
    FakeUser.notified = []


def run_pass(mon: MarketplaceMonitor, market: FakeMarketplace) -> None:
    mon._search_item_impl(
        EbayMarketplaceConfig(name="ebay"),
        market,  # type: ignore[arg-type]
        EbayItemConfig(name="gpu", search_phrases=["rtx 3080"]),  # type: ignore[arg-type]
    )


def test_pause_stops_the_pass_after_the_current_listing() -> None:
    mon = build_monitor()
    market = FakeMarketplace(50)
    agent = FakeAgent(after_first=lambda: mon.web_paused.set())
    mon.ai_agents = [agent]  # type: ignore[list-item]

    run_pass(mon, market)

    # One rating, not fifty. The pause is read before every AI call, so the
    # listing already in flight finishes and nothing else starts.
    assert agent.calls == ["0"]
    # The session is saved on the way out, exactly as a completed pass does.
    assert market.saved == 1


def test_a_block_mid_pass_stops_it_too() -> None:
    mon = build_monitor()
    market = FakeMarketplace(50)
    agent = FakeAgent(after_first=lambda: mon.block_tracker.block("ebay", "captcha", 3600))
    mon.ai_agents = [agent]  # type: ignore[list-item]

    run_pass(mon, market)

    assert agent.calls == ["0"]
    assert market.saved == 1


def test_an_uninterrupted_pass_still_rates_everything() -> None:
    mon = build_monitor()
    market = FakeMarketplace(5)
    agent = FakeAgent()
    mon.ai_agents = [agent]  # type: ignore[list-item]

    run_pass(mon, market)

    assert len(agent.calls) == 5
    assert len(FakeUser.notified) == 5


def test_pause_before_the_first_listing_costs_nothing() -> None:
    mon = build_monitor()
    mon.web_paused.set()
    market = FakeMarketplace(5)
    agent = FakeAgent()
    mon.ai_agents = [agent]  # type: ignore[list-item]

    run_pass(mon, market)

    assert agent.calls == []
