// Interfaz de Desk AI. Python llama a `window.ui.*` con evaluate_js; los botones llaman a `pywebview.api.*`.

const ICONS = {
  sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  zap: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  steps: '<path d="M4 6h16M4 12h10M4 18h6"/>',
  volume: '<path d="M11 5L6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/>',
  mute: '<path d="M11 5L6 9H2v6h4l5 4z"/><path d="M22 9l-6 6M16 9l6 6"/>',
  voice: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  layers: '<path d="M12 2L2 7l10 5 10-5z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  camera: '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z"/><circle cx="12" cy="13" r="3"/>',
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>',
  arrow: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  x: '<path d="M18 6L6 18M6 6l12 12"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  speak: '<path d="M11 5L6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>',
};

const svg = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = () => window.pywebview.api;

function paintIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => { el.innerHTML = svg(el.dataset.icon); });
}

// ---------- Markdown ----------
marked.use({
  gfm: true,
  renderer: {
    html({ text }) { return esc(text); },  // nada de HTML crudo en las respuestas
    code({ text, lang }) {
      const name = (lang || "").trim().split(/\s+/)[0];
      const body = name && hljs.getLanguage(name)
        ? hljs.highlight(text, { language: name, ignoreIllegals: true }).value
        : esc(text);
      return `<div class="code"><div class="code-head"><span>${esc(name || "código")}</span>` +
        `<button data-copy-code>${svg("copy")}<span>Copiar</span></button></div>` +
        `<pre><code class="hljs">${body}</code></pre></div>`;
    },
  },
});

function renderMarkdown(el, text, streaming) {
  el.innerHTML = marked.parse(text);
  if (!streaming) return;
  // cursor parpadeante al final del último bloque de texto
  let target = el.lastElementChild;
  while (target && ["UL", "OL", "LI", "BLOCKQUOTE"].includes(target.tagName) && target.lastElementChild) {
    target = target.lastElementChild;
  }
  if (!target || target.classList.contains("code") || target.tagName === "TABLE") target = el;
  const caret = document.createElement("span");
  caret.className = "caret";
  target.appendChild(caret);
}

// ---------- Estado ----------
const chat = $("#chat");
const scroller = $("#scroller");
const prompt = $("#prompt");
const composer = $("#composer");
let hotkeys = {};
let turn = null;          // { body, buf, meta }
let renderQueued = false;
let recStart = 0;
let recTimer = null;
const WAVE_BARS = 48;

function nearBottom() {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
}
function toBottom(force) {
  if (force || nearBottom()) scroller.scrollTop = scroller.scrollHeight;
}

function autosize() {
  prompt.style.height = "auto";
  prompt.style.height = Math.min(prompt.scrollHeight, 220) + "px";
}

function openImage(src) {
  const lb = $("#lightbox");
  lb.querySelector("img").src = src;
  lb.classList.add("open");
}

function flash(btn, label) {
  const old = btn.innerHTML;
  btn.innerHTML = `${svg("check")}<span>${label}</span>`;
  setTimeout(() => { btn.innerHTML = old; }, 1400);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

// ---------- API expuesta a Python ----------
window.ui = {
  model(label) {
    $("#model-pill").textContent = `Claude · ${label}`;
  },

  init(s) {
    hotkeys = s.hotkeys;
    document.querySelectorAll("kbd[data-hk]").forEach((k) => { k.textContent = hotkeys[k.dataset.hk] || ""; });
    $("#model-pill").textContent = `Claude · ${s.model}`;
    $("#shot-btn").title = "Capturar la pantalla elegida";
    $("#mic-btn").title = "Mantén presionado para dictar";
    $("#all-btn").title = "Mantén presionado: captura + dicta, al soltar se envía";
    $("#send-btn").title = `Enviar (${hotkeys.send} o Enter)`;

    const vs = $("#voice-select");
    vs.innerHTML = s.voices.map((v) => `<option>${esc(v)}</option>`).join("");
    vs.value = s.voice;
    const ms = $("#monitor-select");
    ms.innerHTML = s.monitors.map((v) => `<option>${esc(v)}</option>`).join("");
    ms.value = s.monitor;

    this.voice(s.voice_on, false);
    this.status(s.status[0], s.status[1]);
    this.attachments([]);
  },

  status(text, tone) {
    $("#status-text").textContent = text;
    $("#status-dot").className = `status-dot ${tone || ""}`;
  },

  voice(on, speaking) {
    const b = $("#voice-btn");
    b.classList.toggle("on", !!on);
    b.classList.toggle("speaking", !!speaking);
    b.innerHTML = `${svg(on ? "volume" : "mute")}<span>Voz</span><kbd>${esc(hotkeys.voice || "")}</kbd>`;
  },

  attachments(list) {
    const box = $("#attachments");
    box.classList.toggle("has", list.length > 0);
    box.innerHTML = list.map((a, i) =>
      `<div class="att"><img src="${a.thumb}" data-full="${i}"><span class="size">${a.w}×${a.h}</span>` +
      `<button class="x" data-remove="${i}" title="Quitar">${svg("x")}</button></div>`).join("");
    prompt.placeholder = list.length
      ? `${plural(list.length, "captura")} lista${list.length === 1 ? "" : "s"} · pregunta algo o envía directo`
      : "Escribe o dicta tu consulta…";
  },

  appendPrompt(text) {
    const cur = prompt.value;
    prompt.value = cur + (cur && !/\s$/.test(cur) ? " " : "") + text;
    autosize();
  },

  getPrompt() { return prompt.value; },

  clearPrompt() { prompt.value = ""; autosize(); },

  recording(on, mode) {
    composer.classList.toggle("rec", on);
    composer.classList.remove("proc", "proc-timing");
    $("#mic-btn").classList.toggle("live", on && mode === "mic");
    $("#all-btn").classList.toggle("live", on && mode === "all");
    $("#rec-text").textContent = mode === "all" ? "Grabando… suelta para enviar" : "Grabando… suelta para terminar";
    clearInterval(recTimer);
    if (on) {
      recStart = Date.now();
      $("#wave").innerHTML = "<i></i>".repeat(WAVE_BARS);
      const tick = () => {
        const s = Math.floor((Date.now() - recStart) / 1000);
        $("#rec-time").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      };
      tick();
      recTimer = setInterval(tick, 250);
    }
  },

  processing(on, text) {
    composer.classList.toggle("proc", on);
    clearInterval(recTimer);
    if (!on) { composer.classList.remove("proc-timing"); return; }
    $("#rec-text").textContent = text;
    if (!composer.classList.contains("proc-timing")) {  // al cambiar a la CPU el contador sigue corriendo
      composer.classList.add("proc-timing");
      recStart = Date.now();
    }
    const tick = () => { $("#rec-time").textContent = `${((Date.now() - recStart) / 1000).toFixed(1)}s`; };
    tick();
    recTimer = setInterval(tick, 100);
  },

  level(v) {
    const wave = $("#wave");
    if (!wave.firstChild) return;
    const bar = wave.firstChild;
    const h = Math.max(3, Math.min(28, Math.sqrt(v) * 90));
    bar.style.height = `${h}px`;
    wave.appendChild(bar);  // la barra más nueva entra por la derecha
  },

  busy(on) {
    $("#send-btn").classList.toggle("busy", on);
  },

  beginTurn(text, thumbs) {
    $("#empty")?.remove();
    const user = document.createElement("div");
    user.className = "msg user";
    user.innerHTML =
      (thumbs.length ? `<div class="thumbs">${thumbs.map((t) => `<img src="${t}">`).join("")}</div>` : "") +
      `<div class="bubble ${text ? "" : "muted"}">${esc(text || "(solo captura)")}</div>`;
    chat.appendChild(user);

    const ai = document.createElement("div");
    ai.className = "msg ai";
    ai.innerHTML =
      `<div class="avatar">${svg("sparkles")}</div><div class="ai-body"><div class="ai-name">Claude</div>` +
      `<div class="md"><div class="thinking"><div class="dots"><i></i><i></i><i></i></div>` +
      `<span class="shimmer">Pensando…</span></div></div><div class="meta"></div></div>`;
    chat.appendChild(ai);
    turn = { body: ai.querySelector(".md"), meta: ai.querySelector(".meta"), buf: "" };
    toBottom(true);
  },

  delta(text) {
    if (!turn) return;
    turn.buf += text;
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      if (!turn) return;
      const stick = nearBottom();
      renderMarkdown(turn.body, turn.buf, true);
      if (stick) toBottom(true);
    });
  },

  endTurn(meta, error) {
    if (!turn) return;
    const stick = nearBottom();
    if (turn.buf) renderMarkdown(turn.body, turn.buf, false);
    else turn.body.innerHTML = "";
    if (error) {
      const box = document.createElement("div");
      box.className = "error-box";
      box.textContent = `⚠ ${error}`;
      turn.body.after(box);
    }
    if (turn.buf) {
      turn.meta.innerHTML =
        `<button data-copy-msg>${svg("copy")}<span>Copiar</span></button>` +
        `<button data-speak-msg>${svg("speak")}<span>Leer</span></button>` +
        (meta ? `<span>${esc(meta)}</span>` : "");
      turn.meta.dataset.raw = turn.buf;
    } else if (meta) {
      turn.meta.innerHTML = `<span>${esc(meta)}</span>`;
    }
    turn = null;
    if (stick) toBottom(true);
  },

  reset() {
    turn = null;
    location.reload();
  },
};

// ---------- Eventos de la interfaz ----------
paintIcons();

prompt.addEventListener("input", autosize);
prompt.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    api().send();
  }
});

$("#send-btn").addEventListener("click", () => api().send());
$("#shot-btn").addEventListener("click", () => api().shot());
$("#reset-btn").addEventListener("click", () => api().reset());
$("#overlay-btn").addEventListener("click", () => api().toggle_overlay());

// ---------- Ventana sin marco ----------
$("#min-btn").addEventListener("click", () => api().minimize());
$("#close-btn").addEventListener("click", () => api().close());
$(".topbar").addEventListener("mousedown", (e) => {
  if (e.button !== 0 || e.target.closest("button, select, label, input")) return;
  e.preventDefault();
  api().drag("move");
});
document.querySelectorAll("[data-edge]").forEach((el) => {
  el.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    api().drag(el.dataset.edge);
  });
});
$("#voice-btn").addEventListener("click", () => api().toggle_voice());
$("#voice-select").addEventListener("change", (e) => api().set_voice(e.target.value));
$("#monitor-select").addEventListener("change", (e) => api().set_monitor(e.target.value));

// Botones de "mantener presionado": el pointer capture asegura que siempre llegue el soltar.
function holdButton(btn, down, up) {
  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    btn.setPointerCapture(e.pointerId);
    down();
  });
  btn.addEventListener("pointerup", () => up());
  btn.addEventListener("pointercancel", () => up());
}
holdButton($("#mic-btn"), () => api().mic_down(), () => api().mic_up());
holdButton($("#all-btn"), () => api().all_down(), () => api().all_up());

document.addEventListener("click", async (e) => {
  const t = e.target;
  const link = t.closest("a[href]");
  if (link) {
    e.preventDefault();
    api().open_url(link.href);
    return;
  }
  const codeBtn = t.closest("[data-copy-code]");
  if (codeBtn) {
    await copyText(codeBtn.closest(".code").querySelector("code").innerText);
    flash(codeBtn, "Copiado");
    return;
  }
  const msgBtn = t.closest("[data-copy-msg]");
  if (msgBtn) {
    await copyText(msgBtn.closest(".meta").dataset.raw);
    flash(msgBtn, "Copiado");
    return;
  }
  const speakBtn = t.closest("[data-speak-msg]");
  if (speakBtn) {
    api().speak(speakBtn.closest(".meta").dataset.raw);
    return;
  }
  const rm = t.closest("[data-remove]");
  if (rm) {
    api().remove_attachment(Number(rm.dataset.remove));
    return;
  }
  if (t.matches(".thumbs img, .att img")) {
    openImage(t.src);
    return;
  }
  if (t.closest("#lightbox")) $("#lightbox").classList.remove("open");
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#lightbox").classList.remove("open");
});

window.addEventListener("pywebviewready", () => {
  api().ready();
  prompt.focus();
});
