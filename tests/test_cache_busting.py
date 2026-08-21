"""Frontend changes must reach an already-open tab.

index.html loaded /static/app.js unversioned, so a tab opened before a deploy
kept running the old script against the new server. That surfaces as a feature
being "not there" — the user looks for a button the server is serving happily.
Stamping each asset with its own mtime means a changed file is a changed URL.
"""

import re

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


def body(resp) -> str:
    return resp.body.decode()


def test_assets_are_versioned():
    html = body(appmod.index())

    assert re.search(r"/static/app\.js\?v=\d+", html)
    assert re.search(r"/static/style\.css\?v=\d+", html)


def test_the_version_follows_the_file(tmp_path, monkeypatch):
    """A hand-bumped constant is one I will forget on the deploy that matters,
    so the version has to come from the file itself."""
    before = re.search(r"/static/app\.js\?v=(\d+)", body(appmod.index())).group(1)

    real = appmod.STATIC / "app.js"
    monkeypatch.setattr(
        appmod.Path, "stat",
        lambda self: type("S", (), {"st_mtime": 9_999_999})() if self.name == "app.js" else real.stat(),
    )
    after = re.search(r"/static/app\.js\?v=(\d+)", body(appmod.index())).group(1)

    assert before != after


def test_the_page_itself_is_never_cached():
    """Versioned assets are safe to cache hard, but only if the document that
    names them is always re-fetched — otherwise the new URLs never arrive."""
    assert "no-cache" in appmod.index().headers.get("cache-control", "")
