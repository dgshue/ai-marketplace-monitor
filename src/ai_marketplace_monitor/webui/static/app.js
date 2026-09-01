// AI Marketplace Monitor — Web UI frontend.
// Vanilla JS, no build step. Provides:
//   - Login form + session cookie handling
//   - TOML editor with line numbers and syntax highlighting (lightweight)
//   - Live log tail via WebSocket with level/text filtering + expand
//   - Save / Validate with inline error at the offending line

(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    csrf: null,
    fileId: "primary",
    baseMtime: null,
    originalContent: "",
    currentContent: "",
    logLevel: "ALL",
    logKind: "",
    logItem: "",
    logMinScore: null,
    logFilter: "",
    ws: null,
    records: [],
    expanded: new Set(),
    knownItems: new Set(),
    lastActivity: null, // epoch seconds of the most recent log record
    monitorState: "disconnected", // "connected" | "idle" | "disconnected"
    wsConnected: false,
    errorCount: 0, // unread ERROR-level messages (for tab badge)
    monitorInfo: null, // last /api/monitor/state payload
    supportedMarketplaces: ["facebook", "ebay", "depop", "poshmark"], // refreshed from /api/status
    envVars: null, // last /api/env-status payload
    appView: "deals",
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

  // ---------------------------------------------------------------
  // Login flow
  // ---------------------------------------------------------------
  const showLogin = async () => {
    $("#login-screen").classList.remove("hidden");
    $("#app").classList.add("hidden");
    // Fetch the auth mode so we can decide between login form and open mode.
    try {
      const info = await (await fetch("/api/auth/info", { credentials: "same-origin" })).json();
      if (info.open) {
        // Open mode — no credentials configured, auto-login as anonymous.
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
      // Proxy-only mode: authentication happens at the reverse proxy and
      // there is no password here to accept, so a form would be a dead end.
      const subtitle = $("#login-subtitle");
      if (info.proxy_auth && !info.password_login) {
        $("#login-fields").hidden = true;
        subtitle.textContent =
          "Sign-in happens at your identity provider. Open this app through " +
          "its SSO address (the Traefik hostname) — direct access has no " +
          "password to accept.";
        subtitle.hidden = false;
        return;
      }
      // Authenticated mode — show sign-in form.
      const form = $("#login-form");
      subtitle.textContent =
        "Sign in with the marketplace credentials from your config.";
      subtitle.hidden = false;
      $("#login-submit").textContent = "Sign in";
      if (info.username_hint) form.username.value = info.username_hint;
    } catch (err) {
      // fall back to generic login form
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

  $("#logout-btn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    if (state.ws) state.ws.close();
    state.csrf = null;
    showLogin();
  });

  // ---------------------------------------------------------------
  // Editor — CodeMirror 5 with TOML syntax highlighting
  // ---------------------------------------------------------------
  const editorHost = $("#editor-host");

  // Thin wrapper so the rest of the code uses editor.getValue() / editor.setValue()
  // regardless of whether CodeMirror loaded successfully.
  let editor;
  let validateTimer = null;
  const onEditorChange = () => {
    state.currentContent = editor.getValue();
    const dirty = state.currentContent !== state.originalContent;
    $("#save-btn").disabled = !dirty;
    if (validateTimer) clearTimeout(validateTimer);
    if (dirty) {
      setEditorStatus("typing…");
      validateTimer = setTimeout(() => {
        validateTimer = null;
        validateConfig();
      }, 400);
    }
  };

  if (window.CodeMirror) {
    editor = CodeMirror(editorHost, {
      mode: "toml",
      theme: "default",
      lineNumbers: true,
      indentUnit: 2,
      tabSize: 2,
      indentWithTabs: false,
      lineWrapping: false,
      extraKeys: {
        "Cmd-S": () => saveConfig(),
        "Ctrl-S": () => saveConfig(),
        Tab: (cm) => cm.replaceSelection("  ", "end"),
      },
    });
    editor.on("change", onEditorChange);
    // Expose a uniform API.
    editor.getValue = editor.getValue.bind(editor);
    editor.setValue = editor.setValue.bind(editor);
    editor.getScrollInfo = editor.getScrollInfo.bind(editor);
  } else {
    // Fallback: plain textarea if CodeMirror failed to load.
    const textarea = document.createElement("textarea");
    textarea.className = "aimm-editor";
    textarea.spellcheck = false;
    editorHost.appendChild(textarea);
    editor = {
      getValue: () => textarea.value,
      setValue: (v) => { textarea.value = v; },
      getScrollInfo: () => ({ top: textarea.scrollTop }),
      on: () => {},
      refresh: () => {},
    };
    textarea.addEventListener("input", onEditorChange);
    textarea.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        saveConfig();
      }
    });
  }

  // ---------------------------------------------------------------
  // Config load / save / validate
  // ---------------------------------------------------------------
  const setEditorStatus = (msg, cls = "") => {
    const el = $("#editor-status");
    el.className = "editor-status " + cls;
    el.textContent = msg;
  };

  const loadConfig = async () => {
    const files = await (await api("/api/config/files")).json();
    if (!files.files.length) return;
    const f = files.files[0];
    state.fileId = f.id;
    $("#config-name").textContent = f.path;
    $("#mtime").textContent = "mtime " + new Date(f.mtime * 1000).toLocaleString();

    const res = await (await api(`/api/config/file/${f.id}`)).json();
    state.originalContent = res.content;
    state.currentContent = res.content;
    state.baseMtime = res.mtime;
    editor.setValue(res.content);
    // Prefer the server-provided sections list, but fall back to a
    // client-side scan if the server didn't include one (e.g. user is
    // running an older aimm that hasn't been restarted yet).
    if (Array.isArray(res.sections) && res.sections.length) {
      state.sections = res.sections;
    } else {
      state.sections = scanSectionsClient(res.content);
    }
    renderGutter();
    $("#save-btn").disabled = true;
    if (res.has_masked_secrets) {
      setEditorStatus(
        `🔒 Secrets masked as "${res.mask_token}" — leave them alone to preserve, or type over to replace.`,
        "ok"
      );
    } else {
      setEditorStatus("");
    }
  };

  const validateConfig = async () => {
    setEditorStatus("validating…");
    try {
      const res = await api("/api/config/validate", {
        method: "POST",
        body: JSON.stringify({ content: state.currentContent }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setEditorStatus(
          "✗ " + (data.detail || `HTTP ${res.status}`),
          "err"
        );
        return false;
      }
      if (data.valid) {
        setEditorStatus("✓ config is valid", "ok");
        return true;
      }
      setEditorStatus("✗ " + (data.error || "invalid"), "err");
      return false;
    } catch (err) {
      console.error("validate failed", err);
      setEditorStatus("✗ validate failed: " + err.message, "err");
      return false;
    }
  };

  const saveConfig = async () => {
    // If a debounced validate is pending, cancel it — the server will
    // re-validate on PUT anyway.
    if (validateTimer) {
      clearTimeout(validateTimer);
      validateTimer = null;
    }
    setEditorStatus("saving…");
    let res, data;
    try {
      res = await api(`/api/config/file/${state.fileId}`, {
        method: "PUT",
        body: JSON.stringify({
          content: state.currentContent,
          base_mtime: state.baseMtime,
        }),
      });
      data = await res.json().catch(() => ({}));
    } catch (err) {
      console.error("save failed", err);
      setEditorStatus("✗ save failed: " + err.message, "err");
      return;
    }
    if (!res.ok || !data.ok) {
      setEditorStatus(
        "✗ " + (data.error || data.detail || `HTTP ${res.status}`),
        "err"
      );
      if (res.status === 409) {
        if (confirm("Config was modified on disk. Reload from disk and lose your changes?")) {
          await loadConfig();
        }
      }
      return;
    }
    state.originalContent = state.currentContent;
    state.baseMtime = data.mtime;
    $("#save-btn").disabled = true;
    setEditorStatus("✓ saved — monitor will reload within 1s", "ok");
    $("#mtime").textContent = "mtime " + new Date(data.mtime * 1000).toLocaleString();
  };

  $("#save-btn").addEventListener("click", saveConfig);

  // ---------------------------------------------------------------
  // Logs
  // ---------------------------------------------------------------
  const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

  const matchesLevel = (record) => {
    if (state.logLevel === "ALL") return true;
    return LEVEL_ORDER[record.level] >= LEVEL_ORDER[state.logLevel];
  };
  const matchesFilter = (record) => {
    if (!state.logFilter) return true;
    return record.message.toLowerCase().includes(state.logFilter.toLowerCase());
  };
  const matchesKind = (record) => {
    if (!state.logKind) return true;
    return record.extra && record.extra.kind === state.logKind;
  };
  const matchesItem = (record) => {
    if (!state.logItem) return true;
    return record.extra && record.extra.item === state.logItem;
  };
  const matchesScore = (record) => {
    if (state.logMinScore == null) return true;
    const score = record.extra && record.extra.score;
    return typeof score === "number" && score >= state.logMinScore;
  };

  const updateItemDropdown = (record) => {
    const item = record.extra && record.extra.item;
    if (!item || state.knownItems.has(item)) return;
    state.knownItems.add(item);
    const opt = document.createElement("option");
    opt.value = item;
    opt.textContent = item;
    $("#item-filter").appendChild(opt);
  };

  const renderDetail = (record) => {
    const lines = [];
    lines.push(
      `<dl><dt>logger</dt><dd>${esc(record.logger)}</dd>` +
        `<dt>source</dt><dd>${esc(record.location)}</dd></dl>`
    );
    if (record.extra) {
      const extra = record.extra;
      const rows = Object.entries(extra)
        .map(([k, v]) => {
          if (k === "url" && typeof v === "string") {
            return `<dt>${esc(k)}</dt><dd><a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a></dd>`;
          }
          return `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : String(v))}</dd>`;
        })
        .join("");
      lines.push(`<dl>${rows}</dl>`);
    }
    if (record.exc_text) {
      lines.push(`<pre>${esc(record.exc_text)}</pre>`);
    }
    return `<div class="log-detail">${lines.join("")}</div>`;
  };

  const renderLogs = () => {
    const container = $("#logs");
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 16;
    const visible = state.records.filter(
      (r) =>
        matchesLevel(r) &&
        matchesFilter(r) &&
        matchesKind(r) &&
        matchesItem(r) &&
        matchesScore(r)
    );
    container.innerHTML = visible
      .map((r) => {
        const expanded = state.expanded.has(r.id);
        const kind = r.extra && r.extra.kind;
        const badge = kind
          ? `<span class="kind-badge kind-${esc(kind)}">${esc(kind.replace(/_/g, " "))}</span>`
          : "";
        return (
          `<div class="log-row level-${esc(r.level)}${expanded ? " expanded" : ""}" data-id="${r.id}">` +
          `<span class="log-time">${esc(r.iso_time)}</span>` +
          `<span class="log-level">${esc(r.level)}</span>` +
          `<span class="log-msg">${badge}${esc(r.message)}</span>` +
          (expanded ? renderDetail(r) : "") +
          `</div>`
        );
      })
      .join("");
    if ($("#autoscroll").checked && (atBottom || state.records.length < 20)) {
      container.scrollTop = container.scrollHeight;
    }
  };

  const esc = (s) =>
    String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  $("#logs").addEventListener("click", (e) => {
    const row = e.target.closest(".log-row");
    if (!row) return;
    const id = Number(row.dataset.id);
    if (state.expanded.has(id)) state.expanded.delete(id);
    else state.expanded.add(id);
    renderLogs();
  });

  $$(".level-chips .chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".level-chips .chip").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.logLevel = btn.dataset.level;
      // Clear error badge when user views errors.
      if (btn.dataset.level === "ERROR" || btn.dataset.level === "ALL") {
        state.errorCount = 0;
        renderErrorBadge();
      }
      renderLogs();
    });
  });

  $$(".kind-chips .chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".kind-chips .chip").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.logKind = btn.dataset.kind;
      renderLogs();
    });
  });

  $("#item-filter").addEventListener("change", (e) => {
    state.logItem = e.target.value;
    renderLogs();
  });

  $("#score-filter").addEventListener("change", (e) => {
    const v = e.target.value;
    state.logMinScore = v === "" ? null : Number(v);
    renderLogs();
  });

  $("#log-filter").addEventListener("input", (e) => {
    state.logFilter = e.target.value;
    renderLogs();
  });

  const loadLogs = async () => {
    const res = await (await api("/api/logs?limit=500")).json();
    state.records = res.records;
    state.records.forEach((r) => {
      updateItemDropdown(r);
      noteActivity(r);
    });
    renderLogs();
    renderMonitorStatus();
  };

  // -------- Monitor status chip derived from the log stream --------
  // Track activity timestamp from any log record.
  const noteActivity = (record) => {
    state.lastActivity = record.time;
    // Track error count for the Error tab badge.
    if (record.level === "ERROR" || record.level === "CRITICAL") {
      state.errorCount++;
      renderErrorBadge();
    }
  };

  const formatAgo = (epoch) => {
    if (!epoch) return "—";
    const s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    return `${Math.round(s / 3600)}h ago`;
  };

  // Monitor status is purely about process liveness — driven by
  // WebSocket connection state, not log message content.
  const renderMonitorStatus = () => {
    const chip = $("#monitor-status");
    if (!chip) return;
    // Polled monitor state beats log-derived inference whenever available:
    // it reports what the monitor IS doing, not what it last said.
    const info = state.monitorInfo;
    if (state.wsConnected && info && info.available) {
      const act = info.activity || {};
      // A block outranks everything else the chip could say: while it holds,
      // no item on that marketplace is searched at all.
      const blocks = activeBlocks(info);
      if (blocks.length) {
        chip.className = "status-chip status-err";
        chip.textContent = "⛔ " + blocks.map(blockChipLabel).join(" · ");
        chip.title =
          blocks.map((b) => b.marketplace + " blocked: " + (b.reason || "")).join("\n") +
          "\n\nSearches on it are skipped until the cooldown ends. Clear it from the Status page if the block has already lifted.";
        return;
      }
      if (info.paused) {
        chip.className = "status-chip status-warn";
        chip.textContent = "⏸ paused";
        chip.title = "Scheduled searches are paused. Press Resume to continue.";
      } else if (act.state === "searching") {
        chip.className = "status-chip status-ok";
        chip.textContent = "● searching " + (act.item || "");
        chip.title = "A search is running now.";
      } else {
        const jobs = info.jobs || [];
        const nj = jobs.find((j) => j.next_run);
        const eta = nj
          ? " · next " + nj.item + " in " +
            fmtDur(new Date(nj.next_run).getTime() / 1000 - Date.now() / 1000)
          : "";
        chip.className = "status-chip status-ok";
        chip.textContent = "● idle" + eta;
        chip.title = "Monitor is idle between scheduled searches.";
      }
      return;
    }
    if (!state.wsConnected) {
      chip.className = "status-chip status-err";
      chip.textContent = "● monitor: disconnected";
      chip.title = "The aimm process may have stopped. Reconnecting…";
    } else if (!state.lastActivity) {
      chip.className = "status-chip status-warn";
      chip.textContent = "● monitor: connected";
      chip.title = "Connected, waiting for first log message.";
    } else {
      const ago = Math.round(Date.now() / 1000 - state.lastActivity);
      if (ago > 300) {
        chip.className = "status-chip status-warn";
        chip.textContent = `● monitor: idle · ${formatAgo(state.lastActivity)}`;
        chip.title = "Connected but no activity for 5+ minutes.";
      } else {
        chip.className = "status-chip status-ok";
        chip.textContent = `● monitor: running · ${formatAgo(state.lastActivity)}`;
        chip.title = "Process is alive and active.";
      }
    }
  };

  // Error badge on the "Error" filter chip in the logs toolbar.
  const renderErrorBadge = () => {
    const errorChip = document.querySelector('.level-chips [data-level="ERROR"]');
    if (!errorChip) return;
    if (state.errorCount > 0) {
      errorChip.dataset.badge = state.errorCount;
      errorChip.classList.add("has-badge");
    } else {
      delete errorChip.dataset.badge;
      errorChip.classList.remove("has-badge");
    }
  };

  // Tick the "Xs ago" display once a second so it stays fresh even
  // without new log records.
  setInterval(renderMonitorStatus, 1000);

  // Restart button — soft-restarts the monitor by touching the config.
  const wireClick = (sel, fn) => {
    const el = $(sel);
    if (el) el.addEventListener("click", fn);
    else console.warn("missing element:", sel);
  };
  wireClick("#restart-btn", async () => {
    const btn = $("#restart-btn");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/monitor/restart", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setEditorStatus("▶ Waking monitor — searching all items now…", "ok");
      } else {
        setEditorStatus("▶ Failed: " + (data.detail || "unknown"), "err");
      }
    } catch (err) {
      setEditorStatus("↻ Restart failed: " + err.message, "err");
    } finally {
      setTimeout(() => { if (btn) btn.disabled = false; }, 2000);
    }
  });

  wireClick("#export-csv-btn", async () => {
    const btn = $("#export-csv-btn");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/found.csv");
      if (!res.ok) {
        setEditorStatus("⬇ Export failed: " + res.status, "err");
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
      setEditorStatus("⬇ Export failed: " + err.message, "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // ---------------------------------------------------------------
  // Sections sidebar (AI-assisted edit / delete / add)
  // ---------------------------------------------------------------
  //
  // The backend ships a list of section headers found in the file.
  // We render them in a sidebar with a ⋯ menu per section. Clicking
  // the section name scrolls the textarea to that section. No pixel
  // measurement of the textarea is needed.

  state.sections = [];

  const SECTION_HEADER_RE = /^\s*\[([^\]\n]+)\]\s*$/;

  const scanSectionsClient = (text) => {
    const lines = text.split("\n");
    const headers = [];
    for (let i = 0; i < lines.length; i++) {
      const m = lines[i].match(SECTION_HEADER_RE);
      if (m) headers.push({ lineIdx: i, name: m[1].trim() });
    }
    return headers.map((h, i) => {
      const dot = h.name.indexOf(".");
      const lineEnd = i + 1 < headers.length ? headers[i + 1].lineIdx : lines.length;
      return {
        name: h.name,
        prefix: dot >= 0 ? h.name.slice(0, dot) : h.name,
        suffix: dot >= 0 ? h.name.slice(dot + 1) : "",
        line_start: h.lineIdx,
        line_end: lineEnd,
      };
    });
  };

  // -------- Thin gutter with ⋯ buttons aligned to section headers --------

  const getLineMetrics = () => {
    if (editor.defaultTextHeight) {
      // CodeMirror path
      const lineHeight = editor.defaultTextHeight();
      const scrollInfo = editor.getScrollInfo();
      return { lineHeight, paddingTop: 0, scrollHeight: scrollInfo.height };
    }
    // Fallback textarea path
    const el = editorHost.querySelector("textarea");
    if (!el) return { lineHeight: 20, paddingTop: 0, scrollHeight: 0 };
    const cs = window.getComputedStyle(el);
    const lineHeight = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.55;
    const paddingTop = parseFloat(cs.paddingTop) || 0;
    return { lineHeight, paddingTop, scrollHeight: el.scrollHeight };
  };

  const renderGutter = () => {
    const inner = $("#gutter-inner");
    if (!inner) return;
    inner.innerHTML = "";
    const { lineHeight, paddingTop, scrollHeight } = getLineMetrics();
    // Match gutter height to editor scroll height so the transform
    // range is correct.
    inner.style.height = scrollHeight + "px";

    state.sections.forEach((section) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "section-btn";
      btn.innerHTML = "⋯";
      btn.title = `[${section.name}]`;
      btn.style.top = (paddingTop + lineHeight * section.line_start) + "px";
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleSectionMenu(section, btn);
      });
      inner.appendChild(btn);
    });
  };

  // Sync gutter scroll position with the editor.
  const syncGutter = () => {
    const inner = $("#gutter-inner");
    if (inner) inner.style.transform = `translateY(${-editor.getScrollInfo().top}px)`;
  };
  if (editor.on) {
    editor.on("scroll", syncGutter);
    editor.on("change", () => {
      if (refreshSectionsFromBuffer._t) clearTimeout(refreshSectionsFromBuffer._t);
      refreshSectionsFromBuffer._t = setTimeout(refreshSectionsFromBuffer, 150);
    });
  }

  // Re-scan sections from the buffer after edits (debounced).
  const refreshSectionsFromBuffer = () => {
    state.sections = scanSectionsClient(editor.getValue());
    renderGutter();
    // Keep the form tab in step with edits made in the TOML tab, so switching
    // back never shows a stale card. Called through state because the renderer
    // is declared further down: a bare `typeof renderConfigForm` would still
    // throw here, since typeof does not shield a const in its dead zone.
    if (state.renderConfigForm) state.renderConfigForm();
  };

  // -------- Popover menu (Edit / Delete / Add another) --------

  const closeSectionMenus = () => {
    document.querySelectorAll(".section-menu").forEach((m) => m.remove());
  };

  const toggleSectionMenu = (section, btn) => {
    const existing = document.querySelector(".section-menu");
    if (existing && existing.dataset.section === section.name) {
      existing.remove();
      return;
    }
    closeSectionMenus();

    const menu = document.createElement("div");
    menu.className = "section-menu";
    menu.dataset.section = section.name;

    // Position relative to the button using viewport coordinates
    // (position: fixed in CSS) so the menu can escape the sidebar's
    // overflow clip.
    const rect = btn.getBoundingClientRect();
    menu.style.top = rect.bottom + 4 + "px";
    menu.style.left = rect.left + "px";

    const addMenuItem = (label, handler, cls = "") => {
      const item = document.createElement("button");
      item.type = "button";
      item.textContent = label;
      if (cls) item.className = cls;
      item.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        closeSectionMenus();
        handler();
      });
      menu.appendChild(item);
    };

    addMenuItem("Edit", () => openEditSectionModal(section.name));
    addMenuItem("Duplicate", () => duplicateSection(section));
    const sep = document.createElement("div");
    sep.className = "menu-sep";
    menu.appendChild(sep);
    addMenuItem("Delete", () => deleteSection(section), "danger");

    document.body.appendChild(menu);
  };

  // Close popovers when clicking anywhere else.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".section-btn") && !e.target.closest(".section-menu")) {
      closeSectionMenus();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSectionMenus();
  });

  // -------- Delete section (pure client-side string op) --------

  const deleteSection = (section) => {
    const lines = editor.getValue().split("\n");
    let start = section.line_start;
    let end = section.line_end; // exclusive

    const preview = lines.slice(start, end).filter((l) => l.trim()).join("\n");
    const ok = confirm(
      `Delete [${section.name}] ?\n\n` +
        preview +
        "\n\nThis only updates the editor buffer — click Save to commit."
    );
    if (!ok) return;

    // If there's a blank line right before this section, consume it
    // too so we don't leave a double blank.
    if (start > 0 && lines[start - 1].trim() === "") {
      start--;
    }
    const next = lines.slice(0, start).concat(lines.slice(end)).join("\n");
    editor.setValue(next);
    state.currentContent = next;
    const dirty = state.currentContent !== state.originalContent;
    $("#save-btn").disabled = !dirty;
    setEditorStatus(
      `Deleted [${section.name}] — review and click Save to commit.`,
      "ok"
    );
    refreshSectionsFromBuffer();
  };

  // -------- Duplicate section --------
  // Opens the Add form pre-populated with the original section's values
  // and a unique auto-generated name.

  const duplicateSection = (section) => {
    const schema = findFormSchema(section.name);
    if (!schema) {
      alert(`No form defined for [${section.name}] — duplicate manually in the TOML editor.`);
      return;
    }

    // Get the original fields (server-provided or client-parsed).
    let fields = (section && section.fields) || {};
    if (!Object.keys(fields).length && window.tomlEdit) {
      try {
        const parsed = window.tomlEdit.parse(state.currentContent);
        const parts = section.name.split(".");
        let node = parsed;
        for (const p of parts) { node = node && node[p]; }
        if (node && typeof node === "object") fields = node;
      } catch (err) { /* fall through with empty fields */ }
    }

    // Generate a unique suffix: append 1, 2, 3…
    const existingNames = new Set(state.sections.map((s) => s.name));
    let newSuffix = section.suffix;
    let i = 1;
    while (existingNames.has(`${section.prefix}.${newSuffix}`)) {
      newSuffix = section.suffix + i;
      i++;
    }

    formContext = {
      sectionName: `${section.prefix}.__new__`,
      fields,
      schema,
      addMode: true,
      addPrefix: section.prefix,
      nameValue: newSuffix,
    };
    activeTab = "left";
    $("#form-modal-title").textContent = `Duplicate [${section.name}]`;
    $("#form-modal-hint").hidden = false;
    $("#form-modal-hint").textContent =
      "Review the values copied from the original section, change what you need, and save.";
    renderForm(schema, fields);
    formModal.open();
    setTimeout(() => $("#add-section-name").focus(), 50);
  };

  // ---------------------------------------------------------------
  // Section form modal (placeholder — full form rendering coming next)
  // ---------------------------------------------------------------
  //
  // The ⋯ menu wires Edit / Add another to the two functions below.
  // For now they pop a minimal placeholder modal so the page doesn't
  // crash. The form-rendering engine that uses toml-edit-js will be
  // wired here in the next iteration.

  // ---------------------------------------------------------------
  // Form-based section editor using toml-edit-js
  // ---------------------------------------------------------------

  // Form field schema definitions per section type. Each field:
  //   key       — TOML key name
  //   label     — display name
  //   type      — "text" | "password" | "number" | "select" | "textarea"
  //   options   — for select: [{value, label}, ...]
  //   required  — boolean
  //   help      — tooltip / small hint
  //   group     — optional group header (for visual grouping)
  //   advanced  — if true, hidden by default

  const BUILT_IN_REGIONS = [
    "usa", "usa_full", "can", "mex", "bra", "arg",
    "aus", "aus_miles", "nzl", "ind", "gbr", "fra", "spa",
  ];

  // override: true means this field can also be set per-item in [item.*]
  // to override the marketplace default.
  const OV = "Can be overridden per-item in [item.*] sections.";

  const CATEGORIES = [
    { value: "", label: "(any)" },
    { value: "vehicles", label: "Vehicles" },
    { value: "propertyrentals", label: "Property rentals" },
    { value: "apparel", label: "Apparel" },
    { value: "electronics", label: "Electronics" },
    { value: "entertainment", label: "Entertainment" },
    { value: "family", label: "Family" },
    { value: "freestuff", label: "Free stuff" },
    { value: "free", label: "Free" },
    { value: "garden", label: "Garden" },
    { value: "hobbies", label: "Hobbies" },
    { value: "homegoods", label: "Home goods" },
    { value: "homeimprovement", label: "Home improvement" },
    { value: "homesales", label: "Home sales" },
    { value: "musicalinstruments", label: "Musical instruments" },
    { value: "officesupplies", label: "Office supplies" },
    { value: "petsupplies", label: "Pet supplies" },
    { value: "sportinggoods", label: "Sporting goods" },
    { value: "tickets", label: "Tickets" },
    { value: "toys", label: "Toys" },
    { value: "videogames", label: "Video games" },
  ];

  const FORM_SCHEMAS = {
    "marketplace.facebook": [
      // ---- Left column: Facebook-specific ----
      { key: "username", label: "Facebook username (email)", type: "text", column: "left",
        help: "Your Facebook login email." },
      { key: "password", label: "Facebook password", type: "password", column: "left",
        help: "Leave blank to keep the current password." },
      { key: "login_wait_time", label: "Login wait time (seconds)", type: "number", column: "left",
        help: "Seconds to wait after Facebook login for 2FA / captcha. Default: 60." },
      { key: "language", label: "Language", type: "text", column: "left", advanced: true,
        help: "Non-English Facebook locale — must match a [translation.*] section." },

      // ---- Right column: Shared — can be overridden per-item ----
      { key: "home_location", label: "Your location (for distance & maps)", type: "text", column: "right",
        help: "Where you are, e.g. 'Asheboro, NC' or a bare 'lat, lon'. Drives the distance shown on every listing, the pickup map, and the drive-time estimate. Separate from search city, which is Facebook's own place id and does not geocode." },
      { key: "search_city", label: "Search city", type: "text", required: true, keepString: true, column: "right",
        help: "City code from the Facebook Marketplace URL (lowercase, e.g. 'houston')." },
      { key: "search_region", label: "Search region", type: "select", column: "right",
        options: [{ value: "", label: "(none)" }].concat(
          BUILT_IN_REGIONS.map((r) => ({ value: r, label: r }))
        ),
        help: "Pre-defined region (expands to multiple cities)." },

      // ---- Filters (advanced, overridable) ----
      { key: "category", label: "Category", type: "select", group: "Filters", advanced: true, column: "right",
        options: CATEGORIES, help: "Marketplace listing category." },
      { key: "condition", label: "Condition", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "new", label: "New" },
          { value: "used_like_new", label: "Used — like new" },
          { value: "used_good", label: "Used — good" },
          { value: "used_fair", label: "Used — fair" },
        ],
        help: "Filter by item condition. " + OV },
      { key: "availability", label: "Availability", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "all", label: "All" },
          { value: "in", label: "In stock" },
          { value: "out", label: "Out of stock" },
        ] },
      { key: "date_listed", label: "Date listed", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "1", label: "Last 24 hours" },
          { value: "7", label: "Last 7 days" },
          { value: "30", label: "Last 30 days" },
        ] },
      { key: "delivery_method", label: "Delivery method", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "local_pick_up", label: "Local pick-up" },
          { value: "shipping", label: "Shipping" },
        ] },
      { key: "seller_locations", label: "Seller locations", type: "text", advanced: true, column: "right",
        help: "Comma-separated location names to filter by." },
      { key: "exclude_sellers", label: "Exclude sellers", type: "text", advanced: true, column: "right",
        help: "Comma-separated seller names to skip." },
      { key: "keywords", label: "Keywords (include)", type: "text", advanced: true, column: "right",
        help: "Boolean expression, e.g. 'drone AND (DJI OR Orqa)'" },
      { key: "antikeywords", label: "Anti-keywords (exclude)", type: "text", advanced: true, column: "right",
        help: "Boolean expression for exclusion." },

      // ---- Pricing ----
      { key: "min_price", label: "Min price", type: "text", group: "Pricing", advanced: true, column: "right",
        help: "e.g. '50' or '50 USD'" },
      { key: "max_price", label: "Max price", type: "text", advanced: true, column: "right",
        help: "e.g. '300' or '300 USD'" },

      // ---- Location ----
      { key: "radius", label: "Search radius (km)", type: "text", group: "Location", advanced: true, column: "right",
        help: "Comma-separated radius per city (must match search_city count)." },
      { key: "currency", label: "Currency", type: "text", advanced: true, column: "right",
        help: "Comma-separated currency code per city, e.g. 'USD, CAD'." },

      // ---- AI evaluation ----
      { key: "ai", label: "AI backends", type: "text", group: "AI evaluation", advanced: true, column: "right",
        help: "Comma-separated [ai.*] names." },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "right",
        options: [
          { value: "", label: "Default (3)" },
          { value: "1", label: "1 — everything, no filtering" },
          { value: "2", label: "2 — potential match or better" },
          { value: "3", label: "3 — poor match or better (default)" },
          { value: "4", label: "4 — good match or better" },
          { value: "5", label: "5 — great deals only" },
        ],
        help: "Default notification threshold for every item. Items can override it." },
      { key: "prompt", label: "AI prompt", type: "textarea", advanced: true, column: "right",
        help: "Custom evaluation prompt (replaces default)." },
      { key: "extra_prompt", label: "Extra prompt", type: "textarea", advanced: true, column: "right",
        help: "Additional text appended before the rating prompt." },
      { key: "rating_prompt", label: "Rating prompt", type: "textarea", advanced: true, column: "right",
        help: "Custom rating instructions (replaces default 1–5 scale)." },

      // ---- Notification ----
      { key: "notify", label: "Notify users", type: "text", group: "Notification", advanced: true, column: "right",
        help: "Comma-separated [user.*] names. Default: all users." },

      // ---- Pacing & block safety ----
      { key: "request_delay", label: "Delay between page loads (seconds)", type: "text", group: "Pacing & block safety", column: "right",
        help: "Two numbers, e.g. '6, 15'. The monitor waits a random time in that range before every Facebook page load — a search page or a listing page. Default: 6 to 15 seconds. Lower it and blocks get likely; a fixed value (e.g. '10, 10') is worse than a range, because a metronome is the easiest bot signature there is." },
      { key: "block_cooldown", label: "Pause after a block", type: "text", column: "right",
        help: "How long to stop searching Facebook entirely once it serves a block page ('You're temporarily blocked', a checkpoint, or a bounce to login). Default: 2h, doubling for repeat blocks up to 8h. You get one notification, and the Status page offers 'Clear block' if you know the block has lifted." },

      // ---- Schedule (defaults every item inherits) ----
      { key: "search_interval", label: "Search every (minimum)", type: "text", group: "Schedule", column: "right",
        help: "Default cadence for every item that does not set its own. Duration, e.g. '30m', '1h'. Default: 30 min." },
      { key: "max_search_interval", label: "… and at most", type: "text", column: "right",
        help: "Upper bound for the random interval jitter. Default: 1 hour." },
      { key: "start_at", label: "Start at", type: "text", advanced: true, column: "right",
        help: "Comma-separated time patterns: 'HH:MM', '*:MM', '*:*:SS'." },
    ],

    // ---- Item form ----
    // Matched by prefix "item" — see the lookup logic below.
    // eBay goes through the official Browse API, so this section is credentials
    // and search scope -- there is no browser, login, or 2FA to configure.
    "marketplace.ebay": [
      { key: "client_id", label: "eBay App ID (Client ID)", type: "text", required: true, column: "left",
        help: "From an application key set at developer.ebay.com. Use ${EBAY_CLIENT_ID} to read it from the environment." },
      { key: "client_secret", label: "eBay Cert ID (Client Secret)", type: "password", required: true, column: "left",
        help: "Use ${EBAY_CLIENT_SECRET} to keep it out of the config file." },
      { key: "marketplace_id", label: "eBay site", type: "select", column: "left",
        options: [
          { value: "", label: "EBAY_US (default)" },
          { value: "EBAY_GB", label: "EBAY_GB — United Kingdom" },
          { value: "EBAY_CA", label: "EBAY_CA — Canada" },
          { value: "EBAY_DE", label: "EBAY_DE — Germany" },
          { value: "EBAY_AU", label: "EBAY_AU — Australia" },
        ] },
      { key: "delivery_country", label: "Ships to", type: "text", column: "left",
        help: "Two-letter country code, e.g. US. Excludes items that will not ship to you." },
      { key: "buying_options", label: "Buying options", type: "checkboxes", column: "left",
        options: [
          { value: "FIXED_PRICE", label: "Buy It Now" },
          { value: "AUCTION", label: "Auction" },
          { value: "BEST_OFFER", label: "Best Offer" },
        ],
        help: "Leave empty for all." },

      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "right",
        options: [
          { value: "", label: "Default (3)" },
          { value: "1", label: "1 — everything, no filtering" },
          { value: "2", label: "2 — potential match or better" },
          { value: "3", label: "3 — poor match or better (default)" },
          { value: "4", label: "4 — good match or better" },
          { value: "5", label: "5 — great deals only" },
        ],
        help: "Default notification threshold for eBay items." },
      { key: "notify", label: "Notify users", type: "text", column: "right",
        help: "Comma-separated [user.*] names." },
      { key: "search_interval", label: "Search interval", type: "text", column: "right",
        help: "e.g. '30m'. The Browse API allows ~5000 calls/day across the whole app." },
      { key: "max_search_interval", label: "Max search interval", type: "text", column: "right", advanced: true },
      { key: "min_price", label: "Min price", type: "text", group: "Pricing", column: "right", advanced: true },
      { key: "max_price", label: "Max price", type: "text", column: "right", advanced: true },
      { key: "ai", label: "AI backends", type: "text", group: "AI evaluation", column: "right", advanced: true },
      { key: "prompt", label: "AI prompt", type: "textarea", column: "right", advanced: true },
      { key: "extra_prompt", label: "Extra prompt", type: "textarea", column: "right", advanced: true },
      { key: "enabled", label: "Enabled", type: "checkbox", group: "Status", column: "right" },
    ],

    "marketplace.depop": [
      { key: "enabled", label: "Enabled", type: "checkbox", column: "left" },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "left",
        options: [
          { value: "", label: "Default (3)" },
          { value: "3", label: "3 — poor match or better" },
          { value: "4", label: "4 — good match or better" },
          { value: "5", label: "5 — great deals only" },
        ] },
      { key: "notify", label: "Notify users", type: "text", column: "left" },
      { key: "search_interval", label: "Search interval", type: "text", column: "left",
        help: "e.g. '1h'. Scraped in the shared browser — be polite." },
      { key: "min_price", label: "Min price", type: "text", column: "right" },
      { key: "max_price", label: "Max price", type: "text", column: "right",
        help: "Search tiles carry no description, so the AI judges on title + price only." },
    ],

    "marketplace.poshmark": [
      { key: "enabled", label: "Enabled", type: "checkbox", column: "left" },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "left",
        options: [
          { value: "", label: "Default (3)" },
          { value: "3", label: "3 — poor match or better" },
          { value: "4", label: "4 — good match or better" },
          { value: "5", label: "5 — great deals only" },
        ] },
      { key: "notify", label: "Notify users", type: "text", column: "left" },
      { key: "search_interval", label: "Search interval", type: "text", column: "left",
        help: "e.g. '1h'. Scraped in the shared browser — be polite." },
      { key: "min_price", label: "Min price", type: "text", column: "right" },
      { key: "max_price", label: "Max price", type: "text", column: "right",
        help: "Search tiles carry no description, so the AI judges on title + price only." },
    ],

    "item.*": [
      // Left: item-specific
      { key: "search_phrases", label: "Search phrases", type: "text", required: true, column: "left",
        help: "Comma-separated. e.g. 'gopro hero 11, gopro hero 12'" },
      { key: "description", label: "Description (helps AI)", type: "textarea", column: "left",
        help: "Free-text description of what you want. The AI uses this to evaluate listings." },
      { key: "marketplace", label: "Marketplace", type: "text", column: "left", advanced: true,
        help: "Which [marketplace.*] to search. Default: first defined marketplace." },

      // Right: overrides from marketplace defaults
      { key: "search_city", label: "Search city", type: "text", column: "right",
        help: "Override marketplace's search city for this item." },
      { key: "search_region", label: "Search region", type: "select", column: "right",
        options: [{ value: "", label: "(inherit from marketplace)" }].concat(
          BUILT_IN_REGIONS.map((r) => ({ value: r, label: r }))
        ) },
      { key: "min_price", label: "Min price", type: "text", column: "right",
        help: "e.g. '50' or '50 USD'" },
      { key: "max_price", label: "Max price", type: "text", column: "right",
        help: "e.g. '300' or '300 USD'" },
      { key: "category", label: "Category", type: "select", column: "right", advanced: true,
        options: CATEGORIES },
      { key: "condition", label: "Condition", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "new", label: "New" },
          { value: "used_like_new", label: "Used — like new" },
          { value: "used_good", label: "Used — good" },
          { value: "used_fair", label: "Used — fair" },
        ] },
      { key: "availability", label: "Availability", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "all", label: "All" },
          { value: "in", label: "In stock" },
          { value: "out", label: "Out of stock" },
        ] },
      { key: "date_listed", label: "Date listed", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "1", label: "Last 24 hours" },
          { value: "7", label: "Last 7 days" },
          { value: "30", label: "Last 30 days" },
        ] },
      { key: "delivery_method", label: "Delivery method", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "local_pick_up", label: "Local pick-up" },
          { value: "shipping", label: "Shipping" },
        ] },
      { key: "keywords", label: "Keywords (include)", type: "text", column: "right", advanced: true,
        help: "Boolean expression, e.g. 'drone AND (DJI OR Orqa)'" },
      { key: "antikeywords", label: "Anti-keywords (exclude)", type: "text", column: "right", advanced: true },
      { key: "seller_locations", label: "Seller locations", type: "text", column: "right", advanced: true,
        help: "Comma-separated." },
      { key: "exclude_sellers", label: "Exclude sellers", type: "text", column: "right", advanced: true },
      { key: "notify", label: "Notify users", type: "text", column: "right", advanced: true,
        help: "Comma-separated [user.*] names. Default: inherit from marketplace." },
      { key: "ai", label: "AI backends", type: "text", group: "AI", column: "right", advanced: true },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "right",
        options: [
          { value: "", label: "Inherit from marketplace (default 3)" },
          { value: "1", label: "1 — everything, no filtering" },
          { value: "2", label: "2 — potential match or better" },
          { value: "3", label: "3 — poor match or better (default)" },
          { value: "4", label: "4 — good match or better" },
          { value: "5", label: "5 — great deals only" },
        ],
        help: "The notification threshold. Listings the AI scores below this are logged and shown in Activity as dismissed, but never notified." },
      { key: "prompt", label: "AI prompt", type: "textarea", column: "right", advanced: true },
      { key: "extra_prompt", label: "Extra prompt", type: "textarea", column: "right", advanced: true },
      { key: "rating_prompt", label: "Rating prompt", type: "textarea", column: "right", advanced: true },
      { key: "search_interval", label: "Search every (minimum)", type: "text", group: "Schedule", column: "right",
        help: "Duration, e.g. '45m', '2h', '1d'. Blank inherits the marketplace value (30m by default). Every search phrase on this item is its own page load, so a six-phrase item searched every 30 minutes is 288 page loads a day — enough to get an account temporarily blocked." },
      { key: "max_search_interval", label: "… and at most", type: "text", column: "right",
        help: "Upper bound for the random wait. Set it higher than the minimum and the monitor picks a fresh random interval every cycle, so its requests never form a detectable pattern. Blank inherits the marketplace value (1h by default)." },
      { key: "request_delay", label: "Delay between page loads (seconds)", type: "text", column: "right", advanced: true,
        help: "Two numbers, e.g. '6, 15' — a random pause in that range before each page load for this item. Blank inherits the marketplace setting." },
      { key: "start_at", label: "Start at", type: "text", column: "right", advanced: true,
        help: "Comma-separated time patterns: 'HH:MM', '*:MM', '*:*:SS'. Setting this replaces the interval above with fixed clock times — which is more predictable to Facebook, so prefer the randomized interval unless you need a specific hour." },
    ],

    // ---- User form ----
    "user.*": [
      { key: "pushbullet_token", label: "Pushbullet token", type: "password",
        help: "Get your token from pushbullet.com → Settings → Access tokens." },
      { key: "pushover_user_key", label: "Pushover user key", type: "password", group: "Pushover" },
      { key: "pushover_api_token", label: "Pushover API token", type: "password" },
      { key: "telegram_token", label: "Telegram bot token", type: "password", group: "Telegram",
        help: "Format: 123456789:ABCdef..." },
      { key: "telegram_chat_id", label: "Telegram chat ID", type: "text",
        help: "Numeric ID or @username." },
      { key: "ntfy_server", label: "ntfy server URL", type: "text", group: "ntfy",
        help: "e.g. https://ntfy.sh" },
      { key: "ntfy_topic", label: "ntfy topic", type: "text" },
      { key: "email", label: "Email address", type: "text", group: "Email",
        help: "Comma-separated list of recipient addresses." },
      { key: "smtp_server", label: "SMTP server", type: "text", advanced: true },
      { key: "smtp_port", label: "SMTP port", type: "number", advanced: true,
        help: "Default: 587" },
      { key: "smtp_username", label: "SMTP username", type: "text", advanced: true },
      { key: "smtp_password", label: "SMTP password (app password)", type: "password", advanced: true },
      { key: "smtp_from", label: "SMTP from address", type: "text", advanced: true },
      { key: "notify_with", label: "Notification sections", type: "text", group: "Other", advanced: true,
        help: "Comma-separated [notification.*] section names for shared credentials." },
      { key: "remind", label: "Remind interval", type: "text", advanced: true,
        help: "Resend after this interval, e.g. '1d', '6h'. Default: one-time." },
    ],

    // ---- AI backend form ----
    "ai.*": [
      { key: "api_key", label: "API key", type: "password",
        help: "If left blank, the env var for the provider is used (e.g. ${OPENAI_API_KEY}, ${ANTHROPIC_API_KEY}, ${DEEPSEEK_API_KEY})." },
      { key: "model", label: "Model", type: "text",
        help: "e.g. 'gpt-4o', 'deepseek-chat', 'deepseek-r1:14b', 'claude-sonnet-4-20250514'" },
      { key: "provider", label: "Provider override", type: "text", advanced: true,
        help: "Override the provider (auto-detected from section name). Only needed for custom OpenAI-compatible endpoints." },
      { key: "base_url", label: "Base URL", type: "text", advanced: true,
        help: "Custom API endpoint. Required for Ollama (e.g. http://localhost:11434/v1)." },
      { key: "timeout", label: "Timeout (seconds)", type: "number", advanced: true },
      { key: "max_retries", label: "Max retries", type: "number", advanced: true,
        help: "Default: 10" },
    ],
  };

  // Look up a schema for a section name. Exact match first, then
  // prefix-wildcard (e.g. "item.gopro" → "item.*").
  const findFormSchema = (sectionName) => {
    if (FORM_SCHEMAS[sectionName]) return FORM_SCHEMAS[sectionName];
    const dot = sectionName.indexOf(".");
    if (dot >= 0) {
      const prefix = sectionName.slice(0, dot);
      const wildcard = prefix + ".*";
      if (FORM_SCHEMAS[wildcard]) return FORM_SCHEMAS[wildcard];
      if (prefix === "marketplace") {
        // A marketplace section under any name still has a concrete type:
        // its market_type key, else facebook (mirroring the backend's
        // section-name inference). A renamed section must not lose its form.
        const section = state.sections.find((x) => x.name === sectionName);
        const fields = section ? fieldsForSection(section) : {};
        const kind = String(fields.market_type || "facebook").toLowerCase();
        return FORM_SCHEMAS["marketplace." + kind] || FORM_SCHEMAS["marketplace.facebook"];
      }
    }
    return null;
  };

  // Tracks which section is currently being edited.
  let formContext = { sectionName: "", fields: {}, schema: [] };
  let showAdvanced = false;

  const formModal = {
    el: () => $("#form-modal"),
    open() { this.el().classList.remove("hidden"); },
    close() {
      this.el().classList.add("hidden");
      $("#form-error").hidden = true;
      const form = $("#section-form");
      if (form) form.innerHTML = "";
    },
  };

  // Which tab is selected (for two-tab forms).
  let activeTab = "left";

  // Render form fields into #section-form.
  const renderForm = (schema, fields) => {
    const form = $("#section-form");
    form.innerHTML = "";

    // Always render the section name field first. In edit mode it shows
    // the current suffix (editable for rename); in add/duplicate mode
    // it shows the suggested new name.
    const currentPrefix = formContext.addMode ? formContext.addPrefix : formContext.sectionName.split(".")[0];
    // For AI sections, show a dropdown of known providers instead of a
    // free-text name input.
    const aiAutoName = currentPrefix === "ai";
    const nameWrapper = document.createElement("div");
    nameWrapper.className = "form-field";
    const currentSuffix = formContext.nameValue ??
      (formContext.addMode ? "" : (formContext.sectionName.split(".").slice(1).join(".") || formContext.sectionName));
    if (aiAutoName) {
      const aiProviders = [
        { value: "openai", label: "OpenAI" },
        { value: "deepseek", label: "DeepSeek" },
        { value: "anthropic", label: "Anthropic" },
        { value: "ollama", label: "Ollama" },
      ];
      const opts = aiProviders.map((p) =>
        `<option value="${p.value}" ${currentSuffix === p.value ? "selected" : ""}>${p.label}</option>`
      ).join("");
      nameWrapper.innerHTML =
        `<label class="form-label">AI Provider <span class="required">*</span></label>` +
        `<select id="add-section-name">${opts}</select>` +
        `<p class="form-help">[ai.<em>provider</em>]</p>`;
      const nameSelect = nameWrapper.querySelector("select");
      nameSelect.addEventListener("change", () => { formContext.nameValue = nameSelect.value; });
      // Set initial value.
      if (!currentSuffix) {
        formContext.nameValue = nameSelect.value;
      }
    } else {
      nameWrapper.innerHTML =
        `<label class="form-label">Section name <span class="required">*</span></label>` +
        `<input type="text" id="add-section-name" name="aimm_section_name" ` +
        `autocomplete="one-time-code" readonly onfocus="this.removeAttribute('readonly')" ` +
        `value="${esc(currentSuffix)}" ` +
        `placeholder="e.g. gopro, me" />` +
        `<p class="form-help">[${esc(currentPrefix)}.<em>name</em>]</p>`;
      const nameInput = nameWrapper.querySelector("input");
      nameInput.addEventListener("input", () => { formContext.nameValue = nameInput.value; });
    }
    form.appendChild(nameWrapper);

    const hasColumns = schema.some((f) => f.column);
    const hasAdvanced = schema.some((f) => f.advanced);

    // If the schema uses columns, render tabs.
    if (hasColumns) {
      // Labels follow which schema is actually rendering — an eBay form
      // titled "Facebook Login" is how this read before there was a second
      // marketplace.
      const prefix = formContext.sectionName.split(".")[0];
      const schemaKind = (Object.entries(FORM_SCHEMAS).find(([, v]) => v === schema) || [""])[0];
      const MARKET_TABS = {
        "marketplace.facebook": ["Facebook Login", "Search Defaults (overridable per item)"],
        "marketplace.ebay": ["eBay API", "Search Defaults"],
        "marketplace.depop": ["Depop", "Pricing"],
        "marketplace.poshmark": ["Poshmark", "Pricing"],
      };
      const marketTabs = MARKET_TABS[schemaKind];
      const leftLabel =
        prefix === "marketplace"
          ? (marketTabs ? marketTabs[0] : "Login")
          : "Item Settings";
      const rightLabel =
        prefix === "marketplace"
          ? (marketTabs ? marketTabs[1] : "Search Defaults")
          : "Filters & AI";

      const tabBar = document.createElement("div");
      tabBar.className = "form-tab-bar";
      const leftBtn = document.createElement("button");
      leftBtn.type = "button";
      leftBtn.className = "form-tab" + (activeTab === "left" ? " active" : "");
      leftBtn.textContent = leftLabel;
      leftBtn.addEventListener("click", () => { activeTab = "left"; renderForm(schema, fields); });
      const rightBtn = document.createElement("button");
      rightBtn.type = "button";
      rightBtn.className = "form-tab" + (activeTab === "right" ? " active" : "");
      rightBtn.textContent = rightLabel;
      rightBtn.addEventListener("click", () => { activeTab = "right"; renderForm(schema, fields); });
      tabBar.appendChild(leftBtn);
      tabBar.appendChild(rightBtn);
      form.appendChild(tabBar);
    }

    // Toggle for advanced fields.
    const visibleFields = hasColumns
      ? schema.filter((f) => (f.column || "left") === activeTab)
      : schema;
    const tabHasAdvanced = visibleFields.some((f) => f.advanced);
    if (tabHasAdvanced) {
      const toggle = document.createElement("label");
      toggle.className = "form-label";
      toggle.style.cursor = "pointer";
      toggle.innerHTML =
        `<input type="checkbox" id="show-advanced" ${showAdvanced ? "checked" : ""} /> ` +
        `Show advanced fields`;
      toggle.querySelector("input").addEventListener("change", (e) => {
        showAdvanced = e.target.checked;
        renderForm(schema, fields);
      });
      form.appendChild(toggle);
    }

    let lastGroup = null;
    visibleFields.forEach((fieldDef) => {
      if (fieldDef.advanced && !showAdvanced) return;

      // Group header.
      if (fieldDef.group && fieldDef.group !== lastGroup) {
        lastGroup = fieldDef.group;
        const groupEl = document.createElement("div");
        groupEl.className = "form-group-title";
        groupEl.textContent = fieldDef.group;
        form.appendChild(groupEl);
      }

      const wrapper = document.createElement("div");
      wrapper.className = "form-field";

      const label = document.createElement("label");
      label.className = "form-label";
      label.innerHTML =
        esc(fieldDef.label) +
        (fieldDef.required
          ? ' <span class="required">*</span>'
          : ' <span class="optional">optional</span>');
      wrapper.appendChild(label);

      let input;
      const rawVal = fields[fieldDef.key];
      // Flatten arrays to comma-separated for text fields.
      const currentVal =
        Array.isArray(rawVal) ? rawVal.join(", ") : rawVal ?? "";
      // For checkboxes, track which values are currently selected.
      const checkedSet = new Set(
        Array.isArray(rawVal) ? rawVal.map(String) : currentVal ? [String(currentVal)] : []
      );

      if (fieldDef.type === "checkboxes") {
        // Render a group of checkboxes for multi-value fields.
        input = document.createElement("div");
        input.className = "checkboxes";
        input.dataset.key = fieldDef.key;
        (fieldDef.options || []).forEach((opt) => {
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.value = opt.value;
          cb.checked = checkedSet.has(String(opt.value));
          cb.id = `field-${fieldDef.key}-${opt.value}`;
          const lbl = document.createElement("label");
          lbl.htmlFor = cb.id;
          lbl.appendChild(cb);
          lbl.append(` ${opt.label}`);
          input.appendChild(lbl);
        });
      } else if (fieldDef.type === "select") {
        input = document.createElement("select");
        (fieldDef.options || []).forEach((opt) => {
          const o = document.createElement("option");
          o.value = opt.value;
          o.textContent = opt.label;
          if (String(currentVal) === String(opt.value)) o.selected = true;
          input.appendChild(o);
        });
      } else if (fieldDef.type === "textarea") {
        input = document.createElement("textarea");
        input.rows = 3;
        input.value = currentVal;
      } else {
        input = document.createElement("input");
        input.type = fieldDef.type || "text";
        // For password fields with <REDACTED>, show placeholder instead.
        if (fieldDef.type === "password" && String(currentVal) === "<REDACTED>") {
          input.value = "";
          input.placeholder = "(unchanged — leave blank to keep current)";
        } else {
          input.value = currentVal;
        }
        if (fieldDef.type === "number") {
          input.min = "0";
          input.step = "1";
        }
      }
      if (fieldDef.type !== "checkboxes") {
        // Prefixed name + explicit autocomplete: with name="username" beside
        // name="password", Chrome decides this is a login form and autofills
        // saved site credentials into it — which is how a user's web UI
        // password ended up renaming a config section. new-password is the
        // one value password managers reliably leave alone.
        input.name = "aimm_" + fieldDef.key;
        input.autocomplete = fieldDef.type === "password" ? "new-password" : "off";
        // Chrome fills credential-looking fields at render time regardless of
        // autocomplete when a password input is nearby. It skips readonly
        // fields, so inputs open on focus instead — a saved "admin" login
        // renamed a config section through this exact gap, twice.
        if (fieldDef.type === "text" || fieldDef.type === "password") {
          input.readOnly = true;
          input.addEventListener("focus", () => { input.readOnly = false; });
        }
        input.dataset.key = fieldDef.key;
        label.htmlFor = fieldDef.key;
        input.id = "field-" + fieldDef.key;
      }
      wrapper.appendChild(input);

      if (fieldDef.help) {
        const help = document.createElement("p");
        help.className = "form-help";
        help.textContent = fieldDef.help;
        wrapper.appendChild(help);
      }
      form.appendChild(wrapper);
    });

    // For AI sections in add mode, set the API key to the env var
    // reference matching the selected provider.
    if (aiAutoName && formContext.addMode) {
      const envVarMap = {
        openai: "${OPENAI_API_KEY}",
        deepseek: "${DEEPSEEK_API_KEY}",
        anthropic: "${ANTHROPIC_API_KEY}",
        ollama: "${OLLAMA_API_KEY}",
      };
      const nameSelect = $("#add-section-name");
      const apiKeyInput = form.querySelector('[data-key="api_key"]');
      if (nameSelect && apiKeyInput) {
        const syncApiKey = () => {
          const envRef = envVarMap[nameSelect.value] || "";
          // Only auto-fill if the user hasn't typed something custom.
          if (!apiKeyInput.value || apiKeyInput.value.startsWith("${")) {
            apiKeyInput.value = envRef;
          }
        };
        nameSelect.addEventListener("change", syncApiKey);
        syncApiKey();
      }
    }

  };

  // Collect form field values into a {key: coerced_value} dict.
  const collectFormValues = () => {
    const form = $("#section-form");
    const errors = [];
    const values = {};

    formContext.schema.forEach((fieldDef) => {
      if (fieldDef.advanced && !showAdvanced) return;
      const input = form.querySelector(`[data-key="${fieldDef.key}"]`);
      if (!input) return;

      let newVal;
      if (fieldDef.type === "checkboxes") {
        const checked = Array.from(input.querySelectorAll("input:checked")).map(
          (cb) => cb.value
        );
        newVal = checked.length ? checked.join(", ") : "";
      } else {
        newVal = input.value.trim();
      }

      if (fieldDef.required && !newVal) {
        errors.push(`${fieldDef.label} is required.`);
        return;
      }
      if (!newVal) return;
      if (fieldDef.type === "password" && !newVal) return;

      // Type coercion.
      let value;
      if (fieldDef.type === "number" && newVal) {
        value = parseInt(newVal, 10);
        if (isNaN(value)) { errors.push(`${fieldDef.label} must be a number.`); return; }
      } else if (fieldDef.coerce === "int" && newVal) {
        // Selects yield strings; a rating written as "4" fails the config
        // validator, which wants an integer. Coerce explicitly-marked fields.
        value = parseInt(newVal, 10);
        if (isNaN(value)) { errors.push(`${fieldDef.label} must be a number.`); return; }
      } else if (!fieldDef.keepString && /^-?\d+$/.test(newVal) && !newVal.includes(",")) {
        // An integer doesn't wear quotes. Bare-integer text becomes a TOML
        // int — the backend coerces int→str where it wants strings (prices),
        // and fields that MUST stay strings (search_city: the validator
        // rejects non-strings) carry keepString in their schema.
        value = parseInt(newVal, 10);
      } else if (newVal.includes(",") && fieldDef.type === "text") {
        const original = formContext.fields[fieldDef.key];
        if (Array.isArray(original) || newVal.includes(",")) {
          value = newVal.split(",").map((s) => s.trim()).filter(Boolean);
          if (!fieldDef.keepString && value.every((x) => /^-?\d+$/.test(x))) {
            value = value.map((x) => parseInt(x, 10)); // e.g. radius wants ints
          }
        } else {
          value = newVal;
        }
      } else {
        value = newVal;
      }
      values[fieldDef.key] = value;
    });
    return { values, errors };
  };

  // Generate a TOML section block as text for "add" mode.
  const generateSectionToml = (sectionFullName, values) => {
    const lines = [`[${sectionFullName}]`];
    for (const [key, val] of Object.entries(values)) {
      if (Array.isArray(val)) {
        const items = val.map((v) =>
          typeof v === "number" ? String(v) : `"${String(v).replace(/"/g, '\\"')}"`
        );
        lines.push(`${key} = [${items.join(", ")}]`);
      } else if (typeof val === "number") {
        lines.push(`${key} = ${val}`);
      } else if (typeof val === "boolean") {
        lines.push(`${key} = ${val}`);
      } else {
        lines.push(`${key} = "${String(val).replace(/"/g, '\\"')}"`);
      }
    }
    return lines.join("\n") + "\n";
  };

  // Save handler — works for both edit and add modes.
  const saveForm = async () => {
    const form = $("#section-form");
    const { values, errors } = collectFormValues();

    // ---- Add mode: generate a new section block and append ----
    if (formContext.addMode) {
      const nameInput = $("#add-section-name");
      const sectionSuffix = (nameInput ? nameInput.value.trim() : "").replace(/[^a-zA-Z0-9_\-]/g, "_");
      if (!sectionSuffix) {
        errors.push("Section name is required.");
      }
      if (errors.length) {
        $("#form-error").textContent = errors.join(" ");
        $("#form-error").hidden = false;
        return;
      }

      const fullName = `${formContext.addPrefix}.${sectionSuffix}`;
      // Check for duplicate.
      if (state.sections.some((s) => s.name === fullName)) {
        $("#form-error").textContent = `Section [${fullName}] already exists.`;
        $("#form-error").hidden = false;
        return;
      }

      if (formContext.addKind && !("market_type" in values)) {
        values.market_type = formContext.addKind;
      }
      if (formContext.addKind === "ebay" && !("enabled" in values)) {
        // A fresh eBay section starts disabled: with placeholder credentials
        // it cannot search yet, and starting paused says so honestly.
        values.enabled = false;
      }
      const block = generateSectionToml(fullName, values);
      let buffer = state.currentContent;
      // Append after the last section of the same type, or at end.
      const samePrefixSections = state.sections.filter(
        (s) => s.prefix === formContext.addPrefix
      );
      if (samePrefixSections.length) {
        const last = samePrefixSections[samePrefixSections.length - 1];
        const lines = buffer.split("\n");
        const insertAt = last.line_end;
        lines.splice(insertAt, 0, "", ...block.split("\n"));
        buffer = lines.join("\n");
      } else {
        buffer = buffer.replace(/\n*$/, "") + "\n\n" + block;
      }

      editor.setValue(buffer);
      state.currentContent = buffer;
      const dirty = state.currentContent !== state.originalContent;
      $("#save-btn").disabled = !dirty;
      refreshSectionsFromBuffer();
      formModal.close();
      if (dirty) await saveConfig();
      return;
    }

    // ---- Edit mode ----
    // Check if the user renamed the section.
    const nameInput = $("#add-section-name");
    const newSuffix = nameInput ? nameInput.value.trim().replace(/[^a-zA-Z0-9_\-]/g, "_") : "";
    if (!newSuffix) {
      errors.push("Section name is required.");
    }
    const prefix = formContext.sectionName.split(".")[0];
    const newFullName = prefix + "." + newSuffix;
    const renamed = newFullName !== formContext.sectionName;

    if (renamed && state.sections.some((s) => s.name === newFullName)) {
      errors.push(`Section [${newFullName}] already exists.`);
    }
    if (errors.length) {
      $("#form-error").textContent = errors.join(" ");
      $("#form-error").hidden = false;
      return;
    }

    // Rename rewrites ONLY the section header line, in place. The previous
    // approach deleted the section and regenerated it from the form's values,
    // which silently dropped every key the form did not carry — a rename of
    // [marketplace.facebook] once lost home_location, login_wait_time and
    // search_interval this way. After the header rewrite, the field edits
    // fall through to the same patch-in-place path as a plain edit.
    let targetName = formContext.sectionName;
    if (renamed) {
      const section = state.sections.find((s) => s.name === formContext.sectionName);
      if (!section) {
        $("#form-error").textContent = "Section not found in the buffer — reload and retry.";
        $("#form-error").hidden = false;
        return;
      }
      const lines = state.currentContent.split("\n");
      lines[section.line_start] = lines[section.line_start].replace(
        /\[[^\]]+\]/,
        "[" + newFullName + "]"
      );
      state.currentContent = lines.join("\n");
      editor.setValue(state.currentContent);
      refreshSectionsFromBuffer();
      targetName = newFullName;
    }
    {
      // No rename — patch fields in place via tomlEdit.edit().
      if (!window.tomlEdit) {
        $("#form-error").textContent =
          "TOML editor library failed to load — edit the TOML directly.";
        $("#form-error").hidden = false;
        return;
      }
      let buffer = state.currentContent;
      const editErrors = [];
      formContext.schema.forEach((fieldDef) => {
        if (fieldDef.advanced && !showAdvanced) return;
        if (fieldDef.key in values) {
          try {
            buffer = window.tomlEdit.edit(
              buffer, targetName + "." + fieldDef.key, values[fieldDef.key]
            );
          } catch (err) {
            editErrors.push(`Failed to set ${fieldDef.key}: ${err.message}`);
          }
        }
      });
      if (editErrors.length) {
        $("#form-error").textContent = editErrors.join(" ");
        $("#form-error").hidden = false;
        return;
      }
      editor.setValue(buffer);
      state.currentContent = buffer;
    }

    const dirty = state.currentContent !== state.originalContent;
    $("#save-btn").disabled = !dirty;
    refreshSectionsFromBuffer();
    formModal.close();
    if (dirty) await saveConfig();
  };

  // Open the Edit form for a specific section.
  const openEditSectionModal = (sectionName) => {
    // Find the section in state.sections (populated from the server
    // or the client-side scanner).
    const section = state.sections.find((s) => s.name === sectionName);
    let fields = (section && section.fields) || {};

    // If the server didn't provide parsed fields (e.g. aimm wasn't
    // restarted), try parsing the textarea content with tomlEdit.
    if (!Object.keys(fields).length && window.tomlEdit) {
      try {
        const parsed = window.tomlEdit.parse(state.currentContent);
        // Navigate the nested dict: "marketplace.facebook" → parsed.marketplace.facebook
        const parts = sectionName.split(".");
        let node = parsed;
        for (const p of parts) { node = node && node[p]; }
        if (node && typeof node === "object") fields = node;
      } catch (err) {
        console.warn("tomlEdit.parse failed for form:", err);
      }
    }

    // Look up the schema. If we don't have one for this section type,
    // show a "raw TOML only" message.
    const schema = findFormSchema(sectionName);
    if (!schema) {
      $("#form-modal-title").textContent = `Edit [${sectionName}]`;
      $("#form-modal-hint").hidden = false;
      $("#form-modal-hint").textContent =
        `No form defined for [${sectionName}] yet — edit the TOML directly ` +
        "in the editor. (Forms for item, user, and AI sections are coming soon.)";
      $("#section-form").innerHTML = "";
      formModal.open();
      return;
    }

    const dot = sectionName.indexOf(".");
    const suffix = dot >= 0 ? sectionName.slice(dot + 1) : sectionName;
    formContext = { sectionName, fields, schema, nameValue: suffix };
    $("#form-modal-title").textContent = `Edit [${sectionName}]`;
    $("#form-modal-hint").hidden = true;
    renderForm(schema, fields);
    formModal.open();
  };

  // Open form in "add" mode: empty fields + a name input at top.
  const openAddSectionModal = (prefix, suggested) => {
    const schema =
      (suggested && FORM_SCHEMAS[prefix + "." + suggested]) ||
      findFormSchema(prefix + ".*") ||
      findFormSchema(prefix + ".facebook");
    if (!schema) {
      alert(`No form defined for [${prefix}.*] — add it manually in the TOML editor.`);
      return;
    }
    // Build a placeholder section name from the prefix.
    const existingNames = state.sections
      .filter((s) => s.prefix === prefix)
      .map((s) => s.suffix);
    let suggestedName = suggested || (prefix === "marketplace" ? "facebook" : "");

    formContext = {
      sectionName: `${prefix}.__new__`,
      fields: {},
      schema,
      addMode: true,
      addPrefix: prefix,
      // The concrete marketplace type this add is for, when known. Written
      // into the section as market_type so the section NAME never has to
      // carry the type — renaming (or an autofill accident) cannot silently
      // turn an eBay section into a facebook one.
      addKind: prefix === "marketplace" ? suggestedName || "facebook" : null,
      nameValue: suggestedName,
    };
    activeTab = "left";
    $("#form-modal-title").textContent = `Add a new [${prefix}.*] section`;
    $("#form-modal-hint").hidden = false;
    $("#form-modal-hint").textContent =
      "Choose a name and fill in the fields. The new section will be " +
      "appended to the end of your config.";
    renderForm(schema, {});
    formModal.open();
    setTimeout(() => {
      const nameInput = $("#add-section-name");
      if (nameInput && !nameInput.value) nameInput.focus();
    }, 50);
  };

  wireClick("#form-modal-close", () => formModal.close());
  wireClick("#form-cancel", () => formModal.close());
  wireClick("#form-save", () => saveForm());
  const backdrop = document.querySelector("#form-modal .modal-backdrop");
  if (backdrop) backdrop.addEventListener("click", () => formModal.close());

  // -------- "+ Add" dropdown in the header --------
  wireClick("#add-btn", () => {
    const menu = $("#add-menu");
    if (menu) menu.classList.toggle("hidden");
  });
  // Close dropdown when clicking outside.
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#add-dropdown")) {
      const menu = $("#add-menu");
      if (menu) menu.classList.add("hidden");
    }
  });
  // Wire each menu item to openAddSectionModal.
  document.querySelectorAll("#add-menu button[data-prefix]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const menu = $("#add-menu");
      if (menu) menu.classList.add("hidden");
      openAddSectionModal(btn.dataset.prefix);
    });
  });

  // ---------------------------------------------------------------
  // Config form view
  //
  // A browsable view of the same buffer the TOML editor holds. Editing goes
  // through the existing section modal and its FORM_SCHEMAS, so this adds a
  // way in rather than a second implementation -- Save still commits the one
  // buffer, whichever tab produced the change.
  // ---------------------------------------------------------------
  const CONFIG_GROUPS = [
    { prefix: "marketplace", title: "Marketplace", addable: true },
    { prefix: "item", title: "Items to watch", addable: true },
    { prefix: "ai", title: "AI backends", addable: true },
    { prefix: "user", title: "Users", addable: true },
    { prefix: "notification", title: "Notifications", addable: false },
    { prefix: "translation", title: "Translations", addable: false },
  ];

  // The handful of keys worth showing on a collapsed card -- enough to tell
  // two sections apart without opening either.
  const SUMMARY_KEYS = {
    marketplace: ["search_city", "search_interval", "rating", "notify"],
    item: ["enabled", "search_phrases", "min_price", "max_price", "rating"],
    ai: ["provider", "model", "base_url"],
    user: ["notify_with", "email"],
    notification: [
      "ntfy_server",
      "ntfy_topic",
      "pushover_user_key",
      "smtp_username",
      "telegram_chat_id",
    ],
    translation: ["locale"],
  };

  // tomlEdit.parse on every card would re-parse the whole document per section,
  // so parse once per distinct buffer and hand out slices of the result.
  const parsedConfig = { content: null, tree: null };

  const configTree = () => {
    // Read the editor, not state.currentContent: the latter is only synced at
    // certain points, so mid-edit it can lag and the cards would show stale
    // values against freshly rescanned section names.
    const content =
      (editor && editor.getValue ? editor.getValue() : state.currentContent) || "";
    if (parsedConfig.content === content) return parsedConfig.tree;
    let tree = null;
    if (window.tomlEdit) {
      try {
        tree = window.tomlEdit.parse(content);
      } catch (err) {
        tree = null; // half-typed TOML mid-edit; cards fall back to name only
      }
    }
    parsedConfig.content = content;
    parsedConfig.tree = tree;
    return tree;
  };

  const fieldsForSection = (section) => {
    if (section.fields && Object.keys(section.fields).length) return section.fields;
    const tree = configTree();
    if (!tree) return {};
    let node = tree;
    for (const part of section.name.split(".")) {
      node = node && node[part];
    }
    return node && typeof node === "object" && !Array.isArray(node) ? node : {};
  };

  const formatConfigValue = (value) => {
    if (Array.isArray(value)) return value.join(", ");
    if (typeof value === "boolean") return value ? "true" : "false";
    return String(value);
  };

  // ---- Search cadence -------------------------------------------------
  // The backend takes either a bare number of seconds or a human duration
  // ('45m', '2h', '1d'), parsed by convert_to_seconds. This mirrors the cases
  // a config actually uses so the card can show the real cadence rather than
  // echoing back whatever string was typed.
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

  // Defaults from monitor.py schedule_jobs: 30 minutes, jittered up to an hour.
  const DEFAULT_SEARCH_INTERVAL = 30 * 60;
  const DEFAULT_MAX_SEARCH_INTERVAL = 60 * 60;

  // First marketplace section that sets the key, mirroring the backend's
  // "item value, else marketplace value, else default" precedence.
  const marketplaceScheduleValue = (key) => {
    for (const mk of state.sections.filter((x) => x.prefix === "marketplace")) {
      const v = fieldsForSection(mk)[key];
      if (v !== undefined && v !== null && v !== "") return v;
    }
    return null;
  };

  // What the scheduler will actually do with this item, and where it came from.
  const itemCadence = (fields) => {
    const startAt = fields.start_at ? [].concat(fields.start_at) : null;
    const ownMin = fields.search_interval;
    const ownMax = fields.max_search_interval;
    const mkMin = marketplaceScheduleValue("search_interval");
    const mkMax = marketplaceScheduleValue("max_search_interval");
    const source =
      ownMin || ownMax ? "item" : mkMin || mkMax ? "marketplace" : "default";
    const min = Math.max(
      parseDuration(ownMin) || parseDuration(mkMin) || DEFAULT_SEARCH_INTERVAL,
      1
    );
    const max = Math.max(
      parseDuration(ownMax) || parseDuration(mkMax) || DEFAULT_MAX_SEARCH_INTERVAL,
      min
    );
    let label;
    if (startAt && startAt.length) {
      label = "at " + startAt.join(", ");
    } else if (min === max) {
      label = "every " + fmtCadence(min);
    } else {
      label = "every " + fmtCadence(min) + "–" + fmtCadence(max);
    }
    return { min, max, source, startAt, label };
  };

  // ---- Marketplace block state ----------------------------------------
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

  const blockChipLabel = (blk) => {
    const retry = new Date((blk.until || 0) * 1000);
    const hh = String(retry.getHours()).padStart(2, "0");
    const mm = String(retry.getMinutes()).padStart(2, "0");
    return (blk.marketplace || "marketplace") + ": blocked · retry " + hh + ":" + mm;
  };

  // Guidance for the inline controls. The per-field `help:` strings in
  // FORM_SCHEMAS cover the modal; these cover the card, and carry the "why"
  // that the schema hints never had room for.
  const INLINE_HELP = {
    phrases: {
      hint: "What gets typed into each marketplace's search box. This only decides what is fetched — the AI does the judging afterwards.",
      ex: "Add variants sellers actually type, not synonyms of your own. \u201Crtx 3090\u201D and \u201Cgeforce 3090\u201D surface different listings; \u201Cnvidia graphics card\u201D would just flood you.",
    },
    description: {
      hint: "Plain English, read by the AI on every listing. The single biggest lever on match quality.",
      ex: "Say what disqualifies as well as what qualifies: \u201C24GB card, must post video, no mining rigs, no water-cooled loops I\u2019d have to drain.\u201D",
    },
    prices: {
      hint: "A hard filter applied before the AI sees anything — listings outside it are never fetched or rated.",
      ex: "Leave the ceiling a little above your real limit. A seller who lists at $1,600 and takes $1,400 never appears if you cap at $1,500.",
    },
    antikeywords: {
      hint: "Cheap text filters run before the AI, to keep obvious junk out of your token spend.",
      ex: "Keep these blunt. Subtle judgements — \u201Cnot mined on\u201D — belong in the description above, where the AI can weigh them.",
    },
    threshold: {
      hint: "Listings the AI scores below this are still fetched, rated and listed under Activity as dismissed — they just never reach your phone.",
      ex: "3 gives you nearly everything the search returns. 5 only great deals. 4 is the useful middle for something you actually want.",
    },
    sources: {
      hint: "Which configured marketplaces this item is searched on. All of them when none is picked.",
      ex: "eBay ships nationwide so distance matters less there; Facebook is local pickup, where a tighter radius pays off.",
    },
    cadence: {
      hint: "How often this item is searched. Each search phrase is a separate page load, so an item with six phrases costs six page loads every time it runs — which is how accounts land in Facebook jail.",
      ex: "Leave the two boxes different and the monitor picks a fresh random wait in between on every cycle, so the searches never form a pattern. Six phrases: 2h to 4h. One phrase: 1h to 2h. Blank inherits from the marketplace (30m–60m by default).",
    },
    enabled: {
      hint: "Paused items keep their history and settings but are not searched.",
      ex: "Pause rather than delete when a hunt is over — the ratings already collected stay available for comparison later.",
    },
  };

  const helpBlock = (key) => {
    const h = INLINE_HELP[key];
    if (!h) return "";
    return `<div class="fieldhelp">${esc(h.hint)}<div class="ex">${esc(h.ex)}</div></div>`;
  };

  const THRESHOLD_WORDS = {
    1: "everything, no filtering",
    2: "potential match or better",
    3: "poor match or better",
    4: "good match or better",
    5: "great deals only",
  };

  // Write one key straight into the buffer the TOML tab shows. Same
  // tomlEdit.edit() the section modal uses, so comments and formatting survive
  // and Save behaves identically no matter which control produced the change.
  // Deleting a key is line surgery, not tomlEdit.edit: TOML has no null, so
  // there is nothing to "set" a cleared value to.
  const removeKeyFromSection = (sectionName, key) => {
    const lines = editor.getValue().split("\n");
    const section = scanSectionsClient(lines.join("\n")).find((x) => x.name === sectionName);
    if (!section) return null;
    const keyRe = new RegExp("^\\s*" + key + "\\s*=");
    const kept = lines.filter(
      (line, i) => !(i > section.line_start && i < section.line_end && keyRe.test(line))
    );
    return kept.join("\n");
  };

  const applyInline = (sectionName, key, value) => {
    if (!window.tomlEdit) {
      setEditorStatus("TOML editor library failed to load — use the TOML tab.", "error");
      return false;
    }
    try {
      const next =
        value === null
          ? removeKeyFromSection(sectionName, key)
          : window.tomlEdit.edit(editor.getValue(), `${sectionName}.${key}`, value);
      if (next === null) return false;
      editor.setValue(next);
      state.currentContent = next;
      // setValue fires CodeMirror's change handler, but call these directly
      // so the card re-renders and Save enables without waiting on the debounce.
      onEditorChange();
      refreshSectionsFromBuffer();
      return true;
    } catch (err) {
      setEditorStatus(`Could not set ${key}: ${err.message}`, "error");
      return false;
    }
  };

  const marketplaceNames = () =>
    state.sections.filter((s) => s.prefix === "marketplace").map((s) => s.suffix);

  const chipsInput = (section, key, values, opts = {}) => {
    const chips = (values || [])
      .map(
        (v) =>
          `<span class="chip2 ${opts.neg ? "neg" : ""}">${esc(v)}<x data-chip-del="${esc(
            key
          )}" data-chip-val="${esc(v)}">×</x></span>`
      )
      .join("");
    return `<div class="chips" data-chips="${esc(key)}">${chips}
      <input type="text" data-chip-add="${esc(key)}" placeholder="${esc(
        opts.placeholder || "add…"
      )}" autocomplete="off" /></div>`;
  };

  const renderItemCard = (section) => {
    const fields = fieldsForSection(section);
    const enabled = fields.enabled !== false;
    const open = state.openItems && state.openItems.has(section.name);
    const phrases = [].concat(fields.search_phrases || []);
    const anti = [].concat(fields.antikeywords || []);

    let threshold = fields.rating;
    if (Array.isArray(threshold)) threshold = threshold[threshold.length - 1];
    let inherited = 3;
    for (const mk of state.sections.filter((x) => x.prefix === "marketplace")) {
      let r = fieldsForSection(mk).rating;
      if (Array.isArray(r)) r = r[r.length - 1];
      if (typeof r === "number") inherited = r;
    }
    const effective = threshold || inherited;

    // Live performance numbers from the Deals data, so the card answers
    // "is this hunt working" without leaving the page.
    const sum = (activity.summary || []).find((x) => x.item === (section.suffix || section.name));

    const bound = fields.marketplace ? [].concat(fields.marketplace) : null;
    const SRC_SUB = { facebook: "browser · local pickup", ebay: "Browse API · ships", depop: "scrape · ships", poshmark: "scrape · ships" };
    const sources = marketplaceNames()
      .map((mk) => {
        const on = bound === null || bound.includes(mk);
        return `<span class="src ${on ? "on" : ""}" data-src="${esc(mk)}">
          <span class="sw ${on ? "on" : ""}"><i></i></span>
          <span><span class="nm">${esc(mk)}</span><br /><span class="st">${esc(
            SRC_SUB[mk] || "marketplace"
          )}</span></span></span>`;
      })
      .join("");

    const thrButtons = [1, 2, 3, 4, 5]
      .map((n) => `<button data-thr="${n}" class="${threshold === n ? "on" : ""}">${n}</button>`)
      .join("");

    const priceVal = (v) => (v == null ? "" : String(v).replace(/USD/i, "").trim());

    const cadence = itemCadence(fields);
    const cadenceNote = cadence.startAt
      ? "fixed times from Start at (advanced) — intervals ignored"
      : cadence.source === "item"
        ? "set on this item"
        : cadence.source === "marketplace"
          ? "inherited from the marketplace"
          : "default (no interval set anywhere)";
    const durVal = (v) => (v === undefined || v === null ? "" : String(v));

    return `
    <div class="item-card icard ${open ? "open" : ""} ${enabled ? "" : "disabled"}" data-section="${esc(
      section.name
    )}">
      <div class="ihead">
        <span class="caret">▸</span>
        <span class="iname">${esc(section.suffix || section.name)}</span>
        <span class="iphrases">${esc(phrases.map((x) => `“${x}”`).join(", "))}</span>
        <span class="istat">
          ${sum ? `<span><b>${sum.examined}</b> examined</span>
                  <span class="hit"><b>${sum.promising}</b> promising</span>` : ""}
          <span>notify ≥ <b>${effective}</b></span>
          <span class="icadence">${esc(cadence.label)}</span>
          <span class="sw ${enabled ? "on" : ""}" data-toggle="enabled" title="${
      enabled ? "Pause this item" : "Enable this item"
    }"><i></i></span>
        </span>
      </div>
      <div class="ibody">

        <div class="irow">
          <div class="lab">Search phrases</div>
          <div class="fld">
            ${chipsInput(section, "search_phrases", phrases, { placeholder: "add a phrase…" })}
            ${helpBlock("phrases")}
          </div>
        </div>

        <div class="irow">
          <div class="lab">What a good one looks like</div>
          <div class="fld">
            <textarea data-field="description" rows="2" placeholder="Describe a good listing — the AI reads this on every candidate.">${esc(
              fields.description || ""
            )}</textarea>
            ${helpBlock("description")}
          </div>
        </div>

        <div class="irow">
          <div class="lab">Price range</div>
          <div class="fld">
            <div class="inrow">
              <input type="text" class="w90" data-field="min_price" value="${esc(
                priceVal(fields.min_price)
              )}" placeholder="min" autocomplete="off" />
              <span class="range-sep">to</span>
              <input type="text" class="w90" data-field="max_price" value="${esc(
                priceVal(fields.max_price)
              )}" placeholder="max" autocomplete="off" />
              <span class="range-sep">USD</span>
            </div>
            ${helpBlock("prices")}
          </div>
        </div>

        <div class="irow">
          <div class="lab">Notify when AI rates</div>
          <div class="fld">
            <div class="inrow">
              <span class="thr">${thrButtons}</span>
              <span class="thr-note">${
                threshold
                  ? `≥ ${threshold} — ${THRESHOLD_WORDS[threshold]}`
                  : `inherited from marketplace (≥ ${inherited} — ${THRESHOLD_WORDS[inherited]})`
              }</span>
            </div>
            ${helpBlock("threshold")}
          </div>
        </div>

        <div class="irow">
          <div class="lab">Search on</div>
          <div class="fld">
            <div class="inrow">${sources || '<span class="thr-note">no marketplaces configured</span>'}</div>
            ${helpBlock("sources")}
          </div>
        </div>

        <div class="irow">
          <div class="lab">How often to search</div>
          <div class="fld">
            <div class="inrow">
              <input type="text" class="w90" data-field="search_interval" value="${esc(
                durVal(fields.search_interval)
              )}" placeholder="30m" autocomplete="off" />
              <span class="range-sep">to</span>
              <input type="text" class="w90" data-field="max_search_interval" value="${esc(
                durVal(fields.max_search_interval)
              )}" placeholder="1h" autocomplete="off" />
              <span class="thr-note">${esc(cadence.label)} — ${esc(cadenceNote)}</span>
            </div>
            ${helpBlock("cadence")}
          </div>
        </div>

        <div class="irow">
          <div class="lab">Must-not contain</div>
          <div class="fld">
            ${chipsInput(section, "antikeywords", anti, { neg: true, placeholder: "add an exclusion…" })}
            ${helpBlock("antikeywords")}
          </div>
        </div>

        <div class="irow more-row">
          <div class="lab"></div>
          <div class="fld"><div class="inrow">
            <button class="ghost small" data-act="edit">More settings…</button>
            <button class="ghost small" data-act="duplicate">Duplicate</button>
            <button class="ghost small danger" data-act="delete">Delete</button>
          </div></div>
        </div>
      </div>
    </div>`;
  };

  // ---- Sources / plumbing strip, per the mockup: one status card per
  // configured backend, with env-var resolution where the card uses one.
  const envRefsIn = (fields) => {
    const refs = [];
    const scan = (v) => {
      if (typeof v === "string") {
        const m = v.match(/^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/);
        if (m) refs.push(m[1]);
      } else if (Array.isArray(v)) v.forEach(scan);
    };
    Object.values(fields || {}).forEach(scan);
    return refs;
  };

  const renderSourceCards = () => {
    const cards = [];
    const env = state.envVars || {};
    const fb = (state.monitorInfo && state.monitorInfo.fb_session) || {};

    for (const mk of state.sections.filter((x) => x.prefix === "marketplace")) {
      const f = fieldsForSection(mk);
      const kind = String(f.market_type || mk.suffix || "facebook").toLowerCase();
      let dot = "ok";
      let detail = "";
      if (kind === "facebook") {
        const homeTxt = f.home_location
          ? " · home " + f.home_location
          : " · no home set — distances and maps are off";
        if (fb.logged_in) detail = "signed in" + homeTxt;
        else if (fb.exists) { dot = "warn"; detail = "anonymous session — log in via the Browser view"; }
        else { dot = "warn"; detail = "not signed in yet"; }
      } else if (kind === "ebay") {
        detail = "Browse API";
      }
      if (f.enabled === false) { dot = "dim"; detail = "disabled"; }
      const refs = envRefsIn(f);
      if (refs.some((r) => env[r] === false)) dot = "err";
      cards.push(`
        <div class="set">
          <div class="t"><span class="state-dot ${dot}"></span>${esc(mk.suffix || mk.name)}</div>
          <div class="d">${esc(detail)}</div>
          ${refs.map((r) => `<div class="envline"><span class="${env[r] ? "okv" : "bad"}">${
            env[r] ? "✓" : "✗"
          } ${esc(r)} ${env[r] ? "set" : "not set"}</span></div>`).join("")}
          <button class="ghost small edit" data-edit-section="${esc(mk.name)}">Edit</button>
        </div>`);
    }
    for (const ai of state.sections.filter((x) => x.prefix === "ai")) {
      const f = fieldsForSection(ai);
      cards.push(`
        <div class="set">
          <div class="t"><span class="state-dot ok"></span>${esc(ai.suffix || ai.name)}</div>
          <div class="d">${esc([f.model, f.base_url].filter(Boolean).join(" · ") || "AI backend")}</div>
          <button class="ghost small edit" data-edit-section="${esc(ai.name)}">Edit</button>
        </div>`);
    }
    for (const nt of state.sections.filter((x) => x.prefix === "notification")) {
      const f = fieldsForSection(nt);
      const refs = envRefsIn(f);
      const dot = refs.some((r) => env[r] === false) ? "err" : "ok";
      cards.push(`
        <div class="set">
          <div class="t"><span class="state-dot ${dot}"></span>${esc(nt.suffix || nt.name)}</div>
          <div class="d">${esc(f.ntfy_server ? "server " + f.ntfy_server : "notification channel")}</div>
          ${refs.map((r) => `<div class="envline"><span class="${env[r] ? "okv" : "bad"}">${
            env[r] ? "✓" : "✗"
          } ${esc(r)} ${env[r] ? "set" : "not set"}</span></div>`).join("")}
          <button class="ghost small edit" data-edit-section="${esc(nt.name)}">Edit</button>
        </div>`);
    }
    // Sources the backend supports but the config does not mention yet —
    // configuring a new marketplace should not require knowing a dropdown
    // exists. "Set up" opens the add modal pre-named with the right schema.
    const configuredKinds = new Set(
      state.sections
        .filter((x) => x.prefix === "marketplace")
        .map((x) => {
          const f = fieldsForSection(x);
          return String(f.market_type || x.suffix || "facebook").toLowerCase();
        })
    );
    const KIND_DESC = {
      ebay: "official Browse API · free key from developer.ebay.com",
      depop: "browser scrape · ships nationwide",
      poshmark: "browser scrape · ships nationwide",
      facebook: "browser · local pickup",
    };
    for (const kind of state.supportedMarketplaces || []) {
      if (configuredKinds.has(kind)) continue;
      cards.push(`
        <div class="set avail">
          <div class="t"><span class="state-dot dim"></span>${esc(kind)}</div>
          <div class="d">${esc(KIND_DESC[kind] || "marketplace")} · not configured</div>
          <button class="ghost small edit" data-setup-marketplace="${esc(kind)}">Set up</button>
        </div>`);
    }

    for (const us of state.sections.filter((x) => x.prefix === "user")) {
      const f = fieldsForSection(us);
      cards.push(`
        <div class="set">
          <div class="t"><span class="state-dot ok"></span>${esc(us.suffix || us.name)}</div>
          <div class="d">notify via ${esc([].concat(f.notify_with || []).join(", ") || "—")}</div>
          <button class="ghost small edit" data-edit-section="${esc(us.name)}">Edit</button>
        </div>`);
    }
    return cards.join("");
  };

  const renderConfigForm = () => {
    const host = $("#config-form-body");
    if (!host) return;
    if (!state.openItems) state.openItems = new Set();

    if (!state.sections.length) {
      host.innerHTML =
        '<div class="config-form-empty">No sections yet. Use “+ Add section” above to create one.</div>';
      return;
    }

    const items = state.sections.filter((x) => x.prefix === "item");
    const itemCards = items.map(renderItemCard).join("");

    const known = new Set(["item", "marketplace", "ai", "user", "notification", "translation", "monitor", "region"]);
    const strays = state.sections.filter((x) => !known.has(x.prefix));
    const strayBlock = strays.length
      ? `<div class="sechead">Other sections</div>` +
        strays
          .map(
            (x) => `<div class="set"><div class="t">[${esc(x.name)}]</div>
              <button class="ghost small edit" data-edit-section="${esc(x.name)}">Edit</button></div>`
          )
          .join("")
      : "";

    host.innerHTML =
      `<div class="hunt-head">
        <button class="primary-btn" data-add="item">+ Add item</button>
      </div>` +
      (itemCards || '<div class="config-form-empty">Nothing hunted yet — add an item.</div>') +
      `<div class="sechead">Sources &amp; plumbing</div>
       <div class="setgrid">${renderSourceCards()}</div>` +
      strayBlock;

    const sub = $("#hunting-sub");
    if (sub) {
      const sources = marketplaceNames().length;
      sub.textContent = `${items.length} item${items.length === 1 ? "" : "s"} · ${sources} source${
        sources === 1 ? "" : "s"
      }`;
    }
  };

  // Published so refreshSectionsFromBuffer, declared above this point, can
  // reach the renderer without tripping over the const's dead zone.
  state.renderConfigForm = renderConfigForm;

  const configFormBody = $("#config-form-body");
  if (configFormBody) {
    // Inline controls. Each writes one key and re-renders; nothing is staged,
    // so the TOML tab and the Save button stay the single source of truth.
    configFormBody.addEventListener("click", (e) => {
      const itemCard = e.target.closest(".item-card");
      if (itemCard) {
        const sectionName = itemCard.dataset.section;
        const fields = fieldsForSection(
          state.sections.find((s) => s.name === sectionName) || {}
        );

        const sw = e.target.closest("[data-toggle]");
        if (sw) {
          applyInline(sectionName, "enabled", fields.enabled === false);
          return;
        }

        const thr = e.target.closest("[data-thr]");
        if (thr) {
          let current = fields.rating;
          if (Array.isArray(current)) current = current[current.length - 1];
          const picked = Number(thr.dataset.thr);
          // Clicking the active level clears it, falling back to whatever the
          // marketplace sets -- otherwise there is no way back to inheriting.
          applyInline(sectionName, "rating", current === picked ? null : picked);
          return;
        }

        const src = e.target.closest("[data-src]");
        if (src) {
          const all = marketplaceNames();
          const bound = fields.marketplace ? [].concat(fields.marketplace) : all.slice();
          const name = src.dataset.src;
          const next = bound.includes(name)
            ? bound.filter((x) => x !== name)
            : bound.concat([name]);
          if (!next.length) {
            setEditorStatus(
              "An item needs at least one source — disable the item instead.",
              "error"
            );
            return;
          }
          // Omit the key entirely when every source is selected, so the config
          // keeps meaning "all marketplaces" rather than freezing today's list.
          const sameAsAll = next.length === all.length && all.every((x) => next.includes(x));
          applyInline(sectionName, "marketplace", sameAsAll ? null : next);
          return;
        }
      }

      const addBtn = e.target.closest("[data-add]");
      if (addBtn) {
        openAddSectionModal(addBtn.dataset.add);
        return;
      }

      const setupBtn = e.target.closest("[data-setup-marketplace]");
      if (setupBtn) {
        openAddSectionModal("marketplace", setupBtn.dataset.setupMarketplace);
        return;
      }

      const editLink = e.target.closest("[data-edit-section]");
      if (editLink) {
        openEditSectionModal(editLink.dataset.editSection);
        return;
      }

      const chipDel = e.target.closest("[data-chip-del]");
      if (chipDel) {
        const cardEl = chipDel.closest("[data-section]");
        if (!cardEl) return;
        const sec = state.sections.find((x) => x.name === cardEl.dataset.section);
        if (!sec) return;
        const key = chipDel.dataset.chipDel;
        const current = [].concat(fieldsForSection(sec)[key] || []);
        const next = current.filter((v) => v !== chipDel.dataset.chipVal);
        if (key === "search_phrases" && !next.length) {
          setEditorStatus("An item needs at least one search phrase.", "error");
          return;
        }
        applyInline(cardEl.dataset.section, key, next.length ? next : null);
        return;
      }

      const head = e.target.closest(".ihead");
      if (head && !e.target.closest(".sw") && !e.target.closest("button")) {
        const cardEl = head.closest("[data-section]");
        if (cardEl) {
          const name = cardEl.dataset.section;
          if (state.openItems.has(name)) state.openItems.delete(name);
          else state.openItems.add(name);
          renderConfigForm();
        }
        return;
      }
      const actionBtn = e.target.closest("[data-act]");
      if (!actionBtn) return;
      const card = actionBtn.closest("[data-section]");
      if (!card) return;
      const section = state.sections.find((s) => s.name === card.dataset.section);
      if (!section) return;
      const act = actionBtn.dataset.act;
      if (act === "edit") openEditSectionModal(section.name);
      else if (act === "duplicate") duplicateSection(section);
      else if (act === "delete") deleteSection(section);
    });
  }

  // Chips add on Enter; text fields commit on change (blur). Bare integers
  // write as TOML ints, empty clears the key.
  if (configFormBody) {
    configFormBody.addEventListener("keydown", (e) => {
      const input = e.target.closest("[data-chip-add]");
      if (!input || e.key !== "Enter") return;
      e.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      const cardEl = input.closest("[data-section]");
      const sec = state.sections.find((x) => x.name === cardEl.dataset.section);
      if (!sec) return;
      const key = input.dataset.chipAdd;
      const current = [].concat(fieldsForSection(sec)[key] || []);
      if (current.includes(value)) return;
      // Keep focus usable across the re-render: remember which card + key.
      const section = cardEl.dataset.section;
      applyInline(section, key, current.concat([value]));
      const again = configFormBody.querySelector(
        `[data-section="${CSS.escape(section)}"] [data-chip-add="${CSS.escape(key)}"]`
      );
      if (again) again.focus();
    });

    configFormBody.addEventListener(
      "change",
      (e) => {
        const field = e.target.closest("[data-field]");
        if (!field) return;
        const cardEl = field.closest("[data-section]");
        if (!cardEl) return;
        const key = field.dataset.field;
        const raw = field.value.trim();
        let value = null;
        if (raw) value = /^-?\d+$/.test(raw) ? parseInt(raw, 10) : raw;
        applyInline(cardEl.dataset.section, key, value);
      },
      true
    );
  }

  const showConfigView = (view) => {
    $$("[data-config-view]").forEach((btn) =>
      btn.classList.toggle("active", btn.dataset.configView === view)
    );
    const formView = $("#config-form-view");
    const tomlView = $("#config-toml-view");
    if (formView) formView.classList.toggle("hidden", view !== "form");
    if (tomlView) tomlView.classList.toggle("hidden", view !== "toml");
    if (view === "form") {
      renderConfigForm();
    } else if (editor.refresh) {
      // CodeMirror measures wrong if it was laid out while display:none.
      editor.refresh();
      renderGutter();
    }
  };

  $$("[data-config-view]").forEach((btn) => {
    btn.addEventListener("click", () => showConfigView(btn.dataset.configView));
  });

  // Help level lives on <body> so CSS alone decides what shows -- no re-render,
  // and it survives across every card without threading state through them.
  const helpSeg = $("#help-seg");
  if (helpSeg) {
    const applyHelpLevel = (level) => {
      document.body.classList.remove("help-hints", "help-guided");
      if (level !== "off") document.body.classList.add("help-" + level);
      $$("#help-seg button").forEach((b) =>
        b.classList.toggle("on", b.dataset.help === level)
      );
      try {
        localStorage.setItem("aimm.helpLevel", level);
      } catch (_) {
        /* private browsing: the choice just does not persist */
      }
    };
    helpSeg.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (btn) applyHelpLevel(btn.dataset.help);
    });
    let saved = "hints";
    try {
      saved = localStorage.getItem("aimm.helpLevel") || "hints";
    } catch (_) {
      /* ignore */
    }
    applyHelpLevel(saved);
  }

  // ---------------------------------------------------------------
  // Activity pane
  //
  // The log tail says what the monitor is doing; this says what it found and
  // what it thought. Data comes from /api/activity, which joins the on-disk
  // cache -- so it survives a reload and a restart, unlike the log buffer.
  // ---------------------------------------------------------------
  const activity = {
    summary: [],
    home: null, // [lat, lon] from home_location, for the pickup map
    sort: "score", // score | myrank | distance | newest
    listings: [],
    total: 0,
    truncated: false,
    verdict: "",
    item: "",
    filter: "",
    selected: null, // rowKey of the listing shown in the detail pane
    reloadTimer: null,
    loading: false,
  };

  const rowKey = (row) => `${row.marketplace}:${row.id}`;

  const loadActivity = async () => {
    if (activity.loading) return;
    activity.loading = true;
    try {
      const res = await api("/api/activity");
      if (!res.ok) return;
      const data = await res.json();
      activity.summary = data.summary || [];
      activity.home = data.home || null;
      activity.listings = data.listings || [];
      activity.total = data.total || 0;
      activity.truncated = !!data.truncated;
      syncActivityItemDropdown();
      renderActivity();
    } catch (err) {
      console.error("activity load failed", err);
    } finally {
      activity.loading = false;
    }
  };

  // A search burst emits one ai_eval per listing. Coalesce them so a run over
  // 40 listings triggers one reload instead of 40 cache scans.
  const scheduleActivityReload = () => {
    if (activity.reloadTimer) clearTimeout(activity.reloadTimer);
    activity.reloadTimer = setTimeout(() => {
      activity.reloadTimer = null;
      loadActivity();
    }, 4000);
  };

  const syncActivityItemDropdown = () => {
    /* The item dropdown was replaced by the clickable summary pills. */
  };

  // One compact pill per item — click filters the list to it. Replaces the
  // wide scorecards, which stopped scaling past a handful of items.
  const renderActivitySummary = () => {
    const host = $("#activity-summary");
    if (!host) return;
    if (!activity.summary.length) {
      host.innerHTML = "";
      return;
    }
    const ordered = activity.summary
      .slice()
      .sort((a, b) => Number(b.active !== false) - Number(a.active !== false));
    const pills = ordered
      .map((s) => {
        const active = activity.item === s.item;
        const paused = s.active === false;
        const tip =
          `${s.item}: ${s.examined} examined · ${s.promising} promising · ` +
          `${s.notified} notified · ${s.dismissed} dismissed` +
          (paused ? " · paused — click to view its history" : "");
        return `
        <button class="ipill ${active ? "on" : ""} ${paused ? "paused" : ""}" data-item-pill="${esc(
          s.item
        )}" title="${esc(tip)}">
          <span class="n">${esc(s.item)}</span>
          <span class="c">${s.examined}</span>
          ${s.promising ? `<span class="c warn">${s.promising}★</span>` : ""}
          ${s.notified ? `<span class="c ok">${s.notified}✓</span>` : ""}
          ${paused ? '<span class="c">⏸</span>' : ""}
        </button>`;
      })
      .join("");
    host.innerHTML =
      `<button class="ipill ${activity.item ? "" : "on"}" data-item-pill="">All items</button>` + pills;
  };

  const visibleActivityRows = () => {
    const needle = activity.filter.trim().toLowerCase();
    return activity.listings.filter((row) => {
      // "hidden" is user state, orthogonal to the AI verdict: off the radar
      // but fully tracked. Its own chip shows them; every other view hides.
      if (activity.verdict === "hidden") {
        if (!row.hidden) return false;
      } else {
        if (row.hidden) return false;
        if (activity.verdict && row.verdict !== activity.verdict) return false;
      }
      if (activity.item && row.item !== activity.item) return false;
      // Paused or since-removed items stay tracked but leave the default view.
      // Picking their pill is the explicit ask to see that history.
      if (!activity.item && row.item_active === false) return false;
      if (!needle) return true;
      return (
        (row.title || "").toLowerCase().includes(needle) ||
        (row.comment || "").toLowerCase().includes(needle)
      );
    });
  };

  const renderDealDetail = (row) => {
    const host = $("#deal-detail");
    if (!host) return;
    if (!row) {
      if (dealMap) {
        dealMap.remove();
        dealMap = null;
      }
      host.innerHTML =
        '<div class="dd-empty">Select a listing to read the AI\u2019s reasoning.</div>';
      return;
    }
    const scoreClass = row.score >= 4 ? "score-high" : row.score === 3 ? "score-mid" : "";
    const badge =
      row.verdict === "dismissed"
        ? '<span class="verdict-badge">dismissed</span>'
        : `<span class="verdict-badge ${row.verdict}">${row.verdict}</span>`;
    host.innerHTML = `
      <h2>${esc(row.title)}</h2>
      <div class="dd-sub">
        <span class="score-badge ${scoreClass}">${row.score}/5 ${esc(row.conclusion)}</span>
        ${badge}
        <span>${esc(row.marketplace)}</span>
        <span>${esc(row.item)}</span>
        ${row.notified_at ? `<span>notified ${esc(row.notified_at)}</span>` : ""}
      </div>
      <div class="dd-price">${esc(row.price)}${
        row.distance_mi != null ? `<span class="dist">${row.distance_mi} mi away</span>` : ""
      }<span id="dd-drive" class="drive"></span></div>
      <div class="dd-cols">
        <div class="dd-main">
          <div class="dd-kv">
            ${row.location ? `<span class="k">Location</span><span>${esc(row.location)}</span>` : ""}
            ${row.condition ? `<span class="k">Condition</span><span>${esc(row.condition)}</span>` : ""}
            ${row.seller ? `<span class="k">Seller</span><span>${esc(row.seller)}</span>` : ""}
            <span class="k">Threshold</span><span>notify at \u2265 ${row.threshold}</span>
          </div>
          <div class="dd-ai"><div class="h">Why the AI scored it ${row.score}/5${
            row.ai_name ? " \u00B7 " + esc(row.ai_name) : ""
          }</div>${esc(row.comment || "(no reasoning recorded)")}</div>
          <div class="dd-actions">
            ${row.url ? `<a class="primary" href="${esc(row.url)}" target="_blank" rel="noopener">Open listing \u2197</a>` : ""}
            <button class="ghost small" data-flag="hide">${row.hidden ? "Restore" : "Dismiss"}</button>
          </div>
          <div class="dd-myrank">
            <span class="k">My rating</span>
            <span class="stars" data-flag="rank">${[1, 2, 3, 4, 5]
              .map(
                (n) =>
                  `<button class="star ${row.my_rank >= n ? "on" : ""}" data-rank="${n}">${
                    row.my_rank >= n ? "\u2605" : "\u2606"
                  }</button>`
              )
              .join("")}</span>
            <span class="hint">${
              row.my_rank ? "click the same star to clear" : "your own read, separate from the AI's"
            }</span>
          </div>
        </div>
        <div class="dd-side">
          ${
            row.image
              ? `<img class="dd-photo" alt="listing photo" loading="lazy"
                   src="/api/listing-image?post=${encodeURIComponent(row.url)}"
                   onerror="this.remove()" />`
              : ""
          }
          ${
            row.coords && activity.home && row.marketplace === "facebook"
              ? '<div id="dd-map" class="dd-map"></div>'
              : ""
          }
        </div>
      </div>`;
  };

  // Leaflet map + OSRM drive time for the selected physical listing.
  // The map instance is torn down per selection; tiles are OSM's public
  // servers (attribution required), routing is OSRM's demo router — both
  // free for light personal use, cached server-side for a day.
  let dealMap = null;
  const mountDealExtras = (row) => {
    const mapHost = $("#dd-map");
    if (dealMap) {
      dealMap.remove();
      dealMap = null;
    }
    if (!mapHost || !window.L || !row.coords || !activity.home) return;
    const item = row.coords;
    const home = activity.home;
    dealMap = L.map(mapHost, { zoomControl: false, attributionControl: true });
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
    const hint = L.polyline([home, item], {
      weight: 2,
      opacity: 0.35,
      dashArray: "5,6",
    }).addTo(dealMap);
    dealMap.fitBounds([home, item], { padding: [30, 30] });

    const driveHost = $("#dd-drive");
    const fmt = (r) => {
      const h = Math.floor(r.minutes / 60);
      const m = r.minutes % 60;
      return `${h ? h + "h " : ""}${m}m \u00B7 ${r.miles} mi by road`;
    };
    const drawRoute = (r) => {
      if (driveHost) driveHost.textContent = fmt(r);
      if (!dealMap || !r.geometry || r.geometry.length < 2) return;
      dealMap.removeLayer(hint);
      const line = L.polyline(r.geometry, { weight: 5, opacity: 0.85 }).addTo(dealMap);
      dealMap.fitBounds(line.getBounds(), { padding: [26, 26] });
    };

    if (row._route) {
      drawRoute(row._route);
      return;
    }
    if (driveHost) driveHost.textContent = "estimating drive\u2026";
    api(`/api/route?to=${item[0]},${item[1]}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((r) => {
        if (!r) {
          if (driveHost) driveHost.textContent = "";
          return;
        }
        row._route = r;
        // The selection may have moved on while this was in flight.
        if (activity.selected === rowKey(row)) drawRoute(r);
      })
      .catch(() => {
        if (driveHost) driveHost.textContent = "";
      });
  };

  const sendFlag = async (row, payload) => {
    try {
      const res = await api("/api/listing/flag", {
        method: "POST",
        body: JSON.stringify({ marketplace: row.marketplace, id: row.id, ...payload }),
      });
      if (!res.ok) return;
      const flags = (await res.json()).flags || {};
      row.my_rank = flags.my_rank ?? null;
      row.hidden = !!flags.hidden;
      renderActivity();
    } catch (err) {
      console.error("flag update failed", err);
    }
  };

  const dealDetailHost = $("#deal-detail");
  if (dealDetailHost) {
    dealDetailHost.addEventListener("click", (e) => {
      const row = activity.listings.find((r) => rowKey(r) === activity.selected);
      if (!row) return;
      const star = e.target.closest(".star");
      if (star) {
        const picked = Number(star.dataset.rank);
        sendFlag(row, { my_rank: row.my_rank === picked ? null : picked });
        return;
      }
      const hide = e.target.closest('[data-flag="hide"]');
      if (hide) sendFlag(row, { hidden: !row.hidden });
    });
  }

  // Sorting is client-side so switching order never needs a refetch, and it
  // orders the full returned set rather than re-slicing the server's top-N.
  // Missing values sort last in every mode -- an unresolvable distance or an
  // un-stamped legacy row should not masquerade as the best hit.
  const SORTERS = {
    score: (a, b) => b.score - a.score || a.item.localeCompare(b.item),
    myrank: (a, b) => (b.my_rank || 0) - (a.my_rank || 0) || b.score - a.score,
    distance: (a, b) => {
      const av = a.distance_mi == null ? Infinity : a.distance_mi;
      const bv = b.distance_mi == null ? Infinity : b.distance_mi;
      return av - bv || b.score - a.score;
    },
    newest: (a, b) => (b.rated_at || 0) - (a.rated_at || 0) || b.score - a.score,
  };

  const renderActivity = () => {
    renderActivitySummary();
    const host = $("#activity-rows");
    if (!host) return;

    const rows = visibleActivityRows().sort(SORTERS[activity.sort] || SORTERS.score);
    const counter = $("#activity-count");
    if (counter) {
      counter.textContent = activity.total
        ? `${rows.length} of ${activity.total} rated`
        : "";
    }

    if (!rows.length) {
      host.innerHTML = `<div class="activity-empty">${
        activity.total
          ? "No listings match these filters."
          : "Nothing rated yet. Listings appear here once the AI has scored them."
      }</div>`;
      renderDealDetail(null);
      return;
    }

    // Keep the selection while it remains visible; otherwise fall back to the
    // top row, so the detail pane is never showing a filtered-out listing.
    let selectedRow = rows.find((r) => rowKey(r) === activity.selected);
    if (!selectedRow) {
      selectedRow = rows[0];
      activity.selected = rowKey(selectedRow);
    }

    host.innerHTML = rows
      .map((row) => {
        const key = rowKey(row);
        const scoreClass =
          row.score >= 4 ? "score-high" : row.score === 3 ? "score-mid" : "";
        return `
        <div class="dli verdict-${row.verdict} ${key === activity.selected ? "sel" : ""}" data-key="${esc(key)}">
          <div class="top">
            <span class="score-badge ${scoreClass}">${row.score}</span>
            <span class="t" title="${esc(row.title)}">${esc(row.title)}</span>
            ${row.my_rank ? `<span class="myrank">★${row.my_rank}</span>` : ""}
            <span class="p">${esc(row.price)}</span>
          </div>
          <div class="m">
            <span>${esc(row.item)}</span>
            <span>${esc(row.marketplace)}</span>
            ${row.distance_mi != null ? `<span>${row.distance_mi} mi</span>` : ""}
            ${row.location ? `<span>${esc(row.location)}</span>` : ""}
          </div>
        </div>`;
      })
      .join("");

    renderDealDetail(selectedRow);
    mountDealExtras(selectedRow);
  };

  // ---------------------------------------------------------------
  // App views -- the top nav is the router. Deals / Config / Logs / Status
  // each own the full width; switching views refreshes what the view shows.
  // ---------------------------------------------------------------
  const showAppView = (view) => {
    state.appView = view;
    $$("#app-nav button").forEach((btn) =>
      btn.classList.toggle("on", btn.dataset.appview === view)
    );
    ["deals", "config", "logs", "status"].forEach((name) => {
      const el = $("#view-" + name);
      if (el) el.classList.toggle("hidden", name !== view);
    });
    if (view === "deals") loadActivity();
    else if (view === "config") {
      // CodeMirror measures wrong if laid out while display:none.
      if (editor.refresh) editor.refresh();
      renderGutter();
      renderConfigForm();
    } else if (view === "logs") renderLogs();
    else if (view === "status") {
      loadMonitorState();
      loadEnvStatus();
    }
  };

  $$("#app-nav button").forEach((btn) => {
    btn.addEventListener("click", () => showAppView(btn.dataset.appview));
  });

  // ---------------------------------------------------------------
  // Monitor state: one poller feeds the header chip, the pause button,
  // and the Status page.
  // ---------------------------------------------------------------
  const fmtDur = (seconds) => {
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 90) return seconds + "s";
    if (seconds < 5400) return Math.round(seconds / 60) + "m";
    if (seconds < 172800) return (seconds / 3600).toFixed(1).replace(/\.0$/, "") + "h";
    return Math.round(seconds / 86400) + "d";
  };

  const nextJob = () => {
    const jobs = (state.monitorInfo && state.monitorInfo.jobs) || [];
    return jobs.find((j) => j.next_run) || null;
  };

  const renderPauseBtn = () => {
    const btn = $("#pause-btn");
    if (!btn) return;
    const info = state.monitorInfo;
    btn.hidden = !(info && info.available);
    if (info && info.available) {
      btn.textContent = info.paused ? "▶ Resume" : "⏸ Pause";
      btn.title = info.paused
        ? "Resume scheduled searches"
        : "Pause scheduled searches (schedule keeps ticking)";
    }
  };

  const loadMonitorState = async () => {
    try {
      const res = await api("/api/monitor/state");
      if (!res.ok) return;
      state.monitorInfo = await res.json();
      renderMonitorStatus();
      renderPauseBtn();
      if (state.appView === "status") renderStatusPage();
    } catch (err) {
      /* transient; next poll retries */
    }
  };

  const loadEnvStatus = async () => {
    try {
      const res = await api("/api/env-status");
      if (res.ok) {
        state.envVars = (await res.json()).vars || {};
        if (state.appView === "status") renderStatusPage();
      }
    } catch (err) {
      /* ignore */
    }
  };

  const pauseBtn = $("#pause-btn");
  if (pauseBtn) {
    pauseBtn.addEventListener("click", async () => {
      const paused = state.monitorInfo && state.monitorInfo.paused;
      try {
        const res = await api("/api/monitor/" + (paused ? "resume" : "pause"), {
          method: "POST",
        });
        if (res.ok) await loadMonitorState();
      } catch (err) {
        console.error(err);
      }
    });
  }

  const statusRefreshBtn = $("#status-refresh");
  if (statusRefreshBtn) {
    statusRefreshBtn.addEventListener("click", () => {
      loadMonitorState();
      loadEnvStatus();
    });
  }

  // ---------------------------------------------------------------
  // Status page renderer. Stat tiles + tables. Status colors always ride
  // with a word (never color alone); values wear text tokens.
  // ---------------------------------------------------------------
  const renderStatusPage = () => {
    const host = $("#status-body");
    if (!host) return;
    const info = state.monitorInfo;
    if (!info) {
      host.innerHTML = '<div class="status-loading">Loading…</div>';
      return;
    }
    $("#status-updated").textContent = "updated " + new Date().toLocaleTimeString();

    const act = info.activity || {};
    const fb = info.fb_session || {};
    const now = Date.now() / 1000;

    const blocks = activeBlocks(info, now);
    let monDot = "ok";
    let monText = "Idle";
    if (!info.available) {
      monDot = "dim";
      monText = "Not attached";
    } else if (blocks.length) {
      monDot = "err";
      monText = "Blocked by " + blocks.map((b) => b.marketplace).join(", ");
    } else if (info.paused) {
      monDot = "warn";
      monText = "Paused";
    } else if (act.state === "searching") {
      monText = "Searching " + (act.item || "");
    } else if (act.state === "starting") {
      monDot = "warn";
      monText = "Starting";
    }
    const upFor = info.started_at ? fmtDur(now - info.started_at) : "?";

    let fbDot = "dim";
    let fbText = "No saved session";
    let fbDetail =
      "Log in once through the Browser view; the session persists after that.";
    if (fb.logged_in) {
      fbDot = "ok";
      fbText = "Signed in";
      fbDetail =
        "Session saved " +
        (fb.saved_at ? fmtDur(now - fb.saved_at) + " ago" : "") +
        " · survives restarts.";
    } else if (fb.exists) {
      fbDot = "warn";
      fbText = "Anonymous session";
      fbDetail =
        "A state file exists but holds no login — complete the Facebook login via the Browser view.";
    }

    const nj = nextJob();
    const njText = nj
      ? esc(nj.item) +
        " · in " +
        fmtDur(new Date(nj.next_run).getTime() / 1000 - now)
      : "—";

    const counters = info.counters || {};
    const notifTotals = counters["Notifications sent"] || {};
    const notifSum = Object.values(notifTotals).reduce((a, b) => a + b, 0);

    const tiles = `
      <div class="status-grid">
        <div class="stile"><div class="t">Monitor</div>
          <div class="v"><span class="state-dot ${monDot}"></span>${esc(monText)}</div>
          <div class="d">up ${esc(upFor)} · browser ${
            info.browser_active ? "running" : "not running"
          }</div>
        </div>
        <div class="stile"><div class="t">Facebook session</div>
          <div class="v"><span class="state-dot ${fbDot}"></span>${esc(fbText)}</div>
          <div class="d">${fbDetail}</div>
        </div>
        <div class="stile"><div class="t">Next search</div>
          <div class="v">${njText}</div>
          <div class="d">${nj ? esc(new Date(nj.next_run).toLocaleString()) : "no scheduled jobs"}</div>
        </div>
        <div class="stile"><div class="t">Notifications sent</div>
          <div class="v">${notifSum}</div>
          <div class="d">${
            Object.keys(notifTotals)
              .map((k) => esc(k) + ": " + notifTotals[k])
              .join(" · ") || "none yet"
          }</div>
        </div>
      </div>`;

    const jobs = info.jobs || [];
    const scheduleTable = jobs.length
      ? `<div class="status-section">Schedule</div>
         <table class="status-table"><thead><tr><th>Item</th><th>Last run</th><th>Next run</th></tr></thead><tbody>` +
        jobs
          .map(
            (j) => `<tr>
            <td>${esc(j.item)}</td>
            <td>${
              j.last_run
                ? esc(fmtDur(now - new Date(j.last_run).getTime() / 1000)) + " ago"
                : "—"
            }</td>
            <td>${
              j.next_run
                ? "in " + esc(fmtDur(new Date(j.next_run).getTime() / 1000 - now))
                : "—"
            }</td>
          </tr>`
          )
          .join("") +
        `</tbody></table>`
      : "";

    const counterCols = [
      ["Search performed", "Searches"],
      ["Total listing examined", "Examined"],
      ["New AI Queries", "AI rated"],
      ["Notifications sent", "Notified"],
    ];
    const itemNames = new Set();
    counterCols.forEach(([key]) =>
      Object.keys(counters[key] || {}).forEach((n) => itemNames.add(n))
    );
    const countTable = itemNames.size
      ? `<div class="status-section">Totals</div>
         <table class="status-table"><thead><tr><th>Item</th>${counterCols
           .map(([, label]) => `<th style="text-align:right">${label}</th>`)
           .join("")}</tr></thead><tbody>` +
        Array.from(itemNames)
          .sort()
          .map(
            (name) =>
              `<tr><td>${esc(name)}</td>${counterCols
                .map(([key]) => `<td class="num">${(counters[key] || {})[name] || 0}</td>`)
                .join("")}</tr>`
          )
          .join("") +
        `</tbody></table>`
      : "";

    const envVars = state.envVars;
    const env =
      envVars && Object.keys(envVars).length
        ? `<div class="status-section">Environment variables referenced by the config</div>` +
          Object.keys(envVars)
            .map(
              (name) =>
                `<div class="envline"><span class="${envVars[name] ? "okv" : "bad"}">${
                  envVars[name] ? "✓" : "✗"
                }</span><span>${esc(name)}</span><span class="${
                  envVars[name] ? "okv" : "bad"
                }">${envVars[name] ? "set" : "not set"}</span></div>`
            )
            .join("")
        : "";

    host.innerHTML = tiles + renderBlockNotice(info, now) + scheduleTable + countTable + env;
  };

  // Rendered as its own section rather than a fifth stat tile: it is an
  // action, not a number, and it should only exist while it applies.
  const renderBlockNotice = (info, now) => {
    const blocks = activeBlocks(info, now);
    if (!blocks.length) return "";
    const t = now === undefined || now === null ? Date.now() / 1000 : now;
    return (
      `<div class="status-section">Blocked marketplaces</div>` +
      `<table class="status-table"><thead><tr><th>Marketplace</th><th>Signal</th>` +
      `<th>Retry</th><th></th></tr></thead><tbody>` +
      blocks
        .map(
          (b) => `<tr>
            <td>${esc(b.marketplace || "")}</td>
            <td>${esc(b.reason || "unknown")}</td>
            <td>in ${esc(fmtDur((b.until || t) - t))}${
              b.strikes > 1 ? ` · strike ${b.strikes}` : ""
            }</td>
            <td><button class="ghost small" data-clear-block="${esc(
              b.marketplace || ""
            )}">Clear block / retry now</button></td>
          </tr>`
        )
        .join("") +
      `</tbody></table>`
    );
  };

  // Exposed for the QA harness: these decide whether the monitor is reported
  // as blocked, and are worth asserting without provoking a real block.
  window.__aimm = Object.assign(window.__aimm || {}, {
    parseDuration,
    fmtCadence,
    activeBlocks,
    blockChipLabel,
    renderBlockNotice,
  });

  const statusBody = $("#status-body");
  if (statusBody) {
    statusBody.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-clear-block]");
      if (!btn) return;
      btn.disabled = true;
      try {
        const res = await api("/api/monitor/clear-block", {
          method: "POST",
          body: JSON.stringify({ marketplace: btn.dataset.clearBlock }),
        });
        if (res.ok) await loadMonitorState();
      } catch (err) {
        console.error(err);
      } finally {
        btn.disabled = false;
      }
    });
  }

  $$(".verdict-chips .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      activity.verdict = chip.dataset.verdict || "";
      $$(".verdict-chips .chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      renderActivity();
    });
  });

  const activitySummaryHost = $("#activity-summary");
  if (activitySummaryHost) {
    activitySummaryHost.addEventListener("click", (e) => {
      const pill = e.target.closest("[data-item-pill]");
      if (!pill) return;
      const picked = pill.dataset.itemPill;
      // Clicking the active pill returns to All.
      activity.item = activity.item === picked ? "" : picked;
      renderActivity();
    });
  }

  const activityFilterInput = $("#activity-filter");
  if (activityFilterInput) {
    activityFilterInput.addEventListener("input", (e) => {
      activity.filter = e.target.value;
      renderActivity();
    });
  }

  const dealSort = $("#deal-sort");
  if (dealSort) {
    dealSort.addEventListener("change", (e) => {
      activity.sort = e.target.value;
      try {
        localStorage.setItem("aimm.dealSort", activity.sort);
      } catch (_) {
        /* private browsing: the choice just does not persist */
      }
      renderActivity();
    });
    try {
      const saved = localStorage.getItem("aimm.dealSort");
      if (saved && SORTERS[saved]) {
        activity.sort = saved;
        dealSort.value = saved;
      }
    } catch (_) {
      /* ignore */
    }
  }

  const activityRefreshBtn = $("#activity-refresh");
  if (activityRefreshBtn) {
    activityRefreshBtn.addEventListener("click", () => loadActivity());
  }

  const activityRowsHost = $("#activity-rows");
  if (activityRowsHost) {
    activityRowsHost.addEventListener("click", (e) => {
      const row = e.target.closest(".dli");
      if (!row) return;
      activity.selected = row.dataset.key;
      renderActivity();
    });
  }

  const logClearBtn = $("#log-clear");
  if (logClearBtn) {
    logClearBtn.addEventListener("click", () => {
      state.records = [];
      state.errorCount = 0;
      renderLogs();
      renderErrorBadge();
    });
  }

  const logDownloadBtn = $("#log-download");
  if (logDownloadBtn) {
    logDownloadBtn.addEventListener("click", () => {
      // Plain navigation so the browser handles the attachment download;
      // the session cookie rides along automatically.
      window.location.href = "/api/logs/download";
    });
  }

  const connectWs = () => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    state.ws = ws;
    ws.onopen = () => {
      state.wsConnected = true;
      $("#ws-status").textContent = "● streaming";
      renderMonitorStatus();
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "log") {
        state.records.push(msg.record);
        updateItemDropdown(msg.record);
        noteActivity(msg.record);
        if (state.records.length > 5000) state.records.shift();
        renderLogs();
        renderMonitorStatus();
        // A new rating or a finished search changes what the activity pane
        // should show; the cache is already written by the time this arrives.
        const kind = msg.record && msg.record.extra && msg.record.extra.kind;
        if (kind === "ai_eval" || kind === "search_summary") scheduleActivityReload();
      }
    };
    ws.onclose = () => {
      state.wsConnected = false;
      $("#ws-status").textContent = "● disconnected — retrying…";
      renderMonitorStatus();
      setTimeout(connectWs, 2000);
    };
    ws.onerror = () => {
      ws.close();
    };
  };

  // ---------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------
  const bootstrap = async () => {
    try {
      await loadConfig();
      // CodeMirror needs a refresh after becoming visible (the editor
      // host is hidden during the login screen).
      if (editor.refresh) editor.refresh();
      // The Form tab is the default, so it has to be populated before the user
      // ever clicks a tab.
      renderConfigForm();
      await loadLogs();
      await loadActivity();
      connectWs();
      loadMonitorState();
      loadEnvStatus();
      if (!state._monitorPoll) {
        state._monitorPoll = setInterval(loadMonitorState, 10000);
      }
    } catch (err) {
      console.error(err);
    }
  };

  // If we already have a session cookie from a prior visit, try bootstrapping.
  (async () => {
    try {
      const res = await fetch("/api/status", { credentials: "same-origin" });
      if (res.ok) {
        state.csrf = getCookie("aimm_csrf");
        try {
          const status = await res.clone().json();
          const browserBtn = document.getElementById("browser-btn");
          if (browserBtn && status && status.vnc_enabled) browserBtn.hidden = false;
          if (status && Array.isArray(status.marketplaces)) {
            state.supportedMarketplaces = status.marketplaces;
          }
        } catch (_) {}
        hideLogin();
        await bootstrap();
      } else {
        showLogin();
      }
    } catch (err) {
      showLogin();
    }
  })();
})();
