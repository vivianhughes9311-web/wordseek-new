/* ============================================================================
   WordSeek — Telegram dashboard (vanilla JS)
   Polls /api/status and drives the server-side monitor via /api/* endpoints.
   All dynamic text is written with textContent, so nothing is ever injected
   as HTML. Every POST carries the per-session CSRF token.
   ========================================================================== */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const CSRF = document.querySelector('meta[name="csrf"]').content;
  const toastStack = $("toastStack");

  /* ---- API helper ---- */
  async function api(path, body) {
    const opt = { method: body ? "POST" : "GET", headers: {} };
    if (body) {
      opt.headers["Content-Type"] = "application/json";
      opt.headers["X-CSRF-Token"] = CSRF;
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opt);
    } catch {
      return { ok: false, error: "Network error" };
    }
    if (res.status === 401) { window.location.href = "/login"; return null; }
    let data = {};
    try { data = await res.json(); } catch { /* ignore */ }
    if (res.status === 429) data.error = data.error || "Rate limited";
    return data;
  }

  /* ---- toast ---- */
  function toast(message, kind = "success") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const icon = kind === "error" ? "!" : kind === "info" ? "i" : "✓";
    const i = document.createElement("span"); i.className = "toast-icon"; i.textContent = icon;
    const s = document.createElement("span"); s.textContent = message;
    el.appendChild(i); el.appendChild(s);
    toastStack.appendChild(el);
    setTimeout(() => { el.classList.add("out"); el.addEventListener("animationend", () => el.remove(), { once: true }); }, 1800);
    while (toastStack.children.length > 3) toastStack.firstChild.remove();
  }

  function ack(result, okMsg) {
    if (!result) return;
    if (result.ok === false || result.error) toast(result.error || "Action failed", "error");
    else if (okMsg) toast(okMsg, "success");
  }

  /* ---- time formatting ---- */
  function clock(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function ago(ts) {
    if (!ts) return "—";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  }

  /* ---- confidence (visual only, from rank) ---- */
  function confidences(n) {
    const w = Array.from({ length: n }, (_, i) => 1 / Math.pow(i + 1, 0.62));
    const max = w[0] || 1;
    return w.map((x) => x / max);
  }

  /* ---- copy ---- */
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); }
    catch {
      const t = document.createElement("textarea");
      t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
      document.body.appendChild(t); t.select();
      try { document.execCommand("copy"); } catch { /* noop */ }
      t.remove();
    }
  }

  /* ---- element refs ---- */
  const connPill = $("connPill"), connPillText = $("connPillText");
  const modeTag = $("modeTag"), kvStatus = $("kvStatus"), kvAccount = $("kvAccount"), kvReconnect = $("kvReconnect");
  const kvGroupName = $("kvGroupName"), kvGroupId = $("kvGroupId");
  const groupSelect = $("groupSelect");
  const autoSendToggle = $("autoSendToggle"), delayInput = $("delayInput");
  const kvCooldown = $("kvCooldown"), kvLastSent = $("kvLastSent");
  const latestBoard = $("latestBoard"), boardMode = $("boardMode"), lastMsgTs = $("lastMsgTs");
  const bestGuess = $("bestGuess"), sendBestBtn = $("sendBestBtn"), copyBestBtn = $("copyBestBtn");
  const confFill = $("confFill"), confPct = $("confPct"), candCount = $("candCount"), topGuesses = $("topGuesses");
  const solveTime = $("solveTime"), pauseBtn = $("pauseBtn");
  const mMonitor = $("mMonitor"), mSolve = $("mSolve"), mCooldown = $("mCooldown"), mReconnect = $("mReconnect");
  const activityLog = $("activityLog"), errorLog = $("errorLog");

  let currentBest = null;
  let editingDelay = false;

  /* ---- render ---- */
  function render(st) {
    if (!st) return;

    // connection pill
    let pillClass = "pill pill-idle", pillText = "Not configured";
    if (!st.available) { pillText = "Unavailable"; }
    else if (!st.configured) { pillText = "Not configured"; }
    else if (st.connecting) { pillClass = "pill pill-warn"; pillText = "Connecting…"; }
    else if (st.reconnecting) { pillClass = "pill pill-warn"; pillText = "Reconnecting…"; }
    else if (st.connected && st.authorized) { pillClass = "pill pill-ok"; pillText = st.paused ? "Connected · paused" : "Connected"; }
    else { pillClass = "pill pill-err"; pillText = "Disconnected"; }
    connPill.className = pillClass; connPillText.textContent = pillText;

    modeTag.textContent = st.mode ? st.mode.toUpperCase() : "—";
    kvStatus.textContent = pillText;
    kvAccount.textContent = st.account || "—";
    kvReconnect.textContent = st.reconnecting ? "reconnecting…" : (st.connected ? "healthy" : "idle");

    // group
    kvGroupName.textContent = st.group ? st.group.name : "none";
    kvGroupId.textContent = st.group ? String(st.group.id) : "—";

    // auto-send + delay
    if (document.activeElement !== autoSendToggle) autoSendToggle.checked = !!st.auto_send;
    if (!editingDelay && document.activeElement !== delayInput) delayInput.value = st.delay_ms;

    // cooldown
    const cd = st.cooldown_remaining_ms || 0;
    kvCooldown.textContent = cd > 0 ? `${(cd / 1000).toFixed(1)}s` : "ready";
    mCooldown.textContent = cd > 0 ? `${(cd / 1000).toFixed(1)}s` : "ready";
    kvLastSent.textContent = st.last_sent ? `${st.last_sent.word} · ${ago(st.last_sent.ts)}` : "none";

    // pause button label
    pauseBtn.textContent = st.paused ? "Resume" : "Pause";
    pauseBtn.dataset.action = st.paused ? "resume" : "pause";

    // board
    if (st.last_board) {
      latestBoard.textContent = st.last_board.text || "—";
      boardMode.textContent = `${st.last_board.mode}-LETTER`;
    } else {
      latestBoard.textContent = "Waiting for a board…";
      boardMode.textContent = "—";
    }
    lastMsgTs.textContent = st.last_message_ts ? `${clock(st.last_message_ts)} (${ago(st.last_message_ts)})` : "—";

    // result
    const r = st.last_result;
    if (r && r.answer) {
      currentBest = r.answer;
      bestGuess.textContent = r.answer.toUpperCase();
      sendBestBtn.disabled = !(st.connected && st.authorized && st.group);
      copyBestBtn.disabled = false;
      const conf = confidences(r.guesses.length || 1);
      const pct = Math.round((conf[0] || 1) * 100);
      confFill.style.width = pct + "%";
      confPct.textContent = pct + "%";
      candCount.textContent = `${(r.count || 0).toLocaleString()} possible`;
      solveTime.textContent = (r.solve_ms != null ? r.solve_ms + " ms" : "—");
      mSolve.textContent = (r.solve_ms != null ? r.solve_ms + " ms" : "—");

      // top guesses chips
      topGuesses.innerHTML = "";
      r.guesses.forEach((w, i) => {
        const b = document.createElement("button");
        b.className = "chip"; b.type = "button";
        b.disabled = sendBestBtn.disabled;
        const rk = document.createElement("span"); rk.className = "rk"; rk.textContent = "#" + (i + 1);
        b.appendChild(rk);
        b.appendChild(document.createTextNode(w.toUpperCase()));
        b.addEventListener("click", () => sendGuess(w));
        topGuesses.appendChild(b);
      });
    } else {
      currentBest = null;
      bestGuess.textContent = r ? "no match" : "—";
      sendBestBtn.disabled = true; copyBestBtn.disabled = true;
      confFill.style.width = "0%"; confPct.textContent = "—"; candCount.textContent = "";
      solveTime.textContent = "—"; mSolve.textContent = "—";
      topGuesses.innerHTML = "";
    }

    // metrics
    mMonitor.textContent = !st.connected ? "offline" : (st.paused ? "paused" : (st.group ? "active" : "no group"));
    mReconnect.textContent = st.reconnecting ? "in progress" : (st.connected ? "healthy" : "idle");

    // logs
    renderLog(activityLog, st.activity, (e) => e.text, "No activity yet");
    renderLog(errorLog, st.errors, (e) => e.text, "No errors 🎉");
  }

  function renderLog(el, items, textOf, emptyMsg) {
    el.innerHTML = "";
    if (!items || !items.length) {
      const li = document.createElement("li"); li.className = "empty"; li.textContent = emptyMsg;
      el.appendChild(li); return;
    }
    items.forEach((e) => {
      const li = document.createElement("li");
      const t = document.createElement("span"); t.className = "t"; t.textContent = clock(e.ts);
      const m = document.createElement("span"); m.className = "m"; m.textContent = textOf(e);
      li.appendChild(t); li.appendChild(m); el.appendChild(li);
    });
  }

  /* ---- actions ---- */
  async function poll() {
    const st = await api("/api/status");
    if (st) render(st);
  }

  async function doAction(action) {
    const map = {
      connect: ["/api/connect", "Connecting…"],
      disconnect: ["/api/disconnect", "Disconnected"],
      pause: ["/api/pause", "Monitoring paused"],
      resume: ["/api/resume", "Monitoring resumed"],
    };
    const [path, msg] = map[action] || [];
    if (!path) return;
    ack(await api(path, { csrf: CSRF }), msg);
    poll();
  }

  async function sendGuess(word) {
    const res = await api("/api/send", { csrf: CSRF, word });
    ack(res, res && res.ok ? `Sent ${word.toUpperCase()}` : null);
    poll();
  }

  /* ---- wire up ---- */
  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => doAction(btn.dataset.action));
  });
  pauseBtn.addEventListener("click", () => doAction(pauseBtn.dataset.action));

  $("loadGroupsBtn").addEventListener("click", async () => {
    groupSelect.innerHTML = '<option value="">Loading…</option>';
    const res = await api("/api/groups");
    groupSelect.innerHTML = "";
    if (!res || !res.ok) {
      groupSelect.innerHTML = '<option value="">Connect first…</option>';
      ack(res || { error: "Could not load groups" });
      return;
    }
    if (!res.groups.length) {
      groupSelect.innerHTML = '<option value="">No groups found</option>';
      return;
    }
    const ph = document.createElement("option"); ph.value = ""; ph.textContent = "Select a group…";
    groupSelect.appendChild(ph);
    res.groups.forEach((g) => {
      const o = document.createElement("option");
      o.value = g.id; o.textContent = `${g.name}  (${g.id})`; o.dataset.name = g.name;
      groupSelect.appendChild(o);
    });
    toast(`Loaded ${res.groups.length} groups`, "info");
  });

  $("setGroupBtn").addEventListener("click", async () => {
    const opt = groupSelect.selectedOptions[0];
    if (!opt || !opt.value) { toast("Pick a group first", "error"); return; }
    ack(await api("/api/select_group", { csrf: CSRF, id: opt.value, name: opt.dataset.name }), "Target group updated");
    poll();
  });

  autoSendToggle.addEventListener("change", async () => {
    ack(await api("/api/auto_send", { csrf: CSRF, enabled: autoSendToggle.checked }),
        autoSendToggle.checked ? "Auto-send ON" : "Auto-send OFF");
    poll();
  });

  delayInput.addEventListener("focus", () => { editingDelay = true; });
  delayInput.addEventListener("blur", async () => {
    editingDelay = false;
    ack(await api("/api/delay", { csrf: CSRF, delay_ms: delayInput.value }), "Delay saved");
    poll();
  });

  sendBestBtn.addEventListener("click", () => { if (currentBest) sendGuess(currentBest); });
  copyBestBtn.addEventListener("click", () => { if (currentBest) { copy(currentBest.toUpperCase()); toast("Copied", "info"); } });

  /* ---- boot ---- */
  poll();
  setInterval(poll, 2500);
})();
