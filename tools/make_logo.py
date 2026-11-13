"""Genera los íconos de Desk AI a partir de los SVG de assets/ (escritos a mano), rasterizándolos con Chrome.

- logo.svg / logo-small.svg: ícono de la app, sin Claude (ventana, bandeja, favicon). logo-small se usa en 16-40 px.
- logo-claude.svg: logo con la insignia de integración de Claude, solo para el README, la landing y redes.
"""

import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
LANDING = os.path.join(ROOT, "landing")
C1, C2 = (139, 92, 246), (34, 211, 238)  # violeta → cian
CHROME = [
    os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
]


def rasterize(svg_path, size=1024):
    """SVG → imagen RGBA de `size` px, con fondo transparente."""
    browser = next((p for p in CHROME if os.path.exists(p)), None)
    if not browser:
        raise SystemExit("Hace falta Chrome o Edge para rasterizar el SVG.")
    tmp = tempfile.mkdtemp()
    try:
        page = os.path.join(tmp, "page.html")
        out = os.path.join(tmp, "out.png")
        with open(svg_path, encoding="utf-8") as f:
            svg = f.read().replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
        with open(page, "w", encoding="utf-8") as f:
            f.write(f"<!doctype html><style>html,body{{margin:0;background:transparent}}</style>{svg}")
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={os.path.join(tmp, 'profile')}",
                        "--default-background-color=00000000", f"--window-size={size},{size}",
                        f"--screenshot={out}", "file:///" + page.replace("\\", "/")],
                       check=True, capture_output=True, timeout=60)
        return Image.open(out).convert("RGBA").copy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def og_image(logo, path):
    """Imagen para compartir el sitio en redes (1200x630)."""
    w, h = 1200, 630
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.full((h, w, 3), (11, 10, 18), np.float32)
    for cx, cy, rad, col, a in ((260, 120, 520, C1, .35), (1050, 560, 520, C2, .25)):
        f = np.clip(1 - np.hypot(xx - cx, yy - cy) / rad, 0, 1) ** 2 * a
        base += f[..., None] * np.array(col, np.float32)
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    small = logo.resize((230, 230), Image.LANCZOS)
    img.paste(small, (90, 200), small)
    d = ImageDraw.Draw(img)
    fonts = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    bold = ImageFont.truetype(os.path.join(fonts, "segoeuib.ttf"), 96)
    reg = ImageFont.truetype(os.path.join(fonts, "segoeui.ttf"), 38)
    d.text((370, 205), "Desk AI", font=bold, fill=(236, 235, 245))
    d.text((374, 330), "Asistente de voz tipo Jarvis para Windows.", font=reg, fill=(190, 186, 210))
    d.text((374, 380), "Mantén Ctrl+Win, habla y sigue jugando.", font=reg, fill=(154, 151, 176))
    img.save(path)


def main():
    os.makedirs(LANDING, exist_ok=True)
    full = rasterize(os.path.join(ASSETS, "logo.svg"))
    small = rasterize(os.path.join(ASSETS, "logo-small.svg"))
    claude = rasterize(os.path.join(ASSETS, "logo-claude.svg"))
    at = lambda img, n: img.resize((n, n), Image.LANCZOS)

    full.save(os.path.join(ASSETS, "logo.png"))
    claude.save(os.path.join(ASSETS, "logo-claude.png"))
    for name in ("logo.svg", "logo-claude.svg"):
        shutil.copy(os.path.join(ASSETS, name), os.path.join(LANDING, name))
    at(full, 180).save(os.path.join(LANDING, "apple-touch-icon.png"))
    at(small, 64).save(os.path.join(LANDING, "favicon.png"))
    og_image(claude, os.path.join(LANDING, "og.png"))

    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [at(small if n <= 40 else full, n) for n in sizes]
    frames[-1].save(os.path.join(ASSETS, "icon.ico"), sizes=[(n, n) for n in sizes], append_images=frames[:-1])


if __name__ == "__main__":
    main()
