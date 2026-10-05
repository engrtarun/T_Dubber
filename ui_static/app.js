async () => {
  /* =======================================================================
     Tarun Dubber - browser runtime.

       * settings store (theme / accent / scale / clock / assistant) in
         localStorage, applied instantly
       * live clock with date + elapsed timer for the running dubbing job
       * Puter.js assistant on ONE 1s ticker

     demo.load() runs this once per full page load, so every interval and
     every handler is stored on `window` / assigned with `onclick` (never
     addEventListener) - re-running can then never double up a ticker or a
     click handler.
     ======================================================================= */

  const SET_KEY = "td_settings_v1";
  const JOB_KEY = "td_job_started_at";

  const DEFAULTS = {
    theme: "system",     // system | light | dark
    accent: "",          // "" = keep whatever the Gradio theme uses
    scale: 100,          // percent, 85..115
    clock: "24",         // "24" | "12"
    seconds: true,       // show seconds in the clock
    persona: "Funny",    // default assistant persona
    refresh: 30,         // seconds between auto insights
    typewriter: true,    // type the answer in instead of dumping it
  };

  const PERSONAS = {
    Funny:
      "You are a highly entertaining and funny Indian AI assistant helping a user with a video dubbing tool. Read the following logs and explain what is happening in 1 or 2 lines. STRICTLY RESPOND IN HINGLISH (Hindi words written in English alphabet). DO NOT USE DEVNAGARI SCRIPT. Make it casual and funny.\nLogs:\n",
    Serious:
      "You are a professional DevOps AI assistant. Read the following logs and provide a 1-line status update. STRICTLY RESPOND IN HINGLISH (Hindi words written in English alphabet). DO NOT USE DEVNAGARI SCRIPT.\nLogs:\n",
    Roast:
      "You are a savage Indian AI assistant who loves roasting the user. Read the logs and explain the status while casually roasting the user in 1-2 lines. STRICTLY RESPOND IN HINGLISH (Hindi words written in English alphabet). DO NOT USE DEVNAGARI SCRIPT.\nLogs:\n",
  };
  const EMOJI = { Funny: "😄", Serious: "🧐", Roast: "🔥" };

  // ---- tear down anything a previous run left behind --------------------
  if (window.td_tick_interval) { clearInterval(window.td_tick_interval); window.td_tick_interval = null; }

  // ============================ settings =================================
  function loadSettings() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(SET_KEY) || "{}") || {}; } catch (e) { saved = {}; }
    const s = Object.assign({}, DEFAULTS, saved);
    if (!PERSONAS[s.persona]) s.persona = DEFAULTS.persona;
    s.theme = ["system", "light", "dark"].includes(s.theme) ? s.theme : DEFAULTS.theme;
    s.clock = String(s.clock) === "12" ? "12" : "24";
    s.seconds = !!s.seconds;
    s.typewriter = !!s.typewriter;
    s.refresh = [15, 30, 60, 120].includes(Number(s.refresh)) ? Number(s.refresh) : DEFAULTS.refresh;
    s.scale = Math.min(115, Math.max(85, Number(s.scale) || DEFAULTS.scale));
    s.accent = typeof s.accent === "string" && /^#[0-9a-fA-F]{6}$/.test(s.accent) ? s.accent : "";
    return s;
  }

  let S = loadSettings();

  function saveSettings() {
    try { localStorage.setItem(SET_KEY, JSON.stringify(S)); } catch (e) { /* private mode: keep going */ }
  }

  const ACCENT_PROPS = [
    "--color-accent", "--color-accent-hover", "--color-accent-soft",
    "--primary-500", "--primary-600", "--primary-700",
    "--button-primary-background-fill", "--button-primary-background-fill-hover",
    "--button-primary-border-color", "--link-text-color",
    "--radio-checked-background-color", "--checkbox-checked-background-color",
    "--slider-color", "--td-accent",
  ];

  function applyTheme() {
    const root = document.documentElement;
    const prefersDark = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    const dark = S.theme === "dark" || (S.theme === "system" && prefersDark);
    root.setAttribute("data-td-theme", dark ? "dark" : "light");
    root.style.setProperty("--td-zoom", String(S.scale / 100), "important");
    if (S.accent) {
      for (const p of ACCENT_PROPS) root.style.setProperty(p, S.accent, "important");
    } else {
      for (const p of ACCENT_PROPS) root.style.removeProperty(p);
    }
  }

  function paintSeg(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    for (const b of el.querySelectorAll("button[data-v]")) {
      b.classList.toggle("is-on", b.dataset.v === String(value));
    }
  }

  function wireSeg(id, read, write) {
    const el = document.getElementById(id);
    if (!el) return;
    el.onclick = (ev) => {
      const b = ev.target.closest("button[data-v]");
      if (!b) return;
      write(b.dataset.v);
    };
    paintSeg(id, read());
  }

  function paintSettings() {
    paintSeg("td-set-theme", S.theme);
    paintSeg("td-set-clock", S.clock);

    const sw = document.getElementById("td-set-accent");
    if (sw) {
      const want = (S.accent || "").toLowerCase();
      for (const b of sw.querySelectorAll("button[data-v]")) {
        b.classList.toggle("is-on", ((b.dataset.v || "").toLowerCase()) === want);
      }
    }
    const ci = document.getElementById("td-set-accent-custom");
    if (ci && S.accent) ci.value = S.accent;
    const sc = document.getElementById("td-set-scale");
    if (sc) sc.value = String(S.scale);
    const sv = document.getElementById("td-set-scale-val");
    if (sv) sv.textContent = S.scale + "%";
    const se = document.getElementById("td-set-seconds");
    if (se) se.checked = S.seconds;
    const rf = document.getElementById("td-set-refresh");
    if (rf) rf.value = String(S.refresh);
    const tw = document.getElementById("td-set-typewriter");
    if (tw) tw.checked = S.typewriter;
    const pe = document.getElementById("td-set-persona");
    if (pe) pe.value = S.persona;
  }

  function settingsChanged() {
    saveSettings();
    paintSettings();
    applyTheme();
  }

  function wireSettings() {
    wireSeg("td-set-theme", () => S.theme, (v) => { S.theme = v; settingsChanged(); });
    wireSeg("td-set-clock", () => S.clock, (v) => { S.clock = v; settingsChanged(); tickClock(); });

    const sw = document.getElementById("td-set-accent");
    if (sw) {
      sw.onclick = (ev) => {
        const b = ev.target.closest("button[data-v]");
        if (!b) return;
        S.accent = b.dataset.v || "";
        settingsChanged();
      };
    }
    const ci = document.getElementById("td-set-accent-custom");
    if (ci) ci.oninput = () => { S.accent = ci.value; settingsChanged(); };

    const sc = document.getElementById("td-set-scale");
    if (sc) sc.oninput = () => {
      S.scale = Math.min(115, Math.max(85, Number(sc.value) || 100));
      settingsChanged();
    };

    const se = document.getElementById("td-set-seconds");
    if (se) se.onchange = () => { S.seconds = !!se.checked; settingsChanged(); tickClock(); };

    const rf = document.getElementById("td-set-refresh");
    if (rf) rf.onchange = () => {
      S.refresh = [15, 30, 60, 120].includes(Number(rf.value)) ? Number(rf.value) : DEFAULTS.refresh;
      settingsChanged();
      schedule(S.refresh);
    };

    const tw = document.getElementById("td-set-typewriter");
    if (tw) tw.onchange = () => { S.typewriter = !!tw.checked; settingsChanged(); };

    const pe = document.getElementById("td-set-persona");
    if (pe) pe.onchange = () => {
      if (!PERSONAS[pe.value]) pe.value = S.persona;
      S.persona = pe.value;
      settingsChanged();
      setPersonaUI(S.persona);
    };

    const panel = document.getElementById("td-settings-panel");
    const btn = document.getElementById("td-settings-btn");
    if (btn && panel) {
      btn.onclick = () => {
        const open = panel.hasAttribute("hidden");
        if (open) panel.removeAttribute("hidden"); else panel.setAttribute("hidden", "");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      };
    }
    const close = document.getElementById("td-settings-close");
    if (close && panel && btn) {
      close.onclick = () => {
        panel.setAttribute("hidden", "");
        btn.setAttribute("aria-expanded", "false");
      };
    }
    const reset = document.getElementById("td-settings-reset");
    if (reset) {
      reset.onclick = () => {
        S = Object.assign({}, DEFAULTS);
        settingsChanged();
        setPersonaUI(S.persona);
        schedule(S.refresh);
        tickClock();
      };
    }

    // Follow the OS theme while `theme: system` is selected.
    const scheme = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    if (scheme && scheme.addEventListener) {
      if (window.td_scheme_handler) scheme.removeEventListener("change", window.td_scheme_handler);
      window.td_scheme_handler = () => { if (S.theme === "system") applyTheme(); };
      scheme.addEventListener("change", window.td_scheme_handler);
    }
  }

  // ============================== clock ==================================
  const pad = (n) => String(n).padStart(2, "0");

  function clockText(d) {
    let h = d.getHours();
    const m = d.getMinutes();
    const s = d.getSeconds();
    if (S.clock === "12") {
      const ap = h >= 12 ? "PM" : "AM";
      h = h % 12 || 12;
      return pad(h) + ":" + pad(m) + (S.seconds ? ":" + pad(s) : "") + " " + ap;
    }
    return pad(h) + ":" + pad(m) + (S.seconds ? ":" + pad(s) : "");
  }

  function dateText(d) {
    try {
      return d.toLocaleDateString(undefined, {
        weekday: "short", day: "2-digit", month: "short", year: "numeric",
      });
    } catch (e) {
      return d.toDateString();
    }
  }

  function tickClock() {
    const now = new Date();
    const t = document.getElementById("td-clock-time");
    const dt = document.getElementById("td-clock-date");
    if (t) t.textContent = clockText(now);
    if (dt) dt.textContent = dateText(now);
  }

  // ============================ job timer ================================
  function readLogs() {
    const el = document.querySelector("#log_output_box textarea");
    return el ? (el.value || "") : "";
  }

  function jobStart() {
    const v = Number(sessionStorage.getItem(JOB_KEY));
    return v > 0 ? v : 0;
  }

  function setJobStart(ts) {
    try {
      if (ts) sessionStorage.setItem(JOB_KEY, String(ts));
      else sessionStorage.removeItem(JOB_KEY);
    } catch (e) { /* storage blocked: timer just becomes approximate */ }
  }

  let logsSeenAt = 0;

  function durationText(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s);
  }

  function tickJob() {
    const box = document.getElementById("td-job-time");
    const note = document.getElementById("td-job-note");
    const wrap = document.getElementById("td-job");
    if (!box || !note) return;

    const hasLogs = readLogs().trim() !== "";
    const now = Date.now();
    if (hasLogs && !logsSeenAt) logsSeenAt = now;
    if (!hasLogs) logsSeenAt = 0;

    const started = jobStart();
    const startAt = started || logsSeenAt;
    if (!startAt) {
      box.textContent = "⏱ idle";
      note.textContent = "koi job nahi chal rahi";
      if (wrap) wrap.classList.remove("is-live");
      return;
    }
    box.textContent = "⏱ " + durationText(now - startAt);
    note.textContent =
      (started ? "start " + new Date(started).toLocaleTimeString() : "approx (page load se)") +
      (hasLogs ? " · logs live" : " · logs khaali");
    if (wrap) wrap.classList.toggle("is-live", hasLogs);
  }

  const startBtn = document.getElementById("td-start-btn");
  if (startBtn) startBtn.onclick = () => { if (!jobStart()) setJobStart(Date.now()); };
  const jobReset = document.getElementById("td-job-reset");
  if (jobReset) jobReset.onclick = () => { setJobStart(0); logsSeenAt = 0; tickJob(); };

  // ======================= persona (Gradio radio) ========================
  // The chips get emoji-prefixed below, so reading the raw label text would
  // yield "😄 Funny" and miss PERSONAS entirely - always strip decoration.
  function stripDecoration(s) {
    return String(s || "")
      .replace(/[\u{1F000}-\u{1FAFF}\u{2190}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}\u{20E3}]/gu, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function personaFromUI() {
    const checked = document.querySelector('#puter_mode_selector input[type="radio"]:checked');
    if (checked) {
      const label = checked.closest("label");
      const raw = label
        ? label.textContent
        : (checked.nextElementSibling ? checked.nextElementSibling.textContent : checked.value);
      const name = stripDecoration(raw);
      if (PERSONAS[name]) return name;
    }
    return PERSONAS[S.persona] ? S.persona : "Funny";
  }

  function setPersonaUI(name) {
    const wrap = document.getElementById("puter_mode_selector");
    if (!wrap) return;
    for (const input of wrap.querySelectorAll('input[type="radio"]')) {
      const label = input.closest("label");
      const raw = label
        ? label.textContent
        : (input.nextElementSibling ? input.nextElementSibling.textContent : input.value);
      if (stripDecoration(raw) === name) {
        if (!input.checked) input.click();
        return;
      }
    }
  }

  // Gradio can rebuild the radio markup (tab switch, rerender) - re-applying
  // the emoji decoration every tick keeps it intact without doubling up.
  function decoratePersonas() {
    const wrap = document.getElementById("puter_mode_selector");
    if (!wrap) return;
    for (const s of wrap.querySelectorAll("label span")) {
      const name = stripDecoration(s.textContent);
      const emoji = EMOJI[name];
      if (emoji && s.textContent.trim() !== emoji + " " + name) s.textContent = emoji + " " + name;
    }
  }

  // ========================= assistant plumbing ==========================
  function setStatus(text, busy) {
    const el = document.getElementById("puter-countdown");
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("is-busy", !!busy);
  }

  // Puter answers as a string, {message:{content}}, {content} or an array of
  // parts - anything else used to render as "[object Object]".
  function extractText(v) {
    if (v == null) return "";
    if (typeof v === "string") return v;
    if (typeof v === "number" || typeof v === "boolean") return String(v);
    if (Array.isArray(v)) return v.map(extractText).join("");
    if (typeof v === "object") {
      for (const k of ["message", "content", "text"]) {
        if (v[k] != null && v[k] !== "") return extractText(v[k]);
      }
      return "";
    }
    return String(v);
  }

  function renderHistory() {
    const el = document.getElementById("puter-history");
    if (!el) return;
    const list = window.puter_history || [];
    el.replaceChildren();
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "h-empty";
      empty.textContent = "Abhi koi update nahi.";
      el.appendChild(empty);
      return;
    }
    for (const h of list) {
      const row = document.createElement("div");
      const meta = document.createElement("div");
      meta.className = "h-meta";
      meta.textContent = "[" + h.ts + "] " + h.persona;
      row.appendChild(meta);
      // text node, never markup: log-derived text must stay plain text
      row.appendChild(document.createTextNode(h.text));
      el.appendChild(row);
    }
  }

  function pushHistory(persona, text) {
    window.puter_history = window.puter_history || [];
    const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    window.puter_history.unshift({ persona: persona, ts: ts, text: text });
    if (window.puter_history.length > 6) window.puter_history.pop();
    renderHistory();
  }

  let typeGen = 0;

  function showThinking() {
    const el = document.getElementById("puter-message");
    if (!el) return;
    typeGen += 1;
    el.replaceChildren();
    const s = document.createElement("span");
    s.className = "puter-thinking";
    s.textContent = "⏳ Analyzing logs...";
    el.appendChild(s);
  }

  function renderMessage(text) {
    const el = document.getElementById("puter-message");
    if (!el) return;
    const gen = ++typeGen;
    el.replaceChildren();
    if (!S.typewriter) { el.textContent = text; return; }
    let i = 0;
    (function step() {
      if (gen !== typeGen || !el.isConnected) return;
      const chunk = text.slice(i, i + 4);
      i += 4;
      if (!chunk) return;
      el.appendChild(document.createTextNode(chunk));
      setTimeout(step, 25);
    })();
  }

  let puterLoading = false;

  async function ensurePuter() {
    if (typeof puter !== "undefined") return true;
    if (puterLoading) return false;
    puterLoading = true;
    try {
      await new Promise((resolve) => {
        const s = document.createElement("script");
        s.src = "https://js.puter.com/v2/";
        s.onload = resolve;
        s.onerror = resolve;
        document.head.appendChild(s);
      });
    } finally {
      puterLoading = false;
    }
    return typeof puter !== "undefined";
  }

  let nextAt = Date.now() + S.refresh * 1000;
  let inFlight = false;
  let lastKey = "";
  let failStreak = 0;

  function schedule(sec) { nextAt = Date.now() + Math.max(0, sec) * 1000; }
  function secsLeft() { return Math.max(0, Math.ceil((nextAt - Date.now()) / 1000)); }

  // puter.ai.chat() can hang forever (auth popup dismissed, flaky network).
  // Without this the card froze on "Analyzing..." and inFlight stayed true,
  // so the ticker never recovered - one dead request killed the assistant.
  const CHAT_TIMEOUT_MS = 30000;

  function withTimeout(promise, ms) {
    let timer = 0;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("timeout after " + Math.round(ms / 1000) + "s")), ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  function showReason(message) {
    const errEl = document.getElementById("puter-error");
    if (!errEl) return;
    errEl.style.display = "block";
    const detail = document.getElementById("puter-error-detail");
    if (detail) detail.textContent = message ? String(message).slice(0, 160) : "";
  }

  async function insight(force) {
    // One call at a time, and NEVER a re-entry just because nothing changed:
    // the ticker owns rescheduling, so an unchanged log can't spin the loop.
    if (inFlight) return;

    const logs = readLogs();
    if (!logs.trim()) { setStatus("Waiting for logs...", false); schedule(3); return; }

    if (typeof puter === "undefined") {
      setStatus("Assistant offline - retrying...", false);
      schedule(10);
      void ensurePuter();
      return;
    }

    const persona = personaFromUI();
    const recent = logs.split("\n").filter((l) => l.trim() !== "").slice(-12).join("\n");
    const key = persona + "\n" + recent;
    if (!force && key === lastKey) { schedule(S.refresh); return; }

    inFlight = true;
    setStatus("Analyzing...", true);
    showThinking();
    try {
      const response = await withTimeout(
        puter.ai.chat(PERSONAS[persona] + recent),
        CHAT_TIMEOUT_MS
      );
      const text = (extractText(response) || "").trim() || "(khaali reply - dobara try karo)";
      lastKey = key;
      window.puter_last_successful_key = key;
      window.puter_last_text = text;
      const errEl = document.getElementById("puter-error");
      if (errEl) errEl.style.display = "none";
      const detail = document.getElementById("puter-error-detail");
      if (detail) detail.textContent = "";
      pushHistory(persona, text);
      renderMessage(text);
      failStreak = 0;
      const lastEl = document.getElementById("puter-last");
      if (lastEl) lastEl.textContent = "updated " + new Date().toLocaleTimeString();
      schedule(S.refresh);
    } catch (e) {
      const why = (e && e.message) ? e.message : "request failed";
      console.error("Puter error:", e);
      // 10s, then 30s, then 60s: a dead endpoint must not be hammered, and
      // the countdown keeps telling the user when the next try happens.
      failStreak += 1;
      schedule(failStreak < 2 ? 10 : failStreak < 4 ? 30 : 60);
      showReason(why);
      setStatus("Error: " + why + " - retry in " + secsLeft() + "s", false);
      // The spinner must not sit there claiming progress that is not happening.
      const msg = document.getElementById("puter-message");
      if (msg && !window.puter_last_text) {
        typeGen += 1;
        msg.replaceChildren();
        const hint = document.createElement("span");
        hint.className = "puter-thinking";
        hint.textContent = "⚠️ Insight nahi mila (" + why + "). Neeche Retry dabao — "
          + "ya upar countdown khatam hone par main khud dobara try karunga.";
        msg.appendChild(hint);
      }
    } finally {
      inFlight = false;
    }
  }

  function tickAssistant() {
    const note = document.getElementById("puter-auto-note");
    if (note) note.textContent = "auto: har " + S.refresh + "s · persona " + personaFromUI();
    if (inFlight) return;

    const logs = readLogs();
    if (!logs.trim()) { setStatus("Waiting for logs...", false); return; }

    if (typeof puter === "undefined") {
      setStatus("Assistant offline - loading...", false);
      if (Date.now() >= nextAt) { void ensurePuter(); schedule(10); }
      return;
    }

    setStatus("Next update in " + secsLeft() + "s", false);
    if (Date.now() >= nextAt) void insight(false);
  }

  // ============================== buttons ================================
  const refreshBtn = document.getElementById("puter-refresh-btn");
  if (refreshBtn) refreshBtn.onclick = () => { schedule(0); void insight(true); };

  const retryBtn = document.getElementById("puter-retry-btn");
  if (retryBtn) retryBtn.onclick = () => {
    const e = document.getElementById("puter-error");
    if (e) e.style.display = "none";
    const d = document.getElementById("puter-error-detail");
    if (d) d.textContent = "";
    schedule(0);
    void insight(true);
  };

  const copyBtn = document.getElementById("puter-copy-btn");
  if (copyBtn) copyBtn.onclick = async () => {
    const text = window.puter_last_text || "";
    if (!text || !navigator.clipboard) return;
    const original = "📋 Copy latest";
    try {
      await navigator.clipboard.writeText(text);
      copyBtn.textContent = "✅ Copied!";
    } catch (e) {
      copyBtn.textContent = "❌ Copy failed";
    }
    setTimeout(() => { copyBtn.textContent = original; }, 1500);
  };

  const logsBtn = document.getElementById("puter-scrolllogs-btn");
  if (logsBtn) logsBtn.onclick = () => {
    const box = document.getElementById("log_output_box");
    if (box) box.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  // ============================== start up ===============================
  applyTheme();
  paintSettings();
  wireSettings();
  decoratePersonas();
  setPersonaUI(S.persona);
  renderHistory();
  tickClock();
  tickJob();

  window.td_tick_interval = setInterval(() => {
    tickClock();
    tickJob();
    decoratePersonas();
    tickAssistant();
  }, 1000);

  void ensurePuter();
}
