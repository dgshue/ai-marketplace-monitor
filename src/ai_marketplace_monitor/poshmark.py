"""Poshmark backend — scrapes the server-rendered search page in the shared
headed Chromium. No login. Extraction ported from secondhand-mcp (MIT), keyed
off the stable /listing/<slug> href rather than styled class names."""

from __future__ import annotations

from typing import Any, Dict
from urllib.parse import quote

from .browser_market import BrowserTileMarketplace
from .listing import Listing


class PoshmarkMarketplace(BrowserTileMarketplace):
    display_name = "Poshmark"
    anchor_selector = 'a[href*="/listing/"]'

    # Ported near-verbatim from secondhand-mcp's poshmark.ts page.evaluate.
    extract_js = """
    (() => {
      const seen = new Set();
      const out = [];
      document.querySelectorAll('a[href*="/listing/"]').forEach((a) => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/\\/listing\\/([^/?#]+)/);
        if (!m) return;
        const slug = m[1];
        const idm = slug.match(/([a-f0-9]{24})$/i);
        const id = idm ? idm[1] : slug;
        if (seen.has(id)) return;
        seen.add(id);
        let box = a;
        let hops = 0;
        while (box && hops < 5 && !/[$]\\s?\\d/.test(box.innerText || '')) {
          box = box.parentElement;
          hops++;
        }
        const txt = ((box && box.innerText) || '').replace(/\\s+/g, ' ').trim();
        const img = a.querySelector('img') || (box && box.querySelector('img'));
        out.push({
          id,
          slug,
          title: (img && img.getAttribute('alt')) || '',
          img: (img && img.getAttribute('src')) || '',
          prices: (txt.match(/[$]\\s?\\d[\\d.,]*/g) || []).slice(0, 3),
        });
      });
      return out;
    })()
    """

    def search_url(self: "PoshmarkMarketplace", phrase: str) -> str:
        return f"https://poshmark.com/search?query={quote(phrase)}&type=listings"

    def tile_to_listing(self: "PoshmarkMarketplace", tile: Dict[str, Any]) -> Listing | None:
        listing_id = str(tile.get("id") or "").strip()
        slug = str(tile.get("slug") or "").strip()
        if not listing_id or not slug:
            return None
        title = str(tile.get("title") or "").strip() or slug.replace("-", " ")
        prices = tile.get("prices") or []
        # Poshmark shows "<current> <original>"; the first match is current.
        price = str(prices[0]).replace(" ", "") if prices else ""
        return Listing(
            marketplace="poshmark",
            name="",
            id=listing_id,
            title=title,
            image=str(tile.get("img") or ""),
            price=price,
            post_url=f"https://poshmark.com/listing/{slug}",
            location="",
            seller="",
            condition="",
            description="",
        )
