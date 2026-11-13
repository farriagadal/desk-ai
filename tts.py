"""Lectura en voz alta de las respuestas con voces neuronales de Microsoft Edge (edge-tts, gratis, sin API key).

El texto llega en streaming: SpeechFeeder lo corta en frases y lo limpia de Markdown; Speaker sintetiza
la frase siguiente mientras reproduce la actual, así la voz empieza casi de inmediato.
"""

import asyncio
import io
import queue
import re
import threading

import av
import edge_tts
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 24000
VOICES = [  # (etiqueta, voz de edge-tts)
    ("Catalina · Chile", "es-CL-CatalinaNeural"),
    ("Lorenzo · Chile", "es-CL-LorenzoNeural"),
    ("Dalia · México", "es-MX-DaliaNeural"),
    ("Jorge · México", "es-MX-JorgeNeural"),
    ("Elena · Argentina", "es-AR-ElenaNeural"),
    ("Tomás · Argentina", "es-AR-TomasNeural"),
    ("Elvira · España", "es-ES-ElviraNeural"),
    ("Álvaro · España", "es-ES-AlvaroNeural"),
    ("Ximena · España (multilingüe)", "es-ES-XimenaNeural"),
]


def decode_mp3(data):
    chunks = []
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


class Speaker:
    """Dos hilos: uno sintetiza (red) y otro reproduce, para que no haya silencios entre frases."""

    def __init__(self, voice, rate="+0%", on_error=None):
        self.voice = voice
        self.rate = rate
        self.on_error = on_error or (lambda exc: None)
        self.gen = 0  # cada stop() invalida lo que quedó en cola
        self.pending = 0
        self.lock = threading.Lock()
        self.text_q = queue.Queue()
        self.audio_q = queue.Queue()
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    @property
    def speaking(self):
        return self.pending > 0

    def say(self, text):
        with self.lock:
            self.pending += 1
        self.text_q.put((self.gen, text))

    def stop(self):
        with self.lock:
            self.gen += 1
            self.pending = 0
        for q in (self.text_q, self.audio_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    def _done(self, gen):
        with self.lock:
            if gen == self.gen and self.pending > 0:
                self.pending -= 1

    async def _fetch(self, text):
        buf = bytearray()
        async for chunk in edge_tts.Communicate(text, self.voice, rate=self.rate).stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return bytes(buf)

    def _synth_loop(self):
        loop = asyncio.new_event_loop()
        while True:
            gen, text = self.text_q.get()
            if gen != self.gen:
                continue
            try:
                pcm = decode_mp3(loop.run_until_complete(self._fetch(text)))
            except Exception as exc:  # noqa: BLE001  (sin internet, servicio caído…)
                self._done(gen)
                self.on_error(exc)
                continue
            if gen == self.gen:
                self.audio_q.put((gen, pcm))

    def _play_loop(self):
        block = SAMPLE_RATE // 20  # 50 ms: stop() corta casi al instante
        while True:
            item = self.audio_q.get()
            # El stream queda abierto mientras sigan llegando frases, así no hay chasquidos entre ellas.
            try:
                with sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as out:
                    while item is not None:
                        gen, pcm = item
                        for i in range(0, len(pcm), block):
                            if gen != self.gen:
                                break
                            out.write(pcm[i:i + block].reshape(-1, 1))
                        self._done(gen)
                        try:
                            item = self.audio_q.get(timeout=0.4)
                        except queue.Empty:
                            item = None
            except Exception as exc:  # noqa: BLE001  (dispositivo de audio no disponible…)
                self.on_error(exc)


# ---------- Markdown en streaming -> frases para leer ----------
SENTENCE_END_RE = re.compile(r"[.!?…:;](?=\s)")
TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")
LINE_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?)")
LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
URL_RE = re.compile(r"https?://\S+")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def clean(text):
    text = LINK_RE.sub(r"\1", text)
    text = URL_RE.sub("el enlace", text)
    text = re.sub(r"\*\*|__|~~|`|(?<!\w)\*|\*(?!\w)", "", text)
    text = EMOJI_RE.sub("", text)
    text = text.replace("→", ", ").replace("->", ", ")
    return re.sub(r"\s+", " ", text).strip()


class SpeechFeeder:
    """Recibe los deltas de texto de Claude y entrega a `say` frases completas y limpias."""

    MIN_SPLIT = 25  # no cortar frases muy cortas (evita leer "1." de una lista numerada como frase)

    def __init__(self, say):
        self.say = say
        self.reset()

    def reset(self):
        self.buf = ""
        self.in_code = False
        self.line_started = False  # ya se leyó parte de la línea actual

    def feed(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._line(line)
            self.line_started = False
        self._partial()

    def flush(self):
        if self.buf:
            self._line(self.buf)
        self.reset()

    def _partial(self):
        # Párrafos largos llegan como una sola línea: se leen frase a frase sin esperar el salto de línea.
        s = self.buf.lstrip()
        if self.in_code or s.startswith(("`", "|")):
            return
        cut = None
        for m in SENTENCE_END_RE.finditer(self.buf):
            if m.end() >= self.MIN_SPLIT:
                cut = m.end()
        if cut is not None:
            self._emit(self.buf[:cut])
            self.buf = self.buf[cut:]
            self.line_started = True

    def _line(self, line):
        s = line.strip()
        if s.startswith("```"):
            self.in_code = not self.in_code
            if self.in_code:
                self._emit("Te dejé el código en pantalla.")
            return
        if self.in_code or not s or TABLE_SEP_RE.match(s) or re.fullmatch(r"[-*_]{3,}", s):
            return
        if s.startswith("|"):
            s = ", ".join(c.strip() for c in s.strip("|").split("|") if c.strip()) + "."
        self._emit(s)

    def _emit(self, text):
        if not self.line_started:
            text = LINE_PREFIX_RE.sub("", text)
        text = clean(text)
        if re.search(r"\w", text):
            self.say(text)
