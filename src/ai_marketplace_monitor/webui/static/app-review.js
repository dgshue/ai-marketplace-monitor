// AI Marketplace Monitor — Triage UI, review module.
// One listing at a time: swipe right = keep, swipe left = dismiss, tap = details.
// Queue / Reviewed / All are three views of the same /api/activity rows:
//   reviewed  = the user decided (kept, hidden, or rated) -> reviewed_at is set
//   queue     = not reviewed, on an active item
//   all       = everything, with the verdict / item / text filters and sorts
// Three score tiers, set by review_rating / rating in the config:
//   verdict "low"       -- under the review threshold: rated once, cached, and
//                          kept out of Queue, Reviewed and every day count.
//                          Reachable only through the All view's Low chip.
//   verdict "promising" -- at or above the review threshold, not notified.
//   verdict "notified"  -- a notification went out.
(() => {
  const { $, $$, esc, api, state, on, emit, toast, fmtDur, isDesktop, modalOpen, typing, activeBlocks } = window.AIMM;

  const R = {
    listings: [],
    summary: [],
    home: null, // [lat, lon] from home_location
    total: 0,
    truncated: false,
    mode: "queue", // queue | reviewed | all
    item: "", // item pill filter ('' = all active items)
    verdict: "", // All view verdict chip
    rchip: "", // Reviewed view chip: '' | kept | dismissed | notified
    text: "",
    sort: "score", // score | myrank | distance | newest (persisted)
    cursor: null, // rowKey of the current listing
    detail: false, // detail expanded
    undo: [], // [{key, prev}]
    showAll: false, // "Show N more" in the lists
    loading: false,
    reloadTimer: null,
    routes: {}, // rowKey -> /api/route result
  };
  window.AIMM.review = R;

  const rowKey = (row) => `${row.marketplace}:${row.id}`;
  const byKey = (key) => R.listings.find((r) => rowKey(r) === key) || null;
  const isReviewed = (row) => !!(row.kept || row.hidden || row.my_rank != null);
  // "I really don't want to see 1s and 2s": below the review threshold a row
  // is tracked, never queued. Older activity payloads carry no verdict "low",
  // so nothing changes for them.
  const isLow = (row) => row.verdict === "low";
  const notLow = (row) => !isLow(row);
  const scoreClass = (s) => "s" + Math.max(0, Math.min(5, Number(s) || 0));
  const whyClass = (s) => (s >= 4 ? "" : s === 3 ? "mid" : "low");
  const verdictClass = (row) => (row.verdict === "notified" ? "noti" : row.verdict === "promising" ? "prom" : "dism");
  // "dismissed" is the pre-three-tier value; rows cached under it still render.
  const VERDICT_TEXT = { low: "Low", dismissed: "Below threshold" };
  const verdictText = (row) => VERDICT_TEXT[row.verdict] || row.verdict;
  const MARKET_LABEL = { facebook: "Facebook", ebay: "eBay", depop: "Depop", poshmark: "Poshmark" };
  const marketLabel = (m) => MARKET_LABEL[m] || m;
  const srcGlyph = (m) => {
    const cls = MARKET_LABEL[m] ? m : "other";
    return `<span class="src ${cls}" title="${esc(marketLabel(m))}">${esc(String(m || "?")[0])}</span>`;
  };
  // Photos are proxied, never hotlinked: Facebook's CDN URLs are signed and
  // expire, so /api/listing-image serves the server-side snapshot. `i` picks
  // the photo; omitting it means photo 0, which is what every caller that
  // predates galleries asks for.
  const photoUrl = (row, i) =>
    row.url && (row.image || photoCount(row)) ? `/api/listing-image?post=${encodeURIComponent(row.url)}${i ? `&i=${i}` : ""}` : "";
  // Facebook listing pages carry a gallery; every other source gives one tile
  // photo, and so do Facebook listings cached before galleries were read.
  const photoCount = (row) => Math.max(Number(row.image_count) || 0, row.image ? 1 : 0);
  const photoImg = (row, alt) =>
    photoUrl(row)
      ? `<img src="${esc(photoUrl(row))}" alt="${esc(alt || "listing photo")}" loading="lazy" onerror="this.remove()" />`
      : "";
  // The detail hero: a scroll-snap carousel of every photo. The score badge
  // is positioned over it, so when nothing loads — no photos recorded, or
  // every fetch 404s because the CDN URLs expired — the whole block is
  // removed and the inline badge in .badges carries the score.
  //
  // Only the slides near the start get a real `src` in the markup; the rest
  // are filled in by syncGal as they come within LAZY_AHEAD of the photo on
  // screen. Opening a detail costs three images, not twelve.
  const LAZY_AHEAD = 2;
  const detailPhoto = (row) => {
    const n = photoCount(row);
    if (!n || !row.url) return "";
    const slides = Array.from({ length: n }, (_, i) => {
      const url = esc(photoUrl(row, i));
      const src = i < LAZY_AHEAD ? `src="${url}"` : `data-src="${url}"`;
      const label = esc(row.title || "listing photo") + (n > 1 ? ` — photo ${i + 1} of ${n}` : "");
      return `<div class="slide" data-slide="${i}"><img ${src} alt="${label}" loading="lazy" /></div>`;
    }).join("");
    const multi = n > 1;
    const dots = multi
      ? `<div class="dots">${Array.from({ length: n }, (_, i) => `<i class="${i ? "" : "on"}" data-dot="${i}" role="button" tabindex="-1" aria-label="Photo ${i + 1}"></i>`).join("")}</div>`
      : "";
    const arrows = multi
      ? `<button class="gnav prev" data-photo="-1" aria-label="Previous photo" title="Previous photo (,)">‹</button><button class="gnav next" data-photo="1" aria-label="Next photo" title="Next photo (.)">›</button>`
      : "";
    const counter = multi ? `<span class="pcount">1 / ${n}</span>` : "";
    return `<div class="ph gal" data-count="${n}"><div class="track" role="group" aria-label="Listing photos">${slides}</div><span class="sc ${scoreClass(row.score)}">${row.score} / 5</span>${srcGlyph(row.marketplace)}${arrows}${dots}${counter}</div>`;
  };
  const priceText = (row) => (row.price && row.price !== "**unspecified**" ? row.price : "—");
  const startOfToday = () => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d.getTime() / 1000;
  };

  const SORTERS = {
    score: (a, b) => b.score - a.score || (b.rated_at || 0) - (a.rated_at || 0) || a.item.localeCompare(b.item),
    myrank: (a, b) => (b.my_rank || 0) - (a.my_rank || 0) || b.score - a.score,
    distance: (a, b) => {
      const av = a.distance_mi == null ? Infinity : a.distance_mi;
      const bv = b.distance_mi == null ? Infinity : b.distance_mi;
      return av - bv || b.score - a.score;
    },
    newest: (a, b) => (b.rated_at || 0) - (a.rated_at || 0) || b.score - a.score,
  };
  const SORT_LABEL = { score: "best rated first", myrank: "my rating first", distance: "nearest first", newest: "newest first" };

  const textMatch = (row) => {
    const needle = R.text.trim().toLowerCase();
    if (!needle) return true;
    return (row.title || "").toLowerCase().includes(needle) || (row.comment || "").toLowerCase().includes(needle);
  };
  const itemMatch = (row) => {
    if (R.item) return row.item === R.item;
    // Paused or since-removed items stay tracked but leave the default views.
    // Picking their pill is the explicit ask to see that history.
    return row.item_active !== false;
  };

  const queueRows = () =>
    R.listings.filter((r) => notLow(r) && !isReviewed(r) && itemMatch(r) && textMatch(r)).sort(SORTERS[R.sort] || SORTERS.score);

  const reviewedRows = () =>
    R.listings
      .filter((r) => {
        if (isLow(r) || !isReviewed(r) || !itemMatch(r) || !textMatch(r)) return false;
        if (R.rchip === "kept") return !!r.kept;
        if (R.rchip === "dismissed") return !!r.hidden;
        if (R.rchip === "notified") return r.verdict === "notified";
        return true;
      })
      .sort((a, b) => (b.reviewed_at || 0) - (a.reviewed_at || 0) || b.score - a.score);

  // The All view keeps the old Deals semantics: hidden rows only under their
  // own chip, every other chip filters on the AI verdict. Low rows are the
  // same deal — the "All verdicts" chip excludes them so the default stays
  // clean, and the Low chip is the one place they surface.
  const allRows = () =>
    R.listings
      .filter((r) => {
        if (R.verdict === "hidden") {
          if (!r.hidden) return false;
        } else {
          if (r.hidden) return false;
          if (R.verdict && r.verdict !== R.verdict) return false;
          if (!R.verdict && isLow(r)) return false;
        }
        return itemMatch(r) && textMatch(r);
      })
      .sort(SORTERS[R.sort] || SORTERS.score);

  const rowsForMode = (mode) => (mode === "queue" ? queueRows() : mode === "reviewed" ? reviewedRows() : allRows());

  // Keep the cursor while it remains visible; otherwise fall back to the top.
  const currentRows = () => rowsForMode(R.mode);
  const current = () => {
    const rows = currentRows();
    let row = rows.find((r) => rowKey(r) === R.cursor);
    // While the detail is expanded the listing stays put even after a
    // decision moves it out of this list (rating from the detail must not
    // silently jump to the next card); collapsing returns to the flow.
    if (!row && R.detail && R.cursor) row = byKey(R.cursor);
    if (!row) {
      row = rows[0] || null;
      R.cursor = row ? rowKey(row) : null;
    }
    return row;
  };

  // ---------------------------------------------------------------
  // Data
  // ---------------------------------------------------------------
  const load = async () => {
    if (R.loading) return;
    R.loading = true;
    try {
      const res = await api("/api/activity");
      if (!res.ok) return;
      const data = await res.json();
      R.summary = data.summary || [];
      R.home = data.home || null;
      R.listings = data.listings || [];
      R.total = data.total || 0;
      R.truncated = !!data.truncated;
      render();
      emit("activity", data);
    } catch (err) {
      console.error("activity load failed", err);
    } finally {
      R.loading = false;
    }
  };
  // A search burst emits one ai_eval per listing. Coalesce them so a run over
  // 40 listings triggers one reload instead of 40 cache scans.
  const scheduleReload = () => {
    if (R.reloadTimer) clearTimeout(R.reloadTimer);
    R.reloadTimer = setTimeout(() => {
      R.reloadTimer = null;
      load();
    }, 4000);
  };
  on("log", (record) => {
    const kind = record && record.extra && record.extra.kind;
    if (kind === "ai_eval" || kind === "search_summary") scheduleReload();
  });

  // ---------------------------------------------------------------
  // Flags: optimistic local update, then the API; revert on failure.
  // ---------------------------------------------------------------
  const snapshot = (row) => ({ kept: !!row.kept, hidden: !!row.hidden, my_rank: row.my_rank ?? null });
  const applyLocal = (row, flags) => {
    if ("kept" in flags) row.kept = !!flags.kept;
    if ("hidden" in flags) row.hidden = !!flags.hidden;
    if ("my_rank" in flags) row.my_rank = flags.my_rank ?? null;
    const decided = row.kept || row.hidden || row.my_rank != null;
    if (decided && !row.reviewed_at) row.reviewed_at = Date.now() / 1000;
    if (!decided) row.reviewed_at = null;
  };
  const sendFlag = async (row, flags) => {
    const before = snapshot(row);
    applyLocal(row, flags);
    try {
      const res = await api("/api/listing/flag", {
        method: "POST",
        body: JSON.stringify({ marketplace: row.marketplace, id: row.id, ...flags }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const got = (await res.json()).flags || {};
      row.my_rank = got.my_rank ?? null;
      row.hidden = !!got.hidden;
      row.kept = !!got.kept;
      row.reviewed_at = got.reviewed_at ?? null;
      return true;
    } catch (err) {
      console.error("flag update failed", err);
      applyLocal(row, before);
      toast("Could not save that decision", { error: true });
      render();
      return false;
    }
  };

  const pushUndo = (row) => {
    R.undo.push({ key: rowKey(row), prev: snapshot(row) });
    if (R.undo.length > 50) R.undo.shift();
  };
  const undo = async () => {
    const last = R.undo.pop();
    if (!last) return;
    const row = byKey(last.key);
    if (!row) return;
    await sendFlag(row, last.prev);
    R.cursor = last.key;
    R.detail = false;
    render();
    toast("Undone");
  };

  // After a queue decision the next card takes over; elsewhere the row just
  // re-renders in place.
  const advanceAfter = (row) => {
    if (R.mode !== "queue") return;
    const rows = queueRows();
    const idx = rows.findIndex((r) => rowKey(r) === rowKey(row));
    const next = rows[idx + 1] || rows[idx - 1] || null;
    R.cursor = next ? rowKey(next) : null;
  };

  const decide = async (row, kind) => {
    if (!row) return;
    pushUndo(row);
    const flags = kind === "keep" ? { kept: !row.kept || R.mode === "queue", hidden: false } : { hidden: true, kept: false };
    if (kind === "keep" && R.mode !== "queue" && row.kept) flags.kept = false; // toggle off outside the queue
    const wasQueue = R.mode === "queue";
    if (wasQueue) advanceAfter(row);
    const ok = await sendFlag(row, flags);
    if (wasQueue || !isDesktop()) R.detail = false;
    render();
    if (ok) {
      const label = kind === "keep" ? (flags.kept ? "Kept" : "Un-kept") : "Dismissed";
      toast(label + " · " + (row.title || "").slice(0, 32), { undo });
    }
  };
  const toggleHidden = async (row) => {
    if (!row) return;
    pushUndo(row);
    const wasQueue = R.mode === "queue";
    if (wasQueue && !row.hidden) advanceAfter(row);
    await sendFlag(row, { hidden: !row.hidden, kept: row.hidden ? row.kept : false });
    render();
    toast(row.hidden ? "Hidden" : "Restored", { undo });
  };
  const rate = async (row, n) => {
    if (!row) return;
    pushUndo(row);
    const next = row.my_rank === n ? null : n;
    await sendFlag(row, { my_rank: next });
    render();
    toast(next ? "Rated " + "★".repeat(next) : "Rating cleared", { undo });
  };
  const openListing = (row) => {
    if (row && row.url) window.open(row.url, "_blank", "noopener");
  };

  // ---------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------
  const setSeg = () => {
    $$("#mode-seg [data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === R.mode));
    $("#n-queue").textContent = queueRows().length;
    $("#n-reviewed").textContent = reviewedRows().length;
    $("#n-all").textContent = allRows().length;
  };

  const renderHead = () => {
    const q = queueRows();
    const reviewedActive = R.listings.filter((r) => notLow(r) && isReviewed(r) && itemMatch(r)).length;
    const denom = q.length + reviewedActive;
    const pct = denom ? Math.round((reviewedActive / denom) * 100) : 0;
    $("#q-count").textContent = q.length === 1 ? "1 to review" : `${q.length} to review`;
    $("#q-bar").style.width = pct + "%";
    const perItem = {};
    q.forEach((r) => {
      perItem[r.item] = (perItem[r.item] || 0) + 1;
    });
    const parts = Object.keys(perItem)
      .sort((a, b) => perItem[b] - perItem[a])
      .slice(0, 3)
      .map((k) => `${k} ${perItem[k]}`);
    parts.push(SORT_LABEL[R.sort] || R.sort);
    $("#q-sub").textContent = parts.join(" · ");
    $("#qhead").classList.toggle("hidden", R.mode !== "queue");

    // Tab badge: what is waiting.
    const badge = $("#tab-badge");
    const nq = queueRows().length;
    badge.hidden = !nq;
    badge.textContent = nq > 99 ? "99+" : String(nq);

    // Command-style "today" strip: what arrived while you were away.
    const t0 = startOfToday();
    const ratedToday = R.listings.filter((r) => (r.rated_at || 0) >= t0);
    // "N new" counts what is worth a look; the low ones get a muted tail so
    // the AI's real workload is still visible without cluttering the number.
    const today = ratedToday.filter(notLow);
    const lowToday = ratedToday.length - today.length;
    const notified = today.filter((r) => r.verdict === "notified").length;
    const promising = today.filter((r) => r.verdict === "promising").length;
    const blocks = activeBlocks(state.monitorInfo);
    const chips = [];
    chips.push(`<span class="pill">today <b>${today.length}</b> new${lowToday ? ` <span class="muted">· ${lowToday} low</span>` : ""}</span>`);
    if (notified) chips.push(`<span class="pill"><b>${notified}</b> notified</span>`);
    if (promising) chips.push(`<span class="pill"><b>${promising}</b> promising</span>`);
    blocks.forEach((b) => chips.push(`<span class="pill blk">⛔ ${esc(b.marketplace)} blocked</span>`));
    $("#today-strip").innerHTML = R.listings.length ? chips.join("") : "";
  };

  const renderSession = () => {
    const t0 = startOfToday();
    const today = R.listings.filter((r) => (r.reviewed_at || 0) >= t0);
    $("#sess-reviewed").textContent = today.length;
    $("#sess-kept").textContent = today.filter((r) => r.kept).length;
    $("#sess-dismissed").textContent = today.filter((r) => r.hidden).length;
    const notifiedToday = R.listings.filter((r) => r.verdict === "notified" && String(r.notified_at || "").startsWith(new Date().toISOString().slice(0, 10)));
    $("#sess-notified").textContent = notifiedToday.length || R.listings.filter((r) => r.verdict === "notified").length;
    $("#act-undo").disabled = !R.undo.length;
  };

  const renderPills = () => {
    const host = $("#item-pills");
    if (!R.summary.length) {
      host.innerHTML = "";
      return;
    }
    const ordered = R.summary.slice().sort((a, b) => Number(b.active !== false) - Number(a.active !== false));
    host.innerHTML =
      `<button class="chip ${R.item ? "" : "on"}" data-item-pill="">All items</button>` +
      ordered
        .map((s) => {
          const paused = s.active === false;
          const tip = `${s.item}: ${s.examined} rated · review ≥ ${s.review_threshold ?? s.threshold}, notify ≥ ${s.threshold} · ${s.promising} promising · ${s.notified} notified · ${s.low || 0} low` + (paused ? " · paused — tap to view its history" : "");
          return `<button class="chip ${R.item === s.item ? "on" : ""} ${paused ? "paused" : ""}" data-item-pill="${esc(s.item)}" title="${esc(tip)}">${esc(s.item)} <span class="c">${s.examined}${paused ? " ⏸" : ""}</span></button>`;
        })
        .join("");
    $$("#verdict-chips .chip").forEach((c) => c.classList.toggle("on", (c.dataset.verdict || "") === R.verdict));
    $("#verdict-chips").classList.toggle("hidden", R.mode !== "all");
    const counter = $("#activity-count");
    const rows = currentRows();
    counter.textContent = R.total ? `${rows.length} of ${R.total} rated` + (R.truncated ? " (truncated)" : "") : "";
  };

  // ---- card stack ----
  const cardHtml = (row, cls) => {
    const why = row.comment ? `<div class="why ${whyClass(row.score)}"><b>${esc(row.conclusion)}.</b> ${esc(row.comment)}</div>` : "";
    const metaBits = [];
    if (row.distance_mi != null) metaBits.push(`<b>${row.distance_mi} mi</b>`);
    const route = R.routes[rowKey(row)];
    if (route) metaBits.push(`≈ ${route.minutes} min drive`);
    if (row.location) metaBits.push(esc(row.location));
    if (row.condition) metaBits.push(esc(row.condition));
    if (row.marketplace !== "facebook") metaBits.unshift(esc(marketLabel(row.marketplace)));
    const tagCls = verdictClass(row);
    const tagTxt = verdictText(row);
    return `
      <div class="tcard ${cls}" data-key="${esc(rowKey(row))}">
        <span class="stamp keep">KEEP</span><span class="stamp nope">NOPE</span>
        <div class="ph">${photoImg(row)}<span class="phtxt">${photoUrl(row) ? "" : "no photo"}</span>
          <span class="sc ${scoreClass(row.score)}">${row.score} / 5</span>${srcGlyph(row.marketplace)}
          <span class="tag ${tagCls}">${esc(tagTxt)} · ${esc(row.item)}</span>
          ${row.my_rank ? `<span class="dist">★ ${row.my_rank} mine</span>` : ""}
        </div>
        <div class="body">
          <div class="price">${esc(priceText(row))}</div>
          <div class="title">${esc(row.title)}</div>
          <div class="meta">${metaBits.join(" · ")}</div>
          ${why}
        </div>
      </div>`;
  };

  const renderStack = () => {
    const host = $("#stack");
    const rows = queueRows();
    const row = current();
    host.querySelectorAll(".tcard, .stack-empty").forEach((n) => n.remove());
    if (!row) {
      const done = R.listings.length ? "Queue clear" : "Nothing rated yet";
      const sub = R.listings.length
        ? "Every listing has a decision. New ones appear here as the AI rates them."
        : "Listings appear here once the AI has scored them.";
      host.insertAdjacentHTML("beforeend", `<div class="stack-empty"><b>${done}</b><span>${sub}</span></div>`);
      return;
    }
    const idx = rows.findIndex((r) => rowKey(r) === rowKey(row));
    const behind = rows[idx + 1];
    const behind2 = rows[idx + 2];
    let html = "";
    if (behind2) html += `<div class="tcard behind2"></div>`;
    if (behind) html += cardHtml(behind, "behind");
    html += cardHtml(row, "top");
    host.insertAdjacentHTML("beforeend", html);
    bindSwipe(host.querySelector(".tcard.top"));
    prefetchRoute(row);
  };

  // ---- lists (rail on desktop; Reviewed / All in the centre on phones) ----
  const rowHtml = (row, extraCls) => {
    const meta = [];
    if (row.marketplace !== "facebook") meta.push(esc(marketLabel(row.marketplace)));
    if (row.distance_mi != null) meta.push(`${row.distance_mi} mi`);
    const route = R.routes[rowKey(row)];
    if (route) meta.push(`≈ ${route.minutes} min`);
    if (row.location) meta.push(esc(row.location));
    meta.push(esc(row.item));
    let right = `<span class="sc ${scoreClass(row.score)}">${row.score}</span>`;
    if (row.my_rank) right += `<span class="stars">${"★".repeat(row.my_rank)}${"☆".repeat(5 - row.my_rank)}</span>`;
    else if (row.verdict === "notified") right += `<span class="tag noti">Notified</span>`;
    else if (row.kept) right += `<span class="tag keep">Kept</span>`;
    const undoBtn = isReviewed(row) ? `<button class="ubtn" data-undo-row="${esc(rowKey(row))}" title="Undo — back to the queue" aria-label="Undo">↶</button>` : "";
    return `
      <div class="lrow ${extraCls || ""} ${row.hidden ? "dis" : ""} ${rowKey(row) === R.cursor ? "cur" : ""}" data-key="${esc(rowKey(row))}">
        <div class="th">${photoImg(row, "")}img</div>
        <div class="tx"><div class="p">${esc(priceText(row))}</div><div class="t">${esc(row.title)}</div><div class="m">${meta.join(" · ")}</div></div>
        <div class="right">${right}</div>${undoBtn}
        <button class="undo" data-undo-row="${esc(rowKey(row))}">Undo</button>
      </div>`;
  };

  const LIST_PAGE = 30;
  const listHtml = (mode) => {
    if (mode === "queue") {
      const rows = queueRows();
      const t0 = startOfToday();
      const doneToday = R.listings.filter((r) => notLow(r) && isReviewed(r) && itemMatch(r) && (r.reviewed_at || 0) >= t0).sort((a, b) => (b.reviewed_at || 0) - (a.reviewed_at || 0)).slice(0, 8);
      let html = rows.length ? rows.map((r) => rowHtml(r)).join("") : `<div class="list-empty">Queue clear.</div>`;
      if (doneToday.length) html += `<div class="grp">Reviewed today</div>` + doneToday.map((r) => rowHtml(r, "dis")).join("");
      return html;
    }
    if (mode === "reviewed") {
      const rows = reviewedRows();
      if (!rows.length) return `<div class="list-empty">Nothing reviewed yet${R.rchip ? " under this filter" : ""}. Decisions from the queue land here.</div>`;
      const shown = R.showAll ? rows : rows.slice(0, LIST_PAGE);
      const kept = shown.filter((r) => r.kept);
      const dismissed = shown.filter((r) => r.hidden && !r.kept);
      const rated = shown.filter((r) => !r.kept && !r.hidden);
      let html = "";
      if (kept.length) html += `<div class="grp">Kept <span class="n">· swipe a row left to undo</span></div><div class="rowcard">${kept.map((r) => rowHtml(r)).join("")}</div>`;
      if (rated.length) html += `<div class="grp">Rated <span class="n">· ${rated.length}</span></div><div class="rowcard">${rated.map((r) => rowHtml(r)).join("")}</div>`;
      if (dismissed.length) html += `<div class="grp">Dismissed <span class="n">· ${dismissed.length}</span></div><div class="rowcard">${dismissed.map((r) => rowHtml(r)).join("")}</div>`;
      if (rows.length > shown.length) html += `<button class="btn ghost more" data-show-more>Show ${rows.length - shown.length} more</button>`;
      return html;
    }
    const rows = allRows();
    if (!rows.length) return `<div class="list-empty">${R.total ? "No listings match these filters." : "Nothing rated yet. Listings appear here once the AI has scored them."}</div>`;
    const shown = R.showAll ? rows : rows.slice(0, LIST_PAGE);
    let html = `<div class="fgrid">${shown.map(feedCard).join("")}</div>`;
    if (rows.length > shown.length) html += `<button class="btn ghost more" data-show-more>Show ${rows.length - shown.length} more</button>`;
    return html;
  };

  const feedCard = (row) => {
    const tagCls = verdictClass(row);
    const tagTxt = verdictText(row);
    const meta = [];
    if (row.distance_mi != null) meta.push(`<b>${row.distance_mi} mi</b>`);
    const route = R.routes[rowKey(row)];
    if (route) meta.push(`≈ ${route.minutes} min`);
    if (row.location) meta.push(esc(row.location));
    if (row.marketplace !== "facebook") meta.unshift(esc(marketLabel(row.marketplace)));
    const mine = row.my_rank ? `<span class="mine">★ ${row.my_rank}</span>` : "";
    return `
      <div class="fcard ${row.hidden ? "dis" : ""}" data-key="${esc(rowKey(row))}">
        <div class="ph">${photoImg(row)}${photoUrl(row) ? "" : "no photo"}
          <span class="sc ${scoreClass(row.score)}">${row.score} / 5</span>${srcGlyph(row.marketplace)}
          <span class="tags"><span class="tag ${tagCls}">${esc(tagTxt)}</span><span class="tag">${esc(row.item)}</span>${row.kept ? '<span class="tag keep">Kept</span>' : ""}${row.hidden ? '<span class="tag hid">Hidden</span>' : ""}</span>
        </div>
        <div class="body"><div class="price">${esc(priceText(row))}${mine}</div><div class="title">${esc(row.title)}</div><div class="meta">${meta.join(" · ")}</div></div>
      </div>`;
  };

  const renderRail = () => {
    const rail = $("#rail");
    if (!isDesktop()) {
      const filters = $("#review-filters");
      if (filters.parentElement !== $("#review-appbar")) {
        $("#review-appbar").appendChild(filters);
        filters.classList.toggle("hidden", !R.filtersOpen);
      }
      rail.innerHTML = "";
      return;
    }
    const q = queueRows().length;
    // Park the filter drawer in the appbar while the rail is rewritten, or
    // the innerHTML assignment would destroy it (and its listeners).
    const filters = $("#review-filters");
    $("#review-appbar").appendChild(filters);
    rail.innerHTML =
      `<div class="seg">${["queue", "reviewed", "all"].map((m) => `<button data-mode="${m}" class="${R.mode === m ? "on" : ""}">${m[0].toUpperCase() + m.slice(1)} <span class="n">${rowsForMode(m).length}</span></button>`).join("")}</div>` +
      (R.mode === "queue" ? `<div class="qhead"><span class="cnt">${q} to review</span><span class="bar"><i style="width:${$("#q-bar").style.width}"></i></span></div>` : "") +
      `<div id="rail-filters"></div>` +
      (R.mode === "all" ? allRows().slice(0, R.showAll ? undefined : 200).map((r) => rowHtml(r)).join("") || `<div class="list-empty">No listings match.</div>` : listHtml(R.mode));
    // The filter drawer is one element; on desktop it lives in the rail (always
    // open), on phones behind the appbar's filter button. Moving the node
    // keeps its listeners.
    $("#rail-filters").appendChild(filters);
    filters.classList.remove("hidden");
    const cur = rail.querySelector(".lrow.cur");
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
  };

  const renderList = () => {
    const pane = $("#list-pane");
    const desktop = isDesktop();
    // On desktop the rail is the list; the centre shows the current row.
    if (R.mode === "queue" || desktop) {
      pane.classList.add("hidden");
      pane.innerHTML = "";
      return;
    }
    pane.classList.remove("hidden");
    pane.innerHTML =
      (R.mode === "reviewed"
        ? `<div class="chips rchips">${[["", "All"], ["kept", "Kept"], ["dismissed", "Dismissed"], ["notified", "Notified"]]
            .map(([v, l]) => {
              const base = R.listings.filter((r) => notLow(r) && isReviewed(r) && itemMatch(r) && textMatch(r));
              const n = v === "" ? base.length : v === "kept" ? base.filter((r) => r.kept).length : v === "dismissed" ? base.filter((r) => r.hidden).length : base.filter((r) => r.verdict === "notified").length;
              return `<button class="chip ${R.rchip === v ? "on" : ""}" data-rchip="${v}">${l} <span class="c">${n}</span></button>`;
            })
            .join("")}</div>`
        : "") + listHtml(R.mode);
  };


  // ---------------------------------------------------------------
  // Photo carousel + lightbox
  //
  // The track is a native horizontal scroll-snap container, so swiping on a
  // phone is the browser's own gesture — no touch handler competing with the
  // card swipe that keeps or dismisses the listing. Everything else (dots,
  // arrows, keys) works by scrolling that same container, so there is one
  // source of truth for "which photo is showing": its scroll position.
  //
  // Keys, stated once so they cannot collide with the review keys: ← and →
  // always decide the listing (dismiss / keep). Photos move on , and . , or
  // on Shift+← / Shift+→. Inside the lightbox, where there is no listing to
  // decide, bare ← and → move photos too.
  // ---------------------------------------------------------------
  const galEl = () => $("#detail-pane .ph.gal");
  const galTrack = () => $("#detail-pane .ph.gal .track");
  const galIndex = () => {
    const track = galTrack();
    if (!track || !track.clientWidth) return 0;
    return Math.round(track.scrollLeft / track.clientWidth);
  };
  const galLive = () => {
    // Slides whose photo 404'd are dead weight: the CDN URL expired and there
    // is nothing to show. They stay in the DOM (indexes must keep matching
    // the proxy's `i`) but drop out of the dots and the count.
    const gal = galEl();
    return gal ? $$(".slide", gal).filter((s) => !s.classList.contains("dead")) : [];
  };

  // `forceIdx` is the photo a click or a keypress just asked for. A smooth
  // scroll takes a few hundred ms to land, and dots that only follow the
  // scroll position lag visibly behind the press; this lets them move at
  // once, and the scroll listener re-syncs when the animation settles.
  const syncGal = (forceIdx) => {
    const gal = galEl();
    if (!gal) return;
    const idx = typeof forceIdx === "number" ? forceIdx : galIndex();
    const slides = $$(".slide", gal);
    // Fill in the slides now within reach. Two ahead is enough that a swipe
    // never lands on a blank, and far short of loading the whole gallery.
    slides.forEach((slide, i) => {
      if (i > idx + LAZY_AHEAD) return;
      const img = slide.querySelector("img[data-src]");
      if (img) {
        img.src = img.getAttribute("data-src");
        img.removeAttribute("data-src");
      }
    });
    const live = galLive();
    $$(".dots i", gal).forEach((dot, i) => {
      dot.classList.toggle("on", i === idx);
      dot.classList.toggle("gone", slides[i] ? slides[i].classList.contains("dead") : false);
    });
    const counter = gal.querySelector(".pcount");
    if (counter) counter.textContent = `${idx + 1} / ${slides.length}`;
    const prev = gal.querySelector(".gnav.prev");
    const next = gal.querySelector(".gnav.next");
    if (prev) prev.disabled = idx <= 0;
    if (next) next.disabled = idx >= slides.length - 1;
    gal.classList.toggle("single", live.length < 2);
    if (!live.length) gal.remove();
    if (lightboxOpen()) syncLightbox();
  };

  const scrollGalTo = (idx) => {
    const track = galTrack();
    if (!track) return false;
    const slides = $$(".slide", track);
    const target = Math.max(0, Math.min(slides.length - 1, idx));
    track.scrollTo({ left: target * track.clientWidth, behavior: "smooth" });
    syncGal(target);
    return true;
  };
  // Returns whether it did anything, so a keypress over a listing with one
  // photo falls through instead of being swallowed.
  const movePhoto = (delta) => {
    const track = galTrack();
    if (!track || $$(".slide", track).length < 2) return false;
    const idx = galIndex();
    const next = idx + delta;
    if (next < 0 || next >= $$(".slide", track).length) return false;
    scrollGalTo(next);
    return true;
  };

  const bindGallery = () => {
    const gal = galEl();
    if (!gal) return;
    const track = galTrack();
    let raf = null;
    track.addEventListener("scroll", () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        syncGal();
      });
    });
    $$("img", gal).forEach((img) => {
      img.addEventListener("error", () => {
        const slide = img.closest(".slide");
        if (slide) slide.classList.add("dead");
        img.remove();
        syncGal();
      });
    });
    gal.querySelectorAll("[data-photo]").forEach((btn) =>
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        movePhoto(Number(btn.dataset.photo));
      })
    );
    gal.querySelectorAll("[data-dot]").forEach((dot) =>
      dot.addEventListener("click", (e) => {
        e.stopPropagation();
        scrollGalTo(Number(dot.dataset.dot));
      })
    );
    $$(".slide", gal).forEach((slide) =>
      slide.addEventListener("click", () => {
        if (!slide.classList.contains("dead")) openLightbox();
      })
    );
    syncGal();
  };

  // The lightbox is deliberately plain: the same photo, as big as the screen
  // allows, on a black backdrop. No pinch-zoom handling and no pan — a
  // Marketplace photo is 960px wide, so there is nothing to zoom into, and a
  // custom gesture layer here would fight the browser's own.
  const lightbox = () => $("#lightbox");
  const lightboxOpen = () => !!lightbox() && !lightbox().classList.contains("hidden");
  const syncLightbox = () => {
    const gal = galEl();
    const box = lightbox();
    if (!gal || !box) return;
    const idx = galIndex();
    const slides = $$(".slide", gal);
    const src = slides[idx] && slides[idx].querySelector("img");
    const img = $("#lightbox-img");
    if (src && img) {
      img.src = src.currentSrc || src.src;
      img.alt = src.alt || "";
    }
    $("#lightbox-count").textContent = slides.length > 1 ? `${idx + 1} / ${slides.length}` : "";
  };
  const openLightbox = () => {
    if (!galEl() || !lightbox()) return;
    lightbox().classList.remove("hidden");
    syncLightbox();
  };
  const closeLightbox = () => lightbox() && lightbox().classList.add("hidden");
  if (lightbox()) {
    $("#lightbox-backdrop").addEventListener("click", closeLightbox);
    $("#lightbox-close").addEventListener("click", closeLightbox);
  }

  // ---- detail ----
  let dealMap = null;
  const detailHtml = (row, pos, total) => {
    const tagCls = verdictClass(row);
    const tagTxt = verdictText(row);
    const route = R.routes[rowKey(row)];
    const distBits = [];
    if (row.distance_mi != null) distBits.push(`<b>${row.distance_mi} mi away</b>`);
    if (route) distBits.push(`≈ ${route.minutes} min · ${route.miles} mi by road`);
    const facts = [];
    if (row.location) facts.push(["Location", row.location]);
    if (row.condition) facts.push(["Condition", row.condition]);
    if (row.seller) facts.push(["Seller", row.seller]);
    const reviewThr = row.review_threshold == null ? row.threshold : row.review_threshold;
    const tierMet = row.score >= row.threshold ? "notify met" : row.score >= reviewThr ? "review met" : "not met";
    facts.push(["Threshold", `review ≥ ${reviewThr} · notify ≥ ${row.threshold} · ${tierMet}`]);
    if (row.notified_at) facts.push(["Notified", row.notified_at]);
    const showMap = !!(row.coords && R.home && row.marketplace === "facebook" && window.L);
    const RATE = ["pass", "meh", "maybe", "good", "must see"];
    return `
      <div class="dbar">
        <button class="ib" id="detail-back" aria-label="Collapse" title="Collapse (Esc)"><svg class="icon" viewBox="0 0 24 24"><path d="M5 15l7-7 7 7"/></svg></button>
        <span class="t">${typeof pos === "number" ? `${pos} of ${total}` : pos} · ${esc(row.item)} · ${esc(marketLabel(row.marketplace))}</span>
        <button class="ib" id="detail-next" aria-label="Next" title="Next (J)"><svg class="icon" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7"/></svg></button>
      </div>
      <div class="dbody">
      ${detailPhoto(row)}
      <div class="sec">
        <div class="price">${esc(priceText(row))}${row.my_rank ? `<span class="tag keep">★ ${row.my_rank} mine</span>` : ""}</div>
        <h2>${esc(row.title)}</h2>
        <div class="badges"><span class="sc ${scoreClass(row.score)}">${row.score} / 5</span><span class="tag ${tagCls}">${esc(row.conclusion)} · ${esc(tagTxt)}</span><span class="tag">${esc(row.item)}</span>${row.kept ? '<span class="tag keep">Kept</span>' : ""}${row.hidden ? '<span class="tag hid">Hidden</span>' : ""}</div>
        <p class="dist" id="dd-drive-line">${distBits.join(" · ")}<span id="dd-drive">${route || row.distance_mi == null ? "" : ""}</span></p>
      </div>
      <div class="sec">
        <div class="facts">${facts.map(([k, v]) => `<div><small>${esc(k)}</small><span>${esc(v)}</span></div>`).join("")}</div>
        ${showMap ? '<div id="dd-map" class="dd-map"></div>' : ""}
      </div>
      <div class="sec"><h4>Why the AI scored it ${row.score} / 5${row.ai_name ? ` <span class="sub">· ${esc(row.ai_name)}</span>` : ""}</h4>
        <div class="why2 ${whyClass(row.score)}"><div class="hd"><span class="sc ${scoreClass(row.score)}">${row.score}</span>${esc(row.conclusion)}</div>${esc(row.comment || "(no reasoning recorded)")}</div></div>
      <div class="sec"><h4>My rating <span class="sub">· press 1–5 · same key clears</span></h4>
        <div class="rate5" data-flag="rank">${[1, 2, 3, 4, 5].map((n) => `<button class="star ${row.my_rank === n ? "on" : ""}" data-rank="${n}">${row.my_rank >= n ? "★" : n}<small>${RATE[n - 1]}</small></button>`).join("")}</div></div>
      </div>
      <div class="actbar">
        <button class="btn no" data-flag="hide" id="dd-dismiss">${row.hidden ? "↶ Restore" : "✕ Dismiss"}</button>
        ${row.url ? `<a class="btn pri" id="dd-open" href="${esc(row.url)}" target="_blank" rel="noopener">Open ↗</a>` : ""}
        <button class="btn yes ${row.kept ? "on" : ""}" data-flag="keep" id="dd-keep">${row.kept ? "★ Kept" : "★ Keep"}</button>
      </div>`;
  };

  const renderDetail = () => {
    const pane = $("#detail-pane");
    const row = current();
    const desktop = isDesktop();
    const show = row && (R.detail || (desktop && R.mode !== "queue"));
    $("#app").classList.toggle("detail-open", !!(show && !desktop));
    if (dealMap) {
      // Leaflet throws (_leaflet_pos) when a map is removed mid-animation;
      // the map is gone either way, so the error is noise.
      try {
        dealMap.remove();
      } catch (_) {
        /* already torn down */
      }
      dealMap = null;
    }
    // A carousel that is being replaced cannot keep a lightbox open over it.
    closeLightbox();
    if (!show) {
      if (desktop && R.mode !== "queue") {
        // The rail is the list; say so instead of leaving the centre blank.
        pane.classList.remove("hidden");
        pane.innerHTML = `<div class="list-empty">${R.mode === "reviewed" ? "Nothing reviewed yet. Decisions from the queue land here." : "No listings match these filters."}</div>`;
        return;
      }
      pane.classList.add("hidden");
      pane.innerHTML = "";
      return;
    }
    const rows = currentRows();
    const pos = rows.findIndex((r) => rowKey(r) === rowKey(row)) + 1;
    pane.classList.remove("hidden");
    pane.innerHTML = detailHtml(row, pos || "reviewed", rows.length);
    pane.scrollTop = 0;
    const body = pane.querySelector(".dbody");
    if (body) body.scrollTop = 0;
    if (!desktop) window.scrollTo(0, 0);
    bindGallery();
    mountMap(row);
  };

  // Leaflet map + OSRM drive time for the selected physical listing. Tiles
  // are OSM's public servers (attribution required), routing is OSRM's demo
  // router — both free for light personal use, cached server-side for a day.
  const mountMap = (row) => {
    const host = $("#dd-map");
    if (!host || !window.L || !row.coords || !R.home) return;
    const item = row.coords;
    const home = R.home;
    dealMap = L.map(host, { zoomControl: false, attributionControl: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 17,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(dealMap);
    const icon = L.icon({
      iconUrl: "/static/vendor/leaflet/images/marker-icon.png",
      iconRetinaUrl: "/static/vendor/leaflet/images/marker-icon-2x.png",
      shadowUrl: "/static/vendor/leaflet/images/marker-shadow.png",
      iconSize: [25, 41],
      iconAnchor: [12, 41],
    });
    L.marker(home, { icon, title: "Home" }).addTo(dealMap);
    L.marker(item, { icon, title: row.location }).addTo(dealMap);
    // A dashed straight line until real geometry arrives, so the map is never
    // ambiguous about which two points it is relating.
    const hint = L.polyline([home, item], { weight: 2, opacity: 0.35, dashArray: "5,6" }).addTo(dealMap);
    dealMap.fitBounds([home, item], { padding: [30, 30], animate: false });
    const draw = (r) => {
      if (!dealMap || !r.geometry || r.geometry.length < 2) return;
      dealMap.removeLayer(hint);
      const line = L.polyline(r.geometry, { weight: 5, opacity: 0.85, color: "#3b6cf6" }).addTo(dealMap);
      dealMap.fitBounds(line.getBounds(), { padding: [26, 26], animate: false });
    };
    const key = rowKey(row);
    if (R.routes[key]) {
      draw(R.routes[key]);
      return;
    }
    const drive = $("#dd-drive");
    if (drive) drive.textContent = " · estimating drive…";
    fetchRoute(row).then((r) => {
      if (!r || R.cursor !== key) return;
      const line = $("#dd-drive-line");
      if (line) line.innerHTML = `<b>${row.distance_mi != null ? row.distance_mi + " mi away" : ""}</b>${row.distance_mi != null ? " · " : ""}≈ ${r.minutes} min · ${r.miles} mi by road<span id="dd-drive"></span>`;
      draw(r);
    });
  };

  const routePending = {};
  const fetchRoute = (row) => {
    const key = rowKey(row);
    if (R.routes[key]) return Promise.resolve(R.routes[key]);
    if (!row.coords || !R.home) return Promise.resolve(null);
    if (routePending[key]) return routePending[key];
    routePending[key] = api(`/api/route?to=${row.coords[0]},${row.coords[1]}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((r) => {
        if (r) R.routes[key] = r;
        delete routePending[key];
        return r;
      })
      .catch(() => {
        delete routePending[key];
        return null;
      });
    return routePending[key];
  };
  // The card shows "≈ N min drive" once the estimate is in; fetch it for the
  // current card so the next render can use it, without blocking anything.
  const prefetchRoute = (row) => {
    if (!row || row.marketplace !== "facebook" || !row.coords || !R.home || R.routes[rowKey(row)]) return;
    fetchRoute(row).then((r) => {
      if (r && R.cursor === rowKey(row) && !R.detail) {
        const meta = $("#stack .tcard.top .meta");
        if (meta && !/drive/.test(meta.textContent)) meta.insertAdjacentHTML("beforeend", ` · ≈ ${r.minutes} min drive`);
      }
    });
  };

  const render = () => {
    setSeg();
    renderHead();
    renderPills();
    const desktop = isDesktop();
    const queuePane = $("#queue-pane");
    const showStack = R.mode === "queue" && !(R.detail && current());
    queuePane.classList.toggle("hidden", !showStack);
    if (showStack) renderStack();
    else $("#stack").querySelectorAll(".tcard, .stack-empty").forEach((n) => n.remove());
    renderList();
    renderDetail();
    renderRail();
    renderSession();
    if (!desktop && R.detail) {
      R.filtersOpen = false;
      $("#review-filters").classList.add("hidden");
    }
  };

  // ---------------------------------------------------------------
  // Swipe gesture (pointer events, no library).
  // Commit when the drag exceeds 40% of the card width (the mockup's
  // "drag ≥ 40% width"), or on a fling faster than 0.3 px/ms — the same
  // 300 px/s velocity threshold react-tinder-card ships as its default
  // `swipeThreshold`. Direction locks after 8px so vertical scrolling on the
  // page underneath keeps working (touch-action: pan-y on the card).
  // ---------------------------------------------------------------
  const SWIPE_FRACTION = 0.4;
  const FLING_VELOCITY = 0.3; // px per ms
  const LOCK_PX = 8;
  const bindSwipe = (card) => {
    if (!card) return;
    let drag = null;
    const stack = $("#stack");
    const keepStamp = card.querySelector(".stamp.keep");
    const nopeStamp = card.querySelector(".stamp.nope");
    const keepBtn = $("#act-keep");
    const nopeBtn = $("#act-dismiss");
    const reset = () => {
      card.classList.remove("dragging", "keeping", "noping");
      stack.classList.remove("dragging");
      card.style.transform = "";
      card.style.opacity = "";
      keepStamp.style.opacity = "0";
      nopeStamp.style.opacity = "0";
      keepBtn.classList.remove("hot");
      nopeBtn.classList.remove("hot");
      const hint = $("#swipehint");
      if (hint) hint.innerHTML = "Swipe <b>left</b> to dismiss · <b>right</b> to keep · tap card for details";
    };
    card.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      drag = { x0: e.clientX, y0: e.clientY, t0: performance.now(), dx: 0, dy: 0, locked: null, id: e.pointerId, vx: 0, lastT: performance.now() };
      try {
        card.setPointerCapture(e.pointerId);
      } catch (_) {
        /* synthetic events */
      }
    });
    card.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const now = performance.now();
      const prevDx = drag.dx;
      drag.dx = e.clientX - drag.x0;
      drag.dy = e.clientY - drag.y0;
      // Velocity at release, not over the whole gesture: a finger that drags
      // out and pauses is deciding, not flinging (react-tinder-card and
      // Hammer.js both read the last few samples). Exponentially smoothed.
      const dt = Math.max(1, now - drag.lastT);
      drag.vx = 0.6 * ((drag.dx - prevDx) / dt) + 0.4 * drag.vx;
      drag.lastT = now;
      if (drag.locked === null && (Math.abs(drag.dx) > LOCK_PX || Math.abs(drag.dy) > LOCK_PX)) {
        drag.locked = Math.abs(drag.dx) > Math.abs(drag.dy) ? "x" : "y";
        if (drag.locked === "x") {
          card.classList.add("dragging");
          stack.classList.add("dragging");
        }
      }
      if (drag.locked !== "x") return;
      const threshold = card.offsetWidth * SWIPE_FRACTION;
      const dx = drag.dx;
      card.style.transform = `translateX(${dx}px) rotate(${dx / 18}deg)`;
      const p = Math.min(1, Math.abs(dx) / threshold);
      keepStamp.style.opacity = dx > 0 ? String(p) : "0";
      nopeStamp.style.opacity = dx < 0 ? String(p) : "0";
      card.classList.toggle("keeping", dx > 0 && p >= 1);
      card.classList.toggle("noping", dx < 0 && p >= 1);
      keepBtn.classList.toggle("hot", dx > 0 && p >= 1);
      nopeBtn.classList.toggle("hot", dx < 0 && p >= 1);
      const hint = $("#swipehint");
      if (hint) hint.innerHTML = dx > 0 ? `Dragging right · <b>${p >= 1 ? "release to keep" : "keep drag going"}</b>` : `Dragging left · <b>${p >= 1 ? "release to dismiss" : "keep drag going"}</b>`;
    });
    const finish = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag;
      drag = null;
      // A pause of 100ms before lifting means the fling is over.
      const vx = performance.now() - d.lastT > 100 ? 0 : d.vx;
      const threshold = card.offsetWidth * SWIPE_FRACTION;
      if (d.locked === "x" && (Math.abs(d.dx) >= threshold || (Math.abs(vx) >= FLING_VELOCITY && Math.abs(d.dx) > 30))) {
        throwCard(card, d.dx > 0 ? "keep" : "dismiss");
        return;
      }
      if (d.locked === null && e.type === "pointerup") {
        // A tap on the card, not a drag.
        reset();
        openDetail();
        return;
      }
      reset();
    };
    card.addEventListener("pointerup", finish);
    card.addEventListener("pointercancel", (e) => {
      if (drag && e.pointerId === drag.id) {
        drag = null;
        reset();
      }
    });
    card.addEventListener("dragstart", (e) => e.preventDefault());
  };

  // Keyboard and button decisions animate the same throw the finger makes.
  let throwing = false;
  const throwCard = (card, kind) => {
    const row = current();
    if (!row || throwing) return;
    if (!card) {
      decide(row, kind);
      return;
    }
    throwing = true;
    const w = card.offsetWidth || 360;
    const dir = kind === "keep" ? 1 : -1;
    card.classList.add("dragging");
    card.style.transition = "transform .28s ease-in, opacity .28s ease-in";
    card.style.transform = `translateX(${dir * w * 1.6}px) rotate(${dir * 22}deg)`;
    card.style.opacity = "0";
    const stamp = card.querySelector(kind === "keep" ? ".stamp.keep" : ".stamp.nope");
    if (stamp) stamp.style.opacity = "1";
    setTimeout(() => {
      throwing = false;
      $("#stack").classList.remove("dragging");
      $("#act-keep").classList.remove("hot");
      $("#act-dismiss").classList.remove("hot");
      decide(row, kind);
    }, 240);
  };

  const openDetail = () => {
    if (!current()) return;
    R.detail = true;
    render();
  };
  const closeDetail = () => {
    R.detail = false;
    render();
  };
  const move = (delta) => {
    const rows = currentRows();
    if (!rows.length) return;
    const idx = Math.max(0, rows.findIndex((r) => rowKey(r) === R.cursor));
    const next = rows[Math.min(rows.length - 1, Math.max(0, idx + delta))];
    if (next) R.cursor = rowKey(next);
    render();
  };

  // ---------------------------------------------------------------
  // Wiring
  // ---------------------------------------------------------------
  const setMode = (mode) => {
    R.mode = mode;
    R.detail = false;
    R.showAll = false;
    R.cursor = null;
    render();
  };
  document.addEventListener("click", (e) => {
    const seg = e.target.closest("#mode-seg [data-mode], #rail [data-mode]");
    if (seg) {
      setMode(seg.dataset.mode);
      return;
    }
    const pill = e.target.closest("[data-item-pill]");
    if (pill) {
      const picked = pill.dataset.itemPill;
      R.item = R.item === picked ? "" : picked;
      R.cursor = null;
      render();
      return;
    }
    const vchip = e.target.closest("#verdict-chips [data-verdict]");
    if (vchip) {
      R.verdict = vchip.dataset.verdict || "";
      R.cursor = null;
      render();
      return;
    }
    const rchip = e.target.closest("[data-rchip]");
    if (rchip) {
      R.rchip = rchip.dataset.rchip || "";
      R.showAll = false;
      render();
      return;
    }
    const more = e.target.closest("[data-show-more]");
    if (more) {
      R.showAll = true;
      render();
      return;
    }
    const undoRow = e.target.closest("[data-undo-row]");
    if (undoRow) {
      e.stopPropagation();
      const row = byKey(undoRow.dataset.undoRow);
      if (row) {
        pushUndo(row);
        sendFlag(row, { kept: false, hidden: false, my_rank: null }).then(() => {
          render();
          toast("Back in the queue", { undo });
        });
      }
      return;
    }
    const lrow = e.target.closest("#rail .lrow, #list-pane .lrow, #list-pane .fcard");
    if (lrow) {
      if (rowJustDragged) return; // the tail end of a swipe, not a tap
      if (lrow.classList.contains("revealed")) {
        lrow.classList.remove("revealed");
        return;
      }
      R.cursor = lrow.dataset.key;
      if (!isDesktop() || R.mode !== "queue") R.detail = true;
      render();
      return;
    }
    if (e.target.closest("#detail-back")) {
      closeDetail();
      return;
    }
    if (e.target.closest("#detail-next")) {
      move(1);
      return;
    }
    const star = e.target.closest("#detail-pane .star");
    if (star) {
      rate(current(), Number(star.dataset.rank));
      return;
    }
    if (e.target.closest("#detail-pane [data-flag=hide]")) {
      const row = current();
      if (row && row.hidden) toggleHidden(row);
      else decide(row, "dismiss");
      return;
    }
    if (e.target.closest("#detail-pane [data-flag=keep]")) {
      decide(current(), "keep");
      return;
    }
  });
  $("#act-keep").addEventListener("click", () => throwCard($("#stack .tcard.top"), "keep"));
  $("#act-dismiss").addEventListener("click", () => throwCard($("#stack .tcard.top"), "dismiss"));
  $("#act-details").addEventListener("click", openDetail);
  $("#act-undo").addEventListener("click", undo);
  $("#filter-btn").addEventListener("click", () => {
    R.filtersOpen = !R.filtersOpen;
    $("#review-filters").classList.toggle("hidden", !R.filtersOpen);
  });
  $("#activity-filter").addEventListener("input", (e) => {
    R.text = e.target.value;
    R.cursor = null;
    render();
  });
  $("#activity-refresh").addEventListener("click", load);
  const sortSel = $("#deal-sort");
  sortSel.addEventListener("change", (e) => {
    R.sort = e.target.value;
    try {
      localStorage.setItem("aimm.dealSort", R.sort);
    } catch (_) {
      /* private browsing: the choice just does not persist */
    }
    R.cursor = null;
    render();
  });
  try {
    const saved = localStorage.getItem("aimm.dealSort");
    if (saved && SORTERS[saved]) {
      R.sort = saved;
      sortSel.value = saved;
    }
  } catch (_) {
    /* ignore */
  }

  // Row swipe-left reveals Undo on the reviewed lists (mockup: "swipe a row
  // left to undo"). Same lock logic as the card, smaller travel.
  let rowDrag = null;
  document.addEventListener("pointerdown", (e) => {
    const row = e.target.closest("#list-pane .lrow");
    if (!row || e.button !== 0 || !row.querySelector("[data-undo-row]")) return;
    rowDrag = { row, x0: e.clientX, y0: e.clientY, locked: null };
  });
  document.addEventListener("pointermove", (e) => {
    if (!rowDrag) return;
    const dx = e.clientX - rowDrag.x0;
    const dy = e.clientY - rowDrag.y0;
    if (rowDrag.locked === null && (Math.abs(dx) > LOCK_PX || Math.abs(dy) > LOCK_PX)) rowDrag.locked = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    if (rowDrag.locked !== "x") return;
    if (dx < -60) rowDrag.row.classList.add("revealed");
    else if (dx > 20) rowDrag.row.classList.remove("revealed");
  });
  let rowJustDragged = false;
  document.addEventListener("pointerup", () => {
    if (rowDrag && rowDrag.locked === "x") {
      rowJustDragged = true;
      setTimeout(() => {
        rowJustDragged = false;
      }, 300);
    }
    rowDrag = null;
  });

  // Desktop keyboard flow. Ignored while typing or with a modal open.
  document.addEventListener("keydown", (e) => {
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    // The lightbox is its own little mode: there is no listing to decide on
    // while a photo fills the screen, so the arrows move photos there.
    if (lightboxOpen()) {
      if (e.key === "Escape") closeLightbox();
      else if (e.key === "ArrowRight" || e.key === "." || e.key === ">") movePhoto(1);
      else if (e.key === "ArrowLeft" || e.key === "," || e.key === "<") movePhoto(-1);
      else return;
      e.preventDefault();
      return;
    }
    if (state.view !== "review" || modalOpen()) return;
    const row = current();
    const key = e.key;
    const top = $("#stack .tcard.top");
    const handled = () => e.preventDefault();
    // Photos before decisions: , and . (and Shift+arrows for anyone who
    // reaches for the arrows) never reach the keep/dismiss branches below,
    // and bare arrows never reach these.
    if (key === "," || key === "<" || (key === "ArrowLeft" && e.shiftKey)) {
      if (movePhoto(-1)) handled();
    } else if (key === "." || key === ">" || (key === "ArrowRight" && e.shiftKey)) {
      if (movePhoto(1)) handled();
    } else if (key === "j" || key === "J") {
      move(1);
      handled();
    } else if (key === "k" || key === "K") {
      move(-1);
      handled();
    } else if (key === "ArrowRight" || key === "s" || key === "S") {
      if (R.mode === "queue" && !R.detail) throwCard(top, "keep");
      else decide(row, "keep");
      handled();
    } else if (key === "ArrowLeft" || key === "x" || key === "X") {
      if (R.mode === "queue" && !R.detail) throwCard(top, "dismiss");
      else decide(row, "dismiss");
      handled();
    } else if (/^[1-5]$/.test(key)) {
      rate(row, Number(key));
      handled();
    } else if (key === "Enter") {
      if (R.detail) closeDetail();
      else openDetail();
      handled();
    } else if (key === "Escape") {
      if (R.detail) {
        closeDetail();
        handled();
      } else if (!isDesktop() && R.filtersOpen) {
        // Phone drawer only; on desktop the filters live in the rail.
        R.filtersOpen = false;
        $("#review-filters").classList.add("hidden");
      }
    } else if (key === "o" || key === "O") {
      openListing(row);
      handled();
    } else if (key === "z" || key === "Z") {
      undo();
      handled();
    } else if (key === "h" || key === "H") {
      toggleHidden(row);
      handled();
    } else if (key === "r" || key === "R") {
      window.AIMM.searchNow();
      handled();
    }
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (state.view === "review") render();
    }, 150);
  });
  on("monitor", () => {
    if (state.view === "review") renderHead();
  });

  window.AIMM.views.review = { show: () => (R.listings.length ? render() : load()) };
  window.AIMM.boot(load);

  // Pure derivations for the QA harness: the queue split must be assertable
  // without a browser gesture.
  window.__aimm = Object.assign(window.__aimm || {}, {
    isReviewed,
    rowKey,
    queueRows,
    reviewedRows,
    allRows,
    review: R,
    swipe: { SWIPE_FRACTION, FLING_VELOCITY, LOCK_PX },
    photoCount,
    photoUrl,
    gallery: { index: galIndex, move: movePhoto, to: scrollGalTo, open: openLightbox, close: closeLightbox, isOpen: lightboxOpen },
  });
})();
