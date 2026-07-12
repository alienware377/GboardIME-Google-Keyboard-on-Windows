"""Generate the GboardIME app icon (Android launcher + Windows .ico).

A chunky, friendly keyboard on the Gboard purple->pink gradient used by the
relay's theme. Drawn at 1024px and downscaled, so every size stays crisp.
Run:  python assets/make_icon.py
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
S = 1024

# Theme colours (sampled from the Gboard theme in use)
TOP    = (34, 24, 108)     # deep indigo
BOTTOM = (233, 74, 158)    # pink/magenta
KEY    = (255, 255, 255, 236)
KEY_SOFT = (255, 255, 255, 210)
ACCENT = (255, 45, 143, 255)   # hot pink (enter key)

def vertical_gradient(size, top, bottom):
    img = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        img.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img.resize((size, size))

def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m

def draw_keyboard(draw):
    # 3 rows of chunky keys, centered; spacebar + pink enter on the bottom row.
    kr = 34                       # key corner radius
    # Row 1: 4 keys
    y, h = 262, 148
    w, gap = 166, 36
    x0 = (S - (4 * w + 3 * gap)) // 2
    for i in range(4):
        x = x0 + i * (w + gap)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=kr, fill=KEY)
    # Row 2: 3 wider keys, offset
    y, h = 456, 148
    w, gap = 210, 36
    x0 = (S - (3 * w + 2 * gap)) // 2
    for i in range(3):
        x = x0 + i * (w + gap)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=kr, fill=KEY_SOFT)
    # Row 3: spacebar + accent enter key
    y, h = 650, 148
    total = 4 * 166 + 3 * 36      # match row 1 width
    x0 = (S - total) // 2
    space_w = total - 166 - 36
    draw.rounded_rectangle([x0, y, x0 + space_w, y + h], radius=kr, fill=KEY)
    ex = x0 + space_w + 36
    draw.rounded_rectangle([ex, y, ex + 166, y + h], radius=h // 2, fill=ACCENT)

def build(radius):
    """Full icon with the given corner radius (Android likes smaller; ico rounder)."""
    grad = vertical_gradient(S, TOP, BOTTOM).convert("RGBA")
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), rounded_mask(S, radius))
    d = ImageDraw.Draw(icon)
    draw_keyboard(d)
    return icon

# ── Android launcher PNGs (legacy icon, all densities) ───────────────────────
android = build(radius=200)
res = os.path.join(ROOT, "android", "GboardRelay", "app", "src", "main", "res")
for dpi, px in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96),
                ("xxhdpi", 144), ("xxxhdpi", 192)):
    d = os.path.join(res, f"mipmap-{dpi}")
    os.makedirs(d, exist_ok=True)
    android.resize((px, px), Image.LANCZOS).save(os.path.join(d, "ic_launcher.png"))
    print(f"  mipmap-{dpi}/ic_launcher.png ({px}px)")

# ── Windows: preview PNG + multi-size .ico ────────────────────────────────────
win = build(radius=230)
win.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "icon.png"))
win.save(os.path.join(HERE, "GboardIME.ico"),
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("  assets/icon.png (512px preview)")
print("  assets/GboardIME.ico (16..256)")
