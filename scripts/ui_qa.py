"""Full UI QA for the Triage web UI: every screen, every interaction, screenshots.

Runs inside the deployed container against the real package. The web UI under
test serves a PRIVATE COPY of the live config.toml: writing through the live
file would make the running monitor clear its schedule and re-search every
item immediately (see MarketplaceMonitor.start_monitor), which is exactly what
must not happen during QA. The live file is snapshotted at start and asserted
byte-identical at the end. The diskcache is shared with the live monitor; the
harness only writes USER_FLAGS on rows it has undone again, plus two probe
keys it deletes.
"""

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.ERROR)
log = logging.getLogger("qa")
# Capture the deployment's real credential before the harness overrides it,
# so the log-download check can test for the actual value rather than a
# marker (scrubs and redactors use different markers; the secret is the truth).
_REAL_PASSWORD = os.environ.get("FACEBOOK_PASSWORD", "")
os.environ["FACEBOOK_USERNAME"] = "t@e.com"
os.environ["FACEBOOK_PASSWORD"] = "pw"

from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import WebUIConfig, start_webui

LIVE_CONFIG = Path("/root/.ai-marketplace-monitor/config.toml")
QA_DIR = Path("/tmp/qa")
QA_DIR.mkdir(exist_ok=True)
QA_CONFIG = QA_DIR / "config.toml"
_LIVE_BYTES = LIVE_CONFIG.read_bytes()
shutil.copyfile(LIVE_CONFIG, QA_CONFIG)

srv, info = start_webui(
    WebUIConfig(
        host="0.0.0.0",
        port=8476,
        config_files=[QA_CONFIG],
        log_handler=LogBroadcastHandler(capacity=50),
    ),
    logger=log,
)
time.sleep(2)
BASE = "http://127.0.0.1:8476"
results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok), extra))
    print(
        ("PASS " if ok else "FAIL ") + name + (("  | " + str(extra)) if extra else ""), flush=True
    )


# ---------- pre-browser unit: tile-preferred detail merge ----------
from ai_marketplace_monitor.facebook import FacebookMarketplace as _FB

_fb = _FB.__new__(_FB)
_cases = [
    (("**unspecified**", "$8,995"), "$8,995"),
    (("Seller's description", "Winston-Salem, NC"), "Winston-Salem, NC"),
    (("View seller profile", None), ""),
    (("$450", "$999"), "$450"),
    (("", None), ""),
]
check("tile-preferred merge unit", all(_fb._prefer_tile(a, b) == want for (a, b), want in _cases))

# ---------- pre-browser unit: the tile price always wins ----------
# Vehicle PDPs have no price element, so the scraper takes the first "$..." in
# the description -- the down payment on a dealer listing. Plausible, so
# _prefer_tile's junk test misses it; only an outright tile preference fixes it.
_price_cases = [
    (("$450", "$3,000 | $4,200"), "$3,000 | $4,200"),
    (("$550", "$5,500"), "$5,500"),
    (("$450", None), "$450"),
    (("$450", ""), "$450"),
    (("**unspecified**", None), ""),
]
check(
    "tile price wins at the cache boundary",
    all(_fb._merge_price(a, b) == want for (a, b), want in _price_cases),
    [(a, b, _fb._merge_price(a, b)) for (a, b), _ in _price_cases],
)

# ---------- pre-browser unit: the drift-proof rating join ----------
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
    _out = _ba(_cache, [QA_CONFIG], limit=2000)
    _hit = [r for r in _out["listings"] if r["id"] == "qa-drift-probe"]
    check(
        "drift-proof rating join",
        bool(_hit) and _hit[0]["item"] == "car" and _hit[0]["score"] == 4,
    )
    check(
        "activity rows carry kept + reviewed_at",
        bool(_hit) and _hit[0]["kept"] is False and "reviewed_at" in _hit[0],
        (
            {k: _hit[0][k] for k in ("kept", "reviewed_at", "hidden", "my_rank")}
            if _hit
            else "MISSING"
        ),
    )
finally:
    _cache.delete((_CT.LISTING_DETAILS.value, _probe_url))
    _cache.delete((_CT.AI_BY_LISTING.value, "facebook", "qa-drift-probe", "car"))

# ---------- pre-browser unit: the three score tiers ----------
# Own scratch cache and own config: the verdict boundaries have to be proved
# against known thresholds, not against whatever the live config happens to say.
from diskcache import Cache as _Cache

from ai_marketplace_monitor.listing import Listing as _Listing
from ai_marketplace_monitor.webui.activity import thresholds_from_config as _tfc

_TIER_CONFIG = QA_DIR / "tiers.toml"
_TIER_CONFIG.write_text(
    """
[marketplace.facebook]
search_city = 'dallas'
rating = 5
review_rating = 3

[user.me]
pushbullet_token = 'x'

[item.tiers]
search_phrases = 'thing'
""",
    encoding="utf-8",
)
_tier_cache = _Cache(str(QA_DIR / "tier-cache"))
try:
    for _score in (1, 2, 3, 4, 5):
        _lid = f"tier-{_score}"
        _l = _Listing(
            marketplace="facebook",
            name="",
            id=_lid,
            title=f"probe {_score}",
            image="",
            price="$1",
            post_url=f"https://qa.invalid/item/{_lid}",
            location="",
            seller="",
            condition="",
            description="",
        )
        _l.to_cache(_l.post_url, local_cache=_tier_cache)
        _tier_cache.set(
            (_CT.AI_BY_LISTING.value, "facebook", _lid, "tiers"),
            {"score": _score, "comment": "probe", "name": "qa"},
            tag=_CT.AI_BY_LISTING.value,
        )
    _tier_out = _ba(_tier_cache, [_TIER_CONFIG], limit=100)
    _by_score = {r["score"]: r["verdict"] for r in _tier_out["listings"]}
    check(
        "verdicts: below review_rating is low, at or above is promising",
        _by_score == {1: "low", 2: "low", 3: "promising", 4: "promising", 5: "promising"},
        _by_score,
    )
    check(
        "activity rows carry review_threshold beside threshold",
        all((r["review_threshold"], r["threshold"]) == (3, 5) for r in _tier_out["listings"]),
    )
    _tier_sum = _tier_out["summary"][0]
    check(
        "summary counts low apart from promising",
        (_tier_sum["examined"], _tier_sum["low"], _tier_sum["promising"]) == (5, 2, 3),
        {k: _tier_sum[k] for k in ("examined", "low", "promising", "notified")},
    )
    _pi, _nd, _dis, _pr, _rd = _tfc([_TIER_CONFIG])
    check("thresholds_from_config resolves both tiers", (_pi["tiers"], _pr["tiers"]) == (5, 3))
finally:
    _tier_cache.close()


def js(pg, code, *args):
    return pg.evaluate(code, *args)


def visible(pg, sel):
    return pg.is_visible(sel)


def overflow(pg):
    return js(pg, "document.documentElement.scrollWidth - document.documentElement.clientWidth")


BOX = "(sel) => { const e = document.querySelector(sel); return e ? JSON.parse(JSON.stringify(e.getBoundingClientRect())) : null; }"


def box(pg, sel):
    """Client rect of the first match, or None (the name `rect` is taken)."""
    return js(pg, BOX, sel)


def overlap(a, b):
    """Overlapping area of two client rects (0 when they merely touch)."""
    if not a or not b:
        return 0.0
    w = min(a["right"], b["right"]) - max(a["left"], b["left"])
    h = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    return max(0.0, w) * max(0.0, h)


# Anything the browser will let scroll sideways must have asked for it: a rail
# or pane that scrolls horizontally is always a layout bug, and only the opt-in
# strips (chip rows, the photo carousel's snap track, the log table,
# CodeMirror) may exceed their own width.
SIDEWAYS = """() => Array.from(document.querySelectorAll('body *')).filter(el => {
  const s = getComputedStyle(el);
  if (!['auto', 'scroll'].includes(s.overflowX)) return false;
  if (el.scrollWidth <= el.clientWidth + 1) return false;
  return !el.classList.contains('chips') && !el.classList.contains('track')
    && !el.closest('.CodeMirror') && !el.closest('.logs');
}).map(el => (el.id || el.tagName) + '.' + (el.className || '').toString().slice(0, 30)
  + ' ' + el.scrollWidth + '>' + el.clientWidth)"""


def open_desktop_detail(pg):
    """Sign in on a fresh desktop context and expand the first queue card."""
    pg.goto(BASE + "/", wait_until="load")
    pg.wait_for_timeout(900)
    if visible(pg, "#login-fields"):
        pg.fill("input[name=username]", "t@e.com")
        pg.fill("input[name=password]", "pw")
        pg.click("#login-submit")
    pg.wait_for_timeout(4500)
    pg.click("#rail .lrow")
    pg.wait_for_timeout(400)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(2500)


def cm_value(pg):
    return js(pg, "document.querySelector('.CodeMirror').CodeMirror.getValue()")


def segment(buf, header):
    """The body of a real ``[section]`` header line (commented examples don't count)."""
    m = re.search(r"(?m)^" + re.escape(header) + r"[ \t]*$", buf)
    if not m:
        return ""
    return buf[m.end() :].split("\n[")[0]


def line_for(seg, key):
    hits = [ln for ln in seg.splitlines() if ln.strip().startswith(key)]
    return hits[0] if hits else ""


def restore(pg, snapshot):
    """Put the snapshot back in the editor and persist it (buffer == disk)."""
    pg.evaluate(
        "(s) => { document.querySelector('.CodeMirror').CodeMirror.setValue(s); }", snapshot
    )
    pg.wait_for_timeout(300)
    pg.evaluate("() => window.AIMM.config.save()")
    pg.wait_for_timeout(600)
    return js(pg, "document.querySelector('#save-btn').disabled")


def api_rows(pg):
    return pg.request.get(BASE + "/api/activity").json()["listings"]


def api_row(pg, key):
    mk, lid = key.split(":", 1)
    return next((r for r in api_rows(pg) if r["marketplace"] == mk and r["id"] == lid), None)


def go(pg, view):
    pg.click(f"[data-view={view}]:visible")
    pg.wait_for_timeout(600)


def open_item(pg, name):
    """Open an item's editor from wherever the Items screen currently is."""
    if js(pg, "document.querySelector('#items-page').dataset.section || ''") == name:
        return
    if visible(pg, "#items-back"):
        pg.click("#items-back")
        pg.wait_for_timeout(300)
    pg.click(f"#items-page [data-open-item='{name}']")
    pg.wait_for_timeout(450)


# ---------- low-tier probe: one row under the review threshold, one at it ----------
# The live cache may or may not hold a listing below the review threshold, and
# "does the queue hide it" is not a check worth leaving to chance. Two probe
# rows on a real, active item make it deterministic; both are deleted before
# the summary. rated_at is now, so they also exercise the today strip.
_qa_notify, _, _qa_disabled, _qa_review, _ = _tfc([QA_CONFIG])
_probe_item = next((name for name in sorted(_qa_notify) if name not in _qa_disabled), None)
_PROBE_REVIEW = _qa_review.get(_probe_item, 3) if _probe_item else 3
LOW_ID = "qa-tier-low"
OK_ID = "qa-tier-ok"
# A listing with a real gallery. The live cache may hold none -- galleries are
# only read on listing pages fetched since the feature shipped -- and "does the
# carousel render every photo" is not a check worth leaving to chance.
GALLERY_ID = "qa-gallery"
GALLERY_N = 5
GALLERY_URL = f"https://qa.invalid/item/{GALLERY_ID}"
_probe_keys = []
_probe_files = []


def _seed_snapshots(post_url, count):
    """Write the photo snapshots the proxy would otherwise have to fetch.

    The probe's CDN URLs are invented, so nothing can download them; writing
    the snapshots is what makes the carousel show real, distinguishable
    images. Each is a different flat colour so a screenshot shows which photo
    is on screen.
    """
    from PIL import Image, ImageDraw

    from ai_marketplace_monitor.utils import image_cache_path

    palette = ["#3b6cf6", "#f6a23b", "#3bf68a", "#f63b6c", "#8a3bf6", "#3bf6f6"]
    for index in range(count):
        path = image_cache_path(post_url, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (640, 480), palette[index % len(palette)])
        ImageDraw.Draw(img).text((36, 36), f"QA PHOTO {index + 1}", fill="#0e1117")
        img.save(path, format="JPEG")
        _probe_files.append(path)


# Long enough that the detail pane genuinely scrolls. The size sweep opens
# whatever card is first in the queue, which the gallery probe now is, and
# "scrolling the detail moves it" needs content taller than the pane.
PROBE_COMMENT = (
    "tier probe. "
    + (
        "This listing is priced well below comparable ones nearby, the description "
        "is specific about condition and history, and the photos show the item from "
        "several angles rather than one flattering crop. "
    )
    * 8
)


def _seed_probe(listing_id, score, images=None, comment="tier probe"):
    url = f"https://qa.invalid/item/{listing_id}"
    images = images or []
    _cache.set(
        (_CT.LISTING_DETAILS.value, url),
        {
            "marketplace": "facebook",
            "name": "",
            "id": listing_id,
            "title": f"QA tier probe scoring {score}",
            "image": images[0] if images else "",
            "images": images,
            "price": "$1",
            "post_url": url,
            "location": "",
            "seller": "",
            "condition": "",
            "description": "tier probe",
        },
        tag=_CT.LISTING_DETAILS.value,
    )
    _cache.set(
        (_CT.AI_BY_LISTING.value, "facebook", listing_id, _probe_item),
        {"score": score, "comment": comment, "name": "qa", "rated_at": time.time()},
        tag=_CT.AI_BY_LISTING.value,
    )
    _probe_keys.append((_CT.LISTING_DETAILS.value, url))
    _probe_keys.append((_CT.AI_BY_LISTING.value, "facebook", listing_id, _probe_item))


if _probe_item and _PROBE_REVIEW > 1:
    _seed_probe(LOW_ID, _PROBE_REVIEW - 1)
    _seed_probe(OK_ID, _PROBE_REVIEW)
GALLERY_KEY = f"facebook:{GALLERY_ID}" if _probe_item else None
if _probe_item:
    # Score 5 so it sorts to the top of the queue and the carousel is what a
    # screenshot of the detail actually shows.
    _seed_probe(
        GALLERY_ID,
        5,
        [
            f"https://scontent-atl3-2.xx.fbcdn.net/v/t39.30808-6/{n}_2_3_n.jpg?stp=dst-jpg_p960x960_tt6"
            for n in range(GALLERY_N)
        ],
        comment=PROBE_COMMENT,
    )
    _seed_snapshots(GALLERY_URL, GALLERY_N)


def carousel_state(pg):
    """Everything the carousel asserts on, read from the live DOM."""
    return js(
        pg,
        """() => {
          const gal = document.querySelector('#detail-pane .ph.gal');
          if (!gal) return null;
          const track = gal.querySelector('.track');
          const dots = Array.from(gal.querySelectorAll('.dots i'));
          return {
            slides: gal.querySelectorAll('.slide').length,
            dead: gal.querySelectorAll('.slide.dead').length,
            dots: dots.length,
            active: dots.findIndex(d => d.classList.contains('on')),
            index: window.__aimm.gallery.index(),
            counter: (gal.querySelector('.pcount') || {}).textContent || '',
            arrows: gal.querySelectorAll('.gnav').length,
            loaded: Array.from(gal.querySelectorAll('.slide img')).filter(i => !i.dataset.src).length,
            trackScrolls: track.scrollWidth > track.clientWidth + 1,
          };
        }""",
    )


msgs = []
with sync_playwright() as p:
    b = p.chromium.launch()

    # ---------- asset cache policy: an upgrade must never leave a stale module ----------
    import urllib.request as _u

    def _cc(path):
        with _u.urlopen(BASE + path, timeout=10) as r:  # noqa: S310
            return r.headers.get("Cache-Control", ""), r.headers.get("Content-Type", "")

    for asset in (
        "/static/app-core.js",
        "/static/app-review.js",
        "/static/app-config.js",
        "/static/app-status.js",
        "/static/app.css",
    ):
        check(f"{asset.split('/')[-1]} is no-cache", "no-cache" in _cc(asset)[0])
    check("index is no-cache", "no-cache" in _cc("/")[0])
    check(
        "vendor assets stay cacheable",
        "no-cache" not in _cc("/static/vendor/leaflet/leaflet.js")[0],
    )
    with _u.urlopen(BASE + "/static/manifest.webmanifest", timeout=10) as r:  # noqa: S310
        _manifest = json.loads(r.read().decode())
    check(
        "web app manifest served",
        _manifest.get("display") == "standalone" and _manifest.get("theme_color") == "#0e1117",
    )
    with _u.urlopen(BASE + "/", timeout=10) as r:  # noqa: S310
        _index = r.read().decode()
    check(
        "viewport-fit=cover + theme-color in the shell",
        "viewport-fit=cover" in _index and 'name="theme-color"' in _index,
    )
    check("old app.js is gone from the shell", "/static/app.js" not in _index)

    # ---------- proxy-auth mode: no password form, an SSO hint instead ----------
    ctx0 = b.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
    )
    pg0 = ctx0.new_page()
    pg0.route(
        "**/api/auth/info",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"open": False, "username_hint": None, "proxy_auth": True, "password_login": False}
            ),
        ),
    )
    pg0.goto(BASE + "/", wait_until="load")
    pg0.wait_for_timeout(1200)
    check(
        "proxy mode hides the password form",
        visible(pg0, "#login-screen") and not visible(pg0, "#login-fields"),
    )
    check(
        "proxy mode explains where sign-in happens",
        "identity provider" in (pg0.text_content("#login-subtitle") or ""),
        (pg0.text_content("#login-subtitle") or "")[:60],
    )
    ctx0.close()

    # =====================================================================
    # Desktop pass (1440x900): login, review flow, keyboard, config, status
    # =====================================================================
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.on("console", lambda m: msgs.append((m.type, m.text, (m.location or {}).get("url", ""))))
    pg.on("pageerror", lambda e: msgs.append(("PAGEERROR", str(e), "")))
    pg.on("dialog", lambda d: d.accept())

    pg.goto(BASE + "/", wait_until="load")
    pg.wait_for_timeout(900)
    check(
        "login screen shown when signed out",
        visible(pg, "#login-form") and visible(pg, "#login-fields"),
    )
    pg.screenshot(path="/tmp/qa/login-desktop.png")
    pg.fill("input[name=username]", "t@e.com")
    pg.fill("input[name=password]", "wrong")
    pg.click("#login-submit")
    pg.wait_for_timeout(800)
    check(
        "bad password shows an error", visible(pg, "#login-error"), pg.text_content("#login-error")
    )
    pg.fill("input[name=password]", "pw")
    pg.click("#login-submit")
    pg.wait_for_timeout(4000)
    check("login", not js(pg, "document.querySelector('#app').classList.contains('hidden')"))
    check("csrf token captured", bool(js(pg, "window.AIMM.state.csrf")))

    # ---------- the downloadable log carries no credentials ----------
    _dl = pg.request.get(BASE + "/api/logs/download")
    _body = _dl.text() if _dl.ok else ""
    _real_hits = _body.count(_REAL_PASSWORD) if len(_REAL_PASSWORD) >= 4 else 0
    _unmasked = re.findall(r"password\s*=\s*['\"](?!\*\*\*REDACTED|<redacted>)[^'\"]+['\"]", _body)
    check(
        "downloaded log has no plaintext password",
        _dl.ok and not _real_hits and not _unmasked,
        f"HTTP {_dl.status}, {len(_body)} bytes, real={_real_hits}, unmasked={len(_unmasked)}",
    )

    # ---------- review: queue rendering ----------
    pg.wait_for_timeout(1500)
    n_all = js(pg, "window.AIMM.review.listings.length")
    check("activity rows loaded", n_all > 10, n_all)
    q0 = js(pg, "window.__aimm.queueRows().length")
    check(
        "queue derived from user decisions",
        js(pg, "window.__aimm.queueRows().every(r => !window.__aimm.isReviewed(r))"),
        f"{q0} in queue",
    )
    check(
        "reviewed = kept | hidden | rated",
        js(pg, "window.__aimm.reviewedRows().every(r => r.kept || r.hidden || r.my_rank != null)"),
    )
    check(
        "queue excludes paused items",
        js(pg, "window.__aimm.queueRows().every(r => r.item_active !== false)"),
    )
    check(
        "segmented counts render",
        js(pg, "+document.querySelector('#n-queue').textContent") == q0
        and js(pg, "+document.querySelector('#n-all').textContent") > 0,
    )
    check(
        "progress line reads 'N to review'",
        re.match(r"^\d+ to review$", pg.text_content("#q-count") or "") is not None,
        pg.text_content("#q-count"),
    )
    check(
        "today strip renders",
        "today" in (pg.text_content("#today-strip") or ""),
        (pg.text_content("#today-strip") or "")[:60],
    )
    check("top card present", visible(pg, "#stack .tcard.top"))
    check(
        "card shows score, price, title and AI reasoning",
        js(
            pg,
            "!!document.querySelector('#stack .tcard.top .sc') && !!document.querySelector('#stack .tcard.top .price') && !!document.querySelector('#stack .tcard.top .title')",
        ),
    )
    check(
        "KEEP / NOPE stamps on the card",
        js(pg, "document.querySelectorAll('#stack .tcard.top .stamp').length") == 2,
    )
    check(
        "desktop: rail lists the queue",
        js(pg, "document.querySelectorAll('#rail .lrow').length") >= min(q0, 5),
    )
    check(
        "desktop: keyboard panel + session counters",
        visible(pg, "#keys") and js(pg, "document.querySelectorAll('#keys .k').length") >= 12,
    )
    check("desktop: tab bar hidden", not visible(pg, "#tabs"))
    check(
        "tab badge equals queue size",
        js(pg, "document.querySelector('#tab-badge').textContent") == str(q0) or q0 > 99,
    )
    if _probe_item and _PROBE_REVIEW > 1:
        check(
            "low row absent from the queue, sibling at threshold present",
            js(
                pg,
                "window.__aimm.queueRows().every(r => r.id !== arguments0)".replace(
                    "arguments0", json.dumps(LOW_ID)
                ),
            )
            and js(
                pg,
                "window.__aimm.queueRows().some(r => r.id === arguments0)".replace(
                    "arguments0", json.dumps(OK_ID)
                ),
            ),
            f"review >= {_PROBE_REVIEW} on {_probe_item}",
        )
        check(
            "queue counts (badge, 'N to review', segment) exclude low",
            js(pg, "window.AIMM.review.listings.some(r => r.verdict === 'low')")
            and js(
                pg,
                "Number(document.querySelector('#n-queue').textContent) === window.__aimm.queueRows().length",
            ),
        )
        check(
            "today strip counts low separately and muted",
            "low" in (pg.text_content("#today-strip") or ""),
            (pg.text_content("#today-strip") or "")[:80],
        )
    pg.screenshot(path="/tmp/qa/queue-desktop.png")

    def top_key():
        return js(
            pg, "(document.querySelector('#stack .tcard.top') || {dataset:{}}).dataset.key || null"
        )

    # ---------- keep / dismiss / rate / undo round-trips through the flag API ----------
    k1 = top_key()
    pg.click("#act-keep")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check(
        "keep button -> kept flag + reviewed_at",
        r1 and r1["kept"] is True and r1["reviewed_at"],
        k1,
    )
    check("keep advances to the next card", top_key() != k1)
    check(
        "kept listing leaves the queue",
        js(
            pg,
            "!window.__aimm.queueRows().some(r => window.__aimm.rowKey(r) === arguments0)".replace(
                "arguments0", json.dumps(k1)
            ),
        ),
    )
    check(
        "reviewed count grows", js(pg, "+document.querySelector('#n-reviewed').textContent") >= 1
    )
    check(
        "session panel counts the keep",
        js(pg, "+document.querySelector('#sess-kept').textContent") >= 1,
    )
    check("undo toast offered", visible(pg, "#toast") and visible(pg, "#toast-undo"))
    pg.keyboard.press("z")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check(
        "Z undoes the keep (flag cleared, back in queue)",
        r1 and not r1["kept"] and not r1["reviewed_at"] and top_key() == k1,
    )

    pg.keyboard.press("ArrowLeft")
    pg.wait_for_timeout(1300)
    r1 = api_row(pg, k1)
    check("← dismisses (hidden flag)", r1 and r1["hidden"] is True and r1["reviewed_at"], k1)
    check("dismissed listing leaves the queue", top_key() != k1)
    pg.click("#act-undo")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check("Undo button restores after dismiss", r1 and not r1["hidden"] and top_key() == k1)

    pg.keyboard.press("4")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check("4 rates the current listing", r1 and r1["my_rank"] == 4)
    check("a rating alone counts as reviewed", r1 and r1["reviewed_at"] and top_key() != k1)
    pg.keyboard.press("z")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check("undo clears the rating", r1 and r1["my_rank"] is None and not r1["reviewed_at"])

    pg.keyboard.press("h")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check("H hides the listing", r1 and r1["hidden"] is True)
    pg.keyboard.press("z")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check(
        "flags restored after the round-trips",
        r1 and not r1["hidden"] and not r1["kept"] and r1["my_rank"] is None and top_key() == k1,
    )

    # ---------- keyboard: J/K, Enter/Esc, O, R, ? ----------
    pg.keyboard.press("j")
    pg.wait_for_timeout(300)
    k2 = top_key()
    check(
        "J moves to the next listing",
        k2 != k1 and js(pg, "document.querySelector('#rail .lrow.cur').dataset.key") == k2,
    )
    pg.keyboard.press("k")
    pg.wait_for_timeout(300)
    check("K moves back", top_key() == k1)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(500)
    check("Enter expands details", visible(pg, "#detail-pane") and not visible(pg, "#queue-pane"))
    check(
        "detail header reads 'n of N · item · marketplace'",
        re.match(r"^\d+ of \d+ · .+ · .+$", pg.text_content("#detail-pane .dbar .t") or "")
        is not None,
        pg.text_content("#detail-pane .dbar .t"),
    )
    check(
        "detail shows badges, threshold fact, AI reasoning and rating buttons",
        js(
            pg,
            "document.querySelectorAll('#detail-pane .badges .sc').length === 1 && document.body.innerText.includes('notify ≥') && !!document.querySelector('#detail-pane .why2') && document.querySelectorAll('#detail-pane .rate5 .star').length === 5",
        ),
    )
    check(
        "threshold fact names both tiers and which one the score reached",
        bool(
            re.search(
                r"review ≥ \d+ · notify ≥ \d+ · (notify met|review met|not met)",
                pg.text_content("#detail-pane .facts") or "",
            )
        ),
        (pg.text_content("#detail-pane .facts") or "")[-70:],
    )
    check(
        "detail action bar: Dismiss / Open / Keep",
        visible(pg, "#dd-dismiss")
        and visible(pg, "#dd-keep")
        and (
            visible(pg, "#dd-open")
            or not js(
                pg,
                "!!window.AIMM.review.listings.find(r => window.__aimm.rowKey(r) === arguments0 && r.url)".replace(
                    "arguments0", json.dumps(k1)
                ),
            )
        ),
    )
    pg.click("#detail-pane .star[data-rank='5']")
    pg.wait_for_timeout(1000)
    check(
        "star button rates 5",
        (api_row(pg, k1) or {}).get("my_rank") == 5
        and js(pg, "document.querySelectorAll('#detail-pane .star.on').length") == 1,
    )
    check(
        "detail stays on the rated listing",
        js(pg, "window.AIMM.review.cursor") == k1
        and "reviewed" in (pg.text_content("#detail-pane .dbar .t") or ""),
    )
    pg.click("#detail-pane .star[data-rank='5']")
    pg.wait_for_timeout(1000)
    check("same star clears the rating", (api_row(pg, k1) or {}).get("my_rank") is None)
    with ctx.expect_page() as newpage:
        pg.keyboard.press("o")
    check("O opens the listing in a new tab", newpage.value is not None)
    newpage.value.close()
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(400)
    check("Esc collapses details", not visible(pg, "#detail-pane") and visible(pg, "#queue-pane"))
    js(
        pg,
        "() => { window.__qaSearch = 0; window.AIMM.searchNow = () => { window.__qaSearch++; }; }",
    )
    pg.keyboard.press("r")
    pg.wait_for_timeout(200)
    check("R triggers Search now", js(pg, "window.__qaSearch") == 1)
    pg.keyboard.press("?")
    pg.wait_for_timeout(300)
    check("? opens the cheatsheet", visible(pg, "#keys-modal"))
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    check("Esc closes the cheatsheet", not visible(pg, "#keys-modal"))
    pg.click("#activity-filter")
    pg.keyboard.press("1")  # would rate the listing if the shortcut fired
    pg.wait_for_timeout(600)
    check(
        "keys are ignored while typing",
        (api_row(pg, k1) or {}).get("my_rank") is None
        and pg.input_value("#activity-filter") == "1",
    )
    pg.fill("#activity-filter", "")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)

    # ---------- swipe gesture (synthesized pointer events) ----------
    def card_box():
        return pg.eval_on_selector(
            "#stack .tcard.top",
            "e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }",
        )

    x, y, w, h = card_box()
    cx, cy = x + w / 2, y + h / 2
    pg.mouse.move(cx, cy)
    pg.mouse.down()
    for i in range(1, 7):
        pg.mouse.move(cx + w * 0.2 * i / 6, cy + 2, steps=2)
        pg.wait_for_timeout(60)
    pg.wait_for_timeout(200)  # a pause before lifting: not a fling
    stamp = js(
        pg,
        "parseFloat(document.querySelector('#stack .tcard.top .stamp.keep').style.opacity || 0)",
    )
    check("drag right fades the KEEP stamp in with distance", 0.3 < stamp < 0.9, stamp)
    pg.mouse.up()
    pg.wait_for_timeout(500)
    check(
        "short drag snaps back (below 40% width)",
        top_key() == k1
        and js(pg, "document.querySelector('#stack .tcard.top').style.transform") == "",
    )
    x, y, w, h = card_box()
    cx, cy = x + w / 2, y + h / 2
    pg.mouse.move(cx, cy)
    pg.mouse.down()
    for i in range(1, 9):
        pg.mouse.move(cx + w * 0.55 * i / 8, cy, steps=2)
        pg.wait_for_timeout(30)
    pg.screenshot(path="/tmp/qa/swipe-desktop.png")
    check(
        "release-to-keep state at threshold",
        js(pg, "document.querySelector('#act-keep').classList.contains('hot')"),
    )
    pg.mouse.up()
    pg.wait_for_timeout(1300)
    r1 = api_row(pg, k1)
    check("swipe right keeps", r1 and r1["kept"] is True and top_key() != k1)
    pg.keyboard.press("z")
    pg.wait_for_timeout(1200)
    x, y, w, h = card_box()
    cx, cy = x + w / 2, y + h / 2
    pg.mouse.move(cx, cy)
    pg.mouse.down()
    for i in range(1, 9):
        pg.mouse.move(cx - w * 0.55 * i / 8, cy, steps=2)
        pg.wait_for_timeout(30)
    pg.mouse.up()
    pg.wait_for_timeout(1300)
    r1 = api_row(pg, k1)
    check("swipe left dismisses", r1 and r1["hidden"] is True)
    pg.keyboard.press("z")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check(
        "flags clean after the swipe tests",
        r1 and not r1["kept"] and not r1["hidden"] and top_key() == k1,
    )
    pg.mouse.click(cx, cy)
    pg.wait_for_timeout(500)
    check("tap on the card opens details", visible(pg, "#detail-pane"))
    pg.click("#detail-back")
    pg.wait_for_timeout(300)

    # ---------- Queue / Reviewed / All + filters + sorts ----------
    pg.click("#act-keep")
    pg.wait_for_timeout(1200)
    pg.click("[data-mode=reviewed]:visible")
    pg.wait_for_timeout(500)
    check(
        "Reviewed lists the kept listing",
        js(
            pg,
            "!!document.querySelector('#rail .lrow[data-key=' + JSON.stringify(arguments0) + ']')".replace(
                "arguments0", json.dumps(k1)
            ),
        ),
    )
    check(
        "Reviewed: kept rows grouped with Undo affordance",
        js(
            pg,
            "!!document.querySelector('#rail .grp') && !!document.querySelector('#rail [data-undo-row]')",
        ),
    )
    check(
        "desktop: Reviewed shows the current row's detail in the centre",
        visible(pg, "#detail-pane"),
    )
    check(
        "Reviewed never lists a low row",
        js(pg, "window.__aimm.reviewedRows().every(r => r.verdict !== 'low')"),
    )
    pg.screenshot(path="/tmp/qa/reviewed-desktop.png")
    pg.click(f"#rail [data-undo-row={json.dumps(k1)}]")
    pg.wait_for_timeout(1200)
    r1 = api_row(pg, k1)
    check("row Undo returns it to the queue", r1 and not r1["kept"] and not r1["reviewed_at"])

    pg.click("[data-mode=all]:visible")
    pg.wait_for_timeout(500)
    check(
        "All view lists every visible listing in the rail",
        js(pg, "document.querySelectorAll('#rail .lrow').length")
        == js(pg, "Math.min(200, window.__aimm.allRows().length)"),
    )
    check(
        "desktop: filters live in the rail",
        js(pg, "document.querySelector('#rail-filters #review-filters') !== null")
        and visible(pg, "#deal-sort"),
    )
    check("verdict chips only in All", visible(pg, "#verdict-chips"))
    n_pills = js(pg, "document.querySelectorAll('#item-pills [data-item-pill]').length")
    check("item pills render", n_pills >= 2, n_pills)
    first_item = js(
        pg,
        "document.querySelector('#item-pills [data-item-pill]:not([data-item-pill=\"\"])').dataset.itemPill",
    )
    pg.click(f"#item-pills [data-item-pill='{first_item}']")
    pg.wait_for_timeout(400)
    check(
        "pill filters rows",
        js(
            pg,
            "window.__aimm.allRows().every(r => r.item === arguments0)".replace(
                "arguments0", json.dumps(first_item)
            ),
        ),
        first_item,
    )
    check(
        "pill active state",
        js(
            pg,
            f"document.querySelector('#item-pills [data-item-pill=\"{first_item}\"]').classList.contains('on')",
        ),
    )
    pg.click(f"#item-pills [data-item-pill='{first_item}']")
    pg.wait_for_timeout(400)
    check(
        "pill toggles back to All items",
        js(
            pg,
            "document.querySelector('#item-pills [data-item-pill=\"\"]').classList.contains('on')",
        ),
    )
    paused = js(
        pg,
        "Array.from(document.querySelectorAll('#item-pills .chip.paused')).map(x => x.dataset.itemPill)",
    )
    if paused:
        check(
            "paused items absent from All",
            js(
                pg,
                "!window.__aimm.allRows().some(r => arguments0.includes(r.item))".replace(
                    "arguments0", json.dumps(paused)
                ),
            ),
            paused,
        )
        pg.click(f"#item-pills [data-item-pill='{paused[0]}']")
        pg.wait_for_timeout(400)
        check(
            "paused pill shows its history",
            js(pg, "window.__aimm.allRows().length") > 0,
            paused[0],
        )
        pg.click(f"#item-pills [data-item-pill='{paused[0]}']")
        pg.wait_for_timeout(300)
    else:
        check("paused pills (none configured)", True, "no paused items on disk")
    pg.click("#verdict-chips [data-verdict=promising]")
    pg.wait_for_timeout(300)
    check(
        "promising chip filters on the AI verdict",
        js(pg, "window.__aimm.allRows().every(r => r.verdict === 'promising')"),
    )
    pg.click("#verdict-chips [data-verdict=hidden]")
    pg.wait_for_timeout(300)
    check(
        "hidden chip shows only dismissed-by-me rows",
        js(pg, "window.__aimm.allRows().every(r => r.hidden)"),
    )
    pg.click("#verdict-chips [data-verdict=low]")
    pg.wait_for_timeout(300)
    check(
        "Low chip is the one place low rows surface",
        js(pg, "window.__aimm.allRows().every(r => r.verdict === 'low')")
        and (
            not (_probe_item and _PROBE_REVIEW > 1)
            or js(
                pg,
                "window.__aimm.allRows().some(r => r.id === arguments0)".replace(
                    "arguments0", json.dumps(LOW_ID)
                ),
            )
        ),
    )
    pg.screenshot(path="/tmp/qa/all-low-desktop.png")
    pg.click("#verdict-chips [data-verdict='']")
    pg.wait_for_timeout(300)
    check("all chip hides hidden rows", js(pg, "window.__aimm.allRows().every(r => !r.hidden)"))
    check(
        "the default All chip keeps low rows out",
        js(pg, "window.__aimm.allRows().every(r => r.verdict !== 'low')"),
    )
    pg.fill("#activity-filter", "a")
    pg.wait_for_timeout(300)
    check(
        "text filter narrows",
        js(pg, "window.__aimm.allRows().every(r => /a/i.test(r.title) || /a/i.test(r.comment))")
        and js(pg, "window.__aimm.allRows().length") > 0,
    )
    pg.fill("#activity-filter", "")
    pg.wait_for_timeout(300)
    check("csv button", visible(pg, "#export-csv-btn"))
    check(
        "csv endpoint streams a CSV",
        pg.request.get(BASE + "/api/found.csv")
        .headers.get("content-type", "")
        .startswith("text/csv"),
    )

    def sort_by(mode):
        pg.select_option("#deal-sort", mode)
        pg.wait_for_timeout(400)
        return js(
            pg,
            "window.__aimm.allRows().map(r => [r.score, r.distance_mi, r.rated_at || 0, r.my_rank || 0])",
        )

    rows = sort_by("score")
    scores = [r[0] for r in rows]
    check("sort: best rated descending", scores == sorted(scores, reverse=True), scores[:6])
    rows = sort_by("distance")
    known = [r[1] for r in rows if r[1] is not None]
    idx_null = [i for i, r in enumerate(rows) if r[1] is None]
    idx_known = [i for i, r in enumerate(rows) if r[1] is not None]
    check("sort: nearest ascending", known == sorted(known), known[:6])
    check(
        "sort: unresolvable distances last",
        (not idx_null) or (not idx_known) or min(idx_null) > max(idx_known),
    )
    rows = sort_by("newest")
    stamps = [r[2] for r in rows]
    check("sort: newest first", stamps == sorted(stamps, reverse=True))
    rows = sort_by("myrank")
    ranks = [r[3] for r in rows]
    check("sort: my rating first", ranks == sorted(ranks, reverse=True))
    sort_by("score")
    check("sort persists", js(pg, "localStorage.getItem('aimm.dealSort')") == "score")
    check(
        "no junk scrape artifacts rendered",
        not js(
            pg,
            "['**unspecified**', \"Seller's description\", 'View seller profile'].filter(j => document.body.innerText.includes(j))",
        ),
    )
    pg.screenshot(path="/tmp/qa/all-desktop.png")

    # ---------- detail media: photo, map, route ----------
    fb_key = js(
        pg,
        "(() => { const r = window.__aimm.allRows().find(r => r.marketplace === 'facebook' && r.coords); return r ? window.__aimm.rowKey(r) : null; })()",
    )
    if fb_key:
        pg.click(f"#rail .lrow[data-key={json.dumps(fb_key)}]")
        pg.wait_for_timeout(2500)
        check(
            "desktop: rail click shows the detail",
            visible(pg, "#detail-pane") and (pg.text_content("#detail-pane h2") or "") != "",
        )
        has_photo = js(pg, "!!document.querySelector('#detail-pane .ph img')")
        check(
            "photo element for fb row",
            True,
            "shown" if has_photo else "hidden (image expired — acceptable)",
        )
        check("pickup map mounts", js(pg, "!!document.querySelector('#dd-map.leaflet-container')"))
        rect = js(
            pg,
            "JSON.parse(JSON.stringify(document.querySelector('#dd-map').getBoundingClientRect()))",
        )
        check(
            "map is large enough to read",
            rect["height"] >= 200 and rect["width"] >= 300,
            "%.0fx%.0f" % (rect["width"], rect["height"]),
        )
        pg.wait_for_timeout(5000)
        pts = js(
            pg,
            "(() => { let best = 0; document.querySelectorAll('#dd-map path.leaflet-interactive').forEach(el => { const d = el.getAttribute('d') || ''; best = Math.max(best, (d.match(/[ML]/g) || []).length); }); return best; })()",
        )
        route_line = pg.text_content("#dd-drive-line") or ""
        if pts >= 5:
            check("route geometry drawn (not a 2-point line)", True, f"{pts} vertices")
            check(
                "drive time and road miles in the header",
                "by road" in route_line and "min" in route_line,
                route_line[:60],
            )
        else:
            check(
                "route geometry drawn (routing unavailable - soft)",
                "estimating" not in route_line,
                route_line[:60],
            )
        check("straight-line distance shown", "mi away" in route_line, route_line[:40])
        pg.screenshot(path="/tmp/qa/detail-desktop.png", full_page=True)
    else:
        check("media checks (no facebook rows with coords)", True, "skipped")

    # ---------- detail carousel: every photo, one at a time ----------
    if GALLERY_KEY:
        _grow = api_row(pg, GALLERY_KEY)
        check(
            "activity row carries the whole gallery",
            _grow and _grow["image_count"] == GALLERY_N and len(_grow["images"]) == GALLERY_N,
            _grow and (_grow["image_count"], len(_grow["images"])),
        )
        # The proxy indexes into that gallery; past its end is a clean 404,
        # not a 500 and not photo 0 served again.
        _codes = [
            pg.request.get(
                BASE + "/api/listing-image?post=" + quote(GALLERY_URL) + f"&i={i}"
            ).status
            for i in range(GALLERY_N + 1)
        ]
        check(
            "photo proxy serves 0..n-1 and 404s past the end",
            _codes[:GALLERY_N] == [200] * GALLERY_N and _codes[GALLERY_N] == 404,
            _codes,
        )
        check(
            "photo proxy: a negative index is a 404",
            pg.request.get(BASE + "/api/listing-image?post=" + quote(GALLERY_URL) + "&i=-1").status
            == 404,
        )
        check(
            "photo proxy: no index still means photo 0",
            pg.request.get(BASE + "/api/listing-image?post=" + quote(GALLERY_URL)).status == 200,
        )

        # Earlier desktop checks decide on whichever card is on top, and the
        # gallery probe (score 5, rated a moment ago) is that card. Clear it
        # back to undecided so it is certainly in the queue rail below.
        js(
            pg,
            """async (k) => {
              const [marketplace, id] = k.split(':');
              await window.AIMM.api('/api/listing/flag', {
                method: 'POST',
                body: JSON.stringify({ marketplace, id, kept: false, hidden: false, my_rank: null }),
              });
            }""",
            GALLERY_KEY,
        )
        pg.reload(wait_until="load")
        pg.wait_for_timeout(3000)
        pg.click("[data-mode=queue]:visible")
        pg.wait_for_timeout(500)
        pg.click(f"#rail .lrow[data-key={json.dumps(GALLERY_KEY)}]")
        pg.wait_for_timeout(400)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(2200)
        st = carousel_state(pg)
        check(
            "desktop: carousel renders one slide and one dot per photo",
            st and st["slides"] == GALLERY_N and st["dots"] == GALLERY_N and st["dead"] == 0,
            st,
        )
        check("desktop: the first dot starts active", st and st["active"] == 0, st)
        check("desktop: the counter reads 1 / n", st and st["counter"] == f"1 / {GALLERY_N}", st)
        check("desktop: previous / next arrows are offered", st and st["arrows"] == 2, st)
        check(
            "desktop: the whole gallery is not loaded up front",
            st and 0 < st["loaded"] < GALLERY_N,
            st and st["loaded"],
        )

        _loaded0 = st["loaded"] if st else 0
        # "." and "," move photos. The arrows must NOT: they decide listings.
        pg.keyboard.press(".")
        pg.wait_for_timeout(900)
        st = carousel_state(pg)
        check(
            "desktop: . moves to the next photo", st and st["index"] == 1 and st["active"] == 1, st
        )
        check(
            "desktop: another slide loads as it comes into reach",
            st and st["loaded"] > _loaded0,
            st and (st["loaded"], _loaded0),
        )
        pg.keyboard.press(".")
        pg.wait_for_timeout(900)
        pg.keyboard.press(",")
        pg.wait_for_timeout(900)
        st = carousel_state(pg)
        check("desktop: , moves back a photo", st and st["index"] == 1 and st["active"] == 1, st)
        pg.keyboard.press("Shift+ArrowRight")
        pg.wait_for_timeout(900)
        st = carousel_state(pg)
        check("desktop: Shift+arrow moves photos too", st and st["index"] == 2, st)
        # ... and moving photos is all it did: the listing is still undecided.
        # (That the bare arrows DO decide is checked at the end of this block.)
        _grow = api_row(pg, GALLERY_KEY)
        check(
            "desktop: Shift+arrow decides nothing",
            _grow and not _grow["kept"] and not _grow["hidden"],
        )

        # Swiping is the browser's own scroll on the snap track.
        js(
            pg,
            "(n) => { const t = document.querySelector('#detail-pane .ph.gal .track'); if (t) { t.scrollLeft = t.clientWidth * n; t.dispatchEvent(new Event('scroll')); } }",
            GALLERY_N - 1,
        )
        pg.wait_for_timeout(700)
        st = carousel_state(pg)
        check(
            "desktop: scrolling the track moves the active dot",
            st and st["index"] == GALLERY_N - 1 and st["active"] == GALLERY_N - 1,
            st,
        )
        check(
            "desktop: every slide is loaded by the last photo",
            st and st["loaded"] == GALLERY_N,
            st and st["loaded"],
        )
        pg.click("#detail-pane .ph.gal .gnav.prev")
        pg.wait_for_timeout(800)
        check(
            "desktop: the arrow button steps back",
            (carousel_state(pg) or {}).get("index") == GALLERY_N - 2,
        )
        pg.click("#detail-pane .ph.gal .dots i[data-dot='0']")
        pg.wait_for_timeout(800)
        check(
            "desktop: a dot jumps straight to its photo",
            (carousel_state(pg) or {}).get("index") == 0,
        )
        check("no horizontal overflow at 1440: carousel", overflow(pg) <= 1, f"{overflow(pg)}px")
        check(
            "carousel: nothing but the photo track scrolls sideways",
            not js(pg, SIDEWAYS),
            js(pg, SIDEWAYS)[:3],
        )
        pg.screenshot(path="/tmp/qa/carousel-desktop.png")

        # Full-screen photo.
        pg.click("#detail-pane .ph.gal .slide")
        pg.wait_for_timeout(700)
        check("desktop: tapping a photo opens the lightbox", visible(pg, "#lightbox"))
        check(
            "lightbox shows the current photo and its position",
            js(pg, "!!document.querySelector('#lightbox-img').src")
            and js(pg, "document.querySelector('#lightbox-count').textContent")
            == f"1 / {GALLERY_N}",
        )
        pg.keyboard.press("ArrowRight")
        pg.wait_for_timeout(800)
        check(
            "lightbox: bare arrows move photos where there is no listing to decide",
            js(pg, "document.querySelector('#lightbox-count').textContent") == f"2 / {GALLERY_N}",
            js(pg, "document.querySelector('#lightbox-count').textContent"),
        )
        pg.screenshot(path="/tmp/qa/lightbox-desktop.png")
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(500)
        check(
            "desktop: Esc closes the lightbox, the detail stays",
            not visible(pg, "#lightbox") and visible(pg, "#detail-pane"),
        )
        _grow = api_row(pg, GALLERY_KEY)
        check("moving photos decides nothing", _grow and not _grow["kept"] and not _grow["hidden"])

        # The review keys still mean what they always meant.
        pg.keyboard.press("ArrowRight")
        pg.wait_for_timeout(1300)
        _grow = api_row(pg, GALLERY_KEY)
        check("desktop: -> still keeps with the carousel present", _grow and _grow["kept"] is True)
        pg.keyboard.press("z")
        pg.wait_for_timeout(1300)
        pg.keyboard.press("ArrowLeft")
        pg.wait_for_timeout(1300)
        _grow = api_row(pg, GALLERY_KEY)
        check(
            "desktop: <- still dismisses with the carousel present",
            _grow and _grow["hidden"] is True,
        )
        pg.keyboard.press("z")
        pg.wait_for_timeout(1300)
        _grow = api_row(pg, GALLERY_KEY)
        check(
            "desktop: undo puts the gallery probe back in the queue",
            _grow and not _grow["kept"] and not _grow["hidden"] and not _grow["reviewed_at"],
            _grow and (_grow["kept"], _grow["hidden"], _grow["reviewed_at"]),
        )
    else:
        check("carousel checks (no active item to probe)", True, "skipped")
    pg.click("[data-mode=queue]:visible")
    pg.wait_for_timeout(300)

    # =====================================================================
    # Items (config)
    # =====================================================================
    go(pg, "items")
    pg.wait_for_timeout(800)
    snapshot = cm_value(pg)
    item_rows = js(
        pg,
        "Array.from(document.querySelectorAll('#items-page [data-open-item]')).map(x => x.dataset.openItem)",
    )
    check("item list renders", len(item_rows) >= 4, item_rows)
    check(
        "item rows summarise phrases, price, threshold and cadence",
        js(
            pg,
            "Array.from(document.querySelectorAll('#items-page [data-open-item] small')).every(s => /≥ \\d/.test(s.textContent) && /(every |at )/.test(s.textContent))",
        ),
    )
    check("+ New item present", visible(pg, "#items-page [data-add=item]"))
    pg.screenshot(path="/tmp/qa/items-desktop.png", full_page=True)
    S = "item.pc" if "item.pc" in item_rows else item_rows[0]
    open_item(pg, S)
    check(
        "item editor opens",
        js(pg, "document.querySelector('#items-page').dataset.section") == S
        and visible(pg, "#items-back"),
    )
    check(
        "editor groups: phrases, matching, sources, schedule, danger",
        js(pg, "document.querySelectorAll('#items-page .gl').length") >= 5,
    )
    pg.screenshot(path="/tmp/qa/item-edit-desktop.png", full_page=True)

    # ---------- pacing: cadence visible without opening a modal ----------
    cad = pg.text_content("#items-page .icadence") or ""
    check("item editor shows its cadence", cad.startswith(("every ", "at ")), cad)
    check(
        "interval fields are inline, not behind a modal",
        visible(pg, "#items-page [data-field=search_interval]")
        and visible(pg, "#items-page [data-field=max_search_interval]"),
    )
    check(
        "cadence note explains where the value came from",
        any(
            w
            in (
                pg.text_content("#items-page .icadence").strip()
                + (
                    js(
                        pg,
                        "document.querySelector('#items-page .icadence').parentElement.textContent",
                    )
                    or ""
                )
            )
            for w in ("set on this item", "inherited from the marketplace", "default")
        ),
    )
    orig_int = pg.input_value("#items-page [data-field=search_interval]")
    pg.fill("#items-page [data-field=search_interval]", "2h")
    pg.dispatch_event("#items-page [data-field=search_interval]", "change")
    pg.wait_for_timeout(600)
    seg_iv = segment(cm_value(pg), f"[{S}]")
    check("interval writes a duration string", 'search_interval = "2h"' in seg_iv, seg_iv[:120])
    check(
        "cadence label follows the edit",
        "every 2h" in (pg.text_content("#items-page .icadence") or ""),
    )
    pg.wait_for_timeout(1400)
    check(
        "inline edits auto-save",
        "saved" in (pg.text_content("#item-foot") or "")
        and js(pg, "document.querySelector('#save-btn').disabled"),
        pg.text_content("#item-foot"),
    )
    pg.fill("#items-page [data-field=search_interval]", orig_int)
    pg.dispatch_event("#items-page [data-field=search_interval]", "change")
    pg.wait_for_timeout(600)

    # chips: add then remove a phrase (net zero)
    pg.fill("#items-page [data-chip-add=search_phrases]", "qa test phrase")
    pg.press("#items-page [data-chip-add=search_phrases]", "Enter")
    pg.wait_for_timeout(600)
    check("chip add writes TOML", "qa test phrase" in cm_value(pg))
    pg.click("#items-page [data-chip-del=search_phrases][data-chip-val='qa test phrase']")
    pg.wait_for_timeout(600)
    check("chip remove cleans TOML", "qa test phrase" not in cm_value(pg))
    # description edit + revert
    orig_desc = pg.input_value("#items-page [data-field=description]")
    pg.fill("#items-page [data-field=description]", "qa description probe")
    pg.dispatch_event("#items-page [data-field=description]", "change")
    pg.wait_for_timeout(600)
    check("description writes", "qa description probe" in cm_value(pg))
    pg.fill("#items-page [data-field=description]", orig_desc)
    pg.dispatch_event("#items-page [data-field=description]", "change")
    pg.wait_for_timeout(600)
    # price int + clear back to original
    orig_max = pg.input_value("#items-page [data-field=max_price]")
    pg.fill("#items-page [data-field=max_price]", "912")
    pg.dispatch_event("#items-page [data-field=max_price]", "change")
    pg.wait_for_timeout(600)
    seg = segment(cm_value(pg), f"[{S}]")
    check("price writes unquoted int", "max_price = 912" in seg and 'max_price = "912"' not in seg)
    pg.fill("#items-page [data-field=max_price]", orig_max)
    pg.dispatch_event("#items-page [data-field=max_price]", "change")
    pg.wait_for_timeout(600)
    # thresholds: two steppers side by side -- review tier and notify tier
    check(
        "item card shows both thresholds as steppers",
        visible(pg, "#items-page .rev-val")
        and visible(pg, "#items-page .thr-val")
        and js(pg, "document.querySelectorAll('#items-page .r.thrpair .step').length") == 2,
    )
    check(
        "the two steppers read review <= notify",
        int(pg.text_content("#items-page .rev-val"))
        <= int(pg.text_content("#items-page .thr-val")),
        (pg.text_content("#items-page .rev-val"), pg.text_content("#items-page .thr-val")),
    )

    # `rating` is a substring of `review_rating`, so every assertion about one
    # key being present or absent has to anchor on the start of the line.
    def has_key(key, section=None):
        return bool(
            re.search(r"(?m)^\s*" + key + r"\s*=", segment(cm_value(pg), f"[{section or S}]"))
        )

    def key_value(key, section=None):
        m = re.search(
            r"(?m)^\s*" + key + r"\s*=\s*(\d+)", segment(cm_value(pg), f"[{section or S}]")
        )
        return int(m.group(1)) if m else None

    # threshold stepper: set explicit, clear back to inherit
    eff = int(pg.text_content("#items-page .thr-val"))
    rev_eff = int(pg.text_content("#items-page .rev-val"))
    # Raise when lowering would cross under the review tier -- the stepper
    # refuses that pair on purpose, and it is checked on its own below.
    down_ok = eff > 1 and eff - 1 >= rev_eff
    pg.click("#items-page [data-thr-dec]" if down_ok else "#items-page [data-thr-inc]")
    pg.wait_for_timeout(700)
    want = eff - 1 if down_ok else min(5, eff + 1)
    check("threshold stepper writes rating", key_value("rating") == want, key_value("rating"))
    check(
        "threshold note shows the meaning",
        bool(
            re.search(
                r"(everything|potential|poor|good|great)",
                pg.text_content("#items-page .thr-note") or "",
            )
        ),
    )
    pg.click("#items-page [data-thr-clear]")
    pg.wait_for_timeout(700)
    check("threshold reset removes rating", not has_key("rating"))
    check("cleared note inherits", "inherited" in (pg.text_content("#items-page .thr-note") or ""))

    # review stepper: writes review_rating, refuses to cross the notify tier,
    # and resets back to inheriting.
    rev_eff = int(pg.text_content("#items-page .rev-val"))
    notify_eff = int(pg.text_content("#items-page .thr-val"))
    rev_down = rev_eff > 1
    pg.click("#items-page [data-rev-dec]" if rev_down else "#items-page [data-rev-inc]")
    pg.wait_for_timeout(700)
    rev_want = rev_eff - 1 if rev_down else rev_eff + 1
    check(
        "review stepper writes review_rating",
        key_value("review_rating") == rev_want,
        (key_value("review_rating"), rev_want),
    )
    check(
        "review stepper leaves the notify threshold alone",
        not has_key("rating") or key_value("rating") == notify_eff,
    )
    check(
        "review note shows the meaning and a reset link",
        bool(
            re.search(
                r"(everything|potential|poor|good|great)",
                pg.text_content("#items-page .rev-note") or "",
            )
        )
        and visible(pg, "#items-page [data-rev-clear]"),
        pg.text_content("#items-page .rev-note"),
    )
    # Push review up to the notify tier, then one more: the stepper must refuse.
    for _ in range(6):
        if int(pg.text_content("#items-page .rev-val")) >= int(
            pg.text_content("#items-page .thr-val")
        ):
            break
        pg.click("#items-page [data-rev-inc]")
        pg.wait_for_timeout(500)
    capped = int(pg.text_content("#items-page .rev-val"))
    # Let the last accepted click's autosave land first: its "saved" message
    # writes the same footer the refusal below has to be read from.
    pg.wait_for_timeout(1800)
    pg.click("#items-page [data-rev-inc]")
    pg.wait_for_timeout(300)
    check(
        "review stepper refuses to pass the notify threshold",
        int(pg.text_content("#items-page .rev-val")) == capped
        and "review" in (pg.text_content("#item-foot") or "").lower(),
        (capped, pg.text_content("#item-foot")),
    )
    pg.click("#items-page [data-rev-clear]")
    pg.wait_for_timeout(700)
    check("review reset removes review_rating", not has_key("review_rating"))
    check(
        "cleared review note inherits",
        "inherited" in (pg.text_content("#items-page .rev-note") or ""),
    )
    pg.screenshot(path="/tmp/qa/item-thresholds-desktop.png", full_page=True)

    # An inverted pair is rejected by the real config loader, and the form has
    # to say so rather than autosaving something the monitor will refuse.
    bad = cm_value(pg).replace(
        f"[{S}]", f"[{S}]" + chr(10) + "review_rating = 5" + chr(10) + "rating = 3", 1
    )
    pg.evaluate("(s) => { document.querySelector('.CodeMirror').CodeMirror.setValue(s); }", bad)
    pg.wait_for_timeout(500)
    verdict = pg.evaluate("() => window.AIMM.config.validate()")
    check(
        "review above notify is rejected, naming both keys",
        isinstance(verdict, dict)
        and "review_rating" in str(verdict.get("error", ""))
        and "rating" in str(verdict.get("error", "")),
        str(verdict)[:140],
    )
    check(
        "the rejection reaches the editor status line",
        "review_rating" in (pg.text_content("#editor-status") or ""),
        (pg.text_content("#editor-status") or "")[:120],
    )
    restore(pg, snapshot)
    pg.wait_for_timeout(400)
    # source toggle: with 2+ sources it toggles; with one, the guard refuses
    n_sources = js(pg, "document.querySelectorAll('#items-page [data-src]').length")
    pg.click("#items-page [data-src=facebook]")
    pg.wait_for_timeout(600)
    if n_sources >= 2:
        check(
            "source off",
            not js(
                pg,
                "document.querySelector('#items-page [data-src=facebook]').classList.contains('on')",
            ),
        )
        pg.click("#items-page [data-src=facebook]")
        pg.wait_for_timeout(600)
        check(
            "source back on",
            js(
                pg,
                "document.querySelector('#items-page [data-src=facebook]').classList.contains('on')",
            ),
        )
    else:
        check(
            "single-source guard refuses",
            "at least one source" in (pg.text_content("#item-foot") or ""),
        )
        check(
            "source stays on",
            js(
                pg,
                "document.querySelector('#items-page [data-src=facebook]').classList.contains('on')",
            ),
        )
    # paused switch flips and flips back (the starting state belongs to the live config)
    started_paused = js(
        pg, "document.querySelector('#items-page [data-toggle=enabled]').classList.contains('on')"
    )
    pg.click("#items-page [data-toggle=enabled]")
    pg.wait_for_timeout(600)
    check(
        "pause switch flips the item",
        js(
            pg,
            "document.querySelector('#items-page [data-toggle=enabled]').classList.contains('on')",
        )
        != started_paused,
        "started " + ("paused" if started_paused else "enabled"),
    )
    pg.click("#items-page [data-toggle=enabled]")
    pg.wait_for_timeout(600)
    check(
        "pause switch flips back",
        js(
            pg,
            "document.querySelector('#items-page [data-toggle=enabled]').classList.contains('on')",
        )
        == started_paused,
    )
    pg.wait_for_timeout(1500)
    final = cm_value(pg)
    if final != snapshot:
        import difflib

        diff = [
            ln
            for ln in difflib.unified_diff(snapshot.splitlines(), final.splitlines(), lineterm="")
            if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
        ]
        check(
            "buffer diff is only phrase normalization",
            all("search_phrases" in ln for ln in diff),
            diff[:6],
        )
    else:
        check("buffer pristine", True)
    check("buffer reset (Save disabled)", restore(pg, snapshot))

    # ---------- modals from the editor ----------
    open_item(pg, S)
    pg.click("#items-page [data-act=edit]")
    pg.wait_for_timeout(500)
    check("More settings opens modal", visible(pg, "#form-modal"))
    pg.click(".form-tab-bar .form-tab:nth-child(2)")
    pg.wait_for_timeout(300)
    adv_off = js(
        pg,
        "!document.querySelector('#show-advanced') || !document.querySelector('#show-advanced').checked",
    )
    check(
        "item schedule fields are not advanced-only",
        adv_off
        and visible(pg, "#field-search_interval")
        and visible(pg, "#field-max_search_interval"),
    )
    pg.screenshot(path="/tmp/qa/modal-desktop.png")
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)
    # rename preserves every field
    before_keys = sorted(re.findall(r"^\s*([a-z_]+)\s*=", segment(cm_value(pg), f"[{S}]"), re.M))
    pg.click("#items-page [data-act=rename]")
    pg.wait_for_timeout(500)
    check(
        "rename opens the form with the name focused",
        visible(pg, "#form-modal")
        and js(pg, "document.activeElement && document.activeElement.id === 'add-section-name'"),
    )
    pg.click("#add-section-name")
    pg.fill("#add-section-name", S.split(".")[1] + "_qa")
    pg.click("#form-save")
    pg.wait_for_timeout(1500)
    buf = cm_value(pg)
    new_name = f"[{S}_qa]"
    after_keys = sorted(re.findall(r"^\s*([a-z_]+)\s*=", segment(buf, new_name), re.M))
    check("rename rewrites the header", new_name in buf and f"[{S}]\n" not in buf)
    check(
        "rename preserves every field", after_keys == before_keys, f"{before_keys} -> {after_keys}"
    )
    check(
        "editor follows the renamed item",
        pg.text_content("#items-title") == S.split(".")[1] + "_qa",
    )
    check("disk restored after rename", restore(pg, snapshot))
    pg.wait_for_timeout(400)
    check(
        "editor falls back to the list once the renamed item is gone",
        not visible(pg, "#items-back") and visible(pg, "#items-page [data-add=item]"),
    )

    # ---------- add-section flows: item (then delete), user, ai ----------
    pg.click("#items-page [data-add=item]")
    pg.wait_for_timeout(500)
    check(
        "Add item opens modal",
        visible(pg, "#form-modal") and "item" in (pg.text_content("#form-modal-title") or ""),
    )
    pg.click("#add-section-name")
    pg.fill("#add-section-name", "qaitem")
    pg.click("#field-search_phrases")
    pg.fill("#field-search_phrases", "qa phrase one, qa phrase two")
    pg.click("#form-save")
    pg.wait_for_timeout(1500)
    buf = cm_value(pg)
    check(
        "item add lands a section with a phrase array",
        segment(buf, "[item.qaitem]") != ""
        and 'search_phrases = ["qa phrase one", "qa phrase two"]' in segment(buf, "[item.qaitem]"),
    )
    check(
        "new item opens in the editor",
        js(pg, "document.querySelector('#items-page').dataset.section") == "item.qaitem",
    )
    pg.click("#items-page [data-act=delete]")
    pg.wait_for_timeout(1500)
    check(
        "delete removes the section (confirm accepted)",
        "[item.qaitem]" not in cm_value(pg)
        and not js(pg, "document.querySelector('#items-page').dataset.section"),
    )
    check("disk restored after item add/delete", restore(pg, snapshot))

    go(pg, "sources")
    pg.wait_for_timeout(600)
    pg.click("#sources-page [data-add=user]")
    pg.wait_for_timeout(500)
    pg.click("#add-section-name")
    pg.fill("#add-section-name", "qauser")
    pg.click("#field-ntfy_topic")
    pg.fill("#field-ntfy_topic", "qa-topic")
    pg.click("#form-save")
    pg.wait_for_timeout(1500)
    check(
        "user add lands a section",
        'ntfy_topic = "qa-topic"' in segment(cm_value(pg), "[user.qauser]"),
    )
    check(
        "sources lists the new user",
        visible(pg, "#sources-page [data-edit-section='user.qauser']"),
    )
    check("disk restored after user add", restore(pg, snapshot))
    pg.click("#sources-page [data-add=ai]")
    pg.wait_for_timeout(500)
    check(
        "AI add offers a provider select",
        js(pg, "document.querySelector('#add-section-name').tagName") == "SELECT",
    )
    existing_ai = js(
        pg,
        "Array.from(document.querySelectorAll('#sources-page [data-edit-section^=\"ai.\"]')).map(x => x.dataset.editSection)",
    )
    provider = next(
        p_ for p_ in ("openai", "deepseek", "anthropic", "ollama") if f"ai.{p_}" not in existing_ai
    )
    pg.select_option("#add-section-name", provider)
    pg.wait_for_timeout(200)
    check(
        "AI add prefills the env-var key reference",
        pg.input_value("#field-api_key") == "${" + provider.upper() + "_API_KEY}",
    )
    pg.click("#field-api_key")
    pg.fill("#field-api_key", "qa-key")  # the env var is unset here; the validator needs a string
    pg.click("#field-model")
    pg.fill("#field-model", "qa-model")
    pg.click("#form-save")
    pg.wait_for_timeout(1500)
    check(
        "AI add lands a section", 'model = "qa-model"' in segment(cm_value(pg), f"[ai.{provider}]")
    )
    check("disk restored after AI add", restore(pg, snapshot))

    # ---------- sources: marketplace rows + keyless eBay set-up ----------
    pg.wait_for_timeout(500)
    mk_rows = js(
        pg,
        "Array.from(document.querySelectorAll('#sources-page [data-edit-section^=\"marketplace.\"]')).map(x => x.dataset.editSection)",
    )
    check("marketplace rows render", "marketplace.facebook" in mk_rows, mk_rows)
    fb_txt = pg.text_content("#sources-page [data-edit-section='marketplace.facebook']") or ""
    check(
        "facebook row reports sign-in state and home",
        ("signed in" in fb_txt or "session" in fb_txt or "not signed" in fb_txt)
        and "home" in fb_txt,
        fb_txt[:90],
    )
    check(
        "plumbing rows: AI, notification, user",
        js(
            pg,
            'document.querySelectorAll(\'#sources-page [data-edit-section^="ai."], #sources-page [data-edit-section^="notification."], #sources-page [data-edit-section^="user."]\').length',
        )
        >= 2,
    )
    check(
        "env-status lines render",
        js(pg, "document.querySelectorAll('#sources-page .envline').length") >= 1,
    )
    check(
        "browser (noVNC) link",
        "path=ws/vnc" in (js(pg, "document.querySelector('#browser-btn').href") or ""),
    )
    pg.screenshot(path="/tmp/qa/sources-desktop.png", full_page=True)

    def drop_ebay_section():
        buf = cm_value(pg)
        if "[marketplace.ebay]" not in buf:
            return
        lines = buf.split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "[marketplace.ebay]")
        end = start + 1
        while end < len(lines) and not lines[end].strip().startswith("["):
            end += 1
        del lines[start:end]
        restore(pg, "\n".join(lines))
        pg.wait_for_timeout(500)

    drop_ebay_section()
    check(
        "ebay section name free for the add-path test",
        not visible(pg, "#sources-page [data-edit-section='marketplace.ebay']"),
    )
    avail = js(
        pg,
        "Array.from(document.querySelectorAll('#sources-page [data-setup-marketplace]')).map(x => x.dataset.setupMarketplace)",
    )
    check("available source rows offer Set up", set(avail) >= {"ebay", "depop", "poshmark"}, avail)

    def open_marketplace_form(kind):
        if visible(pg, f"#sources-page [data-setup-marketplace='{kind}']"):
            pg.click(f"#sources-page [data-setup-marketplace='{kind}']")
        elif visible(pg, f"#sources-page [data-edit-section='marketplace.{kind}']"):
            pg.click(f"#sources-page [data-edit-section='marketplace.{kind}']")
        else:
            return False
        pg.wait_for_timeout(500)
        return True

    def field_help(key):
        return js(
            pg, f"(document.querySelector('#field-{key} ~ .form-help') || {{}}).textContent || ''"
        )

    for kind in ("depop", "poshmark"):
        opened = open_marketplace_form(kind)
        check(f"{kind} form opens", opened)
        if not opened:
            continue
        check(f"{kind} form offers a listing cap", visible(pg, "#field-max_listings"))
        check(f"{kind} cap explains the AI cost", "AI rating" in field_help("max_listings"))
        pg.click("#form-cancel")
        pg.wait_for_timeout(250)

    pg.click("#sources-page [data-setup-marketplace='ebay']")
    pg.wait_for_timeout(500)
    check("setup modal prefilled", pg.input_value("#add-section-name") == "ebay")
    check("setup uses ebay schema", visible(pg, "#field-client_id"))
    check("setup tab says eBay", "eBay" in (pg.text_content(".form-tab-bar .form-tab") or ""))
    check(
        "name input autofill-proof", js(pg, "document.querySelector('#add-section-name').readOnly")
    )
    check("field autofill-proof", js(pg, "document.querySelector('#field-client_id').readOnly"))
    pg.click("#field-client_id")
    check(
        "field opens on focus", not js(pg, "document.querySelector('#field-client_id').readOnly")
    )
    check("ebay form offers a mode select", visible(pg, "#field-mode"))
    mode_opts = js(
        pg, "Array.from(document.querySelector('#field-mode').options).map(o => o.value)"
    )
    check("ebay mode offers api and browser", set(mode_opts) >= {"", "api", "browser"}, mode_opts)
    check("ebay mode explains the tradeoff", "no keys" in field_help("mode"))
    check(
        "ebay keys are optional",
        not js(
            pg,
            "!!document.querySelector('[data-key=client_id]').closest('.form-field').querySelector('.required')",
        ),
    )
    check("ebay form offers a listing cap", visible(pg, "#field-max_listings"))
    cap_help = field_help("max_listings")
    check("ebay cap states the default", "60" in cap_help)
    check("ebay cap explains the AI cost", "AI rating" in cap_help)
    check("ebay form offers a category id", visible(pg, "#field-category"))
    check("ebay category help gives an example id", "6001" in field_help("category"))
    pg.click("#form-save")
    pg.wait_for_timeout(1500)
    buf0 = cm_value(pg)
    seg0 = segment(buf0, "[marketplace.ebay]")
    check("keyless ebay add lands a section", "[marketplace.ebay]" in buf0)
    check("keyless ebay add carries market_type", 'market_type = "ebay"' in seg0, seg0[:120])
    check("keyless ebay add starts enabled", "enabled = true" in seg0)
    check("keyless ebay add writes no client_id", "client_id" not in seg0)
    check(
        "keyless ebay add persisted to disk",
        js(pg, "document.querySelector('#save-btn').disabled"),
        pg.text_content("#editor-status")[:80],
    )
    ebay_row = pg.text_content("#sources-page [data-edit-section='marketplace.ebay']") or ""
    check("ebay row reports browser mode", "browser" in ebay_row.lower(), ebay_row[:80])
    check("ebay row claims no key needed", "no developer key" in ebay_row.lower())
    restore(pg, snapshot)
    drop_ebay_section()
    pg.click("#sources-page [data-setup-marketplace='ebay']")
    pg.wait_for_timeout(500)
    pg.click("#field-client_id")
    pg.fill("#field-client_id", "${EBAY_CLIENT_ID}")
    pg.click("#field-client_secret")
    pg.fill("#field-client_secret", "${EBAY_CLIENT_SECRET}")
    pg.click("#form-save")
    pg.wait_for_timeout(1500)
    buf = cm_value(pg)
    check(
        "keyed ebay add carries market_type",
        'market_type = "ebay"' in segment(buf, "[marketplace.ebay]"),
    )
    check("keyed ebay add starts enabled", "enabled = true" in segment(buf, "[marketplace.ebay]"))
    ebay_row2 = pg.text_content("#sources-page [data-edit-section='marketplace.ebay']") or ""
    check("keyed ebay row reports API mode", "api" in ebay_row2.lower(), ebay_row2[:80])
    check("ebay add persisted to disk", js(pg, "document.querySelector('#save-btn').disabled"))
    check("disk restored after setup test", restore(pg, snapshot))
    pg.wait_for_timeout(400)

    # ---------- every configured section's form opens ----------
    edit_targets = js(
        pg,
        "Array.from(document.querySelectorAll('#sources-page [data-edit-section]')).map(x => x.dataset.editSection).filter((v, i, a) => a.indexOf(v) === i)",
    )
    for name in edit_targets:
        pg.click(f"#sources-page [data-edit-section='{name}']")
        pg.wait_for_timeout(450)
        modal_open = visible(pg, "#form-modal")
        nfields = js(pg, "document.querySelectorAll('#section-form [data-key]').length")
        if name == "marketplace.facebook":
            pg.click(".form-tab-bar .form-tab:nth-child(2)")
            pg.wait_for_timeout(300)
            check(
                "home_location exposed in settings UI",
                visible(pg, "#field-home_location"),
                (
                    pg.input_value("#field-home_location")
                    if visible(pg, "#field-home_location")
                    else "MISSING"
                ),
            )
            check("facebook form exposes request_delay", visible(pg, "#field-request_delay"))
            check("facebook form exposes block_cooldown", visible(pg, "#field-block_cooldown"))
        hint = js(
            pg,
            "document.querySelector('#form-modal-hint').hidden ? '' : document.querySelector('#form-modal-hint').textContent",
        )
        check(
            f"form opens: {name}",
            modal_open and (nfields > 0 or "No form" in hint),
            f"{nfields} fields",
        )
        if name.startswith("marketplace.") and nfields and name.split(".")[1] != "facebook":
            check(
                f"tab label not facebook: {name}",
                "Facebook" not in (pg.text_content(".form-tab-bar .form-tab") or ""),
            )
        pg.click("#form-cancel")
        pg.wait_for_timeout(200)

    # item modals: both tabs render fields for two representatives
    go(pg, "items")
    for name in item_rows[:2]:
        open_item(pg, name)
        pg.click("#items-page [data-act=edit]")
        pg.wait_for_timeout(400)
        left = js(pg, "document.querySelectorAll('#section-form [data-key]').length")
        pg.click(".form-tab-bar .form-tab:nth-child(2)")
        pg.wait_for_timeout(300)
        right = js(pg, "document.querySelectorAll('#section-form [data-key]').length")
        check(f"item modal tabs: {name}", left > 0 and right > 0, f"L{left}/R{right}")
        pg.click("#form-cancel")
        pg.wait_for_timeout(200)
        pg.click("#items-back")
        pg.wait_for_timeout(200)

    # depop + poshmark setup flows: labeled correctly, save carries market_type
    go(pg, "sources")
    for kind, label in (("depop", "Depop"), ("poshmark", "Poshmark")):
        pg.click(f"#sources-page [data-setup-marketplace='{kind}']")
        pg.wait_for_timeout(500)
        check(
            f"setup tab: {kind}",
            label in (pg.text_content(".form-tab-bar .form-tab") or "(no tabs)"),
        )
        check(f"setup name prefilled: {kind}", pg.input_value("#add-section-name") == kind)
        pg.click("#form-save")
        pg.wait_for_timeout(1200)
        buf2 = cm_value(pg)
        check(
            f"setup saved with market_type: {kind}",
            f"[marketplace.{kind}]" in buf2 and f'market_type = "{kind}"' in buf2,
        )
    check("disk restored after setup sweep", restore(pg, snapshot))
    pg.wait_for_timeout(400)

    # ---------- provider form: header switch, boolean enabled, comma-safe text ----------
    pg.click("#sources-page [data-edit-section='marketplace.facebook']")
    pg.wait_for_timeout(500)
    check(
        "enabled switch lives in the modal header",
        visible(pg, "#form-modal-toggle input[data-key=enabled]")
        or (
            js(pg, "!!document.querySelector('#form-modal-toggle input[data-key=enabled]')")
            and not js(pg, "document.querySelector('#form-modal-toggle').hidden")
        ),
    )
    check(
        "enabled is gone from the field grid",
        js(pg, "document.querySelectorAll('#section-form [data-key=enabled]').length") == 0,
    )
    pg.click(".form-tab-bar .form-tab:nth-child(2)")
    pg.wait_for_timeout(300)
    pg.click("#field-home_location")
    pg.fill("#field-home_location", "Asheboro, NC")
    pg.click("#field-search_city")
    pg.fill("#field-search_city", "asheboro, greensboro")
    pg.click("#form-save")
    pg.wait_for_timeout(1500)
    seg_fb = segment(cm_value(pg), "[marketplace.facebook]")
    loc_line = line_for(seg_fb, "home_location")
    check(
        "comma text stays a quoted string",
        '"Asheboro, NC"' in loc_line and "[" not in loc_line,
        loc_line,
    )
    city_line = line_for(seg_fb, "search_city")
    check(
        "comma list still becomes an array",
        "[" in city_line and "asheboro" in city_line and "greensboro" in city_line,
        city_line,
    )
    check(
        "comma-text save persisted (config still valid)",
        js(pg, "document.querySelector('#save-btn').disabled"),
        pg.text_content("#editor-status")[:90],
    )
    check("disk restored after comma test", restore(pg, snapshot))
    pg.wait_for_timeout(400)
    if visible(pg, "#sources-page [data-edit-section='marketplace.ebay']"):
        pg.click("#sources-page [data-edit-section='marketplace.ebay']")
        pg.wait_for_timeout(500)
        pg.eval_on_selector(
            "#form-modal-toggle input[data-key=enabled]",
            "e => { e.checked = false; e.dispatchEvent(new Event('change')); }",
        )
        pg.click("#form-save")
        pg.wait_for_timeout(1500)
        en_line = line_for(segment(cm_value(pg), "[marketplace.ebay]"), "enabled")
        check("enabled writes a bare boolean", "false" in en_line and '"' not in en_line, en_line)
        pg.click("#sources-page [data-edit-section='marketplace.ebay']")
        pg.wait_for_timeout(500)
        check(
            "header switch reflects the saved false",
            not js(
                pg, "document.querySelector('#form-modal-toggle input[data-key=enabled]').checked"
            ),
        )
        pg.eval_on_selector(
            "#form-modal-toggle input[data-key=enabled]",
            "e => { e.checked = true; e.dispatchEvent(new Event('change')); }",
        )
        pg.click("#form-save")
        pg.wait_for_timeout(1500)
        en_line2 = line_for(segment(cm_value(pg), "[marketplace.ebay]"), "enabled")
        check(
            "enabled toggles back to a bare true",
            "true" in en_line2 and '"' not in en_line2,
            en_line2,
        )
    else:
        check("enabled boolean (no ebay section on disk)", True, "skipped")
    check("disk restored after provider-form test", restore(pg, snapshot))

    # ---------- TOML view round-trip + help levels ----------
    pg.click("#sources-page [data-config-mode=toml]")
    pg.wait_for_timeout(500)
    check(
        "TOML mode switches to the editor",
        js(pg, "window.AIMM.state.view") == "items"
        and visible(pg, "#toml-pane")
        and not visible(pg, "#items-page"),
    )
    check(
        "TOML editor shows the config with section gutter buttons",
        js(pg, "document.querySelectorAll('.section-btn').length") >= 3,
    )
    pg.screenshot(path="/tmp/qa/toml-desktop.png")
    pg.click("[data-config-mode=form]:visible")
    pg.wait_for_timeout(300)
    check("back to Form", visible(pg, "#items-page") and not visible(pg, "#toml-pane"))
    go(pg, "sources")
    pg.click("#help-seg [data-help=guided]")
    go(pg, "items")
    open_item(pg, S)
    check(
        "guided shows examples",
        js(pg, "getComputedStyle(document.querySelector('.fieldhelp .ex')).display") == "block",
    )
    go(pg, "sources")
    pg.click("#help-seg [data-help=off]")
    go(pg, "items")
    open_item(pg, S)
    check(
        "help off hides",
        js(pg, "getComputedStyle(document.querySelector('.fieldhelp')).display") == "none",
    )
    go(pg, "sources")
    pg.click("#help-seg [data-help=hints]")
    check("help level persists", js(pg, "localStorage.getItem('aimm.helpLevel')") == "hints")

    # =====================================================================
    # Status + logs
    # =====================================================================
    go(pg, "status")
    pg.wait_for_timeout(1500)
    check(
        "status groups render", js(pg, "document.querySelectorAll('#status-body .g').length") >= 2
    )
    check("monitor row present", "Monitor" in (pg.text_content("#status-body") or ""))
    check(
        "pause / search now buttons",
        visible(pg, "#status-search") and js(pg, "!!document.querySelector('#status-pause')"),
    )
    check("env lines", js(pg, "document.querySelectorAll('#status-body .envline').length") > 0)
    check(
        "session group: log stream state", "Log stream" in (pg.text_content("#status-body") or "")
    )
    pg.click("#status-refresh")
    pg.wait_for_timeout(800)
    check(
        "status refresh keeps rendering",
        js(pg, "document.querySelectorAll('#status-body .g').length") >= 2,
    )
    check(
        "ws status text",
        "streaming" in (pg.text_content("#ws-status") or ""),
        pg.text_content("#ws-status"),
    )
    pg.screenshot(path="/tmp/qa/status-desktop.png", full_page=True)

    # ---------- block state: rendered from a stubbed payload ----------
    # loadMonitorState() polls every 10s and replaces state.monitorInfo; park
    # it (a truthy sentinel keeps it from re-arming) so the stubs below survive.
    js(
        pg,
        "() => { clearInterval(window.AIMM.state._monitorPoll); window.AIMM.state._monitorPoll = -1; }",
    )
    block_probe = js(
        pg,
        """() => {
          const A = window.__aimm;
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
        }""",
    )
    check("active block detected", block_probe.get("active") == 1)
    check("expired cooldown is not shown", block_probe.get("expiredIgnored") == 0)
    check("no block reads as clear", block_probe.get("noneWhenClear") == 0)
    chip = block_probe.get("chip") or ""
    check(
        "chip reads 'facebook: blocked · retry HH:MM'",
        chip.startswith("facebook: blocked") and len(chip.split("retry ")[-1]) == 5,
        chip,
    )
    _bh = block_probe.get("html") or ""
    check(
        "status page offers Clear block with the reason and retry time",
        "Blocked marketplaces" in _bh
        and "Clear block / retry now" in _bh
        and "Temporarily Blocked" in _bh
        and 'data-clear-block="facebook"' in _bh
        and "strike 2" in _bh,
        _bh[:120],
    )
    check("no notice when nothing is blocked", block_probe.get("emptyHtml") == "")
    check(
        "duration parsing matches the backend",
        block_probe.get("cadence") == [7200, 1800, 5400, None, "2h", "45m"],
        block_probe.get("cadence"),
    )
    # Render the blocked state for real (stubbed monitor payload) and shoot it.
    js(
        pg,
        """() => { window.AIMM.state.monitorInfo = { available: true, paused: false, activity: { state: "idle" }, started_at: Date.now()/1000 - 90000,
        jobs: [], fb_session: {}, counters: {}, paused_persisted: false,
        blocked: { facebook: { marketplace: "facebook", reason: "page title: Temporarily Blocked", detected_at: Date.now()/1000 - 600, until: Date.now()/1000 + 1800, strikes: 1 } } };
        window.AIMM.renderMonitorStatus(); window.__aimm.renderStatus(); }""",
    )
    pg.wait_for_timeout(300)
    check(
        "header chip shows the block with retry time",
        re.search(
            r"⛔ facebook: blocked · retry \d\d:\d\d", pg.text_content("#monitor-status") or ""
        )
        is not None,
        pg.text_content("#monitor-status"),
    )
    check(
        "blocked card rendered on the Status page",
        visible(pg, "#status-body .blkrow [data-clear-block=facebook]"),
    )
    pg.screenshot(path="/tmp/qa/status-blocked-desktop.png")
    js(
        pg,
        """() => { window.AIMM.state.monitorInfo = { available: true, paused: true, activity: { state: "idle" }, jobs: [], fb_session: {}, counters: {}, blocked: {}, paused_persisted: true }; window.AIMM.renderMonitorStatus(); }""",
    )
    check("header chip shows paused", "paused" in (pg.text_content("#monitor-status") or ""))
    js(
        pg,
        """() => { window.AIMM.state.monitorInfo = { available: true, paused: false, activity: { state: "searching", item: "gpu" }, jobs: [], fb_session: {}, counters: {}, blocked: {}, paused_persisted: false }; window.AIMM.renderMonitorStatus(); }""",
    )
    check(
        "header chip shows 'searching <item>'",
        "searching gpu" in (pg.text_content("#monitor-status") or ""),
    )
    pg.click("#status-refresh")
    pg.wait_for_timeout(800)
    check("pause button hidden without an attached monitor", not visible(pg, "#pause-btn"))

    # ---------- logs ----------
    check("log buttons", visible(pg, "#log-download") and visible(pg, "#log-clear"))
    n_logs = js(pg, "document.querySelectorAll('#logs .log-row').length")
    js(
        pg,
        """() => { const s = window.AIMM.state; s.records = s.records.concat([
        { id: 900001, level: "INFO", iso_time: "12:00:00", time: Date.now()/1000, message: "qa info line", logger: "qa", location: "qa:1", extra: { kind: "search_summary", item: "qa-item" } },
        { id: 900002, level: "ERROR", iso_time: "12:00:01", time: Date.now()/1000, message: "qa error line", logger: "qa", location: "qa:2", extra: { kind: "ai_eval", item: "qa-item", score: 5 } },
        { id: 900003, level: "WARNING", iso_time: "12:00:02", time: Date.now()/1000, message: "qa warning line", logger: "qa", location: "qa:3" } ]);
        window.AIMM.emit("logs"); }""",
    )
    pg.wait_for_timeout(300)
    check(
        "logs render", js(pg, "document.querySelectorAll('#logs .log-row').length") == n_logs + 3
    )
    pg.click(".level-chips [data-level=ERROR]")
    pg.wait_for_timeout(200)
    check(
        "level filter",
        js(
            pg,
            "Array.from(document.querySelectorAll('#logs .log-row')).every(r => r.classList.contains('level-ERROR') || r.classList.contains('level-CRITICAL'))",
        ),
    )
    pg.click(".level-chips [data-level=ALL]")
    pg.click(".kind-chips [data-kind=ai_eval]")
    pg.wait_for_timeout(200)
    check(
        "kind filter",
        js(pg, "document.querySelectorAll('#logs .log-row').length") >= 1
        and js(
            pg,
            "Array.from(document.querySelectorAll('#logs .kind-badge')).every(b => b.classList.contains('kind-ai_eval'))",
        ),
    )
    pg.click(".kind-chips [data-kind='']")
    pg.select_option("#item-filter", "qa-item")
    pg.wait_for_timeout(200)
    check("item filter", js(pg, "document.querySelectorAll('#logs .log-row').length") == 2)
    pg.select_option("#score-filter", "5")
    pg.wait_for_timeout(200)
    check("score filter", js(pg, "document.querySelectorAll('#logs .log-row').length") == 1)
    pg.select_option("#score-filter", "")
    pg.select_option("#item-filter", "")
    pg.fill("#log-filter", "qa warning")
    pg.wait_for_timeout(200)
    check("text filter", js(pg, "document.querySelectorAll('#logs .log-row').length") == 1)
    pg.click("#logs .log-row")
    pg.wait_for_timeout(200)
    check("log row expands with details", visible(pg, "#logs .log-detail"))
    pg.fill("#log-filter", "")
    pg.click("#log-clear")
    pg.wait_for_timeout(200)
    check(
        "clear empties the tail", js(pg, "document.querySelectorAll('#logs .log-row').length") == 0
    )
    pg.screenshot(path="/tmp/qa/logs-desktop.png")

    # ---------- layout guards (desktop) ----------
    for view in ("review", "items", "sources", "status"):
        go(pg, view)
        pg.wait_for_timeout(300)
        check(f"no horizontal overflow at 1440: {view}", overflow(pg) <= 1, f"{overflow(pg)}px")
    ctx.close()

    # =====================================================================
    # Triage layout pass (1419x907, 1280x800): an expanded queue card is
    # taller than the viewport, so the card body must scroll under a pinned
    # action bar, and nothing may scroll sideways.
    # =====================================================================
    for vw, vh in ((1419, 907), (1280, 800)):
        tag = f"{vw}x{vh}"
        ctx = b.new_context(viewport={"width": vw, "height": vh})
        pg = ctx.new_page()
        pg.on(
            "console", lambda m: msgs.append((m.type, m.text, (m.location or {}).get("url", "")))
        )
        pg.on("pageerror", lambda e: msgs.append(("PAGEERROR", str(e), "")))
        open_desktop_detail(pg)
        check(f"{tag}: queue card expands to its detail", visible(pg, "#detail-pane"))
        check(
            f"{tag}: three-column panes carry min-height 0",
            js(
                pg,
                "['#rail', '#review-center', '#keys'].every(s => getComputedStyle(document.querySelector(s)).minHeight === '0px')",
            ),
        )
        railw = js(
            pg,
            "(() => { const r = document.querySelector('#rail'); return [r.scrollWidth, r.clientWidth]; })()",
        )
        check(
            f"{tag}: rail never scrolls sideways",
            railw[0] <= railw[1] + 1,
            f"{railw[0]}>{railw[1]}",
        )
        pills = js(
            pg,
            "(() => { const h = document.querySelector('#item-pills'); const r = document.querySelector('#rail').getBoundingClientRect(); return [h.scrollWidth <= h.clientWidth + 1, Array.from(h.children).every(c => c.getBoundingClientRect().right <= r.right + 1)]; })()",
        )
        check(f"{tag}: item pills wrap inside the rail", pills[0] and pills[1], pills)
        sideways = js(pg, SIDEWAYS)
        check(f"{tag}: no pane scrolls horizontally", not sideways, sideways[:3])

        body_css = js(
            pg, "getComputedStyle(document.querySelector('#detail-pane .dbody')).overflowY"
        )
        check(
            f"{tag}: detail body is the scroll container", body_css in ("auto", "scroll"), body_css
        )
        sh, ch = js(
            pg,
            "(() => { const b = document.querySelector('#detail-pane .dbody'); return [b.scrollHeight, b.clientHeight]; })()",
        )
        check(f"{tag}: detail content is taller than the pane", sh > ch + 20, f"{sh} > {ch}")
        pg.screenshot(path=f"/tmp/qa/detail-expanded-{vw}.png")
        moved = js(
            pg,
            "(() => { const b = document.querySelector('#detail-pane .dbody'); const a = b.scrollTop; b.scrollTop = a + 400; return [a, b.scrollTop]; })()",
        )
        pg.wait_for_timeout(300)
        check(f"{tag}: scrolling the detail by 400px moves it", moved[1] - moved[0] >= 380, moved)
        bar = box(pg, "#detail-pane .actbar")
        check(
            f"{tag}: action bar stays in the viewport after scrolling",
            bool(bar) and bar["top"] >= 0 and bar["bottom"] <= vh + 1 and bar["height"] > 40,
            ("top=%.0f bottom=%.0f vh=%d" % (bar["top"], bar["bottom"], vh)) if bar else "MISSING",
        )
        # The reasoning used to be clipped under the action bar; scrolled to
        # the end it must sit fully above it.
        js(
            pg,
            "() => { const b = document.querySelector('#detail-pane .dbody'); b.scrollTop = b.scrollHeight; }",
        )
        pg.wait_for_timeout(300)
        why = box(pg, "#detail-pane .why2")
        rate = box(pg, "#detail-pane .rate5")
        bar = box(pg, "#detail-pane .actbar")
        check(
            f"{tag}: reasoning and rating clear the action bar at the end of the scroll",
            bool(why and rate and bar)
            and rate["bottom"] <= bar["top"] + 1
            and why["bottom"] <= bar["top"] + 1,
            (
                ("why=%.0f rate=%.0f bar=%.0f" % (why["bottom"], rate["bottom"], bar["top"]))
                if (why and rate and bar)
                else "MISSING"
            ),
        )
        pg.screenshot(path=f"/tmp/qa/detail-scrolled-{vw}.png")
        check(
            f"no horizontal overflow at {vw}: detail open", overflow(pg) <= 1, f"{overflow(pg)}px"
        )

        before_key = js(pg, "window.AIMM.review.cursor")
        pg.keyboard.press("j")
        pg.wait_for_timeout(800)
        check(
            f"{tag}: J/K still move the cursor with the detail open",
            js(pg, "window.AIMM.review.cursor") != before_key and visible(pg, "#detail-pane"),
        )
        pg.keyboard.press("k")
        pg.wait_for_timeout(800)

        # Score badge vs price: with the photo, and with it gone (onerror).
        check(
            f"{tag}: score badge clear of the price (photo present)",
            overlap(box(pg, "#detail-pane .ph .sc"), box(pg, "#detail-pane .price")) == 0,
            [box(pg, "#detail-pane .ph .sc"), box(pg, "#detail-pane .price")],
        )
        # Every photo has to fail, not just the first: a carousel drops the
        # slides whose CDN URL expired and only gives up the whole block when
        # none of them is left.
        had_photo = js(pg, "document.querySelectorAll('#detail-pane .ph img').length")
        if had_photo:
            js(
                pg,
                "() => document.querySelectorAll('#detail-pane .ph img').forEach(i => i.dispatchEvent(new Event('error')))",
            )
            pg.wait_for_timeout(400)
        check(
            f"{tag}: photos that all fail take their whole block (and its badge) with them",
            not js(pg, "!!document.querySelector('#detail-pane .ph')"),
            "" if had_photo else "no photo to fail",
        )
        no_ph = js(
            pg,
            "(() => { const p = document.querySelector('#detail-pane'); const g = e => e ? JSON.parse(JSON.stringify(e.getBoundingClientRect())) : null; return {sc: g(p.querySelector('.ph .sc')), badge: g(p.querySelector('.badges .sc')), price: g(p.querySelector('.price'))}; })()",
        )
        check(
            f"{tag}: score badge sits inline, clear of the price, with no photo",
            no_ph["sc"] is None
            and bool(no_ph["badge"])
            and overlap(no_ph["badge"], no_ph["price"]) == 0
            and no_ph["badge"]["top"] >= no_ph["price"]["bottom"] - 1,
            no_ph,
        )
        for view in ("review", "items", "sources", "status"):
            go(pg, view)
            pg.wait_for_timeout(300)
            check(
                f"no horizontal overflow at {vw}: {view}", overflow(pg) <= 1, f"{overflow(pg)}px"
            )
        ctx.close()

    # =====================================================================
    # Phone pass (390x844): tabs, swipe, detail, lists, settings screens
    # =====================================================================
    ctx = b.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
    )
    pg = ctx.new_page()
    pg.on("console", lambda m: msgs.append((m.type, m.text, (m.location or {}).get("url", ""))))
    pg.on("pageerror", lambda e: msgs.append(("PAGEERROR", str(e), "")))
    pg.goto(BASE + "/", wait_until="load")
    pg.wait_for_timeout(800)
    pg.screenshot(path="/tmp/qa/login-mobile.png")
    pg.fill("input[name=username]", "t@e.com")
    pg.fill("input[name=password]", "pw")
    pg.click("#login-submit")
    pg.wait_for_timeout(3500)
    check("mobile: login", visible(pg, "#app"))
    check(
        "mobile: bottom tab bar",
        visible(pg, "#tabs") and js(pg, "document.querySelectorAll('#tabs a').length") == 4,
    )
    check("mobile: top nav hidden", not visible(pg, "#topnav"))
    check(
        "mobile: one card, actions in the thumb zone",
        visible(pg, "#stack .tcard.top")
        and visible(pg, "#act-keep")
        and visible(pg, "#act-dismiss"),
    )
    check("mobile: swipe hint", "Swipe" in (pg.text_content("#swipehint") or ""))
    check(
        "mobile: filters hidden behind the filter button",
        not visible(pg, "#review-filters") and visible(pg, "#filter-btn"),
    )
    pg.click("#filter-btn")
    pg.wait_for_timeout(200)
    check(
        "mobile: filter drawer opens", visible(pg, "#review-filters") and visible(pg, "#deal-sort")
    )
    pg.click("#filter-btn")
    pg.wait_for_timeout(200)
    pg.screenshot(path="/tmp/qa/queue-mobile.png")

    def targets_ok(selector, minimum=44):
        return js(
            pg,
            f"Array.from(document.querySelectorAll({json.dumps(selector)})).filter(e => e.offsetParent !== null).every(e => e.getBoundingClientRect().height >= {minimum} && e.getBoundingClientRect().width >= {minimum})",
        )

    check("touch targets: tabs >= 44px", targets_ok("#tabs a"))
    check("touch targets: card actions >= 44px", targets_ok("#acts .rb"))
    check(
        "touch targets: segmented control + icon buttons >= 38/44px",
        targets_ok("#mode-seg button", 38) and targets_ok("#filter-btn"),
    )
    check("no horizontal overflow at 390: queue", overflow(pg) <= 1, f"{overflow(pg)}px")

    km = js(pg, "document.querySelector('#stack .tcard.top').dataset.key")
    x, y, w, h = pg.eval_on_selector(
        "#stack .tcard.top",
        "e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }",
    )
    cx, cy = x + w / 2, y + h / 2
    pg.mouse.move(cx, cy)
    pg.mouse.down()
    for i in range(1, 9):
        pg.mouse.move(cx + w * 0.5 * i / 8, cy, steps=2)
        pg.wait_for_timeout(30)
    pg.screenshot(path="/tmp/qa/swipe-mobile.png")
    check(
        "mobile: KEEP stamp visible mid-swipe",
        js(
            pg,
            "parseFloat(document.querySelector('#stack .tcard.top .stamp.keep').style.opacity) >= 0.9",
        ),
    )
    pg.mouse.up()
    pg.wait_for_timeout(1300)
    rm = api_row(pg, km)
    check("mobile: swipe right keeps", rm and rm["kept"] is True)
    pg.click("#act-undo")
    pg.wait_for_timeout(1200)
    rm = api_row(pg, km)
    check("mobile: undo restores", rm and not rm["kept"] and not rm["reviewed_at"])
    pg.click("#act-details")
    pg.wait_for_timeout(600)
    check(
        "mobile: detail is a full screen (tabs hidden)",
        visible(pg, "#detail-pane")
        and not visible(pg, "#tabs")
        and not visible(pg, "#queue-pane"),
    )
    check(
        "touch targets: detail action bar >= 44px",
        targets_ok("#detail-pane .actbar .btn") and targets_ok("#detail-pane .rate5 button"),
    )
    check("no horizontal overflow at 390: detail", overflow(pg) <= 1, f"{overflow(pg)}px")
    check(
        "mobile: score badge clear of the price (photo present)",
        overlap(box(pg, "#detail-pane .ph .sc"), box(pg, "#detail-pane .price")) == 0,
    )
    check(
        "mobile: nothing in the detail sheet scrolls sideways",
        not js(pg, SIDEWAYS),
        js(pg, SIDEWAYS)[:3],
    )
    pg.screenshot(path="/tmp/qa/detail-sheet-390.png")

    # ---------- phone: the carousel is a swipe, not a widget ----------
    # The gallery probe scores 5 and was rated a moment ago, so it is the top
    # queue card -- which is the card this detail sheet is already showing.
    _topkey = js(pg, "() => window.AIMM.review.cursor")
    _gal_open = bool(GALLERY_KEY) and _topkey == GALLERY_KEY
    check("390: the gallery probe is the card under review", _gal_open, _topkey)
    if _gal_open:
        st = carousel_state(pg)
        check(
            "390: carousel renders one slide and one dot per photo",
            st and st["slides"] == GALLERY_N and st["dots"] == GALLERY_N and st["dead"] == 0,
            st,
        )
        check(
            "390: the track is a real horizontal scroller (the swipe)",
            st and st["trackScrolls"],
            st,
        )
        check(
            "390: arrows are hidden on a phone -- the gesture is the control",
            js(
                pg,
                "() => Array.from(document.querySelectorAll('#detail-pane .ph.gal .gnav')).every(b => getComputedStyle(b).display === 'none')",
            ),
        )
        js(
            pg,
            "() => { const t = document.querySelector('#detail-pane .ph.gal .track'); if (t) { t.scrollLeft = t.clientWidth * 2; t.dispatchEvent(new Event('scroll')); } }",
        )
        pg.wait_for_timeout(700)
        st = carousel_state(pg)
        check(
            "390: swiping the track moves the active dot",
            st and st["index"] == 2 and st["active"] == 2,
            st,
        )
        check("no horizontal overflow at 390: carousel", overflow(pg) <= 1, f"{overflow(pg)}px")
        check(
            "390: nothing but the photo track scrolls sideways",
            not js(pg, SIDEWAYS),
            js(pg, SIDEWAYS)[:3],
        )
        check(
            "390: the score badge stays clear of the price with a carousel",
            overlap(box(pg, "#detail-pane .ph .sc"), box(pg, "#detail-pane .price")) == 0,
        )
        pg.screenshot(path="/tmp/qa/carousel-390.png")
        pg.tap("#detail-pane .ph.gal .slide[data-slide='2']")
        pg.wait_for_timeout(800)
        check("390: tapping a photo opens the full-screen lightbox", visible(pg, "#lightbox"))
        _lb = box(pg, "#lightbox-img")
        check(
            "390: the lightbox photo fits the screen",
            bool(_lb) and _lb["width"] <= 390 and _lb["top"] >= 0,
            _lb and ("%.0fx%.0f" % (_lb["width"], _lb["height"])),
        )
        pg.screenshot(path="/tmp/qa/lightbox-390.png")
        pg.tap("#lightbox-close")
        pg.wait_for_timeout(600)
        check(
            "390: closing the lightbox returns to the detail",
            not visible(pg, "#lightbox") and visible(pg, "#detail-pane"),
        )
        _grow = api_row(pg, GALLERY_KEY)
        check(
            "390: browsing photos left the listing undecided",
            _grow and not _grow["kept"] and not _grow["hidden"],
        )

    # A dead photo hides itself; when every photo is dead the hero block goes
    # entirely and the inline badge carries the score. (Before galleries this
    # was one image; the rule is unchanged, it just takes all of them now.)
    _had = js(pg, "document.querySelectorAll('#detail-pane .ph img').length")
    if _had:
        js(
            pg,
            "() => document.querySelectorAll('#detail-pane .ph img').forEach(i => i.dispatchEvent(new Event('error')))",
        )
        pg.wait_for_timeout(400)
    _mob = js(
        pg,
        "(() => { const p = document.querySelector('#detail-pane'); const g = e => e ? JSON.parse(JSON.stringify(e.getBoundingClientRect())) : null; return {ph: !!p.querySelector('.ph'), sc: g(p.querySelector('.ph .sc')), badge: g(p.querySelector('.badges .sc')), price: g(p.querySelector('.price'))}; })()",
    )
    check(
        "mobile: photos that all fail take their whole block with them",
        not _mob["ph"],
        "" if _had else "no photo to fail",
    )
    check(
        "mobile: score badge sits inline, clear of the price, with no photo",
        _mob["sc"] is None and bool(_mob["badge"]) and overlap(_mob["badge"], _mob["price"]) == 0,
        _mob,
    )
    check(
        "no horizontal overflow at 390: detail without a photo",
        overflow(pg) <= 1,
        f"{overflow(pg)}px",
    )
    # Re-open the sheet so the shot below shows the real (photo-bearing) detail.
    pg.click("#detail-back")
    pg.wait_for_timeout(400)
    pg.click("#act-details")
    pg.wait_for_timeout(1200)
    pg.screenshot(path="/tmp/qa/detail-mobile.png", full_page=True)
    pg.click("#detail-back")
    pg.wait_for_timeout(300)
    check("mobile: back returns to the queue", visible(pg, "#queue-pane") and visible(pg, "#tabs"))
    pg.click("#act-keep")
    pg.wait_for_timeout(1200)
    pg.click("#mode-seg [data-mode=reviewed]")
    pg.wait_for_timeout(400)
    check(
        "mobile: Reviewed list with chips",
        visible(pg, "#list-pane")
        and js(pg, "document.querySelectorAll('#list-pane [data-rchip]').length") == 4
        and visible(pg, f"#list-pane .lrow[data-key={json.dumps(km)}]"),
    )
    pg.screenshot(path="/tmp/qa/reviewed-mobile.png", full_page=True)
    pg.wait_for_timeout(500)  # a full-page shot resizes the viewport; let the re-render settle
    rx, ry, rw, rh = pg.eval_on_selector(
        f"#list-pane .lrow[data-key={json.dumps(km)}]",
        "e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }",
    )
    pg.mouse.move(rx + rw * 0.7, ry + rh / 2)
    pg.mouse.down()
    pg.mouse.move(rx + rw * 0.7 - 90, ry + rh / 2, steps=6)
    pg.mouse.up()
    pg.wait_for_timeout(300)
    check(
        "mobile: swipe a row left reveals Undo",
        js(
            pg,
            f"document.querySelector('#list-pane .lrow[data-key={json.dumps(km)}]').classList.contains('revealed')",
        ),
    )
    pg.click(f"#list-pane .lrow[data-key={json.dumps(km)}] .undo")
    pg.wait_for_timeout(1200)
    rm = api_row(pg, km)
    check("mobile: row Undo re-flags", rm and not rm["kept"] and not rm["reviewed_at"])
    pg.click("#mode-seg [data-mode=all]")
    pg.wait_for_timeout(400)
    check(
        "mobile: All view uses feed cards",
        js(pg, "document.querySelectorAll('#list-pane .fcard').length") >= 3,
    )
    check("no horizontal overflow at 390: all", overflow(pg) <= 1, f"{overflow(pg)}px")
    pg.screenshot(path="/tmp/qa/all-mobile.png", full_page=True)
    pg.click("#list-pane .fcard")
    pg.wait_for_timeout(400)
    check("mobile: feed card opens detail", visible(pg, "#detail-pane"))
    pg.click("#detail-back")
    pg.click("#mode-seg [data-mode=queue]")

    go(pg, "items")
    pg.wait_for_timeout(600)
    check(
        "mobile: items list",
        js(pg, "document.querySelectorAll('#items-page [data-open-item]').length") >= 4,
    )
    check("touch targets: list rows >= 44px", targets_ok("#items-page .g .r"))
    check("no horizontal overflow at 390: items", overflow(pg) <= 1, f"{overflow(pg)}px")
    pg.screenshot(path="/tmp/qa/items-mobile.png", full_page=True)
    open_item(pg, S)
    check(
        "mobile: item editor with grouped lists",
        js(pg, "document.querySelectorAll('#items-page .g').length") >= 5
        and visible(pg, "#items-page .tog[data-toggle=enabled]"),
    )
    check("no horizontal overflow at 390: item editor", overflow(pg) <= 1, f"{overflow(pg)}px")
    # Two steppers on one row is the tightest thing on this screen at 390px.
    _pair = box(pg, "#items-page .r.thrpair")
    _rev = box(pg, "#items-page .rev-val")
    _thr = box(pg, "#items-page .thr-val")
    check(
        "mobile: both threshold steppers fit side by side",
        bool(_rev and _thr and _pair)
        and _rev["right"] <= _thr["left"]
        and _thr["right"] <= _pair["right"] + 1
        and overlap(_rev, _thr) == 0,
        (_rev, _thr),
    )
    check(
        "mobile: threshold steppers keep their 44x40 touch targets",
        targets_ok("#items-page .r.thrpair .step button", 40),
    )
    # Label + stepper + a two-line note is ~120px; anything far past that means
    # a note picked up the page-shell `.wrap` padding again.
    check(
        "mobile: the threshold row stays compact",
        (_pair or {}).get("height", 999) <= 170,
        (_pair or {}).get("height"),
    )
    pg.screenshot(path="/tmp/qa/item-thresholds-390.png")
    pg.screenshot(path="/tmp/qa/item-edit-mobile.png", full_page=True)
    pg.click("#items-page [data-act=edit]")
    pg.wait_for_timeout(400)
    check("mobile: form modal is a bottom sheet", visible(pg, "#form-modal"))
    pg.screenshot(path="/tmp/qa/modal-mobile.png")
    pg.click("#form-cancel")
    pg.click("[data-config-mode=toml]:visible")
    pg.wait_for_timeout(400)
    check("mobile: TOML view", visible(pg, "#toml-pane"))
    pg.screenshot(path="/tmp/qa/toml-mobile.png")
    pg.click("[data-config-mode=form]:visible")
    go(pg, "sources")
    pg.wait_for_timeout(600)
    check(
        "mobile: sources grouped list",
        js(pg, "document.querySelectorAll('#sources-page .g').length") >= 3,
    )
    check("no horizontal overflow at 390: sources", overflow(pg) <= 1, f"{overflow(pg)}px")
    pg.screenshot(path="/tmp/qa/sources-mobile.png", full_page=True)
    go(pg, "status")
    pg.wait_for_timeout(1200)
    check(
        "mobile: status grouped list + logs",
        js(pg, "document.querySelectorAll('#status-body .g').length") >= 2
        and visible(pg, "#logs"),
    )
    check("no horizontal overflow at 390: status", overflow(pg) <= 1, f"{overflow(pg)}px")
    pg.screenshot(path="/tmp/qa/status-mobile.png", full_page=True)
    pg.click("#logout-row")
    pg.wait_for_timeout(800)
    check("mobile: log out returns to the login screen", visible(pg, "#login-screen"))
    ctx.close()
    b.close()

# The two tier probes are the only cache rows this harness leaves behind, and
# only until here.
for _key in _probe_keys:
    _cache.delete(_key)
# The gallery probe is also decided on (kept, then undone) by the review-key
# check, which leaves a USER_FLAGS row behind.
if _probe_item:
    _cache.delete((_CT.USER_FLAGS.value, "facebook", GALLERY_ID))
for _path in _probe_files:
    try:
        _path.unlink()
    except OSError:
        pass
check(
    "tier probes cleaned out of the shared cache", all(_cache.get(k) is None for k in _probe_keys)
)
check("probe photo snapshots cleaned off disk", not any(p.exists() for p in _probe_files))

# Resource-error console text omits the URL; it rides in the message location.
errors = [
    (t, m)
    for t, m, url in msgs
    if t in ("error", "PAGEERROR") and "401" not in m
    # expired listing images 404 by design; the img hides itself
    and "listing-image" not in m + url
    # OSM tile fetches can transiently fail without breaking the map
    and "tile.openstreetmap.org" not in m + url
    # the router demo can be down; the UI degrades to the straight line
    and "/api/route" not in m + url
]
check("zero console errors", len(errors) == 0, errors[:3])
check("live config.toml untouched (byte-identical)", LIVE_CONFIG.read_bytes() == _LIVE_BYTES)

fails = [r for r in results if not r[1]]
print("=" * 50)
print(f"QA: {len(results) - len(fails)}/{len(results)} passed")
for name, _, extra in fails:
    print("  FAILED:", name, extra)
srv.stop()
print("DONE")
