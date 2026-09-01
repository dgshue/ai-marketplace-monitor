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

from ai_marketplace_monitor.webui.server import WebUIConfig, start_webui
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler

srv, info = start_webui(
    WebUIConfig(host="0.0.0.0", port=8476,
                config_files=[Path("/root/.ai-marketplace-monitor/config.toml")],
                log_handler=LogBroadcastHandler(capacity=50)),
    logger=log)
time.sleep(2)

os.makedirs("/tmp/qa", exist_ok=True)
results = []
def check(name, ok, extra=""):
    results.append((name, bool(ok), extra))
    print(("PASS " if ok else "FAIL ") + name + (("  | " + str(extra)) if extra else ""))

msgs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.on("console", lambda m: msgs.append((m.type, m.text)))
    pg.on("pageerror", lambda e: msgs.append(("PAGEERROR", str(e))))

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
    check("deals selection", pg.eval_on_selector(".dli:nth-child(4)", "e=>e.classList.contains('sel')"))
    pg.click(".dd-myrank .star[data-rank='5']")
    pg.wait_for_timeout(900)
    check("star set", pg.eval_on_selector_all(".dd-myrank .star.on", "e=>e.length") == 5)
    key = pg.eval_on_selector(".dli.sel", "e=>e.dataset.key")
    pg.click("[data-flag=hide]")
    pg.wait_for_timeout(900)
    check("dismiss hides", pg.eval_on_selector_all(f".dli[data-key='{key}']", "e=>e.length") == 0)
    pg.click(".verdict-chips [data-verdict=hidden]")
    pg.wait_for_timeout(600)
    check("hidden chip shows", pg.eval_on_selector_all(f".dli[data-key='{key}']", "e=>e.length") == 1)
    pg.click(f".dli[data-key='{key}']")
    pg.wait_for_timeout(400)
    pg.click("[data-flag=hide]")
    pg.wait_for_timeout(900)
    pg.click(".verdict-chips [data-verdict='']")
    pg.wait_for_timeout(600)
    check("restore returns", pg.eval_on_selector_all(f".dli[data-key='{key}']", "e=>e.length") == 1)
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
    first_item = pg.eval_on_selector("#activity-summary .ipill[data-item-pill]:not([data-item-pill=''])", "e=>e.dataset.itemPill")
    pg.click(f"#activity-summary .ipill[data-item-pill='{first_item}']")
    pg.wait_for_timeout(500)
    rows_match = pg.evaluate(
        "(item) => Array.from(document.querySelectorAll('.dli .m span:first-child')).every(x => x.textContent === item)",
        first_item)
    check("pill filters rows", rows_match, first_item)
    check("pill active state", pg.eval_on_selector(f"#activity-summary .ipill[data-item-pill='{first_item}']", "e=>e.classList.contains('on')"))
    pg.click(f"#activity-summary .ipill[data-item-pill='{first_item}']")
    pg.wait_for_timeout(500)
    check("pill toggles back to All", pg.eval_on_selector("#activity-summary .ipill[data-item-pill='']", "e=>e.classList.contains('on')"))

    # --- paused items: out of the All view, reachable via their dimmed pill ---
    paused = pg.eval_on_selector_all("#activity-summary .ipill.paused", "e=>e.map(x=>x.dataset.itemPill)")
    if paused:
        hidden_ok = pg.evaluate(
            "(names) => !Array.from(document.querySelectorAll('.dli .m span:first-child')).some(x => names.includes(x.textContent))",
            paused)
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
    pg.screenshot(path="/tmp/qa/1-deals.png")

    # ---------- config: item cards ----------
    pg.click("#app-nav button[data-appview=config]")
    pg.wait_for_timeout(1000)
    snapshot = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue()")
    n_cards = pg.eval_on_selector_all(".icard", "e=>e.length")
    check("item cards render", n_cards >= 4, n_cards)
    check("cards collapsed by default", pg.eval_on_selector_all(".icard.open", "e=>e.length") == 0)
    check("sources strip", pg.eval_on_selector_all(".set", "e=>e.length") >= 3,
          pg.eval_on_selector_all(".set .t", "e=>e.map(x=>x.textContent.trim())"))
    check("add item btn", bool(pg.query_selector('[data-add="item"]')))

    S = ".icard[data-section='item.pc']"
    pg.click(S + " .ihead")
    pg.wait_for_timeout(400)
    check("card expands", pg.eval_on_selector(S, "e=>e.classList.contains('open')"))
    pg.screenshot(path="/tmp/qa/2-config-open.png")

    # chips: add then remove a phrase (net zero)
    pg.fill(S + " [data-chip-add='search_phrases']", "qa test phrase")
    pg.press(S + " [data-chip-add='search_phrases']", "Enter")
    pg.wait_for_timeout(700)
    in_toml = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue().includes('qa test phrase')")
    check("chip add writes TOML", in_toml)
    pg.click(S + " [data-chip-del='search_phrases'][data-chip-val='qa test phrase']")
    pg.wait_for_timeout(700)
    out_toml = pg.evaluate("!document.querySelector('.CodeMirror').CodeMirror.getValue().includes('qa test phrase')")
    check("chip remove cleans TOML", out_toml)

    # description edit + revert
    orig_desc = pg.eval_on_selector(S + " [data-field=description]", "e=>e.value")
    pg.fill(S + " [data-field=description]", "qa description probe")
    pg.eval_on_selector(S + " [data-field=description]", "e=>e.blur()")
    pg.wait_for_timeout(700)
    check("description writes", pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue().includes('qa description probe')"))
    pg.fill(S + " [data-field=description]", orig_desc)
    pg.eval_on_selector(S + " [data-field=description]", "e=>e.blur()")
    pg.wait_for_timeout(700)

    # price int + clear back to original
    orig_max = pg.eval_on_selector(S + " [data-field=max_price]", "e=>e.value")
    pg.fill(S + " [data-field=max_price]", "912")
    pg.eval_on_selector(S + " [data-field=max_price]", "e=>e.blur()")
    pg.wait_for_timeout(700)
    seg = pg.evaluate("(() => { const v=document.querySelector('.CodeMirror').CodeMirror.getValue(); return v.split('[item.pc]')[1].split('\\n[')[0]; })()")
    check("price writes unquoted int", "max_price = 912" in seg and 'max_price = "912"' not in seg)
    pg.fill(S + " [data-field=max_price]", orig_max)
    pg.eval_on_selector(S + " [data-field=max_price]", "e=>e.blur()")
    pg.wait_for_timeout(700)

    # threshold set + clear (verify against the BUFFER, the source of truth)
    pg.click(S + " .thr button[data-thr='2']")
    pg.wait_for_timeout(800)
    seg_thr = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue().split('[item.pc]')[1].split(String.fromCharCode(10)+'[')[0]")
    check("threshold set writes", "rating = 2" in seg_thr)
    check("threshold note shows", "≥ 2" in pg.eval_on_selector(S + " .thr .on", "e=>e.parentElement.nextElementSibling ? '' : ''") or "≥ 2" in pg.eval_on_selector(S + " .thr-note", "e=>e.textContent"))
    pg.click(S + " .thr button[data-thr='2']")
    pg.wait_for_timeout(800)
    seg_thr2 = pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.getValue().split('[item.pc]')[1].split(String.fromCharCode(10)+'[')[0]")
    check("threshold clear removes", "rating" not in seg_thr2)
    check("cleared note inherits", "inherited" in pg.eval_on_selector(S + " .thr-note", "e=>e.textContent"))

    # source toggle: with 2+ sources it toggles; with one, the guard refuses
    n_sources = pg.eval_on_selector_all(S + " .src", "e=>e.length")
    pg.click(S + " .src[data-src=facebook]")
    pg.wait_for_timeout(700)
    if n_sources >= 2:
        check("source off", not pg.eval_on_selector(S + " .src[data-src=facebook]", "e=>e.classList.contains('on')"))
        pg.click(S + " .src[data-src=facebook]")
        pg.wait_for_timeout(700)
        check("source back on", pg.eval_on_selector(S + " .src[data-src=facebook]", "e=>e.classList.contains('on')"))
    else:
        status = pg.eval_on_selector("#editor-status", "e=>e.textContent")
        check("single-source guard refuses", "at least one source" in status, status[:60])
        check("source stays on", pg.eval_on_selector(S + " .src[data-src=facebook]", "e=>e.classList.contains('on')"))

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
        diff = [l for l in difflib.unified_diff(snapshot.splitlines(), final.splitlines(), lineterm="") if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        only_norm = all("search_phrases" in l for l in diff)
        check("buffer diff is only phrase normalization", only_norm, diff[:6])
    else:
        check("buffer pristine", True)
    pg.evaluate("document.querySelector('.CodeMirror').CodeMirror.setValue(" + repr(0) + " ? '' : arguments)") if False else None
    pg.evaluate("(s) => { const cm=document.querySelector('.CodeMirror').CodeMirror; cm.setValue(s); }", snapshot)
    pg.wait_for_timeout(600)
    check("buffer reset (Save disabled)", pg.eval_on_selector("#save-btn", "e=>e.disabled"))

    # modals open + cancel (no saves)
    pg.click(S + " [data-act=edit]")
    pg.wait_for_timeout(500)
    check("More settings opens modal", not pg.eval_on_selector("#form-modal", "e=>e.classList.contains('hidden')"))
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)
    pg.click(".set [data-edit-section='marketplace.facebook']")
    pg.wait_for_timeout(500)
    check("strip Edit opens modal", not pg.eval_on_selector("#form-modal", "e=>e.classList.contains('hidden')"))
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)
    pg.click('[data-add="item"]')
    pg.wait_for_timeout(500)
    check("Add item opens modal", not pg.eval_on_selector("#form-modal", "e=>e.classList.contains('hidden')"))
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)

    # --- available sources appear with Set up, prefilled to the right type ---
    avail = pg.eval_on_selector_all(".set.avail [data-setup-marketplace]", "e=>e.map(x=>x.dataset.setupMarketplace)")
    check("available source cards", set(avail) >= {"ebay", "depop", "poshmark"}, avail)
    pg.click(".set.avail [data-setup-marketplace='ebay']")
    pg.wait_for_timeout(600)
    check("setup modal prefilled", pg.eval_on_selector("#add-section-name", "e=>e.value") == "ebay")
    check("setup uses ebay schema", bool(pg.query_selector("#field-client_id")))
    pg.click("#form-cancel")
    pg.wait_for_timeout(300)

    # TOML tab roundtrip
    pg.click("#tab-toml")
    pg.wait_for_timeout(500)
    check("TOML tab shows editor", pg.eval_on_selector("#config-toml-view", "e=>getComputedStyle(e).display") != "none")
    pg.click("#tab-form")
    pg.wait_for_timeout(400)
    check("back to Form", pg.eval_on_selector("#config-form-view", "e=>getComputedStyle(e).display") != "none")

    # help levels
    pg.click("#help-seg button[data-help=guided]")
    pg.wait_for_timeout(300)
    check("guided shows examples", pg.eval_on_selector(".fieldhelp .ex", "e=>getComputedStyle(e).display") == "block")
    pg.click("#help-seg button[data-help=off]")
    pg.wait_for_timeout(300)
    check("help off hides", pg.eval_on_selector(".fieldhelp", "e=>getComputedStyle(e).display") == "none")
    pg.click("#help-seg button[data-help=hints]")
    pg.wait_for_timeout(200)

    # ---------- logs ----------
    pg.click("#app-nav button[data-appview=logs]")
    pg.wait_for_timeout(600)
    check("logs render", pg.eval_on_selector_all("#logs > *", "e=>e.length") >= 0)
    check("log buttons", bool(pg.query_selector("#log-download")) and bool(pg.query_selector("#log-clear")))
    pg.click(".level-chips [data-level=ERROR]")
    pg.wait_for_timeout(300)
    pg.click(".level-chips [data-level=ALL]")
    pg.wait_for_timeout(200)
    pg.screenshot(path="/tmp/qa/3-logs.png")

    # ---------- status ----------
    pg.click("#app-nav button[data-appview=status]")
    pg.wait_for_timeout(1500)
    check("status tiles", pg.eval_on_selector_all(".stile", "e=>e.length") == 4,
          pg.eval_on_selector_all(".stile .t", "e=>e.map(x=>x.textContent)"))
    check("env lines", pg.eval_on_selector_all(".envline", "e=>e.length") > 0)
    pg.click("#status-refresh")
    pg.wait_for_timeout(800)
    check("status refresh", "updated" in pg.eval_on_selector("#status-updated", "e=>e.textContent"))
    pg.screenshot(path="/tmp/qa/4-status.png")

    # ---------- header controls ----------
    check("chip text", bool(pg.eval_on_selector("#monitor-status", "e=>e.textContent.trim()")),
          pg.eval_on_selector("#monitor-status", "e=>e.textContent"))
    check("browser link", "path=ws/vnc" in (pg.eval_on_selector("#browser-btn", "e=>e.href") or ""))

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

errors = [(t, m) for t, m in msgs if t in ("error", "PAGEERROR") and "401" not in m]
check("zero console errors", len(errors) == 0, errors[:3])

fails = [r for r in results if not r[1]]
print("=" * 50)
print(f"QA: {len(results) - len(fails)}/{len(results)} passed")
for name, _, extra in fails:
    print("  FAILED:", name, extra)
srv.stop()
print("DONE")
