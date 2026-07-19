/* ============================================================================
   WordSeek — frontend logic
   - Auto-solve on paste/input (debounced, stale-request safe)
   - Mode auto-detection (4 / 5 letter)
   - Best-guess spotlight + ranked guesses with confidence bars
   - Copy on click, favorites (★), persistent history — all in localStorage
   - Keyboard shortcuts, confetti on unique solve, toasts, skeleton loading
   - Ambient particle field + aurora mouse parallax (perf & a11y aware)
   The backend contract is unchanged:
     POST /solve  {mode, board}  ->  {text, answer, guesses[], count, mode}
   ========================================================================== */
(function () {
  "use strict";

  /* ---------------------------------------------------------------- */
  /* Element references                                               */
  /* ---------------------------------------------------------------- */
  const $ = (id) => document.getElementById(id);

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

  /* ---------------------------------------------------------------- */
  /* Local persistence                                                */
  /* ---------------------------------------------------------------- */
  const STORE = { history: "ws.history", favorites: "ws.favorites" };
  const MAX_HISTORY = 12;

  const load = (key, fallback) => {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
    catch { return fallback; }
  };
  const save = (key, value) => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore quota */ }
  };

  let history   = load(STORE.history, []);
  let favorites = load(STORE.favorites, []);

  /* ---------------------------------------------------------------- */
  /* Toast notifications                                              */
  /* ---------------------------------------------------------------- */
  function toast(message, kind = "success") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const icon = kind === "error" ? "!" : kind === "info" ? "i" : "✓";
    el.innerHTML = `<span class="toast-icon">${icon}</span><span>${message}</span>`;
    toastStack.appendChild(el);
    // Auto-dismiss
    setTimeout(() => {
      el.classList.add("out");
      el.addEventListener("animationend", () => el.remove(), { once: true });
    }, 1600);
    // Never let the stack grow unbounded
    while (toastStack.children.length > 3) toastStack.firstChild.remove();
  }

  /* ---------------------------------------------------------------- */
  /* Clipboard                                                        */
  /* ---------------------------------------------------------------- */
  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const t = document.createElement("textarea");
      t.value = text;
      t.style.position = "fixed";
      t.style.opacity = "0";
      document.body.appendChild(t);
      t.select();
      try { document.execCommand("copy"); } catch { /* noop */ }
      t.remove();
    }
  }

  function copyWord(word, sourceEl) {
    copy(word);
    toast(`<b>${word.toUpperCase()}</b> copied`);
    if (sourceEl) {
      sourceEl.classList.add("copied");
      setTimeout(() => sourceEl.classList.remove("copied"), 500);
    }
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
    if (mode === 4 || mode === 5) {
      modeLabel.textContent = `${mode} LETTER`;
      modeBadge.classList.add(`mode-${mode}`);
    } else {
      modeLabel.textContent = "AUTO";
    }
    modeBadge.classList.remove("pop");
    void modeBadge.offsetWidth; // restart animation
    modeBadge.classList.add("pop");
  }

  /* ---------------------------------------------------------------- */
  /* Status                                                           */
  /* ---------------------------------------------------------------- */
  function setStatus(message, state = "idle") {
    statusText.textContent = message;
    statusDot.className = "status-dot" + (state !== "idle" ? ` ${state}` : "");
    inputWrap.classList.toggle("solving", state === "loading");
  }

  function showSkeleton(show) {
    skeleton.classList.toggle("show", show);
    if (show) {
      bestWrap.hidden = true;
      guessesBox.innerHTML = "";
      emptyState.classList.add("hidden");
    }
  }

  /* ---------------------------------------------------------------- */
  /* Confidence weighting (visual only)                               */
  /* Backend returns ranked order, not scores. We map rank -> a       */
  /* smoothly decaying confidence normalised so the top guess = 100%. */
  /* ---------------------------------------------------------------- */
  function confidences(n) {
    const weights = Array.from({ length: n }, (_, i) => 1 / Math.pow(i + 1, 0.62));
    const max = weights[0] || 1;
    return weights.map((w) => w / max);
  }

  /* ---------------------------------------------------------------- */
  /* Favorites helpers                                                */
  /* ---------------------------------------------------------------- */
  const isFav = (word) => favorites.includes(word);

  function toggleFav(word, btn) {
    if (isFav(word)) {
      favorites = favorites.filter((w) => w !== word);
      toast(`<b>${word.toUpperCase()}</b> removed`, "info");
    } else {
      favorites = [word, ...favorites].slice(0, 40);
      toast(`<b>${word.toUpperCase()}</b> favorited`);
    }
    save(STORE.favorites, favorites);
    // Reflect state on every matching star currently on screen
    document.querySelectorAll(`.fav-btn[data-word="${word}"]`).forEach((b) => {
      b.classList.toggle("is-fav", isFav(word));
    });
    if (btn && isFav(word)) {
      btn.classList.remove("burst"); void btn.offsetWidth; btn.classList.add("burst");
    }
    renderFavorites();
  }

  function makeFavBtn(word, small) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "fav-btn" + (isFav(word) ? " is-fav" : "");
    b.dataset.word = word;
    b.setAttribute("aria-label", "Toggle favorite");
    b.innerHTML =
      '<svg viewBox="0 0 24 24" class="star"><path d="M12 2.5l2.9 6.06 6.6.79-4.9 4.55 1.3 6.6L12 17.9 6.1 21.1l1.3-6.6L2.5 9.35l6.6-.79z"/></svg>';
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleFav(word, b); });
    return b;
  }

  /* ---------------------------------------------------------------- */
  /* Render guesses                                                   */
  /* ---------------------------------------------------------------- */
  function renderResults(guesses) {
    guessesBox.innerHTML = "";

    if (!guesses.length) {
      bestWrap.hidden = true;
      emptyState.classList.remove("hidden");
      return;
    }

    emptyState.classList.add("hidden");
    const conf = confidences(guesses.length);

    // --- Best guess spotlight ---
    const best = guesses[0];
    bestWrap.hidden = false;
    bestWord.textContent = best;
    bestFav.dataset.word = best;
    bestFav.classList.toggle("is-fav", isFav(best));
    const pct = Math.round(conf[0] * 100);
    bestPct.textContent = `${pct}%`;
    // animate the bar from 0
    bestBar.style.width = "0%";
    requestAnimationFrame(() => { bestBar.style.width = `${pct}%`; });

    // --- Remaining guesses ---
    guesses.slice(1).forEach((word, i) => {
      const rank = i + 2;
      const idx = i + 1;
      const item = document.createElement("div");
      item.className = "guess";
      item.setAttribute("role", "listitem");
      item.setAttribute("tabindex", "0");
      item.dataset.word = word;
      item.title = "Click to copy";
      item.style.animationDelay = `${Math.min(i * 45, 320)}ms`;

      const cpct = Math.round(conf[idx] * 100);
      item.innerHTML =
        `<div class="guess-top">` +
          `<span class="guess-rank">#${rank}</span>` +
        `</div>` +
        `<span class="guess-word">${word}</span>` +
        `<div class="guess-bar"><span style="width:0%"></span></div>`;

      // favorite star (top-right)
      item.querySelector(".guess-top").appendChild(makeFavBtn(word, true));

      // animate confidence bar
      const bar = item.querySelector(".guess-bar span");
      requestAnimationFrame(() => { bar.style.width = `${cpct}%`; });

      item.addEventListener("click", () => copyWord(word, item));
      guessesBox.appendChild(item);
    });
  }

  /* ---------------------------------------------------------------- */
  /* History                                                          */
  /* ---------------------------------------------------------------- */
  function pushHistory(entry) {
    // dedupe by board text
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
          `</span>` +
        `</span>`;
      item.addEventListener("click", () => {
        board.value = h.board;
        board.focus();
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
      w.type = "button";
      w.className = "fav-word";
      w.textContent = word;
      w.title = "Click to copy";
      w.addEventListener("click", () => copyWord(word, row));

      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "fav-remove";
      rm.setAttribute("aria-label", `Remove ${word}`);
      rm.textContent = "✕";
      rm.addEventListener("click", () => toggleFav(word));

      row.appendChild(w);
      row.appendChild(rm);
      favoritesList.appendChild(row);
    });
  }

  /* ---------------------------------------------------------------- */
  /* Solve — talks to the Flask backend                               */
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
      return;
    }

    const mode = detectMode(text);
    setMode(mode);

    if (!mode) {
      setStatus("Add a row with 🟥 🟨 🟩 tiles to detect the mode", "idle");
      showSkeleton(false);
      renderResults([]);
      return;
    }

    setStatus("Analyzing board…", "loading");
    showSkeleton(true);

    try {
      const response = await fetch("/solve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, board: text }),
      });
      const data = await response.json();
      if (current !== requestId) return; // a newer request superseded this one

      showSkeleton(false);
      if (!response.ok) throw new Error(data.error || "Request failed");

      const guesses = data.guesses || [];
      renderResults(guesses);

      if (data.count) {
        setStatus(
          `${data.count.toLocaleString()} possible ${data.count === 1 ? "word" : "words"} — ranked best first`,
          "active"
        );
        // Persist to history
        pushHistory({
          board: text,
          answer: data.answer,
          count: data.count,
          mode: data.mode || mode,
        });
        // Celebrate a unique solve (exactly one candidate) — once per board
        if (data.count === 1 && lastConfettiBoard !== text) {
          lastConfettiBoard = text;
          fireConfetti();
          toast("Puzzle solved! 🎉", "success");
        }
      } else {
        setStatus("No candidates match this board", "error");
      }
    } catch (error) {
      if (current !== requestId) return;
      showSkeleton(false);
      renderResults([]);
      setStatus(error.message || "Something went wrong", "error");
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

  clearBtn.addEventListener("click", () => {
    board.value = "";
    board.focus();
    solve();
  });

  /* Star on the best guess */
  bestFav.addEventListener("click", (e) => {
    e.stopPropagation();
    const word = bestFav.dataset.word;
    if (word) toggleFav(word, bestFav);
  });
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

  clearHistory.addEventListener("click", () => {
    history = [];
    save(STORE.history, history);
    renderHistory();
    toast("History cleared", "info");
  });

  /* ---------------------------------------------------------------- */
  /* Shortcuts modal                                                  */
  /* ---------------------------------------------------------------- */
  function openModal() { shortcutsModal.hidden = false; }
  function closeModal() { shortcutsModal.hidden = true; }
  shortcutsBtn.addEventListener("click", openModal);
  shortcutsModal.addEventListener("click", (e) => {
    if (e.target.hasAttribute("data-close")) closeModal();
  });

  /* ---------------------------------------------------------------- */
  /* Keyboard shortcuts                                               */
  /*   /      focus the board                                         */
  /*   Esc    clear board / close modal                               */
  /*   Enter  copy the best guess (when not typing in the board)      */
  /*   ←/→    move focus between guesses                              */
  /* ---------------------------------------------------------------- */
  document.addEventListener("keydown", (e) => {
    const typing = document.activeElement === board;

    if (!shortcutsModal.hidden && e.key === "Escape") { closeModal(); return; }

    if (e.key === "/" && !typing) {
      e.preventDefault();
      board.focus();
      return;
    }

    if (e.key === "Escape") {
      if (board.value) {
        board.value = "";
        solve();
        toast("Board cleared", "info");
      }
      board.blur();
      return;
    }

    if (e.key === "Enter" && !typing && !e.metaKey && !e.ctrlKey) {
      const w = bestWord.textContent;
      if (!bestWrap.hidden && w && w !== "—") {
        e.preventDefault();
        copyWord(w, bestCard);
      }
      return;
    }

    if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && document.activeElement.classList.contains("guess")) {
      const items = [...guessesBox.querySelectorAll(".guess")];
      const idx = items.indexOf(document.activeElement);
      const next = e.key === "ArrowRight" ? idx + 1 : idx - 1;
      if (items[next]) { e.preventDefault(); items[next].focus(); }
    }
  });

  // Copy a focused guess with space (Enter handled above for best guess)
  guessesBox.addEventListener("keydown", (e) => {
    const el = e.target.closest(".guess");
    if (!el) return;
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      copyWord(el.dataset.word, el);
    }
  });

  /* ---------------------------------------------------------------- */
  /* Confetti — lightweight canvas burst on a unique solve            */
  /* ---------------------------------------------------------------- */
  function fireConfetti() {
    if (prefersReduced) return;
    const canvas = $("confetti");
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    ctx.scale(dpr, dpr);

    const colors = ["#22d3ee", "#7c5cff", "#ff5c8a", "#4c8fff", "#34e5a0", "#ffffff"];
    const W = window.innerWidth;
    const pieces = Array.from({ length: 130 }, () => ({
      x: W / 2 + (Math.random() - 0.5) * 120,
      y: window.innerHeight * 0.35,
      vx: (Math.random() - 0.5) * 11,
      vy: Math.random() * -13 - 4,
      size: Math.random() * 7 + 4,
      rot: Math.random() * Math.PI,
      vr: (Math.random() - 0.5) * 0.3,
      color: colors[(Math.random() * colors.length) | 0],
      life: 1,
    }));

    let frame = 0;
    const gravity = 0.32;
    function tick() {
      ctx.clearRect(0, 0, W, window.innerHeight);
      let alive = false;
      for (const p of pieces) {
        p.vy += gravity;
        p.vx *= 0.99;
        p.x += p.vx;
        p.y += p.vy;
        p.rot += p.vr;
        if (frame > 60) p.life -= 0.02;
        if (p.life > 0 && p.y < window.innerHeight + 40) {
          alive = true;
          ctx.save();
          ctx.globalAlpha = Math.max(p.life, 0);
          ctx.translate(p.x, p.y);
          ctx.rotate(p.rot);
          ctx.fillStyle = p.color;
          ctx.fillRect(-p.size / 2, -p.size / 2, p.size, p.size * 0.6);
          ctx.restore();
        }
      }
      frame++;
      if (alive && frame < 220) {
        requestAnimationFrame(tick);
      } else {
        ctx.clearRect(0, 0, W, window.innerHeight);
      }
    }
    tick();
  }

  /* ---------------------------------------------------------------- */
  /* Ambient particle field (canvas) + aurora parallax                */
  /* ---------------------------------------------------------------- */
  function initParticles() {
    if (prefersReduced) return;
    const canvas = $("particles");
    const ctx = canvas.getContext("2d");
    let w, h, dpr, particles;

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.min(56, Math.floor((w * h) / 26000));
      particles = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        r: Math.random() * 1.6 + 0.4,
        vx: (Math.random() - 0.5) * 0.14,
        vy: (Math.random() - 0.5) * 0.14,
        a: Math.random() * 0.5 + 0.2,
      }));
    }

    function draw() {
      ctx.clearRect(0, 0, w, h);
      for (const p of particles) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0) p.x = w; if (p.x > w) p.x = 0;
        if (p.y < 0) p.y = h; if (p.y > h) p.y = 0;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(180, 190, 255, ${p.a})`;
        ctx.fill();
      }
      requestAnimationFrame(draw);
    }

    resize();
    window.addEventListener("resize", resize, { passive: true });
    draw();
  }

  function initParallax() {
    if (prefersReduced || window.matchMedia("(pointer: coarse)").matches) return;
    const aurora = document.querySelector(".aurora");
    if (!aurora) return;
    let raf = null;
    window.addEventListener("mousemove", (e) => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        const dx = (e.clientX / window.innerWidth - 0.5) * 26;
        const dy = (e.clientY / window.innerHeight - 0.5) * 26;
        aurora.style.transform = `translate(${dx}px, ${dy}px)`;
        raf = null;
      });
    }, { passive: true });
  }

  /* ---------------------------------------------------------------- */
  /* Boot                                                             */
  /* ---------------------------------------------------------------- */
  renderHistory();
  renderFavorites();
  initParticles();
  initParallax();
  board.focus();
})();
