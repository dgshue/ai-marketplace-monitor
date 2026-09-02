// AI Marketplace Monitor — Triage UI, status module.
// Grouped-list Status screen (monitor state, blocks, counters, schedule,
// session, environment) plus the live log panel with its filters.
(() => {
  const { $, $$, esc, state, on, fmtDur, fmtClock, activeBlocks, nextJob, togglePause, searchNow, clearBlock } = window.AIMM;

  // ---------------------------------------------------------------
  // Block notice — a pure renderer so the QA harness can assert it against
  // a stubbed payload without provoking a real Facebook block.
  // ---------------------------------------------------------------
  const renderBlockNotice = (info, now) => {
    const blocks = activeBlocks(info, now);
    if (!blocks.length) return "";
    const t = now === undefined || now === null ? Date.now() / 1000 : now;
    return (
      `<div class="gl">Blocked marketplaces</div>` +
      blocks
        .map(
          (b) => `<div class="blkrow" data-block="${esc(b.marketplace || "")}">
            <div class="hd">⛔ ${esc(b.marketplace || "")}: blocked${b.strikes > 1 ? ` · strike ${b.strikes}` : ""}</div>
            <p>${esc(b.reason || "unknown signal")}${b.detected_at ? ` · detected ${esc(fmtClock(b.detected_at))}` : ""} · automatic retry in <b>${esc(fmtDur((b.until || t) - t))}</b> (at ${esc(fmtClock(b.until || t))}).</p>
            <button class="btn" data-clear-block="${esc(b.marketplace || "")}">Clear block / retry now</button>
          </div>`
        )
        .join("")
    );
  };

  const renderStatus = () => {
    const host = $("#status-body");
    const info = state.monitorInfo;
    if (!info) {
      host.innerHTML = '<div class="g"><div class="r"><div class="l muted">Loading…</div></div></div>';
      return;
    }
    const act = info.activity || {};
    const fb = info.fb_session || {};
    const now = Date.now() / 1000;
    const blocks = activeBlocks(info, now);

    let monCls = "c-ok";
    let monText = "Idle";
    if (!info.available) {
      monCls = "muted";
      monText = "Not attached";
    } else if (blocks.length) {
      monCls = "c-err";
      monText = "Blocked by " + blocks.map((b) => b.marketplace).join(", ");
    } else if (info.paused) {
      monCls = "c-warn";
      monText = "Paused";
    } else if (act.state === "searching") {
      monText = "Searching " + (act.item || "");
    } else if (act.state === "starting") {
      monCls = "c-warn";
      monText = "Starting";
    }
    const jobs = info.jobs || [];
    const nj = nextJob();
    const nextText = nj ? `${nj.item} ≈ ${fmtClock(new Date(nj.next_run).getTime() / 1000)} · in ${fmtDur(new Date(nj.next_run).getTime() / 1000 - now)}` : "no scheduled jobs";
    const counters = info.counters || {};
    const notifTotals = counters["Notifications sent"] || {};
    const examined = counters["Total listing examined"] || {};
    const rated = counters["New AI Queries"] || {};
    const searched = counters["Search performed"] || {};
    const itemNames = new Set([...Object.keys(examined), ...Object.keys(rated), ...Object.keys(notifTotals), ...Object.keys(searched)]);
    const persistedMismatch = info.available && !!info.paused !== !!info.paused_persisted;

    const monitorGroup = `
      <div class="g">
        <div class="r"><div class="l">Monitor</div><span class="v val ${monCls}">${esc(monText)}</span></div>
        ${act.state === "searching" && act.progress ? `<div class="r"><div class="l">Progress</div><span class="v">${esc(act.progress)}</span></div>` : ""}
        <div class="r"><div class="l">Next</div><span class="v">${esc(nextText)}</span></div>
        <div class="r"><div class="l">Uptime</div><span class="v">${info.started_at ? esc(fmtDur(now - info.started_at)) : "—"} · browser ${info.browser_active ? "running" : "not running"}</span></div>
        ${persistedMismatch ? `<div class="r"><div class="l c-warn">Pause state not saved<small class="wrap">The on-disk state file disagrees with the running monitor; a restart would ${info.paused_persisted ? "resume paused" : "resume searching"}.</small></div></div>` : ""}
      </div>
      <div class="btnrow">
        <button class="btn" id="status-pause" ${info.available ? "" : "disabled"}>${info.paused ? "Resume" : "Pause"}</button>
        <button class="btn pri" id="status-search">Search now</button>
      </div>`;

    const counterGroup = itemNames.size
      ? `<div class="gl">Counters</div><div class="g">${Array.from(itemNames)
          .sort()
          .map((name) => {
            const bits = [];
            if (searched[name]) bits.push(`${searched[name]} searches`);
            bits.push(`${examined[name] || 0} seen`);
            bits.push(`${rated[name] || 0} rated`);
            if (notifTotals[name]) bits.push(`${notifTotals[name]} notified`);
            return `<div class="r"><div class="l">${esc(name)}</div><span class="v">${esc(bits.join(" · "))}</span></div>`;
          })
          .join("")}</div>`
      : "";

    const scheduleGroup = jobs.length
      ? `<div class="gl">Schedule</div><div class="g">${jobs
          .map(
            (j) => `<div class="r"><div class="l">${esc(j.item)}<small>${j.last_run ? "last " + esc(fmtDur(now - new Date(j.last_run).getTime() / 1000)) + " ago" : "not run yet"}</small></div><span class="v">${j.next_run ? "in " + esc(fmtDur(new Date(j.next_run).getTime() / 1000 - now)) + " · " + esc(fmtClock(new Date(j.next_run).getTime() / 1000)) : "—"}</span></div>`
          )
          .join("")}</div>`
      : "";

    let fbDot = "dim";
    let fbText = "no saved session";
    if (fb.logged_in) {
      fbDot = "ok";
      fbText = "signed in" + (fb.saved_at ? " · saved " + fmtDur(now - fb.saved_at) + " ago" : "");
    } else if (fb.exists) {
      fbDot = "warn";
      fbText = "anonymous session — log in via the browser";
    }
    const st = state.status || {};
    const envVars = state.envVars || {};
    const envRows = Object.keys(envVars)
      .map((name) => `<div class="r envline"><div class="l">${esc(name)}</div><span class="v ${envVars[name] ? "okv" : "bad"}">${envVars[name] ? "✓ set" : "✗ not set"}</span></div>`)
      .join("");
    const sessionGroup = `
      <div class="gl">Session</div>
      <div class="g">
        <div class="r"><div class="l">Facebook</div><span class="v">${esc(fbText)}</span><span class="dot ${fbDot}"></span></div>
        ${st.vnc_enabled ? `<a class="r tap" href="/vnc/vnc.html?path=ws/vnc&autoconnect=1&resize=scale" target="_blank" rel="noopener"><div class="l">Browser (noVNC)<small>sign in or clear a 2FA prompt</small></div><span class="v">open</span><span class="chev">›</span></a>` : ""}
        <div class="r"><div class="l">Log stream</div><span class="v">${state.wsConnected ? "streaming" : "disconnected"}</span><span class="dot ${state.wsConnected ? "ok" : "err"}"></span></div>
        <div class="r"><div class="l">Sign-in</div><span class="v">${esc(st.auth_mode || "—")}</span></div>
        ${(st.config_files || []).map((f) => `<div class="r"><div class="l">Config<small>${esc(f.path)}</small></div><span class="v">${esc(fmtDur(now - (f.mtime || now)))} ago</span></div>`).join("")}
        ${(st.urls || []).length ? `<div class="r"><div class="l">Serving<small class="wrap">${(st.urls || []).map(esc).join(" · ")}</small></div></div>` : ""}
      </div>
      ${envRows ? `<div class="gl">Environment variables referenced by the config</div><div class="g">${envRows}</div>` : ""}`;

    host.innerHTML = monitorGroup + renderBlockNotice(info, now) + counterGroup + scheduleGroup + sessionGroup;
  };

  $("#status-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-clear-block]");
    if (btn) {
      btn.disabled = true;
      await clearBlock(btn.dataset.clearBlock);
      return;
    }
    if (e.target.closest("#status-pause")) togglePause();
    else if (e.target.closest("#status-search")) searchNow();
  });
  $("#status-refresh").addEventListener("click", () => {
    window.AIMM.loadMonitorState();
    window.AIMM.loadEnvStatus();
  });

  // ---------------------------------------------------------------
  // Logs panel
  // ---------------------------------------------------------------
  const L = { level: "ALL", kind: "", item: "", minScore: null, text: "", expanded: new Set(), knownItems: new Set() };
  const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
  const matches = (r) =>
    (L.level === "ALL" || LEVEL_ORDER[r.level] >= LEVEL_ORDER[L.level]) &&
    (!L.text || (r.message || "").toLowerCase().includes(L.text.toLowerCase())) &&
    (!L.kind || (r.extra && r.extra.kind === L.kind)) &&
    (!L.item || (r.extra && r.extra.item === L.item)) &&
    (L.minScore == null || (r.extra && typeof r.extra.score === "number" && r.extra.score >= L.minScore));

  const noteItem = (record) => {
    const item = record.extra && record.extra.item;
    if (!item || L.knownItems.has(item)) return;
    L.knownItems.add(item);
    const opt = document.createElement("option");
    opt.value = item;
    opt.textContent = item;
    $("#item-filter").appendChild(opt);
  };

  const renderDetail = (r) => {
    const lines = [`<dl><dt>logger</dt><dd>${esc(r.logger)}</dd><dt>source</dt><dd>${esc(r.location)}</dd></dl>`];
    if (r.extra) {
      const rows = Object.entries(r.extra)
        .map(([k, v]) => {
          if (k === "url" && typeof v === "string") return `<dt>${esc(k)}</dt><dd><a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a></dd>`;
          return `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : String(v))}</dd>`;
        })
        .join("");
      lines.push(`<dl>${rows}</dl>`);
    }
    if (r.exc_text) lines.push(`<pre>${esc(r.exc_text)}</pre>`);
    return `<div class="log-detail">${lines.join("")}</div>`;
  };

  const renderLogs = () => {
    const container = $("#logs");
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 16;
    const visible = state.records.filter(matches);
    container.innerHTML = visible.length
      ? visible
          .map((r) => {
            const expanded = L.expanded.has(r.id);
            const kind = r.extra && r.extra.kind;
            const badge = kind ? `<span class="kind-badge kind-${esc(kind)}">${esc(kind.replace(/_/g, " "))}</span>` : "";
            return (
              `<div class="log-row level-${esc(r.level)}${expanded ? " expanded" : ""}" data-id="${r.id}">` +
              `<span class="log-time">${esc(r.iso_time)}</span><span class="log-level">${esc(r.level)}</span>` +
              `<span class="log-msg">${badge}${esc(r.message)}</span>${expanded ? renderDetail(r) : ""}</div>`
            );
          })
          .join("")
      : `<div class="logs-empty">${state.records.length ? "No log lines match these filters." : "No log lines yet."}</div>`;
    if ($("#autoscroll").checked && (atBottom || state.records.length < 20)) container.scrollTop = container.scrollHeight;
    const errorChip = $('.level-chips [data-level="ERROR"]');
    if (state.errorCount > 0) {
      errorChip.dataset.badge = state.errorCount;
      errorChip.classList.add("has-badge");
    } else {
      delete errorChip.dataset.badge;
      errorChip.classList.remove("has-badge");
    }
  };

  $("#logs").addEventListener("click", (e) => {
    const row = e.target.closest(".log-row");
    if (!row) return;
    const id = Number(row.dataset.id);
    if (L.expanded.has(id)) L.expanded.delete(id);
    else L.expanded.add(id);
    renderLogs();
  });
  $$(".level-chips .chip").forEach((btn) =>
    btn.addEventListener("click", () => {
      $$(".level-chips .chip").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      L.level = btn.dataset.level;
      if (L.level === "ERROR" || L.level === "ALL") state.errorCount = 0;
      renderLogs();
    })
  );
  $$(".kind-chips .chip").forEach((btn) =>
    btn.addEventListener("click", () => {
      $$(".kind-chips .chip").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      L.kind = btn.dataset.kind;
      renderLogs();
    })
  );
  $("#item-filter").addEventListener("change", (e) => {
    L.item = e.target.value;
    renderLogs();
  });
  $("#score-filter").addEventListener("change", (e) => {
    L.minScore = e.target.value === "" ? null : Number(e.target.value);
    renderLogs();
  });
  $("#log-filter").addEventListener("input", (e) => {
    L.text = e.target.value;
    renderLogs();
  });
  $("#log-clear").addEventListener("click", () => {
    state.records = [];
    state.errorCount = 0;
    renderLogs();
  });
  $("#log-download").addEventListener("click", () => {
    // Plain navigation so the browser handles the attachment download;
    // the session cookie rides along automatically.
    window.location.href = "/api/logs/download";
  });

  const visible = () => state.view === "status";
  on("logs", () => {
    state.records.forEach(noteItem);
    if (visible()) renderLogs();
  });
  on("log", (record) => {
    noteItem(record);
    if (visible()) renderLogs();
  });
  on("monitor", () => visible() && renderStatus());
  on("env", () => visible() && renderStatus());
  on("ws", () => visible() && renderStatus());

  window.AIMM.views.status = {
    show: () => {
      renderStatus();
      renderLogs();
      window.AIMM.loadMonitorState();
      window.AIMM.loadEnvStatus();
    },
  };
  window.__aimm = Object.assign(window.__aimm || {}, { renderBlockNotice, renderStatus, logFilters: L });
})();
