import pytest

from ai_marketplace_monitor.ai import OllamaBackend, OllamaConfig
from ai_marketplace_monitor.depop import DepopMarketplace
from ai_marketplace_monitor.ebay import EbayMarketplace
from ai_marketplace_monitor.facebook import (
    FacebookItemConfig,
    FacebookMarketplace,
    FacebookMarketplaceConfig,
)
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.marketplace import MARKETPLACE_DISPLAY_NAMES
from ai_marketplace_monitor.poshmark import PoshmarkMarketplace


@pytest.mark.skipif(True, reason="Condition met, skipping this test")
def test_ai(
    ollama_config: OllamaConfig,
    item_config: FacebookItemConfig,
    marketplace_config: FacebookMarketplaceConfig,
    listing: Listing,
) -> None:
    ai = OllamaBackend(ollama_config)
    # ai.config = ollama_config
    res = ai.evaluate(listing, item_config, marketplace_config)
    assert res.score >= 1 and res.score <= 5


def test_prompt(
    ollama: OllamaBackend,
    listing: Listing,
    item_config: FacebookItemConfig,
    marketplace_config: FacebookMarketplaceConfig,
) -> None:
    prompt = ollama.get_prompt(listing, item_config, marketplace_config)
    assert item_config.name in prompt
    assert (item_config.description or "something weird") in prompt
    assert str(item_config.min_price) in prompt
    assert str(item_config.max_price) in prompt

    assert listing.title in prompt
    assert listing.condition in prompt
    assert listing.price in prompt
    assert listing.post_url in prompt


def test_extra_prompt(
    ollama: OllamaBackend,
    listing: Listing,
    item_config: FacebookItemConfig,
    marketplace_config: FacebookMarketplaceConfig,
) -> None:
    marketplace_config.extra_prompt = "This is an extra prompt"
    prompt = ollama.get_prompt(listing, item_config, marketplace_config)
    assert "extra prompt" in prompt
    #
    item_config.extra_prompt = "This overrides marketplace prompt"
    prompt = ollama.get_prompt(listing, item_config, marketplace_config)
    assert "extra prompt" not in prompt
    assert "overrides marketplace prompt" in prompt
    #
    assert "Great deal: Fully matches" in prompt
    item_config.rating_prompt = "something else"
    prompt = ollama.get_prompt(listing, item_config, marketplace_config)
    assert "Great deal: Fully matches" not in prompt
    assert "something else" in prompt
    #
    assert "Evaluate how well this listing" in prompt
    marketplace_config.prompt = "myprompt"
    prompt = ollama.get_prompt(listing, item_config, marketplace_config)
    assert "Evaluate how well this listing" not in prompt
    assert "myprompt" in prompt


def test_prompt_names_the_listings_own_marketplace(
    ollama: OllamaBackend,
    listing: Listing,
    item_config: FacebookItemConfig,
    marketplace_config: FacebookMarketplaceConfig,
) -> None:
    """An eBay listing must not be described to the AI as a Facebook one.

    This is the whole bug: the prompt said "from Facebook Marketplace" for
    every listing, and the model rated eBay results 1 with the reasoning
    "it is on eBay, not Facebook Marketplace".
    """
    listing.marketplace = "ebay"
    listing.post_url = "https://www.ebay.com/itm/227501449913"
    prompt = ollama.get_prompt(listing, item_config, marketplace_config)
    assert "from eBay." in prompt
    assert "facebook" not in prompt.lower()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("facebook", "from Facebook Marketplace."),
        ("ebay", "from eBay."),
        ("depop", "from Depop."),
        ("poshmark", "from Poshmark."),
    ],
)
def test_prompt_names_every_known_source(
    ollama: OllamaBackend,
    listing: Listing,
    item_config: FacebookItemConfig,
    marketplace_config: FacebookMarketplaceConfig,
    key: str,
    expected: str,
) -> None:
    listing.marketplace = key
    assert expected in ollama.get_prompt(listing, item_config, marketplace_config)


def test_prompt_is_neutral_for_an_unknown_source(
    ollama: OllamaBackend,
    listing: Listing,
    item_config: FacebookItemConfig,
    marketplace_config: FacebookMarketplaceConfig,
) -> None:
    """An unnamed source says nothing rather than guessing one."""
    listing.marketplace = "craigslist"
    prompt = ollama.get_prompt(listing, item_config, marketplace_config)
    assert f"A user wants to buy a {item_config.name}. Search phrases" in prompt
    assert "Facebook" not in prompt


def test_every_backend_agrees_with_the_display_name_table() -> None:
    """One table names the sources; a backend must not invent its own name.

    The prompt resolves the name from Listing.marketplace, and the logs
    resolve it from the backend class. They have to be the same string.
    """
    for backend, key in (
        (FacebookMarketplace, "facebook"),
        (EbayMarketplace, "ebay"),
        (DepopMarketplace, "depop"),
        (PoshmarkMarketplace, "poshmark"),
    ):
        assert backend.display_name == MARKETPLACE_DISPLAY_NAMES[key]
