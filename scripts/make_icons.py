"""Regenerate the add-to-home-screen icons.

Not part of the served app and not run at build time — the PNGs are committed.
This exists so the icons can be changed without hand-editing binaries. Needs
Pillow, which the app itself does not depend on: `pip install pillow` to run it.

The first version of these icons was the favicon's 🎵 rendered from Noto Color
Emoji — one flat slate fill, RGB (84, 110, 122), with mushy antialiased edges.
This draws the glyph instead of borrowing it: the same beamed note the
dashboard's link row uses. The slate colour itself was kept by choice
(2026-08-30, over the accent green tried in between) — it is deliberately
quiet on the home screen; the crisp redraw is what fixes the smudge.

Two paddings, two icon sets. Android treats a `maskable` icon as a full-bleed
adaptive-icon layer: the launcher zooms the whole canvas to fill its shape and
only the central ~61% circle (66/108dp) is guaranteed visible. A square glyph
zone does NOT survive that — its diagonal corners poke through the circle,
which is exactly the clipping seen on install 2026-08-30. So the manifest now
lists tight icons as `purpose: "any"` and deep-padded ones as `"maskable"`.
Dropping maskable entirely would letterbox the icon onto a white rounded
square, which is its own kind of ugly.
"""

import pathlib

from PIL import Image, ImageDraw

STATIC = pathlib.Path(__file__).resolve().parent.parent / "sortify" / "static"

BG = "#121212"   # the manifest's background_color, so the launch splash is seamless
FG = "#546e7a"   # Noto's 🎵 slate, kept from the original icon
SS = 4           # supersample factor; PIL has no antialiased draw
PAD = 0.20       # `purpose: any` — shown unmasked, modest breathing room
# `purpose: maskable` — the glyph's farthest points (beam tip, notehead
# bottoms) sit on the canvas diagonal at 0.601*(1-2*pad) from center; keeping
# that under the 0.305 visible-circle radius with margin needs pad >= ~0.26.
PAD_MASKABLE = 0.28


def note(size, pad):
    S = size * SS
    im = Image.new("RGB", (S, S), BG)
    d = ImageDraw.Draw(im)
    # feather's 24x24 viewBox mapped into the central (1 - 2*pad) of the canvas
    span = S * (1 - 2 * pad)
    def P(x, y):
        return (S * pad + x / 24 * span, S * pad + y / 24 * span)
    w = int(2.4 / 24 * span)     # stem/beam width
    # Feather's note heads are STROKED circles: r=3 is the centerline, so the
    # filled silhouette has radius 3 + stroke/2. Filling at r=3 leaves the
    # heads too small to swallow the stem ends and their fake caps, which then
    # dangle off each head as a wart.
    r = (3.0 + 2.4 / 2) / 24 * span
    stem = [P(9, 18), P(9, 5), P(21, 3), P(21, 16)]
    d.line(stem, fill=FG, width=w, joint="curve")
    for x, y in stem:            # PIL has no round line caps — fake them
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=FG)
    for cx, cy in ((6, 18), (18, 16)):
        x, y = P(cx, cy)
        d.ellipse([x - r, y - r, x + r, y + r], fill=FG)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for size, name, pad in (
        (192, "icon-192.png", PAD),
        (512, "icon-512.png", PAD),
        (180, "apple-touch-icon.png", PAD),
        (192, "icon-maskable-192.png", PAD_MASKABLE),
        (512, "icon-maskable-512.png", PAD_MASKABLE),
    ):
        note(size, pad).save(STATIC / name)
        print("wrote", name)
