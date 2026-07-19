/* ============================================================================
   WordSeek — frontend logic (vanilla JS, no libraries)
   Kept deliberately lean for performance:
     - No always-on animation loops (no particle canvas, no mouse parallax).
     - Panda reactions are pure CSS, toggled by a single data-state attribute.
     - Confetti is the only rAF usage, and only on a unique solve (short burst).
   Backend contract is unchanged:
     POST /solve  {mode, board}  ->  {text, answer, guesses[], count, mode}
   ========================================================================== */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  /* ---- element references ---- */
  const board       = $("board");
  const inputWrap   = $("inputWrap");
  const statusText  = $("status");
  const statusDot   = $("statusDot");
  const modeBadge   = $("modeBadge");
  const modeLabel   = modeBadge.querySelector(".mode-label");
  const clearBtn    = $("clearBtn");

  const skeleton    = $("skeleton");
  const bestWrap    = $("bestWrap");
  const bestWord    = $("bestWord");
  const bestBar     = $("bestBar");
  const bestPct     = $("bestPct");
  const bestCard    = bestWrap.querySelector(".best-card");
  const bestFav     = bestWrap.querySelector(".best-fav");

  const guessesBox  = $("guesses");
  const emptyState  = $("emptyState");
  const toastStack  = $("toastStack");

  const heroPanda   = $("heroPanda");
  const pandaBubble = $("pandaBubble");

  const tabHistory   = $("tabHistory");
  const tabFavorites = $("tabFavorites");
  const historyView  = $("historyView");
  const favoritesView= $("favoritesView");
  const historyList  = $("historyList");
  const historyEmpty = $("historyEmpty");
  const favoritesList= $("favoritesList");
  const favoritesEmpty = $("favoritesEmpty");
  const clearHistory = $("clearHistory");
  const tabIndicator = document.querySelector(".tab-indicator");

  const shortcutsBtn = $("shortcutsBtn");
  const shortcutsModal = $("shortcutsModal");

  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- persistence ---- */
  const STORE = { history: "ws.history", favorites: "ws.favorites" };
  const MAX_HISTORY = 12;
  const load = (k, f) => { try { return JSON.parse(localStorage.getItem(k)) ?? f; } catch { return f; } };
  const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* quota */ } };
  let history   = load(STORE.history, []);
  let favorites = load(STORE.favorites, []);

  /* ---------------------------------------------------------------- */
  /* Pip the panda — expression + speech bubble                       */
  /* ---------------------------------------------------------------- */
  let bubbleTimer;
  function setPanda(state, message) {
    heroPanda.dataset.state = state;
    if (message) {
      pandaBubble.textContent = message;
      // replay the bubble bounce
      pandaBubble.style.animation = "none";
      void pandaBubble.offsetWidth;
      pandaBubble.style.animation = "";
    }
    // A happy pop should settle back to idle so it can retrigger later
    if (state === "happy") {
      clearTimeout(bubbleTimer);
      bubbleTimer = setTimeout(() => { if (heroPanda.dataset.state === "happy") heroPanda.dataset.state = "idle"; }, 1600);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Toasts                                                           */
  /* ---------------------------------------------------------------- */
  function toast(message, kind = "success") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const icon = kind === "error" ? "!" : kind === "info" ? "i" : "✓";
    el.innerHTML = `<span class="toast-icon">${icon}</span><span>${message}</span>`;
    toastStack.appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      el.addEventListener("animationend", () => el.remove(), { once: true });
    }, 1600);
    while (toastStack.children.length > 3) toastStack.firstChild.remove();
  }

  /* ---------------------------------------------------------------- */
  /* Clipboard                                                        */
  /* ---------------------------------------------------------------- */
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
  function copyWord(word, sourceEl) {
    copy(word);
    toast(`<b>${word.toUpperCase()}</b> copied`);
    if (sourceEl) { sourceEl.classList.add("copied"); setTimeout(() => sourceEl.classList.remove("copied"), 520); }
  }

  /* ---------------------------------------------------------------- */
  /* Mode detection                                                   */
  /* ---------------------------------------------------------------- */
  const EMOJI = /[🟥🟨🟩]/u;
  const EMOJI_G = /[🟥🟨🟩]/gu;
  function detectMode(text) {
    if (/4[\s-]?letter/i.test(text)) return 4;
    if (/5[\s-]?letter/i.test(text)) return 5;
    const line = text.split("\n").find((row) => EMOJI.test(row));
    if (!line) return null;
    const count = (line.match(EMOJI_G) || []).length;
    return count === 4 ? 4 : count === 5 ? 5 : null;
  }
  function setMode(mode) {
    modeBadge.classList.remove("mode-4", "mode-5");
    modeLabel.textContent = (mode === 4 || mode === 5) ? `${mode} letter` : "auto";
    if (mode === 4 || mode === 5) modeBadge.classList.add(`mode-${mode}`);
    modeBadge.classList.remove("pop"); void modeBadge.offsetWidth; modeBadge.classList.add("pop");
  }

  /* ---------------------------------------------------------------- */
  /* Status + skeleton                                                */
  /* ---------------------------------------------------------------- */
  function setStatus(message, state = "idle") {
    statusText.textContent = message;
    statusDot.className = "status-dot" + (state !== "idle" ? ` ${state}` : "");
  }
  function showSkeleton(show) {
    skeleton.classList.toggle("show", show);
    if (show) { bestWrap.hidden = true; guessesBox.innerHTML = ""; emptyState.classList.add("hidden"); }
  }

  /* ---------------------------------------------------------------- */
  /* Confidence weighting (visual only; backend returns ranked order) */
  /* ---------------------------------------------------------------- */
  function confidences(n) {
    const w = Array.from({ length: n }, (_, i) => 1 / Math.pow(i + 1, 0.62));
    const max = w[0] || 1;
    return w.map((x) => x / max);
  }

  /* ---------------------------------------------------------------- */
  /* Favorites                                                        */
  /* ---------------------------------------------------------------- */
  const isFav = (w) => favorites.includes(w);
  function toggleFav(word, btn) {
    if (isFav(word)) {
      favorites = favorites.filter((w) => w !== word);
      toast(`<b>${word.toUpperCase()}</b> removed`, "info");
    } else {
      favorites = [word, ...favorites].slice(0, 40);
      toast(`<b>${word.toUpperCase()}</b> favorited`);
    }
    save(STORE.favorites, favorites);
    document.querySelectorAll(`.fav-btn[data-word="${word}"]`).forEach((b) => b.classList.toggle("is-fav", isFav(word)));
    if (btn && isFav(word)) { btn.classList.remove("burst"); void btn.offsetWidth; btn.classList.add("burst"); }
    renderFavorites();
  }
  function makeFavBtn(word) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "fav-btn" + (isFav(word) ? " is-fav" : "");
    b.dataset.word = word;
    b.setAttribute("aria-label", "Toggle favorite");
    b.innerHTML = '<svg viewBox="0 0 24 24" class="star"><path d="M12 2.5l2.9 6.06 6.6.79-4.9 4.55 1.3 6.6L12 17.9 6.1 21.1l1.3-6.6L2.5 9.35l6.6-.79z"/></svg>';
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleFav(word, b); });
    return b;
  }

  /* ---------------------------------------------------------------- */
  /* Render results                                                   */
  /* ---------------------------------------------------------------- */
  function renderResults(guesses) {
    guessesBox.innerHTML = "";
    if (!guesses.length) { bestWrap.hidden = true; emptyState.classList.remove("hidden"); return; }
    emptyState.classList.add("hidden");
    const conf = confidences(guesses.length);

    // best guess
    const best = guesses[0];
    bestWrap.hidden = false;
    bestWord.textContent = best;
    bestFav.dataset.word = best;
    bestFav.classList.toggle("is-fav", isFav(best));
    const pct = Math.round(conf[0] * 100);
    bestPct.textContent = `${pct}%`;
    bestBar.style.width = "0%";
    requestAnimationFrame(() => { bestBar.style.width = `${pct}%`; });

    // remaining guesses
    guesses.slice(1).forEach((word, i) => {
      const idx = i + 1;
      const item = document.createElement("div");
      item.className = "guess";
      item.setAttribute("role", "listitem");
      item.tabIndex = 0;
      item.dataset.word = word;
      item.title = "Click to copy";
      item.style.animationDelay = `${Math.min(i * 45, 320)}ms`;
      const cpct = Math.round(conf[idx] * 100);
      item.innerHTML =
        `<div class="guess-top"><span class="guess-rank">#${idx + 1}</span></div>` +
        `<span class="guess-word">${word}</span>` +
        `<div class="guess-bar"><span></span></div>`;
      item.querySelector(".guess-top").appendChild(makeFavBtn(word));
      const bar = item.querySelector(".guess-bar span");
      requestAnimationFrame(() => { bar.style.width = `${cpct}%`; });
      item.addEventListener("click", () => copyWord(word, item));
      guessesBox.appendChild(item);
    });
  }

  /* ---------------------------------------------------------------- */
  /* History + favorites panels                                       */
  /* ---------------------------------------------------------------- */
  function pushHistory(entry) {
    history = history.filter((h) => h.board !== entry.board);
    history.unshift(entry);
    history = history.slice(0, MAX_HISTORY);
    save(STORE.history, history);
    renderHistory();
  }
  function renderHistory() {
    historyList.innerHTML = "";
    const has = history.length > 0;
    historyEmpty.hidden = has;
    clearHistory.hidden = !has;
    history.forEach((h) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "hist-item";
      item.innerHTML =
        `<span class="hist-avatar">${(h.answer || "?").slice(0, 2)}</span>` +
        `<span class="hist-body">` +
          `<span class="hist-word">${h.answer || "—"}</span>` +
          `<span class="hist-meta">` +
            `<span class="hist-tag ${h.mode === 4 ? "m4" : ""}">${h.mode}L</span>` +
            `<span>${h.count.toLocaleString()} words</span>` +
          `</span></span>`;
      item.addEventListener("click", () => {
        board.value = h.board; board.focus();
        toast("Board restored", "info");
        solve();
      });
      historyList.appendChild(item);
    });
  }
  function renderFavorites() {
    favoritesList.innerHTML = "";
    const has = favorites.length > 0;
    favoritesEmpty.hidden = has;
    favorites.forEach((word) => {
      const row = document.createElement("div");
      row.className = "fav-item";
      const w = document.createElement("button");
      w.type = "button"; w.className = "fav-word"; w.textContent = word; w.title = "Click to copy";
      w.addEventListener("click", () => copyWord(word, row));
      const rm = document.createElement("button");
      rm.type = "button"; rm.className = "fav-remove"; rm.setAttribute("aria-label", `Remove ${word}`); rm.textContent = "✕";
      rm.addEventListener("click", () => toggleFav(word));
      row.appendChild(w); row.appendChild(rm);
      favoritesList.appendChild(row);
    });
  }

  /* ---------------------------------------------------------------- */
  /* Solve                                                            */
  /* ---------------------------------------------------------------- */
  let requestId = 0;
  let solveTimer = null;
  let lastConfettiBoard = "";

  async function solve() {
    const text = board.value.trim();
    const current = ++requestId;

    if (!text) {
      setMode(null);
      setStatus("Waiting for a board…");
      showSkeleton(false);
      renderResults([]);
      setPanda("idle", "Paste a board and I'll help! ✨");
      return;
    }

    const mode = detectMode(text);
    setMode(mode);

    if (!mode) {
      setStatus("Add a row with 🟥 🟨 🟩 tiles to detect the mode", "idle");
      showSkeleton(false);
      renderResults([]);
      setPanda("thinking", "Hmm, I need some coloured tiles 🎨");
      return;
    }

    setStatus("Analyzing board…", "loading");
    setPanda("thinking", "Thinking really hard… 🧠");
    showSkeleton(true);

    try {
      const response = await fetch("/solve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, board: text }),
      });
      const data = await response.json();
      if (current !== requestId) return;

      showSkeleton(false);
      if (!response.ok) throw new Error(data.error || "Request failed");

      const guesses = data.guesses || [];
      renderResults(guesses);

      if (data.count) {
        setStatus(`${data.count.toLocaleString()} possible ${data.count === 1 ? "word" : "words"} — ranked best first`, "active");
        pushHistory({ board: text, answer: data.answer, count: data.count, mode: data.mode || mode });

        if (data.count === 1) {
          setPanda("happy", "That's the one! 🎉");
          if (lastConfettiBoard !== text) { lastConfettiBoard = text; fireConfetti(); toast("Puzzle solved! 🎉", "success"); }
        } else {
          setPanda("happy", `Got ${data.count.toLocaleString()} — my top pick is ${(data.answer || "").toUpperCase()}!`);
        }
      } else {
        setStatus("No candidates match this board", "error");
        setPanda("sad", "Uh oh, nothing matches 😳");
      }
    } catch (error) {
      if (current !== requestId) return;
      showSkeleton(false);
      renderResults([]);
      setStatus(error.message || "Something went wrong", "error");
      setPanda("sad", "Something went wrong 😖");
    }
  }

  /* ---------------------------------------------------------------- */
  /* Input events — auto-solve                                        */
  /* ---------------------------------------------------------------- */
  board.addEventListener("paste", () => setTimeout(solve, 0));
  board.addEventListener("input", () => {
    clearTimeout(solveTimer);
    solveTimer = setTimeout(solve, 120);
  });
  clearBtn.addEventListener("click", () => { board.value = ""; board.focus(); solve(); });

  bestFav.addEventListener("click", (e) => { e.stopPropagation(); const w = bestFav.dataset.word; if (w) toggleFav(w, bestFav); });
  bestCard.addEventListener("click", (e) => {
    if (e.target.closest(".fav-btn")) return;
    if (bestWord.textContent && bestWord.textContent !== "—") copyWord(bestWord.textContent, bestCard);
  });
  bestCard.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (bestWord.textContent && bestWord.textContent !== "—") copyWord(bestWord.textContent, bestCard);
    }
  });

  /* ---------------------------------------------------------------- */
  /* Panel tabs                                                       */
  /* ---------------------------------------------------------------- */
  function switchTab(tab) {
    const showFav = tab === "favorites";
    tabHistory.classList.toggle("active", !showFav);
    tabFavorites.classList.toggle("active", showFav);
    tabHistory.setAttribute("aria-selected", String(!showFav));
    tabFavorites.setAttribute("aria-selected", String(showFav));
    historyView.hidden = showFav;
    favoritesView.hidden = !showFav;
    tabIndicator.classList.toggle("right", showFav);
  }
  tabHistory.addEventListener("click", () => switchTab("history"));
  tabFavorites.addEventListener("click", () => switchTab("favorites"));
  clearHistory.addEventListener("click", () => { history = []; save(STORE.history, history); renderHistory(); toast("History cleared", "info"); });

  /* ---------------------------------------------------------------- */
  /* Shortcuts modal                                                  */
  /* ---------------------------------------------------------------- */
  const openModal = () => { shortcutsModal.hidden = false; };
  const closeModal = () => { shortcutsModal.hidden = true; };
  shortcutsBtn.addEventListener("click", openModal);
  shortcutsModal.addEventListener("click", (e) => { if (e.target.hasAttribute("data-close")) closeModal(); });

  /* ---------------------------------------------------------------- */
  /* Keyboard shortcuts                                               */
  /* ---------------------------------------------------------------- */
  document.addEventListener("keydown", (e) => {
    const typing = document.activeElement === board;
    if (!shortcutsModal.hidden && e.key === "Escape") { closeModal(); return; }

    if (e.key === "/" && !typing) { e.preventDefault(); board.focus(); return; }

    if (e.key === "Escape") {
      if (board.value) { board.value = ""; solve(); toast("Board cleared", "info"); }
      board.blur();
      return;
    }

    if (e.key === "Enter" && !typing && !e.metaKey && !e.ctrlKey) {
      const w = bestWord.textContent;
      if (!bestWrap.hidden && w && w !== "—") { e.preventDefault(); copyWord(w, bestCard); }
      return;
    }

    if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && document.activeElement.classList.contains("guess")) {
      const items = [...guessesBox.querySelectorAll(".guess")];
      const idx = items.indexOf(document.activeElement);
      const next = e.key === "ArrowRight" ? idx + 1 : idx - 1;
      if (items[next]) { e.preventDefault(); items[next].focus(); }
    }
  });
  guessesBox.addEventListener("keydown", (e) => {
    const el = e.target.closest(".guess");
    if (!el) return;
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); copyWord(el.dataset.word, el); }
  });

  /* ---------------------------------------------------------------- */
  /* Confetti — the only rAF; short on-demand burst                   */
  /* ---------------------------------------------------------------- */
  function fireConfetti() {
    if (prefersReduced) return;
    const canvas = $("confetti");
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const W = window.innerWidth, H = window.innerHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.scale(dpr, dpr);

    const colors = ["#a78bfa", "#7cc7ff", "#5ee0b0", "#ff9ec4", "#ffd76a", "#ffb59e"];
    const pieces = Array.from({ length: 90 }, () => ({
      x: W / 2 + (Math.random() - 0.5) * 120,
      y: H * 0.34,
      vx: (Math.random() - 0.5) * 10,
      vy: Math.random() * -12 - 4,
      size: Math.random() * 8 + 5,
      rot: Math.random() * Math.PI,
      vr: (Math.random() - 0.5) * 0.3,
      color: colors[(Math.random() * colors.length) | 0],
      round: Math.random() > 0.5,
      life: 1,
    }));

    let frame = 0;
    function tick() {
      ctx.clearRect(0, 0, W, H);
      let alive = false;
      for (const p of pieces) {
        p.vy += 0.3; p.vx *= 0.99;
        p.x += p.vx; p.y += p.vy; p.rot += p.vr;
        if (frame > 55) p.life -= 0.02;
        if (p.life > 0 && p.y < H + 40) {
          alive = true;
          ctx.save();
          ctx.globalAlpha = Math.max(p.life, 0);
          ctx.translate(p.x, p.y); ctx.rotate(p.rot);
          ctx.fillStyle = p.color;
          if (p.round) { ctx.beginPath(); ctx.arc(0, 0, p.size / 2, 0, Math.PI * 2); ctx.fill(); }
          else { ctx.fillRect(-p.size / 2, -p.size / 2, p.size, p.size * 0.7); }
          ctx.restore();
        }
      }
      frame++;
      if (alive && frame < 220) requestAnimationFrame(tick);
      else ctx.clearRect(0, 0, W, H);
    }
    tick();
  }

  /* ---- boot ---- */
  renderHistory();
  renderFavorites();
  board.focus();
})();
