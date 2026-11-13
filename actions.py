"""Acciones en el PC que Claude puede pedir (tipo Jarvis) escribiendo [[ACCION {...}]] en su respuesta.

Solo se ejecuta lo de esta lista, nunca comandos arbitrarios: abrir apps, páginas web y música de Spotify,
mover / maximizar / minimizar / enfocar ventanas y teclas multimedia. Tras cada acción se le devuelve el foco
al juego, para que se pueda seguir jugando mientras se abre todo en la otra pantalla.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser

import mss

u = ctypes.windll.user32
k32 = ctypes.windll.kernel32
u.GetForegroundWindow.restype = ctypes.c_void_p
u.GetWindow.restype = ctypes.c_void_p
u.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
u.MonitorFromPoint.restype = ctypes.c_void_p
k32.OpenProcess.restype = ctypes.c_void_p
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

MEDIA_KEYS = {"play_pausa": 0xB3, "siguiente": 0xB0, "anterior": 0xB1, "subir_volumen": 0xAF,
              "bajar_volumen": 0xAE, "silenciar": 0xAD}
MASK_KEY = 0xE8  # tecla sin uso: tocarla habilita SetForegroundWindow desde segundo plano
INFO = []        # lo que de verdad pasó con las acciones (p. ej. qué quedó sonando); lo lee y vacía la app
SAY = []         # respuestas que la app lee en voz alta tal cual (estado del PC, listas): Claude no las ve a tiempo


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def norm(text):
    t = unicodedata.normalize("NFD", str(text).lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def words(text):
    return [w for w in re.findall(r"[a-z0-9]+", norm(text)) if len(w) > 1]


# ---------- Apps instaladas (las del menú Inicio) ----------
_apps = None
_apps_lock = threading.Lock()


def load_apps():
    """[(nombre, AppID)] del menú Inicio. Tarda ~2 s: se llama al iniciar, en segundo plano."""
    global _apps
    with _apps_lock:
        if _apps is None:
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                    capture_output=True, text=True, encoding="utf-8", timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                data = json.loads(out or "[]")
                _apps = [(a["Name"], a["AppID"]) for a in (data if isinstance(data, list) else [data])]
            except (OSError, ValueError, subprocess.SubprocessError):
                _apps = []
        return _apps


def find_app(name):
    n, ws = norm(name).strip(), words(name)
    best, best_score = None, 0
    for app_name, app_id in load_apps():
        a = norm(app_name)
        if a == n:
            score = 4
        elif a.startswith(n) or n.startswith(a):
            score = 3
        elif ws and all(w in a for w in ws):
            score = 2
        else:
            continue
        if score > best_score or (score == best_score and len(app_name) < len(best[0])):
            best, best_score = (app_name, app_id), score
    return best


# ---------- Ventanas ----------
def exe_of(hwnd):
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return exe_of_pid(pid.value)


def exe_of_pid(pid):
    hp = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not hp:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(520), wt.DWORD(520)
        if k32.QueryFullProcessImageNameW(ctypes.c_void_p(hp), 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0]
        return ""
    finally:
        k32.CloseHandle(ctypes.c_void_p(hp))


def top_windows():
    """[(hwnd, título, exe)] de las ventanas normales visibles (sin las de herramientas ni las ocultas)."""
    out = []

    def cb(h, _):
        if not u.IsWindowVisible(ctypes.c_void_p(h)) or u.GetWindow(ctypes.c_void_p(h), 4):  # GW_OWNER
            return True
        if u.GetWindowLongW(ctypes.c_void_p(h), -20) & 0x80:  # WS_EX_TOOLWINDOW
            return True
        cloaked = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(h), 14, ctypes.byref(cloaked), 4)
        n = u.GetWindowTextLengthW(ctypes.c_void_p(h))
        if cloaked.value or not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(ctypes.c_void_p(h), buf, n + 1)
        out.append((h, buf.value, exe_of(h)))
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def match_score(name, title, exe):
    ws = words(name)
    if not ws:
        return 0
    t, e = norm(title), norm(exe)
    if "".join(ws) in e.replace("-", "").replace("_", ""):
        return 3
    if all(w in t or w in e for w in ws):
        return 2
    return 0


def find_window(name, skip=()):
    best, best_score = None, 0
    for h, title, exe in top_windows():
        if h in skip:
            continue
        s = match_score(name, title, exe)
        if s > best_score:
            best, best_score = h, s
    return best


def wait_window(name, before, timeout=15.0, skip=()):
    """Espera la ventana de lo que se acaba de abrir (de preferencia una nueva)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        new = [(h, t, e) for h, t, e in top_windows() if h not in before and h not in skip]
        scored = sorted(((match_score(name, t, e), h) for h, t, e in new), reverse=True)
        if scored and scored[0][0] > 0:
            return scored[0][1]
        if time.time() - t0 > 3:  # ya estaba abierta: Windows solo la trae al frente
            h = find_window(name, skip)
            if h:
                return h
        time.sleep(0.3)
    return None


# ---------- Monitores ----------
def monitors():
    with getattr(mss, "MSS", mss.mss)() as sct:
        return sct.monitors[1:]


def describe_monitors():
    return ", ".join(f"{i}: {m['width']}x{m['height']}" + (" (principal)" if m.get("is_primary") else "")
                     for i, m in enumerate(monitors(), start=1))


def monitor_of(hwnd):
    r = wt.RECT()
    u.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    for i, m in enumerate(monitors(), start=1):
        if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
            return i
    return None


def pick_monitor(spec, current=None):
    mons = monitors()
    s = norm(spec).strip() if spec is not None else ""
    if s.isdigit() and 1 <= int(s) <= len(mons):
        return mons[int(s) - 1]
    primary = next((i for i, m in enumerate(mons, start=1) if m.get("is_primary")), 1)
    if s in ("principal", "primaria", "primera", "main"):
        return mons[primary - 1]
    # secundaria / segunda / otra: la primera que no sea la principal (ni donde ya está la ventana)
    for i, m in enumerate(mons, start=1):
        if i != primary and i != current:
            return m
    for i, m in enumerate(mons, start=1):
        if i != current:
            return m
    return mons[0]


def move_to_monitor(hwnd, mon, maximize=None, side=None):
    h = ctypes.c_void_p(hwnd)
    was_max = bool(u.IsZoomed(h))
    if was_max or u.IsIconic(h):
        u.ShowWindow(h, 4 if not was_max else 9)  # restaurar para poder moverla
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    center = wt.POINT(mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2)
    u.GetMonitorInfoW(ctypes.c_void_p(u.MonitorFromPoint(center, 2)), ctypes.byref(info))
    work = info.rcWork
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    ww, wh = work.right - work.left, work.bottom - work.top
    w, hh = min(r.right - r.left, ww), min(r.bottom - r.top, wh)
    x, y = work.left + (ww - w) // 2, work.top + (wh - hh) // 2
    side = norm(side or "")
    if side in ("izquierda", "derecha"):  # media pantalla
        x, y, w, hh, maximize = work.left + (ww // 2 if side == "derecha" else 0), work.top, ww // 2, wh, False
    elif side in ("arriba", "abajo"):
        x, y, w, hh, maximize = work.left, work.top + (wh // 2 if side == "abajo" else 0), ww, wh // 2, False
    u.SetWindowPos(h, None, x, y, w, hh, 0x0010 | 0x0004)  # NOACTIVATE · NOZORDER
    if maximize or (maximize is None and was_max):
        u.ShowWindow(h, 3)  # SW_MAXIMIZE


def focus(hwnd):
    if not hwnd:
        return
    u.keybd_event(MASK_KEY, 0, 0, 0)
    u.keybd_event(MASK_KEY, 0, 2, 0)
    h = ctypes.c_void_p(hwnd)
    if u.IsIconic(h):
        u.ShowWindow(h, 9)
    u.SetForegroundWindow(h)


def tap(vk, times=1):
    for _ in range(max(1, min(int(times), 25))):
        u.keybd_event(vk, 0, 0, 0)
        u.keybd_event(vk, 0, 2, 0)
        time.sleep(0.03)


# ---------- Acciones ----------
def _spotify_uri(target):
    m = re.search(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)",
                  target)
    if m:
        return f"spotify:{m[1]}:{m[2]}", m[1]
    if target.startswith("spotify:"):
        return target, target.split(":")[1] if target.count(":") >= 2 else ""
    track = spotify_track(target)  # solo con la API oficial configurada
    if track:
        return track, "track"
    # Sin API: se abre la búsqueda dentro de Spotify y se le da a reproducir al mejor resultado.
    return "spotify:search:" + urllib.parse.quote(target), "search"


def _http(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130 Safari/537.36",
        "Accept-Language": "es-CL,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


_sp_token = (None, 0.0)


def _spotify_api_track(query):
    """Con SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (app gratuita en developer.spotify.com): búsqueda oficial."""
    global _sp_token
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID", "").strip(), os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        return None
    try:
        token, expires = _sp_token
        if not token or time.time() > expires - 60:
            import base64
            req = urllib.request.Request(
                "https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
                headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode(),
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            token, _sp_token = data["access_token"], (data["access_token"], time.time() + data.get("expires_in", 3600))
        req = urllib.request.Request(
            "https://api.spotify.com/v1/search?" + urllib.parse.urlencode({"q": query, "type": "track", "limit": 1}),
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            items = json.loads(r.read())["tracks"]["items"]
        return items[0]["uri"] if items else None
    except (OSError, ValueError, KeyError):
        return None


def spotify_track(query):
    """URI de la canción con la API oficial, si SPOTIFY_CLIENT_ID/SECRET están configurados; si no, None."""
    return _spotify_api_track(query)


_uia = None


def uia():
    """UI Automation de Windows (vía pythonnet): permite apretar botones de otra app sin darle el foco."""
    global _uia
    if _uia is None:
        import clr
        for name in ("UIAutomationClient", "UIAutomationTypes"):
            clr.AddReference(f"{name}, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35")
        import System
        import System.Windows.Automation as auto
        _uia = (auto, System.IntPtr)
    return _uia


def window_title(hwnd):
    n = u.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(ctypes.c_void_p(hwnd), buf, n + 1)
    return buf.value


def spotify_playing(hwnd):
    """Lo que suena según el título de la ventana ("Artista - Canción"); "" si está en pausa ("Spotify Premium")."""
    title = window_title(hwnd)
    return "" if norm(title).startswith("spotify") or not title else title


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]

    def __init__(self, text):
        import uuid
        super().__init__()
        ctypes.memmove(self.data, uuid.UUID(text).bytes_le, 16)


def _vcall(ptr, index, *argtypes):
    """Método `index` de la vtable de un puntero COM."""
    vtable = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])


def enable_web_accessibility(hwnd):
    """Las apps Chromium (Spotify) no exponen su contenido web a UI Automation hasta que un lector de pantalla
    pide IAccessible2; se hace esa petición a cada ventana de contenido para que publiquen sus botones."""
    oleacc = ctypes.windll.oleacc
    oleacc.AccessibleObjectFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_void_p)]
    ctypes.windll.ole32.CoInitialize(None)
    iid_acc = _GUID("618736e0-3c3d-11cf-810c-00aa00389b71")
    iid_sp = _GUID("6d5140c1-7436-11ce-8034-00aa006009fa")
    iid_ia2 = _GUID("e89f726e-c4f4-4c19-bb19-b647d7fa8478")
    kids = []

    def cb(child, _):
        buf = ctypes.create_unicode_buffer(80)
        u.GetClassNameW(ctypes.c_void_p(child), buf, 80)
        if "RenderWidgetHost" in buf.value:
            kids.append(child)
        return True

    u.EnumChildWindows(ctypes.c_void_p(hwnd), WNDENUMPROC(cb), 0)
    out = ctypes.POINTER(ctypes.c_void_p)
    for child in kids:
        acc, sp, ia2 = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            if oleacc.AccessibleObjectFromWindow(child, 0xFFFFFFFC, ctypes.byref(iid_acc), ctypes.byref(acc)) or not acc:
                continue  # 0xFFFFFFFC = OBJID_CLIENT
            _vcall(acc, 0, ctypes.c_void_p, out)(acc, ctypes.byref(iid_sp), ctypes.byref(sp))  # QueryInterface
            if sp:  # IServiceProvider::QueryService
                _vcall(sp, 3, ctypes.c_void_p, ctypes.c_void_p, out)(sp, ctypes.byref(iid_acc), ctypes.byref(iid_ia2),
                                                                      ctypes.byref(ia2))
        finally:
            for ptr in (ia2, sp, acc):
                if ptr:
                    _vcall(ptr, 2)(ptr)  # Release


def raise_window(hwnd):
    """La trae delante de las demás sin darle el foco (una ventana tapada no dibuja ni expone su contenido)."""
    h = ctypes.c_void_p(hwnd)
    if u.IsIconic(h):
        u.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
    flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE · NOMOVE · NOACTIVATE
    u.SetWindowPos(h, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST
    u.SetWindowPos(h, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)  # HWND_NOTOPMOST: queda arriba, pero normal


_LIB_STOP = set("mi mis la el los las de del en lista listas playlist playlists spotify tu tus".split())


def _spotify_ui(hwnd):
    """Raíz de UI Automation de Spotify, con la ventana a la vista y su contenido web publicado."""
    auto, IntPtr = uia()
    raise_window(hwnd)
    enable_web_accessibility(hwnd)
    by_type = lambda t: auto.PropertyCondition(auto.AutomationElement.ControlTypeProperty, t)
    return auto, auto.AutomationElement.FromHandle(IntPtr(hwnd)), by_type


def spotify_play_library(hwnd, name, timeout=8.0):
    """Reproduce una playlist/álbum de "Tu biblioteca" (el panel izquierdo) cuyo nombre calce con `name`.

    Abre el elemento desde la biblioteca y le da a reproducir en su página. Devuelve (título elegido o "",
    títulos disponibles, si quedó sonando).
    """
    want = [w for w in words(name) if w not in _LIB_STOP] or words(name)
    t0, titles = time.time(), []
    while time.time() - t0 < timeout:
        try:
            auto, win, by_type = _spotify_ui(hwnd)
            grids = win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.DataGrid))
            if grids.Count:  # la biblioteca es la primera tabla de la ventana
                grid = grids[0]
                titles = list(dict.fromkeys(g.Current.Name for g in grid.FindAll(
                    auto.TreeScope.Descendants, by_type(auto.ControlType.Group)) if g.Current.Name))
                if titles:
                    scored = sorted(((sum(w in norm(t) for w in want), -len(t), t) for t in titles), reverse=True)
                    if scored[0][0] == 0:
                        return "", titles, False
                    best = scored[0][2]
                    for button in grid.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Button)):
                        if button.Current.Name.startswith(best):
                            button.GetCurrentPattern(auto.InvokePattern.Pattern).Invoke()  # abre su página
                            return best, titles, spotify_play_top(hwnd, best, 10)
        except Exception:  # noqa: BLE001  (la ventana aún está cargando)
            pass
        time.sleep(0.4)
    return "", titles, False


def spotify_volume(hwnd, change, timeout=6.0):
    """Mueve el control de volumen del propio Spotify (no el del sistema). `change(actual 0-100) -> nuevo`."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            auto, win, by_type = _spotify_ui(hwnd)
            for slider in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Slider)):
                rv = slider.GetCurrentPattern(auto.RangeValuePattern.Pattern)
                if rv.Current.Maximum == 1.0 and rv.Current.Minimum == 0.0:  # el de volumen va de 0 a 1
                    new = max(0.0, min(100.0, change(rv.Current.Value * 100)))
                    rv.SetValue(new / 100)
                    return round(new)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return None


def system_volume(change=None, mute=None, flow=0, role=1):
    """Volumen maestro de Windows con la API de audio (exacto, no a golpes de tecla). Devuelve (nivel, mudo)."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
    out = ctypes.POINTER(ctypes.c_void_p)
    enum, dev, vol = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(ctypes.byref(_GUID("bcde0395-e52f-467c-8e3d-c4579291692e")), None, 23,
                                    ctypes.byref(_GUID("a95664d2-9614-4f35-a746-de8db63617e6")), ctypes.byref(enum))
        if hr or not enum:
            raise OSError("no pude acceder al audio de Windows")
        _vcall(enum, 4, ctypes.c_int, ctypes.c_int, out)(enum, flow, role, ctypes.byref(dev))  # flow 0 = salida, 1 = micrófono
        if not dev:
            raise OSError("no hay dispositivo de salida de audio")
        _vcall(dev, 3, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, out)(  # Activate(IAudioEndpointVolume)
            dev, ctypes.byref(_GUID("5cdf2c82-841e-4546-9722-0cf74078229a")), 23, None, ctypes.byref(vol))
        if not vol:
            raise OSError("no pude acceder al volumen de Windows")
        level, muted = ctypes.c_float(), ctypes.c_int()
        _vcall(vol, 9, ctypes.POINTER(ctypes.c_float))(vol, ctypes.byref(level))
        _vcall(vol, 15, ctypes.POINTER(ctypes.c_int))(vol, ctypes.byref(muted))
        now, is_muted = level.value * 100, bool(muted.value)
        if change is not None:
            now = max(0.0, min(100.0, change(now)))
            _vcall(vol, 7, ctypes.c_float, ctypes.c_void_p)(vol, ctypes.c_float(now / 100), None)
            if mute is None and now > 0:
                mute = False  # subir o fijar el volumen quita el silencio
        if mute is not None and mute != is_muted:
            _vcall(vol, 14, ctypes.c_int, ctypes.c_void_p)(vol, int(mute), None)
            is_muted = mute
        return round(now), is_muted
    finally:
        for ptr in (vol, dev, enum):
            if ptr:
                _vcall(ptr, 2)(ptr)


def app_volume(is_app, change=None, mute=None):
    """Volumen de una app en el mezclador de Windows (lo mismo que su barra en "Mezclador de volumen").

    `is_app(exe)` elige las sesiones de audio. Devuelve (nivel, mudo, [exes]) o None si esa app no tiene audio.
    """
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
    out = ctypes.POINTER(ctypes.c_void_p)
    iid_ctl2 = _GUID("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")
    iid_vol = _GUID("87ce5498-68d6-44e5-9215-6da47ef883d8")
    enum, dev, mgr, sessions = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    result, exes = None, []
    try:
        ole32.CoCreateInstance(ctypes.byref(_GUID("bcde0395-e52f-467c-8e3d-c4579291692e")), None, 23,
                               ctypes.byref(_GUID("a95664d2-9614-4f35-a746-de8db63617e6")), ctypes.byref(enum))
        if not enum:
            raise OSError("no pude acceder al audio de Windows")
        _vcall(enum, 4, ctypes.c_int, ctypes.c_int, out)(enum, 0, 1, ctypes.byref(dev))
        if not dev:
            raise OSError("no hay dispositivo de salida de audio")
        _vcall(dev, 3, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, out)(  # Activate(IAudioSessionManager2)
            dev, ctypes.byref(_GUID("77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f")), 23, None, ctypes.byref(mgr))
        if not mgr:
            raise OSError("no pude acceder al mezclador de Windows")
        _vcall(mgr, 5, out)(mgr, ctypes.byref(sessions))  # GetSessionEnumerator
        count = ctypes.c_int()
        _vcall(sessions, 3, ctypes.POINTER(ctypes.c_int))(sessions, ctypes.byref(count))
        for i in range(count.value):
            ctl, ctl2, vol = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            try:
                _vcall(sessions, 4, ctypes.c_int, out)(sessions, i, ctypes.byref(ctl))
                if not ctl:
                    continue
                _vcall(ctl, 0, ctypes.c_void_p, out)(ctl, ctypes.byref(iid_ctl2), ctypes.byref(ctl2))
                _vcall(ctl, 0, ctypes.c_void_p, out)(ctl, ctypes.byref(iid_vol), ctypes.byref(vol))
                if not (ctl2 and vol):
                    continue
                pid = wt.DWORD()
                _vcall(ctl2, 14, ctypes.POINTER(wt.DWORD))(ctl2, ctypes.byref(pid))  # GetProcessId
                exe = exe_of_pid(pid.value) if pid.value else ""
                if not exe or not is_app(norm(exe)):
                    continue
                level, muted = ctypes.c_float(), ctypes.c_int()
                _vcall(vol, 4, ctypes.POINTER(ctypes.c_float))(vol, ctypes.byref(level))
                _vcall(vol, 6, ctypes.POINTER(ctypes.c_int))(vol, ctypes.byref(muted))
                now, is_muted, set_mute = level.value * 100, bool(muted.value), mute
                if change is not None:
                    now = max(0.0, min(100.0, change(now)))
                    _vcall(vol, 3, ctypes.c_float, ctypes.c_void_p)(vol, ctypes.c_float(now / 100), None)
                    if set_mute is None and now > 0:
                        set_mute = False
                if set_mute is not None and set_mute != is_muted:
                    _vcall(vol, 5, ctypes.c_int, ctypes.c_void_p)(vol, int(set_mute), None)
                    is_muted = set_mute
                result = (round(now), is_muted)
                if exe not in exes:
                    exes.append(exe)
            finally:
                for ptr in (vol, ctl2, ctl):
                    if ptr:
                        _vcall(ptr, 2)(ptr)
        return (*result, exes) if result else None
    finally:
        for ptr in (sessions, mgr, dev, enum):
            if ptr:
                _vcall(ptr, 2)(ptr)


def _youtube_page(timeout=4.0):
    """Pestaña de YouTube (de preferencia la que suena), ya activa en su ventana. Devuelve (hwnd, find) o None;
    find(clase, tipo) busca un control del reproductor por su clase CSS."""
    auto, IntPtr = uia()
    by_type = lambda t: auto.PropertyCondition(auto.AutomationElement.ControlTypeProperty, t)
    found = None
    for hwnd, _, exe in top_windows():
        if norm(exe) not in BROWSERS:
            continue
        try:
            win = auto.AutomationElement.FromHandle(IntPtr(hwnd))
            for tab in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.TabItem)):
                if tab.Current.ClassName == "Tab" and "youtube" in norm(tab.Current.Name):
                    playing = any(b.Current.ClassName == "AlertIndicatorButton" for b in
                                  tab.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Button)))
                    if playing or found is None:
                        found = (hwnd, tab)
                    if playing:
                        break
        except Exception:  # noqa: BLE001
            pass
    if not found:
        # En pantalla completa el navegador no muestra pestañas: se usa la ventana cuyo título es de YouTube.
        found = next(((hwnd, None) for hwnd, title, exe in top_windows()
                      if norm(exe) in BROWSERS and "youtube" in norm(title)), None)
    if not found:
        return None
    hwnd, tab = found
    try:
        selection = tab.GetCurrentPattern(auto.SelectionItemPattern.Pattern) if tab is not None else None
        if selection is not None and not selection.Current.IsSelected:  # solo la pestaña activa expone su contenido
            selection.Select()
            time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass

    def find(css_class, control_type):
        win = auto.AutomationElement.FromHandle(IntPtr(hwnd))
        for el in win.FindAll(auto.TreeScope.Descendants, by_type(control_type)):
            if css_class in el.Current.ClassName:
                return el
        return None

    t0 = time.time()
    while time.time() - t0 < timeout:  # esperar a que la página publique el reproductor
        try:
            enable_web_accessibility(hwnd)
            if find("ytp-progress-bar", auto.ControlType.Slider) is not None:
                return hwnd, find
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return None


def _youtube_keys(hwnd, element, presses):
    """Enfoca un control del reproductor y le manda teclas [(tecla, veces)]. El navegador pasa un instante al
    frente y el foco vuelve a donde estaba. Devuelve False (sin teclear) si el foco no quedó en el reproductor."""
    auto, _ = uia()
    previous = u.GetForegroundWindow()
    try:
        focus(hwnd)
        time.sleep(0.2)
        element.SetFocus()
        time.sleep(0.2)
        focused = auto.AutomationElement.FocusedElement.Current.ClassName
        if "ytp" not in focused and "html5-video-player" not in focused:
            return False
        for vk, times in presses:
            for _ in range(max(0, min(int(times), 60))):
                combo(*vk) if isinstance(vk, tuple) else tap(vk)
                time.sleep(0.03)
        time.sleep(0.5)
        return True
    finally:
        if previous and previous != hwnd:
            focus(previous)


def youtube_volume(change):
    """Volumen del reproductor de YouTube (el control de la página). Devuelve el nivel final o None.
    La página no acepta que se le fije el valor desde fuera: se usan las flechas (5 puntos cada una)."""
    auto, _ = uia()
    page = _youtube_page()
    if not page:
        return None
    hwnd, find = page
    value = lambda el: el.GetCurrentPattern(auto.RangeValuePattern.Pattern).Current.Value
    el = find("ytp-volume-panel", auto.ControlType.Slider)
    if el is None:
        return None
    current = value(el)
    target = max(0.0, min(100.0, change(current)))
    steps = round((target - current) / 5)
    if steps == 0:
        return round(current)
    if not _youtube_keys(hwnd, el, [(0x26 if steps > 0 else 0x28, abs(steps))]):  # flecha arriba / abajo
        return None
    el = find("ytp-volume-panel", auto.ControlType.Slider)
    return round(value(el)) if el is not None else round(target)


def youtube_seek(position):
    """Mueve la reproducción de YouTube. `position(actual, duración) -> segundos`. Devuelve (posición, duración).
    Teclas del reproductor: 0-9 saltan al 0-90 %, L avanza 10 s y las flechas 5 s."""
    auto, _ = uia()
    page = _youtube_page()
    if not page:
        return None
    hwnd, find = page
    rng = lambda el: el.GetCurrentPattern(auto.RangeValuePattern.Pattern).Current
    bar = find("ytp-progress-bar", auto.ControlType.Slider)
    current, duration = rng(bar).Value, rng(bar).Maximum
    if duration <= 0:
        return None
    target = max(0.0, min(duration - 1, position(current, duration)))
    delta = target - current
    if abs(delta) <= 90:  # cerca: solo saltos de 10 s (J/L) y de 5 s (flechas)
        presses = [(0x4C if delta > 0 else 0x4A, int(abs(delta) // 10)),
                   (0x27 if delta > 0 else 0x25, round((abs(delta) % 10) / 5))]
    else:  # lejos: salto al 10 % anterior y de ahí hacia adelante
        tenth = min(9, int(target / duration * 10))
        rest = target - tenth * duration / 10
        presses = [(0x30 + tenth, 1), (0x4C, min(60, int(rest // 10))), (0x27, round((rest % 10) / 5))]
    if not _youtube_keys(hwnd, bar, presses):
        return None
    bar = find("ytp-progress-bar", auto.ControlType.Slider)
    return (rng(bar).Value if bar is not None else target), duration


def youtube_set_playing(play):
    """Pausa o reanuda el video con el botón del reproductor (sin darle el foco). None si no hay YouTube."""
    auto, _ = uia()
    page = _youtube_page()
    if not page:
        return None
    _, find = page
    button = find("ytp-play-button", auto.ControlType.Button)
    if button is None:
        return None
    if norm(button.Current.Name).startswith("paus") != play:  # el botón dice "Pausa" cuando está sonando
        button.GetCurrentPattern(auto.InvokePattern.Pattern).Invoke()
        time.sleep(0.6)
    return True


def spotify_seek(hwnd, position):
    """Mueve la reproducción de Spotify con su barra de progreso (en milisegundos). Devuelve (posición, duración)."""
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            auto, win, by_type = _spotify_ui(hwnd)
            for slider in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Slider)):
                rv = slider.GetCurrentPattern(auto.RangeValuePattern.Pattern)
                if rv.Current.Maximum > 1000:  # volumen: 0-1, ancho del panel: cientos, progreso: milisegundos
                    duration = rv.Current.Maximum / 1000
                    target = max(0.0, min(duration - 1, position(rv.Current.Value / 1000, duration)))
                    rv.SetValue(target * 1000)
                    return target, duration
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return None


def clock(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


# ---------- Cerrar programas ----------
PROTECTED = set("""system registry smss csrss wininit winlogon services lsass svchost dwm fontdrvhost explorer sihost
    ctfmon taskhostw runtimebroker searchhost startmenuexperiencehost shellexperiencehost applicationframehost
    msmpeng securityhealthservice conhost python pythonw claude audiodg spoolsv memcompression""".split())


def process_names():
    """Nombres (sin .exe) de los procesos en ejecución."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.split('","')[0].strip('"')[:-4] for line in out.splitlines() if ".exe" in line.lower()})


def close_app(name, force=False, skip=()):
    """Cierra un programa: normal (como la X, puede pedir guardar) o a la fuerza (finalizar tarea)."""
    windows = [(h, exe) for h, title, exe in top_windows() if h not in skip and match_score(name, title, exe)]
    exes = {exe for _, exe in windows if exe}
    if not exes:  # sin ventana (en segundo plano): se busca por nombre de proceso
        key = "".join(words(name))
        exes = {p for p in process_names() if key and key in norm(p).replace(" ", "").replace("-", "")}
    if not exes:
        return f"No encontré {name} abierto."
    label = ", ".join(sorted(exes))
    if force:
        # Finalizar la tarea mata el proceso entero: nunca los de Windows ni esta misma app.
        if any(norm(e) in PROTECTED for e in exes):
            return f"{label} es un proceso de Windows o de este asistente: no lo finalizo a la fuerza."
        failed = [e for e in exes if subprocess.run(
            ["taskkill", "/F", "/T", "/IM", f"{e}.exe"], capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode != 0]
        if failed:
            return f"No pude finalizar {', '.join(failed)} (puede necesitar permisos de administrador)."
        INFO.append(f"Finalicé a la fuerza: {label}.")
        return ""
    if not windows:
        return f"{label} no tiene ventana que cerrar; pide forzarlo para finalizar la tarea."
    for h, _ in windows:
        u.PostMessageW(ctypes.c_void_p(h), 0x0010, 0, 0)  # WM_CLOSE
    t0 = time.time()
    while time.time() - t0 < 4:
        time.sleep(0.4)
        if not any(h for h, _ in windows if u.IsWindow(ctypes.c_void_p(h)) and u.IsWindowVisible(ctypes.c_void_p(h))):
            INFO.append(f"Cerré {label}.")
            return ""
    return f"Le pedí a {label} que cierre, pero sigue abierto (puede estar preguntando si guardar o quedarse en la " \
           f"bandeja). Si el usuario lo pide, fuérzalo."


# ---------- Temporizadores ----------
NOTIFY = None   # lo pone la app: NOTIFY(mensaje) lo muestra y lo dice en voz alta
_timers = []


def set_timer(seconds, message, every=0, times=0):
    """Aviso en `seconds`; con `every` (segundos) se repite, `times` veces o hasta cancelarlo."""
    entry = {"label": message, "left": int(times) or None}

    def schedule(delay):
        entry["at"] = time.time() + delay
        entry["timer"] = threading.Timer(delay, fire)
        entry["timer"].daemon = True
        entry["timer"].start()

    def fire():
        if entry not in _timers:
            return
        if NOTIFY:
            NOTIFY(message)
        if entry["left"] is not None:
            entry["left"] -= 1
        if every and (entry["left"] is None or entry["left"] > 0):
            schedule(every)
        else:
            _timers.remove(entry)

    _timers.append(entry)
    schedule(seconds)


def combo(*vks):
    """Combinación de teclas: se aprietan en orden y se sueltan al revés (p. ej. Ctrl+V)."""
    for vk in vks:
        u.keybd_event(vk, 0, 0, 0)
        time.sleep(0.04)
    time.sleep(0.06)
    for vk in reversed(vks):
        u.keybd_event(vk, 0, 2, 0)
        time.sleep(0.02)


def char_key(char):
    """Teclas que producen ese carácter en la distribución de teclado actual (p. ej. ">" = Shift + tecla)."""
    u.VkKeyScanW.restype = ctypes.c_short
    code = u.VkKeyScanW(ord(char))
    return ((0x10,) if code >> 8 & 1 else ()) + (code & 0xFF,)


# ---------- Clips de video (Xbox Game Bar) ----------
_GAMEDVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"


def clips_enabled():
    """¿Está activa la grabación en segundo plano de Game Bar (la que permite guardar "lo último que pasó")?"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GAMEDVR) as key:
            return bool(winreg.QueryValueEx(key, "HistoricalCaptureEnabled")[0])
    except OSError:
        return False


def set_clips(enabled):
    """La activa o apaga al instante. Apagada no gasta nada; activa, graba solo mientras un juego está al frente."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GAMEDVR, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "HistoricalCaptureEnabled", 0, winreg.REG_DWORD, int(enabled))
        if enabled:
            winreg.SetValueEx(key, "AppCaptureEnabled", 0, winreg.REG_DWORD, 1)


def clips_seconds():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GAMEDVR) as key:
            return int(winreg.QueryValueEx(key, "HistoricalBufferLength")[0])
    except (OSError, ValueError):
        return 30


def clips_folder():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            custom = os.path.expandvars(winreg.QueryValueEx(key, "{EDC0FE71-98D8-4F4A-B920-C8DC133CB165}")[0])
            if custom and os.path.isdir(custom):
                return custom
    except OSError:
        pass
    return os.path.join(os.path.expanduser("~"), "Videos", "Captures")


def _clip_files():
    folder, files = clips_folder(), {}
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(".mp4"):
                files[name] = os.path.getsize(os.path.join(folder, name))
    except OSError:
        pass
    return files


def clip_recording():
    """Nombre del video que Game Bar está grabando ahora, o "". Es un .mp4 que crece de forma sostenida: un clip
    recién guardado también crece un instante mientras se termina de escribir, por eso se mide dos veces."""
    first = _clip_files()
    time.sleep(1.0)
    second = _clip_files()
    time.sleep(1.0)
    third = _clip_files()
    return next((name for name, size in third.items()
                 if name in second and size > second[name] and second[name] > first.get(name, -1)), "")


def _game_to_front(act):
    game = (act.get("_game") or (None,))[0]
    if game and u.GetForegroundWindow() != game:
        focus(game)  # Game Bar graba la ventana que está al frente
        time.sleep(0.4)


# ---------- Portapapeles ----------
def _clipboard_open():
    for _ in range(10):
        if u.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def clipboard_get_text():
    u.GetClipboardData.restype = ctypes.c_void_p
    k32.GlobalLock.restype, k32.GlobalLock.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not _clipboard_open():
        return None
    try:
        handle = u.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        ptr = k32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr) if ptr else None
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u.CloseClipboard()


def clipboard_set(fmt, data):
    """Pone `data` (bytes) en el portapapeles: fmt 13 = texto UTF-16 terminado en nulo, 8 = imagen (DIB)."""
    k32.GlobalAlloc.restype, k32.GlobalAlloc.argtypes = ctypes.c_void_p, [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype, k32.GlobalLock.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.restype, u.SetClipboardData.argtypes = ctypes.c_void_p, [ctypes.c_uint, ctypes.c_void_p]
    if not _clipboard_open():
        return False
    try:
        u.EmptyClipboard()
        handle = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ptr = k32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(handle)
        return bool(u.SetClipboardData(fmt, handle))
    finally:
        u.CloseClipboard()


def clipboard_set_text(text):
    return clipboard_set(13, (text + "\0").encode("utf-16-le"))


# ---------- Estado del PC ----------
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                                                "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


class _POWER(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def _cpu_percent(interval=0.4):
    def snapshot():
        idle, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        value = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return value(idle), value(kernel) + value(user)
    idle1, total1 = snapshot()
    time.sleep(interval)
    idle2, total2 = snapshot()
    return 100 * (1 - (idle2 - idle1) / max(1, total2 - total1))


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                             "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                             "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                             "PrivateUsage")]


def _top_processes(interval=0.5):
    """([(nombre, % CPU)], [(nombre, GB)]) de lo que más consume ahora, sumando los procesos de cada programa."""
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    entry, handles = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W)), []
    more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
    while more:
        handle = k32.OpenProcess(0x1000, False, entry.th32ProcessID) if entry.th32ProcessID else None
        if handle:
            handles.append((os.path.splitext(entry.szExeFile)[0], handle))
        more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    k32.CloseHandle(ctypes.c_void_p(snapshot))

    def cpu_time(handle):
        times = [wt.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in times[2:])  # kernel + usuario

    try:
        before = {handle: cpu_time(handle) for _, handle in handles}
        t0 = time.time()
        time.sleep(interval)
        elapsed, cores = (time.time() - t0) * 1e7, os.cpu_count() or 1
        cpu, mem = {}, {}
        for name, handle in handles:
            now, counters = cpu_time(handle), _PROCESS_MEMORY_COUNTERS_EX(cb=ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX))
            if now is not None and before[handle] is not None:
                cpu[name] = cpu.get(name, 0) + (now - before[handle]) / elapsed / cores * 100
            if k32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                mem[name] = mem.get(name, 0) + counters.PrivateUsage / 2**30
    finally:
        for _, handle in handles:
            k32.CloseHandle(ctypes.c_void_p(handle))
    top = lambda d, floor: [(n, v) for n, v in sorted(d.items(), key=lambda kv: -kv[1])[:3] if v >= floor]
    return top(cpu, 1), top(mem, 0.3)


def pc_status():
    """Frase (para leer en voz alta) con hora, batería, CPU, memoria, gráfica y lo que más consume."""
    num = lambda v, d=0: f"{v:.{d}f}".replace(".", ",")
    parts = [f"Son las {time.strftime('%H:%M')}."]
    power = _POWER()
    if k32.GetSystemPowerStatus(ctypes.byref(power)) and power.BatteryFlag != 128 and power.BatteryLifePercent <= 100:
        parts.append(f"Batería al {power.BatteryLifePercent} %" + (", cargando." if power.ACLineStatus == 1 else "."))
    memory = _MEMORYSTATUSEX(dwLength=ctypes.sizeof(_MEMORYSTATUSEX))
    k32.GlobalMemoryStatusEx(ctypes.byref(memory))
    used, total = (memory.ullTotalPhys - memory.ullAvailPhys) / 2**30, memory.ullTotalPhys / 2**30
    usage = f"Procesador al {num(_cpu_percent())} %, memoria al {memory.dwMemoryLoad} % ({num(used)} de {num(total)} GB)"
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().splitlines()[0].split(", ")
        usage += f", gráfica al {gpu[0]} % a {gpu[1]} grados con {num(int(gpu[2]) / 1024, 1)} de " \
                 f"{num(int(gpu[3]) / 1024)} GB de video"
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        pass
    parts.append(usage + ".")
    cpu, mem = _top_processes()
    if cpu:
        parts.append("Lo que más usa el procesador: " + ", ".join(f"{n} {num(v)} %" for n, v in cpu) + ".")
    if mem:
        parts.append("Lo que más usa memoria: " + ", ".join(f"{n} {num(v, 1)} GB" for n, v in mem) + ".")
    return " ".join(parts)


# ---------- Dispositivos de audio ----------
class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", wt.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


def _com_create(clsid, iid):
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
    ptr = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_GUID(clsid)), None, 23, ctypes.byref(_GUID(iid)), ctypes.byref(ptr))
    if not ptr:
        raise OSError("no pude acceder al audio de Windows")
    return ptr


def _device_id(dev):
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    raw = ctypes.c_void_p()
    _vcall(dev, 5, ctypes.POINTER(ctypes.c_void_p))(dev, ctypes.byref(raw))  # IMMDevice::GetId
    if not raw:
        return ""
    try:
        return ctypes.wstring_at(raw.value)
    finally:
        ole32.CoTaskMemFree(raw)


def _device_name(dev):
    ole32 = ctypes.windll.ole32
    store, value = ctypes.c_void_p(), _PROPVARIANT()
    _vcall(dev, 4, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(dev, 0, ctypes.byref(store))  # OpenPropertyStore
    if not store:
        return ""
    try:
        key = _PROPERTYKEY()  # PKEY_Device_FriendlyName
        ctypes.memmove(key.fmtid, _GUID("a45c254e-df1c-4efd-8020-67d146a850e0").data, 16)
        key.pid = 14
        _vcall(store, 5, ctypes.c_void_p, ctypes.c_void_p)(store, ctypes.byref(key), ctypes.byref(value))
        name = ctypes.wstring_at(value.ptr) if value.vt == 31 and value.ptr else ""
        ole32.PropVariantClear(ctypes.byref(value))
        return name
    finally:
        _vcall(store, 2)(store)


def audio_devices(flow=0):
    """[(id, nombre, es_el_predeterminado)] de las salidas (flow 0) o micrófonos (flow 1) activos."""
    out = ctypes.POINTER(ctypes.c_void_p)
    enum = _com_create("bcde0395-e52f-467c-8e3d-c4579291692e", "a95664d2-9614-4f35-a746-de8db63617e6")
    coll, default, devices = ctypes.c_void_p(), ctypes.c_void_p(), []
    try:
        _vcall(enum, 4, ctypes.c_int, ctypes.c_int, out)(enum, flow, 1, ctypes.byref(default))
        default_id = _device_id(default) if default else ""
        _vcall(enum, 3, ctypes.c_int, ctypes.c_ulong, out)(enum, flow, 1, ctypes.byref(coll))  # solo los activos
        count = ctypes.c_uint()
        if coll:
            _vcall(coll, 3, ctypes.POINTER(ctypes.c_uint))(coll, ctypes.byref(count))
        for index in range(count.value):
            dev = ctypes.c_void_p()
            _vcall(coll, 4, ctypes.c_uint, out)(coll, index, ctypes.byref(dev))
            if dev:
                try:
                    dev_id = _device_id(dev)
                    devices.append((dev_id, _device_name(dev), dev_id == default_id))
                finally:
                    _vcall(dev, 2)(dev)
        return devices
    finally:
        for ptr in (coll, default, enum):
            if ptr:
                _vcall(ptr, 2)(ptr)


def set_default_audio(device_id):
    """Deja el dispositivo como predeterminado (para todo: multimedia, juegos y comunicaciones)."""
    policy = _com_create("870af99c-171d-4f9e-af0d-e63df40c2bc9", "f8679f50-850a-41cf-9c72-430f290290c8")
    try:
        ok = True
        for role in (0, 1, 2):  # IPolicyConfig::SetDefaultEndpoint
            ok = _vcall(policy, 13, ctypes.c_wchar_p, ctypes.c_int)(policy, device_id, role) == 0 and ok
        return ok
    finally:
        _vcall(policy, 2)(policy)


AUDIO_WORDS = [{"audifonos", "auriculares", "headphones", "headset", "cascos", "earphone", "earbuds", "buds"},
               {"parlantes", "altavoces", "speakers", "speaker", "bocinas", "parlante"},
               {"monitor", "pantalla", "hdmi", "displayport", "tv", "tele"},
               {"microfono", "microphone", "mic", "micro"}]


def pick_audio_device(devices, query):
    """Dispositivo que calza con lo dicho ("audífonos", "los parlantes", "el otro"); None si ninguno."""
    wanted = [w for w in words(query) if w not in ("los", "las", "el", "la", "mis", "mi", "de", "del", "a")]
    if any(w in ("otro", "otra", "siguiente", "cambia") for w in wanted) or not wanted:
        current = next((i for i, d in enumerate(devices) if d[2]), -1)
        return devices[(current + 1) % len(devices)] if len(devices) > 1 else None
    def score(name):
        n = norm(name)
        total = 0
        for w in wanted:
            group = next((g for g in AUDIO_WORDS if w in g), {w})
            total += any(alt in n for alt in group)
        return total
    best = max(devices, key=lambda d: score(d[1]), default=None)
    return best if best and score(best[1]) else None


# ---------- Discord y Spotify: botones ----------
def discord_toggle(names, state):
    """Aprieta un interruptor de Discord (Silenciar / Ensordecer). `state`: True, False o None = alternar.
    Devuelve el estado final, o None si no se encontró la ventana o el botón."""
    hwnd = find_window("discord")
    if not hwnd:
        return None
    auto, IntPtr = uia()
    by_type = lambda t: auto.PropertyCondition(auto.AutomationElement.ControlTypeProperty, t)
    for attempt in range(6):
        try:
            if attempt >= 2:
                raise_window(hwnd)  # solo si hace falta: tapada no publica sus botones
            enable_web_accessibility(hwnd)
            win = auto.AutomationElement.FromHandle(IntPtr(hwnd))
            for button in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Button)):
                if norm(button.Current.Name) in names:
                    toggle = button.GetCurrentPattern(auto.TogglePattern.Pattern)
                    is_on = toggle.Current.ToggleState == auto.ToggleState.On
                    if state is None or state != is_on:
                        toggle.Toggle()
                        time.sleep(0.4)
                        is_on = toggle.Current.ToggleState == auto.ToggleState.On
                    return is_on
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return None


def spotify_control(hwnd, control, playlist=""):
    """Botones de la barra de Spotify: siguiente, anterior, aleatorio, repetir, me_gusta, agregar_a_lista.
    Devuelve un texto con lo que pasó, o None si no encontró el control."""
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            auto, win, by_type = _spotify_ui(hwnd)
            buttons = list(win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Button)))
            named = lambda *keys, exact=False: next(
                (b for b in buttons if (norm(b.Current.Name) in keys if exact else
                                        any(k in norm(b.Current.Name) for k in keys))), None)
            def click(element):  # un mismo botón puede publicarse como "invocar" o como "desplegar"
                for pattern, method in ((auto.InvokePattern.Pattern, "Invoke"),
                                        (auto.ExpandCollapsePattern.Pattern, "Expand"),
                                        (auto.TogglePattern.Pattern, "Toggle")):
                    try:
                        getattr(element.GetCurrentPattern(pattern), method)()
                        return
                    except Exception:  # noqa: BLE001
                        continue
                raise RuntimeError("no se pudo apretar el botón")
            if control in ("siguiente", "anterior"):
                button = named("siguiente", "next") if control == "siguiente" else named("anterior", "previous")
                if button is not None:
                    click(button)
                    time.sleep(1.2)
                    return f"Spotify: {spotify_playing(hwnd) or 'en pausa'}."
            elif control == "aleatorio":
                # El botón dice lo que haría el próximo clic y pasa por tres estados: apagado -> aleatorio ->
                # aleatorio inteligente -> apagado.
                def shuffle_state():
                    current = next((norm(b.Current.Name) for b in _spotify_ui(hwnd)[1].FindAll(
                        auto.TreeScope.Descendants, by_type(auto.ControlType.Button))
                        if any(k in norm(b.Current.Name) for k in ("aleatorio", "shuffle"))), None)
                    if current is None:
                        return None
                    smart = "inteligente" in current or "smart" in current
                    if smart and current.split(" ")[0] in ("desactivar", "disable"):
                        return "inteligente"
                    return "normal" if smart else "apagado"
                start = shuffle_state()
                if start is not None:
                    want_on = (start == "apagado") if playlist == "" else playlist == "si"
                    for _ in range(3):
                        state = shuffle_state()
                        if state is None or (state != "apagado") == want_on:
                            break
                        click(named("aleatorio", "shuffle"))
                        time.sleep(0.7)
                        buttons = list(_spotify_ui(hwnd)[1].FindAll(auto.TreeScope.Descendants,
                                                                    by_type(auto.ControlType.Button)))
                    state = shuffle_state()
                    return "Spotify: modo aleatorio " + ("desactivado." if state == "apagado" else "activado.")
            elif control == "repetir":
                for box in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.CheckBox)):
                    if any(k in norm(box.Current.Name) for k in ("repetir", "repeticion", "repeat")):
                        box.GetCurrentPattern(auto.TogglePattern.Pattern).Toggle()
                        time.sleep(0.6)
                        after = next((norm(b.Current.Name) for b in _spotify_ui(hwnd)[1].FindAll(
                            auto.TreeScope.Descendants, by_type(auto.ControlType.CheckBox))
                            if any(k in norm(b.Current.Name) for k in ("repetir", "repeticion", "repeat"))), "")
                        # También dice lo que haría el próximo clic: repetir (apagado) -> una canción -> desactivar.
                        mode = "repitiendo esta canción" if after.split(" ")[0] in ("desactivar", "disable") else                             "repitiendo la lista" if ("una" in after or "one" in after) else "sin repetir"
                        return f"Spotify: {mode}."
            elif control in ("me_gusta", "agregar_a_lista"):
                # Solo los botones de la barra de abajo (la canción que suena): las listas tienen los suyos.
                def bar_button(*keys):
                    for group in _spotify_ui(hwnd)[1].FindAll(auto.TreeScope.Descendants,
                                                              by_type(auto.ControlType.Group)):
                        if norm(group.Current.Name).startswith(("barra estas escuchando", "now playing bar")):
                            return next((b for b in group.FindAll(auto.TreeScope.Descendants,
                                                                  by_type(auto.ControlType.Button))
                                         if norm(b.Current.Name) in keys), None)
                    return None
                like = bar_button("agregar a tus me gusta", "add to liked songs")
                add = bar_button("agregar a playlist", "add to playlist")
                if control == "me_gusta" and like is not None:
                    click(like)
                    return f"Agregué a Tus me gusta: {spotify_playing(hwnd) or 'la canción actual'}."
                if control == "me_gusta":  # ya está en alguna playlist: se marca Tus me gusta en el diálogo
                    playlist = "me gusta"
                if like is not None or add is not None:
                    was_liked = like is None  # (o ya estaba guardada en otra playlist)
                    if like is not None:  # el primer clic la marca como me gusta; el botón pasa a "Agregar a playlist"
                        click(like)
                        time.sleep(0.8)
                        add = bar_button("agregar a playlist", "add to playlist")
                    click(add)
                    time.sleep(1.0)
                    auto, win, by_type = _spotify_ui(hwnd)
                    boxes = list(win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.CheckBox)))
                    want = [w for w in words(playlist) if w not in _LIB_STOP] or words(playlist)
                    scored = sorted(((sum(w in norm(b.Current.Name) for w in want), -len(b.Current.Name), i)
                                     for i, b in enumerate(boxes)
                                     if b.Current.Name and norm(b.Current.Name) != "auto" and not any(
                                         k in norm(b.Current.Name) for k in ("repetir", "repeticion", "repeat"))),
                                    reverse=True)
                    dialog_buttons = lambda: list(_spotify_ui(hwnd)[1].FindAll(
                        auto.TreeScope.Descendants, by_type(auto.ControlType.Button)))
                    press = lambda *keys: next((click(b) or True for b in dialog_buttons()
                                                if norm(b.Current.Name) in keys), False)
                    if not scored or scored[0][0] == 0:
                        names = [boxes[i].Current.Name for _, _, i in sorted(scored, key=lambda item: item[2])][:15]
                        press("cancelar", "cancel")
                        return f"FALLÓ: no tienes una playlist {playlist}. Tienes: {', '.join(names)}."
                    box = boxes[scored[0][2]]
                    name = box.Current.Name
                    toggle = box.GetCurrentPattern(auto.TogglePattern.Pattern)
                    already = toggle.Current.ToggleState == auto.ToggleState.On
                    if not already:
                        toggle.Toggle()
                    if not was_liked:  # no dejarla en Tus me gusta si no lo estaba
                        for other in boxes:
                            if norm(other.Current.Name) in ("tus me gusta", "liked songs"):
                                other_toggle = other.GetCurrentPattern(auto.TogglePattern.Pattern)
                                if other_toggle.Current.ToggleState == auto.ToggleState.On and other is not box:
                                    other_toggle.Toggle()
                    time.sleep(0.4)
                    if not press("listo", "done", "hecho", "guardar", "save"):
                        press("cancelar", "cancel")
                        return None if not already else f"Esa canción ya estaba en tu playlist {name}."
                    song = spotify_playing(hwnd) or "la canción actual"
                    return f"Esa canción ya estaba en tu playlist {name}." if already else \
                        f"Agregué {song} a tu playlist {name}."
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return None


# ---------- Rutinas ----------
ROUTINES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rutinas.json")


def load_routines():
    try:
        with open(ROUTINES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_routines(data):
    with open(ROUTINES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_routine(data, name):
    want = [w for w in words(name) if w not in ("rutina", "modo", "la", "el", "de", "mi")] or words(name)
    scored = sorted(((sum(w in norm(k) for w in want), -len(k), k) for k in data), reverse=True)
    return scored[0][2] if scored and scored[0][0] else None


FOLDERS = {"descargas": "Downloads", "downloads": "Downloads", "documentos": "Personal", "documents": "Personal",
           "escritorio": "Desktop", "desktop": "Desktop", "imagenes": "My Pictures", "fotos": "My Pictures",
           "musica": "My Music", "videos": "My Video", "papelera": "RecycleBinFolder"}


def _volume_change(act):
    """Función actual->nuevo según la acción: nivel (absoluto), cambio (± puntos) o factor (0.5 = la mitad)."""
    if act.get("nivel") is not None:
        return lambda cur: float(act["nivel"])
    if act.get("factor") is not None:
        return lambda cur: cur * float(act["factor"])
    if act.get("cambio") is not None:
        return lambda cur: cur + float(act["cambio"])
    return None


BROWSERS = {"chrome", "msedge", "brave", "vivaldi", "opera"}


def browser_tabs():
    """[(elemento, título, suena)] de las pestañas de los navegadores Chromium abiertos (vía UI Automation)."""
    auto, IntPtr = uia()
    by_type = lambda t: auto.PropertyCondition(auto.AutomationElement.ControlTypeProperty, t)
    out = []
    for hwnd, _, exe in top_windows():
        if norm(exe) not in BROWSERS:
            continue
        try:
            win = auto.AutomationElement.FromHandle(IntPtr(hwnd))
            for tab in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.TabItem)):
                if tab.Current.ClassName != "Tab":  # las páginas web también tienen "pestañas" propias
                    continue
                buttons = {b.Current.ClassName: b for b in
                           tab.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Button))}
                # El indicador de audio (altavoz) solo existe en las pestañas que están sonando.
                out.append((buttons.get("TabCloseButton"), tab.Current.Name, "AlertIndicatorButton" in buttons))
        except Exception:  # noqa: BLE001  (ventana cerrándose)
            pass
    return out


def tab_title(name):
    """Título legible: sin el contador "(18) " ni el sufijo "- YouTube: reproducción de audio"."""
    return re.sub(r"\s*[-–|]\s*YouTube.*$", "", re.sub(r"^\(\d+\)\s*", "", name)).strip() or name


def close_tabs(should_close):
    """Cierra (sin darles el foco) las pestañas para las que should_close(título, suena) sea cierto."""
    auto, _ = uia()
    closed = []
    for button, name, playing in browser_tabs():
        if button is not None and should_close(name, playing):
            try:
                button.GetCurrentPattern(auto.InvokePattern.Pattern).Invoke()
                closed.append(tab_title(name))
            except Exception:  # noqa: BLE001
                pass
    return closed


def stop_youtube(reason="para que suene una sola cosa"):
    closed = close_tabs(lambda name, playing: playing and "youtube" in norm(name))
    if closed:
        INFO.append(f"Cerré {len(closed)} pestaña(s) de YouTube que sonaban ({'; '.join(closed)[:160]}) {reason}.")
    return closed


def spotify_set_playing(hwnd, play):
    """Pausa o reanuda Spotify con su propio botón (la tecla multimedia podría ir a parar al navegador)."""
    if bool(spotify_playing(hwnd)) == play:
        return True
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            auto, win, by_type = _spotify_ui(hwnd)
            for group in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Group)):
                if not any(k in norm(group.Current.Name) for k in ("controles del reproductor", "player controls")):
                    continue
                for button in group.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Button)):
                    if norm(button.Current.Name) in (("reproducir", "play") if play else ("pausar", "pause")):
                        button.GetCurrentPattern(auto.InvokePattern.Pattern).Invoke()
                        time.sleep(0.8)
                        break
                break
        except Exception:  # noqa: BLE001
            pass
        if bool(spotify_playing(hwnd)) == play:
            return True
        time.sleep(0.4)
    return False


def pause_spotify_if_playing(skip=()):
    hwnd = find_window("spotify", skip)
    if hwnd and spotify_playing(hwnd) and spotify_set_playing(hwnd, False):
        INFO.append("Pausé Spotify para que suene una sola cosa.")


def spotify_play_top(hwnd, query, timeout=14.0):
    """Aprieta "Reproducir" en el mejor resultado de la búsqueda abierta en Spotify. Devuelve si lo logró.

    La página de resultados es un grupo llamado "Buscar <consulta> | Spotify"; su primer botón de reproducir es
    el de la tarjeta del mejor resultado. Se espera a que cargue la búsqueda nueva (no la anterior).
    """
    auto, IntPtr = uia()
    want = "".join(words(urllib.parse.unquote(query)))
    by_type = lambda t: auto.PropertyCondition(auto.AutomationElement.ControlTypeProperty, t)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            raise_window(hwnd)
            enable_web_accessibility(hwnd)
            win = auto.AutomationElement.FromHandle(IntPtr(hwnd))
            for group in win.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Group)):
                name = group.Current.Name
                # Páginas: "Buscar … | Spotify", "<lista> - playlist by … | Spotify", "Spotify – Tus me gusta".
                if "spotify" not in norm(name) or want not in "".join(words(name)):
                    continue
                for button in group.FindAll(auto.TreeScope.Descendants, by_type(auto.ControlType.Button)):
                    first = norm(button.Current.Name).split(" ")[0]
                    if first in ("pausar", "pause"):
                        return True  # el botón pasó a "Pausar": el mejor resultado está sonando
                    if first in ("reproducir", "play"):
                        # Con la página recién cargada el clic a veces no hace nada: se reintenta hasta que
                        # el botón cambie a "Pausar".
                        button.GetCurrentPattern(auto.InvokePattern.Pattern).Invoke()
                        time.sleep(1.2)
                        break
                break
        except Exception:  # noqa: BLE001  (la ventana aún está cargando)
            pass
        time.sleep(0.4)
    return False


def youtube_video(query):
    """URL del primer video de YouTube para la búsqueda (de la página de resultados, ~1 s)."""
    try:
        html = _http("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
    except OSError:
        return None
    m = re.search(r'"videoId":"([\w-]{11})"', html)
    return f"https://www.youtube.com/watch?v={m[1]}" if m else None


def _place(hwnd, act, skip):
    if hwnd and (act.get("pantalla") is not None or act.get("lado")):
        mon = pick_monitor(act["pantalla"], monitor_of(hwnd)) if act.get("pantalla") is not None \
            else monitors()[(monitor_of(hwnd) or 1) - 1]
        move_to_monitor(hwnd, mon, act.get("maximizar"), act.get("lado"))
    elif hwnd and act.get("maximizar"):
        u.ShowWindow(ctypes.c_void_p(hwnd), 3)


def run(act, skip=()):
    """Ejecuta una acción (dict). Devuelve un mensaje de error para el usuario, o "" si salió bien."""
    kind = norm(act.get("accion", ""))
    if kind == "microfono":
        wanted = act.get("silenciar")
        _, muted = system_volume(flow=1)
        target = (not muted) if wanted in (None, "alternar") else bool(wanted)
        level = 0
        for role in (1, 2):  # el predeterminado y el de comunicaciones (el que usan Discord y los juegos)
            level, muted = system_volume(_volume_change(act), target, flow=1, role=role)
        INFO.append("Micrófono en silencio para todo el PC." if muted else f"Micrófono activo (nivel {level} %).")
        return ""
    if kind == "discord":
        if not find_window("discord", skip):
            return "Discord no tiene una ventana abierta (si está en la bandeja, ábrelo primero)."
        deafen = act.get("ensordecer")
        which, names = ("ensordecer", ("ensordecer", "deafen")) if deafen is not None \
            else ("silenciar", ("silenciar", "mute"))
        wanted = deafen if deafen is not None else act.get("silenciar")
        state = discord_toggle(names, None if wanted in (None, "alternar") else bool(wanted))
        if state is None:
            return "No encontré el botón en Discord."
        if which == "silenciar":
            INFO.append("Discord: micrófono " + ("silenciado." if state else "activo."))
        else:
            INFO.append("Discord: " + ("ensordecido (no oyes ni te oyen)." if state else "audio activo."))
        return ""
    if kind == "escribir":
        text = str(act.get("texto") or "")
        if not text:
            return "Dime qué escribir."
        foreground = u.GetForegroundWindow()
        target = (act.get("_game") or (None,))[0] or foreground
        if not target or target in skip:
            return "No hay una ventana activa donde escribir."
        previous = clipboard_get_text()
        if not clipboard_set_text(text):
            return "No pude usar el portapapeles."
        if foreground != target:
            focus(target)
            time.sleep(0.3)
        combo(0x11, 0x56)  # Ctrl+V
        time.sleep(0.25)
        if act.get("enviar"):
            tap(0x0D)
        time.sleep(0.3)
        if previous is not None:
            clipboard_set_text(previous)  # el portapapeles queda como estaba
        INFO.append(f"Pegué el texto en {window_title(target)[:50]}" + (" y apreté Enter." if act.get("enviar") else "."))
        return ""
    if kind == "captura":
        clips = norm(str(act.get("clips") or ""))
        if clips:  # la grabación en segundo plano se enciende solo cuando se pide, para no gastar recursos
            if clips.startswith(("activ", "encend", "prend", "si")):
                set_clips(True)
                INFO.append(f"Clips activados: mientras un juego esté al frente se van guardando sus últimos "
                            f"{clips_seconds()} segundos, y ya se puede pedir que guarde un clip. Usa algo de "
                            f"gráfica y disco hasta que se desactiven.")
            elif clips.startswith(("desactiv", "apag", "no")):
                set_clips(False)
                INFO.append("Clips desactivados: ya no se graba nada en segundo plano.")
            else:
                INFO.append("Los clips en segundo plano están " + ("activados." if clips_enabled() else "desactivados."))
            return ""
        clip = norm(act.get("clip") or "")
        if clip.startswith("ultim"):
            if not clips_enabled():
                return "Los clips están desactivados (para no gastar recursos), así que no hay nada grabado que " \
                       "guardar. Pregúntale si los activa; desde ese momento se puede guardar lo último que pase."
            _game_to_front(act)
            before = _clip_files()
            combo(0x5B, 0x12, 0x47)  # Win+Alt+G
            t0 = time.time()
            while time.time() - t0 < 8:
                time.sleep(0.5)
                new = [name for name in _clip_files() if name not in before]
                if new:
                    INFO.append(f"Clip guardado: {new[0]}, en {clips_folder()}.")
                    return ""
            return "No se guardó el clip: Game Bar solo graba mientras el juego está al frente y lleva unos " \
                   "segundos así (no graba el escritorio)."
        if clip:
            stop = clip.startswith(("deten", "para", "termin", "stop"))
            recording = clip_recording()
            if stop and not recording:
                return "No hay ninguna grabación en curso."
            if not stop and recording:
                INFO.append(f"Ya se está grabando ({recording}).")
                return ""
            _game_to_front(act)
            before = _clip_files()
            combo(0x5B, 0x12, 0x52)  # Win+Alt+R
            if stop:
                time.sleep(1.5)
                INFO.append(f"Grabación guardada: {recording}, en {clips_folder()}.")
                return ""
            t0 = time.time()
            while time.time() - t0 < 6:
                time.sleep(0.5)
                if [name for name in _clip_files() if name not in before]:
                    INFO.append("Grabando la ventana del juego; pide detenerla para guardar el video.")
                    return ""
            return "No empezó la grabación: Game Bar solo graba la ventana de un juego o app al frente, no el " \
                   "escritorio."
        from PIL import Image
        screens = monitors()
        if act.get("pantalla") is not None:
            mon = pick_monitor(act["pantalla"])
        else:  # la pantalla donde está lo que se estaba usando (el juego)
            game = (act.get("_game") or (None,))[0]
            index = monitor_of(game) if game else None
            mon = screens[index - 1] if index else next((m for m in screens if m.get("is_primary")), screens[0])
        with getattr(mss, "MSS", mss.mss)() as sct:
            raw = sct.grab(mon)
        image = Image.frombytes("RGB", raw.size, raw.rgb)
        dest, done = norm(act.get("destino") or "archivo"), []
        if dest in ("portapapeles", "ambos"):
            import io
            buf = io.BytesIO()
            image.save(buf, format="BMP")
            if clipboard_set(8, buf.getvalue()[14:]):  # CF_DIB = BMP sin la cabecera de archivo
                done.append("copiada al portapapeles")
        if dest != "portapapeles":
            folder = os.path.join(os.path.expanduser("~"), "Pictures", "Desk AI")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, time.strftime("Captura %Y-%m-%d %H.%M.%S.png"))
            image.save(path)
            done.append(f"guardada en {path}")
        if not done:
            return "No pude guardar la captura."
        INFO.append("Captura " + " y ".join(done) + ".")
        return ""
    if kind == "estado":
        text = pc_status()
        SAY.append(text)
        INFO.append(text)
        return ""
    if kind == "audio":
        flow = 1 if norm(act.get("tipo") or "") in ("entrada", "microfono", "mic") else 0
        devices = audio_devices(flow)
        kind_name = "micrófonos" if flow else "salidas de audio"
        listing = ", ".join(name + (" (en uso)" if current else "") for _, name, current in devices)
        if act.get("listar") or not act.get("dispositivo"):
            SAY.append(f"Tus {kind_name}: {listing}." if devices else f"No encontré {kind_name}.")
            INFO.append(f"{kind_name}: {listing}")
            return ""
        device = pick_audio_device(devices, str(act["dispositivo"]))
        if not device:
            return f"No encontré {act['dispositivo']} entre tus {kind_name}. Tienes: {listing}."
        if not device[2] and not set_default_audio(device[0]):
            return f"No pude cambiar a {device[1]}."
        INFO.append(f"Audio ({'entrada' if flow else 'salida'}) ahora en: {device[1]}.")
        return ""
    if kind == "rutina":
        data = load_routines()
        if act.get("guardar"):
            steps = act.get("acciones")
            if not isinstance(steps, list) or not steps or not all(
                    isinstance(a, dict) and a.get("accion") and norm(a["accion"]) != "rutina" for a in steps):
                return "Una rutina necesita una lista de acciones válidas."
            data[str(act["guardar"]).strip()] = steps
            save_routines(data)
            INFO.append(f"Guardé la rutina {act['guardar']} con {len(steps)} acciones.")
            return ""
        if act.get("borrar"):
            name = find_routine(data, str(act["borrar"]))
            if not name:
                return f"No tienes una rutina {act['borrar']}."
            del data[name]
            save_routines(data)
            INFO.append(f"Borré la rutina {name}.")
            return ""
        if act.get("ejecutar"):
            name = find_routine(data, str(act["ejecutar"]))
            if not name:
                return f"No tienes una rutina {act['ejecutar']}. Tienes: {', '.join(data) or 'ninguna'}."
            failed = []
            for step in data[name]:
                error = run({**step, "_game": act.get("_game", ())}, skip)
                if error:
                    failed.append(error)
            if failed:
                return f"Rutina {name}: fallaron {len(failed)} de {len(data[name])} pasos. " + " ".join(failed)
            INFO.append(f"Rutina {name} lista ({len(data[name])} acciones).")
            return ""
        SAY.append("Tus rutinas: " + ", ".join(data) + "." if data else "Todavía no tienes rutinas guardadas.")
        INFO.append("rutinas: " + (", ".join(data) or "ninguna"))
        return ""
    if kind == "spotify" and act.get("control"):
        hwnd = find_window("spotify", skip)
        if not hwnd:
            return "Spotify no está abierto."
        control = norm(str(act["control"])).replace(" ", "_")
        extra = str(act.get("lista") or "")
        if control == "aleatorio" and act.get("valor") is not None:
            extra = "si" if act["valor"] else "no"
        result = spotify_control(hwnd, control, extra)
        if result is None:
            return f"No pude hacer {control} en Spotify."
        if result.startswith("FALLÓ: "):
            return result[7:]
        INFO.append(result)
        return ""
    if kind == "youtube" and act.get("control"):
        control = norm(str(act["control"])).replace(" ", "_")
        page = _youtube_page()
        if not page:
            return "No encontré un video de YouTube abierto."
        hwnd, find = page
        auto, IntPtr = uia()
        if control == "me_gusta":
            win = auto.AutomationElement.FromHandle(IntPtr(hwnd))
            buttons = win.FindAll(auto.TreeScope.Descendants, auto.PropertyCondition(
                auto.AutomationElement.ControlTypeProperty, auto.ControlType.Button))
            for button in buttons:
                name = norm(button.Current.Name)
                if ("me gusta en este video" in name and not name.startswith("no me gusta")) or \
                        ("like this video" in name and "dislike" not in name):
                    button.GetCurrentPattern(auto.InvokePattern.Pattern).Invoke()
                    INFO.append("Le di me gusta al video de YouTube.")
                    return ""
            return "No encontré el botón de me gusta (¿ya lo tenía?)."
        keys = {"siguiente": [((0x10, 0x4E), 1)], "anterior": [((0x10, 0x50), 1)], "pantalla_completa": [(0x46, 1)],
                "silenciar": [(0x4D, 1)], "subtitulos": [(0x43, 1)], "modo_cine": [(0x54, 1)]}
        if control == "velocidad":
            try:
                speed = max(0.25, min(2.0, float(act.get("valor", 1))))
            except (TypeError, ValueError):
                return "Dime qué velocidad (entre 0,25 y 2)."
            # No se puede leer la velocidad actual: se baja al mínimo (0,25) y se sube hasta la pedida.
            presses = [(char_key("<"), 8), (char_key(">"), round((speed - 0.25) / 0.25))]
            label = f"velocidad {speed:g}x"
        elif control in keys:
            presses, label = keys[control], control.replace("_", " ")
        else:
            return f"No conozco el control {control} de YouTube."
        bar = find("ytp-progress-bar", auto.ControlType.Slider)
        if bar is None or not _youtube_keys(hwnd, bar, presses):
            return f"No pude hacer {label} en YouTube."
        INFO.append(f"YouTube: {label}.")
        return ""
    if kind == "cerrar":
        name = act.get("app") or act.get("ventana") or ""
        if not name:
            return "Dime qué programa cerrar."
        return close_app(name, bool(act.get("forzar")), skip)
    if kind == "posicion":
        if act.get("fraccion") is not None:
            position = lambda cur, dur: dur * float(act["fraccion"])
        elif act.get("segundos") is not None:
            position = lambda cur, dur: float(act["segundos"])
        elif act.get("cambio") is not None:
            position = lambda cur, dur: cur + float(act["cambio"])
        else:
            return "Dime a qué parte moverla."
        app = norm(act.get("app") or "")
        sp = find_window("spotify", skip)
        if not app:  # lo que esté sonando
            app = "youtube" if any(pl and "youtube" in norm(n) for _, n, pl in browser_tabs()) or not sp else "spotify"
        if "spotify" in app:
            done = spotify_seek(sp, position) if sp else None
            where = "Spotify"
        else:
            done = youtube_seek(position)
            where = "YouTube"
        if not done:
            return f"No pude mover la reproducción de {where}."
        INFO.append(f"{where} quedó en {clock(done[0])} de {clock(done[1])}.")
        return ""
    if kind == "temporizador":
        if act.get("cancelar"):
            count = len(_timers)
            for entry in list(_timers):
                entry["timer"].cancel()
            _timers[:] = []
            INFO.append(f"Cancelé {count} aviso(s).")
            return ""
        if act.get("listar"):
            items = [f"{e['label']} en {clock(max(0, e['at'] - time.time()))}" for e in _timers]
            SAY.append("Avisos pendientes: " + "; ".join(items) + "." if items else "No hay avisos pendientes.")
            INFO.append("avisos: " + ("; ".join(items) or "ninguno"))
            return ""
        message = str(act.get("mensaje") or "Se cumplió el tiempo.")
        every = float(act.get("cada_minutos") or 0) * 60
        if act.get("hora"):
            match = re.match(r"^\s*(\d{1,2})[:.h](\d{2})?\s*$", str(act["hora"])) or \
                re.match(r"^\s*(\d{1,2})()\s*$", str(act["hora"]))
            if not match or int(match[1]) > 23 or int(match[2] or 0) > 59:
                return "No entendí la hora; usa el formato 18:30."
            now = time.localtime()
            seconds = ((int(match[1]) * 60 + int(match[2] or 0)) * 60
                       - (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec)) % 86400
        else:
            seconds = float(act.get("minutos") or 0) * 60 + float(act.get("segundos") or 0) or every
        if seconds <= 0:
            return "Dime en cuánto tiempo o a qué hora avisarte."
        set_timer(seconds, message, every, int(act.get("veces") or 0))
        when = time.strftime("%H:%M", time.localtime(time.time() + seconds))
        INFO.append(f"Aviso puesto para las {when} (en {clock(seconds)})"
                    + (f", repitiendo cada {clock(every)}." if every else "."))
        return ""
    if kind == "sistema":
        what = norm(act.get("hacer") or "")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if what == "bloquear":
            u.LockWorkStation()
        elif what == "cancelar_apagado":
            subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=flags)
        elif what in ("apagar", "reiniciar", "suspender"):
            if act.get("confirmado") is not True:
                return f"Antes de {what} el PC pregúntale al usuario si está seguro; solo con su sí repite la acción " \
                       f"con \"confirmado\":true."
            if what == "suspender":
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=flags)
            else:  # con 30 s de margen: se puede cancelar con cancelar_apagado
                subprocess.run(["shutdown", "/s" if what == "apagar" else "/r", "/t", "30"], capture_output=True,
                               creationflags=flags)
                INFO.append(f"El PC se va a {what} en 30 segundos; se puede cancelar.")
        else:
            return "No conozco esa acción del sistema."
        return ""
    if kind == "youtube" and (act.get("pausar") or act.get("reanudar")):
        play = bool(act.get("reanudar"))
        if play and not act.get("mantener"):
            pause_spotify_if_playing(skip)
        if youtube_set_playing(play) is None:
            return "No encontré un video de YouTube abierto."
        INFO.append("YouTube " + ("sonando." if play else "en pausa."))
        return ""
    if kind == "abrir" and norm(act.get("carpeta") or act.get("app") or "") in FOLDERS:
        subprocess.Popen(["explorer.exe", "shell:" + FOLDERS[norm(act.get("carpeta") or act.get("app"))]])
        return ""
    if kind == "abrir":
        name = act.get("app", "")
        before = {h for h, *_ in top_windows()}
        app = find_app(name)
        if app and re.match(r"https?://", app[1]):
            webbrowser.open(app[1])
        elif app:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app[1]}"])
        else:
            try:
                os.startfile(name)
            except OSError:
                return f"No encontré {name} en el menú Inicio."
        if act.get("pantalla") is not None or act.get("maximizar"):
            hwnd = wait_window(app[0] if app else name, before, skip=skip) or wait_window(name, before, 2, skip)
            if not hwnd:
                return f"Abrí {name}, pero no encontré su ventana para moverla."
            _place(hwnd, act, skip)
        return ""
    if kind == "pestanas":
        dejar, cerrar = act.get("dejar"), act.get("cerrar")
        if dejar:
            # Deja la que calza y cierra las demás de YouTube o que estén sonando; nunca las otras páginas.
            want = words(dejar)
            match = lambda name: bool(want) and all(w in norm(name) for w in want)
            tabs = browser_tabs()
            if not any(match(name) for _, name, _ in tabs):
                media = [tab_title(n) for _, n, playing in tabs if playing or "youtube" in norm(n)]
                return f"No encontré una pestaña de {dejar}. Hay: {'; '.join(media) or 'ninguna de YouTube'}."
            closed = close_tabs(lambda name, playing: not match(name) and (playing or "youtube" in norm(name)))
            INFO.append(f"Dejé la pestaña de {dejar} y cerré {len(closed)}: {'; '.join(closed)[:200] or 'ninguna'}.")
            return ""
        if cerrar:
            want = [w for w in words(cerrar) if w not in ("pestana", "pestanas", "de", "la", "las", "el", "los")]
            if norm(str(cerrar)).strip() in ("sonando", "audio", "las que suenan"):
                closed = close_tabs(lambda name, playing: playing)
            elif want:
                closed = close_tabs(lambda name, playing: all(w in norm(name) for w in want))
            else:
                return "Dime qué pestañas cerrar."
            if not closed:
                return f"No encontré pestañas de {cerrar} abiertas."
            INFO.append(f"Cerré {len(closed)} pestaña(s): {'; '.join(closed)[:200]}.")
            return ""
        return "Dime qué pestañas cerrar o cuál dejar."
    if kind == "spotify" and (act.get("pausar") or act.get("reanudar")):
        hwnd = find_window("spotify", skip)
        if not hwnd:
            return "Spotify no está abierto."
        play = bool(act.get("reanudar"))
        if play and not act.get("mantener"):
            stop_youtube()
        if not spotify_set_playing(hwnd, play):
            return "No pude " + ("reanudar" if play else "pausar") + " Spotify."
        INFO.append(f"Spotify sonando: {spotify_playing(hwnd)}." if play else "Spotify en pausa.")
        return ""
    if kind == "spotify" and not act.get("mantener"):
        stop_youtube()  # una sola cosa suena a la vez
    if kind == "youtube":
        query = act.get("buscar") or act.get("video") or ""
        if not act.get("mantener"):  # reemplaza el video anterior en vez de sumar otra pestaña sonando
            stop_youtube("para reemplazarlas por el video nuevo")
            pause_spotify_if_playing(skip)
        act = {**act, "accion": "abrir_web",
               "url": youtube_video(query) or "https://www.youtube.com/results?search_query="
                      + urllib.parse.quote_plus(query)}
        kind = "abrir_web"
    if kind in ("abrir_web", "web"):
        url = act.get("url", "")
        if not re.match(r"https?://", url):
            return "Esa dirección no es válida."
        before = {h for h, *_ in top_windows()}
        webbrowser.open(url)
        if act.get("pantalla") is not None or act.get("maximizar"):
            site = urllib.parse.urlparse(url).hostname or ""
            site = site.removeprefix("www.").removeprefix("m.").split(".")[0]
            time.sleep(1.5)
            hwnd = wait_window(site, before, 8, skip)
            if not hwnd:
                fg = u.GetForegroundWindow()
                hwnd = fg if fg and fg not in skip and fg not in act.get("_game", ()) else None
            if not hwnd:
                return "Abrí la página, pero no encontré la ventana del navegador para moverla."
            _place(hwnd, act, skip)
        return ""
    if kind == "spotify" and (act.get("lista") or act.get("biblioteca")):
        # "Mi lista de …": se busca en Tu biblioteca, no en el buscador (que trae listas de otra gente).
        name = str(act.get("lista") or act.get("biblioteca"))
        hwnd = find_window("spotify", skip)
        if not hwnd:
            before = {h for h, *_ in top_windows()}
            os.startfile("spotify:")
            hwnd = wait_window("spotify", before, 15, skip)
            if not hwnd:
                return "Abrí Spotify, pero no encontré su ventana."
            time.sleep(2)
        _place(hwnd, act, skip)
        if any(k in norm(name) for k in ("gusta", "liked", "favorit")):  # "mis me gusta": tiene su propio enlace
            os.startfile("spotify:collection:tracks")
            played, names = "Tus me gusta", []
            ok = spotify_play_top(hwnd, "me gusta", 7) or spotify_play_top(hwnd, "liked", 3)
        else:
            played, names, ok = spotify_play_library(hwnd, name)
        if not played:
            if names:
                return f"No encontré {name} en tu biblioteca de Spotify. Tienes: {', '.join(names[:15])}."
            return "No pude leer tu biblioteca de Spotify."
        t0 = time.time()
        while ok and not spotify_playing(hwnd) and time.time() - t0 < 4:
            time.sleep(0.3)
        if not ok or not spotify_playing(hwnd):
            return f"Abrí tu lista {played} en Spotify, pero no pude darle a reproducir."
        INFO.append(f"Sonando tu lista {played} en Spotify: {spotify_playing(hwnd)}.")
        return ""
    if kind == "volumen":
        change = _volume_change(act)
        mute = act.get("silenciar")
        if change is None and mute is None:
            return "No me dijiste cuánto volumen."
        app = norm(act.get("app") or "")
        if app and app not in ("sistema", "windows", "pc", "todo") and "spotify" not in app:
            # Cualquier otra app (YouTube = el navegador, un juego, Discord…): su barra del mezclador de Windows.
            label = act.get("app")
            if "youtube" in app and change is not None:
                level = youtube_volume(change)  # el control del propio reproductor, que es el que suele estar bajo
                if level is not None:
                    INFO.append(f"Volumen del reproductor de YouTube: {level} %.")
                    return ""
            if any(k in app for k in ("youtube", "navegador", "browser", "video", "chrome", "twitch", "netflix")):
                is_app = lambda exe: exe in BROWSERS
            else:
                keys = ["".join(words(app))] + words(app)
                hwnd = find_window(app, skip)
                if hwnd:
                    keys.append(norm(exe_of(hwnd)))
                is_app = lambda exe: any(k and (k in exe.replace(" ", "") or exe in k) for k in keys)
            done = app_volume(is_app, change, mute)
            if not done:
                return f"{label} no está reproduciendo audio ahora, así que no tiene volumen que cambiar."
            INFO.append(f"Volumen de {label} en el mezclador de Windows: {done[0]} %"
                        + (" (en silencio)." if done[1] else "."))
            return ""
        if app and app not in ("sistema", "windows", "pc", "todo"):
            hwnd = find_window("spotify", skip)
            if not hwnd:
                return "Spotify no está abierto."
            level = spotify_volume(hwnd, change or (lambda cur: 0.0 if mute else cur))
            if level is None:
                return "No pude cambiar el volumen de Spotify."
            INFO.append(f"Volumen de Spotify: {level} %.")
            return ""
        level, muted = system_volume(change, mute)
        INFO.append(f"Volumen de Windows: {level} %" + (" (en silencio)." if muted else "."))
        return ""
    if kind == "spotify":
        target = (act.get("cancion") or act.get("uri") or act.get("buscar") or "").strip()
        if not target:
            return "No me dijiste qué poner en Spotify."
        uri, what = _spotify_uri(target)
        before = {h for h, *_ in top_windows()}
        running = find_window("spotify", skip)
        was = spotify_playing(running) if running else ""
        os.startfile(uri)
        hwnd = running or wait_window("spotify", before, 15, skip)
        if not hwnd:
            return "Abrí Spotify, pero no encontré su ventana."
        _place(hwnd, act, skip)
        if what == "search":
            if not spotify_play_top(hwnd, target):
                return f"Abrí la búsqueda de {target} en Spotify, pero no pude darle a reproducir."
        elif what not in ("track", "episode"):
            INFO.append(f"Abrí en Spotify: {target}.")
            return ""
        # Comprobar que de verdad suena (y, al abrir un enlace, que cambió lo que sonaba).
        t0, now = time.time(), ""
        while time.time() - t0 < 6:
            now = spotify_playing(hwnd)
            if now and (what == "search" or now != was or time.time() - t0 > 3):
                break
            time.sleep(0.3)
        if not now and what != "search":  # Spotify recién abierto deja la canción en pausa
            tap(MEDIA_KEYS["play_pausa"])
            time.sleep(1.5)
            now = spotify_playing(hwnd)
        if not now:
            return "Abrí Spotify, pero no está sonando."
        INFO.append(f"Sonando en Spotify: {now}.")
        return ""
    if kind in ("mover", "maximizar", "minimizar", "enfocar"):
        name = act.get("ventana") or act.get("app") or ""
        hwnd = find_window(name, skip)
        if not hwnd:
            return f"No encontré ninguna ventana de {name} abierta."
        if kind == "mover":
            if act.get("pantalla") is None and not act.get("lado"):
                act = {**act, "pantalla": "secundaria"}
            _place(hwnd, act, skip)
        elif kind == "maximizar":
            u.ShowWindow(ctypes.c_void_p(hwnd), 3)
        elif kind == "minimizar":
            u.ShowWindow(ctypes.c_void_p(hwnd), 7)  # SW_SHOWMINNOACTIVE
        else:
            focus(hwnd)
        return ""
    if kind == "multimedia":
        vk = MEDIA_KEYS.get(norm(act.get("tecla", "")))
        if not vk:
            return "No conozco esa tecla multimedia."
        tap(vk, act.get("veces", 1))
        return ""
    return f"No sé hacer la acción {act.get('accion')!r}."


def is_action(text):
    """¿`text` (lo de dentro de [[ ]]) es una acción completa y bien formada?"""
    try:
        return isinstance(json.loads(re.sub(r"^\s*ACCI[OÓ]N\s*", "", text.strip(), flags=re.I)), dict)
    except ValueError:
        return False


def run_text(text, game=None, skip=()):
    """`text` es lo de dentro de [[ ]]: "ACCION {json}". Ejecuta y devuelve un error o ""."""
    raw = re.sub(r"^\s*ACCI[OÓ]N\s*", "", text.strip(), flags=re.I)
    try:
        act = json.loads(raw)
    except ValueError:
        return "No entendí la acción que pidió Claude."
    if not isinstance(act, dict):
        return "No entendí la acción que pidió Claude."
    act["_game"] = (game,) if game else ()
    try:
        err = run(act, skip=tuple(skip))
    except Exception as exc:  # noqa: BLE001
        err = f"Falló la acción: {exc}"
    # Seguir jugando: el foco vuelve al juego, salvo que se haya pedido enfocar otra ventana.
    if game and norm(act.get("accion", "")) != "enfocar" and u.GetForegroundWindow() != game:
        time.sleep(0.2)
        focus(game)
    return err


def prompt():
    """Instrucciones para Claude (van al final del system prompt)."""
    try:
        screens = describe_monitors()
    except Exception:  # noqa: BLE001
        screens = "1 (principal)"
    return (
        " Eres también un asistente tipo Jarvis que controla el PC del usuario mientras juega. Para hacer algo, "
        "escribe al inicio de tu respuesta una línea [[ACCION {json}]] por acción (se ejecutan en orden, no se "
        "leen en voz alta y le devuelven el foco al juego) y después una confirmación de pocas palabras. "
        "Acciones posibles: "
        '{"accion":"abrir","app":"<nombre como en el menú Inicio>","pantalla":<opcional>,"maximizar":<opcional>}; '
        '{"accion":"abrir_web","url":"https://...","pantalla":<opcional>,"maximizar":<opcional>}; '
        '{"accion":"spotify","cancion":"<canción, artista, álbum o tema>","pantalla":<opcional>} (busca eso en '
        'Spotify y reproduce el mejor resultado); '
        '{"accion":"spotify","lista":"<nombre>","pantalla":<opcional>} (reproduce una playlist de SU biblioteca: '
        'úsala cuando diga "mi lista", "mi playlist" o "mis me gusta"; pon solo la palabra clave, p. ej. "anime"); '
        '{"accion":"youtube","buscar":"<qué video o podcast>","pantalla":<opcional>,"maximizar":<opcional>} '
        '(abre y reproduce el primer resultado en el navegador); '
        '{"accion":"spotify","pausar":true} o {"accion":"spotify","reanudar":true}; '
        '{"accion":"pestanas","dejar":"<palabras del título>"} (deja esa pestaña y cierra las demás de YouTube o '
        'que estén sonando; no toca las otras páginas); '
        '{"accion":"pestanas","cerrar":"<palabras del título>"|"sonando"} (cierra las pestañas que calzan, o '
        'todas las que suenan); '
        '{"accion":"mover","ventana":"<app o título>","pantalla":<opcional>,"lado":<opcional: "izquierda"|"derecha"|'
        '"arriba"|"abajo" = media pantalla>,"maximizar":<opcional>}; '
        '{"accion":"cerrar","app":"<programa>","forzar":<true solo si pide forzar, matar o finalizar la tarea, o si '
        'el cierre normal no funcionó y lo pide>}; '
        '{"accion":"posicion","app":"youtube"|"spotify" (opcional: por defecto lo que suena),"fraccion":<0-1, 0.5 = '
        'la mitad> | "segundos":<desde el inicio> | "cambio":<± segundos>} (mueve la reproducción, no el volumen); '
        '{"accion":"youtube","pausar":true} o {"accion":"youtube","reanudar":true}; '
        '{"accion":"abrir","carpeta":"descargas|documentos|escritorio|imagenes|musica|videos|papelera"}; '
        '{"accion":"temporizador","minutos":<n>,"segundos":<n> | "hora":"HH:MM" (24 h),"cada_minutos":<opcional: se '
        'repite>,"veces":<opcional>,"mensaje":"<qué decirle al cumplirse>"} (o "cancelar":true, o "listar":true); '
        '{"accion":"microfono","silenciar":true|false|"alternar"} (su micrófono en todo el PC); '
        '{"accion":"discord","silenciar":true|false|"alternar"} o {"accion":"discord","ensordecer":true|false|'
        '"alternar"}; '
        '{"accion":"escribir","texto":"<texto>","enviar":<true = además aprieta Enter>} (lo pega en la ventana que '
        'estaba usando; corrige ortografía y puntuación del dictado, sin agregar nada); '
        '{"accion":"captura","destino":"archivo"|"portapapeles"|"ambos","pantalla":<opcional>} (guarda la pantalla) o '
        '{"accion":"captura","clips":"activar"|"desactivar"|"estado"} (grabación en segundo plano del juego: parte '
        'apagada para no gastar recursos; actívala cuando la pida o cuando quiera poder guardar jugadas); '
        '{"accion":"captura","clip":"ultimos"} (guarda en video lo último que pasó; solo si los clips están '
        'activados), {"accion":"captura","clip":"grabar"} y {"accion":"captura","clip":"detener"} (grabación '
        'manual del juego, funciona siempre); '
        '{"accion":"estado"} (hora, batería, uso de procesador, memoria y gráfica, y qué consume más: la app lo '
        'lee en voz alta, tú solo di "Revisando."); '
        '{"accion":"audio","dispositivo":"<audífonos, parlantes, nombre… u \"otro\">","tipo":"salida"|"entrada"} '
        '(cambia el dispositivo de audio; con "listar":true la app los lee en voz alta); '
        '{"accion":"spotify","control":"siguiente|anterior|aleatorio|repetir|me_gusta"} o {"accion":"spotify",'
        '"control":"agregar_a_lista","lista":"<playlist suya>"} (la canción que suena); '
        '{"accion":"youtube","control":"siguiente|anterior|pantalla_completa|silenciar|subtitulos|modo_cine|me_gusta"} '
        'o {"accion":"youtube","control":"velocidad","valor":<0.25-2>}; '
        '{"accion":"rutina","ejecutar":"<nombre>"}, {"accion":"rutina","guardar":"<nombre>","acciones":[<acciones de '
        'esta lista, en orden>]}, {"accion":"rutina","borrar":"<nombre>"} o {"accion":"rutina","listar":true} '
        '(rutinas = varias acciones con una frase, p. ej. "modo juego"; si pide crear una, ármala tú con las '
        'acciones que describa); '
        '{"accion":"sistema","hacer":"bloquear|suspender|apagar|reiniciar|cancelar_apagado"} (apagar, reiniciar y '
        'suspender exigen que el usuario confirme: pregúntale primero y solo entonces manda "confirmado":true); '
        '{"accion":"maximizar"|"minimizar"|"enfocar","ventana":"<app o título>"}; '
        '{"accion":"volumen","app":"sistema"|"spotify"|"youtube"|"<cualquier app o juego>","nivel":<0-100> | '
        '"cambio":<± puntos> | "factor":<0.5 = a la mitad>,"silenciar":<true|false, opcional>} (si nombra una app, '
        'usa esa: cada una tiene su propio volumen; "un poco" son unos 15 puntos; el resultado te dice el nivel '
        'que quedó); '
        '{"accion":"multimedia","tecla":"play_pausa|siguiente|anterior"}. '
        f"Pantallas: {screens}; en \"pantalla\" usa el número, \"principal\" o \"secundaria\". "
        f"Rutinas guardadas: {', '.join(load_routines()) or 'ninguna'}. "
        "Solo suena una cosa a la vez y la app se encarga sola: al poner algo en YouTube cierra las pestañas de "
        "YouTube que estaban sonando y pausa Spotify; al poner algo en Spotify cierra las pestañas de YouTube que "
        "sonaban. No agregues acciones para eso; solo si el usuario pide que suenen dos cosas a la vez, añade "
        "\"mantener\":true. "
        "Para música, videos o podcasts usa spotify o youtube con una búsqueda precisa (la app encuentra el "
        "enlace exacto): no busques en la web para eso, es más lento. Si el usuario habla de un tema (p. ej. "
        "\"cómo ganar en mid\"), arma la búsqueda con el juego y el tema. "
        "La consulta viene dictada: corrige los nombres mal transcritos antes de buscar (p. ej. \"chingeki\" es "
        "\"Shingeki no Kyojin\"). Tú no ves si la acción funcionó: confirma en una frase lo que estás haciendo "
        "(\"Poniendo…\", \"Abriendo…\"), nunca digas que ya está sonando o abierto. El resultado real te llega "
        "en una nota al inicio del siguiente mensaje; si el usuario dice que no funcionó, créele y repite la "
        "acción (con otra búsqueda si hace falta) en vez de insistir. "
        "Si dice \"ponlo\", \"muévelo\" o similar, se refiere a lo último que abriste. No inventes acciones fuera "
        "de esta lista: si te piden otra cosa en el PC, dilo en una frase."
    )
