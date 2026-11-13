"""Ícono de Desk AI en la bandeja del sistema (área de íconos ocultos de Windows).

Clic izquierdo: muestra la ventana. Clic derecho: menú con overlay, voz, nueva conversación y salir.
Las opciones no tocan la interfaz directamente: mandan eventos a la cola de la app, como los atajos globales.
"""

import os
import threading

import pystray
from PIL import Image

ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")


class Tray:
    def __init__(self, app, title):
        self.app = app
        put = lambda *ev: (lambda icon, item: app.events.put(ev))
        menu = pystray.Menu(
            pystray.MenuItem("Mostrar ventana", put("show_window"), default=True),
            pystray.MenuItem("Modo overlay", put("overlay"), checked=lambda item: app.overlay_mode),
            pystray.MenuItem("Leer en voz alta", put("toggle_voice"), checked=lambda item: app.voice_on),
            pystray.MenuItem("Nueva conversación", put("reset")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", put("quit")),
        )
        image = Image.open(ICON)
        image.size = (32, 32)  # el ICO trae una versión simplificada para tamaños chicos
        self.icon = pystray.Icon("desk-ai", image.convert("RGBA"), title, menu)

    def start(self):
        threading.Thread(target=self.icon.run, daemon=True).start()

    def stop(self):
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            pass
