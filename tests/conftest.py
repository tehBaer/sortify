"""Bind the data directory before anything imports sortify.app.

app.py builds its Store and Spotify client at module scope, so a bare import
would otherwise bind to the real data/ — reading tokens.json and, worse,
writing the live budget ledger. Tests get a throwaway directory instead.

The same hazard applies to the shared account ledger (~/kode/spotify-ledger):
it is one file for sortify, playlistener and spotify-autoqueuer, so a test
suite spending against the real one would eat the day's allowance for all
three and could park a cooldown on the account.
"""

import os
import tempfile

os.environ["SORTIFY_DATA_DIR"] = tempfile.mkdtemp(prefix="sortify-tests-")
os.environ["SPOTIFY_ACCOUNT_LEDGER"] = os.path.join(
    tempfile.mkdtemp(prefix="sortify-ledger-"), "account-ledger.json"
)
# clientui's find_text saves the screen it failed on — real forensics for a
# live UI run. Unit tests exercise that failure path with toy images, which
# must not clobber a real run's saved evidence.
os.environ["SORTIFY_CLIENTUI_FAIL_SHOT"] = os.path.join(
    tempfile.mkdtemp(prefix="sortify-failshot-"), "fail.png"
)

import pytest  # noqa: E402  (must follow the env binding above)


@pytest.fixture(autouse=True)
def isolated_account_ledger(tmp_path, monkeypatch):
    """One ledger per test.

    AccountLedger resolves its path per instance, so setting the variable here
    is enough — but only because it does. If that ever moves back to an
    import-time constant, every test in the suite starts sharing one ledger and
    the cap tests begin failing in whatever order pytest happens to run them.
    """
    monkeypatch.setenv("SPOTIFY_ACCOUNT_LEDGER", str(tmp_path / "account-ledger.json"))


@pytest.fixture(autouse=True)
def no_real_deezer(monkeypatch):
    """No test may reach the real Deezer API through the app's client factory.

    The preview routes (`/api/playlist_preview`, the hold-to-preview player)
    construct a real `Deezer()` through `_deezer_client` and would hit
    api.deezer.com from any route test that exercises them without faking the
    factory — this raise turns that into a loud failure instead of live
    network traffic. Tests that need a working client monkeypatch
    `_deezer_client` themselves, which overrides this guard; tests of the
    `Deezer` class itself construct it directly with a fake transport and
    never touch the factory.
    """
    import sortify.app as appmod

    def _blocked():
        raise AssertionError("test reached the real Deezer client — fake appmod._deezer_client")

    monkeypatch.setattr(appmod, "_deezer_client", _blocked)
