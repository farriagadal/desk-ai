// Overlay de Desk AI. Python le reenvía los mismos `ui.*` que a la ventana principal mientras el modo overlay
// está activo; la tarjeta deriva de eso un único estado visible y le informa su tamaño a Python.

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = () => window.pywebview.api;

marked.use({ gfm: true, renderer: { html({ text }) { return esc(text); } } });

const card = $("#card");
const WAVE_BARS = 56;
const st = {
  hotkeys: {}, idleMs: 20000,
  recording: false, recMode: "", processing: "", busy: false, speaking: false, voiceOn: true,
  question: "", answer: "", error: "", meta: "", shots: 0,
  awake: false,        // con algo reciente que mostrar; si no, la tarjeta se reduce a la pastilla
  since: 0,            // inicio del estado con contador (grabación, transcripción, espera)
};
let idleTimer = null;
let tickTimer = null;
let renderQueued = false;

function phase() {
  if (st.recording) return "rec";
  if (st.processing) return "proc";
  if (st.busy) return st.answer ? "stream" : "think";
  if (st.speaking) return "speak";
  if (!st.awake) return "idle";
  return st.error ? "err" : "done";
}

const LABELS = { rec: "Escuchando", think: "Pensando", stream: "Respondiendo", speak: "Hablando", done: "Listo", err: "Error" };

// Algo pasó: la tarjeta se despierta y vuelve a la pastilla tras un rato sin actividad.
function wake() {
  st.awake = true;
  clearTimeout(idleTimer);
  idleTimer = setTimeout(function check() {
    if (["rec", "proc", "think", "stream", "speak"].includes(phase())) {
      idleTimer = setTimeout(check, 2000);  // sigue activo: esperar a que termine
      return;
    }
    st.awake = false;
    render();
  }, st.idleMs);
}

function render() {
  const p = phase();
  card.className = `card ${p}` + (st.question ? " has-q" : "") + (st.answer || st.error ? " has-a" : "");
  const hk = st.hotkeys;
  $("#q").textContent = st.question;
  $("#state-text").textContent = p === "idle"
    ? `Desk AI · mantén ${hk.all || "F7"} para preguntar`
    : p === "proc" ? st.processing.replace(/…$/, "") : LABELS[p];
  $("#shots").textContent = st.shots === 1 ? "1 captura" : `${st.shots} capturas`;
  $("#shots").classList.toggle("on", st.shots > 0 && p !== "rec" && p !== "proc");
  $("#voice").textContent = st.voiceOn ? "Voz" : "Silencio";
  $("#voice").classList.toggle("on", !st.voiceOn);
  $("#keys").innerHTML =
    `<kbd>${esc(hk.all || "")}</kbd>hablar <kbd>${esc(hk.voice || "")}</kbd>${st.speaking ? "callar" : "voz"} ` +
    `<kbd>${esc(hk.overlay || "")}</kbd>ventana`;
  tick();
  clearInterval(tickTimer);
  if (["rec", "proc", "think"].includes(p)) tickTimer = setInterval(tick, 100);
  reportSize(false);
}

function tick() {
  const p = phase();
  const secs = (Date.now() - st.since) / 1000;
  $("#timer").textContent =
    p === "rec" ? `${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, "0")}` :
    p === "proc" || p === "think" ? `${secs.toFixed(1)}s` :
    p === "done" ? st.meta.split(" · ")[0] : "";
}

function renderAnswer(streaming) {
  const a = $("#a");
  a.classList.toggle("error", !st.answer && !!st.error);
  if (!st.answer) { a.textContent = st.error ? `⚠ ${st.error}` : ""; return; }
  a.innerHTML = marked.parse(st.answer);
  if (streaming) {
    const caret = document.createElement("span");
    caret.className = "caret";
    (a.lastElementChild || a).appendChild(caret);
  }
  a.scrollTop = a.scrollHeight;  // si la respuesta no cabe, se ve lo último
  reportSize(false);
}

// ---------- API expuesta a Python (mismos nombres que la ventana principal) ----------
window.ui = {
  init(s) {
    st.hotkeys = s.hotkeys;
    st.idleMs = s.idle_seconds * 1000;
    this.snapshot(s.snapshot);
  },

  snapshot(s) {
    Object.assign(st, { question: s.question, answer: s.answer, error: s.error || "", busy: s.busy,
                        speaking: s.speaking, voiceOn: s.voice_on, shots: s.shots, meta: "" });
    st.since = Date.now();
    renderAnswer(s.busy);
    $("#status").textContent = s.status[0];
    $("#status").className = s.status[1] || "";
    wake();
    render();
  },

  status(text, tone) {
    $("#status").textContent = text;
    $("#status").className = tone || "";
    if (tone === "warn" || tone === "err") { wake(); render(); }
  },

  voice(on, speaking) {
    st.voiceOn = !!on;
    st.speaking = !!speaking;
    if (speaking) wake();
    render();
  },

  recording(on, mode) {
    st.recording = on;
    st.recMode = mode;
    if (on) {
      st.since = Date.now();
      st.processing = "";
      $("#wave").innerHTML = "<i></i>".repeat(WAVE_BARS);
      wake();
    }
    render();
  },

  processing(on, text) {
    if (on && !st.processing) st.since = Date.now();  // al pasar a la CPU el contador sigue
    st.processing = on ? text : "";
    render();
  },

  level(v) {
    const wave = $("#wave");
    if (!wave.firstChild) return;
    const bar = wave.firstChild;
    bar.style.height = `${Math.max(3, Math.min(30, Math.sqrt(v) * 95))}px`;
    wave.appendChild(bar);
  },

  attachments(list) {
    st.shots = list.length;
    if (list.length) wake();
    render();
  },

  busy(on) {
    st.busy = on;
    render();
  },

  beginTurn(text, thumbs) {
    Object.assign(st, { question: text || (thumbs.length ? "(solo captura)" : ""), answer: "", error: "", meta: "",
                        since: Date.now() });
    renderAnswer(true);
    wake();
    render();
  },

  delta(text) {
    st.answer += text;
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      const first = card.classList.contains("think");
      renderAnswer(true);
      if (first) render();
    });
  },

  endTurn(meta, error) {
    st.meta = meta || "";
    st.error = error || "";
    renderAnswer(false);
    wake();
    render();
  },
};

// La ventana se ajusta al tamaño de la tarjeta. No basta el ResizeObserver: WebView2 deja de dispararlo cuando
// pausa el renderizado (ventana oculta o tapada por un juego a pantalla completa) y la ventana quedaba con la
// altura de la respuesta anterior. Por eso también se mide tras cada render (offsetHeight fuerza el layout)
// y cada segundo como red de seguridad; si el tamaño no cambió no se envía nada.
let lastSize = "";
function reportSize(force) {
  const size = `${card.offsetWidth}x${card.offsetHeight}`;
  if ((size === lastSize && !force) || !window.pywebview?.api) return;
  lastSize = size;
  api().overlay_size(card.offsetWidth, card.offsetHeight);
}
new ResizeObserver(() => reportSize(false)).observe(card);

// Arrastrar la tarjeta la mueve (a cualquier pantalla); doble clic la devuelve a su esquina.
card.addEventListener("mousedown", (e) => {
  if (e.button === 0 && e.detail === 1) api().overlay_drag();
});
card.addEventListener("dblclick", () => api().overlay_reset());
setInterval(() => reportSize(false), 1000);

window.addEventListener("pywebviewready", () => {
  api().overlay_ready();
  reportSize(true);
});
render();
