// AI Marketplace Monitor — Triage UI, config module.
// Items screen (grouped-list item editor), Sources screen, the TOML editor
// (CodeMirror + toml-edit-js round-tripping) and the section form modal with
// FORM_SCHEMAS. Every control writes into ONE buffer -- the TOML the editor
// holds -- so Form and TOML are two views of the same text.
(() => {
  const { $, $$, esc, api, state, on, emit, toast, parseDuration, fmtCadence, fmtDur, fmtClock } = window.AIMM;

  const C = {
    fileId: "primary",
    baseMtime: null,
    originalContent: "",
    currentContent: "",
    sections: [],
    mode: "form", // form | toml
    openItem: null, // section name of the item being edited, or null for the list
    autosaveTimer: null,
    footText: "",
    footErr: false,
  };
  window.AIMM.config = C;

  // ---------------------------------------------------------------
  // Editor — CodeMirror 5 with TOML highlighting, textarea fallback
  // ---------------------------------------------------------------
  const editorHost = $("#editor-host");
  let editor;
  let validateTimer = null;
  const setEditorStatus = (msg, cls = "") => {
    const el = $("#editor-status");
    el.className = "editor-status " + cls;
    el.textContent = msg;
  };
  const onEditorChange = () => {
    C.currentContent = editor.getValue();
    const dirty = C.currentContent !== C.originalContent;
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
    editor.getValue = editor.getValue.bind(editor);
    editor.setValue = editor.setValue.bind(editor);
    editor.getScrollInfo = editor.getScrollInfo.bind(editor);
  } else {
    const textarea = document.createElement("textarea");
    textarea.className = "aimm-editor";
    textarea.spellcheck = false;
    editorHost.appendChild(textarea);
    editor = {
      getValue: () => textarea.value,
      setValue: (v) => {
        textarea.value = v;
      },
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
  // Load / validate / save
  // ---------------------------------------------------------------
  const loadConfig = async () => {
    const files = await (await api("/api/config/files")).json();
    if (!files.files.length) return;
    const f = files.files[0];
    C.fileId = f.id;
    $("#config-name").textContent = f.path;
    const res = await (await api(`/api/config/file/${f.id}`)).json();
    C.originalContent = res.content;
    C.currentContent = res.content;
    C.baseMtime = res.mtime;
    editor.setValue(res.content);
    C.sections = Array.isArray(res.sections) && res.sections.length ? res.sections : scanSectionsClient(res.content);
    renderGutter();
    $("#save-btn").disabled = true;
    if (res.has_masked_secrets) {
      setEditorStatus(`🔒 Secrets masked as "${res.mask_token}" — leave them alone to preserve, or type over to replace.`, "ok");
    } else setEditorStatus("");
    rerender();
    emit("config");
  };

  const validateConfig = async () => {
    setEditorStatus("validating…");
    try {
      const res = await api("/api/config/validate", { method: "POST", body: JSON.stringify({ content: C.currentContent }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setEditorStatus("✗ " + (data.detail || `HTTP ${res.status}`), "err");
        return false;
      }
      if (data.valid) {
        setEditorStatus("✓ config is valid", "ok");
        return true;
      }
      setEditorStatus("✗ " + (data.error || "invalid"), "err");
      return { error: data.error || "invalid" };
    } catch (err) {
      setEditorStatus("✗ validate failed: " + err.message, "err");
      return false;
    }
  };

  const saveConfig = async () => {
    if (validateTimer) {
      clearTimeout(validateTimer);
      validateTimer = null;
    }
    setEditorStatus("saving…");
    setFoot("Saving…");
    let res;
    let data;
    try {
      res = await api(`/api/config/file/${C.fileId}`, {
        method: "PUT",
        body: JSON.stringify({ content: C.currentContent, base_mtime: C.baseMtime }),
      });
      data = await res.json().catch(() => ({}));
    } catch (err) {
      setEditorStatus("✗ save failed: " + err.message, "err");
      setFoot("✗ save failed: " + err.message, true);
      return false;
    }
    if (!res.ok || !data.ok) {
      const msg = data.error || data.detail || `HTTP ${res.status}`;
      setEditorStatus("✗ " + msg, "err");
      setFoot("✗ not saved: " + msg, true);
      if (res.status === 409 && confirm("Config was modified on disk. Reload from disk and lose your changes?")) {
        await loadConfig();
      }
      return false;
    }
    C.originalContent = C.currentContent;
    C.baseMtime = data.mtime;
    $("#save-btn").disabled = true;
    setEditorStatus("✓ saved — monitor will reload within 1s", "ok");
    setFoot("Changes save automatically · saved " + fmtClock(Date.now() / 1000));
    emit("config");
    return true;
  };
  $("#save-btn").addEventListener("click", saveConfig);

  // Inline controls on the Items screen write the buffer and then save on
  // their own after a short pause (mockup: "Changes save automatically").
  const setFoot = (text, err) => {
    C.footText = text;
    C.footErr = !!err;
    const el = $("#item-foot");
    if (el) {
      el.textContent = text;
      el.classList.toggle("err", !!err);
    }
  };
  const scheduleAutosave = () => {
    if (C.autosaveTimer) clearTimeout(C.autosaveTimer);
    setFoot("Saving…");
    C.autosaveTimer = setTimeout(async () => {
      C.autosaveTimer = null;
      if (C.currentContent === C.originalContent) {
        setFoot("Changes save automatically · nothing to save");
        return;
      }
      const ok = await validateConfig();
      if (ok === true) await saveConfig();
      else setFoot("✗ not saved: " + ((ok && ok.error) || "config invalid") + " — fix it in the TOML view", true);
    }, 900);
  };

  // ---------------------------------------------------------------
  // Sections: header scan, gutter buttons, ⋯ menu
  // ---------------------------------------------------------------
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

  const getLineMetrics = () => {
    if (editor.defaultTextHeight) {
      return { lineHeight: editor.defaultTextHeight(), paddingTop: 0, scrollHeight: editor.getScrollInfo().height };
    }
    const el = editorHost.querySelector("textarea");
    if (!el) return { lineHeight: 20, paddingTop: 0, scrollHeight: 0 };
    const cs = window.getComputedStyle(el);
    return {
      lineHeight: parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.55,
      paddingTop: parseFloat(cs.paddingTop) || 0,
      scrollHeight: el.scrollHeight,
    };
  };
  const renderGutter = () => {
    const inner = $("#gutter-inner");
    inner.innerHTML = "";
    const { lineHeight, paddingTop, scrollHeight } = getLineMetrics();
    inner.style.height = scrollHeight + "px";
    C.sections.forEach((section) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "section-btn";
      btn.textContent = "⋯";
      btn.title = `[${section.name}]`;
      btn.style.top = paddingTop + lineHeight * section.line_start + "px";
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleSectionMenu(section, btn);
      });
      inner.appendChild(btn);
    });
  };
  const syncGutter = () => {
    $("#gutter-inner").style.transform = `translateY(${-editor.getScrollInfo().top}px)`;
  };
  if (editor.on) {
    editor.on("scroll", syncGutter);
    editor.on("change", () => {
      // Inline controls rescan synchronously; a second, debounced re-render
      // would only steal focus from the field the user moved on to.
      if (C.suppressRescan) return;
      if (refreshSectionsFromBuffer._t) clearTimeout(refreshSectionsFromBuffer._t);
      refreshSectionsFromBuffer._t = setTimeout(refreshSectionsFromBuffer, 150);
    });
  }
  const refreshSectionsFromBuffer = () => {
    C.sections = scanSectionsClient(editor.getValue());
    renderGutter();
    rerender();
  };

  const closeSectionMenus = () => $$(".section-menu").forEach((m) => m.remove());
  const toggleSectionMenu = (section, btn) => {
    const existing = $(".section-menu");
    if (existing && existing.dataset.section === section.name) {
      existing.remove();
      return;
    }
    closeSectionMenus();
    const menu = document.createElement("div");
    menu.className = "section-menu";
    menu.dataset.section = section.name;
    const rect = btn.getBoundingClientRect();
    menu.style.top = rect.bottom + 4 + "px";
    menu.style.left = rect.left + "px";
    const add = (label, handler, cls = "") => {
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
    add("Edit", () => openEditSectionModal(section.name));
    add("Duplicate", () => duplicateSection(section));
    const sep = document.createElement("div");
    sep.className = "menu-sep";
    menu.appendChild(sep);
    add("Delete", () => deleteSection(section), "danger");
    document.body.appendChild(menu);
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".section-btn") && !e.target.closest(".section-menu")) closeSectionMenus();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSectionMenus();
  });

  const deleteSection = (section, opts = {}) => {
    const lines = editor.getValue().split("\n");
    let start = section.line_start;
    const end = section.line_end;
    const preview = lines.slice(start, end).filter((l) => l.trim()).join("\n");
    if (!opts.silent && !confirm(`Delete [${section.name}] ?\n\n${preview}\n\nThis removes the section from your config.`)) return false;
    if (start > 0 && lines[start - 1].trim() === "") start--;
    const next = lines.slice(0, start).concat(lines.slice(end)).join("\n");
    editor.setValue(next);
    C.currentContent = next;
    $("#save-btn").disabled = C.currentContent === C.originalContent;
    setEditorStatus(`Deleted [${section.name}].`, "ok");
    if (C.openItem === section.name) C.openItem = null;
    refreshSectionsFromBuffer();
    scheduleAutosave();
    return true;
  };

  const duplicateSection = (section) => {
    const schema = findFormSchema(section.name);
    if (!schema) {
      alert(`No form defined for [${section.name}] — duplicate manually in the TOML editor.`);
      return;
    }
    const fields = fieldsForSection(section);
    const existingNames = new Set(C.sections.map((s) => s.name));
    let newSuffix = section.suffix;
    let i = 1;
    while (existingNames.has(`${section.prefix}.${newSuffix}`)) {
      newSuffix = section.suffix + i;
      i++;
    }
    formContext = { sectionName: `${section.prefix}.__new__`, fields, schema, addMode: true, addPrefix: section.prefix, nameValue: newSuffix };
    activeTab = "left";
    $("#form-modal-title").textContent = `Duplicate [${section.name}]`;
    $("#form-modal-hint").hidden = false;
    $("#form-modal-hint").textContent = "Review the values copied from the original section, change what you need, and save.";
    renderForm(schema, fields);
    formModal.open();
    setTimeout(() => $("#add-section-name").focus(), 50);
  };

  // ---------------------------------------------------------------
  // Form schemas. Field keys:
  //   key, label, type ("text"|"password"|"number"|"select"|"textarea"|
  //   "checkbox"|"checkboxes"), options, required, help, group, advanced,
  //   column ("left"|"right" -> tabs), coerce ("int"), keepString, list,
  //   headerToggle (rendered as the Enabled switch in the modal header).
  // ---------------------------------------------------------------
  const BUILT_IN_REGIONS = ["usa", "usa_full", "can", "mex", "bra", "arg", "aus", "aus_miles", "nzl", "ind", "gbr", "fra", "spa"];
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
  // One sentence per tier, reused by every review_rating field in the schemas.
  const THREE_TIER_HELP =
    "Below this score a listing is rated once, cached and only tracked — it never reaches the review queue. " +
    "At or above it the listing waits in Review for your decision. " +
    "At or above the notify threshold it also sends a notification.";
  const RATING_OPTS = (first) => [
    { value: "", label: first },
    { value: "1", label: "1 — everything, no filtering" },
    { value: "2", label: "2 — potential match or better" },
    { value: "3", label: "3 — poor match or better (default)" },
    { value: "4", label: "4 — good match or better" },
    { value: "5", label: "5 — great deals only" },
  ];
  const CONDITION_OPTS = [
    { value: "new", label: "New" },
    { value: "used_like_new", label: "Used — like new" },
    { value: "used_good", label: "Used — good" },
    { value: "used_fair", label: "Used — fair" },
  ];
  const AVAIL_OPTS = [
    { value: "all", label: "All" },
    { value: "in", label: "In stock" },
    { value: "out", label: "Out of stock" },
  ];
  const DATE_OPTS = [
    { value: "1", label: "Last 24 hours" },
    { value: "7", label: "Last 7 days" },
    { value: "30", label: "Last 30 days" },
  ];
  const DELIVERY_OPTS = [
    { value: "local_pick_up", label: "Local pick-up" },
    { value: "shipping", label: "Shipping" },
  ];
  const MAX_IMAGES_HELP =
    "How many of a listing's photos to keep once the AI rates it worth reviewing. Default: 6, maximum 12, 0 to keep none. Facebook only for now — eBay, Depop and Poshmark are read from search tiles that carry a single photo each, so those listings show one picture whatever this says. Photos are copied to disk because Facebook's image links are signed and expire within days: a photo not saved while the listing is fresh is a broken image by the time you get to it.";
  const MAX_LISTINGS_HELP =
    "How many results one search phrase may hand to the AI. Default: 60. Every listing that comes back costs one AI rating — about 25 seconds on a local Ollama — so a phrase that returns 240 tiles is an hour of rating for that phrase alone.";

  const FORM_SCHEMAS = {
    "marketplace.facebook": [
      { key: "enabled", label: "Enabled", type: "checkbox", headerToggle: true },
      { key: "username", label: "Facebook username (email)", type: "text", column: "left", help: "Your Facebook login email." },
      { key: "password", label: "Facebook password", type: "password", column: "left", help: "Leave blank to keep the current password." },
      { key: "login_wait_time", label: "Login wait time (seconds)", type: "number", column: "left", help: "Seconds to wait after Facebook login for 2FA / captcha. Default: 60." },
      { key: "language", label: "Language", type: "text", column: "left", advanced: true, help: "Non-English Facebook locale — must match a [translation.*] section." },
      { key: "home_location", label: "Your location (for distance & maps)", type: "text", column: "right",
        help: "Where you are, e.g. 'Asheboro, NC' or a bare 'lat, lon'. Drives the distance shown on every listing, the pickup map, and the drive-time estimate. Separate from search city, which is Facebook's own place id and does not geocode." },
      { key: "search_city", label: "Search city", type: "text", required: true, keepString: true, column: "right", help: "City code from the Facebook Marketplace URL (lowercase, e.g. 'houston')." },
      { key: "search_region", label: "Search region", type: "select", column: "right",
        options: [{ value: "", label: "(none)" }].concat(BUILT_IN_REGIONS.map((r) => ({ value: r, label: r }))), help: "Pre-defined region (expands to multiple cities)." },
      { key: "category", label: "Category", type: "select", group: "Filters", advanced: true, column: "right", options: CATEGORIES, help: "Marketplace listing category." },
      { key: "condition", label: "Condition", type: "checkboxes", advanced: true, column: "right", options: CONDITION_OPTS, help: "Filter by item condition. " + OV },
      { key: "availability", label: "Availability", type: "checkboxes", advanced: true, column: "right", options: AVAIL_OPTS },
      { key: "date_listed", label: "Date listed", type: "checkboxes", advanced: true, column: "right", options: DATE_OPTS },
      { key: "delivery_method", label: "Delivery method", type: "checkboxes", advanced: true, column: "right", options: DELIVERY_OPTS },
      { key: "seller_locations", label: "Seller locations", type: "text", advanced: true, column: "right", help: "Comma-separated location names to filter by." },
      { key: "exclude_sellers", label: "Exclude sellers", type: "text", advanced: true, column: "right", help: "Comma-separated seller names to skip." },
      { key: "keywords", label: "Keywords (include)", type: "text", advanced: true, column: "right", help: "Boolean expression, e.g. 'drone AND (DJI OR Orqa)'" },
      { key: "antikeywords", label: "Anti-keywords (exclude)", type: "text", advanced: true, column: "right", help: "Boolean expression for exclusion." },
      { key: "min_price", label: "Min price", type: "text", group: "Pricing", advanced: true, column: "right", help: "e.g. '50' or '50 USD'" },
      { key: "max_price", label: "Max price", type: "text", advanced: true, column: "right", help: "e.g. '300' or '300 USD'" },
      { key: "radius", label: "Search radius (km)", type: "text", group: "Location", advanced: true, column: "right", help: "Comma-separated radius per city (must match search_city count)." },
      { key: "currency", label: "Currency", type: "text", advanced: true, column: "right", help: "Comma-separated currency code per city, e.g. 'USD, CAD'." },
      { key: "ai", label: "AI backends", type: "text", group: "AI evaluation", advanced: true, column: "right", help: "Comma-separated [ai.*] names." },
      { key: "review_rating", label: "Review at AI rating ≥", type: "select", coerce: "int", column: "right", options: RATING_OPTS("Default (3)"),
        help: THREE_TIER_HELP + " Default review threshold for every item; items can override it." },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "right", options: RATING_OPTS("Default (3)"), help: "Default notification threshold for every item. Items can override it. Must be at or above the review threshold." },
      { key: "prompt", label: "AI prompt", type: "textarea", advanced: true, column: "right", help: "Custom evaluation prompt (replaces default)." },
      { key: "extra_prompt", label: "Extra prompt", type: "textarea", advanced: true, column: "right", help: "Additional text appended before the rating prompt." },
      { key: "rating_prompt", label: "Rating prompt", type: "textarea", advanced: true, column: "right", help: "Custom rating instructions (replaces default 1–5 scale)." },
      { key: "notify", label: "Notify users", type: "text", group: "Notification", advanced: true, column: "right", help: "Comma-separated [user.*] names. Default: all users." },
      { key: "request_delay", label: "Delay between page loads (seconds)", type: "text", group: "Pacing & block safety", column: "right",
        help: "Two numbers, e.g. '6, 15'. The monitor waits a random time in that range before every Facebook page load — a search page or a listing page. Default: 6 to 15 seconds. Lower it and blocks get likely; a fixed value (e.g. '10, 10') is worse than a range, because a metronome is the easiest bot signature there is." },
      { key: "block_cooldown", label: "Pause after a block", type: "text", column: "right",
        help: "How long to stop searching Facebook entirely once it serves a block page ('You're temporarily blocked', a checkpoint, or a bounce to login). Default: 2h, doubling for repeat blocks up to 8h. You get one notification, and the Status page offers 'Clear block' if you know the block has lifted." },
      { key: "max_images", label: "Photos to keep per listing", type: "number", group: "Photos", column: "right", help: MAX_IMAGES_HELP },
      { key: "search_interval", label: "Search every (minimum)", type: "text", group: "Schedule", column: "right", help: "Default cadence for every item that does not set its own. Duration, e.g. '30m', '1h'. Default: 30 min." },
      { key: "max_search_interval", label: "… and at most", type: "text", column: "right", help: "Upper bound for the random interval jitter. Default: 1 hour." },
      { key: "start_at", label: "Start at", type: "text", advanced: true, column: "right", help: "Comma-separated time patterns: 'HH:MM', '*:MM', '*:*:SS'." },
    ],
    // eBay has two backends and `mode` picks between them, so the credentials
    // are optional: browser mode needs none.
    "marketplace.ebay": [
      { key: "mode", label: "How to search eBay", type: "select", column: "left",
        options: [
          { value: "", label: "Automatic — browser unless API keys are set" },
          { value: "browser", label: "Browser — scrape ebay.com, no keys needed" },
          { value: "api", label: "API — official Browse API, needs a developer key" },
        ],
        help: "Browser mode drives the same Chromium the Depop and Poshmark sources use: no eBay account, no keys, the newest listings per phrase (60 by default — see the cap below). Tiles carry no seller and no description, so the AI judges on title, price and condition. API mode is faster and richer (seller, description, exact location) but needs a free application key set from developer.ebay.com and is capped at about 5000 calls a day. Automatic picks API when both keys below are filled in, browser otherwise." },
      { key: "client_id", label: "eBay App ID (Client ID)", type: "text", column: "left", help: "Only used in API mode. From an application key set at developer.ebay.com. Use ${EBAY_CLIENT_ID} to read it from the environment. Leave blank to search with the browser instead." },
      { key: "client_secret", label: "eBay Cert ID (Client Secret)", type: "password", column: "left", help: "Only used in API mode. Use ${EBAY_CLIENT_SECRET} to keep it out of the config file." },
      { key: "marketplace_id", label: "eBay site", type: "select", column: "left",
        options: [
          { value: "", label: "EBAY_US (default)" },
          { value: "EBAY_GB", label: "EBAY_GB — United Kingdom" },
          { value: "EBAY_CA", label: "EBAY_CA — Canada" },
          { value: "EBAY_DE", label: "EBAY_DE — Germany" },
          { value: "EBAY_AU", label: "EBAY_AU — Australia" },
        ],
        help: "Also picks the domain browser mode searches (ebay.co.uk, ebay.de, ...)." },
      { key: "delivery_country", label: "Ships to", type: "text", column: "left", help: "API mode only. Two-letter country code, e.g. US. Excludes items that will not ship to you." },
      { key: "buying_options", label: "Buying options", type: "checkboxes", column: "left",
        options: [
          { value: "FIXED_PRICE", label: "Buy It Now" },
          { value: "AUCTION", label: "Auction" },
          { value: "BEST_OFFER", label: "Best Offer" },
        ],
        help: "API mode only. Leave empty for all." },
      { key: "category", label: "eBay category id", type: "text", column: "left",
        help: "Optional. Restricts every search to one eBay category, e.g. 6001 for Cars & Trucks (the number after _sacat in a category URL). A phrase alone cannot do this: searching 'toyota' returns floor mats, badges and key fobs long before it returns a car. Works in both modes." },
      { key: "max_listings", label: "Max listings per search phrase", type: "number", column: "left",
        help: MAX_LISTINGS_HELP + " Four uncapped phrases is how one car search spent five hours rating car parts." },
      { key: "review_rating", label: "Review at AI rating ≥", type: "select", coerce: "int", column: "right", options: RATING_OPTS("Default (3)"),
        help: THREE_TIER_HELP + " Default review threshold for eBay items." },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "right", options: RATING_OPTS("Default (3)"), help: "Default notification threshold for eBay items. Must be at or above the review threshold." },
      { key: "notify", label: "Notify users", type: "text", column: "right", help: "Comma-separated [user.*] names." },
      { key: "search_interval", label: "Search interval", type: "text", column: "right", help: "e.g. '30m'. The Browse API allows ~5000 calls/day across the whole app; browser mode is scraped in the shared browser, so be polite." },
      { key: "max_search_interval", label: "Max search interval", type: "text", column: "right", advanced: true },
      { key: "min_price", label: "Min price", type: "text", group: "Pricing", column: "right", advanced: true },
      { key: "max_price", label: "Max price", type: "text", column: "right", advanced: true },
      { key: "ai", label: "AI backends", type: "text", group: "AI evaluation", column: "right", advanced: true },
      { key: "prompt", label: "AI prompt", type: "textarea", column: "right", advanced: true },
      { key: "extra_prompt", label: "Extra prompt", type: "textarea", column: "right", advanced: true },
      { key: "enabled", label: "Enabled", type: "checkbox", headerToggle: true },
    ],
    "marketplace.depop": [
      { key: "enabled", label: "Enabled", type: "checkbox", headerToggle: true },
      { key: "review_rating", label: "Review at AI rating ≥", type: "select", coerce: "int", column: "left", options: RATING_OPTS("Default (3)").filter((o) => !["1", "2"].includes(o.value)), help: THREE_TIER_HELP },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "left", options: RATING_OPTS("Default (3)").filter((o) => !["1", "2"].includes(o.value)) },
      { key: "notify", label: "Notify users", type: "text", column: "left" },
      { key: "search_interval", label: "Search interval", type: "text", column: "left", help: "e.g. '1h'. Scraped in the shared browser — be polite." },
      { key: "max_listings", label: "Max listings per search phrase", type: "number", column: "left", help: MAX_LISTINGS_HELP },
      { key: "min_price", label: "Min price", type: "text", column: "right" },
      { key: "max_price", label: "Max price", type: "text", column: "right", help: "Search tiles carry no description, so the AI judges on title + price only." },
    ],
    "marketplace.poshmark": [
      { key: "enabled", label: "Enabled", type: "checkbox", headerToggle: true },
      { key: "review_rating", label: "Review at AI rating ≥", type: "select", coerce: "int", column: "left", options: RATING_OPTS("Default (3)").filter((o) => !["1", "2"].includes(o.value)), help: THREE_TIER_HELP },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "left", options: RATING_OPTS("Default (3)").filter((o) => !["1", "2"].includes(o.value)) },
      { key: "notify", label: "Notify users", type: "text", column: "left" },
      { key: "search_interval", label: "Search interval", type: "text", column: "left", help: "e.g. '1h'. Scraped in the shared browser — be polite." },
      { key: "max_listings", label: "Max listings per search phrase", type: "number", column: "left", help: MAX_LISTINGS_HELP },
      { key: "min_price", label: "Min price", type: "text", column: "right" },
      { key: "max_price", label: "Max price", type: "text", column: "right", help: "Search tiles carry no description, so the AI judges on title + price only." },
    ],
    "item.*": [
      { key: "search_phrases", label: "Search phrases", type: "text", required: true, column: "left", help: "Comma-separated. e.g. 'gopro hero 11, gopro hero 12'" },
      { key: "description", label: "Description (helps AI)", type: "textarea", column: "left", help: "Free-text description of what you want. The AI uses this to evaluate listings." },
      { key: "marketplace", label: "Marketplace", type: "text", column: "left", advanced: true, help: "Which [marketplace.*] to search. Default: first defined marketplace." },
      { key: "search_city", label: "Search city", type: "text", column: "right", help: "Override marketplace's search city for this item." },
      { key: "search_region", label: "Search region", type: "select", column: "right", options: [{ value: "", label: "(inherit from marketplace)" }].concat(BUILT_IN_REGIONS.map((r) => ({ value: r, label: r }))) },
      { key: "min_price", label: "Min price", type: "text", column: "right", help: "e.g. '50' or '50 USD'" },
      { key: "max_price", label: "Max price", type: "text", column: "right", help: "e.g. '300' or '300 USD'" },
      { key: "category", label: "Category", type: "select", column: "right", advanced: true, options: CATEGORIES },
      { key: "condition", label: "Condition", type: "checkboxes", column: "right", advanced: true, options: CONDITION_OPTS },
      { key: "availability", label: "Availability", type: "checkboxes", column: "right", advanced: true, options: AVAIL_OPTS },
      { key: "date_listed", label: "Date listed", type: "checkboxes", column: "right", advanced: true, options: DATE_OPTS },
      { key: "delivery_method", label: "Delivery method", type: "checkboxes", column: "right", advanced: true, options: DELIVERY_OPTS },
      { key: "keywords", label: "Keywords (include)", type: "text", column: "right", advanced: true, help: "Boolean expression, e.g. 'drone AND (DJI OR Orqa)'" },
      { key: "antikeywords", label: "Anti-keywords (exclude)", type: "text", column: "right", advanced: true },
      { key: "seller_locations", label: "Seller locations", type: "text", column: "right", advanced: true, help: "Comma-separated." },
      { key: "exclude_sellers", label: "Exclude sellers", type: "text", column: "right", advanced: true },
      { key: "notify", label: "Notify users", type: "text", column: "right", advanced: true, help: "Comma-separated [user.*] names. Default: inherit from marketplace." },
      { key: "ai", label: "AI backends", type: "text", group: "AI", column: "right", advanced: true },
      { key: "review_rating", label: "Review at AI rating ≥", type: "select", coerce: "int", column: "right", options: RATING_OPTS("Inherit from marketplace (default 3)"),
        help: THREE_TIER_HELP + " Blank inherits the marketplace value. It may never be higher than the notify threshold below — that config is rejected." },
      { key: "rating", label: "Notify at AI rating ≥", type: "select", coerce: "int", column: "right", options: RATING_OPTS("Inherit from marketplace (default 3)"),
        help: "The notification threshold. Listings between the review threshold and this one wait in Review; listings at or above it also reach your phone." },
      { key: "prompt", label: "AI prompt", type: "textarea", column: "right", advanced: true },
      { key: "extra_prompt", label: "Extra prompt", type: "textarea", column: "right", advanced: true },
      { key: "rating_prompt", label: "Rating prompt", type: "textarea", column: "right", advanced: true },
      { key: "max_images", label: "Photos to keep per listing", type: "number", column: "right", advanced: true, help: MAX_IMAGES_HELP + " Blank inherits the marketplace value." },
      { key: "search_interval", label: "Search every (minimum)", type: "text", group: "Schedule", column: "right",
        help: "Duration, e.g. '45m', '2h', '1d'. Blank inherits the marketplace value (30m by default). Every search phrase on this item is its own page load, so a six-phrase item searched every 30 minutes is 288 page loads a day — enough to get an account temporarily blocked." },
      { key: "max_search_interval", label: "… and at most", type: "text", column: "right",
        help: "Upper bound for the random wait. Set it higher than the minimum and the monitor picks a fresh random interval every cycle, so its requests never form a detectable pattern. Blank inherits the marketplace value (1h by default)." },
      { key: "request_delay", label: "Delay between page loads (seconds)", type: "text", column: "right", advanced: true, help: "Two numbers, e.g. '6, 15' — a random pause in that range before each page load for this item. Blank inherits the marketplace setting." },
      { key: "start_at", label: "Start at", type: "text", column: "right", advanced: true,
        help: "Comma-separated time patterns: 'HH:MM', '*:MM', '*:*:SS'. Setting this replaces the interval above with fixed clock times — which is more predictable to Facebook, so prefer the randomized interval unless you need a specific hour." },
    ],
    "user.*": [
      { key: "pushbullet_token", label: "Pushbullet token", type: "password", help: "Get your token from pushbullet.com → Settings → Access tokens." },
      { key: "pushover_user_key", label: "Pushover user key", type: "password", group: "Pushover" },
      { key: "pushover_api_token", label: "Pushover API token", type: "password" },
      { key: "telegram_token", label: "Telegram bot token", type: "password", group: "Telegram", help: "Format: 123456789:ABCdef..." },
      { key: "telegram_chat_id", label: "Telegram chat ID", type: "text", help: "Numeric ID or @username." },
      { key: "ntfy_server", label: "ntfy server URL", type: "text", group: "ntfy", help: "e.g. https://ntfy.sh" },
      { key: "ntfy_topic", label: "ntfy topic", type: "text" },
      { key: "email", label: "Email address", type: "text", group: "Email", help: "Comma-separated list of recipient addresses." },
      { key: "smtp_server", label: "SMTP server", type: "text", advanced: true },
      { key: "smtp_port", label: "SMTP port", type: "number", advanced: true, help: "Default: 587" },
      { key: "smtp_username", label: "SMTP username", type: "text", advanced: true },
      { key: "smtp_password", label: "SMTP password (app password)", type: "password", advanced: true },
      { key: "smtp_from", label: "SMTP from address", type: "text", advanced: true },
      { key: "notify_with", label: "Notification sections", type: "text", group: "Other", advanced: true, help: "Comma-separated [notification.*] section names for shared credentials." },
      { key: "remind", label: "Remind interval", type: "text", advanced: true, help: "Resend after this interval, e.g. '1d', '6h'. Default: one-time." },
    ],
    "ai.*": [
      { key: "api_key", label: "API key", type: "password", help: "If left blank, the env var for the provider is used (e.g. ${OPENAI_API_KEY}, ${ANTHROPIC_API_KEY}, ${DEEPSEEK_API_KEY})." },
      { key: "model", label: "Model", type: "text", help: "e.g. 'gpt-4o', 'deepseek-chat', 'deepseek-r1:14b', 'claude-sonnet-4-20250514'" },
      { key: "provider", label: "Provider override", type: "text", advanced: true, help: "Override the provider (auto-detected from section name). Only needed for custom OpenAI-compatible endpoints." },
      { key: "base_url", label: "Base URL", type: "text", advanced: true, help: "Custom API endpoint. Required for Ollama (e.g. http://localhost:11434/v1)." },
      { key: "timeout", label: "Timeout (seconds)", type: "number", advanced: true },
      { key: "max_retries", label: "Max retries", type: "number", advanced: true, help: "Default: 10" },
    ],
  };

  const findFormSchema = (sectionName) => {
    if (FORM_SCHEMAS[sectionName]) return FORM_SCHEMAS[sectionName];
    const dot = sectionName.indexOf(".");
    if (dot >= 0) {
      const prefix = sectionName.slice(0, dot);
      if (FORM_SCHEMAS[prefix + ".*"]) return FORM_SCHEMAS[prefix + ".*"];
      if (prefix === "marketplace") {
        // A marketplace section under any name still has a concrete type:
        // its market_type key, else facebook (mirroring the backend).
        const section = C.sections.find((x) => x.name === sectionName);
        const fields = section ? fieldsForSection(section) : {};
        const kind = String(fields.market_type || "facebook").toLowerCase();
        return FORM_SCHEMAS["marketplace." + kind] || FORM_SCHEMAS["marketplace.facebook"];
      }
    }
    return null;
  };

  // ---------------------------------------------------------------
  // Section form modal
  // ---------------------------------------------------------------
  let formContext = { sectionName: "", fields: {}, schema: [] };
  let showAdvanced = false;
  let activeTab = "left";
  const formModal = {
    el: () => $("#form-modal"),
    open() {
      this.el().classList.remove("hidden");
    },
    close() {
      this.el().classList.add("hidden");
      $("#form-error").hidden = true;
      $("#section-form").innerHTML = "";
      const toggle = $("#form-modal-toggle");
      toggle.innerHTML = "";
      toggle.hidden = true;
    },
  };

  const renderForm = (schema, fields) => {
    const form = $("#section-form");
    form.innerHTML = "";
    const currentPrefix = formContext.addMode ? formContext.addPrefix : formContext.sectionName.split(".")[0];
    const aiAutoName = currentPrefix === "ai";
    const nameWrapper = document.createElement("div");
    nameWrapper.className = "form-field";
    const currentSuffix = formContext.nameValue ?? (formContext.addMode ? "" : formContext.sectionName.split(".").slice(1).join(".") || formContext.sectionName);
    if (aiAutoName) {
      const aiProviders = [
        { value: "openai", label: "OpenAI" },
        { value: "deepseek", label: "DeepSeek" },
        { value: "anthropic", label: "Anthropic" },
        { value: "ollama", label: "Ollama" },
      ];
      const opts = aiProviders.map((p) => `<option value="${p.value}" ${currentSuffix === p.value ? "selected" : ""}>${p.label}</option>`).join("");
      nameWrapper.innerHTML = `<label class="form-label">AI Provider <span class="required">*</span></label><select id="add-section-name">${opts}</select><p class="form-help">[ai.<em>provider</em>]</p>`;
      const nameSelect = nameWrapper.querySelector("select");
      nameSelect.addEventListener("change", () => {
        formContext.nameValue = nameSelect.value;
      });
      if (!currentSuffix) formContext.nameValue = nameSelect.value;
    } else {
      // Prefixed name, explicit autocomplete and readonly-until-focus: Chrome
      // autofills saved site credentials into anything that looks like a
      // login form, which is how a web UI password once renamed a section.
      nameWrapper.innerHTML =
        `<label class="form-label">Section name <span class="required">*</span></label>` +
        `<input type="text" id="add-section-name" name="aimm_section_name" autocomplete="one-time-code" readonly onfocus="this.removeAttribute('readonly')" value="${esc(currentSuffix)}" placeholder="e.g. gopro, me" />` +
        `<p class="form-help">[${esc(currentPrefix)}.<em>name</em>]</p>`;
      const nameInput = nameWrapper.querySelector("input");
      nameInput.addEventListener("input", () => {
        formContext.nameValue = nameInput.value;
      });
    }
    form.appendChild(nameWrapper);

    // A `headerToggle` field is lifted out of the grid into the modal header.
    const headerHost = $("#form-modal-toggle");
    const headerField = schema.find((f) => f.headerToggle);
    headerHost.innerHTML = "";
    headerHost.hidden = !headerField;
    if (headerField) {
      const raw = fields[headerField.key];
      const isOn = raw === undefined || raw === null || raw === "" ? headerField.default !== false : String(raw).toLowerCase() !== "false";
      const label = document.createElement("label");
      label.innerHTML = `<span>${esc(headerField.label)}</span><input type="checkbox" id="field-${esc(headerField.key)}" ${isOn ? "checked" : ""} /><span class="sw ${isOn ? "on" : ""}"><i></i></span>`;
      const box = label.querySelector("input");
      box.dataset.key = headerField.key;
      box.name = "aimm_" + headerField.key;
      box.autocomplete = "off";
      const pip = label.querySelector(".sw");
      box.addEventListener("change", () => pip.classList.toggle("on", box.checked));
      headerHost.appendChild(label);
    }

    const hasColumns = schema.some((f) => f.column);
    if (hasColumns) {
      const prefix = formContext.sectionName.split(".")[0];
      const schemaKind = (Object.entries(FORM_SCHEMAS).find(([, v]) => v === schema) || [""])[0];
      const MARKET_TABS = {
        "marketplace.facebook": ["Facebook Login", "Search Defaults (overridable per item)"],
        "marketplace.ebay": ["eBay", "Search Defaults"],
        "marketplace.depop": ["Depop", "Pricing"],
        "marketplace.poshmark": ["Poshmark", "Pricing"],
      };
      const marketTabs = MARKET_TABS[schemaKind];
      const leftLabel = prefix === "marketplace" ? (marketTabs ? marketTabs[0] : "Login") : "Item Settings";
      const rightLabel = prefix === "marketplace" ? (marketTabs ? marketTabs[1] : "Search Defaults") : "Filters & AI";
      const tabBar = document.createElement("div");
      tabBar.className = "form-tab-bar";
      [["left", leftLabel], ["right", rightLabel]].forEach(([side, label]) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "form-tab" + (activeTab === side ? " active" : "");
        b.textContent = label;
        b.addEventListener("click", () => {
          activeTab = side;
          renderForm(schema, fields);
        });
        tabBar.appendChild(b);
      });
      form.appendChild(tabBar);
    }

    const gridFields = schema.filter((f) => !f.headerToggle);
    const visibleFields = hasColumns ? gridFields.filter((f) => (f.column || "left") === activeTab) : gridFields;
    if (visibleFields.some((f) => f.advanced)) {
      const toggle = document.createElement("label");
      toggle.className = "form-label";
      toggle.style.cursor = "pointer";
      toggle.innerHTML = `<input type="checkbox" id="show-advanced" ${showAdvanced ? "checked" : ""} /> Show advanced fields`;
      toggle.querySelector("input").addEventListener("change", (e) => {
        showAdvanced = e.target.checked;
        renderForm(schema, fields);
      });
      form.appendChild(toggle);
    }

    let lastGroup = null;
    visibleFields.forEach((fieldDef) => {
      if (fieldDef.advanced && !showAdvanced) return;
      if (fieldDef.group && fieldDef.group !== lastGroup) {
        lastGroup = fieldDef.group;
        const g = document.createElement("div");
        g.className = "form-group-title";
        g.textContent = fieldDef.group;
        form.appendChild(g);
      }
      const wrapper = document.createElement("div");
      wrapper.className = "form-field";
      const label = document.createElement("label");
      label.className = "form-label";
      label.innerHTML = esc(fieldDef.label) + (fieldDef.required ? ' <span class="required">*</span>' : ' <span class="optional">optional</span>');
      wrapper.appendChild(label);

      let input;
      const rawVal = fields[fieldDef.key];
      const currentVal = Array.isArray(rawVal) ? rawVal.join(", ") : rawVal ?? "";
      const checkedSet = new Set(Array.isArray(rawVal) ? rawVal.map(String) : currentVal ? [String(currentVal)] : []);
      if (fieldDef.type === "checkboxes") {
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
        if (fieldDef.type === "password" && String(currentVal) === "<REDACTED>") {
          input.value = "";
          input.placeholder = "(unchanged — leave blank to keep current)";
        } else input.value = currentVal;
        if (fieldDef.type === "number") {
          input.min = "0";
          input.step = "1";
        }
      }
      if (fieldDef.type !== "checkboxes") {
        // Autofill defences: prefixed name, explicit autocomplete, and
        // readonly until focus (Chrome skips readonly fields at render time).
        input.name = "aimm_" + fieldDef.key;
        input.autocomplete = fieldDef.type === "password" ? "new-password" : "off";
        if (fieldDef.type === "text" || fieldDef.type === "password") {
          input.readOnly = true;
          input.addEventListener("focus", () => {
            input.readOnly = false;
          });
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

    if (aiAutoName && formContext.addMode) {
      const envVarMap = { openai: "${OPENAI_API_KEY}", deepseek: "${DEEPSEEK_API_KEY}", anthropic: "${ANTHROPIC_API_KEY}", ollama: "${OLLAMA_API_KEY}" };
      const nameSelect = $("#add-section-name");
      const apiKeyInput = form.querySelector('[data-key="api_key"]');
      if (nameSelect && apiKeyInput) {
        const syncApiKey = () => {
          const envRef = envVarMap[nameSelect.value] || "";
          if (!apiKeyInput.value || apiKeyInput.value.startsWith("${")) apiKeyInput.value = envRef;
        };
        nameSelect.addEventListener("change", syncApiKey);
        syncApiKey();
      }
    }
  };

  // Config keys the backend types as a list, so a comma in them is a
  // separator. Everywhere else a comma is punctuation: splitting it turned
  // home_location = "Asheboro, NC" into ["Asheboro", "NC"], which the
  // validator rejects. A schema field can also opt in with `list: true`.
  const LIST_VALUED_KEYS = new Set([
    "ai", "antikeywords", "availability", "buying_options", "city_name", "condition", "currency", "date_listed",
    "delivery_method", "email", "exclude_sellers", "keywords", "marketplace", "notify", "notify_with", "radius",
    "rating", "request_delay", "search_city", "search_phrases", "search_region", "seller_locations", "start_at",
  ]);
  const fieldIsList = (fieldDef, currentValue) =>
    fieldDef.list === true || fieldDef.type === "checkboxes" || LIST_VALUED_KEYS.has(fieldDef.key) || Array.isArray(currentValue);

  const collectFormValues = () => {
    const errors = [];
    const values = {};
    formContext.schema.forEach((fieldDef) => {
      if (fieldDef.advanced && !showAdvanced) return;
      // Whole modal, not just the form: a headerToggle renders in the header.
      const input = formModal.el().querySelector(`[data-key="${fieldDef.key}"]`);
      if (!input) return;
      let newVal;
      if (fieldDef.type === "checkboxes") {
        const checked = Array.from(input.querySelectorAll("input:checked")).map((cb) => cb.value);
        newVal = checked.length ? checked.join(", ") : "";
      } else if (fieldDef.type === "checkbox") {
        // .checked, not .value -- reading .value wrote enabled = "true".
        // Absent means "on", so only write the key when turning it off or
        // when it was already written.
        const isOn = !!input.checked;
        const had = Object.prototype.hasOwnProperty.call(formContext.fields || {}, fieldDef.key);
        if (isOn && !had) return;
        values[fieldDef.key] = isOn;
        return;
      } else newVal = input.value.trim();

      if (fieldDef.required && !newVal) {
        errors.push(`${fieldDef.label} is required.`);
        return;
      }
      if (!newVal) return;
      let value;
      if (fieldDef.type === "number") {
        value = parseInt(newVal, 10);
        if (isNaN(value)) {
          errors.push(`${fieldDef.label} must be a number.`);
          return;
        }
      } else if (fieldDef.coerce === "int") {
        // Selects yield strings; a rating written as "4" fails the validator.
        value = parseInt(newVal, 10);
        if (isNaN(value)) {
          errors.push(`${fieldDef.label} must be a number.`);
          return;
        }
      } else if (newVal.includes(",") && fieldIsList(fieldDef, (formContext.fields || {})[fieldDef.key])) {
        value = newVal.split(",").map((s) => s.trim()).filter(Boolean);
        if (!fieldDef.keepString && value.every((x) => /^-?\d+$/.test(x))) value = value.map((x) => parseInt(x, 10));
      } else if (!fieldDef.keepString && /^-?\d+$/.test(newVal)) {
        // An integer doesn't wear quotes; fields that MUST stay strings
        // (search_city) carry keepString.
        value = parseInt(newVal, 10);
      } else value = newVal;
      values[fieldDef.key] = value;
    });
    return { values, errors };
  };

  const generateSectionToml = (sectionFullName, values) => {
    const lines = [`[${sectionFullName}]`];
    for (const [key, val] of Object.entries(values)) {
      if (Array.isArray(val)) {
        lines.push(`${key} = [${val.map((v) => (typeof v === "number" ? String(v) : `"${String(v).replace(/"/g, '\\"')}"`)).join(", ")}]`);
      } else if (typeof val === "number" || typeof val === "boolean") lines.push(`${key} = ${val}`);
      else lines.push(`${key} = "${String(val).replace(/"/g, '\\"')}"`);
    }
    return lines.join("\n") + "\n";
  };

  const showFormError = (msg) => {
    $("#form-error").textContent = msg;
    $("#form-error").hidden = false;
  };

  const saveForm = async () => {
    const { values, errors } = collectFormValues();
    if (formContext.addMode) {
      const nameInput = $("#add-section-name");
      const sectionSuffix = (nameInput ? nameInput.value.trim() : "").replace(/[^a-zA-Z0-9_\-]/g, "_");
      if (!sectionSuffix) errors.push("Section name is required.");
      if (errors.length) return showFormError(errors.join(" "));
      const fullName = `${formContext.addPrefix}.${sectionSuffix}`;
      if (C.sections.some((s) => s.name === fullName)) return showFormError(`Section [${fullName}] already exists.`);
      if (formContext.addKind && !("market_type" in values)) values.market_type = formContext.addKind;
      if (formContext.addKind === "ebay" && !("enabled" in values)) {
        // Browser mode needs no credentials, so a fresh eBay section is live
        // the moment it is added. Only explicit API mode with no key starts
        // paused, because it genuinely cannot search until one arrives.
        const apiWithoutKeys = String(values.mode || "").toLowerCase() === "api" && !(values.client_id && values.client_secret);
        values.enabled = !apiWithoutKeys;
      }
      const block = generateSectionToml(fullName, values);
      let buffer = C.currentContent;
      const samePrefix = C.sections.filter((s) => s.prefix === formContext.addPrefix);
      if (samePrefix.length) {
        const last = samePrefix[samePrefix.length - 1];
        const lines = buffer.split("\n");
        lines.splice(last.line_end, 0, "", ...block.split("\n"));
        buffer = lines.join("\n");
      } else buffer = buffer.replace(/\n*$/, "") + "\n\n" + block;
      editor.setValue(buffer);
      C.currentContent = buffer;
      $("#save-btn").disabled = C.currentContent === C.originalContent;
      refreshSectionsFromBuffer();
      formModal.close();
      if (formContext.addPrefix === "item") C.openItem = fullName;
      if (C.currentContent !== C.originalContent) await saveConfig();
      rerender();
      return;
    }

    // ---- Edit mode ----
    const nameInput = $("#add-section-name");
    const newSuffix = nameInput ? nameInput.value.trim().replace(/[^a-zA-Z0-9_\-]/g, "_") : "";
    if (!newSuffix) errors.push("Section name is required.");
    const prefix = formContext.sectionName.split(".")[0];
    const newFullName = prefix + "." + newSuffix;
    const renamed = newFullName !== formContext.sectionName;
    if (renamed && C.sections.some((s) => s.name === newFullName)) errors.push(`Section [${newFullName}] already exists.`);
    if (errors.length) return showFormError(errors.join(" "));

    // Rename rewrites ONLY the header line, in place. Regenerating the section
    // from the form's values silently dropped every key the form did not
    // carry (home_location, login_wait_time, search_interval once vanished).
    let targetName = formContext.sectionName;
    if (renamed) {
      const section = C.sections.find((s) => s.name === formContext.sectionName);
      if (!section) return showFormError("Section not found in the buffer — reload and retry.");
      // Decide this before the rescan: the rescan drops an open item whose
      // section name no longer exists, which is exactly what a rename does.
      if (C.openItem === formContext.sectionName) C.openItem = newFullName;
      const lines = C.currentContent.split("\n");
      lines[section.line_start] = lines[section.line_start].replace(/\[[^\]]+\]/, "[" + newFullName + "]");
      C.currentContent = lines.join("\n");
      editor.setValue(C.currentContent);
      refreshSectionsFromBuffer();
      targetName = newFullName;
    }
    if (!window.tomlEdit) return showFormError("TOML editor library failed to load — edit the TOML directly.");
    let buffer = C.currentContent;
    const editErrors = [];
    formContext.schema.forEach((fieldDef) => {
      if (fieldDef.advanced && !showAdvanced) return;
      if (fieldDef.key in values) {
        try {
          buffer = window.tomlEdit.edit(buffer, targetName + "." + fieldDef.key, values[fieldDef.key]);
        } catch (err) {
          editErrors.push(`Failed to set ${fieldDef.key}: ${err.message}`);
        }
      }
    });
    if (editErrors.length) return showFormError(editErrors.join(" "));
    editor.setValue(buffer);
    C.currentContent = buffer;
    $("#save-btn").disabled = C.currentContent === C.originalContent;
    refreshSectionsFromBuffer();
    formModal.close();
    if (C.currentContent !== C.originalContent) await saveConfig();
    rerender();
  };

  const openEditSectionModal = (sectionName, opts = {}) => {
    const section = C.sections.find((s) => s.name === sectionName);
    const fields = section ? fieldsForSection(section) : {};
    const schema = findFormSchema(sectionName);
    if (!schema) {
      $("#form-modal-title").textContent = `Edit [${sectionName}]`;
      $("#form-modal-hint").hidden = false;
      $("#form-modal-hint").textContent = `No form defined for [${sectionName}] yet — edit it in the TOML view.`;
      $("#section-form").innerHTML = "";
      formModal.open();
      return;
    }
    const dot = sectionName.indexOf(".");
    formContext = { sectionName, fields, schema, nameValue: dot >= 0 ? sectionName.slice(dot + 1) : sectionName };
    activeTab = opts.tab || "left";
    $("#form-modal-title").textContent = `Edit [${sectionName}]`;
    $("#form-modal-hint").hidden = true;
    renderForm(schema, fields);
    formModal.open();
    if (opts.focusName) {
      setTimeout(() => {
        const n = $("#add-section-name");
        if (n) {
          n.focus();
          if (n.select) n.select();
        }
      }, 50);
    }
  };

  const openAddSectionModal = (prefix, suggested) => {
    const schema = (suggested && FORM_SCHEMAS[prefix + "." + suggested]) || findFormSchema(prefix + ".*") || findFormSchema(prefix + ".facebook");
    if (!schema) {
      alert(`No form defined for [${prefix}.*] — add it manually in the TOML editor.`);
      return;
    }
    const suggestedName = suggested || (prefix === "marketplace" ? "facebook" : "");
    formContext = {
      sectionName: `${prefix}.__new__`,
      fields: {},
      schema,
      addMode: true,
      addPrefix: prefix,
      // The concrete marketplace type, written as market_type so the section
      // NAME never has to carry the type.
      addKind: prefix === "marketplace" ? suggestedName || "facebook" : null,
      nameValue: suggestedName,
    };
    activeTab = "left";
    $("#form-modal-title").textContent = `Add a new [${prefix}.*] section`;
    $("#form-modal-hint").hidden = false;
    $("#form-modal-hint").textContent = "Choose a name and fill in the fields. The new section is appended to your config.";
    renderForm(schema, {});
    formModal.open();
    setTimeout(() => {
      const nameInput = $("#add-section-name");
      if (nameInput && !nameInput.value) nameInput.focus();
    }, 50);
  };

  $("#form-modal-close").addEventListener("click", () => formModal.close());
  $("#form-cancel").addEventListener("click", () => formModal.close());
  $("#form-save").addEventListener("click", saveForm);
  $("#form-modal .modal-backdrop").addEventListener("click", () => formModal.close());

  // ---------------------------------------------------------------
  // Parsed config helpers
  // ---------------------------------------------------------------
  const parsedConfig = { content: null, tree: null };
  const configTree = () => {
    const content = editor.getValue() || "";
    if (parsedConfig.content === content) return parsedConfig.tree;
    let tree = null;
    if (window.tomlEdit) {
      try {
        tree = window.tomlEdit.parse(content);
      } catch (err) {
        tree = null; // half-typed TOML mid-edit
      }
    }
    parsedConfig.content = content;
    parsedConfig.tree = tree;
    return tree;
  };
  const fieldsForSection = (section) => {
    if (section.fields && Object.keys(section.fields).length && parsedConfig.content === null) return section.fields;
    const tree = configTree();
    if (!tree) return section.fields || {};
    let node = tree;
    for (const part of section.name.split(".")) node = node && node[part];
    return node && typeof node === "object" && !Array.isArray(node) ? node : section.fields || {};
  };
  const sectionByName = (name) => C.sections.find((s) => s.name === name) || null;
  const marketplaceSections = () => C.sections.filter((s) => s.prefix === "marketplace");
  const marketplaceNames = () => marketplaceSections().map((s) => s.suffix);
  const marketKind = (mk) => String(fieldsForSection(mk).market_type || mk.suffix || "facebook").toLowerCase();
  const itemSections = () => C.sections.filter((s) => s.prefix === "item");

  // Defaults from monitor.py schedule_jobs: 30 minutes, jittered up to an hour.
  const DEFAULT_SEARCH_INTERVAL = 30 * 60;
  const DEFAULT_MAX_SEARCH_INTERVAL = 60 * 60;
  const marketplaceScheduleValue = (key) => {
    for (const mk of marketplaceSections()) {
      const v = fieldsForSection(mk)[key];
      if (v !== undefined && v !== null && v !== "") return v;
    }
    return null;
  };
  const itemCadence = (fields) => {
    const startAt = fields.start_at ? [].concat(fields.start_at) : null;
    const ownMin = fields.search_interval;
    const ownMax = fields.max_search_interval;
    const mkMin = marketplaceScheduleValue("search_interval");
    const mkMax = marketplaceScheduleValue("max_search_interval");
    const source = ownMin || ownMax ? "item" : mkMin || mkMax ? "marketplace" : "default";
    const min = Math.max(parseDuration(ownMin) || parseDuration(mkMin) || DEFAULT_SEARCH_INTERVAL, 1);
    const max = Math.max(parseDuration(ownMax) || parseDuration(mkMax) || DEFAULT_MAX_SEARCH_INTERVAL, min);
    let label;
    if (startAt && startAt.length) label = "at " + startAt.join(", ");
    else if (min === max) label = "every " + fmtCadence(min);
    else label = "every " + fmtCadence(min) + "–" + fmtCadence(max);
    return { min, max, source, startAt, label };
  };
  // Same precedence the monitor applies: the item's own value, else the last
  // marketplace that sets one, else 3. `key` is "rating" or "review_rating".
  const inheritedFor = (key) => {
    let inherited = 3;
    for (const mk of marketplaceSections()) {
      let r = fieldsForSection(mk)[key];
      if (Array.isArray(r)) r = r[r.length - 1];
      if (typeof r === "number") inherited = r;
    }
    return inherited;
  };
  const inheritedThreshold = () => inheritedFor("rating");
  const inheritedReview = () => Math.min(inheritedFor("review_rating"), inheritedThreshold());
  // A two-element [first search, every search after] list shows its steady state.
  const lastOf = (v) => (Array.isArray(v) ? v[v.length - 1] : v);
  const THRESHOLD_WORDS = { 1: "everything, no filtering", 2: "potential match or better", 3: "poor match or better", 4: "good match or better", 5: "great deals only" };

  const INLINE_HELP = {
    phrases: {
      hint: "What gets typed into each marketplace's search box. This only decides what is fetched — the AI does the judging afterwards.",
      ex: "Add variants sellers actually type, not synonyms of your own. “rtx 3090” and “geforce 3090” surface different listings; “nvidia graphics card” would just flood you.",
    },
    description: {
      hint: "Plain English, read by the AI on every listing. The single biggest lever on match quality.",
      ex: "Say what disqualifies as well as what qualifies: “24GB card, must post video, no mining rigs, no water-cooled loops I’d have to drain.”",
    },
    prices: {
      hint: "A hard filter applied before the AI sees anything — listings outside it are never fetched or rated.",
      ex: "Leave the ceiling a little above your real limit. A seller who lists at $1,600 and takes $1,400 never appears if you cap at $1,500.",
    },
    antikeywords: {
      hint: "Cheap text filters run before the AI, to keep obvious junk out of your token spend.",
      ex: "Keep these blunt. Subtle judgements — “not mined on” — belong in the description above, where the AI can weigh them.",
    },
    threshold: {
      hint: "Three tiers. Under the review score a listing is rated once, cached and only tracked. At or above it the listing waits in Review. At or above the notify score it also reaches your phone.",
      ex: "Review 3 · notify 5 is the common pair: the 1s and 2s disappear, 3s and 4s queue up for a look, and only a great deal buzzes your phone.",
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
    return h ? `<div class="fieldhelp">${esc(h.hint)}<div class="ex">${esc(h.ex)}</div></div>` : "";
  };

  // ---------------------------------------------------------------
  // Inline writes: one key straight into the buffer via tomlEdit.edit(), so
  // comments and formatting survive. Deleting a key is line surgery: TOML
  // has no null to "set" a cleared value to.
  // ---------------------------------------------------------------
  const removeKeyFromSection = (sectionName, key) => {
    const lines = editor.getValue().split("\n");
    const section = scanSectionsClient(lines.join("\n")).find((x) => x.name === sectionName);
    if (!section) return null;
    const keyRe = new RegExp("^\\s*" + key + "\\s*=");
    return lines.filter((line, i) => !(i > section.line_start && i < section.line_end && keyRe.test(line))).join("\n");
  };
  const applyInline = (sectionName, key, value) => {
    if (!window.tomlEdit) {
      setFoot("TOML editor library failed to load — use the TOML view.", true);
      return false;
    }
    try {
      const next = value === null ? removeKeyFromSection(sectionName, key) : window.tomlEdit.edit(editor.getValue(), `${sectionName}.${key}`, value);
      if (next === null) return false;
      C.suppressRescan = true;
      try {
        editor.setValue(next);
      } finally {
        C.suppressRescan = false;
      }
      C.currentContent = next;
      onEditorChange();
      refreshSectionsFromBuffer();
      scheduleAutosave();
      return true;
    } catch (err) {
      setFoot(`Could not set ${key}: ${err.message}`, true);
      toast(`Could not set ${key}`, { error: true });
      return false;
    }
  };

  // ---------------------------------------------------------------
  // Items screen
  // ---------------------------------------------------------------
  const SRC_SUB = { facebook: "browser · local pickup", ebay: "nationwide · ships", depop: "scrape · ships", poshmark: "scrape · ships" };
  const priceVal = (v) => (v == null ? "" : String(v).replace(/USD/i, "").trim());
  const fmtPrice = (v) => {
    const s = priceVal(v);
    return s === "" ? "" : /^\d+$/.test(s) ? "$" + Number(s).toLocaleString() : s;
  };
  const activitySummaryFor = (name) => ((window.AIMM.review && window.AIMM.review.summary) || []).find((x) => x.item === name);

  const editorGroup = (withId) => `
    <div class="gl">Editor</div>
    <div class="g">
      <div class="r"><div class="l">Config mode<small>Form edits and the TOML text are the same file</small></div>
        <span class="seg tight"><button data-config-mode="form" class="${C.mode === "form" ? "on" : ""}">Form</button><button data-config-mode="toml" class="${C.mode === "toml" ? "on" : ""}">TOML</button></span></div>
      <div class="r"><div class="l">Help<small>Hints explain each control; Guided adds examples</small></div>
        <span class="seg tight" ${withId ? 'id="help-seg"' : ""}><button data-help="off">Off</button><button data-help="hints">Hints</button><button data-help="guided">Guided</button></span></div>
    </div>`;

  const itemSummaryLine = (section) => {
    const f = fieldsForSection(section);
    const phrases = [].concat(f.search_phrases || []);
    const bits = [];
    if (phrases.length) bits.push(phrases.map((p) => `“${p}”`).join(" · "));
    const lo = fmtPrice(f.min_price);
    const hi = fmtPrice(f.max_price);
    if (lo || hi) bits.push(`${lo || "$0"}–${hi || "∞"}`);
    const notifyThr = lastOf(f.rating) || inheritedThreshold();
    const reviewThr = Math.min(lastOf(f.review_rating) || inheritedReview(), notifyThr);
    bits.push(`review ≥ ${reviewThr} · notify ≥ ${notifyThr}`);
    if (f.marketplace) bits.push([].concat(f.marketplace).join(", "));
    bits.push(itemCadence(f).label);
    const sum = activitySummaryFor(section.suffix || section.name);
    if (sum) bits.push(`${sum.examined} rated${sum.promising ? ` · ${sum.promising} promising` : ""}`);
    return bits.join(" · ");
  };

  const renderItemsList = () => {
    const items = itemSections();
    const rows = items
      .map((s) => {
        const f = fieldsForSection(s);
        const paused = f.enabled === false;
        return `<button class="r tap" data-open-item="${esc(s.name)}"><div class="l">${esc(s.suffix || s.name)} ${paused ? '<span class="tag paused">paused</span>' : ""}<small>${esc(itemSummaryLine(s))}</small></div><span class="chev">›</span></button>`;
      })
      .join("");
    const known = new Set(["item", "marketplace", "ai", "user", "notification", "translation", "monitor", "region"]);
    const strays = C.sections.filter((x) => !known.has(x.prefix));
    return `
      <div class="gl">Items to watch</div>
      <div class="g">${rows || '<div class="r"><div class="l muted">Nothing hunted yet — add an item.</div></div>'}<button class="r tap link" data-add="item"><div class="l">+ New item</div></button></div>
      ${strays.length ? `<div class="gl">Other sections</div><div class="g">${strays.map((x) => `<button class="r tap" data-edit-section="${esc(x.name)}"><div class="l">[${esc(x.name)}]</div><span class="chev">›</span></button>`).join("")}</div>` : ""}
      ${editorGroup(false)}`;
  };

  const renderItemEditor = (section) => {
    const fields = fieldsForSection(section);
    const enabled = fields.enabled !== false;
    const phrases = [].concat(fields.search_phrases || []);
    const anti = [].concat(fields.antikeywords || []);
    const threshold = lastOf(fields.rating);
    const inherited = inheritedThreshold();
    const effective = threshold || inherited;
    const review = lastOf(fields.review_rating);
    const inheritedRev = inheritedReview();
    // Never draw a review tier above the notify tier: the config loader
    // rejects that pair, so showing it would be showing an unsavable state.
    const effectiveRev = Math.min(review || inheritedRev, effective);
    const bound = fields.marketplace ? [].concat(fields.marketplace) : null;
    const cadence = itemCadence(fields);
    const cadenceNote = cadence.startAt
      ? "fixed times from Start at — intervals ignored"
      : cadence.source === "item" ? "set on this item" : cadence.source === "marketplace" ? "inherited from the marketplace" : "default (no interval set anywhere)";
    const durVal = (v) => (v === undefined || v === null ? "" : String(v));
    const jobs = (state.monitorInfo && state.monitorInfo.jobs) || [];
    const job = jobs.find((j) => j.item === (section.suffix || section.name));
    const nextRun = !enabled ? "paused" : job && job.next_run ? `≈ ${fmtClock(new Date(job.next_run).getTime() / 1000)} · in ${fmtDur(new Date(job.next_run).getTime() / 1000 - Date.now() / 1000)}` : "not scheduled yet";
    const chips = (key, values, opts) =>
      `<div class="pchips" data-chips="${esc(key)}">${(values || []).map((v) => `<span class="chip ${opts.neg ? "neg" : ""}">${esc(v)}<x data-chip-del="${esc(key)}" data-chip-val="${esc(v)}" title="Remove">✕</x></span>`).join("")}<input type="text" data-chip-add="${esc(key)}" placeholder="${esc(opts.placeholder)}" autocomplete="off" enterkeyhint="done" /></div>`;
    const sources = marketplaceNames()
      .map((mk) => {
        const isOn = bound === null || bound.includes(mk);
        const kind = marketKind(sectionByName("marketplace." + mk) || { suffix: mk });
        return `<div class="r"><span class="src ${esc(kind)}">${esc(mk[0])}</span><div class="l">${esc(mk)}<small>${esc(SRC_SUB[kind] || "marketplace")}</small></div><span class="tog ${isOn ? "on" : ""}" data-src="${esc(mk)}" role="switch" aria-checked="${isOn}" tabindex="0"></span></div>`;
      })
      .join("");
    const others = itemSections().filter((s) => s.name !== section.name);
    return `
      <div class="gl">Search phrases</div>
      <div class="g">${chips("search_phrases", phrases, { placeholder: "+ Add phrase" })}</div>
      ${helpBlock("phrases")}
      <div class="gl">Matching</div>
      <div class="g">
        <div class="r col"><span class="lab">Description for the AI</span><textarea class="inp" data-field="description" rows="3" placeholder="Describe a good listing — the AI reads this on every candidate.">${esc(fields.description || "")}</textarea></div>
        <div class="r"><div class="l">Price range</div><input type="text" class="inp w90" data-field="min_price" value="${esc(priceVal(fields.min_price))}" placeholder="min" autocomplete="off" inputmode="numeric" /><span class="sep">to</span><input type="text" class="inp w90" data-field="max_price" value="${esc(priceVal(fields.max_price))}" placeholder="max" autocomplete="off" inputmode="numeric" /><span class="sep">USD</span></div>
        <div class="r thrpair">
          <div class="thrcol">
            <span class="thrlab">Review when score ≥</span>
            <span class="step"><button data-rev-dec aria-label="Lower review threshold">−</button><b class="rev-val ${review ? "" : "inh"}" data-rev="${effectiveRev}">${effectiveRev}</b><button data-rev-inc aria-label="Raise review threshold">+</button></span>
            <small class="rev-note">${review ? `${THRESHOLD_WORDS[effectiveRev]} · <a data-rev-clear>reset to inherit</a>` : `inherited (≥ ${effectiveRev} — ${THRESHOLD_WORDS[effectiveRev]})`}</small>
          </div>
          <div class="thrcol">
            <span class="thrlab">Notify when score ≥</span>
            <span class="step"><button data-thr-dec aria-label="Lower threshold">−</button><b class="thr-val ${threshold ? "" : "inh"}" data-thr="${effective}">${effective}</b><button data-thr-inc aria-label="Raise threshold">+</button></span>
            <small class="thr-note">${threshold ? `${THRESHOLD_WORDS[threshold]} · <a data-thr-clear>reset to inherit</a>` : `inherited from marketplace (≥ ${inherited} — ${THRESHOLD_WORDS[inherited]})`}</small>
          </div>
        </div>
        <button class="r tap" data-act="edit"><div class="l">More settings<small>condition, category, keywords, region, AI prompt, start times…</small></div><span class="chev">›</span></button>
      </div>
      ${helpBlock("threshold")}
      <div class="gl">Must-not contain</div>
      <div class="g">${chips("antikeywords", anti, { neg: true, placeholder: "+ Add exclusion" })}</div>
      ${helpBlock("antikeywords")}
      <div class="gl">Sources</div>
      <div class="g">${sources || '<div class="r"><div class="l muted">No marketplaces configured — add one under Sources.</div></div>'}</div>
      ${helpBlock("sources")}
      <div class="gl">Schedule</div>
      <div class="g">
        <div class="r"><div class="l">Search every<small class="wrap"><span class="icadence">${esc(cadence.label)}</span> · ${esc(cadenceNote)}</small></div><input type="text" class="inp w90" data-field="search_interval" value="${esc(durVal(fields.search_interval))}" placeholder="30m" autocomplete="off" /><span class="sep">to</span><input type="text" class="inp w90" data-field="max_search_interval" value="${esc(durVal(fields.max_search_interval))}" placeholder="1h" autocomplete="off" /></div>
        <div class="r"><div class="l">Next run</div><span class="v">${esc(nextRun)}</span></div>
        <div class="r"><div class="l">Paused<small>Stop searching this item</small></div><span class="tog ${enabled ? "" : "on"}" data-toggle="enabled" role="switch" aria-checked="${!enabled}" tabindex="0"></span></div>
      </div>
      ${helpBlock("cadence")}
      <div class="gl">Other items</div>
      <div class="g">${others.map((s) => {
        const f = fieldsForSection(s);
        return `<button class="r tap" data-open-item="${esc(s.name)}"><div class="l">${esc(s.suffix || s.name)} ${f.enabled === false ? '<span class="tag paused">paused</span>' : ""}<small>${esc(itemSummaryLine(s))}</small></div><span class="chev">›</span></button>`;
      }).join("")}<button class="r tap link" data-add="item"><div class="l">+ New item</div></button></div>
      <div class="g">
        <button class="r tap" data-act="rename"><div class="l">Rename<small>keeps every setting</small></div><span class="chev">›</span></button>
        <button class="r tap" data-act="duplicate"><div class="l">Duplicate</div><span class="chev">›</span></button>
        <button class="r tap danger" data-act="delete"><div class="l">Delete ${esc(section.suffix || section.name)}</div></button>
      </div>
      <p class="item-foot ${C.footErr ? "err" : ""}" id="item-foot">${esc(C.footText || "Changes save automatically")}</p>`;
  };

  const renderItems = () => {
    const host = $("#items-page");
    const back = $("#items-back");
    const title = $("#items-title");
    const section = C.openItem ? sectionByName(C.openItem) : null;
    if (C.openItem && !section) C.openItem = null;
    if (section) {
      back.hidden = false;
      title.textContent = section.suffix || section.name;
      host.innerHTML = renderItemEditor(section);
      host.dataset.section = section.name;
    } else {
      back.hidden = true;
      title.textContent = "Items";
      host.innerHTML = C.sections.length ? renderItemsList() : '<div class="g"><div class="r"><div class="l muted">No config sections yet. Use “+ New item” or the TOML view.</div></div></div><div class="g"><button class="r tap link" data-add="item"><div class="l">+ New item</div></button></div>' + editorGroup(false);
      delete host.dataset.section;
    }
    syncHelpButtons();
  };

  $("#items-back").addEventListener("click", () => {
    C.openItem = null;
    renderItems();
    window.scrollTo(0, 0);
  });

  const itemsPage = $("#items-page");
  itemsPage.addEventListener("click", (e) => {
    const sectionName = itemsPage.dataset.section;
    const section = sectionName ? sectionByName(sectionName) : null;
    const fields = section ? fieldsForSection(section) : {};

    const openBtn = e.target.closest("[data-open-item]");
    if (openBtn) {
      C.openItem = openBtn.dataset.openItem;
      renderItems();
      window.scrollTo(0, 0);
      return;
    }
    const addBtn = e.target.closest("[data-add]");
    if (addBtn) return openAddSectionModal(addBtn.dataset.add);
    const editLink = e.target.closest("[data-edit-section]");
    if (editLink) return openEditSectionModal(editLink.dataset.editSection);
    if (!section) return;

    if (e.target.closest("[data-toggle=enabled]")) return applyInline(sectionName, "enabled", fields.enabled === false);

    if (e.target.closest("[data-thr-dec], [data-thr-inc], [data-thr-clear]")) {
      const current = lastOf(fields.rating);
      const effective = current || inheritedThreshold();
      if (e.target.closest("[data-thr-clear]")) return applyInline(sectionName, "rating", null);
      // The notify tier cannot drop under the review tier; the config loader
      // rejects that pair, so the stepper refuses instead of writing it.
      const floor = Math.min(lastOf(fields.review_rating) || inheritedReview(), 5);
      const next = Math.max(1, Math.min(5, effective + (e.target.closest("[data-thr-inc]") ? 1 : -1)));
      if (next < floor) {
        setFoot(`Notify cannot be lower than review (≥ ${floor}) — lower the review threshold first.`, true);
        toast("Notify cannot be lower than review", { error: true });
        return;
      }
      if (next === effective && current) return;
      return applyInline(sectionName, "rating", next);
    }

    if (e.target.closest("[data-rev-dec], [data-rev-inc], [data-rev-clear]")) {
      const current = lastOf(fields.review_rating);
      const notify = lastOf(fields.rating) || inheritedThreshold();
      const effective = Math.min(current || inheritedReview(), notify);
      if (e.target.closest("[data-rev-clear]")) return applyInline(sectionName, "review_rating", null);
      const next = Math.max(1, Math.min(5, effective + (e.target.closest("[data-rev-inc]") ? 1 : -1)));
      if (next > notify) {
        setFoot(`Review cannot be higher than notify (≥ ${notify}) — raise the notify threshold first.`, true);
        toast("Review cannot be higher than notify", { error: true });
        return;
      }
      if (next === effective && current) return;
      return applyInline(sectionName, "review_rating", next);
    }

    const src = e.target.closest("[data-src]");
    if (src) {
      const all = marketplaceNames();
      const bound = fields.marketplace ? [].concat(fields.marketplace) : all.slice();
      const name = src.dataset.src;
      const next = bound.includes(name) ? bound.filter((x) => x !== name) : bound.concat([name]);
      if (!next.length) {
        setFoot("An item needs at least one source — pause the item instead.", true);
        toast("An item needs at least one source — pause the item instead", { error: true });
        return;
      }
      // Omit the key when every source is selected, so the config keeps
      // meaning "all marketplaces" rather than freezing today's list.
      const sameAsAll = next.length === all.length && all.every((x) => next.includes(x));
      return applyInline(sectionName, "marketplace", sameAsAll ? null : next);
    }

    const chipDel = e.target.closest("[data-chip-del]");
    if (chipDel) {
      const key = chipDel.dataset.chipDel;
      const current = [].concat(fields[key] || []);
      const next = current.filter((v) => v !== chipDel.dataset.chipVal);
      if (key === "search_phrases" && !next.length) {
        setFoot("An item needs at least one search phrase.", true);
        toast("An item needs at least one search phrase", { error: true });
        return;
      }
      return applyInline(sectionName, key, next.length ? next : null);
    }

    const act = e.target.closest("[data-act]");
    if (act) {
      const kind = act.dataset.act;
      if (kind === "edit") openEditSectionModal(section.name);
      else if (kind === "rename") openEditSectionModal(section.name, { focusName: true });
      else if (kind === "duplicate") duplicateSection(section);
      else if (kind === "delete") {
        if (deleteSection(section)) {
          C.openItem = null;
          renderItems();
        }
      }
    }
  });
  itemsPage.addEventListener("keydown", (e) => {
    const input = e.target.closest("[data-chip-add]");
    if (input && e.key === "Enter") {
      e.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      const sectionName = itemsPage.dataset.section;
      const section = sectionByName(sectionName);
      if (!section) return;
      const key = input.dataset.chipAdd;
      const current = [].concat(fieldsForSection(section)[key] || []);
      if (current.includes(value)) return;
      applyInline(sectionName, key, current.concat([value]));
      const again = itemsPage.querySelector(`[data-chip-add="${CSS.escape(key)}"]`);
      if (again) again.focus();
      return;
    }
    // Space / Enter flips a focused switch.
    const sw = e.target.closest(".tog[tabindex]");
    if (sw && (e.key === " " || e.key === "Enter")) {
      e.preventDefault();
      sw.click();
    }
  });
  // Text fields commit on change (blur). Bare integers write as TOML ints,
  // empty clears the key.
  itemsPage.addEventListener(
    "change",
    (e) => {
      const field = e.target.closest("[data-field]");
      if (!field) return;
      const sectionName = itemsPage.dataset.section;
      if (!sectionName) return;
      const raw = field.value.trim();
      let value = null;
      if (raw) value = /^-?\d+$/.test(raw) ? parseInt(raw, 10) : raw;
      applyInline(sectionName, field.dataset.field, value);
    },
    true
  );

  // ---------------------------------------------------------------
  // Sources screen
  // ---------------------------------------------------------------
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
  const itemsUsing = (mkName) =>
    itemSections()
      .filter((s) => {
        const f = fieldsForSection(s);
        return !f.marketplace || [].concat(f.marketplace).includes(mkName);
      })
      .map((s) => s.suffix || s.name);
  const envLines = (refs, env) =>
    refs.map((r) => `<div class="r envline"><div class="l">${esc(r)}</div><span class="v ${env[r] ? "okv" : "bad"}">${env[r] ? "✓ set" : "✗ not set"}</span></div>`).join("");

  const renderSources = () => {
    const host = $("#sources-page");
    const env = state.envVars || {};
    const fb = (state.monitorInfo && state.monitorInfo.fb_session) || {};
    const st = state.status || {};
    const now = Date.now() / 1000;
    const KIND_LABEL = { facebook: "Facebook Marketplace", ebay: "eBay", depop: "Depop", poshmark: "Poshmark" };
    const KIND_DESC = { ebay: "browser scrape or official API · no developer key needed", depop: "browser scrape · ships nationwide", poshmark: "browser scrape · ships nationwide", facebook: "browser · local pickup" };

    const mkRows = [];
    const configuredKinds = new Set();
    let homeLocation = "";
    for (const mk of marketplaceSections()) {
      const f = fieldsForSection(mk);
      const kind = marketKind(mk);
      configuredKinds.add(kind);
      let dot = "ok";
      let detail = "";
      let ebayMode = "";
      if (kind === "facebook") {
        if (f.home_location) homeLocation = f.home_location;
        const homeTxt = f.home_location ? " · home " + f.home_location : " · no home set — distances and maps are off";
        if (fb.logged_in) detail = "signed in" + homeTxt;
        else if (fb.exists) {
          dot = "warn";
          detail = "anonymous session — log in via the browser" + homeTxt;
        } else {
          dot = "warn";
          detail = "not signed in yet" + homeTxt;
        }
      } else if (kind === "ebay") {
        // Mirrors EbayMarketplaceConfig.resolved_mode: an explicit mode wins,
        // otherwise credentials decide.
        ebayMode = String(f.mode || "").toLowerCase() || (f.client_id && f.client_secret ? "api" : "browser");
        if (ebayMode === "api") {
          detail = "official Browse API";
          if (!(f.client_id && f.client_secret)) {
            dot = "warn";
            detail = "API mode, but no key set — add one or switch to browser mode";
          }
        } else detail = "browser scrape · no developer key needed";
      } else detail = KIND_DESC[kind] || "marketplace";
      if (f.enabled === false) {
        dot = "dim";
        detail = "disabled";
      }
      const used = itemsUsing(mk.suffix);
      detail += used.length ? " · " + used.join(", ") : " · not used by any item";
      const refs = ebayMode === "browser" ? [] : envRefsIn(f);
      if (refs.some((r) => env[r] === false)) dot = "err";
      mkRows.push(
        `<button class="r tap" data-edit-section="${esc(mk.name)}"><span class="src ${esc(kind)}">${esc(mk.suffix[0] || "?")}</span><div class="l">${esc(KIND_LABEL[kind] || mk.suffix)}${mk.suffix !== kind ? ` <span class="muted">[${esc(mk.suffix)}]</span>` : ""}<small>${esc(detail)}</small></div><span class="dot ${dot}"></span><span class="chev">›</span></button>` +
          envLines(refs, env)
      );
    }
    for (const kind of state.supportedMarketplaces || []) {
      if (configuredKinds.has(kind)) continue;
      mkRows.push(`<button class="r tap avail" data-setup-marketplace="${esc(kind)}"><span class="src ${esc(kind)}">${esc(kind[0])}</span><div class="l">${esc(KIND_LABEL[kind] || kind)}<small>${esc(KIND_DESC[kind] || "marketplace")} · not configured</small></div><span class="tag">Set up</span><span class="chev">›</span></button>`);
    }
    mkRows.push(`<button class="r tap link" data-add="marketplace"><div class="l">+ Add a marketplace section</div></button>`);

    let fbText = "no saved session";
    let fbDot = "dim";
    if (fb.logged_in) {
      fbDot = "ok";
      fbText = "signed in" + (fb.saved_at ? " · saved " + fmtDur(now - fb.saved_at) + " ago" : "");
    } else if (fb.exists) {
      fbDot = "warn";
      fbText = "anonymous — complete the login in the browser";
    }
    const fbGroup = `
      <div class="gl">Facebook session</div>
      <div class="g">
        ${st.vnc_enabled ? `<a class="r tap" id="browser-link" href="/vnc/vnc.html?path=ws/vnc&autoconnect=1&resize=scale" target="_blank" rel="noopener"><div class="l">Open browser<small>noVNC · sign in or clear a 2FA prompt</small></div><span class="chev">›</span></a>` : `<div class="r"><div class="l">Browser view<small>not enabled in this deployment (AIMM_ENABLE_VNC)</small></div></div>`}
        <div class="r"><div class="l">Cookies</div><span class="v">${esc(fbText)}</span><span class="dot ${fbDot}"></span></div>
      </div>`;

    const plumbing = [];
    for (const ai of C.sections.filter((x) => x.prefix === "ai")) {
      const f = fieldsForSection(ai);
      const refs = envRefsIn(f);
      const dot = refs.some((r) => env[r] === false) ? "err" : "ok";
      plumbing.push(`<button class="r tap" data-edit-section="${esc(ai.name)}"><span class="src ai">AI</span><div class="l">${esc(ai.suffix || ai.name)}<small>${esc([f.model, f.base_url].filter(Boolean).join(" · ") || "AI backend")}</small></div><span class="dot ${dot}"></span><span class="chev">›</span></button>` + envLines(refs, env));
    }
    for (const nt of C.sections.filter((x) => x.prefix === "notification")) {
      const f = fieldsForSection(nt);
      const refs = envRefsIn(f);
      const dot = refs.some((r) => env[r] === false) ? "err" : "ok";
      plumbing.push(`<button class="r tap" data-edit-section="${esc(nt.name)}"><span class="src ai">N</span><div class="l">${esc(nt.suffix || nt.name)}<small>${esc(f.ntfy_server ? "ntfy · " + f.ntfy_server + (f.ntfy_topic ? " · topic " + f.ntfy_topic : "") : "notification channel")}</small></div><span class="dot ${dot}"></span><span class="chev">›</span></button>` + envLines(refs, env));
    }
    for (const us of C.sections.filter((x) => x.prefix === "user")) {
      const f = fieldsForSection(us);
      const refs = envRefsIn(f);
      const dot = refs.some((r) => env[r] === false) ? "err" : "ok";
      plumbing.push(`<button class="r tap" data-edit-section="${esc(us.name)}"><span class="src ai">U</span><div class="l">${esc(us.suffix || us.name)}<small>notify via ${esc([].concat(f.notify_with || []).join(", ") || "—")}${f.email ? " · " + esc([].concat(f.email).join(", ")) : ""}</small></div><span class="dot ${dot}"></span><span class="chev">›</span></button>` + envLines(refs, env));
    }
    const fbSection = marketplaceSections().find((mk) => marketKind(mk) === "facebook");
    plumbing.push(`<button class="r tap" ${fbSection ? `data-edit-section="${esc(fbSection.name)}" data-tab="right"` : 'data-setup-marketplace="facebook"'}><div class="l">Home location<small>drives distances, the pickup map and drive times</small></div><span class="v val">${esc(homeLocation || "not set")}</span><span class="chev">›</span></button>`);
    plumbing.push(`<div class="r"><div class="l">Drive times (OSRM)<small>public demo router, cached a day per listing</small></div><span class="v">${homeLocation ? "on" : "off · needs a home location"}</span><span class="dot ${homeLocation ? "ok" : "dim"}"></span></div>`);
    plumbing.push(`<button class="r tap link" data-add="ai"><div class="l">+ Add AI backend</div></button>`);
    plumbing.push(`<button class="r tap link" data-add="user"><div class="l">+ Add user / notification target</div></button>`);

    host.innerHTML =
      `<div class="gl">Marketplaces</div><div class="g">${mkRows.join("")}</div>` +
      fbGroup +
      `<div class="gl">Plumbing</div><div class="g">${plumbing.join("")}</div>` +
      editorGroup(true);
    syncHelpButtons();
  };

  $("#sources-page").addEventListener("click", (e) => {
    const setup = e.target.closest("[data-setup-marketplace]");
    if (setup) return openAddSectionModal("marketplace", setup.dataset.setupMarketplace);
    const edit = e.target.closest("[data-edit-section]");
    if (edit) return openEditSectionModal(edit.dataset.editSection, { tab: edit.dataset.tab });
    const add = e.target.closest("[data-add]");
    if (add) return openAddSectionModal(add.dataset.add);
  });

  // ---------------------------------------------------------------
  // Config mode (Form / TOML) and help level
  // ---------------------------------------------------------------
  const showConfigMode = (mode) => {
    C.mode = mode;
    $$("[data-config-mode]").forEach((b) => b.classList.toggle("on", b.dataset.configMode === mode));
    $("#items-page").classList.toggle("hidden", mode !== "form");
    $("#toml-pane").classList.toggle("hidden", mode !== "toml");
    $("#items-back").hidden = mode === "toml" || !C.openItem;
    if (mode === "toml") {
      $("#items-title").textContent = "Config";
      // CodeMirror measures wrong if laid out while display:none.
      if (editor.refresh) editor.refresh();
      renderGutter();
    } else renderItems();
  };
  const applyHelpLevel = (level) => {
    document.body.classList.remove("help-hints", "help-guided");
    if (level !== "off") document.body.classList.add("help-" + level);
    C.helpLevel = level;
    syncHelpButtons();
    try {
      localStorage.setItem("aimm.helpLevel", level);
    } catch (_) {
      /* private browsing: the choice just does not persist */
    }
  };
  const syncHelpButtons = () => $$("[data-help]").forEach((b) => b.classList.toggle("on", b.dataset.help === (C.helpLevel || "hints")));
  document.addEventListener("click", (e) => {
    const mode = e.target.closest("[data-config-mode]");
    if (mode) {
      if (state.view !== "items") window.AIMM.showView("items");
      showConfigMode(mode.dataset.configMode);
      return;
    }
    const help = e.target.closest("[data-help]");
    if (help) applyHelpLevel(help.dataset.help);
  });
  let savedHelp = "hints";
  try {
    savedHelp = localStorage.getItem("aimm.helpLevel") || "hints";
  } catch (_) {
    /* ignore */
  }
  applyHelpLevel(savedHelp);

  const rerender = () => {
    if (state.view === "items" && C.mode === "form") renderItems();
    else if (state.view === "sources") renderSources();
  };
  on("monitor", () => {
    if (state.view === "sources") renderSources();
  });
  on("env", () => {
    if (state.view === "sources") renderSources();
  });
  on("activity", () => {
    if (state.view === "items" && C.mode === "form" && !C.openItem) renderItems();
  });

  document.addEventListener("tomlEditReady", () => {
    parsedConfig.content = null;
    rerender();
  });
  window.AIMM.views.items = { show: () => showConfigMode(C.mode) };
  window.AIMM.views.sources = { show: () => renderSources() };
  window.AIMM.boot(loadConfig);

  Object.assign(C, {
    load: loadConfig, save: saveConfig, validate: validateConfig, editor, fieldsForSection, sectionByName,
    openEdit: openEditSectionModal, openAdd: openAddSectionModal, applyInline, itemCadence, FORM_SCHEMAS,
    showConfigMode, renderItems, renderSources, scanSectionsClient,
  });
  window.__aimm = Object.assign(window.__aimm || {}, { itemCadence, FORM_SCHEMAS, LIST_VALUED_KEYS });
})();
