/* ============================================================================
   WordSeek — autoplay dashboard (vanilla JS)
   - "Login with Telegram" (phone -> code -> optional 2FA)
   - Live autoplay state machine view + controls
   - Group select, manual send, timeline. Polls /api/status.
   All dynamic text uses textContent (never innerHTML for server data).
   ========================================================================== */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const CSRF = document.querySelector('meta[name="csrf"]').content;
  const toastStack = $("toastStack");

  async function api(path, body) {
    const opt = { method: body ? "POST" : "GET", headers: {} };
    if (body) {
      opt.headers["Content-Type"] = "application/json";
      opt.headers["X-CSRF-Token"] = CSRF;
      opt.body = JSON.stringify(body);
    }
    let res;
    try { res = await fetch(path, opt); }
    catch { return { ok: false, error: "Network error" }; }
    if (res.status === 401) { window.location.href = "/login"; return null; }
    let data = {};
    try { data = await res.json(); } catch { /* ignore */ }
    if (res.status === 429) data.error = data.error || "Rate limited";
    return data;
  }

  function toast(message, kind = "success") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const i = document.createElement("span"); i.className = "toast-icon";
    i.textContent = kind === "error" ? "!" : kind === "info" ? "i" : "✓";
    const s = document.createElement("span"); s.textContent = message;
    el.append(i, s); toastStack.appendChild(el);
    setTimeout(() => { el.classList.add("out"); el.addEventListener("animationend", () => el.remove(), { once: true }); }, 1800);
    while (toastStack.children.length > 3) toastStack.firstChild.remove();
  }
  function ack(r, okMsg) {
    if (!r) return;
    if (r.ok === false || r.error) toast(r.error || "Action failed", "error");
    else if (okMsg) toast(okMsg, "success");
  }

  const clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
  function ago(ts) {
    if (!ts) return "—";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  }
  function confidences(n) {
    const w = Array.from({ length: n }, (_, i) => 1 / Math.pow(i + 1, 0.62));
    const max = w[0] || 1; return w.map((x) => x / max);
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); }
    catch {
      const t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
      document.body.appendChild(t); t.select(); try { document.execCommand("copy"); } catch {} t.remove();
    }
  }

  /* ---------------- login-with-telegram ---------------- */
  const connectCard = $("connectCard"), mainGrid = $("mainGrid");
  const stepPhone = $("stepPhone"), stepCode = $("stepCode"), stepPassword = $("stepPassword");
  const loginMsg = $("loginMsg");
  function showStep(name) {
    stepPhone.hidden = name !== "phone";
    stepCode.hidden = name !== "code";
    stepPassword.hidden = name !== "password";
  }
  function loginNote(text, kind) {
    loginMsg.hidden = !text; loginMsg.textContent = text || "";
    loginMsg.className = "login-msg " + (kind || "");
  }

  $("sendCodeBtn").addEventListener("click", async () => {
    const phone = $("phoneInput").value.trim();
    if (!phone) { loginNote("Enter your phone number.", "err"); return; }
    loginNote("Sending code…", "");
    const r = await api("/api/tg/send_code", { csrf: CSRF, phone });
    if (r && r.ok && r.next === "code") { showStep("code"); loginNote("Code sent. Check your Telegram app.", "ok"); $("codeInput").focus(); }
    else loginNote((r && r.error) || "Could not send code.", "err");
  });
  $("signInBtn").addEventListener("click", async () => {
    const code = $("codeInput").value.trim();
    if (!code) { loginNote("Enter the code.", "err"); return; }
    loginNote("Verifying…", "");
    const r = await api("/api/tg/sign_in", { csrf: CSRF, code });
    if (r && r.ok && r.next === "password") { showStep("password"); loginNote("Enter your two-step (2FA) password.", ""); $("passwordInput").focus(); }
    else if (r && r.ok && r.next === "done") { loginNote("Connected! Loading…", "ok"); toast("Telegram connected", "success"); poll(); }
    else loginNote((r && r.error) || "Sign-in failed.", "err");
  });
  $("passwordBtn").addEventListener("click", async () => {
    const password = $("passwordInput").value;
    if (!password) { loginNote("Enter your 2FA password.", "err"); return; }
    loginNote("Checking…", "");
    const r = await api("/api/tg/password", { csrf: CSRF, password });
    if (r && r.ok && r.next === "done") { loginNote("Connected! Loading…", "ok"); toast("Telegram connected", "success"); poll(); }
    else loginNote((r && r.error) || "Incorrect password.", "err");
  });
  $("restartBtn").addEventListener("click", () => { showStep("phone"); loginNote("", ""); });

  /* ---------------- refs ---------------- */
  const connPill = $("connPill"), connPillText = $("connPillText");
  const kvAccount = $("kvAccount"), kvPhone = $("kvPhone");
  const kvGroupName = $("kvGroupName"), kvGroupId = $("kvGroupId"), groupSelect = $("groupSelect");
  const apEnabled = $("apEnabled"), apStateBadge = $("apStateBadge");
  const apGame = $("apGame"), apGuessNo = $("apGuessNo"), apMode = $("apMode"), apSince = $("apSince");
  const apStartBtn = $("apStartBtn"), apPauseBtn = $("apPauseBtn");
  const latestBoard = $("latestBoard"), boardMode = $("boardMode"), lastMsgTs = $("lastMsgTs"), solveTime = $("solveTime");
  const bestGuess = $("bestGuess"), sendBestBtn = $("sendBestBtn"), copyBestBtn = $("copyBestBtn");
  const confFill = $("confFill"), confPct = $("confPct"), candCount = $("candCount"), topGuesses = $("topGuesses"), nextGuess = $("nextGuess");
  const autoSendToggle = $("autoSendToggle"), activityLog = $("activityLog"), errorLog = $("errorLog");
  const cfg = {
    max: $("cfgMax"), delay: $("cfgDelay"), cooldown: $("cfgCooldown"), conf: $("cfgConf"),
    repeat: $("cfgRepeat"), autoStart: $("cfgAutoStart"), autoStop: $("cfgAutoStop"),
    sendCmd: $("cfgSendCmd"), cmd: $("cfgCmd"),
  };
  let currentBest = null, editing = false, cfgTouched = false;

  const RUN_STATES = { WAITING_FOR_BOARD: "wait", BOARD_DETECTED: "wait", SOLVING: "run", WAITING_TO_SEND: "run", GUESS_SENT: "run", WAITING_FOR_UPDATE: "wait", WON: "won", LOST: "lost", ERROR: "err", PAUSED: "paused", IDLE: "" };

  /* ---------------- render ---------------- */
  function render(st) {
    if (!st) return;

    // top-level: not configured / connect / main
    if (!st.configured) { connectCard.hidden = true; mainGrid.hidden = true; }
    else if (!st.connected) { connectCard.hidden = false; mainGrid.hidden = true; }
    else { connectCard.hidden = true; mainGrid.hidden = false; }

    // pill
    let cls = "pill pill-idle", txt = "Not configured";
    if (!st.available) txt = "Unavailable";
    else if (!st.configured) txt = "Not configured";
    else if (st.connected) { cls = "pill pill-ok"; txt = "Connected"; }
    else { cls = "pill pill-warn"; txt = "Log in to Telegram"; }
    connPill.className = cls; connPillText.textContent = txt;

    if (!st.connected) return;

    kvAccount.textContent = st.account || "—";
    kvPhone.textContent = st.phone || "—";
    kvGroupName.textContent = st.group ? st.group.name : "none";
    kvGroupId.textContent = st.group ? String(st.group.id) : "—";
    if (document.activeElement !== autoSendToggle) autoSendToggle.checked = !!st.auto_send;

    // board
    if (st.last_board) { latestBoard.textContent = st.last_board.text || "—"; boardMode.textContent = `${st.last_board.mode}-LETTER`; }
    else { latestBoard.textContent = "Waiting for a board…"; boardMode.textContent = "—"; }
    lastMsgTs.textContent = st.last_message_ts ? `${clock(st.last_message_ts)} (${ago(st.last_message_ts)})` : "—";

    // result + manual
    const r = st.last_result;
    if (r && r.answer) {
      currentBest = r.answer;
      bestGuess.textContent = r.answer.toUpperCase();
      sendBestBtn.disabled = !st.group; copyBestBtn.disabled = false;
      const conf = confidences(r.guesses.length || 1);
      const pct = Math.round((conf[0] || 1) * 100);
      confFill.style.width = pct + "%"; confPct.textContent = pct + "%";
      candCount.textContent = `${(r.count || 0).toLocaleString()} possible`;
      solveTime.textContent = r.solve_ms != null ? r.solve_ms + " ms" : "—";
      topGuesses.innerHTML = "";
      r.guesses.forEach((w, i) => {
        const b = document.createElement("button");
        b.className = "chip"; b.type = "button"; b.disabled = !st.group;
        const rk = document.createElement("span"); rk.className = "rk"; rk.textContent = "#" + (i + 1);
        b.append(rk, document.createTextNode(w.toUpperCase()));
        b.addEventListener("click", () => sendGuess(w));
        topGuesses.appendChild(b);
      });
    } else {
      currentBest = null; bestGuess.textContent = "—"; sendBestBtn.disabled = true; copyBestBtn.disabled = true;
      confFill.style.width = "0%"; confPct.textContent = "—"; candCount.textContent = ""; topGuesses.innerHTML = "";
    }

    // autoplay
    const ap = st.autoplay || {};
    if (document.activeElement !== apEnabled) apEnabled.checked = !!ap.enabled;
    apStateBadge.textContent = ap.state || "IDLE";
    apStateBadge.className = "state-badge " + (RUN_STATES[ap.state] || "");
    apGame.textContent = ap.game_id || "—";
    apGuessNo.textContent = ap.guess_number != null ? `${ap.guess_number}/${ap.max_guesses}` : "—";
    apMode.textContent = ap.mode ? ap.mode + "L" : "—";
    apSince.textContent = ap.seconds_since_update != null ? ap.seconds_since_update + "s" : "—";
    nextGuess.textContent = ap.next_guess || "—";
    apStartBtn.textContent = ap.running ? "Stop" : "Start";
    apStartBtn.dataset.run = ap.running ? "1" : "0";
    apPauseBtn.textContent = ap.paused ? "Resume" : "Pause";
    apPauseBtn.dataset.paused = ap.paused ? "1" : "0";

    // config inputs (don't clobber while editing)
    if (!cfgTouched && document.activeElement.tagName !== "INPUT") {
      const c = ap.config || {};
      cfg.max.value = c.max_guesses; cfg.delay.value = c.delay_ms; cfg.cooldown.value = c.cooldown_ms;
      cfg.conf.value = c.min_confidence; cfg.repeat.value = c.max_repeat;
      cfg.autoStart.checked = !!c.auto_start_on_new_game; cfg.autoStop.checked = !!c.auto_stop_after_game;
      cfg.sendCmd.checked = !!c.send_new_game_command; cfg.cmd.value = c.new_game_command || "/new";
    }

    // timeline = autoplay timeline + account activity, newest first
    const items = (ap.timeline || []).concat(st.activity || []).slice(0, 40);
    renderLog(activityLog, items, "No activity yet");
    renderLog(errorLog, st.errors || [], "No errors 🎉");
  }

  function renderLog(el, items, emptyMsg) {
    el.innerHTML = "";
    if (!items || !items.length) {
      const li = document.createElement("li"); li.className = "empty"; li.textContent = emptyMsg; el.appendChild(li); return;
    }
    items.slice(0, 30).forEach((e) => {
      const li = document.createElement("li");
      const t = document.createElement("span"); t.className = "t"; t.textContent = clock(e.ts);
      const m = document.createElement("span"); m.className = "m"; m.textContent = e.text;
      li.append(t, m); el.appendChild(li);
    });
  }

  /* ---------------- actions ---------------- */
  async function poll() { const st = await api("/api/status"); if (st) render(st); }
  async function sendGuess(word) { const r = await api("/api/send", { csrf: CSRF, word }); ack(r, r && r.ok ? `Sent ${word.toUpperCase()}` : null); poll(); }

  $("logoutTgBtn").addEventListener("click", async () => { ack(await api("/api/tg/logout", { csrf: CSRF }), "Logged out"); showStep("phone"); loginNote("", ""); poll(); });

  $("loadGroupsBtn").addEventListener("click", async () => {
    groupSelect.innerHTML = '<option value="">Loading…</option>';
    const r = await api("/api/groups");
    groupSelect.innerHTML = "";
    if (!r || !r.ok) { groupSelect.innerHTML = '<option value="">Connect first…</option>'; ack(r || { error: "Could not load groups" }); return; }
    if (!r.groups.length) { groupSelect.innerHTML = '<option value="">No groups found</option>'; return; }
    const ph = document.createElement("option"); ph.value = ""; ph.textContent = "Select a group…"; groupSelect.appendChild(ph);
    r.groups.forEach((g) => { const o = document.createElement("option"); o.value = g.id; o.textContent = `${g.name}  (${g.id})`; o.dataset.name = g.name; groupSelect.appendChild(o); });
    toast(`Loaded ${r.groups.length} groups`, "info");
  });
  $("setGroupBtn").addEventListener("click", async () => {
    const opt = groupSelect.selectedOptions[0];
    if (!opt || !opt.value) { toast("Pick a group first", "error"); return; }
    ack(await api("/api/select_group", { csrf: CSRF, id: opt.value, name: opt.dataset.name }), "Target group set"); poll();
  });

  apEnabled.addEventListener("change", async () => { ack(await api("/api/autoplay/enabled", { csrf: CSRF, enabled: apEnabled.checked }), apEnabled.checked ? "Autoplay ON" : "Autoplay OFF"); poll(); });
  apStartBtn.addEventListener("click", async () => {
    const running = apStartBtn.dataset.run === "1";
    ack(await api(running ? "/api/autoplay/stop" : "/api/autoplay/start", { csrf: CSRF }), running ? "Stopped" : "Started"); poll();
  });
  apPauseBtn.addEventListener("click", async () => {
    const paused = apPauseBtn.dataset.paused === "1";
    ack(await api(paused ? "/api/autoplay/resume" : "/api/autoplay/pause", { csrf: CSRF }), paused ? "Resumed" : "Paused"); poll();
  });
  $("apStopBtn").addEventListener("click", async () => { ack(await api("/api/autoplay/emergency", { csrf: CSRF }), "Emergency stop"); poll(); });

  Object.values(cfg).forEach((el) => {
    el.addEventListener("focus", () => { cfgTouched = true; });
    el.addEventListener("blur", () => { setTimeout(() => { cfgTouched = false; }, 400); });
  });
  $("cfgSaveBtn").addEventListener("click", async () => {
    const patch = {
      csrf: CSRF,
      max_guesses: +cfg.max.value, delay_ms: +cfg.delay.value, cooldown_ms: +cfg.cooldown.value,
      min_confidence: +cfg.conf.value, max_repeat: +cfg.repeat.value,
      auto_start_on_new_game: cfg.autoStart.checked, auto_stop_after_game: cfg.autoStop.checked,
      send_new_game_command: cfg.sendCmd.checked, new_game_command: cfg.cmd.value,
    };
    cfgTouched = false;
    ack(await api("/api/autoplay_config", patch), "Settings saved"); poll();
  });

  autoSendToggle.addEventListener("change", async () => { ack(await api("/api/auto_send", { csrf: CSRF, enabled: autoSendToggle.checked }), autoSendToggle.checked ? "Auto-send ON" : "Auto-send OFF"); poll(); });
  sendBestBtn.addEventListener("click", () => { if (currentBest) sendGuess(currentBest); });
  copyBestBtn.addEventListener("click", () => { if (currentBest) { copy(currentBest.toUpperCase()); toast("Copied", "info"); } });

  /* ---------------- boot ---------------- */
  showStep("phone");
  poll();
  setInterval(poll, 2500);
})();
