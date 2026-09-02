// AI Marketplace Monitor — Triage UI, core module.
// Vanilla JS, no build step. Classic scripts share the window.AIMM namespace:
//   app-core.js    auth, fetch/CSRF, router, monitor-state poller, header chip,
//                  WebSocket log stream, toast, shared formatters, boot
//   app-review.js  the review queue (cards, swipe, detail, keyboard, lists)
//   app-config.js  Items + Sources screens, TOML editor, section forms
//   app-status.js  Status screen + logs panel
(() => {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const state = {
    csrf: null,
    view: "review",
    monitorInfo: null, // last /api/monitor/state payload
    envVars: null, // last /api/env-status payload
    status: null, // /api/status bootstrap payload
    supportedMarketplaces: ["facebook", "ebay", "depop", "poshmark"],
    wsConnected: false,
    ws: null,
    records: [],
    lastActivity: null,
    errorCount: 0,
    booted: false,
    _monitorPoll: null,
  };

  const esc = (s) =>
    String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  // ---------------------------------------------------------------
  // Tiny event bus so modules can react without importing each other.
  // ---------------------------------------------------------------
  const listeners = {};
  const on = (name, fn) => {
    (listeners[name] = listeners[name] || []).push(fn);
  };
  const emit = (name, data) => {
    (listeners[name] || []).forEach((fn) => {
      try {
        fn(data);
      } catch (err) {
        console.error("listener failed for", name, err);
      }
    });
  };

  // ---------------------------------------------------------------
  // Cookies / auth
  // ---------------------------------------------------------------
  const getCookie = (name) => {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  };

  const api = async (path, opts = {}) => {
    const headers = { ...(opts.headers || {}) };
    if (opts.method && opts.method !== "GET" && state.csrf) {
      headers["X-CSRF-Token"] = state.csrf;
    }
    if (opts.body && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
    if (res.status === 401) {
      showLogin();
      throw new Error("unauthenticated");
    }
    return res;
  };

  const showLogin = async () => {
    $("#login-screen").classList.remove("hidden");
    $("#app").classList.add("hidden");
    try {
      const info = await (await fetch("/api/auth/info", { credentials: "same-origin" })).json();
      if (info.open) {
        // Loopback: no credentials configured, auto-login as anonymous.
        const res = await fetch("/api/login", {
          method: "POST",
          body: new FormData(),
          credentials: "same-origin",
        });
        if (res.ok) {
          const data = await res.json();
          state.csrf = data.csrf || getCookie("aimm_csrf");
          hideLogin();
          await bootstrap();
          return;
        }
      }
      const subtitle = $("#login-subtitle");
      // Proxy-only mode: authentication happens at the reverse proxy and
      // there is no password here to accept, so a form would be a dead end.
      if (info.proxy_auth && !info.password_login) {
        $("#login-fields").hidden = true;
        subtitle.textContent =
          "Sign-in happens at your identity provider. Open this app through " +
          "its SSO address (the Traefik hostname) — direct access has no " +
          "password to accept.";
        subtitle.hidden = false;
        return;
      }
      $("#login-fields").hidden = false;
      subtitle.textContent = "Sign in with the marketplace credentials from your config.";
      subtitle.hidden = false;
      if (info.username_hint) $("#login-form").username.value = info.username_hint;
    } catch (err) {
      /* generic login form stays */
    }
  };
  const hideLogin = () => {
    $("#login-screen").classList.add("hidden");
    $("#app").classList.remove("hidden");
  };

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const body = new FormData();
    body.set("username", form.username.value);
    body.set("password", form.password.value);
    try {
      const res = await fetch("/api/login", { method: "POST", body, credentials: "same-origin" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Login failed" }));
        $("#login-error").textContent = err.detail || "Login failed";
        $("#login-error").hidden = false;
        return;
      }
      const data = await res.json();
      state.csrf = data.csrf || getCookie("aimm_csrf");
      $("#login-error").hidden = true;
      hideLogin();
      await bootstrap();
    } catch (err) {
      $("#login-error").textContent = String(err);
      $("#login-error").hidden = false;
    }
  });

  const logout = async () => {
    try {
      await api("/api/logout", { method: "POST" });
    } catch (_) {
      /* the session is gone either way */
    }
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.close();
    }
    state.csrf = null;
    state.booted = false;
    showLogin();
  };
  $("#logout-btn").addEventListener("click", logout);
  const logoutRow = $("#logout-row");
  if (logoutRow) logoutRow.addEventListener("click", logout);

  // ---------------------------------------------------------------
  // Router: bottom tabs on phones, top nav on desktop, same handler.
  // ---------------------------------------------------------------
  const views = {};
  const showView = (name) => {
    if (!views[name]) name = "review";
    state.view = name;
    $$("[data-view]").forEach((el) => el.classList.toggle("on", el.dataset.view === name));
    ["review", "items", "sources", "status"].forEach((v) => {
      const el = $("#view-" + v);
      if (el) el.classList.toggle("hidden", v !== name);
    });
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    window.scrollTo(0, 0);
    if (views[name] && views[name].show) views[name].show();
    emit("view", name);
  };
  $$("[data-view]").forEach((el) =>
    el.addEventListener("click", (e) => {
      e.preventDefault();
      showView(el.dataset.view);
    })
  );
  window.addEventListener("hashchange", () => {
    const name = (location.hash || "#review").slice(1);
    if (name !== state.view && views[name]) showView(name);
  });

  const isDesktop = () => window.matchMedia("(min-width: 1024px)").matches;
  const modalOpen = () => $$(".modal").some((m) => !m.classList.contains("hidden"));
  const typing = (e) => {
    const t = e.target;
    if (!t || !t.tagName) return false;
    const tag = t.tagName.toLowerCase();
    return (
      tag === "input" ||
      tag === "textarea" ||
      tag === "select" ||
      t.isContentEditable ||
      !!t.closest(".CodeMirror")
    );
  };

  // ---------------------------------------------------------------
  // Toast with optional Undo.
  // ---------------------------------------------------------------
  let toastTimer = null;
  let toastUndo = null;
  const toast = (text, opts = {}) => {
    const el = $("#toast");
    const undoBtn = $("#toast-undo");
    $("#toast-text").textContent = text;
    el.classList.toggle("err", !!opts.error);
    toastUndo = opts.undo || null;
    undoBtn.hidden = !toastUndo;
    el.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), opts.ms || (opts.undo ? 5000 : 2500));
  };
  $("#toast-undo").addEventListener("click", () => {
    if (toastUndo) toastUndo();
    $("#toast").classList.add("hidden");
  });

  // ---------------------------------------------------------------
  // Formatters shared by the review, config and status screens.
  // ---------------------------------------------------------------
  const fmtDur = (seconds) => {
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 90) return seconds + "s";
    if (seconds < 5400) return Math.round(seconds / 60) + "m";
    if (seconds < 172800) return (seconds / 3600).toFixed(1).replace(/\.0$/, "") + "h";
    return Math.round(seconds / 86400) + "d";
  };
  const fmtClock = (epochSeconds) => {
    const d = new Date(epochSeconds * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  };

  // The backend takes either a bare number of seconds or a human duration
  // ('45m', '2h', '1d'), parsed by convert_to_seconds. Mirrors its cases.
  const DURATION_UNITS = {
    s: 1, sec: 1, secs: 1, second: 1, seconds: 1,
    m: 60, min: 60, mins: 60, minute: 60, minutes: 60,
    h: 3600, hr: 3600, hrs: 3600, hour: 3600, hours: 3600,
    d: 86400, day: 86400, days: 86400,
    w: 604800, week: 604800, weeks: 604800,
  };
  const parseDuration = (value) => {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number") return Number.isFinite(value) ? Math.round(value) : null;
    const text = String(value).trim().toLowerCase();
    if (/^\d+$/.test(text)) return parseInt(text, 10);
    const re = /(\d+(?:\.\d+)?)\s*([a-z]+)/g;
    let total = 0;
    let matched = false;
    let m;
    while ((m = re.exec(text)) !== null) {
      const unit = DURATION_UNITS[m[2]];
      if (unit === undefined) return null;
      total += parseFloat(m[1]) * unit;
      matched = true;
    }
    return matched ? Math.round(total) : null;
  };
  // Compact and exact -- '2h', not '1.9h'. Only whole units collapse.
  const fmtCadence = (seconds) => {
    if (seconds === null || seconds === undefined) return "?";
    if (seconds >= 86400 && seconds % 86400 === 0) return seconds / 86400 + "d";
    if (seconds >= 3600 && seconds % 3600 === 0) return seconds / 3600 + "h";
    if (seconds >= 60 && seconds % 60 === 0) return seconds / 60 + "m";
    return seconds + "s";
  };

  // /api/monitor/state reports only cooldowns that have not expired, but the
  // payload can be up to ten seconds stale, so re-check `until` here too.
  const activeBlocks = (info, now) => {
    const blocked = (info && info.blocked) || {};
    const t = now === undefined || now === null ? Date.now() / 1000 : now;
    return Object.keys(blocked)
      .map((k) => blocked[k])
      .filter((b) => b && (b.until || 0) > t)
      .sort((a, b) => (b.until || 0) - (a.until || 0));
  };
  const blockChipLabel = (blk) =>
    (blk.marketplace || "marketplace") + ": blocked · retry " + fmtClock(blk.until || 0);

  const nextJob = () => {
    const jobs = (state.monitorInfo && state.monitorInfo.jobs) || [];
    return jobs.find((j) => j.next_run) || null;
  };

  // ---------------------------------------------------------------
  // Monitor state: one poller feeds every header chip, the pause button and
  // whichever screen is listening.
  // ---------------------------------------------------------------
  const setChips = (cls, text, title) => {
    $$("[data-monitor-chip]").forEach((chip) => {
      chip.className = chip.className.replace(/\b(ok|warn|err|dim)\b/g, "").trim();
      chip.classList.add(cls);
      chip.innerHTML = "<i></i>" + esc(text);
      chip.title = title || "";
    });
  };

  const renderMonitorStatus = () => {
    const info = state.monitorInfo;
    if (state.wsConnected && info && info.available) {
      const act = info.activity || {};
      // A block outranks everything else the chip could say: while it holds,
      // no item on that marketplace is searched at all.
      const blocks = activeBlocks(info);
      if (blocks.length) {
        setChips(
          "err",
          "⛔ " + blocks.map(blockChipLabel).join(" · "),
          blocks.map((b) => b.marketplace + " blocked: " + (b.reason || "")).join("\n") +
            "\n\nSearches on it are skipped until the cooldown ends. Clear it from the Status page if the block has already lifted."
        );
        return;
      }
      if (info.paused) {
        setChips("warn", "⏸ paused", "Scheduled searches are paused. Press Resume to continue.");
      } else if (act.state === "searching") {
        setChips("ok", "searching " + (act.item || ""), "A search is running now.");
      } else {
        const nj = nextJob();
        const eta = nj
          ? " · next " + nj.item + " in " + fmtDur(new Date(nj.next_run).getTime() / 1000 - Date.now() / 1000)
          : "";
        setChips("ok", "idle" + eta, "Monitor is idle between scheduled searches.");
      }
      return;
    }
    if (!state.wsConnected) {
      setChips("err", "disconnected", "The aimm process may have stopped. Reconnecting…");
    } else if (!state.lastActivity) {
      setChips("warn", "connected", "Connected, waiting for the first log message.");
    } else {
      const ago = Math.round(Date.now() / 1000 - state.lastActivity);
      if (ago > 300) setChips("warn", "idle · " + fmtDur(ago) + " ago", "Connected but no activity for 5+ minutes.");
      else setChips("ok", "running · " + fmtDur(ago) + " ago", "Process is alive and active.");
    }
  };
  setInterval(renderMonitorStatus, 1000);

  const renderPauseBtn = () => {
    const info = state.monitorInfo;
    const btn = $("#pause-btn");
    btn.hidden = !(info && info.available);
    if (info && info.available) {
      btn.textContent = info.paused ? "Resume" : "Pause";
      btn.title = info.paused ? "Resume scheduled searches" : "Pause scheduled searches (schedule keeps ticking)";
    }
  };

  const loadMonitorState = async () => {
    try {
      const res = await api("/api/monitor/state");
      if (!res.ok) return;
      state.monitorInfo = await res.json();
      renderMonitorStatus();
      renderPauseBtn();
      emit("monitor", state.monitorInfo);
    } catch (err) {
      /* transient; next poll retries */
    }
  };

  const loadEnvStatus = async () => {
    try {
      const res = await api("/api/env-status");
      if (res.ok) {
        state.envVars = (await res.json()).vars || {};
        emit("env", state.envVars);
      }
    } catch (err) {
      /* ignore */
    }
  };

  const togglePause = async () => {
    const paused = state.monitorInfo && state.monitorInfo.paused;
    try {
      const res = await api("/api/monitor/" + (paused ? "resume" : "pause"), { method: "POST" });
      if (res.ok) {
        toast(paused ? "Searches resumed" : "Searches paused");
        await loadMonitorState();
      } else toast("Could not " + (paused ? "resume" : "pause"), { error: true });
    } catch (err) {
      console.error(err);
    }
  };
  $("#pause-btn").addEventListener("click", togglePause);

  // Soft-restart: touching the config wakes the monitor and runs every item.
  const searchNow = async () => {
    const btn = $("#restart-btn");
    btn.disabled = true;
    try {
      const res = await api("/api/monitor/restart", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.ok) toast("Waking monitor — searching all items now");
      else toast("Search now failed: " + (data.detail || "unknown"), { error: true });
    } catch (err) {
      toast("Search now failed: " + err.message, { error: true });
    } finally {
      setTimeout(() => {
        btn.disabled = false;
      }, 2000);
    }
  };
  $("#restart-btn").addEventListener("click", searchNow);

  const clearBlock = async (marketplace) => {
    try {
      const res = await api("/api/monitor/clear-block", {
        method: "POST",
        body: JSON.stringify({ marketplace }),
      });
      if (res.ok) {
        toast("Block cleared — " + marketplace + " will be searched on the next run");
        await loadMonitorState();
      }
    } catch (err) {
      console.error(err);
    }
  };

  const exportCsv = async () => {
    const btn = $("#export-csv-btn");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/found.csv");
      if (!res.ok) {
        toast("Export failed: " + res.status, { error: true });
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const stamp = new Date()
        .toISOString()
        .slice(0, 19)
        .replace(/[-:T]/g, "")
        .replace(/(\d{8})(\d{6})/, "$1-$2");
      const a = document.createElement("a");
      a.href = url;
      a.download = `found-items-${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast("Export failed: " + err.message, { error: true });
    } finally {
      if (btn) btn.disabled = false;
    }
  };
  const csvBtn = $("#export-csv-btn");
  if (csvBtn) csvBtn.addEventListener("click", exportCsv);

  // ---------------------------------------------------------------
  // Log stream (WebSocket). Records are kept here; the Status screen renders
  // them and the review screen reloads listings when a rating lands.
  // ---------------------------------------------------------------
  const noteActivity = (record) => {
    state.lastActivity = record.time;
    if (record.level === "ERROR" || record.level === "CRITICAL") state.errorCount++;
  };

  const loadLogs = async () => {
    const res = await (await api("/api/logs?limit=500")).json();
    state.records = res.records || [];
    state.records.forEach(noteActivity);
    emit("logs");
    renderMonitorStatus();
  };

  const setWsStatus = (text) => {
    const el = $("#ws-status");
    if (el) el.textContent = "· " + text;
  };

  const connectWs = () => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    state.ws = ws;
    ws.onopen = () => {
      state.wsConnected = true;
      setWsStatus("streaming");
      renderMonitorStatus();
      emit("ws", true);
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "log") {
        state.records.push(msg.record);
        noteActivity(msg.record);
        if (state.records.length > 5000) state.records.shift();
        emit("log", msg.record);
        renderMonitorStatus();
      }
    };
    ws.onclose = () => {
      state.wsConnected = false;
      setWsStatus("disconnected — retrying…");
      renderMonitorStatus();
      emit("ws", false);
      setTimeout(connectWs, 2000);
    };
    ws.onerror = () => ws.close();
  };

  // ---------------------------------------------------------------
  // Global keys: "?" opens the cheatsheet anywhere, Esc closes modals.
  // Screen-specific keys live in their modules.
  // ---------------------------------------------------------------
  const keysModal = $("#keys-modal");
  const openKeys = () => keysModal.classList.remove("hidden");
  const closeKeys = () => keysModal.classList.add("hidden");
  $("#keys-close").addEventListener("click", closeKeys);
  $("#keys-modal .modal-backdrop").addEventListener("click", closeKeys);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !keysModal.classList.contains("hidden")) {
      closeKeys();
      return;
    }
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "?" && !modalOpen()) {
      e.preventDefault();
      openKeys();
    }
  });

  // ---------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------
  const boots = [];
  const bootstrap = async () => {
    if (state.booted) return;
    state.booted = true;
    try {
      const res = await fetch("/api/status", { credentials: "same-origin" });
      if (res.ok) {
        state.status = await res.json();
        $("#browser-btn").hidden = !(state.status && state.status.vnc_enabled);
        if (Array.isArray(state.status.marketplaces) && state.status.marketplaces.length) {
          state.supportedMarketplaces = state.status.marketplaces;
        }
      }
    } catch (_) {
      /* keep defaults */
    }
    for (const fn of boots) {
      try {
        await fn();
      } catch (err) {
        console.error("boot step failed", err);
      }
    }
    try {
      await loadLogs();
    } catch (err) {
      console.error(err);
    }
    connectWs();
    loadMonitorState();
    loadEnvStatus();
    if (!state._monitorPoll) state._monitorPoll = setInterval(loadMonitorState, 10000);
    const initial = (location.hash || "#review").slice(1);
    showView(views[initial] ? initial : "review");
  };

  window.AIMM = {
    $, $$, esc, state, api, on, emit, views, showView, isDesktop, modalOpen, typing, toast,
    fmtDur, fmtClock, parseDuration, fmtCadence, activeBlocks, blockChipLabel, nextJob,
    loadMonitorState, loadEnvStatus, renderMonitorStatus, togglePause, searchNow, clearBlock,
    exportCsv, logout, boot: (fn) => boots.push(fn), openKeys,
  };
  // Pure helpers the QA harness asserts directly (a real block is not
  // something to provoke for a test).
  window.__aimm = Object.assign(window.__aimm || {}, {
    parseDuration, fmtCadence, activeBlocks, blockChipLabel,
  });

  // If we already have a session cookie from a prior visit, try bootstrapping.
  document.addEventListener("DOMContentLoaded", async () => {
    try {
      const res = await fetch("/api/status", { credentials: "same-origin" });
      if (res.ok) {
        state.csrf = getCookie("aimm_csrf");
        hideLogin();
        await bootstrap();
      } else {
        showLogin();
      }
    } catch (err) {
      showLogin();
    }
  });
})();
