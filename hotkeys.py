"""Atajos de teclado globales con un hook de bajo nivel de Windows (WH_KEYBOARD_LL).

Reemplaza a la librería `keyboard`, cuyo hook no recibe eventos en Python 3.13.
Uso:
    hk = GlobalHotkeys()
    hk.on_press("f7", callback)          # una tecla
    hk.on_release("ctrl+win", callback)  # o una combinación: se activa al tener todas presionadas
    hk.start()
Los callbacks se ejecutan en el hilo del hook: deben ser rápidos (p. ej. poner algo en una cola).
"""

import ctypes
import ctypes.wintypes as wt
import threading

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105

user32 = ctypes.WinDLL("user32", use_last_error=True)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.CallNextHookEx.restype = LRESULT

# Modificadores: el hook informa la tecla izquierda o la derecha, cualquiera de las dos vale.
MODIFIERS = {
    "ctrl": (0xA2, 0xA3), "control": (0xA2, 0xA3), "shift": (0xA0, 0xA1), "alt": (0xA4, 0xA5),
    "win": (0x5B, 0x5C), "windows": (0x5B, 0x5C),
}
WIN_KEYS = {0x5B, 0x5C}
MASK_KEY = 0xE8      # tecla virtual sin uso: impide que soltar Win abra el menú Inicio
LLKHF_INJECTED = 0x10

NAMED_KEYS = {
    "pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "printscreen": 0x2C, "capslock": 0x14,
}


def vk_code(name):
    """'f7' -> 0x76, 'pause' -> 0x13, 'a' -> 0x41."""
    n = name.strip().lower().replace(" ", "")
    if n.startswith("f") and n[1:].isdigit() and 1 <= int(n[1:]) <= 24:
        return 0x70 + int(n[1:]) - 1
    if n in NAMED_KEYS:
        return NAMED_KEYS[n]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    raise ValueError(f"Tecla no soportada para atajo global: {name!r}")


def key_groups(spec):
    """'ctrl+win' -> ((0xA2, 0xA3), (0x5B, 0x5C)); 'f7' -> ((0x76,),)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    return tuple(MODIFIERS.get(p) or (vk_code(p),) for p in parts)


def key_label(spec):
    """'ctrl+win' -> 'Ctrl+Win', 'f7' -> 'F7'."""
    return "+".join(p.strip().capitalize() if p.strip().lower() in MODIFIERS else p.strip().upper()
                    for p in spec.split("+"))


def _tap_mask_key():
    user32.keybd_event(MASK_KEY, 0, 0, 0)
    user32.keybd_event(MASK_KEY, 0, 2, 0)  # KEYEVENTF_KEYUP


class GlobalHotkeys:
    def __init__(self):
        self.press = {}
        self.release = {}
        self.chords = {}   # groups -> {"press": cb, "release": cb, "active": bool}
        self.down = set()  # para no repetir on_press con la autorrepetición de la tecla
        self._proc = HOOKPROC(self._hook)  # referencia viva: si se recolecta, Windows llama a basura

    def _chord(self, key):
        groups = key_groups(key)
        if len(groups) == 1 and len(groups[0]) == 1:
            return None, groups[0][0]
        return self.chords.setdefault(groups, {"press": None, "release": None, "active": False}), None

    def on_press(self, key, callback):
        chord, vk = self._chord(key)
        if chord is None:
            self.press[vk] = callback
        else:
            chord["press"] = callback

    def on_release(self, key, callback):
        chord, vk = self._chord(key)
        if chord is None:
            self.release[vk] = callback
        else:
            chord["release"] = callback

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="hotkeys").start()

    def _run(self):
        if not user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            pass

    def _hook(self, n_code, w_param, l_param):
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = info.vkCode
            if vk == MASK_KEY and info.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            try:
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if vk not in self.down:
                        self.down.add(vk)
                        if vk in self.press:
                            self.press[vk]()
                        self._chords_down(vk)
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    self.down.discard(vk)
                    if vk in self.release:
                        self.release[vk]()
                    self._chords_up(vk)
            except Exception:  # noqa: BLE001  (un error aquí no debe bloquear el teclado del sistema)
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _chords_down(self, vk):
        for groups, chord in self.chords.items():
            if chord["active"] or not any(vk in g for g in groups):
                continue
            if all(self.down.intersection(g) for g in groups):
                chord["active"] = True
                if any(WIN_KEYS.intersection(g) for g in groups):
                    # Fuera del hook (no se debe inyectar teclas dentro de él) y antes de que se suelte Win.
                    threading.Thread(target=_tap_mask_key, daemon=True).start()
                if chord["press"]:
                    chord["press"]()

    def _chords_up(self, vk):
        for groups, chord in self.chords.items():
            if chord["active"] and any(vk in g for g in groups):
                chord["active"] = False  # basta con soltar una de las teclas
                if chord["release"]:
                    chord["release"]()
