"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const REFRESH_MS = 5000;
  let state = null;
  let timer = null;
  let editing = null;
  let sharing = null;
  let usageOf = null;
  let openMenu = null;

  // ------------------------------------------------------------ helpers

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function formatBytes(bytes) {
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let value = Number(bytes) || 0;
    let unit = 0;
    while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit++; }
    if (unit === 0) return `${value} B`;
    return `${value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${units[unit]}`;
  }

  function formatRate(perSecond) {
    return perSecond > 0 ? `${formatBytes(perSecond)}/s` : "";
  }

  function ago(seconds) {
    if (seconds === null || seconds === undefined) return "";
    const s = Math.max(0, Math.round(seconds));
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  function duration(seconds) {
    const s = Math.round(seconds);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return m ? `${m}m` : `${s}s`;
  }

  function clock(epoch) {
    const d = new Date(epoch * 1000);
    const today = new Date();
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (d.toDateString() === today.toDateString()) return time;
    return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
  }

  function toast(message, ok = true) {
    const node = el("div", { class: ok ? "toast" : "toast bad", role: ok ? "status" : "alert", text: message });
    $("toasts").append(node);
    setTimeout(() => node.remove(), ok ? 5000 : 9000);
  }

  async function api(method, path, body) {
    const opts = { method, headers: { "X-Nipovpn-Dashboard": "1" }, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(`/api/${path}`, opts);
    } catch (e) {
      throw new Error("cannot reach the dashboard");
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty */ }
    if (res.status === 401 && path !== "login") {
      showLogin();
      throw new Error("log in first");
    }
    if (!res.ok) throw new Error(data.message || `request failed (${res.status})`);
    return data;
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const t = el("textarea", {}, text);
      document.body.append(t);
      t.select();
      document.execCommand("copy");
      t.remove();
    }
    toast("Copied");
  }

  function confirmAction(title, text, label = "Confirm", danger = true) {
    const dlg = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-yes").textContent = label;
    $("confirm-yes").className = danger ? "danger" : "primary";
    dlg.returnValue = "";
    dlg.showModal();
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "yes"), { once: true });
    });
  }

  function reportChange(result) {
    const text = result.change ? `${result.change}. ${result.message}` : result.message;
    toast(text, result.ok !== false);
  }

  // ------------------------------------------------------------ login

  function showLogin() {
    clearInterval(timer);
    timer = null;
    $("app").hidden = true;
    $("login").hidden = false;
    $("password").focus();
  }

  function showApp() {
    $("login").hidden = true;
    $("app").hidden = false;
    showTab();
    refresh();
    clearInterval(timer);
    timer = setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  }

  $("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("login-error").textContent = "";
    const button = ev.submitter || $("login-form").querySelector("button");
    button.disabled = true;
    try {
      await api("POST", "login", { password: $("password").value });
      $("password").value = "";
      showApp();
    } catch (e) {
      $("login-error").textContent = e.message;
    } finally {
      button.disabled = false;
    }
  });

  $("logout-btn").addEventListener("click", async () => {
    try { await api("POST", "logout"); } catch (e) { /* ignore */ }
    showLogin();
  });

  // ------------------------------------------------------------ overview

  async function refresh() {
    try {
      state = await api("GET", "overview");
      render();
    } catch (e) {
      if (e.message !== "log in first") $("server-pill").textContent = e.message;
    }
  }

  function render() {
    const s = state.server;
    const pill = $("server-pill");
    pill.className = `pill ${s.running ? "good" : "bad"}`;
    pill.textContent = s.running ? "Server running" : "Server not running";

    const problems = $("problems");
    problems.replaceChildren(...state.problems.map((p) => el("div", { class: "notice", text: p })));

    $("c-server").textContent = s.running ? `Up ${s.uptime != null ? duration(s.uptime) : ""}` : "Stopped";
    $("c-server-sub").textContent = [
      `port ${s.listenPort}`,
      s.tlsEnable ? "TLS" : "no TLS",
      s.tunnelEnable ? "tunnel" : "polling",
      s.aesEnable ? "AES" : "signed",
    ].join(" · ");

    const users = state.users;
    const enabled = users.filter((u) => u.enabled).length;
    const blocked = users.filter((u) => u.enabled && u.blocked).length;
    const active = users.filter((u) => u.lastSeen && state.now - u.lastSeen < 300).length;
    $("c-users").textContent = `${enabled} / ${users.length}`;
    $("c-users-sub").textContent = `enabled · ${active} active in 5 min${blocked ? ` · ${blocked} over limit` : ""}`;

    const total = users.reduce((a, u) => a + u.used, 0);
    const rate = users.reduce((a, u) => a + (u.rate || 0), 0);
    $("c-traffic").textContent = formatBytes(total);
    $("c-traffic-sub").textContent = [
      s.period ? `since ${s.period}` : "",
      rate ? `now ${formatRate(rate)}` : "",
    ].filter(Boolean).join(" · ");

    $("c-conns").textContent = s.connections == null ? "–" : String(s.connections);
    $("c-conns-sub").textContent = s.connections == null ? "server not found in /proc" : `TCP on port ${s.listenPort}`;

    renderRestartNotice();
    $("add-btn").disabled = !state.managed;
    renderUsers();
    renderEvents();

    const orphans = $("orphans");
    orphans.hidden = !state.orphans.length;
    orphans.textContent = state.orphans.length
      ? `Usage file also has removed users: ${state.orphans.join(", ")}`
      : "";
  }

  function renderUsers() {
    if (openMenu) return; // keep an open menu stable; the next refresh redraws
    const filter = $("search").value.trim().toLowerCase();
    const rows = state.users.filter((u) => !filter || u.name.toLowerCase().includes(filter));
    const body = $("users-body");
    if (!rows.length) {
      const text = state.users.length ? "No user matches the filter" : "No users yet";
      body.replaceChildren(el("tr", {}, el("td", { class: "empty", colspan: "5", text })));
      return;
    }
    body.replaceChildren(...rows.map(userRow));
  }

  function statusPill(u) {
    if (!u.enabled) return el("span", { class: "pill", text: "Disabled" });
    if (u.blocked) return el("span", { class: "pill bad", text: "Over limit" });
    if (u.lastSeen && state.now - u.lastSeen < 300) return el("span", { class: "pill good", text: "Online" });
    return el("span", { class: "pill", text: "Idle" });
  }

  function userRow(u) {
    const pct = u.limitBytes ? Math.min(100, (u.used / u.limitBytes) * 100) : 0;
    const bar = el("div", { class: `bar ${!u.limitBytes ? "none" : pct >= 100 ? "bad" : pct >= 80 ? "warn" : ""}` }, el("span"));
    bar.firstChild.style.width = `${pct}%`;

    const usageText = u.limitBytes
      ? `${formatBytes(u.used)} of ${u.limit} (${Math.floor((u.used / u.limitBytes) * 100)}%)`
      : `${formatBytes(u.used)} · no limit`;

    const activity = [];
    if (u.lastSeen) {
      activity.push(el("div", { text: ago(state.now - u.lastSeen) }));
      const sub = [u.lastSource, u.connectsLastHour ? `${u.connectsLastHour} conn/h` : ""].filter(Boolean).join(" · ");
      if (sub) activity.push(el("div", { class: "user-sub", text: sub }));
    } else {
      activity.push(el("span", { class: "muted", text: "Not seen" }));
    }

    return el("tr", { class: u.enabled ? "" : "off" },
      el("td", {},
        el("div", { class: "user-name", text: u.name }),
        u.sampleToken ? el("div", { class: "user-sub warn", text: "uses the public sample token" }) : null),
      el("td", {}, statusPill(u)),
      el("td", {}, bar, el("div", { class: "usage-text" }, usageText,
        u.rate ? el("span", { class: "muted", text: ` · ${formatRate(u.rate)}` }) : null)),
      el("td", {}, activity),
      el("td", {}, el("div", { class: "actions" },
        el("button", { class: "ghost", onclick: () => openShare(u.name), text: "Share" }),
        el("button", { class: "ghost", onclick: () => openEdit(u), text: "Edit" }),
        menu(u))));
  }

  function menu(u) {
    const wrap = el("div", { class: "menu" });
    const list = el("div", { class: "menu-list", hidden: true, role: "menu" },
      el("button", { role: "menuitem", onclick: () => run(() => toggle(u)), text: u.enabled ? "Disable" : "Enable" }),
      el("button", { role: "menuitem", onclick: () => run(() => openUsage(u)), text: "Set usage…" }),
      el("button", { role: "menuitem", onclick: () => run(() => resetUsage(u)), text: "Reset usage to 0" }),
      el("button", { role: "menuitem", onclick: () => run(() => regenerate(u)), text: "New token…" }),
      el("hr"),
      el("button", { role: "menuitem", class: "bad", onclick: () => run(() => remove(u)), text: "Delete…" }));
    const trigger = el("button", {
      class: "icon", "aria-label": `More actions for ${u.name}`, "aria-haspopup": "menu", text: "⋯",
      onclick: (ev) => {
        ev.stopPropagation();
        const willOpen = list.hidden;
        closeMenu();
        if (willOpen) { list.hidden = false; openMenu = list; placeMenu(list, trigger); }
      },
    });
    function run(fn) { closeMenu(); fn(); }
    wrap.append(trigger, list);
    return wrap;
  }

  function placeMenu(list, trigger) {
    const r = trigger.getBoundingClientRect();
    const h = list.offsetHeight;
    const below = window.innerHeight - r.bottom - 8;
    list.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
    list.style.top = below >= h || r.top < h ? `${r.bottom + 4}px` : `${r.top - h - 4}px`;
  }

  function closeMenu() {
    if (openMenu) { openMenu.hidden = true; openMenu = null; }
  }
  document.addEventListener("click", () => { if (openMenu) { closeMenu(); renderUsers(); } });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && openMenu) { closeMenu(); renderUsers(); } });
  for (const name of ["scroll", "resize"]) {
    window.addEventListener(name, () => { if (openMenu) { closeMenu(); renderUsers(); } }, true);
  }

  function renderEvents() {
    const list = $("events-list");
    const events = state.events.slice().reverse();
    if (!events.length) {
      list.replaceChildren(el("li", { class: "empty", text: "Nothing yet" }));
      return;
    }
    list.replaceChildren(...events.map((e) =>
      el("li", { class: e.level }, el("time", { text: clock(e.time) }), el("span", { class: "msg", text: e.message }))));
  }

  $("search").addEventListener("input", () => state && renderUsers());

  // ------------------------------------------------------------ actions

  async function change(method, path, body, busy) {
    if (busy) busy.disabled = true;
    try {
      const result = await api(method, path, body);
      reportChange(result);
      await refresh();
      return result;
    } finally {
      if (busy) busy.disabled = false;
    }
  }

  async function toggle(u) {
    if (u.enabled) {
      const ok = await confirmAction(`Disable ${u.name}?`,
        "Their open connections are closed and their agent is refused until you enable them again.", "Disable");
      if (!ok) return;
    }
    try {
      await change("PATCH", `users/${encodeURIComponent(u.name)}`, { enabled: !u.enabled });
    } catch (e) { toast(e.message, false); }
  }

  async function regenerate(u) {
    const ok = await confirmAction(`New token for ${u.name}?`,
      "Their current agent stops working. Send them the new config or link afterwards.", "Create new token");
    if (!ok) return;
    try {
      await change("PATCH", `users/${encodeURIComponent(u.name)}`, { regenerateToken: true });
      openShare(u.name);
    } catch (e) { toast(e.message, false); }
  }

  async function remove(u) {
    const ok = await confirmAction(`Delete ${u.name}?`,
      "The user is removed from the server config and their connections are closed. This cannot be undone here.", "Delete");
    if (!ok) return;
    try {
      await change("DELETE", `users/${encodeURIComponent(u.name)}`);
    } catch (e) { toast(e.message, false); }
  }

  async function resetUsage(u) {
    const ok = await confirmAction(`Reset usage of ${u.name}?`,
      `Their usage this period (${formatBytes(u.used)}) is set to 0.`, "Reset", false);
    if (!ok) return;
    try {
      await change("POST", `users/${encodeURIComponent(u.name)}/usage`, { used: 0 });
    } catch (e) { toast(e.message, false); }
  }

  $("reload-btn").addEventListener("click", async (ev) => {
    try { await change("POST", "reload", undefined, ev.currentTarget); } catch (e) { toast(e.message, false); }
  });

  // ------------------------------------------------------------ add / edit

  function randomToken() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function openEdit(u) {
    editing = u || null;
    $("user-title").textContent = u ? `Edit ${u.name}` : "Add user";
    $("u-name").value = u ? u.name : "";
    $("u-name").disabled = !!u;
    $("u-token").value = "";
    $("u-token").placeholder = u ? "Keep the current token" : "Generated when empty";
    $("u-token-hint").textContent = u
      ? "Leave empty to keep the current token. A new token stops their current agent."
      : "At least 32 characters. Leave empty to generate one.";
    $("u-limit").value = u ? u.limit : "";
    $("u-enabled").checked = u ? u.enabled : true;
    $("u-error").textContent = "";
    $("user-dialog").showModal();
    (u ? $("u-limit") : $("u-name")).focus();
  }

  $("add-btn").addEventListener("click", () => openEdit(null));
  $("u-gen").addEventListener("click", () => { $("u-token").value = randomToken(); });

  $("user-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("u-error").textContent = "";
    const token = $("u-token").value.trim();
    if (token && token.length < 32) {
      $("u-error").textContent = "The token must be at least 32 characters.";
      return;
    }
    const body = { limit: $("u-limit").value.trim(), enabled: $("u-enabled").checked };
    if (token) body.token = token;
    try {
      if (editing) {
        await change("PATCH", `users/${encodeURIComponent(editing.name)}`, body, $("u-save"));
        $("user-dialog").close();
      } else {
        body.name = $("u-name").value.trim();
        await change("POST", "users", body, $("u-save"));
        $("user-dialog").close();
        openShare(body.name);
      }
    } catch (e) {
      $("u-error").textContent = e.message;
    }
  });

  // ------------------------------------------------------------ share

  // The address and port come from the server config each time the dialog opens,
  // so changed settings show up at once. A typed address or port is used only
  // while the dialog is open; it is not remembered.
  let shareDefaults = null;

  async function loadShare() {
    $("s-error").textContent = "";
    const params = new URLSearchParams();
    const address = $("s-address").value.trim();
    const port = $("s-port").value.trim();
    if (shareDefaults && address && address !== shareDefaults.address) params.set("address", address);
    if (shareDefaults && port && port !== String(shareDefaults.port)) params.set("port", port);
    try {
      const data = await api("GET", `users/${encodeURIComponent(sharing)}/share?${params}`);
      if (!shareDefaults) shareDefaults = { address: data.address, port: data.port };
      $("s-address").value = data.address;
      $("s-port").value = data.port;
      $("s-url").value = data.url;
      $("s-yaml").value = data.yaml;
      $("s-aes-warning").hidden = data.aesEnable !== false;
      if (data.qrSvg) {
        $("s-qr").src = `data:image/svg+xml;base64,${btoa(data.qrSvg)}`;
      } else {
        $("s-qr").removeAttribute("src");
      }
      if (/^(127\.|localhost|0\.0\.0\.0)/.test(data.address)) {
        $("s-error").textContent = "Enter the public address of this server, or set it under Server settings > "
          + "Agent template > Server address; agents cannot reach a local address.";
      }
    } catch (e) {
      $("s-error").textContent = e.message;
      $("s-url").value = "";
      $("s-yaml").value = "";
      $("s-qr").removeAttribute("src");
    }
  }

  function openShare(name) {
    sharing = name;
    shareDefaults = null;
    try { localStorage.removeItem("nipovpn.shareAddress"); localStorage.removeItem("nipovpn.sharePort"); } catch (e) { /* ignore */ }
    $("share-title").textContent = `Agent config for ${name}`;
    $("s-address").value = "";
    $("s-port").value = "";
    $("s-url").value = "";
    $("s-yaml").value = "";
    $("s-qr").removeAttribute("src");
    $("s-aes-warning").hidden = true;
    $("share-dialog").showModal();
    loadShare();
  }

  $("s-update").addEventListener("click", loadShare);
  for (const id of ["s-address", "s-port"]) {
    $(id).addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); loadShare(); } });
  }
  $("s-download").addEventListener("click", () => {
    const blob = new Blob([$("s-yaml").value], { type: "application/x-yaml" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `nipovpn-${sharing}.yaml` });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  });

  // ------------------------------------------------------------ usage

  function openUsage(u) {
    usageOf = u;
    $("usage-title").textContent = `Usage of ${u.name}`;
    $("usage-current").textContent = `Now ${formatBytes(u.used)}${u.limitBytes ? ` of ${u.limit}` : ""} since ${state.server.period || "the start of the period"}.`;
    $("g-value").value = "0";
    $("g-unit").value = "1000000000";
    $("g-error").textContent = "";
    $("usage-dialog").showModal();
    $("g-value").select();
  }

  $("usage-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const value = Number($("g-value").value);
    if (!Number.isFinite(value) || value < 0) {
      $("g-error").textContent = "Enter a number of zero or more.";
      return;
    }
    try {
      await change("POST", `users/${encodeURIComponent(usageOf.name)}/usage`,
        { used: Math.round(value * Number($("g-unit").value)) });
      $("usage-dialog").close();
    } catch (e) {
      $("g-error").textContent = e.message;
    }
  });

  // ------------------------------------------------------------ dialogs

  document.addEventListener("click", (ev) => {
    const close = ev.target.closest("[data-close]");
    if (close) close.closest("dialog").close();
    const copyBtn = ev.target.closest("[data-copy]");
    if (copyBtn) {
      const text = $(copyBtn.dataset.copy).value;
      if (text) copy(text);
    }
  });

  // ------------------------------------------------------------ theme

  function currentTheme() {
    try { return localStorage.getItem("nipovpn.theme") || "system"; } catch (e) { return "system"; }
  }

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    try {
      if (theme === "system") localStorage.removeItem("nipovpn.theme");
      else localStorage.setItem("nipovpn.theme", theme);
    } catch (e) { /* not remembered */ }
    for (const b of document.querySelectorAll("[data-theme-choice]")) {
      b.setAttribute("aria-pressed", String(b.dataset.themeChoice === theme));
    }
  }

  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-theme-choice]");
    if (b) applyTheme(b.dataset.themeChoice);
  });
  applyTheme(currentTheme());

  // ------------------------------------------------------------ tabs

  function currentTab() {
    return { "#settings": "settings", "#events": "events" }[location.hash] || "users";
  }

  function showTab() {
    const tab = currentTab();
    $("view-users").hidden = tab !== "users";
    $("view-settings").hidden = tab !== "settings";
    $("view-events").hidden = tab !== "events";
    $("main").classList.toggle("full", tab === "users");
    for (const a of document.querySelectorAll("[data-tab]")) {
      if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    if (tab === "settings" && !$("app").hidden && !dirtyCount()) loadSettings();
  }

  window.addEventListener("hashchange", showTab);
  window.addEventListener("beforeunload", (ev) => {
    if (dirtyCount()) { ev.preventDefault(); ev.returnValue = ""; }
  });

  // ------------------------------------------------------------ restart

  function renderRestartNotice() {
    const pending = state.pendingRestart || [];
    $("restart-top-btn").hidden = !state.restartAvailable;
    $("restart-notice").hidden = !pending.length;
    if (!pending.length) return;
    const list = pending.map((p) => `${p.label} (running ${p.running}, saved ${fmtValue(p.value)})`).join(", ");
    $("restart-text").textContent = state.restartAvailable
      ? `Restart the server to apply: ${list}.`
      : `Restart the server to apply: ${list}. For example: systemctl restart nipovpn-server`;
    $("restart-btn").hidden = !state.restartAvailable;
  }

  async function restartServer(ev) {
    const btn = ev.currentTarget;
    const ok = await confirmAction("Restart the server?",
      "Every user's connections drop for a moment; agents reconnect by themselves.", "Restart");
    if (!ok) return;
    btn.disabled = true;
    try {
      const result = await api("POST", "restart");
      toast(result.message, result.ok);
      await refresh();
      if (currentTab() === "settings" && !dirtyCount()) loadSettings();
    } catch (e) {
      toast(e.message, false);
    } finally {
      btn.disabled = false;
    }
  }
  $("restart-btn").addEventListener("click", restartServer);
  $("restart-top-btn").addEventListener("click", restartServer);

  // ------------------------------------------------------------ settings

  let settings = null;
  const edits = new Map();

  function fmtValue(v) {
    if (Array.isArray(v)) return v.join(", ");
    if (v === true) return "on";
    if (v === false) return "off";
    if (v === "" || v === null || v === undefined) return "(empty)";
    return String(v);
  }

  function sameValue(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  function dirtyCount() {
    return edits.size;
  }

  async function loadSettings() {
    try {
      settings = await api("GET", "settings");
      edits.clear();
      renderSettings();
    } catch (e) {
      if (e.message !== "log in first") toast(e.message, false);
    }
  }

  function readInput(field, input) {
    if (field.type === "bool") return input.checked;
    if (field.type === "int") return input.value.trim() === "" ? "" : Number(input.value);
    if (field.type === "list" && field.choices) {
      return [...input.querySelectorAll("input:checked")].map((c) => c.value);
    }
    if (field.type === "list") return input.value.split("\n").map((v) => v.trim()).filter(Boolean);
    return input.value;
  }

  function fieldControl(field) {
    const id = `f-${field.id}`;
    let input;
    if (field.type === "bool") {
      input = el("input", { type: "checkbox", id, role: "switch" });
      input.checked = !!field.value;
      return [input, el("label", { class: "switch", for: id }, input, el("span", { text: field.value ? "On" : "Off" }))];
    }
    if (field.type === "list" && field.choices) {
      // Choices first, then anything else already in the config, so nothing is lost.
      const options = field.choices.concat(field.value.filter((v) => !field.choices.includes(v)));
      input = el("div", { id, class: "choices", role: "group", "aria-label": field.label },
        options.map((o) => {
          const box = el("input", { type: "checkbox", value: o });
          box.checked = field.value.includes(o);
          return el("label", { class: "check" }, box, ` ${o}`);
        }));
      return [input, input];
    }
    if (field.type === "enum") {
      input = el("select", { id }, field.options.map((o) => el("option", { value: o, text: o })));
      input.value = field.value;
    } else if (field.type === "list") {
      input = el("textarea", { id, rows: String(Math.max(3, field.value.length + 1)), spellcheck: "false", class: "mono" });
      input.value = field.value.join("\n");
    } else {
      const attrs = { id, spellcheck: "false", autocomplete: "off" };
      if (field.type === "int") Object.assign(attrs, { type: "number", min: field.min, max: field.max, step: "1", inputmode: "numeric" });
      if (field.type === "path" || field.type === "ip" || field.type === "host") attrs.class = "mono";
      input = el("input", attrs);
      input.value = field.value;
    }
    return [input, input];
  }

  function fieldRow(field) {
    const [input, control] = fieldControl(field);
    const row = el("div", { class: "field" });
    const tags = el("div", { class: "tags" });
    if (field.unit) tags.append(el("span", { class: "tag", text: field.unit }));
    if (field.restart) tags.append(el("span", { class: "tag warn", text: "restart to apply" }));
    if (field.agents) tags.append(el("span", { class: "tag bad", text: "agents must match" }));
    const hint = field.help ? el("p", { class: "hint", text: field.help }) : null;
    const running = field.pending
      ? el("p", { class: "hint", text: `The server is running with ${field.running}; restart it to apply ${fmtValue(field.value)}.` })
      : null;

    row.append(
      el("div", { class: "field-label" }, el("label", { for: `f-${field.id}`, text: field.label }), tags.childNodes.length ? tags : null),
      el("div", { class: "field-control" }, control, hint, running));

    const onChange = () => {
      const value = readInput(field, input);
      if (field.type === "bool") control.querySelector("span").textContent = value ? "On" : "Off";
      if (sameValue(value, field.value)) edits.delete(field.id);
      else edits.set(field.id, value);
      row.classList.toggle("dirty", edits.has(field.id));
      renderSavebar();
    };
    input.addEventListener("input", onChange);
    input.addEventListener("change", onChange);
    return row;
  }

  function renderSettings() {
    $("settings-file").textContent = `Editing ${settings.configFile}. Comments and everything not shown here stay as they are.`;
    const bySection = new Map(settings.sections.map((sec) => [sec.id, []]));
    for (const f of settings.fields) bySection.get(f.section).push(f);
    $("settings-sections").replaceChildren(...settings.sections.map((sec) =>
      el("section", { class: "panel settings-section", "aria-labelledby": `sec-${sec.id}` },
        el("div", { class: "panel-head" }, el("h2", { id: `sec-${sec.id}`, text: sec.title }), el("p", { text: sec.description })),
        bySection.get(sec.id).map(fieldRow))));
    renderSavebar();
  }

  function renderSavebar() {
    const n = edits.size;
    $("savebar").hidden = n === 0;
    $("savebar-text").textContent = `${n} unsaved change${n === 1 ? "" : "s"}`;
  }

  $("discard-btn").addEventListener("click", () => { edits.clear(); renderSettings(); });

  $("review-btn").addEventListener("click", () => {
    const fields = new Map(settings.fields.map((f) => [f.id, f]));
    $("review-error").textContent = "";
    $("review-list").replaceChildren(...[...edits].map(([id, value]) => {
      const f = fields.get(id);
      const notes = [];
      if (f.agents) notes.push("Existing agents stop working until they get a new config.");
      if (f.restart) notes.push("Applies when the server restarts.");
      return el("li", {},
        el("div", { class: "field-label", text: f.label }),
        el("div", {}, el("span", { class: "from", text: fmtValue(f.value) }), " → ", el("span", { class: "to", text: fmtValue(value) })),
        notes.map((n) => el("div", { class: "note", text: n })));
    }));
    $("review-dialog").showModal();
  });

  $("review-save").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    $("review-error").textContent = "";
    try {
      const result = await api("PATCH", "settings", { changes: Object.fromEntries(edits) });
      $("review-dialog").close();
      reportChange(result);
      for (const w of result.warnings || []) toast(`Warning: ${w}`, false);
      edits.clear();
      await loadSettings();
      await refresh();
    } catch (e) {
      $("review-error").textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  });

  // ------------------------------------------------------------ start

  api("GET", "overview").then((data) => { state = data; showApp(); }).catch(() => showLogin());
})();
