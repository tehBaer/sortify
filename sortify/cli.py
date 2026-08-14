"""spx — budgeted Spotify API access for development.

The only sanctioned way to touch api.spotify.com by hand. Every call goes
through the same client as the app: same daily ledger (data/usage.json),
same rolling-window throttle, same cooldown guard. On top of that,
development traffic refuses itself at DEV_CAP so it can never starve real
usage — the rest of the day's budget belongs to the user.

  spx budget            show today's spend, caps, cooldown
  spx GET /me           one budgeted API call (method optional, GET default)
"""

from __future__ import annotations

import json
import sys
import time

from .spotify import BACKGROUND_DAILY_CAP, DAILY_CAP, Spotify, SpotifyError
from .store import Store

# Dev traffic stops here, leaving the rest of DAILY_CAP for real use. Scaled
# down with DAILY_CAP (was 300 of 1200) to keep that reservation intact.
DEV_CAP = 150


def dev_call_allowed(spent_today: int) -> bool:
    return spent_today < DEV_CAP


def main() -> None:
    sp = Spotify(Store())
    args = sys.argv[1:]

    if not args or args[0] in ("budget", "status"):
        spent = sp.budget_spent()
        cd = max(0.0, sp.effective_cooldown_until() - time.time())
        quiet = max(0.0, sp.quiet_until() - time.time())
        print(f"today: {spent}/{DAILY_CAP} calls spent · dev ceiling {DEV_CAP}")
        print(f"  of which background: {sp.background_spent()}/{BACKGROUND_DAILY_CAP}")
        print("cooldown: none" if cd == 0 else f"cooldown: {int(cd / 60)} min left")
        if cd == 0 and quiet > 0:
            print(f"background quiet period: {int(quiet / 60)} min left")
        return

    method, path = (args[0].upper(), args[1]) if len(args) >= 2 else ("GET", args[0])
    spent = sp.budget_spent()
    if not dev_call_allowed(spent):
        print(f"refused: {spent} calls already spent today ≥ dev ceiling {DEV_CAP}.")
        print("Development traffic stops here — the rest belongs to real usage.")
        sys.exit(2)
    try:
        data = sp.request(method, path)
    except SpotifyError as e:
        print("error:", e)
        sys.exit(1)
    print(json.dumps(data, indent=2, ensure_ascii=False)[:4000])
    print(f"-- spent today: {sp.budget_spent()}/{DAILY_CAP} (dev ceiling {DEV_CAP})", file=sys.stderr)
