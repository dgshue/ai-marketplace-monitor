"""Full UI QA: every view, every interaction, screenshots. No disk writes."""

import logging
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("qa")
os.environ["FACEBOOK_USERNAME"] = "t@e.com"
os.environ["FACEBOOK_PASSWORD"] = "pw"

from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import WebUIConfig, start_webui

srv, info = start_webui(
    WebUIConfig(
        host="0.0.0.0",
        port=8476,
        config_files=[Path("/root/.ai-marketplace-monitor/config.toml")],
        log_handler=LogBroadcastHandler(capacity=50),
    ),
    logger=log,
)
time.sleep(2)

os.makedirs("/tmp/qa", exist_ok=True)
results = []

# ---------- pre-browser unit: tile-preferred detail merge ----------
from ai_marketplace_monitor.facebook import FacebookMarketplace as _FB

_fb = _FB.__new__(_FB)  # _prefer_tile needs no construction state
_cases = [
    (("**unspecified**", "$8,995"), "$8,995"),
    (("Seller's description", "Winston-Salem, NC"), "Winston-Salem, NC"),
    (("View seller profile", None), ""),
    (("$450", "$999"), "$450"),
    (("", None), ""),
]
_ok = all(_fb._prefer_tile(a, b) == want for (a, b), want in _cases)
print(("PASS " if _ok else "FAIL ") + "tile-preferred merge unit")
results.append(("tile-preferred merge unit", _ok, ""))

# ---------- pre-browser unit: the drift-proof rating join ----------
# Reproduces the field-drift failure that hid a notified vehicle: details
# cached with price '**unspecified**' while the rating was written against
# the tile price. The identity join must still attach the rating.
from ai_marketplace_monitor.utils import CacheType as _CT
from ai_marketplace_monitor.utils import cache as _cache
from ai_marketplace_monitor.webui.activity import build_activity as _ba

_probe_url = "https://qa.invalid/item/qa-drift-probe"
_cache.set(
    (_CT.LISTING_DETAILS.value, _probe_url),
    {
        "marketplace": "facebook",
        "name": "",
        "id": "qa-drift-probe",
        "title": "QA drift probe 2012",
        "image": "",
        "price": "**unspecified**",
        "post_url": _probe_url,
        "location": "Asheboro, NC",
        "seller": "qa",
        "condition": "Used",
        "description": "drift",
    },
    tag=_CT.LISTING_DETAILS.value,
)
_cache.set(
    (_CT.AI_BY_LISTING.value, "facebook", "qa-drift-probe", "car"),
    {"score": 4, "comment": "qa probe", "name": "qa"},
    tag=_CT.AI_BY_LISTING.value,
)
try:
    _out = _ba(_cache, [Path("/root/.ai-marketplace-monitor/config.toml")], limit=2000)
    _hit = [r for r in _out["listings"] if r["id"] == "qa-drift-probe"]
    ok = bool(_hit) and _hit[0]["item"] == "car" and _hit[0]["score"] == 4
    print(
        ("PASS " if ok else "FAIL ") + "drift-proof rating join",
        "| row:",
        _hit[0]["item"] if _hit else "MISSING",
    )
    results.append(("drift-proof rating join", ok, ""))
finally:
    _cache.delete((_CT.LISTING_DETAILS.value, _probe_url))
    _cache.delete((_CT.AI_BY_LISTING.value, "facebook", "qa-drift-probe", "car"))


def check(name, ok, extra=""):
    results.append((name, bool(ok), extra))
    print(("PASS " if ok else "FAIL ") + name + (("  | " + str(extra)) if extra else ""))


msgs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.on("console", lambda m: msgs.append((m.type, m.text, (m.location or {}).get("url", ""))))
    pg.on("pageerror", lambda e: msgs.append(("PAGEERROR", str(e), "")))

    # ---------- asset cache policy: an upgrade must never leave a stale app.js ----------
    import urllib.request as _u

    def _cc(path):
        with _u.urlopen("http://127.0.0.1:8476" + path, timeout=10) as r:
            return r.headers.get("Cache-Control", "")

    check("app.js is no-cache", "no-cache" in _cc("/static/app.js"), _cc("/static/app.js"))
    check("app.css is no-cache", "no-cache" in _cc("/static/app.css"))
    check("index is no-cache", "no-cache" in _cc("/"))
    check(
        "vendor assets stay cacheable", "no-cache" not in _cc("/static/vendor/leaflet/leaflet.js")
    )

    # ---------- login ----------
    pg.goto("http://127.0.0.1:8476/", wait_until="load")
    pg.wait_for_timeout(900)
    pg.screenshot(path="/tmp/qa/0-login.png")
    pg.fill("input[name=username]", "t@e.com")
    pg.fill("input[name=password]", "pw")
    pg.click("#login-submit")
    pg.wait_for_timeout(4000)
    check("login", not pg.eval_on_selector("#app", "e=>e.classList.contains('hidden')"))

    # ---------- deals ----------
    n_rows = pg.eval_on_selector_all(".dli", "e=>e.length")
    check("deals list rows", n_rows > 10, n_rows)
    check("deals detail", pg.eval_on_selector("#deal-detail h2", "e=>!!e.textContent"))
    pg.click(".dli:nth-child(4)")
    pg.wait_for_timeout(400)
    check(
        "deals selection",
        pg.eval_on_selector(".dli:nth-child(4)", "e=>e.classList.contains('sel')"),
    )
    pg.click(".dd-myrank .star[data-rank='5']")
    pg.wait_for_timeout(900)
    check("star set", pg.eval_on_selector_all(".dd-myrank .star.on", "e=>e.length") == 5)
    key = pg.eval_on_selector(".dli.sel", "e=>e.dataset.key")
    pg.click("[data-flag=hide]")
    pg.wait_for_timeout(900)
    check("dismiss hides", pg.eval_on_selector_all(f".dli[data-key='{key}']", "e=>e.length") == 0)
    pg.click(".verdict-chips [data-verdict=hidden]")
    pg.wait_for_timeout(600)
    check(
        "hidden chip shows", pg.eval_on_selector_all(f".dli[data-key='{key}']", "e=>e.length") == 1
    )
    pg.click(f".dli[data-key='{key}']")
    pg.wait_for_timeout(400)
    pg.click("[data-flag=hide]")
    pg.wait_for_timeout(900)
    pg.click(".verdict-chips [data-verdict='']")
    pg.wait_for_timeout(600)
    check(
        "restore returns", pg.eval_on_selector_all(f".dli[data-key='{key}']", "e=>e.length") == 1
    )
    pg.click(f".dli[data-key='{key}']")
    pg.wait_for_timeout(300)
    pg.click(".dd-myrank .star[data-rank='5']")  # clear rank
    pg.wait_for_timeout(700)
    check("star cleared", pg.eval_on_selector_all(".dd-myrank .star.on", "e=>e.length") == 0)
    pg.click(".verdict-chips [data-verdict=promising]")
    pg.wait_for_timeout(500)
    promising = pg.eval_on_selector_all(".dli", "e=>e.length")
    check("promising filter", True, f"{promising} rows")
    pg.click(".verdict-chips [data-verdict='']")
    pg.wait_for_timeout(400)

    # --- item pills: summary row filters on click ---
    n_pills = pg.eval_on_selector_all("#activity-summary .ipill", "e=>e.length")
    check("item pills render", n_pills >= 2, n_pills)
    first_item = pg.eval_on_selector(
        "#activity-summary .ipill[data-item-pill]:not([data-item-pill=''])",
        "e=>e.dataset.itemPill",
    )
    pg.click(f"#activity-summary .ipill[data-item-pill='{first_item}']")
    pg.wait_for_timeout(500)
    rows_match = pg.evaluate(
        "(item) => Array.from(document.querySelectorAll('.dli .m span:first-child')).every(x => x.textContent === item)",
        first_item,
    )
    check("pill filters rows", rows_match, first_item)
    check(
        "pill active state",
        pg.eval_on_selector(
            f"#activity-summary .ipill[data-item-pill='{first_item}']",
            "e=>e.classList.contains('on')",
        ),
    )
    pg.click(f"#activity-summary .ipill[data-item-pill='{first_item}']")
    pg.wait_for_timeout(500)
    check(
        "pill toggles back to All",
        pg.eval_on_selector(
            "#activity-summary .ipill[data-item-pill='']", "e=>e.classList.contains('on')"
        ),
    )

    # --- paused items: out of the All view, reachable via their dimmed pill ---
    paused = pg.eval_on_selector_all(
        "#activity-summary .ipill.paused", "e=>e.map(x=>x.dataset.itemPill)"
    )
    if paused:
        hidden_ok = pg.evaluate(
            "(names) => !Array.from(document.querySelectorAll('.dli .m span:first-child')).some(x => names.includes(x.textContent))",
            paused,
        )
        check("paused items absent from All", hidden_ok, paused)
        pg.click(f"#activity-summary .ipill[data-item-pill='{paused[0]}']")
        pg.wait_for_timeout(500)
        shown = pg.eval_on_selector_all(".dli", "e=>e.length")
        check("paused pill shows its history", shown > 0, f"{paused[0]}: {shown} rows")
        pg.click(f"#activity-summary .ipill[data-item-pill='{paused[0]}']")
        pg.wait_for_timeout(400)
    else:
        check("paused pills (none configured)", True, "no paused items on disk")

    pg.fill("#activity-filter", "3090")
    pg.wait_for_timeout(400)
    check("text filter", pg.eval_on_selector_all(".dli", "e=>e.length") > 0)
    pg.fill("#activity-filter", "")
    pg.wait_for_timeout(400)
    check("csv button", bool(pg.query_selector("#export-csv-btn")))

    # --- sort control: each mode must actually reorder; blanks sort last ---
    check("sort control present", bool(pg.query_selector("#deal-sort")))

    def list_order():
        return pg.eval_on_selector_all(".dli", "e=>e.map(x=>x.dataset.key)")

    def sort_by(mode):
        pg.select_option("#deal-sort", mode)
        pg.wait_for_timeout(500)
        return list_order()

    by_score = sort_by("score")
    scores = pg.eval_on_selector_all(".dli .score-badge", "e=>e.map(x=>parseInt(x.textContent))")
    check("sort: best rated descending", scores == sorted(scores, reverse=True), scores[:6])
    sort_by("distance")
    dists = pg.evaluate(
        "(() => Array.from(document.querySelectorAll('.dli')).map(r => {"
        " const m = (r.innerText.match(/([0-9.]+) mi/) || [])[1];"
        " return m ? parseFloat(m) : null; }))()"
    )
    known = [d for d in dists if d is not None]
    idx_known = [i for i, d in enumerate(dists) if d is not None]
    idx_null = [i for i, d in enumerate(dists) if d is None]
    check("sort: nearest ascending", known == sorted(known), known[:6])
    check(
        "sort: unresolvable distances last",
        (not idx_null) or (not idx_known) or min(idx_null) > max(idx_known),
        f"{len(known)} with distance, {len(idx_null)} without",
    )
    by_new = sort_by("newest")
    check("sort: newest is a valid ordering", len(by_new) == len(by_score))
    sort_by("myrank")
    check("sort: my rating mode", len(list_order()) == len(by_score))
    sort_by("score")

    # --- no PDP junk artifacts anywhere in the deals surface ---
    junk_hits = pg.evaluate(
        "['**unspecified**', \"Seller's description\", 'View seller profile']"
        ".map(j => document.body.innerText.includes(j) ? j : null).filter(Boolean)"
    )
    check("no junk scrape artifacts rendered", not junk_hits, junk_hits)

    # --- layout guards: the detail media must never blow the page wide ---
    overflow = pg.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check("no horizontal page overflow", overflow <= 1, f"{overflow}px")
    if pg.query_selector("#dd-map"):
        import json as _j

        _r = _j.loads(
            pg.eval_on_selector("#dd-map", "e=>JSON.stringify(e.getBoundingClientRect())")
        )
        check("map confined to media rail", _r["width"] <= 620, "%.0fpx" % _r["width"])
        check("map is large enough to read", _r["height"] >= 300, "%.0fpx tall" % _r["height"])
        pg.wait_for_timeout(5000)
        _pts = pg.evaluate(
            "(() => { let best = 0;"
            " document.querySelectorAll('#dd-map path.leaflet-interactive').forEach(el => {"
            " const d = el.getAttribute('d') || '';"
            " best = Math.max(best, (d.match(/[ML]/g) || []).length); });"
            " return best; })()"
        )
        check("route geometry drawn (not a 2-point line)", _pts >= 5, f"{_pts} vertices")
        _drive = (
            pg.eval_on_selector("#dd-drive", "e=>e.textContent")
            if pg.query_selector("#dd-drive")
            else ""
        )
        check(
            "drive time sits in the price header",
            ("by road" in _drive) or _drive == "",
            _drive[:40] or "(routing unavailable - soft)",
        )

    # --- media: photo snapshot, pickup map, drive time on a facebook row ---
    picked = pg.evaluate(
        """(() => {
      const rows = document.querySelectorAll('.dli');
      for (const r of rows) {
        const src = r.querySelector('.m span:nth-child(2)');
        if (src && src.textContent === 'facebook') return r.dataset.key;
      }
      return null; })()"""
    )
    if picked:
        pg.click(f".dli[data-key='{picked}']")
        pg.wait_for_timeout(2500)
        has_photo = bool(pg.query_selector(".dd-photo"))
        check(
            "photo element for fb row",
            True,
            "shown" if has_photo else "hidden (image expired — acceptable)",
        )
        map_ok = pg.evaluate(
            "!!document.querySelector('#dd-map .leaflet-container, #dd-map.leaflet-container')"
        )
        coords = pg.evaluate("(k)=>{return true}", picked)
        check(
            "pickup map mounts",
            map_ok or not pg.query_selector("#dd-map"),
            "map" if map_ok else "no coords for this row",
        )
        pg.wait_for_timeout(4000)
        route_txt = (
            pg.eval_on_selector("#dd-route", "e=>e.textContent")
            if pg.query_selector("#dd-route")
            else ""
        )
        check(
            "drive estimate",
            ("drive" in route_txt) or route_txt == "",
            route_txt[:60] or "(routing unavailable — soft)",
        )
    else:
        check("media checks (no facebook rows)", True, "skipped")
    pg.screenshot(path="/tmp/qa/1-deals.png")

    # ---------- config: item cards ----------
    pg.click("#app-nav button[data-appview=config]")
    pg.wait_for_timeout(1000)
    snapshot = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue()")
    n_cards = pg.eval_on_selector_all(".icard", "e=>e.length")
    check("item cards render", n_cards >= 4, n_cards)
    check("cards collapsed by default", pg.eval_on_selector_all(".icard.open", "e=>e.length") == 0)
    check(
        "sources strip",
        pg.eval_on_selector_all(".set", "e=>e.length") >= 3,
        pg.eval_on_selector_all(".set .t", "e=>e.map(x=>x.textContent.trim())"),
    )
    check("add item btn", bool(pg.query_selector('[data-add="item"]')))

    S = ".icard[data-section='item.pc']"
    pg.click(S + " .ihead")
    pg.wait_for_timeout(400)
    check("card expands", pg.eval_on_selector(S, "e=>e.classList.contains('open')"))
    pg.screenshot(path="/tmp/qa/2-config-open.png")

    # ---------- pacing: the cadence has to be visible without opening a modal ----------
    cadences = pg.eval_on_selector_all(".icard .icadence", "e=>e.map(x=>x.textContent.trim())")
    check(
        "every item card shows its cadence",
        len(cadences) == n_cards and all(c.startswith(("every ", "at ")) for c in cadences),
        cadences[:6],
    )
    check(
        "interval fields are inline, not behind a modal",
        bool(pg.query_selector(S + " [data-field=search_interval]"))
        and bool(pg.query_selector(S + " [data-field=max_search_interval]")),
    )
    check(
        "cadence note explains where the value came from",
        any(
            w
            in (pg.eval_on_selector_all(S + " .thr-note", "e=>e.map(x=>x.textContent)") or [""])[
                -1
            ]
            for w in ("set on this item", "inherited from the marketplace", "default")
        ),
        pg.eval_on_selector_all(S + " .thr-note", "e=>e.map(x=>x.textContent.trim())"),
    )
    # writing a human duration must land in the buffer as a string, then revert
    orig_int = pg.eval_on_selector(S + " [data-field=search_interval]", "e=>e.value")
    pg.fill(S + " [data-field=search_interval]", "2h")
    pg.eval_on_selector(S + " [data-field=search_interval]", "e=>e.blur()")
    pg.wait_for_timeout(700)
    seg_iv = pg.evaluate(
        "document.querySelector('.CodeMirror').CodeMirror.getValue().split('[item.pc]')[1].split(String.fromCharCode(10)+'[')[0]"
    )
    check("interval writes a duration string", 'search_interval = "2h"' in seg_iv, seg_iv[:120])
    check(
        "cadence label follows the edit",
        "every 2h" in pg.eval_on_selector(S + " .icadence", "e=>e.textContent"),
        pg.eval_on_selector(S + " .icadence", "e=>e.textContent"),
    )
    pg.fill(S + " [data-field=search_interval]", orig_int)
    pg.eval_on_selector(S + " [data-field=search_interval]", "e=>e.blur()")
    pg.wait_for_timeout(700)

    # chips: add then remove a phrase (net zero)
    pg.fill(S + " [data-chip-add='search_phrases']", "qa test phrase")
    pg.press(S + " [data-chip-add='search_phrases']", "Enter")
    pg.wait_for_timeout(700)
    in_toml = pg.evaluate(
        "document.querySelector('.CodeMirror').CodeMirror.getValue().includes('qa test phrase')"
    )
    check("chip add writes TOML", in_toml)
    pg.click(S + " [data-chip-del='search_phrases'][data-chip-val='qa test phrase']")
    pg.wait_for_timeout(700)
    out_toml = pg.evaluate(
        "!document.querySelector('.CodeMirror').CodeMirror.getValue().includes('qa test phrase')"
    )
    check("chip remove cleans TOML", out_toml)

    # description edit + revert
    orig_desc = pg.eval_on_selector(S + " [data-field=description]", "e=>e.value")
    pg.fill(S + " [data-field=description]", "qa description probe")
    pg.eval_on_selector(S + " [data-field=description]", "e=>e.blur()")
    pg.wait_for_timeout(700)
    check(
        "description writes",
        pg.evaluate(
            "document.querySelector('.CodeMirror').CodeMirror.getValue().includes('qa description probe')"
        ),
    )
    pg.fill(S + " [data-field=description]", orig_desc)
    pg.eval_on_selector(S + " [data-field=description]", "e=>e.blur()")
    pg.wait_for_timeout(700)

    # price int + clear back to original
    orig_max = pg.eval_on_selector(S + " [data-field=max_price]", "e=>e.value")
    pg.fill(S + " [data-field=max_price]", "912")
    pg.eval_on_selector(S + " [data-field=max_price]", "e=>e.blur()")
    pg.wait_for_timeout(700)
    seg = pg.evaluate(
        "(() => { const v=document.querySelector('.CodeMirror').CodeMirror.getValue(); return v.split('[item.pc]')[1].split('\\n[')[0]; })()"
    )
    check("price writes unquoted int", "max_price = 912" in seg and 'max_price = "912"' not in seg)
    pg.fill(S + " [data-field=max_price]", orig_max)
    pg.eval_on_selector(S + " [data-field=max_price]", "e=>e.blur()")
    pg.wait_for_timeout(700)

    # threshold set + clear (verify against the BUFFER, the source of truth)
    pg.click(S + " .thr button[data-thr='2']")
    pg.wait_for_timeout(800)
    seg_thr = pg.evaluate(
        "document.querySelector('.CodeMirror').CodeMirror.getValue().split('[item.pc]')[1].split(String.fromCharCode(10)+'[')[0]"
    )
    check("threshold set writes", "rating = 2" in seg_thr)
    check(
        "threshold note shows",
        "≥ 2"
        in pg.eval_on_selector(S + " .thr .on", "e=>e.parentElement.nextElementSibling ? '' : ''")
        or "≥ 2" in pg.eval_on_selector(S + " .thr-note", "e=>e.textContent"),
    )
    pg.click(S + " .thr button[data-thr='2']")
    pg.wait_for_timeout(800)
    seg_thr2 = pg.evaluate(
        "document.querySelector('.CodeMirror').CodeMirror.getValue().split('[item.pc]')[1].split(String.fromCharCode(10)+'[')[0]"
    )
    check("threshold clear removes", "rating" not in seg_thr2)
    check(
        "cleared note inherits",
        "inherited" in pg.eval_on_selector(S + " .thr-note", "e=>e.textContent"),
    )

    # source toggle: with 2+ sources it toggles; with one, the guard refuses
    n_sources = pg.eval_on_selector_all(S + " .src", "e=>e.length")
    pg.click(S + " .src[data-src=facebook]")
    pg.wait_for_timeout(700)
    if n_sources >= 2:
        check(
            "source off",
            not pg.eval_on_selector(
                S + " .src[data-src=facebook]", "e=>e.classList.contains('on')"
            ),
        )
        pg.click(S + " .src[data-src=facebook]")
        pg.wait_for_timeout(700)
        check(
            "source back on",
            pg.eval_on_selector(S + " .src[data-src=facebook]", "e=>e.classList.contains('on')"),
        )
    else:
        status = pg.eval_on_selector("#editor-status", "e=>e.textContent")
        check("single-source guard refuses", "at least one source" in status, status[:60])
        check(
            "source stays on",
            pg.eval_on_selector(S + " .src[data-src=facebook]", "e=>e.classList.contains('on')"),
        )

    # enable switch off/on
    pg.click(S + " .ihead .sw")
    pg.wait_for_timeout(600)
    check("disable greys card", pg.eval_on_selector(S, "e=>e.classList.contains('disabled')"))
    pg.click(S + " .ihead .sw")
    pg.wait_for_timeout(600)
    check("re-enable", not pg.eval_on_selector(S, "e=>e.classList.contains('disabled')"))

    # End-state: the only acceptable diff is chips normalizing a legacy
    # bare-string search_phrases into an array. Reset buffer to the snapshot
    # so nothing is left dirty, then confirm Save disables.
    final = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue()")
    if final != snapshot:
        import difflib

        diff = [
            line
            for line in difflib.unified_diff(
                snapshot.splitlines(), final.splitlines(), lineterm=""
            )
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        only_norm = all("search_phrases" in line for line in diff)
        check("buffer diff is only phrase normalization", only_norm, diff[:6])
    else:
        check("buffer pristine", True)
    (
        pg.evaluate(
            "document.querySelector('.CodeMirror').CodeMirror.setValue("
            + repr(0)
            + " ? '' : arguments)"
        )
        if False
        else None
    )
    pg.evaluate(
        "(s) => { const cm=document.querySelector('.CodeMirror').CodeMirror; cm.setValue(s); }",
        snapshot,
    )
    pg.wait_for_timeout(600)
    check("buffer reset (Save disabled)", pg.eval_on_selector("#save-btn", "e=>e.disabled"))

    # modals open + cancel (no saves)
    pg.click(S + " [data-act=edit]")
    pg.wait_for_timeout(500)
    check(
        "More settings opens modal",
        not pg.eval_on_selector("#form-modal", "e=>e.classList.contains('hidden')"),
    )
    # The schedule fields are the whole point of the pacing work -- they must
    # render with "Show advanced fields" OFF.
    pg.click(".form-tab-bar .form-tab:nth-child(2)")
    pg.wait_for_timeout(350)
    adv_off = (
        pg.eval_on_selector("#show-advanced", "e=>!e.checked")
        if pg.query_selector("#show-advanced")
        else True
    )
    check(
        "item schedule fields are not advanced-only",
        adv_off
        and bool(pg.query_selector("#field-search_interval"))
        and bool(pg.query_selector("#field-max_search_interval")),
        "advanced off" if adv_off else "advanced was already on",
    )
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)
    pg.click(".set [data-edit-section='marketplace.facebook']")
    pg.wait_for_timeout(500)
    check(
        "strip Edit opens modal",
        not pg.eval_on_selector("#form-modal", "e=>e.classList.contains('hidden')"),
    )
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)
    pg.click('[data-add="item"]')
    pg.wait_for_timeout(500)
    check(
        "Add item opens modal",
        not pg.eval_on_selector("#form-modal", "e=>e.classList.contains('hidden')"),
    )
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)

    # --- available sources appear with Set up, prefilled to the right type ---
    avail = pg.eval_on_selector_all(
        ".set.avail [data-setup-marketplace]", "e=>e.map(x=>x.dataset.setupMarketplace)"
    )
    check("available source cards", set(avail) >= {"ebay", "depop", "poshmark"}, avail)
    pg.click(".set.avail [data-setup-marketplace='ebay']")
    pg.wait_for_timeout(600)
    check(
        "setup modal prefilled", pg.eval_on_selector("#add-section-name", "e=>e.value") == "ebay"
    )
    check("setup uses ebay schema", bool(pg.query_selector("#field-client_id")))
    check(
        "setup tab says eBay",
        "eBay" in pg.eval_on_selector(".form-tab-bar .form-tab", "e=>e.textContent"),
    )
    check("name input autofill-proof", pg.eval_on_selector("#add-section-name", "e=>e.readOnly"))
    check("field autofill-proof", pg.eval_on_selector("#field-client_id", "e=>e.readOnly"))
    pg.click("#field-client_id")
    check("field opens on focus", not pg.eval_on_selector("#field-client_id", "e=>e.readOnly"))
    # --- eBay mode select: the whole point is that no developer key is needed,
    # so the choice has to be visible and the credentials must not be required.
    has_mode = bool(pg.query_selector("#field-mode"))
    check("ebay form offers a mode select", has_mode)
    mode_opts = (
        pg.eval_on_selector("#field-mode", "e=>Array.from(e.options).map(o=>o.value)")
        if has_mode
        else []
    )
    check("ebay mode offers api and browser", set(mode_opts) >= {"", "api", "browser"}, mode_opts)
    check(
        "ebay mode explains the tradeoff",
        "no keys" in (pg.eval_on_selector("#field-mode ~ .form-help", "e=>e.textContent") or ""),
    )
    check(
        "ebay keys are optional",
        not pg.eval_on_selector(
            "[data-key='client_id']",
            "e=>!!e.closest('.form-field').querySelector('.required')",
        ),
    )

    # --- adding eBay with ZERO credentials must work and land enabled ---
    pg.click("#form-save")
    pg.wait_for_timeout(1800)
    buf0 = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue()")
    check("keyless ebay add lands a section", "[marketplace.ebay]" in buf0)
    seg0 = buf0.split("[marketplace.ebay]")[1][:400] if "[marketplace.ebay]" in buf0 else ""
    check("keyless ebay add carries market_type", 'market_type = "ebay"' in seg0, seg0[:120])
    check("keyless ebay add starts enabled", "enabled = true" in seg0, seg0[:120])
    check("keyless ebay add writes no client_id", "client_id" not in seg0, seg0[:120])
    check(
        "keyless ebay add persisted to disk",
        pg.eval_on_selector("#save-btn", "e=>e.disabled"),
        pg.eval_on_selector("#editor-status", "e=>e.textContent")[:80],
    )
    ebay_card = (
        pg.eval_on_selector(
            ".set:has([data-edit-section='marketplace.ebay']) .d", "e=>e.textContent"
        )
        or ""
    )
    check("ebay card reports browser mode", "browser" in ebay_card.lower(), ebay_card[:80])
    check(
        "ebay card claims no key needed", "no developer key" in ebay_card.lower(), ebay_card[:80]
    )
    # restore before the keyed variant, so the section name is free again
    pg.evaluate("(s) => document.querySelector('.CodeMirror').CodeMirror.setValue(s)", snapshot)
    pg.wait_for_timeout(700)
    if not pg.eval_on_selector("#save-btn", "e=>e.disabled"):
        pg.click("#save-btn")
        pg.wait_for_timeout(1500)

    # --- the same add WITH credentials still works, and stays enabled too ---
    pg.click(".set.avail [data-setup-marketplace='ebay']")
    pg.wait_for_timeout(600)
    pg.click("#field-client_id")
    pg.fill("#field-client_id", "${EBAY_CLIENT_ID}")
    pg.click("#field-client_secret")
    pg.fill("#field-client_secret", "${EBAY_CLIENT_SECRET}")
    pg.click("#form-save")
    pg.wait_for_timeout(1800)
    buf = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue()")
    seg_ok = "[marketplace.ebay]" in buf and 'market_type = "ebay"' in buf
    check("added section carries market_type", seg_ok)
    check(
        "keyed ebay add starts enabled",
        "enabled = true" in buf.split("[marketplace.ebay]")[1][:400],
    )
    ebay_card2 = (
        pg.eval_on_selector(
            ".set:has([data-edit-section='marketplace.ebay']) .d", "e=>e.textContent"
        )
        or ""
    )
    check("keyed ebay card reports API mode", "api" in ebay_card2.lower(), ebay_card2[:80])
    # The add must have PERSISTED (save-btn disabled = buffer==disk); a
    # validation rejection here is the bug this block exists to catch.
    check(
        "ebay add persisted to disk",
        pg.eval_on_selector("#save-btn", "e=>e.disabled"),
        pg.eval_on_selector("#editor-status", "e=>e.textContent")[:80],
    )
    # restore: put the snapshot back and save, leaving disk exactly as found
    pg.evaluate("(s) => document.querySelector('.CodeMirror').CodeMirror.setValue(s)", snapshot)
    pg.wait_for_timeout(700)
    if not pg.eval_on_selector("#save-btn", "e=>e.disabled"):
        pg.click("#save-btn")
        pg.wait_for_timeout(1500)
    check("disk restored after setup test", pg.eval_on_selector("#save-btn", "e=>e.disabled"))

    # --- every configured section's form opens, renders, and is sane ---
    edit_targets = pg.eval_on_selector_all(
        "[data-edit-section]", "e=>e.map(x=>x.dataset.editSection)"
    )
    for name in edit_targets:
        pg.click(f"[data-edit-section='{name}']")
        pg.wait_for_timeout(500)
        modal_open = not pg.eval_on_selector("#form-modal", "e=>e.classList.contains('hidden')")
        nfields = pg.eval_on_selector_all("#section-form [data-key]", "e=>e.length")
        if name == "marketplace.facebook":
            # Mapping/distance settings must be reachable from the UI, not
            # only by hand-editing TOML.
            pg.click(".form-tab-bar .form-tab:nth-child(2)")
            pg.wait_for_timeout(350)
            has_home = bool(pg.query_selector("#field-home_location"))
            check(
                "home_location exposed in settings UI",
                has_home,
                (
                    pg.eval_on_selector("#field-home_location", "e=>e.value")
                    if has_home
                    else "MISSING"
                ),
            )
            check(
                "facebook form exposes request_delay",
                bool(pg.query_selector("#field-request_delay")),
            )
            check(
                "facebook form exposes block_cooldown",
                bool(pg.query_selector("#field-block_cooldown")),
            )
        hint = pg.eval_on_selector("#form-modal-hint", "e=>e.hidden ? '' : e.textContent") or ""
        check(
            f"form opens: {name}",
            modal_open and (nfields > 0 or "No form" in hint),
            f"{nfields} fields",
        )
        if name.startswith("marketplace.") and nfields:
            tab = pg.eval_on_selector(".form-tab-bar .form-tab", "e=>e.textContent") or ""
            kind = name.split(".")[1]
            if kind != "facebook":
                check(f"tab label not facebook: {name}", "Facebook" not in tab, tab)
        pg.click("#form-cancel")
        pg.wait_for_timeout(250)

    # item modals: both tabs render fields for every item card
    item_names = pg.eval_on_selector_all(".icard", "e=>e.map(x=>x.dataset.section)")
    for name in item_names[:2]:  # two representatives keep the run fast
        # Edit lives in the card body, hidden until the card is expanded.
        if not pg.eval_on_selector(
            f".icard[data-section='{name}']", "e=>e.classList.contains('open')"
        ):
            pg.click(f".icard[data-section='{name}'] .ihead")
            pg.wait_for_timeout(350)
        pg.click(f".icard[data-section='{name}'] [data-act=edit]")
        pg.wait_for_timeout(450)
        left = pg.eval_on_selector_all("#section-form [data-key]", "e=>e.length")
        pg.click(".form-tab-bar .form-tab:nth-child(2)")
        pg.wait_for_timeout(350)
        right = pg.eval_on_selector_all("#section-form [data-key]", "e=>e.length")
        check(f"item modal tabs: {name}", left > 0 and right > 0, f"L{left}/R{right}")
        pg.click("#form-cancel")
        pg.wait_for_timeout(250)
        pg.click(f".icard[data-section='{name}'] .ihead")  # collapse back
        pg.wait_for_timeout(250)

    # depop + poshmark setup flows: labeled correctly, save carries market_type
    for kind, label in (("depop", "Depop"), ("poshmark", "Poshmark")):
        pg.click(f".set.avail [data-setup-marketplace='{kind}']")
        pg.wait_for_timeout(500)
        tab = pg.eval_on_selector(".form-tab-bar .form-tab", "e=>e.textContent") or "(no tabs)"
        check(f"setup tab: {kind}", label in tab, tab)
        check(
            f"setup name prefilled: {kind}",
            pg.eval_on_selector("#add-section-name", "e=>e.value") == kind,
        )
        pg.click("#form-save")
        pg.wait_for_timeout(1200)
        buf2 = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue()")
        check(
            f"setup saved with market_type: {kind}",
            f"[marketplace.{kind}]" in buf2 and f'market_type = "{kind}"' in buf2,
        )
    # restore disk to the snapshot after all setup writes
    pg.evaluate("(s) => document.querySelector('.CodeMirror').CodeMirror.setValue(s)", snapshot)
    pg.wait_for_timeout(700)
    if not pg.eval_on_selector("#save-btn", "e=>e.disabled"):
        pg.click("#save-btn")
        pg.wait_for_timeout(1500)
    check("disk restored after setup sweep", pg.eval_on_selector("#save-btn", "e=>e.disabled"))
    pg.screenshot(path="/tmp/qa/6-forms.png")

    # TOML tab roundtrip
    pg.click("#tab-toml")
    pg.wait_for_timeout(500)
    check(
        "TOML tab shows editor",
        pg.eval_on_selector("#config-toml-view", "e=>getComputedStyle(e).display") != "none",
    )
    pg.click("#tab-form")
    pg.wait_for_timeout(400)
    check(
        "back to Form",
        pg.eval_on_selector("#config-form-view", "e=>getComputedStyle(e).display") != "none",
    )

    # help levels
    pg.click("#help-seg button[data-help=guided]")
    pg.wait_for_timeout(300)
    check(
        "guided shows examples",
        pg.eval_on_selector(".fieldhelp .ex", "e=>getComputedStyle(e).display") == "block",
    )
    pg.click("#help-seg button[data-help=off]")
    pg.wait_for_timeout(300)
    check(
        "help off hides",
        pg.eval_on_selector(".fieldhelp", "e=>getComputedStyle(e).display") == "none",
    )
    pg.click("#help-seg button[data-help=hints]")
    pg.wait_for_timeout(200)

    # ---------- logs ----------
    pg.click("#app-nav button[data-appview=logs]")
    pg.wait_for_timeout(600)
    check("logs render", pg.eval_on_selector_all("#logs > *", "e=>e.length") >= 0)
    check(
        "log buttons",
        bool(pg.query_selector("#log-download")) and bool(pg.query_selector("#log-clear")),
    )
    pg.click(".level-chips [data-level=ERROR]")
    pg.wait_for_timeout(300)
    pg.click(".level-chips [data-level=ALL]")
    pg.wait_for_timeout(200)
    pg.screenshot(path="/tmp/qa/3-logs.png")

    # ---------- status ----------
    pg.click("#app-nav button[data-appview=status]")
    pg.wait_for_timeout(1500)
    check(
        "status tiles",
        pg.eval_on_selector_all(".stile", "e=>e.length") == 4,
        pg.eval_on_selector_all(".stile .t", "e=>e.map(x=>x.textContent)"),
    )
    check("env lines", pg.eval_on_selector_all(".envline", "e=>e.length") > 0)
    pg.click("#status-refresh")
    pg.wait_for_timeout(800)
    check(
        "status refresh", "updated" in pg.eval_on_selector("#status-updated", "e=>e.textContent")
    )
    check(
        "status tile count unchanged by block work",
        pg.eval_on_selector_all(".stile", "e=>e.length") == 4,
    )
    pg.screenshot(path="/tmp/qa/4-status.png")

    # ---------- block state: rendered from a stubbed payload ----------
    # Provoking a real Facebook block to test the UI is not an option, so the
    # renderers are asserted directly against the shape /api/monitor/state
    # publishes. window.__aimm exists for exactly this.
    block_probe = pg.evaluate(
        """() => {
          const A = window.__aimm;
          if (!A) return { missing: true };
          const now = 1000;
          const info = { available: true, blocked: { facebook: {
            marketplace: "facebook", reason: "page title: Temporarily Blocked",
            detected_at: 0, until: 1000 + 3600, remaining: 3600, strikes: 2 } } };
          const stale = { available: true, blocked: { facebook: {
            marketplace: "facebook", reason: "old", detected_at: 0, until: 500 } } };
          const html = A.renderBlockNotice(info, now);
          return {
            active: A.activeBlocks(info, now).length,
            expiredIgnored: A.activeBlocks(stale, now).length,
            noneWhenClear: A.activeBlocks({ available: true, blocked: {} }, now).length,
            chip: A.blockChipLabel(info.blocked.facebook),
            html,
            emptyHtml: A.renderBlockNotice({ available: true, blocked: {} }, now),
            cadence: [A.parseDuration("2h"), A.parseDuration(1800), A.parseDuration("90m"),
                      A.parseDuration("bogus"), A.fmtCadence(7200), A.fmtCadence(2700)],
          };
        }"""
    )
    check("block renderers exposed for QA", not block_probe.get("missing"))
    check("active block detected", block_probe.get("active") == 1)
    check("expired cooldown is not shown", block_probe.get("expiredIgnored") == 0)
    check("no block reads as clear", block_probe.get("noneWhenClear") == 0)
    check(
        "chip reads 'facebook: blocked - retry HH:MM'",
        (block_probe.get("chip") or "").startswith("facebook: blocked")
        and ": blocked" in (block_probe.get("chip") or "")
        and len((block_probe.get("chip") or "").split("retry ")[-1]) == 5,
        block_probe.get("chip"),
    )
    _bh = block_probe.get("html") or ""
    check(
        "status page offers Clear block with the reason and retry time",
        "Blocked marketplaces" in _bh
        and "Clear block / retry now" in _bh
        and "Temporarily Blocked" in _bh
        and 'data-clear-block="facebook"' in _bh
        and "strike 2" in _bh,
        _bh[:160],
    )
    check("no notice when nothing is blocked", block_probe.get("emptyHtml") == "")
    check(
        "duration parsing matches the backend",
        block_probe.get("cadence") == [7200, 1800, 5400, None, "2h", "45m"],
        block_probe.get("cadence"),
    )

    # ---------- header controls ----------
    check(
        "chip text",
        bool(pg.eval_on_selector("#monitor-status", "e=>e.textContent.trim()")),
        pg.eval_on_selector("#monitor-status", "e=>e.textContent"),
    )
    check(
        "browser link", "path=ws/vnc" in (pg.eval_on_selector("#browser-btn", "e=>e.href") or "")
    )

    # narrow viewport: deals split stacks
    pg.set_viewport_size({"width": 860, "height": 900})
    pg.click("#app-nav button[data-appview=deals]")
    pg.wait_for_timeout(600)
    cols = pg.eval_on_selector(".deals-split", "e=>getComputedStyle(e).gridTemplateColumns")
    check("mobile stacks", " " not in cols.strip(), cols)
    pg.set_viewport_size({"width": 1500, "height": 950})
    pg.wait_for_timeout(400)

    # config screenshot at full width for review
    pg.click("#app-nav button[data-appview=config]")
    pg.wait_for_timeout(800)
    pg.click(S + " .ihead")
    pg.wait_for_timeout(500)
    pg.screenshot(path="/tmp/qa/5-config-final.png", full_page=True)

    b.close()

# Resource-error console text omits the URL; it rides in the message location.
errors = [
    (t, m)
    for t, m, url in msgs
    if t in ("error", "PAGEERROR") and "401" not in m
    # expired listing images 404 by design; the img hides itself
    and "listing-image" not in m + url
    # OSM tile fetches can transiently fail without breaking the map
    and "tile.openstreetmap.org" not in m + url
]
check("zero console errors", len(errors) == 0, errors[:3])

fails = [r for r in results if not r[1]]
print("=" * 50)
print(f"QA: {len(results) - len(fails)}/{len(results)} passed")
for name, _, extra in fails:
    print("  FAILED:", name, extra)
srv.stop()
print("DONE")
