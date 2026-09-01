"""Depop backend: scrape the server-rendered search page, no login.

Extraction ported from secondhand-mcp (MIT): Depop's public JSON API
(webapi.depop.com) 403s non-browser callers, so tiles come from the rendered
DOM, keyed off the stable /products/<slug> href.
"""

from __future__ import annotations

from typing import Any, Dict
from urllib.parse import quote

from .browser_market import BrowserTileMarketplace
from .listing import Listing


class DepopMarketplace(BrowserTileMarketplace):
    display_name = "Depop"
    anchor_selector = 'a[href*="/products/"]'

    # Ported near-verbatim from secondhand-mcp's depop.ts page.evaluate.
    extract_js = """
    (() => {
      const seen = new Set();
      const out = [];
      document.querySelectorAll('a[href*="/products/"]').forEach((a) => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/\\/products\\/([^/?#]+)/);
        if (!m) return;
        const slug = m[1];
        if (seen.has(slug)) return;
        seen.add(slug);
        const card = a.closest('li') || a.parentElement;
        const img = a.querySelector('img') || (card && card.querySelector('img'));
        const txt = ((card && card.innerText) || '').replace(/\\s+/g, ' ').trim();
        const prices = txt.match(/[£$€]\\s?\\d[\\d.,]*/g) || [];
        out.push({
          slug,
          label: a.getAttribute('aria-label') || (img && img.getAttribute('alt')) || '',
          img: (img && (img.getAttribute('src') || (img.getAttribute('srcset') || '').split(' ')[0])) || '',
          prices,
        });
      });
      return out;
    })()
    """

    def search_url(self: "DepopMarketplace", phrase: str) -> str:
        return f"https://www.depop.com/search/?q={quote(phrase)}"

    def tile_to_listing(self: "DepopMarketplace", tile: Dict[str, Any]) -> Listing | None:
        slug = str(tile.get("slug") or "").strip()
        if not slug:
            return None
        title = str(tile.get("label") or "").strip() or slug.replace("-", " ")
        prices = tile.get("prices") or []
        # "was £X now £Y" renders both; the last match is the current price.
        price = str(prices[-1]).replace(" ", "") if prices else ""
        return Listing(
            marketplace="depop",
            name="",
            id=slug,
            title=title,
            image=str(tile.get("img") or ""),
            price=price,
            post_url=f"https://www.depop.com/products/{slug}",
            location="",
            seller="",
            condition="",
            description="",
        )
