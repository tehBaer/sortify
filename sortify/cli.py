"""spx — budgeted Spotify API access for development.

The only sanctioned way to touch api.spotify.com by hand. Every call goes
through the same client as the app: same daily ledger (data/usage.json),
same rolling-window throttle, same cooldown guard. On top of that,
development traffic refuses itself at DEV_CAP so it can never starve real
usage — the rest of the day's budget belongs to the user.

  spx budget            show today's spend, caps, cooldown
  spx GET /me           one budgeted API call (method optional, GET default)
  spx POST /me/playlists '{"name":"x"}'   same, with a JSON body

The body argument is what makes writes reachable by hand at all. Without it
this tool could only issue reads, so anyone probing a POST or DELETE was
pushed toward curl and a raw token — the exact thing it exists to prevent.
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
    body = None
    if len(args) >= 3:
        try:
            body = json.loads(args[2])
        except json.JSONDecodeError as e:
            # Refuse before spending: a malformed body would burn a real call
            # on a 400, and the day's dev allowance is small.
            print(f"refused: body is not valid JSON ({e})")
            sys.exit(2)
        if not isinstance(body, dict):
            # Every Spotify write body is a JSON object. json.loads happily
            # accepts '5', '"x"' or 'null' as valid JSON too — none of those
            # are a usable request body, and 'null' in particular would
            # silently degrade into sending no body at all (the same as
            # omitting the argument), which is not what a caller who
            # bothered to pass one asked for.
            print(f"refused: body must be a JSON object, got {type(body).__name__}")
            sys.exit(2)

    spent = sp.budget_spent()
    if not dev_call_allowed(spent):
        print(f"refused: {spent} calls already spent today ≥ dev ceiling {DEV_CAP}.")
        print("Development traffic stops here — the rest belongs to real usage.")
        sys.exit(2)
    try:
        data = sp.request(method, path, **({"json": body} if body is not None else {}))
    except SpotifyError as e:
        print("error:", e)
        sys.exit(1)
    print(json.dumps(data, indent=2, ensure_ascii=False)[:4000])
    print(f"-- spent today: {sp.budget_spent()}/{DAILY_CAP} (dev ceiling {DEV_CAP})", file=sys.stderr)
