"""Add-to-home-screen support: manifest, icons, theme-color.

The Now tab is used from a phone while listening; without these tags it opens
as a plain browser tab (URL bar eating vertical space, generic icon, status
bar clashing with the dark background). No service worker on purpose: the app
is meaningless without its server, and a stale cached app.js is exactly the
failure mode the ?v= stamping exists to prevent.
"""

import json
import struct

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

STATIC = appmod.STATIC


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def test_manifest_is_valid_and_its_icons_exist():
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    sizes = set()
    for icon in manifest["icons"]:
        # src carries a ?v= cache-buster so phones refetch after a redraw
        path = STATIC / icon["src"].removeprefix("/static/").split("?")[0]
        w, h = png_size(path)
        assert f"{w}x{h}" == icon["sizes"]
        sizes.add(icon["sizes"])
    assert {"192x192", "512x512"} <= sizes


def test_index_declares_the_pwa_surface():
    html = (STATIC / "index.html").read_text()
    assert 'rel="manifest"' in html
    # Both themes: the status bar must match whichever palette is active.
    assert html.count('name="theme-color"') == 2
    assert "#121212" in html and "#fafafa" in html
    # iOS ignores most of the manifest; it needs its own tags for standalone.
    assert 'name="apple-mobile-web-app-capable"' in html
    assert 'rel="apple-touch-icon"' in html
    assert png_size(STATIC / "apple-touch-icon.png") == (180, 180)
