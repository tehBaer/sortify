"""Extract the folder tree from the local Spotify desktop client's cache.

The Web API has no folder concept, so this is the only machine-readable
source of the hierarchy. The snap-packaged client on this box keeps its
LevelDB rootlist under ~/snap/spotify/common/.cache/spotify/Users; the
vendored mikez/spotify-folders parser turns it into the same tree JSON
that `POST /api/folders` has always accepted.

None of this touches api.spotify.com: `sync_client` speaks the desktop
client's own sync protocol (not the dev-mode Web API quota), and the
extraction is pure local disk.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import vendor_spotify_folders as vendored

DEFAULT_CACHE = "~/snap/spotify/common/.cache/spotify/Users"

# The client needs long enough after startup to pull a fresh rootlist; 45s
# was comfortable in the 2026-08-23 spike (the rootlist landed within
# seconds of login). Overridable for tests and slower days.
SYNC_SECONDS = int(os.environ.get("SORTIFY_CLIENT_SYNC_SECONDS", "45"))

# A display number nothing else on the box uses (:0 would be a real seat).
_XVFB_DISPLAY = ":93"


def cache_dir() -> str:
    return os.environ.get("SORTIFY_SPOTIFY_CACHE", DEFAULT_CACHE)


def cache_mtime() -> str | None:
    """Newest mtime under the Users cache, as an ISO Zulu stamp.

    This is "how fresh is the tree we can extract" — shown in the UI so a
    stale cache is visible instead of silently trusted.
    """
    root = Path(os.path.expanduser(cache_dir()))
    if not root.exists():
        return None
    newest = 0.0
    for p in root.rglob("*"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    if not newest:
        return None
    return (
        datetime.fromtimestamp(newest, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def extract_tree() -> dict:
    """Parse the client cache into the spotify-folders tree JSON."""
    users = os.path.expanduser(cache_dir())
    if not os.path.isdir(users):
        raise RuntimeError(
            f"no Spotify client cache at {users} — is the desktop client "
            "installed and logged in on this machine?"
        )
    user_id, raw = vendored.get_leveldb_rootlist(None, users)
    if not raw:
        raise RuntimeError(
            "the Spotify client cache holds no rootlist — open the client "
            "once so it syncs, then retry"
        )
    return json.loads(vendored._process(raw, user_id))


def sync_client(seconds: int | None = None) -> None:
    """Run the desktop client headless so it refreshes its local cache.

    Xvfb + `snap run spotify`, wait, terminate. The client is logged in
    already (one-time VNC setup, 2026-08-23); an unattended run is enough
    for it to pull the current rootlist.
    """
    seconds = SYNC_SECONDS if seconds is None else seconds
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb is not installed — cannot run the client headless")
    if not shutil.which("snap"):
        raise RuntimeError("snap is not available — cannot launch the Spotify client")
    xvfb = subprocess.Popen(
        ["Xvfb", _XVFB_DISPLAY, "-screen", "0", "1280x800x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        env = dict(os.environ, DISPLAY=_XVFB_DISPLAY)
        client = subprocess.Popen(
            ["snap", "run", "spotify", "--disable-gpu"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(seconds)
        finally:
            client.terminate()
            try:
                client.wait(timeout=10)
            except subprocess.TimeoutExpired:
                client.kill()
            # The snap wrapper forks; make sure no renderer outlives the run.
            subprocess.run(
                ["pkill", "-f", "/snap/spotify"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(1)
    finally:
        xvfb.terminate()
        try:
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
