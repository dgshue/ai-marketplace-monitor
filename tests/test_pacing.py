"""Request pacing, per-item scheduling fields, and block-cooldown behaviour."""

import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ai_marketplace_monitor.facebook import (
    FacebookItemConfig,
    FacebookMarketplaceConfig,
    detect_block_signal,
)
from ai_marketplace_monitor.marketplace import (
    DEFAULT_BLOCK_COOLDOWN,
    DEFAULT_REQUEST_DELAY,
    BlockState,
    BlockTracker,
    ItemConfig,
    Marketplace,
    MarketplaceConfig,
    block_cooldown_for,
)
from ai_marketplace_monitor.utils import read_monitor_state, write_monitor_state

# ---------------------------------------------------------------------------
# request_delay parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ([6, 15], [6, 15]),
        ((6, 15), [6, 15]),
        (10, [10, 10]),
        ("10", [10, 10]),
        (["5", "20"], [5, 20]),
        ("30s", [30, 30]),
        (["30s", "2m"], [30, 120]),
        ([0, 0], [0, 0]),
    ],
)
def test_request_delay_accepts_numbers_and_durations(value: Any, expected: Any) -> None:
    """A single value, a pair, and human durations all normalize to [min, max]."""
    assert (
        ItemConfig(name="x", search_phrases=["y"], request_delay=value).request_delay == expected
    )


@pytest.mark.parametrize(
    "value",
    [
        [6, 15, 30],  # three values is not a range
        [15, 6],  # inverted
        [-1, 5],  # negative
        "not a duration",
        [],
    ],
)
def test_request_delay_rejects_nonsense(value: Any) -> None:
    with pytest.raises(ValueError):
        ItemConfig(name="x", search_phrases=["y"], request_delay=value)


def test_request_delay_defaults_to_none_and_the_marketplace_default() -> None:
    """Unset means "inherit"; the effective default lives in one constant."""
    assert ItemConfig(name="x", search_phrases=["y"]).request_delay is None
    assert DEFAULT_REQUEST_DELAY == (6, 15)


# ---------------------------------------------------------------------------
# block_cooldown parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2h", 2 * 60 * 60),
        ("30m", 30 * 60),
        ("1d", 24 * 60 * 60),
        (900, 900),
    ],
)
def test_block_cooldown_accepts_durations(value: Any, expected: int) -> None:
    assert MarketplaceConfig(name="facebook", block_cooldown=value).block_cooldown == expected


@pytest.mark.parametrize("value", ["30s", 30, "gibberish", True])
def test_block_cooldown_rejects_too_short_or_unparsable(value: Any) -> None:
    with pytest.raises(ValueError):
        MarketplaceConfig(name="facebook", block_cooldown=value)


def test_block_cooldown_default_is_two_hours() -> None:
    assert MarketplaceConfig(name="facebook").block_cooldown is None
    assert DEFAULT_BLOCK_COOLDOWN == 2 * 60 * 60


# ---------------------------------------------------------------------------
# search_interval / max_search_interval still take human durations
# ---------------------------------------------------------------------------


def test_search_intervals_take_human_durations() -> None:
    # Built through **kwargs: the fields are typed int after validation, but
    # a config file legitimately writes '2h' and the handler converts it.
    fields: Dict[str, Any] = {"search_interval": "2h", "max_search_interval": "4h"}
    item = ItemConfig(name="car", search_phrases=["acura"], **fields)
    assert item.search_interval == 2 * 60 * 60
    assert item.max_search_interval == 4 * 60 * 60


# ---------------------------------------------------------------------------
# effective delay range: item wins, then marketplace, then the default
# ---------------------------------------------------------------------------


def _marketplace(config: FacebookMarketplaceConfig) -> Marketplace:
    market: Marketplace = Marketplace.__new__(Marketplace)
    market.name = "facebook"
    market.logger = None
    market.config = config
    return market


def test_request_delay_range_precedence() -> None:
    plain = FacebookMarketplaceConfig(name="facebook")
    assert _marketplace(plain).request_delay_range() == DEFAULT_REQUEST_DELAY

    paced = FacebookMarketplaceConfig(name="facebook", request_delay=[20, 40])
    assert _marketplace(paced).request_delay_range() == (20, 40)

    item = FacebookItemConfig(name="car", search_phrases=["acura"], request_delay=[60, 90])
    assert _marketplace(paced).request_delay_range(item) == (60, 90)

    unset_item = FacebookItemConfig(name="pc", search_phrases=["desktop"])
    assert _marketplace(paced).request_delay_range(unset_item) == (20, 40)


def test_pace_sleeps_inside_the_configured_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pause is randomized, not fixed -- a metronome is a bot signature."""
    slept: List[float] = []
    monkeypatch.setattr("ai_marketplace_monitor.marketplace.time.sleep", slept.append)
    market = _marketplace(FacebookMarketplaceConfig(name="facebook", request_delay=[6, 15]))
    for _ in range(50):
        market.pace()
    assert len(slept) == 50
    assert all(6 <= x <= 15 for x in slept)
    assert len(set(slept)) > 1


def test_pace_disabled_by_a_zero_range(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: List[float] = []
    monkeypatch.setattr("ai_marketplace_monitor.marketplace.time.sleep", slept.append)
    market = _marketplace(FacebookMarketplaceConfig(name="facebook", request_delay=[0, 0]))
    assert market.pace() == 0.0
    assert not slept


# ---------------------------------------------------------------------------
# block detection signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,title,body",
    [
        # kevinzg/facebook-scraper temp_ban_titles, verbatim
        ("https://www.facebook.com/marketplace/x", "You're Temporarily Blocked", ""),
        ("https://www.facebook.com/marketplace/x", "You Can't Use This Feature at the Moment", ""),
        ("https://www.facebook.com/marketplace/x", "You can't use this feature right now", ""),
        # curly apostrophe, which facebook also serves
        ("https://www.facebook.com/marketplace/x", "You\u2019re Temporarily Blocked", ""),
        # body copy with an innocuous title
        ("https://www.facebook.com/marketplace/x", "Facebook", "We suspended your account"),
        ("https://www.facebook.com/marketplace/x", "Facebook", "Your account has been disabled"),
        (
            "https://www.facebook.com/marketplace/x",
            "Facebook",
            "We saw unusual activity on your account",
        ),
        # redirects
        ("https://www.facebook.com/checkpoint/1234/", "Facebook", ""),
        ("https://www.facebook.com/login.php?next=%2Fmarketplace", "Log in to Facebook", ""),
    ],
)
def test_detect_block_signal_fires(url: str, title: str, body: str) -> None:
    assert detect_block_signal(url, title, body) is not None


def test_detect_block_signal_stays_quiet_on_a_normal_page() -> None:
    assert (
        detect_block_signal(
            "https://www.facebook.com/marketplace/asheboro/search?query=acura",
            "Marketplace",
            "Acura TL 2012 $8,995 Asheboro, NC",
        )
        is None
    )


def test_login_redirect_is_ignorable_while_logging_in() -> None:
    """login() navigates to a /login/ URL on purpose; only the copy counts there."""
    url = "https://www.facebook.com/login/device-based/regular/login/"
    assert detect_block_signal(url, "Log in to Facebook", "") is not None
    assert detect_block_signal(url, "Log in to Facebook", "", check_login_redirect=False) is None
    # a real block interstitial is still caught during login
    assert (
        detect_block_signal(url, "You're Temporarily Blocked", "", check_login_redirect=False)
        is not None
    )


# ---------------------------------------------------------------------------
# cooldown maths and the tracker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "strikes,expected",
    [(0, 7200), (1, 7200), (2, 14400), (3, 28800), (4, 28800), (99, 28800)],
)
def test_block_cooldown_escalates_then_flattens(strikes: int, expected: int) -> None:
    assert block_cooldown_for(DEFAULT_BLOCK_COOLDOWN, strikes) == expected


def test_block_state_remaining_never_goes_negative() -> None:
    state = BlockState("facebook", "title", detected_at=100.0, until=200.0)
    assert state.remaining(now=150.0) == 50.0
    assert state.is_active(now=150.0)
    assert state.remaining(now=999.0) == 0.0
    assert not state.is_active(now=999.0)


def test_block_tracker_lifecycle() -> None:
    tracker = BlockTracker()
    assert tracker.active("facebook", now=0.0) is None
    assert tracker.snapshot(now=0.0) == {}

    state = tracker.block("facebook", "page title", base_cooldown=600, now=1000.0)
    assert state.until == 1600.0
    assert state.strikes == 1
    assert tracker.active("facebook", now=1500.0) is state
    assert tracker.active("ebay", now=1500.0) is None

    # expired: no longer active, but not forgotten -- the strike count is what
    # makes the next block back off further.
    assert tracker.active("facebook", now=2000.0) is None
    assert tracker.snapshot(now=2000.0) == {}

    second = tracker.block("facebook", "checkpoint", base_cooldown=600, now=2000.0)
    assert second.strikes == 2
    assert second.until == 2000.0 + 1200

    snap = tracker.snapshot(now=2100.0)
    assert set(snap) == {"facebook"}
    assert snap["facebook"]["reason"] == "checkpoint"
    assert snap["facebook"]["remaining"] == 1100.0
    assert snap["facebook"]["strikes"] == 2


def test_block_tracker_clear() -> None:
    tracker = BlockTracker()
    tracker.block("facebook", "x", base_cooldown=600, now=0.0)
    tracker.block("depop", "y", base_cooldown=600, now=0.0)

    assert tracker.clear("nothing-here") == []
    assert tracker.clear("depop") == ["depop"]
    assert tracker.active("depop", now=1.0) is None
    assert tracker.active("facebook", now=1.0) is not None

    assert tracker.clear() == ["facebook"]
    assert tracker.snapshot(now=1.0) == {}

    # a cleared marketplace starts over at one strike
    assert tracker.block("facebook", "z", base_cooldown=600, now=10.0).strikes == 1


# ---------------------------------------------------------------------------
# persistence: a restart must not silently resume searching
# ---------------------------------------------------------------------------


def test_monitor_state_file_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "monitor-state.json"
    assert read_monitor_state(target) == {}  # nothing written yet

    assert write_monitor_state({"paused": True, "blocked": {}}, target)
    assert read_monitor_state(target) == {"paused": True, "blocked": {}}

    # a truncated or hand-mangled file reads as "no remembered state" rather
    # than crashing the monitor on startup
    target.write_text("{not json", encoding="utf-8")
    assert read_monitor_state(target) == {}
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_monitor_state(target) == {}


def test_monitor_state_write_is_atomic(tmp_path: Path) -> None:
    """No stray .tmp is left behind, so the next read sees a whole file."""
    target = tmp_path / "monitor-state.json"
    write_monitor_state({"paused": False}, target)
    assert [p.name for p in tmp_path.iterdir()] == ["monitor-state.json"]


def test_block_tracker_survives_a_restart() -> None:
    before = BlockTracker()
    before.block("facebook", "page title", base_cooldown=3600, now=1000.0)

    after = BlockTracker()
    after.restore(before.to_dict())
    live = after.active("facebook", now=2000.0)
    assert live is not None
    assert live.reason == "page title"
    assert live.remaining(now=2000.0) == 2600.0


def test_restored_expired_cooldown_still_counts_as_a_strike() -> None:
    """A block that lapsed while the process was down must not reset backoff."""
    before = BlockTracker()
    before.block("facebook", "checkpoint", base_cooldown=600, now=0.0)

    after = BlockTracker()
    after.restore(before.to_dict())
    assert after.active("facebook", now=10_000.0) is None
    assert after.block("facebook", "again", base_cooldown=600, now=10_000.0).strikes == 2


def test_monitor_restores_pause_and_cooldown_before_any_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart honors both the pause and the cooldown on the very first run.

    start_monitor runs every job once immediately, so "we will pick it up at
    the next scheduled run" is not good enough -- the state has to be in force
    before that first pass, which is why it is restored in the constructor.
    """
    # Imported here: pulling in the monitor drags in playwright and every
    # notification backend, which the rest of this module does not need.
    from ai_marketplace_monitor import utils as utils_module
    from ai_marketplace_monitor.monitor import MarketplaceMonitor

    monkeypatch.setattr(utils_module, "monitor_state_file", tmp_path / "monitor-state.json")

    def bare_monitor() -> Any:
        monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
        monitor.logger = None
        monitor.web_paused = threading.Event()
        monitor.block_tracker = BlockTracker()
        return monitor

    before_restart = bare_monitor()
    before_restart.set_paused(True)
    before_restart.block_tracker.block("facebook", "page title", base_cooldown=3600)
    before_restart._persist_state()

    restarted = bare_monitor()
    restarted._restore_persisted_state()
    assert restarted.web_paused.is_set()
    assert restarted.block_tracker.active("facebook") is not None

    searched: List[Any] = []
    restarted.set_web_activity = lambda *a, **k: None  # type: ignore[method-assign]
    restarted._search_item_impl = lambda *a: searched.append(a)  # type: ignore[method-assign]
    market_config = MarketplaceConfig(name="facebook")
    item = ItemConfig(name="pc", search_phrases=["desktop"])

    restarted.search_item(market_config, None, item)
    assert searched == [], "a restored pause must block the first run"

    restarted.set_paused(False)
    restarted.search_item(market_config, None, item)
    assert searched == [], "a restored cooldown must block the first run"

    restarted.clear_block("facebook")
    restarted.search_item(market_config, None, item)
    assert len(searched) == 1

    # every transition reached disk, so the next restart sees the truth
    persisted = read_monitor_state(tmp_path / "monitor-state.json")
    assert persisted["paused"] is False
    assert persisted["blocked"] == {}


def test_block_tracker_restore_ignores_junk() -> None:
    tracker = BlockTracker()
    tracker.restore(None)
    tracker.restore({"facebook": "not a dict"})
    tracker.restore({"facebook": {"until": "not a number"}})
    assert tracker.snapshot(now=0.0) == {}
