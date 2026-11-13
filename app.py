"""Desk AI: captura de pantalla + voz (Whisper en GPU) -> Claude (usa tu suscripción vía Claude Code).

Atajos globales (funcionan aunque la ventana no tenga el foco):
  F8            Captura la pantalla y la adjunta a la consulta
  F9 (mantener) Graba el micrófono mientras está presionada; al soltar se transcribe en la GPU
  F10           Envía la consulta a Claude
  Ctrl+Win (mantener) Captura + graba; al soltar transcribe y envía (con F7_SCREENSHOT=auto, Claude decide si mira la captura)
  F11           Calla la voz si está hablando; si no, activa/desactiva la lectura en voz alta
  F6            Alterna entre la ventana y el overlay para jugar
"""

import base64
import datetime
import glob
import io
import json
import os
import shutil
import subprocess
import queue
import re
import site
import threading
import time
import ctypes
import ctypes.wintypes
import webbrowser

# Coordenadas en píxeles reales (si no, en pantallas con escala ≠ 100 % la posición del mouse no calza).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    pass

# Las DLL de CUDA (cuBLAS / cuDNN) vienen en los paquetes pip nvidia-*; hay que registrarlas
# antes de importar faster_whisper para que CTranslate2 pueda usar la GPU.
for _sp in site.getsitepackages():
    for _d in glob.glob(os.path.join(_sp, "nvidia", "*", "bin")):
        os.add_dll_directory(_d)
        os.environ["PATH"] = _d + os.pathsep + os.environ["PATH"]

import mss
import numpy as np
import sounddevice as sd
import webview
from faster_whisper import WhisperModel
from PIL import Image

from hotkeys import GlobalHotkeys, key_label
from tray import Tray, ICON as ICON_PATH
from tts import VOICES, Speaker, SpeechFeeder
import actions

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
TITLE = "Desk AI"
OVERLAY_TITLE = "Desk AI · Overlay"


def load_env():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env()

MODEL = os.environ.get("CLAUDE_MODEL", "opus")  # alias de Claude Code: opus, sonnet, haiku…
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
# Ruteo por tarea: cada consulta parte en el modelo rápido y, si es compleja, él mismo la pasa al modelo de arriba
# (CLAUDE_MODEL/CLAUDE_EFFORT) dentro de la misma conversación. CLAUDE_MODEL_FAST vacío lo desactiva.
MODEL_FAST = os.environ.get("CLAUDE_MODEL_FAST", "opus").strip()  # orquestador: Opus 5.5 en esfuerzo bajo
EFFORT_FAST = os.environ.get("CLAUDE_EFFORT_FAST", "low").strip()
ROUTING = bool(MODEL_FAST) and (MODEL_FAST, EFFORT_FAST) != (MODEL, EFFORT)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_LANG = os.environ.get("WHISPER_LANG", "es") or None
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8_float16")  # la mitad de VRAM que float16
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM", "1"))
# Respaldo en CPU: si la GPU no termina a tiempo (un juego la está acaparando) se transcribe también en la CPU
# y se usa el primer resultado. WHISPER_FALLBACK_MODEL vacío lo desactiva.
WHISPER_FALLBACK_MODEL = os.environ.get("WHISPER_FALLBACK_MODEL", "small").strip()
WHISPER_FALLBACK_AFTER = float(os.environ.get("WHISPER_FALLBACK_AFTER", "1.2"))
# Tras una GPU lenta, las siguientes transcripciones corren GPU y CPU en paralelo desde el inicio (sin esperar)
# durante estos segundos; así la espera de WHISPER_FALLBACK_AFTER no se repite en cada grabación.
WHISPER_RACE_FOR = float(os.environ.get("WHISPER_RACE_FOR", "120"))
# Monitor que se captura al iniciar: principal | mouse | todos | 1, 2… (número de la lista del selector)
MONITOR = os.environ.get("MONITOR", "principal").strip().lower()
HOTKEY_SHOT = os.environ.get("HOTKEY_SHOT", "f8")
HOTKEY_MIC = os.environ.get("HOTKEY_MIC", "f9")
HOTKEY_SEND = os.environ.get("HOTKEY_SEND", "f10")
HOTKEY_ALL = os.environ.get("HOTKEY_ALL", "ctrl+win")  # mantener: captura + graba; al soltar transcribe y envía
# Captura de F7: auto = se toma al apretar, pero solo se envía si Claude la pide (ahorra los tokens de la imagen
# cuando la pregunta no depende de la pantalla); always = se envía siempre junto con la pregunta.
F7_SCREENSHOT = os.environ.get("F7_SCREENSHOT", "auto").strip().lower()
SHOT_MARKER = "[VER]"      # Claude pide la captura de F7
THINK_MARKER = "[PENSAR]"  # el modelo rápido pasa la consulta al modelo complejo


MODEL_LADDER = ["haiku", "sonnet", "opus"]                  # de más rápido a más inteligente
EFFORT_LADDER = ["low", "medium", "high", "xhigh", "max"]
MODEL_NAMES = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus"}
EFFORT_NAMES = {"low": "bajo", "medium": "medio", "high": "alto", "xhigh": "muy alto", "max": "máximo"}
# Variantes que Whisper suele escribir al oír el nombre del modelo.
_MODEL_RE = r"(opus|opu|sonn?et+e?|soneto|sonnett|j?h?aiku|jaicu)"
_EFFORT_WORDS = {"minimo": "low", "bajo": "low", "low": "low", "medio": "medium", "normal": "medium",
                 "medium": "medium", "alto": "high", "high": "high", "muy alto": "xhigh", "xhigh": "xhigh",
                 "maximo": "max", "max": "max"}
_FILLER = set("""por favor ahora y ya oye claude de del el la los las un una uno para esto eso ok vale bueno que mas
              me te mi tu con en a al lo le se si porfa porfavor tambien igual entonces pues""".split())


def _plain(text):
    import unicodedata
    t = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def parse_config_command(text, current):
    """Detecta pedidos como "usa opus", "usa un modelo más inteligente", "esfuérzate más", "modo automático".

    `current` es (modelo, esfuerzo) de la última respuesta. Devuelve (config, solo_comando, descripción):
    config es (modelo, esfuerzo), "auto" o None si no hay comando; solo_comando indica que no hay pregunta aparte.
    """
    import re
    t = _plain(text)
    model, effort = current
    found, spans = None, []

    def hit(pattern):
        m = re.search(pattern, t)
        if m:
            spans.append(m.span())
        return m

    if hit(r"\b(modo|modelo|vuelve a(l)?( modo)?|ponte en( modo)?)\s*automatico\b"):
        found = "auto"
    else:
        m = hit(r"\b(usa|usar|utiliza|cambia(te)?( a)?|pasa(te)? a|pon|ponte|con)\s+(el\s+|a\s+)?(modelo\s+)?"
                + _MODEL_RE + r"\b")
        if m:
            name = m.group(m.lastindex)
            model = "opus" if name.startswith("opu") else "sonnet" if name.startswith("son") else "haiku"
            found = True
        elif hit(r"\b(modelo\s+(mas\s+)?(inteligente|listo|potente|capaz|avanzado|bueno)|mejor modelo|"
                 r"modelo\s+mejor|algo\s+mas\s+inteligente)\b"):
            i = MODEL_LADDER.index(model) if model in MODEL_LADDER else 0
            model, found = MODEL_LADDER[min(i + 1, len(MODEL_LADDER) - 1)], True
        elif hit(r"\bmodelo\s+(mas\s+)?(rapido|liviano|ligero|barato|simple)\b"):
            i = MODEL_LADDER.index(model) if model in MODEL_LADDER else len(MODEL_LADDER)
            model, found = MODEL_LADDER[max(i - 1, 0)], True

        m = hit(r"\besfuerzo\s+(al\s+)?(minimo|bajo|medio|normal|muy alto|alto|maximo|low|medium|xhigh|high|max)\b")
        if m:
            effort, found = _EFFORT_WORDS[m.group(2)], True
        elif hit(r"\b(esfuerzate( mas)?|mas esfuerzo|sube (el|tu) esfuerzo|piensa(lo)? mas|piensalo mejor|"
                 r"razona mas|mas a fondo|esfuerzate al maximo)\b"):
            i = EFFORT_LADDER.index(effort) if effort in EFFORT_LADDER else 1
            effort, found = EFFORT_LADDER[min(i + 1, len(EFFORT_LADDER) - 1)], True
        elif hit(r"\b(menos esfuerzo|baja (el|tu) esfuerzo|esfuerzate menos|piensa menos)\b"):
            i = EFFORT_LADDER.index(effort) if effort in EFFORT_LADDER else 1
            effort, found = EFFORT_LADDER[max(i - 1, 0)], True
    if not found:
        return None, False, ""
    rest = t
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + " " + rest[b:]
    words = [w for w in re.findall(r"[a-z0-9]+", rest) if w not in _FILLER]
    only = len(words) <= 1
    if found == "auto":
        return "auto", only, "modo automático"
    return (model, effort), only, f"{MODEL_NAMES.get(model, model)}, esfuerzo {EFFORT_NAMES.get(effort, effort)}"


def marker_note(markers):
    """Nota que se agrega a la consulta para que Claude pueda pedir la captura o un modelo más capaz."""
    if not markers:
        return ""
    opts = []
    if SHOT_MARKER in markers:
        opts.append("si para responder bien necesitas ver mi pantalla en este momento (no la adjunté), "
                    f"responde exactamente {SHOT_MARKER}")
    if THINK_MARKER in markers:
        opts.append("si la consulta es compleja (estrategia, análisis, comparar opciones, varios pasos) y una "
                    f"respuesta rápida podría estar mal, responde exactamente {THINK_MARKER} y la responderá "
                    + ("el mismo modelo pensando con más esfuerzo" if MODEL_FAST == MODEL else "un modelo más capaz"))
    return ("\n\n[Nota automática: " + "; ".join(opts) + ". En ese caso responde solo el marcador, sin nada "
            "más y antes de buscar en la web. Si no, responde normalmente.]")
HOTKEY_VOICE = os.environ.get("HOTKEY_VOICE", "f11")
HOTKEY_OVERLAY = os.environ.get("HOTKEY_OVERLAY", "f6")  # alterna entre la ventana y el overlay para jugar
# Overlay: esquina de la pantalla (top-right, top-left, top-center, bottom-right, bottom-left, bottom-center),
# en el monitor que se captura; segundos sin actividad antes de reducirse a una pastilla.
# Acciones en el PC tipo Jarvis (abrir apps, webs y Spotify, mover ventanas entre pantallas, multimedia).
PC_ACTIONS = os.environ.get("PC_ACTIONS", "1").strip().lower() not in ("0", "no", "false", "off")
# Cómo abre la app: overlay (el widget para jugar; F6 muestra la ventana) o ventana.
START_MODE = os.environ.get("START_MODE", "overlay").strip().lower()
OVERLAY_POSITION = os.environ.get("OVERLAY_POSITION", "top-right").strip().lower()
OVERLAY_MARGIN = int(os.environ.get("OVERLAY_MARGIN", "24"))
OVERLAY_IDLE = float(os.environ.get("OVERLAY_IDLE", "20"))
TTS_ENABLED = os.environ.get("TTS_ENABLED", "1").strip().lower() not in ("0", "no", "false", "off")
TTS_VOICE = os.environ.get("TTS_VOICE", "es-CL-CatalinaNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+5%")  # velocidad de la voz: -20%, +0%, +15%…
SAMPLE_RATE = 16000
MAX_IMAGE_EDGE = 2560
MAX_IMAGE_BYTES = 4_500_000
# Búsqueda web de la versión del juego: once (una vez por juego en la conversación), always (en cada consulta), off.
GAME_SEARCH = os.environ.get("GAME_SEARCH", "once").strip().lower()

SYSTEM_PROMPT = (
    "Eres un asistente de escritorio. El usuario te envía capturas de su pantalla y "
    "consultas dictadas por voz. La transcripción es automática y a veces sale mal: palabras cambiadas por otras "
    "que suenan parecido, nombres propios (juegos, personajes, objetos, canciones, apps) deformados o frases cortadas. "
    "Deduce lo que quiso decir según el contexto (la conversación, la captura, el juego). Si la corrección es obvia, "
    "responde directo sin mencionarla; si tuviste que adivinar algo importante, empieza con algo como "
    "\"Creo que quisiste decir X:\" y responde eso; si de verdad no se entiende, o es una acción en el PC que podría "
    "salir mal si adivinas, pregunta en pocas palabras \"¿Quisiste decir X?\" en vez de actuar. "
    "Normalmente está jugando y te escucha en voz alta sin poder dejar de mirar el juego, así que "
    "cada segundo de respuesta le cuesta. Responde en español en unas 20 palabras como máximo: "
    "lo que tiene que hacer ahora, basándote en lo que se ve en la captura. Da una sola opción, la "
    "más probable; sin saludos, sin repetir la pregunta ni explicaciones extra salvo que las pida; si quiere más detalle, "
    "te lo dirá. Nada de listas, tablas ni formato: solo prosa que suene natural al oírla. "
    f"Hoy es {datetime.date.today():%Y-%m-%d}. Si la consulta es sobre un juego, responde siempre según su "
    "versión actual (último parche o temporada): los juegos cambian seguido y tu conocimiento puede estar "
    "desactualizado; nunca recomiendes algo que fue eliminado o cambiado. "
    + {
        "always": "Si tu respuesta depende de algo que cambia entre parches (objetos, builds, campeones, "
                  "personajes, habilidades, meta, mecánicas, ubicaciones), verifícalo antes con una búsqueda web rápida. ",
        "once": "Haz UNA sola búsqueda web por juego en toda la conversación: la primera vez que aparezca un juego, "
                "busca su parche o temporada actual y sus cambios principales, y desde ahí usa lo que ya encontraste "
                "sin volver a buscar. Solo busca de nuevo si el usuario lo pide o cambia de juego. Cada búsqueda "
                "le cuesta varios segundos. ",
        "off": "No hagas búsquedas web: responde con lo que sabes. ",
    }.get(GAME_SEARCH, "")
    + "No escribas nada antes de buscar: tu texto se lee en voz alta."
    + (actions.prompt() if PC_ACTIONS else "")
)


class Recorder:
    def __init__(self):
        self.frames = []
        self.stream = None
        self.level = 0.0  # volumen RMS del último bloque, para la onda de la interfaz

    def _callback(self, data, *_):
        self.frames.append(data.copy())
        self.level = float(np.sqrt(np.mean(data * data)))

    def start(self):
        self.frames = []
        self.level = 0.0
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=self._callback)
        self.stream.start()

    def stop(self):
        if self.stream is None:
            return np.zeros(0, dtype=np.float32)
        self.stream.stop()
        self.stream.close()
        self.stream = None
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.frames)[:, 0]


class ClaudeSession:
    """Proceso `claude -p` persistente (Claude Code) autenticado con la suscripción de claude.ai.

    Se comunica por stdin/stdout en formato stream-json: cada consulta es una línea JSON y la
    conversación se mantiene viva dentro del mismo proceso, así que hay contexto para seguimientos.
    """

    def __init__(self, on_event):
        self.on_event = on_event  # callback(kind, payload) llamado desde el hilo lector
        self.proc = None
        self.config = (MODEL, EFFORT)  # modelo y esfuerzo activos en el proceso
        self.req = 0

    def _start(self):
        exe = shutil.which("claude")
        if exe is None:
            raise FileNotFoundError("No se encontró `claude` en el PATH. Instala Claude Code y ejecuta `claude` para iniciar sesión.")
        # Sin API key en el entorno, Claude Code usa la sesión de tu suscripción (claude /login).
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        self.proc = subprocess.Popen(
            [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--include-partial-messages",
             "--model", MODEL, "--effort", EFFORT,
             "--tools", "" if GAME_SEARCH == "off" else "WebSearch", "--allowedTools", "WebSearch", "--strict-mcp-config", "--setting-sources", "",
             "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env, cwd=HERE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.config = (MODEL, EFFORT)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "stream_event":
                e = ev["event"]
                if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                    self.on_event("text", e["delta"]["text"])
                elif e.get("type") == "content_block_start" and e["content_block"].get("type") in ("tool_use", "server_tool_use"):
                    self.on_event("search", e["content_block"].get("name", ""))
            elif t == "result":
                self.on_event("result", ev)
        if self.proc is proc:  # el proceso terminó solo (error, sesión cerrada…)
            err = proc.stderr.read().strip()
            self.proc = None
            self.on_event("dead", err or f"claude terminó con código {proc.wait()}")

    def _write(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _control(self, request):
        self.req += 1
        self._write({"type": "control_request", "request_id": f"ia{self.req}", "request": request})

    def send(self, content, model=MODEL, effort=EFFORT):
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        # Se cambia en vivo, sin perder la conversación; Claude Code los aplica antes del mensaje que sigue.
        if model != self.config[0]:
            self._control({"subtype": "set_model", "model": model})
        if effort != self.config[1]:
            self._control({"subtype": "apply_flag_settings", "settings": {"effortLevel": effort}})
        self.config = (model, effort)
        self._write({"type": "user", "message": {"role": "user", "content": content}})

    def close(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.kill()


def set_window_icon(hwnd):
    """Pone el logo en la barra de título y en la barra de tareas (pywebview no lo permite en Windows)."""
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
        hicon = user32.LoadImageW(None, ICON_PATH, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if hicon:
            user32.SendMessageW(ctypes.c_void_p(hwnd), 0x80, which, hicon)  # WM_SETICON


def find_hwnd(window, title=TITLE):
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:  # noqa: BLE001
        return ctypes.windll.user32.FindWindowW(None, title) or None


def style_frame(hwnd):
    """Ventana sin marco: esquinas redondeadas y borde sutil del color de la interfaz (Windows 11)."""
    dwm = ctypes.windll.dwmapi
    for attr, value in ((20, 1), (33, 2), (34, 0x00221C1C)):  # modo oscuro · esquinas redondeadas · borde (0x00BBGGRR)
        v = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))


# Códigos de WM_NCHITTEST que la interfaz puede pedir: mover (barra superior) o redimensionar (bordes).
HIT_CODES = {"move": 2, "w": 10, "e": 11, "n": 12, "nw": 13, "ne": 14, "s": 15, "sw": 16, "se": 17}


def native_drag(window, hwnd, edge):
    """Inicia el arrastre/redimensionado nativo de Windows (con Aero Snap) como si se tomara el marco."""
    from System import Action  # pythonnet, ya cargado por pywebview

    def go():
        user32 = ctypes.windll.user32
        pt = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.ReleaseCapture()  # WebView2 tiene capturado el mouse
        lparam = (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF)
        user32.PostMessageW(ctypes.c_void_p(hwnd), 0x00A1, HIT_CODES[edge], lparam)  # WM_NCLBUTTONDOWN

    window.native.BeginInvoke(Action(go))  # hay que hacerlo en el hilo de la ventana


def exclude_from_capture(hwnd):
    """Hace la ventana invisible para capturas de pantalla (Windows 10 2004+), sin ocultarla."""
    WDA_EXCLUDEFROMCAPTURE = 0x11
    return bool(ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE))


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD), ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT), ("dwFlags", ctypes.wintypes.DWORD)]


class Overlay:
    """Segunda ventana, compacta y siempre encima, para usar mientras se juega.

    No toma el foco, se puede arrastrar a cualquier pantalla (doble clic: vuelve a la esquina) y no sale en las capturas. Nunca se muestra con los métodos de
    pywebview (activarían la ventana y le quitarían el foco al juego): se muestra/oculta con SetWindowPos.
    WebView2 no permite transparencia por píxel, así que la ventana es la tarjeta y toma su tamaño.
    """

    WS_EX = 0x80000 | 0x80 | 0x08000000  # LAYERED · TOOLWINDOW · NOACTIVATE (recibe el mouse para arrastrarla)
    POS_FILE = os.path.join(HERE, ".overlay_pos.json")

    def __init__(self, api):
        self.window = webview.create_window(
            OVERLAY_TITLE, os.path.join(UI_DIR, "overlay.html"), js_api=api,
            width=400, height=60, frameless=True, easy_drag=False, on_top=True, focus=False, hidden=True,
            min_size=(40, 20),  # el mínimo por defecto de pywebview (200x100) dejaba la pastilla con 100 px de alto
            background_color="#0e0e13",
        )
        self.hwnd = None
        self.ready = False
        self.visible = False
        self.size = (400, 60)  # px CSS de la tarjeta
        self.point = (1, 1)    # punto (px reales) dentro del monitor donde va
        self.dragging = False
        self.game = None       # ventana que tenía el foco (el juego), para devolvérselo tras arrastrar
        # Posición elegida arrastrando: monitor (un punto dentro) + esquina más cercana y distancia a ella en px
        # reales, así al crecer o achicarse la tarjeta se queda pegada a ese borde.
        try:
            with open(self.POS_FILE, encoding="utf-8") as f:
                self.custom = json.load(f)
        except (OSError, ValueError):
            self.custom = None

    def _handle(self):
        if self.hwnd is None:
            hwnd = find_hwnd(self.window, OVERLAY_TITLE)
            if not hwnd:
                return None
            u = ctypes.windll.user32
            u.GetWindowLongW.restype = ctypes.c_long
            ex = u.GetWindowLongW(ctypes.c_void_p(hwnd), -20)
            u.SetWindowLongW(ctypes.c_void_p(hwnd), -20, ex | self.WS_EX)
            u.SetLayeredWindowAttributes(ctypes.c_void_p(hwnd), 0, 255, 2)  # LWA_ALPHA opaco
            try:
                style_frame(hwnd)
                exclude_from_capture(hwnd)
            except (AttributeError, OSError):
                pass
            self.hwnd = hwnd
        return self.hwnd

    def _place(self, show):
        hwnd = self._handle()
        if not hwnd:
            return
        u = ctypes.windll.user32
        u.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.wintypes.DWORD]
        u.MonitorFromPoint.restype = ctypes.c_void_p
        point = tuple(self.custom["point"]) if self.custom else self.point
        mon = u.MonitorFromPoint(ctypes.wintypes.POINT(*point), 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(info))
        dpi_x, dpi_y = ctypes.c_uint(96), ctypes.c_uint(96)
        try:
            ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        except (AttributeError, OSError):
            pass
        scale = dpi_x.value / 96
        work = info.rcWork
        w, h, m = round(self.size[0] * scale), round(self.size[1] * scale), round(OVERLAY_MARGIN * scale)
        if self.custom:
            c = self.custom
            x = work.left + c["dx"] if c["h"] == "left" else work.right - w - c["dx"]
            y = work.top + c["dy"] if c["v"] == "top" else work.bottom - h - c["dy"]
            x = max(work.left, min(x, work.right - w))   # que no quede fuera de la pantalla
            y = max(work.top, min(y, work.bottom - h))
        else:
            vert, _, horiz = OVERLAY_POSITION.partition("-")
            x = {"left": work.left + m, "center": (work.left + work.right - w) // 2}.get(horiz, work.right - w - m)
            y = work.bottom - h - m if vert == "bottom" else work.top + m
        if show and not u.IsWindowVisible(ctypes.c_void_p(hwnd)):
            # ShowWindow (no SetWindowPos) para que WinForms reciba WM_SHOWWINDOW y muestre el WebView2.
            u.ShowWindow(ctypes.c_void_p(hwnd), 4)  # SW_SHOWNOACTIVATE
        u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), x, y, w, h, 0x10)  # HWND_TOPMOST · NOACTIVATE

    def drag(self):
        """Arrastre nativo de Windows (entre monitores también); al soltar se guarda dónde quedó."""
        hwnd = self._handle()
        if not hwnd or self.dragging:
            return
        self.dragging = True
        native_drag(self.window, hwnd, "move")

        def wait():
            u = ctypes.windll.user32
            time.sleep(0.15)
            while u.GetAsyncKeyState(0x01) & 0x8000:  # botón izquierdo aún presionado
                time.sleep(0.05)
            time.sleep(0.1)
            self._remember()
            self.dragging = False
            self._place(show=True)  # ajusta el tamaño a la escala (DPI) del monitor nuevo
            u.GetForegroundWindow.restype = ctypes.c_void_p
            if self.game and u.GetForegroundWindow() == hwnd:
                u.SetForegroundWindow(ctypes.c_void_p(self.game))

        threading.Thread(target=wait, daemon=True).start()

    def _remember(self):
        u = ctypes.windll.user32
        r = ctypes.wintypes.RECT()
        u.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(r))
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        u.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.wintypes.DWORD]
        u.MonitorFromPoint.restype = ctypes.c_void_p
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        u.GetMonitorInfoW(ctypes.c_void_p(u.MonitorFromPoint(ctypes.wintypes.POINT(cx, cy), 2)), ctypes.byref(info))
        work = info.rcWork
        left = cx < (work.left + work.right) // 2
        top = cy < (work.top + work.bottom) // 2
        self.custom = {"point": [cx, cy], "h": "left" if left else "right", "v": "top" if top else "bottom",
                       "dx": max(0, r.left - work.left if left else work.right - r.right),
                       "dy": max(0, r.top - work.top if top else work.bottom - r.bottom)}
        self._save()

    def reset_position(self):
        self.custom = None
        self._save()
        if self.visible:
            self._place(show=True)

    def _save(self):
        try:
            if self.custom:
                with open(self.POS_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.custom, f)
            elif os.path.exists(self.POS_FILE):
                os.remove(self.POS_FILE)
        except OSError:
            pass

    def show(self, point):
        self.point = point
        self.visible = True
        self._place(show=True)

    def hide(self):
        self.visible = False
        if self._handle():
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(self.hwnd), 0)  # SW_HIDE

    def resize(self, width, height):
        self.size = (width, height)
        if self.visible and not self.dragging:
            self._place(show=True)  # de paso reafirma que va encima del juego

    def js(self, fn, *args):
        if not self.ready:
            return
        try:
            self.window.evaluate_js(f"ui.{fn}({', '.join(json.dumps(a) for a in args)})")
        except Exception:  # noqa: BLE001
            pass


def raise_gpu_priority():
    """Pide a Windows prioridad alta de GPU para este proceso; si no, el juego en primer plano se la lleva toda."""
    D3DKMT_SCHEDULINGPRIORITYCLASS_HIGH = 4
    try:
        gdi32 = ctypes.windll.gdi32
        gdi32.D3DKMTSetProcessSchedulingPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_int]
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        return gdi32.D3DKMTSetProcessSchedulingPriorityClass(handle, D3DKMT_SCHEDULINGPRIORITYCLASS_HIGH) == 0
    except (AttributeError, OSError):
        return False


def data_url(img, edge, quality=82):
    img = img.copy()
    img.thumbnail((edge, edge))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.standard_b64encode(buf.getvalue()).decode()


class Api:
    """Métodos que la interfaz web puede llamar (`pywebview.api.*`). Todo pasa por la cola del App."""

    def __init__(self, app):
        self._app = app

    def _put(self, *ev):
        self._app.events.put(ev)

    def ready(self): self._put("ready")
    def send(self): self._put("send")
    def shot(self): self._put("shot")
    def mic_down(self): self._put("mic_down")
    def mic_up(self): self._put("mic_up")
    def all_down(self): self._put("all_down")
    def all_up(self): self._put("all_up")
    def toggle_voice(self): self._put("toggle_voice")
    def set_voice(self, label): self._put("set_voice", label)
    def set_monitor(self, label): self._put("set_monitor", label)
    def remove_attachment(self, index): self._put("remove_attachment", index)
    def speak(self, text): self._put("speak", text)
    def reset(self): self._put("reset")

    def drag(self, edge):
        if edge in HIT_CODES and self._app.hwnd:
            native_drag(self._app.window, self._app.hwnd, edge)

    def minimize(self): self._app.window.minimize()
    def close(self): self._put("quit")
    def toggle_overlay(self): self._put("overlay")
    def overlay_ready(self): self._put("overlay_ready")
    def overlay_size(self, width, height): self._put("overlay_size", int(width), int(height))
    def overlay_drag(self): self._app.overlay.drag()  # directo: el botón del mouse sigue presionado
    def overlay_reset(self): self._put("overlay_reset")

    def open_url(self, url):
        if url.startswith(("http://", "https://", "mailto:")):
            webbrowser.open(url)


class App:
    def __init__(self):
        self.events = queue.Queue()
        self.window = None
        self.hwnd = None
        self.overlay = None
        self.overlay_mode = False
        self.turn = {"question": "", "answer": "", "error": "", "shots": 0}  # para poblar el overlay al abrirlo
        self.ui_ready = False
        self.closed = False
        self.claude = ClaudeSession(self._on_claude_event)
        self.got_text = False
        self.images = []           # capturas pendientes: (bytes, media_type, preview data-URL, ancho, alto)
        self.recorder = Recorder()
        self.recording = False
        self.auto_send = False
        self.pending_shots = 0     # capturas codificándose en segundo plano
        self.held_shot = None      # captura de F7 en modo auto: se envía solo si Claude la pide
        self.turn_shot = None      # la captura ofrecida en la consulta en curso
        self.markers = []          # marcadores que Claude puede responder en este paso
        self.peeking = False       # mirando el inicio de la respuesta por si es un marcador
        self.peek_buf = ""
        self.marker = None         # marcador recibido: se descarta el resto del texto de ese paso
        self.turn_fast = False     # el paso en curso va al modelo rápido
        self.turn_model = MODEL
        self.turn_config = (MODEL, EFFORT)  # modelo y esfuerzo con que se respondió la última consulta
        self.pinned = None         # (modelo, esfuerzo) fijado por voz; None = ruteo automático
        self.act_in = False        # dentro de un [[ACCION …]] de la respuesta
        self.act_buf = ""
        self.action_log = []       # resultados reales de las acciones, para contárselos a Claude
        self.action_queue = queue.Queue()  # las acciones de una respuesta se ejecutan de a una, en orden
        # Avisos diferidos de las acciones (temporizadores): se muestran y se dicen en voz alta.
        actions.NOTIFY = lambda msg: (self.events.put(("status", msg, "ok")), self.events.put(("say", msg)))
        threading.Thread(target=self._action_worker, daemon=True).start()
        self.src_hold = ""         # final de línea retenido por si empieza un "Sources:"
        self.src_cut = False       # llegó el "Sources:" de la búsqueda web: el resto no se muestra ni se lee
        self.game = None           # ventana en primer plano al preguntar (el juego): recupera el foco
        self.last_question = ""
        self.turn_t0 = 0.0
        self.busy = False
        self.whisper = None
        self.whisper_cpu = None    # modelo chico de respaldo en CPU
        self.whisper_lock = threading.Lock()
        self.race_until = 0.0     # hasta cuándo correr GPU y CPU en paralelo sin esperar
        self.voice_on = TTS_ENABLED
        self.voice_label = next((l for l, v in VOICES if v == TTS_VOICE), VOICES[0][0])
        self.speaker = Speaker(TTS_VOICE, TTS_RATE, on_error=lambda exc: self._set_status(f"Voz: {exc}", "warn"))
        self.feeder = SpeechFeeder(self.speaker.say)
        self.monitor_choices = self._monitor_choices()
        self.monitor_label = self._default_monitor_label()
        self.capture_excluded = False
        self.last_status = ("Cargando Whisper en la GPU…", "busy")
        self.pending_delta = ""

    def run(self):
        api = Api(self)
        self.window = webview.create_window(
            TITLE, os.path.join(UI_DIR, "index.html"), js_api=api,
            width=860, height=760, min_size=(600, 520), on_top=True, background_color="#0a0a0d",
            frameless=True, easy_drag=False,  # sin barra de Windows: la barra de la interfaz hace de título
            hidden=START_MODE == "overlay",  # al abrir en modo overlay la ventana espera oculta hasta F6
        )
        self.overlay = Overlay(api)
        self.window.events.shown += self._on_shown
        self.window.events.closed += lambda: self.events.put(("quit",))
        self._register_hotkeys()
        self.tray = Tray(self, TITLE)
        self.tray.start()
        threading.Thread(target=self._load_whisper, daemon=True).start()
        if PC_ACTIONS:
            threading.Thread(target=actions.load_apps, daemon=True).start()  # tarda ~2 s: que esté listo antes
        threading.Thread(target=self._loop, daemon=True).start()
        try:
            webview.start()
        finally:
            self.closed = True
            self.tray.stop()
            self.claude.close()
            self.speaker.stop()

    def _on_shown(self):
        hwnd = self.hwnd = find_hwnd(self.window)
        if not hwnd:
            return
        try:
            set_window_icon(hwnd)
            style_frame(hwnd)
            self.capture_excluded = exclude_from_capture(hwnd)
        except (AttributeError, OSError):
            pass

    # ---------- Puente con la interfaz ----------
    # Lo que el overlay también necesita saber (mismos nombres en ui/overlay.js).
    OVERLAY_FNS = {"status", "voice", "recording", "processing", "level", "attachments", "busy",
                   "beginTurn", "delta", "endTurn"}

    def js(self, fn, *args):
        if self.closed:
            return None
        if self.overlay_mode and fn in self.OVERLAY_FNS:
            self.overlay.js(fn, *args)
        if not self.ui_ready:
            return None
        try:
            return self.window.evaluate_js(f"ui.{fn}({', '.join(json.dumps(a) for a in args)})")
        except Exception:  # noqa: BLE001  (ventana cerrándose, recarga en curso…)
            return None

    def set_status(self, text, tone="idle"):
        self.last_status = (text, tone)
        self.js("status", text, tone)

    def _set_status(self, text, tone="idle"):
        self.events.put(("status", text, tone))

    def _register_hotkeys(self):
        # El hook ejecuta los callbacks en su propio hilo: los pasamos al bucle mediante la cola.
        put = lambda *ev: (lambda: self.events.put(ev))
        hk = self.hotkeys = GlobalHotkeys()
        hk.on_press(HOTKEY_SHOT, put("shot"))
        hk.on_press(HOTKEY_MIC, put("mic_down"))
        hk.on_release(HOTKEY_MIC, put("mic_up"))
        hk.on_press(HOTKEY_SEND, put("send"))
        hk.on_press(HOTKEY_ALL, put("all_down"))
        hk.on_release(HOTKEY_ALL, put("all_up"))
        hk.on_press(HOTKEY_VOICE, put("voice"))
        hk.on_press(HOTKEY_OVERLAY, put("overlay"))
        hk.start()

    def _loop(self):
        """Hilo único que procesa todos los eventos en orden (el equivalente al mainloop de Tk)."""
        last_flush = last_level = 0.0
        was_speaking = False
        while not self.closed:
            try:
                ev = self.events.get(timeout=0.04)
            except queue.Empty:
                ev = None
            if ev is not None:
                if ev[0] == "delta":
                    self.pending_delta += ev[1]
                    if self.voice_on:
                        self.feeder.feed(ev[1])
                else:
                    self._flush_delta()
                    try:
                        self._handle(ev)
                    except Exception as exc:  # noqa: BLE001
                        self.set_status(f"Error: {exc}", "err")
            now = time.monotonic()
            if self.pending_delta and (ev is None or now - last_flush > 0.05):
                self._flush_delta()
                last_flush = now
            if self.recording and now - last_level > 0.05:
                last_level = now
                self.js("level", round(self.recorder.level, 4))
            speaking = self.speaker.speaking
            if speaking != was_speaking:
                was_speaking = speaking
                self.js("voice", self.voice_on, speaking)

    def _flush_delta(self):
        if self.pending_delta:
            text, self.pending_delta = self.pending_delta, ""
            self.turn["answer"] += text
            self.js("delta", text)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "ready":
            self.ui_ready = True
            self.js("init", {
                "model": self._config_label(),
                "hotkeys": self._hotkey_labels(),
                "voices": [l for l, _ in VOICES], "voice": self.voice_label, "voice_on": self.voice_on,
                "monitors": [l for l, _ in self.monitor_choices], "monitor": self.monitor_label,
                "status": list(self.last_status),
            })
            self._refresh_attachments()
        elif kind == "shot":
            self.take_screenshot()
        elif kind == "add_image":
            self.pending_shots -= 1
            self._add_image(ev[1])
        elif kind == "hold_image":
            self.pending_shots -= 1
            self.held_shot = ev[1]
        elif kind == "attach_held":  # no hubo pregunta: la captura queda adjunta como antes
            if self.pending_shots > 0:
                threading.Timer(0.1, lambda: self.events.put(("attach_held",))).start()
            elif self.held_shot:
                held, self.held_shot = self.held_shot, None
                self._add_image(held)
        elif kind == "marker":
            self._on_marker(ev[1])
        elif kind == "mic_down":
            self.start_recording()
        elif kind == "mic_up":
            self.stop_recording()
        elif kind == "send":
            self.send()
        elif kind == "all_down":
            self.all_down()
        elif kind == "all_up":
            self.all_up()
        elif kind == "voice":
            self.voice_key()
        elif kind == "toggle_voice":
            self.toggle_voice()
        elif kind == "set_voice":
            self._pick_voice(ev[1])
        elif kind == "set_monitor":
            self.monitor_label = ev[1]
        elif kind == "remove_attachment":
            if 0 <= ev[1] < len(self.images):
                del self.images[ev[1]]
                self._refresh_attachments()
        elif kind == "speak":
            self.shut_up()
            self.feeder.feed(ev[1])
            self.feeder.flush()
        elif kind == "reset":
            self.reset()
        elif kind == "auto_send":
            if self.pending_shots > 0:  # la captura aún se está codificando: reintentar en un momento
                threading.Timer(0.1, lambda: self.events.put(("auto_send",))).start()
            elif self.busy:
                self.set_status(f"Claude sigue respondiendo: la consulta quedó lista, envíala con "
                                f"{key_label(HOTKEY_SEND)}.", "warn")
            else:
                self.send()
        elif kind == "action":
            game, ours = self.game, {self.hwnd, self.overlay.hwnd}
            def go(text=ev[1]):
                err = actions.run_text(text, game=game, skip=ours)
                info, actions.INFO[:] = list(actions.INFO), []
                spoken, actions.SAY[:] = list(actions.SAY), []
                for line in spoken:  # respuestas que no pasan por Claude (estado del PC, listas)
                    self.events.put(("status", line, "ok"))
                    self.events.put(("say", line))
                # Claude no ve el resultado al responder: se le cuenta en la siguiente consulta.
                self.action_log.append(f"{text.strip()} -> " + (f"FALLÓ: {err}" if err else
                                                                 " ".join(info) or "hecho"))
                if err:
                    self.events.put(("status", err, "err"))
                    self.events.put(("say", err))
                elif info and not spoken:
                    self.events.put(("status", " ".join(info), "ok"))
            self.action_queue.put(go)  # en orden: "pausa Spotify y pon YouTube" no deben pisarse
        elif kind == "say":
            if self.voice_on:
                self.speaker.say(ev[1])
        elif kind == "status":
            self.set_status(*ev[1:])
        elif kind == "overlay":
            self.toggle_overlay()
        elif kind == "overlay_ready":
            first = not self.overlay.ready
            self.overlay.ready = True
            self.overlay.js("init", {"hotkeys": self._hotkey_labels(), "idle_seconds": OVERLAY_IDLE,
                                     "snapshot": self._overlay_snapshot()})
            if first and START_MODE == "overlay" and not self.overlay_mode:
                self.toggle_overlay()  # abrir directo como widget
        elif kind == "overlay_size":
            self.overlay.resize(*ev[1:])
        elif kind == "overlay_reset":
            self.overlay.reset_position()
        elif kind == "quit":
            self.quit()
        elif kind == "show_window":
            if self.overlay_mode:
                self.toggle_overlay()  # vuelve a la ventana completa
            else:
                self.window.show()
                self.window.restore()
        elif kind == "processing":
            self.js("processing", *ev[1:])
        elif kind == "append_prompt":
            self.js("appendPrompt", ev[1])
        elif kind == "turn_end":
            self._end_turn(*ev[1:])

    @staticmethod
    def _hotkey_labels():
        return {"all": key_label(HOTKEY_ALL), "shot": key_label(HOTKEY_SHOT), "mic": key_label(HOTKEY_MIC),
                "send": key_label(HOTKEY_SEND), "voice": key_label(HOTKEY_VOICE), "overlay": key_label(HOTKEY_OVERLAY)}

    # ---------- Overlay ----------
    def _overlay_snapshot(self):
        return {**self.turn, "busy": self.busy, "speaking": self.speaker.speaking, "voice_on": self.voice_on,
                "shots": len(self.images), "status": list(self.last_status)}

    def toggle_overlay(self):
        self.overlay_mode = not self.overlay_mode
        if self.overlay_mode:
            with mss.mss() as sct:  # el overlay va en el monitor que se captura (donde está el juego)
                mon = self._pick_monitor(sct.monitors)
                point = (1, 1) if mon is sct.monitors[0] else (mon["left"] + mon["width"] // 2,
                                                                mon["top"] + mon["height"] // 2)
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            prev = user32.GetForegroundWindow()  # normalmente el juego
            self.overlay.game = prev if prev and prev != self.hwnd else None
            self.overlay.js("snapshot", self._overlay_snapshot())
            self.window.hide()  # antes de mostrar el overlay, para que Windows no le pase el foco a él
            self.overlay.show(point)
            if user32.GetForegroundWindow() == self.overlay.hwnd and prev and prev != self.hwnd:
                user32.SetForegroundWindow(ctypes.c_void_p(prev))
        else:
            self.overlay.hide()
            self.window.show()

    def quit(self):
        if self.closed:
            return
        self.closed = True
        for w in (self.overlay.window, self.window):
            try:
                w.destroy()
            except Exception:  # noqa: BLE001  (ya cerrada)
                pass

    # ---------- Conversación ----------
    def _end_turn(self, meta, error=None):
        if self.voice_on:
            self.feeder.flush()
        self.turn["error"] = error or ""
        self.js("endTurn", meta, error)
        self.busy = False
        self.js("busy", False)

    def _refresh_attachments(self):
        self.js("attachments", [{"thumb": prev, "w": w, "h": h} for _, _, prev, w, h in self.images])

    # ---------- Captura ----------
    @staticmethod
    def _monitor_choices():
        """[(etiqueta, clave)] donde clave es 'principal', 'mouse', 'todos' o el índice de mss."""
        with mss.mss() as sct:
            mons = sct.monitors
        choices = [("Principal", "principal"), ("Donde está el mouse", "mouse"), ("Todos los monitores", "todos")]
        for i, m in enumerate(mons[1:], start=1):
            tag = " · principal" if m.get("is_primary") else ""
            choices.append((f"Monitor {i}: {m['width']}x{m['height']}{tag}", i))
        return choices

    def _default_monitor_label(self):
        for label, key in self.monitor_choices:
            if str(key) == MONITOR:
                return label
        return self.monitor_choices[0][0]  # Principal

    def _pick_monitor(self, monitors):
        key = dict(self.monitor_choices).get(self.monitor_label, "principal")
        screens = monitors[1:]
        if key == "todos":
            return monitors[0]
        if key == "mouse":
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            for m in screens:
                if m["left"] <= pt.x < m["left"] + m["width"] and m["top"] <= pt.y < m["top"] + m["height"]:
                    return m
        elif isinstance(key, int) and key < len(monitors):
            return monitors[key]
        # Principal (también el respaldo): el que mss marca como primario o el que contiene el origen (0,0).
        for m in screens:
            if m.get("is_primary") or (m["left"] == 0 and m["top"] == 0):
                return m
        return screens[0]

    def take_screenshot(self, background=False, hold=False):
        # La ventana ya está excluida de las capturas; solo si Windows no lo soporta se oculta un instante.
        hide = not self.capture_excluded and self.window is not None and not self.overlay_mode
        if hide:
            self.window.hide()
            time.sleep(0.3)
        try:
            with mss.mss() as sct:
                raw = sct.grab(self._pick_monitor(sct.monitors))
            img = Image.frombytes("RGB", raw.size, raw.rgb)
        finally:
            if hide:
                self.window.show()
        if background:
            # Codificar la imagen toma ~1 s: se hace aparte para no frenar la grabación.
            self.pending_shots += 1
            kind = "hold_image" if hold else "add_image"
            threading.Thread(target=lambda: self.events.put((kind, self._encode(img))), daemon=True).start()
        else:
            self._add_image(self._encode(img))

    @staticmethod
    def _encode(img):
        img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=1)  # nivel 6 (por defecto) tarda ~1 s
        data, media = buf.getvalue(), "image/png"
        if len(data) > MAX_IMAGE_BYTES:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            data, media = buf.getvalue(), "image/jpeg"
        return data, media, data_url(img, 1400), img.width, img.height

    def _add_image(self, image):
        self.images.append(image)
        self._refresh_attachments()
        if not self.recording:
            self.set_status(f"Captura adjuntada · {image[3]}x{image[4]}", "ok")

    # ---------- Audio ----------
    def _load_whisper(self):
        raise_gpu_priority()
        try:
            model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type=WHISPER_COMPUTE)
            with self.whisper_lock:
                self.whisper = model
            self._set_status(f"Listo · Whisper {WHISPER_MODEL} en GPU · Claude {f'{MODEL_FAST}→{MODEL}' if ROUTING else MODEL}", "ok")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Error cargando Whisper en GPU: {exc}", "err")
        if WHISPER_FALLBACK_MODEL:
            try:
                model = WhisperModel(WHISPER_FALLBACK_MODEL, device="cpu", compute_type="int8",
                                     cpu_threads=max(2, (os.cpu_count() or 4) // 2))
                with self.whisper_lock:
                    self.whisper_cpu = model
            except Exception as exc:  # noqa: BLE001
                self._set_status(f"No se pudo cargar el respaldo de Whisper en CPU: {exc}", "warn")

    def start_recording(self, auto_send=False):
        if self.recording:
            return  # repetición automática de la tecla mientras se mantiene presionada
        try:
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Error de micrófono: {exc}", "err")
            return
        self.shut_up()  # que el micrófono no capture la voz
        self.recording = True
        self.auto_send = auto_send
        self.js("recording", True, "all" if auto_send else "mic")
        self.set_status("Grabando… suelta para terminar" + (" y enviar" if auto_send else ""), "rec")

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        self.js("recording", False, "")
        audio = self.recorder.stop()
        if audio.size < SAMPLE_RATE * 0.3:
            if self.auto_send:
                self.events.put(("attach_held",))
            self.set_status("Audio demasiado corto, ignorado" + (" (la captura quedó adjunta)." if self.auto_send else "."), "warn")
            return
        self.set_status("Transcribiendo en la GPU…", "busy")
        self.js("processing", True, "Transcribiendo para enviar…" if self.auto_send else "Transcribiendo…")
        threading.Thread(target=self._transcribe, args=(audio, self.auto_send), daemon=True).start()

    def all_down(self):
        """Botón todo-en-uno: captura la pantalla y empieza a grabar."""
        if self.recording:
            return
        self.start_recording(auto_send=True)  # primero el micrófono, para no perder el inicio de la frase
        if self.recording:
            self.held_shot = None
            self.take_screenshot(background=True, hold=F7_SCREENSHOT == "auto")

    def all_up(self):
        if self.recording and self.auto_send:
            self.stop_recording()

    def _transcribe(self, audio, auto_send=False):
        try:
            self._do_transcribe(audio, auto_send)
        finally:
            self.events.put(("processing", False))  # va después del texto y del auto_send en la cola

    def _do_transcribe(self, audio, auto_send):
        with self.whisper_lock:
            gpu, cpu = self.whisper, self.whisper_cpu
        if gpu is None and cpu is None:
            self._set_status("Whisper aún no está cargado; espera un momento y vuelve a grabar.", "warn")
            return
        t0 = time.time()
        results = queue.Queue()

        def run(model, where):
            try:
                segments, _ = model.transcribe(audio, language=WHISPER_LANG, vad_filter=True, beam_size=WHISPER_BEAM)
                results.put((where, " ".join(s.text.strip() for s in segments).strip()))
            except Exception as exc:  # noqa: BLE001
                results.put((where, exc))

        def start(model, where):
            threading.Thread(target=run, args=(model, where), daemon=True).start()
            return 1

        # Primero la GPU; la CPU solo entra si la GPU se demora (un juego la acapara) o falla.
        # Si la GPU venía lenta hace poco, se corren ambas desde el principio en vez de esperar de nuevo.
        first, running = None, 0
        race = bool(gpu and cpu and time.time() < self.race_until)
        if gpu:
            running += start(gpu, "GPU")
            if race:
                running += start(cpu, "CPU")
            else:
                try:
                    first = results.get(timeout=WHISPER_FALLBACK_AFTER)
                except queue.Empty:
                    if cpu:
                        self.race_until = time.time() + WHISPER_RACE_FOR
                        self._set_status("La GPU está ocupada (¿el juego?): transcribiendo también en la CPU…", "busy")
                        self.events.put(("processing", True, "Transcribiendo en la CPU…"))
        if cpu and not race and (first is None or isinstance(first[1], Exception)):
            running += start(cpu, "CPU")
        where_done, text = first or results.get()
        running -= 1
        if where_done == "GPU" and not isinstance(text, Exception) and time.time() - t0 < WHISPER_FALLBACK_AFTER:
            self.race_until = 0.0  # la GPU volvió a responder rápido: dejar de gastar CPU en paralelo
        while isinstance(text, Exception) and running > 0:  # falló uno: esperar al otro
            where_done, text = results.get()
            running -= 1
        if isinstance(text, Exception):
            self._set_status(f"Error transcribiendo: {text}", "err")
            return
        if text:
            self.events.put(("append_prompt", text))
            self._set_status(f"Transcrito en {time.time() - t0:.1f}s ({where_done})", "ok")
            if auto_send:
                self.events.put(("auto_send",))  # va después de append_prompt en la cola
        else:
            if auto_send:
                self.events.put(("attach_held",))
            self._set_status("No se detectó voz" + (": no se envió (la captura quedó adjunta)." if auto_send else "."), "warn")

    # ---------- Voz ----------
    def shut_up(self):
        self.speaker.stop()
        self.feeder.reset()

    def toggle_voice(self):
        self.voice_on = not self.voice_on
        if not self.voice_on:
            self.shut_up()
        self.js("voice", self.voice_on, False)
        self.set_status("Lectura en voz alta activada." if self.voice_on else "Lectura en voz alta desactivada.")

    def voice_key(self):
        if self.speaker.speaking:
            self.shut_up()
            self.set_status("Voz detenida.")
        else:
            self.toggle_voice()

    def _pick_voice(self, label):
        self.voice_label = label
        self.speaker.voice = dict(VOICES).get(label, TTS_VOICE)
        self.shut_up()
        if self.voice_on:
            self.speaker.say("Hola, esta será mi voz.")

    # ---------- Claude ----------
    def send(self):
        if self.busy:
            return
        if self.recording:
            self.stop_recording()
        text = (self.js("getPrompt") or "").strip()
        retry = False
        if text:  # "usa opus", "esfuérzate más", "modo automático"…
            config, only, desc = parse_config_command(text, self.pinned or (MODEL, EFFORT))
            if config:
                before = self.turn_config
                self.pinned = None if config == "auto" else config
                self._show_config()
                if only:
                    self.js("clearPrompt")
                    after = self.pinned or (MODEL, EFFORT)
                    better = (MODEL_LADDER.index(after[0]) if after[0] in MODEL_LADDER else 9,
                              EFFORT_LADDER.index(after[1]) if after[1] in EFFORT_LADDER else 9) > \
                             (MODEL_LADDER.index(before[0]) if before[0] in MODEL_LADDER else 9,
                              EFFORT_LADDER.index(before[1]) if before[1] in EFFORT_LADDER else 9)
                    if not (better and self.last_question and not self.images):
                        self.held_shot = None
                        self.set_status(f"Listo: {desc}.", "ok")
                        if self.voice_on:
                            self.shut_up()
                            self.speaker.say(f"Listo, {desc}.")
                        return
                    # Pidió más modelo o esfuerzo justo después de una respuesta: se repite esa consulta.
                    text, retry = "Vuelve a responder mi consulta anterior, ahora con más cuidado.", True
        held, self.held_shot = self.held_shot, None
        if held and (self.images or not text):  # ya hay capturas adjuntas o no hay pregunta: va directo
            self.images.append(held)
            held = None
        if not text and not self.images:
            self.set_status("Nada que enviar: dicta, escribe o captura algo primero.", "warn")
            return
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media,
                                         "data": base64.standard_b64encode(data).decode()}}
            for data, media, *_ in self.images
        ]
        self.turn_shot, self.turn_t0 = held, time.time()
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        fg = user32.GetForegroundWindow()
        self.game = fg if fg and fg not in (self.hwnd, self.overlay.hwnd) else self.overlay.game
        try:
            self._ask(content, text or "¿Qué ves en esta captura? Ayúdame con lo que aparece.",
                      fast=ROUTING and not self.pinned)
        except (OSError, FileNotFoundError) as exc:
            self.set_status(f"No se pudo iniciar Claude Code: {exc}", "err")
            return

        self.shut_up()
        if retry:
            text = f"(otra vez, {desc}) {self.last_question}"
        else:
            self.last_question = text or "(solo captura)"
        self.turn = {"question": text or ("(solo captura)" if self.images else ""), "answer": "", "error": "",
                     "shots": len(self.images)}
        self.js("beginTurn", text, [prev for _, _, prev, *_ in self.images])
        self.js("clearPrompt")
        self.images = []
        self._refresh_attachments()
        self.busy = True
        self.got_text = False
        self.js("busy", True)
        self.set_status("Claude está pensando…", "busy")

    def _config_label(self):
        if self.pinned:
            return f"{self.pinned[0]} · {EFFORT_NAMES.get(self.pinned[1], self.pinned[1])}"
        return f"{MODEL_FAST} → {MODEL}" if ROUTING else MODEL

    def _show_config(self):
        self.js("model", self._config_label())

    def _ask(self, content, text, fast):
        """Envía un paso de la consulta; Claude puede responder un marcador en vez de la respuesta."""
        model, effort = (MODEL_FAST, EFFORT_FAST) if fast else (self.pinned or (MODEL, EFFORT))
        self.turn_config = (model, effort)
        markers = ([SHOT_MARKER] if self.turn_shot else []) + ([THINK_MARKER] if fast else [])
        self.markers, self.peeking, self.peek_buf, self.marker = markers, bool(markers), "", None
        self.src_hold, self.src_cut = "", False
        self.turn_fast, self.turn_model = fast, model
        results, self.action_log[:] = list(self.action_log), []
        if results:
            text = ("[Resultado real de tus acciones anteriores: " + " | ".join(results[-6:]) + "]\n\n") + text
        if PC_ACTIONS:
            text = f"[Hora actual: {time.strftime('%H:%M')}]\n" + text
        self.claude.send(content + [{"type": "text", "text": text + marker_note(markers)}], model, effort)

    def _on_marker(self, marker):
        if not self.busy:
            return
        if marker == SHOT_MARKER and self.turn_shot:
            # Claude pidió ver la pantalla: se le manda la captura tomada al apretar F7.
            (data, media, *_), self.turn_shot = self.turn_shot, None
            content = [{"type": "image", "source": {"type": "base64", "media_type": media,
                                                    "data": base64.standard_b64encode(data).decode()}}]
            text, fast = "Esta es mi pantalla en el momento en que pregunté.", self.turn_fast
            self.turn["shots"] = 1
            self.set_status("Claude pidió la captura: mirando la pantalla…", "busy")
        else:
            content, text, fast = [], "Responde ahora la consulta anterior.", False
            self.set_status(f"Consulta compleja: pasando a {(self.pinned or (MODEL,))[0]}…", "busy")
        try:
            self._ask(content, text, fast)
        except (OSError, FileNotFoundError) as exc:
            self._end_turn("", f"No se pudo continuar la consulta: {exc}")

    def _action_worker(self):
        while True:
            job = self.action_queue.get()
            try:
                job()
            except Exception as exc:  # noqa: BLE001
                self.events.put(("status", f"Falló la acción: {exc}", "err"))

    def _take_actions(self, chunk):
        """Saca del texto los [[ACCION {...}]] (se ejecutan, no se muestran ni se leen) y devuelve el resto."""
        if not PC_ACTIONS:
            return chunk
        buf, out = self.act_buf + chunk, ""
        self.act_buf = ""
        while buf:
            if self.act_in:
                # El JSON de una rutina puede traer "]]" dentro: el cierre es el primero que deja un JSON válido.
                end = buf.find("]]")
                while end >= 0 and not actions.is_action(buf[:end]) and len(buf) < 6000:
                    end = buf.find("]]", end + 1)
                if end < 0:
                    self.act_buf = buf
                    return out
                self.events.put(("action", buf[:end]))
                self._set_status("Ejecutando acción…", "busy")
                buf, self.act_in = buf[end + 2:].lstrip("\n"), False
            else:
                start = buf.find("[[")
                if start < 0:
                    if buf.endswith("["):  # puede ser el inicio de "[["
                        out, self.act_buf = out + buf[:-1], "["
                    else:
                        out += buf
                    return out
                out, buf, self.act_in = out + buf[:start], buf[start + 2:], True
        return out

    SOURCES_RE = re.compile(r"\n\s*[*#_]*\s*(Sources|Fuentes)\s*:", re.I)

    def _cut_sources(self, text):
        """Claude Code agrega "Sources: …" tras buscar en la web; eso no se muestra ni se lee en voz alta."""
        if self.src_cut:
            return ""
        buf, self.src_hold = self.src_hold + text, ""
        m = self.SOURCES_RE.search(buf)
        if m:
            self.src_cut = True
            return buf[:m.start()]
        i = buf.rfind("\n")
        if i >= 0:  # la última línea podría estar empezando a ser "Sources:": se retiene hasta saberlo
            head = buf[i:].lstrip("\n \t*#_").lower()
            if any(w.startswith(head) for w in ("sources", "fuentes")):
                self.src_hold = buf[i:]
                return buf[:i]
        return buf

    def _on_claude_event(self, kind, payload):
        # Se llama desde el hilo lector de ClaudeSession.
        if kind == "text":
            if self.marker:
                return  # lo que venga después del marcador no se muestra ni se lee
            if self.peeking:  # ¿la respuesta empieza con un marcador? Esperar hasta saberlo
                self.peek_buf += payload
                head = self.peek_buf.lstrip()
                hit = next((m for m in self.markers if head.startswith(m)), None)
                if hit:
                    self.peeking, self.marker = False, hit
                    return
                if any(m.startswith(head) for m in self.markers):
                    return
                self.peeking, payload = False, self.peek_buf
            payload = self._cut_sources(self._take_actions(payload))
            if not self.got_text:
                payload = payload.lstrip()
                if not payload:
                    return
                self.got_text = True
                self._set_status("Recibiendo respuesta…", "busy")
            self.events.put(("delta", payload))
        elif kind == "search":
            self._set_status("Buscando en la web…", "busy")
        elif kind == "result":
            tail, self.act_buf, self.act_in = ("" if self.act_in else self.act_buf), "", False
            tail = ("" if self.src_cut else self.src_hold) + tail
            self.src_hold = ""
            if tail.strip():
                self.events.put(("delta", tail))
            if self.peeking and self.peek_buf.strip():  # respuesta cortísima que parecía el marcador
                self.events.put(("delta", self.peek_buf))
            self.peeking = False
            marker, self.marker = self.marker, None
            if marker and not payload.get("is_error"):
                self.events.put(("marker", marker))
                return
            if payload.get("is_error"):
                self.events.put(("turn_end", "", f"Error: {payload.get('result') or payload.get('subtype')}"))
                self._set_status("Error en la respuesta (¿límite de la suscripción o sesión expirada?).", "err")
            else:
                secs = time.time() - self.turn_t0  # toda la consulta, con captura o cambio de modelo incluidos
                self.events.put(("turn_end", f"{secs:.1f}s · {self.turn_model}"))
                self._set_status(f"Listo · {secs:.1f}s · suscripción Claude ({self.turn_model})", "ok")
        elif kind == "dead":
            self.peeking, self.marker, self.turn_shot = False, None, None
            self.act_in, self.act_buf = False, ""
            if self.busy:
                self.events.put(("turn_end", "", f"Claude Code se cerró: {payload[:300]}"))
            self._set_status("Claude Code se cerró; se reiniciará con la próxima consulta (contexto perdido).", "warn")

    def reset(self):
        if self.busy:
            return
        self.claude.close()  # el próximo envío abre una sesión nueva
        self.shut_up()
        self.images = []
        self.last_status = ("Nueva conversación.", "idle")
        self.js("reset")  # recarga la página; al volver a estar lista pide de nuevo el estado inicial
        self.ui_ready = False


def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DeskAI.App")
    except (AttributeError, OSError):
        pass
    App().run()


if __name__ == "__main__":
    main()
