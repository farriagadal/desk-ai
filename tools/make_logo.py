"""Genera el logo de Desk AI: assets/logo.svg, assets/logo.png (1024), assets/icon.ico y las imágenes de landing/.

Un monitor con una onda de voz sobre un cuadrado redondeado violeta → cian. En tamaños chicos (bandeja del
sistema, 16-32 px) se dibuja solo la onda, más gruesa, para que se lea bien.
"""

import os

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C1, C2 = (139, 92, 246), (34, 211, 238)  # violeta → cian
S = 1024
RADIUS = 232

# Geometría en un lienzo de 1024 (la misma para el SVG y el PNG).
SCREEN = (196, 250, 828, 694, 76)          # x0, y0, x1, y1, radio
STROKE = 58
NECK = (474, 694, 550, 776)
BASE = (342, 768, 682, 822, 27)
BARS_FULL = [(-2, 92), (-1, 196), (0, 290), (1, 196), (2, 92)]  # (posición, alto)
BAR_W, BAR_GAP, BAR_CY = 50, 92, 472
BARS_SMALL = [(-1, 360), (0, 600), (1, 360)]
BAR_W_SMALL, BAR_GAP_SMALL = 170, 250


def gradient(size):
    t = np.add.outer(np.arange(size), np.arange(size)) / (2 * (size - 1))
    rgb = np.stack([C1[i] + (C2[i] - C1[i]) * t for i in range(3)], -1).astype(np.uint8)
    return Image.fromarray(rgb, "RGB")


def bars(spec, width, gap, cy):
    out = []
    for pos, h in spec:
        cx = S / 2 + pos * gap
        out.append((cx - width / 2, cy - h / 2, cx + width / 2, cy + h / 2, width / 2))
    return out


def render(size, small):
    k = 4  # supersampling
    big = S * k
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), RADIUS * k, fill=255)
    glyph = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(glyph)
    sc = lambda box: [v * k for v in box]
    if small:
        for x0, y0, x1, y1, r in bars(BARS_SMALL, BAR_W_SMALL, BAR_GAP_SMALL, S / 2):
            d.rounded_rectangle(sc((x0, y0, x1, y1)), r * k, fill=255)
    else:
        x0, y0, x1, y1, r = SCREEN
        d.rounded_rectangle(sc((x0, y0, x1, y1)), r * k, outline=255, width=STROKE * k)
        d.rectangle(sc(NECK), fill=255)
        d.rounded_rectangle(sc(BASE[:4]), BASE[4] * k, fill=255)
        for bx0, by0, bx1, by1, br in bars(BARS_FULL, BAR_W, BAR_GAP, BAR_CY):
            d.rounded_rectangle(sc((bx0, by0, bx1, by1)), br * k, fill=255)
    img = gradient(big).convert("RGBA")
    img.paste((255, 255, 255, 255), (0, 0), glyph)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def svg():
    x0, y0, x1, y1, r = SCREEN
    h = STROKE / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}">',
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="rgb{C1}"/><stop offset="1" stop-color="rgb{C2}"/></linearGradient></defs>',
        f'<rect width="{S}" height="{S}" rx="{RADIUS}" fill="url(#g)"/>',
        '<g fill="#fff">',
        f'<rect x="{x0 + h}" y="{y0 + h}" width="{x1 - x0 - STROKE}" height="{y1 - y0 - STROKE}" rx="{r - h}" '
        f'fill="none" stroke="#fff" stroke-width="{STROKE}"/>',
        f'<rect x="{NECK[0]}" y="{NECK[1]}" width="{NECK[2] - NECK[0]}" height="{NECK[3] - NECK[1]}"/>',
        f'<rect x="{BASE[0]}" y="{BASE[1]}" width="{BASE[2] - BASE[0]}" height="{BASE[3] - BASE[1]}" rx="{BASE[4]}"/>',
    ]
    for bx0, by0, bx1, by1, br in bars(BARS_FULL, BAR_W, BAR_GAP, BAR_CY):
        parts.append(f'<rect x="{bx0:g}" y="{by0:g}" width="{bx1 - bx0:g}" height="{by1 - by0:g}" rx="{br:g}"/>')
    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


def og_image(path):
    """Imagen para compartir el sitio en redes (1200x630)."""
    from PIL import ImageFont
    w, h = 1200, 630
    img = Image.new("RGB", (w, h), (11, 10, 18))
    glow = np.zeros((h, w, 3), np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    for (cx, cy, rad, col, a) in ((260, 120, 520, C1, .35), (1050, 560, 520, C2, .25)):
        f = np.clip(1 - np.hypot(xx - cx, yy - cy) / rad, 0, 1) ** 2 * a
        glow += f[..., None] * np.array(col, np.float32)
    img = Image.fromarray(np.clip(np.array(img, np.float32) + glow, 0, 255).astype(np.uint8))
    logo = render(220, False)
    img.paste(logo, (90, 205), logo)
    d = ImageDraw.Draw(img)
    fonts = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    bold = ImageFont.truetype(os.path.join(fonts, "segoeuib.ttf"), 96)
    reg = ImageFont.truetype(os.path.join(fonts, "segoeui.ttf"), 38)
    d.text((360, 205), "Desk AI", font=bold, fill=(236, 235, 245))
    d.text((364, 330), "Asistente de voz tipo Jarvis para Windows.", font=reg, fill=(190, 186, 210))
    d.text((364, 380), "Mantén Ctrl+Win, habla y sigue jugando.", font=reg, fill=(154, 151, 176))
    img.save(path)


def main():
    assets = os.path.join(ROOT, "assets")
    landing = os.path.join(ROOT, "landing")
    os.makedirs(assets, exist_ok=True)
    os.makedirs(landing, exist_ok=True)
    with open(os.path.join(assets, "logo.svg"), "w", encoding="utf-8") as f:
        f.write(svg())
    with open(os.path.join(landing, "logo.svg"), "w", encoding="utf-8") as f:
        f.write(svg())
    render(1024, False).save(os.path.join(assets, "logo.png"))
    render(512, False).save(os.path.join(landing, "logo.png"))
    render(180, False).save(os.path.join(landing, "apple-touch-icon.png"))
    render(64, True).save(os.path.join(landing, "favicon.png"))
    og_image(os.path.join(landing, "og.png"))
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [render(n, n <= 40) for n in sizes]
    frames[-1].save(os.path.join(assets, "icon.ico"), sizes=[(n, n) for n in sizes], append_images=frames[:-1])


if __name__ == "__main__":
    main()
