"""Ollama num_ctx pinning and stale Chromium profile-lock cleanup.

Background: on a shared Ollama server, two clients asking for different
``num_ctx`` values make Ollama reload the whole model on every alternation,
which pushed rating latency past the request timeout and eventually wedged
the scheduler. The OpenAI-compatible ``/v1`` endpoint ignores ``options``, so
pinning ``num_ctx`` requires Ollama's native ``/api/chat``.
"""

import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from ai_marketplace_monitor import ai as ai_module
from ai_marketplace_monitor.ai import OllamaBackend, OllamaConfig
from ai_marketplace_monitor.monitor import MarketplaceMonitor


def _config(**kwargs: Any) -> OllamaConfig:
    base: Dict[str, Any] = {
        "name": "ollama",
        "base_url": "http://ollama:11434/v1",
        "model": "qwen3.6:35b-a3b",
    }
    base.update(kwargs)
    return OllamaConfig(**base)


def test_num_ctx_defaults_to_none() -> None:
    assert _config().num_ctx is None


@pytest.mark.parametrize("bad", [0, -1, "32768", 1.5])
def test_num_ctx_must_be_positive_int(bad: Any) -> None:
    with pytest.raises(ValueError, match="num_ctx"):
        _config(num_ctx=bad)


# The parameter is deliberately not named `base_url`: pytest-playwright pulls
# in pytest-base-url, whose session-scoped `base_url` fixture shadows a
# parametrize argname of the same name and fails the test with a ScopeMismatch.
@pytest.mark.parametrize(
    "configured_url, expected",
    [
        ("http://ollama:11434/v1", "http://ollama:11434/api/chat"),
        ("http://ollama:11434/v1/", "http://ollama:11434/api/chat"),
        ("http://ollama:11434", "http://ollama:11434/api/chat"),
        ("http://ollama:11434/", "http://ollama:11434/api/chat"),
    ],
)
def test_native_chat_url_strips_v1(configured_url: str, expected: str) -> None:
    backend = OllamaBackend(_config(base_url=configured_url))
    assert backend._native_chat_url() == expected


def test_request_completion_uses_native_api_when_num_ctx_set(monkeypatch: Any) -> None:
    captured: Dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            captured["raised"] = True

        def json(self) -> Dict[str, Any]:
            return {
                "message": {"role": "assistant", "thinking": "...", "content": "Rating 4: ok"},
                "done": True,
            }

    def fake_post(url: str, json: Dict[str, Any], timeout: float) -> FakeResponse:
        captured.update(url=url, body=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(ai_module.httpx, "post", fake_post)
    backend = OllamaBackend(_config(num_ctx=32768, timeout=120))
    # Must not need (or touch) the OpenAI client at all.
    backend.client = object()

    answer, raw = backend._request_completion("prompt text")

    assert answer == "Rating 4: ok"
    assert raw["done"] is True
    assert captured["url"] == "http://ollama:11434/api/chat"
    assert captured["timeout"] == 120
    assert captured["raised"] is True
    body = captured["body"]
    assert body["model"] == "qwen3.6:35b-a3b"
    assert body["stream"] is False
    assert body["options"] == {"num_ctx": 32768}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1] == {"role": "user", "content": "prompt text"}


def test_request_completion_keeps_openai_path_without_num_ctx(monkeypatch: Any) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("native API must not be used when num_ctx is unset")

    monkeypatch.setattr(ai_module.httpx, "post", boom)

    calls: Dict[str, Any] = {}

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            calls.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Rating 2: meh"))]
            )

    backend = OllamaBackend(_config())
    backend.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    answer, raw = backend._request_completion("prompt text")

    assert answer == "Rating 2: meh"
    assert calls["model"] == "qwen3.6:35b-a3b"
    assert calls["stream"] is False
    assert raw.choices[0].message.content == "Rating 2: meh"


def _monitor_stub() -> Any:
    # _clear_stale_profile_lock only needs .logger; avoid starting Playwright.
    return SimpleNamespace(logger=None)


def _make_lock(profile_dir: Path, target: str) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, profile_dir / "SingletonLock")
        os.symlink("1234567890", profile_dir / "SingletonCookie")
        os.symlink("/tmp/org.chromium.Chromium.x/SingletonSocket", profile_dir / "SingletonSocket")
    except (
        OSError,
        NotImplementedError,
    ) as e:  # pragma: no cover - Windows without symlink rights
        pytest.skip(f"symlinks unavailable here: {e}")


def test_stale_lock_from_other_host_is_cleared(tmp_path: Path) -> None:
    profile = tmp_path / "browser-profile"
    _make_lock(profile, "f5ada456d486-1402")

    MarketplaceMonitor._clear_stale_profile_lock(_monitor_stub(), profile)

    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        assert not (profile / name).exists() and not (profile / name).is_symlink()


def test_lock_from_this_host_is_left_for_chromium(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "thishost")
    profile = tmp_path / "browser-profile"
    _make_lock(profile, "thishost-99999")

    MarketplaceMonitor._clear_stale_profile_lock(_monitor_stub(), profile)

    assert (profile / "SingletonLock").is_symlink()


def test_no_lock_is_a_noop(tmp_path: Path) -> None:
    profile = tmp_path / "browser-profile"
    profile.mkdir()

    MarketplaceMonitor._clear_stale_profile_lock(_monitor_stub(), profile)

    assert list(profile.iterdir()) == []
