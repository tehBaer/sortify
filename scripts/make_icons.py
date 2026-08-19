"""Regenerate the add-to-home-screen icons.

Not part of the served app and not run at build time — the PNGs are committed.
This exists so the icons can be changed without hand-editing binaries. Needs
Pillow, which the app itself does not depend on: `pip install pillow` to run it.

The first version of these icons was the favicon's 🎵 rendered from Noto Color
Emoji, and that is why they looked like a smudge on a home screen: Noto draws
U+1F3B5 as one flat slate fill, RGB (84, 110, 122), which on the app's near-black
has almost no contrast at 48dp. This draws the glyph instead of borrowing it —
the same beamed note the dashboard's link row uses, in the app's accent green.

The glyph sits inside the central 60% of the canvas, so it survives Android's
adaptive-icon mask (safe zone is the central 80% circle) and the manifest can
claim `maskable`. Without that claim launchers letterbox the icon onto a white
rounded square, which is its own kind of ugly.
"""

import pathlib

from PIL import Image, ImageDraw

STATIC = pathlib.Path(__file__).resolve().parent.parent / "sortify" / "static"

BG = "#121212"   # the manifest's background_color, so the launch splash is seamless
FG = "#1db954"   # --accent
SS = 4           # supersample factor; PIL has no antialiased draw
PAD = 0.20       # keeps the glyph inside the maskable safe zone


def note(size):
    S = size * SS
    im = Image.new("RGB", (S, S), BG)
    d = ImageDraw.Draw(im)
    # feather's 24x24 viewBox mapped into the central (1 - 2*PAD) of the canvas
    span = S * (1 - 2 * PAD)
    def P(x, y):
        return (S * PAD + x / 24 * span, S * PAD + y / 24 * span)
    w = int(2.4 / 24 * span)     # stem/beam width
    r = 3.0 / 24 * span          # note-head radius
    stem = [P(9, 18), P(9, 5), P(21, 3), P(21, 16)]
    d.line(stem, fill=FG, width=w, joint="curve")
    for x, y in stem:            # PIL has no round line caps — fake them
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=FG)
    for cx, cy in ((6, 18), (18, 16)):
        x, y = P(cx, cy)
        d.ellipse([x - r, y - r, x + r, y + r], fill=FG)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for size, name in ((192, "icon-192.png"), (512, "icon-512.png"),
                       (180, "apple-touch-icon.png")):
        note(size).save(STATIC / name)
        print("wrote", name)
