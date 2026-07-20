/* ============================================================================
   WordSeek dashboard — sidebar app (vanilla JS, no frameworks)
   Pages: Dashboard, Telegram, Autoplay, Custom Messages, Automation,
          Activity Logs, Users (admin), Settings.
   All server data rendered via textContent. Every POST carries the CSRF token.
   ========================================================================== */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const CSRF = document.querySelector('meta[name="csrf"]').content;
  const IS_ADMIN = document.querySelector('meta[name="is-admin"]').content === "1";
  const toastStack = $("toastStack");
  let SOUND = false;

  async function api(path, body, method) {
    const opt = { method: method || (body ? "POST" : "GET"), headers: {} };
    if (body) { opt.headers["Content-Type"] = "application/json"; opt.headers["X-CSRF-Token"] = CSRF; opt.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, opt); } catch { return { ok: false, error: "Network error" }; }
    if (res.status === 401) { window.location.href = "/login"; return null; }
    let data = {}; try { data = await res.json(); } catch {}
    if (res.status === 429) data.error = data.error || "Too many requests";
    return data;
  }
  function toast(msg, kind = "success") {
    if (SOUND) { try { beep(kind); } catch {} }
    const el = document.createElement("div"); el.className = `toast ${kind}`;
    const i = document.createElement("span"); i.className = "toast-icon";
    i.textContent = kind === "error" ? "!" : kind === "info" ? "i" : "✓";
    const s = document.createElement("span"); s.textContent = msg;
    el.append(i, s); toastStack.appendChild(el);
    setTimeout(() => { el.classList.add("out"); el.addEventListener("animationend", () => el.remove(), { once: true }); }, 1800);
    while (toastStack.children.length > 3) toastStack.firstChild.remove();
  }
  function ack(r, ok) { if (!r) return; if (r.ok === false || r.error) toast(r.error || "Failed", "error"); else if (ok) toast(ok, "success"); }
  const clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
  const dt = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "never";
  function ago(ts) { if (!ts) return "—"; const s = Math.max(0, Math.floor(Date.now() / 1000 - ts)); return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`; }
  function confidences(n) { const w = Array.from({ length: n }, (_, i) => 1 / Math.pow(i + 1, 0.62)); const m = w[0] || 1; return w.map((x) => x / m); }
  async function copy(t) { try { await navigator.clipboard.writeText(t); } catch { const a = document.createElement("textarea"); a.value = t; a.style.position = "fixed"; a.style.opacity = "0"; document.body.appendChild(a); a.select(); try { document.execCommand("copy"); } catch {} a.remove(); } }

  /* ---------------- navigation ---------------- */
  const pages = [...document.querySelectorAll(".page")];
  const navItems = [...document.querySelectorAll(".nav-item")];
  const titles = { dashboard: "Dashboard", telegram: "Telegram", groups: "Groups", autoplay: "Autoplay", messages: "Messages", automation: "Automation", history: "History", stats: "Statistics", logs: "Logs", users: "Users", settings: "Settings" };
  function go(page) {
    pages.forEach((p) => { p.hidden = p.dataset.page !== page; });
    navItems.forEach((n) => n.classList.toggle("active", n.dataset.page === page));
    $("pageTitle").textContent = titles[page] || "Dashboard";
    $("sidebar").classList.remove("open");
    if (page === "messages") loadMessages();
    if (page === "automation") loadRules();
    if (page === "users") loadUsers();
    if (page === "groups") loadGroupsPage();
    if (page === "history") loadHistory();
    if (page === "stats") loadStats();
    if (page === "logs") loadLogs();
    if (page === "settings") { $("setUsername").value = ""; loadPrefs(); }
    location.hash = page;
  }
  navItems.forEach((n) => n.addEventListener("click", () => go(n.dataset.page)));
  $("hamburger").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  if (IS_ADMIN) document.querySelectorAll(".admin-only").forEach((e) => (e.hidden = false));

  /* ---------------- telegram login ---------------- */
  const connectCard = $("connectCard"), tgConnected = $("tgConnected"), loginMsg = $("loginMsg");
  function showStep(name) { $("stepPhone").hidden = name !== "phone"; $("stepCode").hidden = name !== "code"; $("stepPassword").hidden = name !== "password"; }
  function loginNote(t, k) { loginMsg.hidden = !t; loginMsg.textContent = t || ""; loginMsg.className = "login-msg " + (k || ""); }
  $("sendCodeBtn").addEventListener("click", async () => {
    const phone = $("phoneInput").value.trim(); if (!phone) return loginNote("Enter your phone number.", "err");
    loginNote("Sending code…", ""); const r = await api("/api/tg/send_code", { csrf: CSRF, phone });
    if (r && r.ok && r.next === "code") { showStep("code"); loginNote("Code sent — check Telegram.", "ok"); $("codeInput").focus(); }
    else loginNote((r && r.error) || "Could not send code.", "err");
  });
  $("signInBtn").addEventListener("click", async () => {
    const code = $("codeInput").value.trim(); if (!code) return loginNote("Enter the code.", "err");
    loginNote("Verifying…", ""); const r = await api("/api/tg/sign_in", { csrf: CSRF, code });
    if (r && r.ok && r.next === "password") { showStep("password"); loginNote("Enter your 2FA password.", ""); $("passwordInput").focus(); }
    else if (r && r.ok && r.next === "done") { loginNote("Connected!", "ok"); toast("Telegram connected"); poll(); }
    else loginNote((r && r.error) || "Sign-in failed.", "err");
  });
  $("passwordBtn").addEventListener("click", async () => {
    const password = $("passwordInput").value; if (!password) return loginNote("Enter your 2FA password.", "err");
    loginNote("Checking…", ""); const r = await api("/api/tg/password", { csrf: CSRF, password });
    if (r && r.ok && r.next === "done") { loginNote("Connected!", "ok"); toast("Telegram connected"); poll(); }
    else loginNote((r && r.error) || "Incorrect password.", "err");
  });
  $("restartBtn").addEventListener("click", () => { showStep("phone"); loginNote("", ""); });
  $("logoutTgBtn").addEventListener("click", async () => { ack(await api("/api/tg/logout", { csrf: CSRF }), "Logged out"); showStep("phone"); poll(); });

  $("loadGroupsBtn").addEventListener("click", async () => {
    const sel = $("groupSelect"); sel.innerHTML = '<option value="">Loading…</option>';
    const r = await api("/api/groups"); sel.innerHTML = "";
    if (!r || !r.ok) { sel.innerHTML = '<option value="">Connect first…</option>'; ack(r || { error: "Could not load groups" }); return; }
    if (!r.groups.length) { sel.innerHTML = '<option value="">No groups found</option>'; return; }
    const ph = document.createElement("option"); ph.value = ""; ph.textContent = "Select a group…"; sel.appendChild(ph);
    r.groups.forEach((g) => { const o = document.createElement("option"); o.value = g.id; o.textContent = `${g.name}  (${g.id})`; o.dataset.name = g.name; sel.appendChild(o); });
    toast(`Loaded ${r.groups.length} groups`, "info");
  });
  $("setGroupBtn").addEventListener("click", async () => {
    const opt = $("groupSelect").selectedOptions[0]; if (!opt || !opt.value) return toast("Pick a group first", "error");
    ack(await api("/api/select_group", { csrf: CSRF, id: opt.value, name: opt.dataset.name }), "Target group set"); poll();
  });

  /* ---------------- autoplay ---------------- */
  const cfg = { max: $("cfgMax"), delay: $("cfgDelay"), cooldown: $("cfgCooldown"), conf: $("cfgConf"), repeat: $("cfgRepeat"), autoStart: $("cfgAutoStart"), autoStop: $("cfgAutoStop"), sendCmd: $("cfgSendCmd"), cmd: $("cfgCmd") };
  let cfgTouched = false, currentBest = null;
  const RUNCLS = { WAITING_FOR_BOARD: "wait", BOARD_DETECTED: "wait", SOLVING: "run", WAITING_TO_SEND: "run", GUESS_SENT: "run", WAITING_FOR_UPDATE: "wait", WON: "won", LOST: "lost", ERROR: "err", PAUSED: "paused", IDLE: "" };
  Object.values(cfg).forEach((el) => { el.addEventListener("focus", () => (cfgTouched = true)); el.addEventListener("blur", () => setTimeout(() => (cfgTouched = false), 400)); });
  $("apEnabled").addEventListener("change", async () => { ack(await api("/api/autoplay/enabled", { csrf: CSRF, enabled: $("apEnabled").checked }), $("apEnabled").checked ? "Autoplay ON" : "Autoplay OFF"); poll(); });
  $("apStartBtn").addEventListener("click", async () => { const run = $("apStartBtn").dataset.run === "1"; ack(await api(run ? "/api/autoplay/stop" : "/api/autoplay/start", { csrf: CSRF }), run ? "Stopped" : "Started"); poll(); });
  $("apPauseBtn").addEventListener("click", async () => { const p = $("apPauseBtn").dataset.paused === "1"; ack(await api(p ? "/api/autoplay/resume" : "/api/autoplay/pause", { csrf: CSRF }), p ? "Resumed" : "Paused"); poll(); });
  $("apStopBtn").addEventListener("click", async () => { ack(await api("/api/autoplay/emergency", { csrf: CSRF }), "Emergency stop"); poll(); });
  $("cfgSaveBtn").addEventListener("click", async () => {
    cfgTouched = false;
    ack(await api("/api/autoplay_config", { csrf: CSRF, max_guesses: +cfg.max.value, delay_ms: +cfg.delay.value, cooldown_ms: +cfg.cooldown.value, min_confidence: +cfg.conf.value, max_repeat: +cfg.repeat.value, auto_start_on_new_game: cfg.autoStart.checked, auto_stop_after_game: cfg.autoStop.checked, send_new_game_command: cfg.sendCmd.checked, new_game_command: cfg.cmd.value }), "Settings saved"); poll();
  });
  $("autoSendToggle").addEventListener("change", async () => { ack(await api("/api/auto_send", { csrf: CSRF, enabled: $("autoSendToggle").checked }), $("autoSendToggle").checked ? "Auto-send ON" : "Auto-send OFF"); poll(); });
  async function sendGuess(w) { const r = await api("/api/send", { csrf: CSRF, word: w }); ack(r, r && r.ok ? `Sent ${w.toUpperCase()}` : null); poll(); }
  $("sendBestBtn").addEventListener("click", () => currentBest && sendGuess(currentBest));
  $("copyBestBtn").addEventListener("click", () => { if (currentBest) { copy(currentBest.toUpperCase()); toast("Copied", "info"); } });

  /* ---------------- status render ---------------- */
  async function poll() { const st = await api("/api/status"); if (st) render(st); }
  function logList(el, items, empty) {
    el.innerHTML = "";
    if (!items || !items.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = empty; el.appendChild(li); return; }
    items.slice(0, 30).forEach((e) => { const li = document.createElement("li"); const t = document.createElement("span"); t.className = "t"; t.textContent = clock(e.ts); const m = document.createElement("span"); m.className = "m"; m.textContent = e.text; li.append(t, m); el.appendChild(li); });
  }
  function render(st) {
    if (!st) return;
    // pill + nav dot
    let cls = "pill pill-idle", txt = "Not configured", dotc = "dot";
    if (!st.available) txt = "Unavailable";
    else if (!st.configured) txt = "Not configured";
    else if (st.connected) { cls = "pill pill-ok"; txt = "Connected"; dotc = "dot on"; }
    else { cls = "pill pill-warn"; txt = "Log in to Telegram"; dotc = "dot warn"; }
    $("connPill").className = cls; $("connPillText").textContent = txt; $("navTgDot").className = dotc;

    // telegram section visibility
    connectCard.hidden = !(st.configured && !st.connected);
    tgConnected.hidden = !st.connected;

    // overview
    $("ovTg").textContent = st.connected ? (st.account || "connected") : "offline";
    $("ovGroup").textContent = st.group ? st.group.name : "none";
    const ap = st.autoplay || {};
    $("ovState").textContent = ap.state || "IDLE";
    const r = st.last_result;
    $("ovGuess").textContent = r && r.answer ? r.answer.toUpperCase() : "—";
    $("ovBoard").textContent = st.last_board ? st.last_board.text : "Waiting for a board…";
    $("ovBoardMode").textContent = st.last_board ? `${st.last_board.mode}-LETTER` : "—";
    $("ovNext").textContent = ap.next_guess || "—";
    logList($("ovActivity"), (ap.timeline || []).concat(st.activity || []).slice(0, 8), "No activity yet");

    if (!st.connected) return;
    $("kvAccount").textContent = st.account || "—";
    $("kvPhone").textContent = st.phone || "—";
    $("kvGroupName").textContent = st.group ? st.group.name : "none";
    $("kvGroupId").textContent = st.group ? String(st.group.id) : "—";
    if (document.activeElement !== $("autoSendToggle")) $("autoSendToggle").checked = !!st.auto_send;

    // board + result
    if (st.last_board) { $("latestBoard").textContent = st.last_board.text || "—"; $("boardMode").textContent = `${st.last_board.mode}-LETTER`; }
    else { $("latestBoard").textContent = "Waiting for a board…"; $("boardMode").textContent = "—"; }
    $("lastMsgTs").textContent = st.last_message_ts ? `${clock(st.last_message_ts)} (${ago(st.last_message_ts)})` : "—";
    if (r && r.answer) {
      currentBest = r.answer; $("bestGuess").textContent = r.answer.toUpperCase();
      $("sendBestBtn").disabled = !st.group; $("copyBestBtn").disabled = false;
      const conf = confidences(r.guesses.length || 1); const pct = Math.round((conf[0] || 1) * 100);
      $("confFill").style.width = pct + "%"; $("confPct").textContent = pct + "%";
      $("candCount").textContent = `${(r.count || 0).toLocaleString()} possible`;
      $("solveTime").textContent = r.solve_ms != null ? r.solve_ms + " ms" : "—";
      const box = $("topGuesses"); box.innerHTML = "";
      r.guesses.forEach((w, i) => { const b = document.createElement("button"); b.className = "chip"; b.type = "button"; b.disabled = !st.group; const rk = document.createElement("span"); rk.className = "rk"; rk.textContent = "#" + (i + 1); b.append(rk, document.createTextNode(w.toUpperCase())); b.addEventListener("click", () => sendGuess(w)); box.appendChild(b); });
    } else { currentBest = null; $("bestGuess").textContent = "—"; $("sendBestBtn").disabled = true; $("copyBestBtn").disabled = true; $("confFill").style.width = "0%"; $("confPct").textContent = "—"; $("candCount").textContent = ""; $("topGuesses").innerHTML = ""; }

    // autoplay
    if (document.activeElement !== $("apEnabled")) $("apEnabled").checked = !!ap.enabled;
    $("apStateBadge").textContent = ap.state || "IDLE"; $("apStateBadge").className = "state-badge " + (RUNCLS[ap.state] || "");
    $("apGame").textContent = ap.game_id || "—";
    $("apGuessNo").textContent = ap.guess_number != null ? `${ap.guess_number}/${ap.max_guesses}` : "—";
    $("apMode").textContent = ap.mode ? ap.mode + "L" : "—";
    $("apSince").textContent = ap.seconds_since_update != null ? ap.seconds_since_update + "s" : "—";
    $("nextGuess").textContent = ap.next_guess || "—";
    $("apStartBtn").textContent = ap.running ? "Stop" : "Start"; $("apStartBtn").dataset.run = ap.running ? "1" : "0";
    $("apPauseBtn").textContent = ap.paused ? "Resume" : "Pause"; $("apPauseBtn").dataset.paused = ap.paused ? "1" : "0";
    if (!cfgTouched && document.activeElement.tagName !== "INPUT") {
      const c = ap.config || {};
      cfg.max.value = c.max_guesses; cfg.delay.value = c.delay_ms; cfg.cooldown.value = c.cooldown_ms; cfg.conf.value = c.min_confidence; cfg.repeat.value = c.max_repeat;
      cfg.autoStart.checked = !!c.auto_start_on_new_game; cfg.autoStop.checked = !!c.auto_stop_after_game; cfg.sendCmd.checked = !!c.send_new_game_command; cfg.cmd.value = c.new_game_command || "/new";
    }
  }

  /* ---------------- custom messages ---------------- */
  const mset = { enabled: $("msgEnabled"), delay: $("msgDelay"), max: $("msgMax"), onNew: $("msgOnNew"), every: $("msgEvery"), wins: $("msgWins"), losses: $("msgLosses") };
  let MESSAGES = [];
  async function loadMessages() {
    const r = await api("/api/messages"); if (!r) return;
    MESSAGES = r.messages || []; renderMessages();
    const s = r.settings || {};
    mset.enabled.checked = !!s.enabled; mset.delay.value = s.delay_ms; mset.max.value = s.max_messages;
    mset.onNew.checked = !!s.on_new_game; mset.every.checked = !!s.after_every_game; mset.wins.checked = !!s.only_wins; mset.losses.checked = !!s.only_losses;
    $(s.mode === "sequential" ? "modeSeq" : s.mode === "weighted" ? "modeWeighted" : "modeRandom").checked = true;
  }
  function renderMessages() {
    const ul = $("msgList"); ul.innerHTML = "";
    if (!MESSAGES.length) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = "No messages yet. Add one above."; ul.appendChild(li); return; }
    MESSAGES.forEach((m) => {
      const li = document.createElement("li"); li.className = "msg-item"; li.draggable = true; li.dataset.id = m.id;
      const h = document.createElement("span"); h.className = "drag-handle"; h.textContent = "⠿";
      const inp = document.createElement("input"); inp.className = "msg-text"; inp.value = m.text; inp.maxLength = 300;
      inp.addEventListener("change", async () => { ack(await api("/api/messages/update", { csrf: CSRF, id: m.id, text: inp.value }), "Saved"); });
      const wl = document.createElement("span"); wl.className = "wlbl"; wl.textContent = "×";
      const wt = document.createElement("input"); wt.className = "msg-weight"; wt.type = "number"; wt.min = 1; wt.max = 20; wt.value = m.weight || 1; wt.title = "Weight (weighted mode)";
      wt.addEventListener("change", async () => { await api("/api/messages/update", { csrf: CSRF, id: m.id, weight: +wt.value }); });
      const dup = document.createElement("button"); dup.className = "icon-btn"; dup.textContent = "⧉"; dup.title = "Duplicate";
      dup.addEventListener("click", async () => { await api("/api/messages/duplicate", { csrf: CSRF, id: m.id }); loadMessages(); });
      const del = document.createElement("button"); del.className = "icon-btn"; del.textContent = "✕"; del.title = "Delete";
      del.addEventListener("click", async () => { await api("/api/messages/delete", { csrf: CSRF, id: m.id }); loadMessages(); });
      li.append(h, inp, wl, wt, dup, del);
      li.addEventListener("dragstart", () => li.classList.add("dragging"));
      li.addEventListener("dragend", () => { li.classList.remove("dragging"); persistOrder(); });
      li.addEventListener("dragover", (e) => { e.preventDefault(); const dragging = ul.querySelector(".dragging"); if (!dragging || dragging === li) return; const rect = li.getBoundingClientRect(); const after = e.clientY > rect.top + rect.height / 2; ul.insertBefore(dragging, after ? li.nextSibling : li); });
      ul.appendChild(li);
    });
  }
  async function persistOrder() {
    const ids = [...$("msgList").querySelectorAll(".msg-item")].map((li) => +li.dataset.id);
    if (ids.length) await api("/api/messages/reorder", { csrf: CSRF, ids });
  }
  $("addMsgBtn").addEventListener("click", async () => {
    const v = $("newMsgInput").value.trim(); if (!v) return;
    const r = await api("/api/messages", { csrf: CSRF, text: v }); if (r && r.ok) { $("newMsgInput").value = ""; loadMessages(); } else ack(r);
  });
  $("newMsgInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("addMsgBtn").click(); });
  $("msgSaveBtn").addEventListener("click", async () => {
    const mode = $("modeWeighted").checked ? "weighted" : $("modeSeq").checked ? "sequential" : "random";
    ack(await api("/api/messages/settings", { csrf: CSRF, enabled: mset.enabled.checked, mode, delay_ms: +mset.delay.value, max_messages: +mset.max.value, on_new_game: mset.onNew.checked, after_every_game: mset.every.checked, only_wins: mset.wins.checked, only_losses: mset.losses.checked }), "Settings saved");
  });
  $("previewBtn").addEventListener("click", () => {
    const out = $("previewOut");
    if (!MESSAGES.length) { out.hidden = false; out.textContent = "No messages to preview."; return; }
    const max = Math.max(1, Math.min(+mset.max.value || 1, MESSAGES.length));
    const seq = $("modeSeq").checked;
    const picks = seq ? MESSAGES.slice(0, max).map((m) => m.text) : (() => { const c = [...MESSAGES]; const r = []; for (let i = 0; i < max; i++) r.push(c.splice(Math.floor(Math.random() * c.length), 1)[0].text); return r; })();
    out.hidden = false; out.textContent = "Would send: " + picks.join("  →  ");
  });
  $("exportBtn").addEventListener("click", () => {
    const text = MESSAGES.map((m) => m.text).join("\n");
    const blob = new Blob([text], { type: "text/plain" }); const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "wordseek-messages.txt"; a.click(); URL.revokeObjectURL(a.href);
  });
  $("importBtn").addEventListener("click", () => $("importFile").click());
  $("importFile").addEventListener("change", async (e) => {
    const file = e.target.files[0]; if (!file) return;
    const text = await file.text();
    let texts;
    try { const j = JSON.parse(text); texts = Array.isArray(j) ? j : (j.messages || []); } catch { texts = text.split(/\r?\n/); }
    texts = texts.map((t) => String(t).trim()).filter(Boolean);
    ack(await api("/api/messages/import", { csrf: CSRF, texts }), `Imported ${texts.length}`); e.target.value = ""; loadMessages();
  });

  /* ---------------- automation rules ---------------- */
  let RULES = [], editingRuleId = null;
  const ACTION_TYPES = [["wait", "Wait"], ["send_text", "Send text"], ["send_message", "Send custom message"], ["start_autoplay", "Start autoplay"], ["stop_autoplay", "Stop autoplay"], ["new_game_command", "Send new-game command"]];
  const TRIG_LABEL = { new_game: "New game", won: "Won", lost: "Lost" };
  function actSummary(a) {
    if (a.type === "wait") return `wait ${a.ms}ms`;
    if (a.type === "send_text") return `send "${a.text}"`;
    if (a.type === "send_message") return `send ${a.mode} message`;
    if (a.type === "start_autoplay") return "start autoplay";
    if (a.type === "stop_autoplay") return "stop autoplay";
    if (a.type === "new_game_command") return "send /new";
    return a.type;
  }
  async function loadRules() { const r = await api("/api/rules"); if (!r) return; RULES = r.rules || []; renderRules(); }
  function renderRules() {
    const ul = $("ruleList"); ul.innerHTML = "";
    if (!RULES.length) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = "No rules yet. Create one to automate steps on game events."; ul.appendChild(li); return; }
    RULES.forEach((rule) => {
      const li = document.createElement("li"); li.className = "rule-item";
      const sw = document.createElement("label"); sw.className = "switch small";
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!rule.enabled;
      cb.addEventListener("change", async () => { await api("/api/rules/update", { csrf: CSRF, id: rule.id, enabled: cb.checked }); loadRules(); });
      const sl = document.createElement("span"); sl.className = "slider"; sw.append(cb, sl);
      const info = document.createElement("div"); info.className = "rule-info";
      const nm = document.createElement("div"); nm.className = "rule-name"; nm.textContent = rule.name;
      const meta = document.createElement("div"); meta.className = "rule-meta";
      const trig = document.createElement("span"); trig.className = "rule-trig"; trig.textContent = TRIG_LABEL[rule.trigger] || rule.trigger;
      meta.append(trig, document.createTextNode((rule.actions || []).map(actSummary).join(" → ") || "no steps"));
      info.append(nm, meta);
      const edit = document.createElement("button"); edit.className = "btn small-btn"; edit.textContent = "Edit"; edit.addEventListener("click", () => openRule(rule));
      const del = document.createElement("button"); del.className = "icon-btn"; del.textContent = "✕"; del.title = "Delete";
      del.addEventListener("click", async () => { await api("/api/rules/delete", { csrf: CSRF, id: rule.id }); loadRules(); });
      li.append(sw, info, edit, del); ul.appendChild(li);
    });
  }
  function addActionRow(a) {
    const li = document.createElement("li"); li.className = "action-row";
    const type = document.createElement("select"); type.className = "a-type";
    ACTION_TYPES.forEach(([v, l]) => { const o = document.createElement("option"); o.value = v; o.textContent = l; type.appendChild(o); });
    type.value = (a && a.type) || "wait";
    const param = document.createElement("span"); param.className = "a-param";
    const del = document.createElement("button"); del.className = "icon-btn"; del.textContent = "✕"; del.addEventListener("click", () => li.remove());
    function renderParam() {
      param.innerHTML = "";
      if (type.value === "wait") { const n = document.createElement("input"); n.type = "number"; n.min = 0; n.max = 20000; n.value = (a && a.ms) || 1000; n.className = "a-val"; n.style.width = "100%"; param.appendChild(n); }
      else if (type.value === "send_text") { const t = document.createElement("input"); t.type = "text"; t.maxLength = 300; t.placeholder = "message to send"; t.value = (a && a.text) || ""; t.className = "a-val"; t.style.width = "100%"; param.appendChild(t); }
      else if (type.value === "send_message") { const s = document.createElement("select"); s.className = "a-val"; s.style.width = "100%"; ["random", "sequential"].forEach((m) => { const o = document.createElement("option"); o.value = m; o.textContent = m; s.appendChild(o); }); s.value = (a && a.mode) || "random"; param.appendChild(s); }
      a = null; // only seed once
    }
    type.addEventListener("change", renderParam); renderParam();
    li.append(type, param, del); $("actionList").appendChild(li);
  }
  function openRule(rule) {
    editingRuleId = rule ? rule.id : null;
    $("ruleModalTitle").textContent = rule ? "Edit rule" : "New rule";
    $("ruleName").value = rule ? rule.name : "";
    $("ruleTrigger").value = rule ? rule.trigger : "new_game";
    $("actionList").innerHTML = "";
    (rule && rule.actions ? rule.actions : [{ type: "wait", ms: 1000 }]).forEach(addActionRow);
    $("ruleModal").hidden = false;
  }
  function closeRule() { $("ruleModal").hidden = true; }
  $("addRuleBtn").addEventListener("click", () => openRule(null));
  $("addActionBtn").addEventListener("click", () => addActionRow(null));
  $("ruleModal").addEventListener("click", (e) => { if (e.target.hasAttribute("data-close")) closeRule(); });
  $("saveRuleBtn").addEventListener("click", async () => {
    const actions = [...$("actionList").querySelectorAll(".action-row")].map((row) => {
      const t = row.querySelector(".a-type").value; const val = row.querySelector(".a-val");
      if (t === "wait") return { type: "wait", ms: +val.value };
      if (t === "send_text") return { type: "send_text", text: val.value };
      if (t === "send_message") return { type: "send_message", mode: val.value };
      return { type: t };
    });
    const body = { csrf: CSRF, name: $("ruleName").value || "Rule", trigger: $("ruleTrigger").value, actions, enabled: true };
    let r; if (editingRuleId) { body.id = editingRuleId; r = await api("/api/rules/update", body); } else r = await api("/api/rules", body);
    if (r && r.ok) { toast("Rule saved"); closeRule(); loadRules(); } else ack(r);
  });

  /* ---------------- users (admin) ---------------- */
  async function loadUsers() {
    if (!IS_ADMIN) return; const r = await api("/api/users"); if (!r || !r.users) return;
    const tb = $("userRows"); tb.innerHTML = "";
    r.users.forEach((u) => {
      const tr = document.createElement("tr");
      const c = (html) => { const td = document.createElement("td"); td.append(html); return td; };
      const name = document.createElement("b"); name.textContent = u.username;
      const role = document.createElement("span"); role.className = "badge " + (u.is_admin ? "admin" : "user"); role.textContent = u.is_admin ? "admin" : "user";
      const stat = document.createElement("span"); stat.className = "badge " + (u.disabled ? "off" : "on"); stat.textContent = u.disabled ? "disabled" : "active";
      const last = document.createElement("span"); last.textContent = dt(u.last_login);
      const sess = document.createElement("span"); sess.className = "badge " + (u.active ? "live" : "user"); sess.textContent = u.active ? "● online" : "offline";
      const actions = document.createElement("div"); actions.className = "row-actions";
      if (u.id !== r.me) {
        const dis = document.createElement("button"); dis.className = "btn small-btn"; dis.textContent = u.disabled ? "Enable" : "Disable";
        dis.addEventListener("click", async () => { await api("/api/users/disable", { csrf: CSRF, id: u.id, disabled: !u.disabled }); loadUsers(); });
        const del = document.createElement("button"); del.className = "icon-btn"; del.textContent = "✕"; del.title = "Delete";
        del.addEventListener("click", async () => { if (confirm(`Delete user ${u.username}?`)) { ack(await api("/api/users/delete", { csrf: CSRF, id: u.id }), "Deleted"); loadUsers(); } });
        actions.append(dis, del);
      } else { const meb = document.createElement("span"); meb.className = "muted small"; meb.textContent = "you"; actions.appendChild(meb); }
      tr.append(c(name), c(role), c(stat), c(last), c(sess), c(actions)); tb.appendChild(tr);
    });
  }
  $("addUserBtn").addEventListener("click", async () => {
    const r = await api("/api/users", { csrf: CSRF, username: $("uName").value, password: $("uPass").value, is_admin: $("uAdmin").checked });
    if (r && r.ok) { toast("User added"); $("uName").value = ""; $("uPass").value = ""; $("uAdmin").checked = false; loadUsers(); } else ack(r);
  });

  /* ---------------- settings ---------------- */
  $("saveUsernameBtn").addEventListener("click", async () => {
    const v = $("setUsername").value.trim(); if (!v) return toast("Enter a username", "error");
    const r = await api("/api/account/username", { csrf: CSRF, username: v });
    if (r && r.ok) { toast("Username updated — reloading"); setTimeout(() => location.reload(), 900); } else ack(r);
  });
  $("savePasswordBtn").addEventListener("click", async () => {
    const r = await api("/api/account/password", { csrf: CSRF, current: $("curPass").value, new: $("newPass").value });
    if (r && r.ok) { toast("Password updated"); $("curPass").value = ""; $("newPass").value = ""; } else ack(r);
  });

  /* ---------------- telegram connect method (phone / string session) ---------------- */
  function connMethod(m) { $("methodPhone").hidden = m !== "phone"; $("methodString").hidden = m !== "string"; }
  document.querySelectorAll('input[name="connMethod"]').forEach((r) => r.addEventListener("change", () => connMethod(r.value)));
  $("ssConnectBtn").addEventListener("click", async () => {
    const api_id = $("ssApiId").value.trim(), api_hash = $("ssApiHash").value.trim(), string_session = $("ssSession").value.trim();
    if (!api_id || !api_hash || !string_session) return loginNote("Fill API ID, API hash and string session.", "err");
    loginNote("Connecting…", "");
    const r = await api("/api/tg/string", { csrf: CSRF, api_id, api_hash, string_session });
    if (r && r.ok && r.next === "done") { loginNote("Connected!", "ok"); toast("Telegram connected"); $("ssSession").value = ""; poll(); }
    else loginNote((r && r.error) || "Could not connect.", "err");
  });

  /* ---------------- Groups page ---------------- */
  async function loadGroupsPage() {
    const st = await api("/api/status"); const cur = st && st.group ? st.group.id : null;
    const t = $("grpTarget"); t.hidden = !(st && st.group);
    if (st && st.group) { t.textContent = ""; const b = document.createElement("b"); b.textContent = st.group.name; t.append("Active target: ", b, ` (${st.group.id})`); }
    const r = await api("/api/groups"); const ul = $("grpList"); ul.innerHTML = "";
    if (!r || !r.ok) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = r && r.error ? r.error : "Connect Telegram, then load groups."; ul.appendChild(li); return; }
    if (!r.groups.length) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = "No groups found."; ul.appendChild(li); return; }
    r.groups.forEach((g) => {
      const li = document.createElement("li"); li.className = "grp-item" + (g.id === cur ? " active" : "");
      const av = document.createElement("span"); av.className = "grp-av"; av.textContent = (g.name || "?")[0].toUpperCase();
      const info = document.createElement("div"); info.className = "grp-info";
      const nm = document.createElement("div"); nm.className = "grp-nm"; nm.textContent = g.name;
      const id = document.createElement("div"); id.className = "grp-id"; id.textContent = g.id;
      info.append(nm, id);
      const btn = document.createElement("button"); btn.className = "btn small-btn" + (g.id === cur ? "" : " btn-primary"); btn.textContent = g.id === cur ? "Active" : "Set target"; btn.disabled = g.id === cur;
      btn.addEventListener("click", async () => { ack(await api("/api/select_group", { csrf: CSRF, id: g.id, name: g.name }), "Target set"); loadGroupsPage(); poll(); });
      li.append(av, info, btn); ul.appendChild(li);
    });
  }
  $("grpLoadBtn").addEventListener("click", loadGroupsPage);

  /* ---------------- History / Replay ---------------- */
  async function loadHistory() {
    const r = await api("/api/history"); const ul = $("histList"); ul.innerHTML = "";
    if (!r || !r.games || !r.games.length) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = "No completed games yet."; ul.appendChild(li); return; }
    r.games.forEach((g) => {
      const li = document.createElement("li"); li.className = "hist-game";
      const badge = document.createElement("span"); badge.className = "badge " + (g.result === "won" ? "on" : "off"); badge.textContent = g.result === "won" ? "WON" : "LOST";
      const word = document.createElement("b"); word.className = "hist-word"; word.textContent = (g.answer || "—").toUpperCase();
      const meta = document.createElement("span"); meta.className = "muted small"; meta.textContent = `${g.mode}L · ${g.guesses} guesses · ${g.seconds ? g.seconds.toFixed(1) + "s" : "—"} · ${dt(g.created_at)}`;
      const view = document.createElement("button"); view.className = "btn small-btn"; view.textContent = "Replay";
      view.addEventListener("click", () => openReplay(g.id));
      li.append(badge, word, meta, view); ul.appendChild(li);
    });
  }
  async function openReplay(gid) {
    const g = await api(`/api/history/${gid}`); if (!g || g.error) return toast("Not found", "error");
    const body = $("replayBody"); body.innerHTML = "";
    $("replayTitle").textContent = `${(g.answer || "").toUpperCase()} · ${g.result === "won" ? "Won" : "Lost"}`;
    const meta = document.createElement("p"); meta.className = "muted small"; meta.textContent = `${g.mode}-letter · ${g.guesses} guesses · ${g.seconds ? g.seconds.toFixed(1) + "s" : "—"} · ${dt(g.created_at)}`;
    const gl = document.createElement("div"); gl.className = "replay-guesses";
    (g.guess_list || []).forEach((w, i) => { const c = document.createElement("span"); c.className = "chip"; const rk = document.createElement("span"); rk.className = "rk"; rk.textContent = "#" + (i + 1); c.append(rk, document.createTextNode(w)); gl.appendChild(c); });
    const bl = document.createElement("label"); bl.className = "fld-label"; bl.textContent = "Final board";
    const pre = document.createElement("pre"); pre.className = "board-view"; pre.textContent = g.board || "—";
    body.append(meta, bl, pre, document.createElement("br")); const gll = document.createElement("label"); gll.className = "fld-label"; gll.textContent = "Our guesses"; body.append(gll, gl);
    $("replayModal").hidden = false;
  }
  $("histRefresh").addEventListener("click", loadHistory);
  $("replayModal").addEventListener("click", (e) => { if (e.target.hasAttribute("data-close")) $("replayModal").hidden = true; });

  /* ---------------- Statistics ---------------- */
  async function loadStats() {
    const s = await api("/api/stats"); if (!s) return;
    const tiles = [["Games", s.games], ["Wins", s.wins], ["Losses", s.losses], ["Win rate", s.win_rate + "%"],
      ["Current streak", s.current_streak], ["Longest streak", s.longest_streak],
      ["Avg guesses", s.avg_guesses], ["Avg solve", s.avg_solve_time + "s"], ["Fastest", s.fastest_solve + "s"]];
    const box = $("statTiles"); box.innerHTML = "";
    tiles.forEach(([label, val]) => { const d = document.createElement("div"); d.className = "stat-tile card"; const b = document.createElement("b"); b.textContent = val; const sm = document.createElement("small"); sm.textContent = label; d.append(b, sm); box.appendChild(d); });
    // SVG bar chart
    const series = s.series || []; const max = Math.max(1, ...series.map((d) => d.games));
    const W = 100, H = 40, n = series.length, bw = W / n;
    let bars = "";
    series.forEach((d, i) => {
      const gh = (d.games / max) * H, wh = (d.wins / max) * H, x = i * bw;
      bars += `<rect x="${(x + bw * 0.12).toFixed(2)}" y="${(H - gh).toFixed(2)}" width="${(bw * 0.76).toFixed(2)}" height="${gh.toFixed(2)}" rx="0.6" fill="rgba(167,139,250,.25)"></rect>`;
      bars += `<rect x="${(x + bw * 0.12).toFixed(2)}" y="${(H - wh).toFixed(2)}" width="${(bw * 0.76).toFixed(2)}" height="${wh.toFixed(2)}" rx="0.6" fill="url(#gg)"></rect>`;
    });
    $("statChart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="chart-svg"><defs><linearGradient id="gg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7cc7ff"/><stop offset="1" stop-color="#a78bfa"/></linearGradient></defs>${bars}</svg>`;
  }

  /* ---------------- Logs (searchable) ---------------- */
  async function loadLogs() {
    const cat = $("logCategory").value, q = $("logSearch").value.trim();
    const qs = new URLSearchParams(); if (cat) qs.set("category", cat); if (q) qs.set("q", q);
    const r = await api("/api/logs?" + qs.toString()); const ul = $("logList"); ul.innerHTML = "";
    if (!r || !r.logs || !r.logs.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = "No logs match."; ul.appendChild(li); return; }
    r.logs.forEach((e) => {
      const li = document.createElement("li"); li.className = "log-row";
      const t = document.createElement("span"); t.className = "t"; t.textContent = dt(e.ts);
      const cat = document.createElement("span"); cat.className = "logcat cat-" + e.category; cat.textContent = e.category;
      const m = document.createElement("span"); m.className = "m"; m.textContent = e.text;
      li.append(t, cat, m); ul.appendChild(li);
    });
  }
  $("logRefresh").addEventListener("click", loadLogs);
  $("logCategory").addEventListener("change", loadLogs);
  $("logSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLogs(); });

  /* ---------------- Themes + preferences ---------------- */
  const THEMES = ["cute", "dark", "glass", "purple", "blue", "cyber", "minimal"];
  function applyTheme(name) { document.documentElement.dataset.theme = THEMES.includes(name) ? name : "cute"; }
  function applyAnim(speed) { document.documentElement.dataset.anim = ["off", "slow", "normal", "fast"].includes(speed) ? speed : "normal"; }
  function beep(kind) {
    const ctx = beep._c || (beep._c = new (window.AudioContext || window.webkitAudioContext)());
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = kind === "error" ? 220 : kind === "info" ? 520 : 660;
    o.connect(g); g.connect(ctx.destination); g.gain.value = 0.04;
    o.start(); g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.18); o.stop(ctx.currentTime + 0.19);
  }
  function renderSwatches(active) {
    const box = $("themeSwatches"); box.innerHTML = "";
    THEMES.forEach((t) => {
      const b = document.createElement("button"); b.className = "swatch t-" + t + (t === active ? " active" : ""); b.type = "button"; b.title = t;
      const lbl = document.createElement("span"); lbl.textContent = t; b.appendChild(lbl);
      b.addEventListener("click", async () => { applyTheme(t); localStorage.setItem("ws.theme", t); renderSwatches(t); await api("/api/prefs", { csrf: CSRF, theme: t }); toast("Theme: " + t, "info"); });
      box.appendChild(b);
    });
  }
  async function loadPrefs() {
    const p = await api("/api/prefs"); if (!p) return;
    applyTheme(p.theme); applyAnim(p.anim_speed); SOUND = !!p.sound;
    localStorage.setItem("ws.theme", p.theme);
    renderSwatches(p.theme);
    $("animSpeed").value = p.anim_speed || "normal";
    $("soundToggle").checked = !!p.sound;
  }
  $("animSpeed").addEventListener("change", async () => { applyAnim($("animSpeed").value); ack(await api("/api/prefs", { csrf: CSRF, anim_speed: $("animSpeed").value }), "Saved"); });
  $("soundToggle").addEventListener("change", async () => { SOUND = $("soundToggle").checked; await api("/api/prefs", { csrf: CSRF, sound: SOUND }); if (SOUND) toast("Sound on", "info"); });

  /* ---------------- boot ---------------- */
  applyTheme(localStorage.getItem("ws.theme") || "cute");   // instant, before network
  showStep("phone"); connMethod("phone");
  loadPrefs();
  const start = (location.hash || "").replace("#", "");
  go(titles[start] ? start : "dashboard");
  poll();
  setInterval(poll, 2500);
})();
