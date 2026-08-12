# sortify

Sort songs out of your Spotify **input playlists** (where new finds land) into their
**home playlists**, one quick decision at a time.

For each song in an input playlist, sortify suggests the best-matching homes with
reasons ("3 tracks by this artist here", "genre fit: shoegaze, dream pop"). One tap
adds the song to the home playlist *and* removes it from the input. Works from phone
and laptop; runs as a small web app on the box.

## Setup (once)

```sh
cd ~/kode/sortify
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Create a (free) Spotify app for the API:

1. Go to <https://developer.spotify.com/dashboard> → **Create app**.
2. Name/description: anything. Redirect URI: exactly `http://127.0.0.1:8888/callback`.
   Tick **Web API**.
3. Copy the **Client ID** (PKCE flow — there is no secret to handle).

Run the server and connect:

```sh
.venv/bin/sortify
```

Open `http://<box>:8800` from any device on the LAN/Tailscale. Paste the Client ID,
follow the login link, approve. Spotify then redirects your browser to a
`127.0.0.1:8888` page that **fails to load — that's expected** (Spotify only allows
loopback redirect URIs, and nothing listens there). Copy the full URL from the
address bar and paste it back into the setup page. This is a one-time dance; the
refresh token lands in `data/tokens.json` and renews itself.

## Use

The main screen is **Now** — sortify follows whatever you're playing in Spotify.
Listen, think as long as you like, then tap a suggested home (or **More…**): the song
is added there and, when you're playing from an input playlist, removed from the
input. **Remove from input** discards without filing; **Undo** reverses. Same
keyboard shortcuts as triage. Requires the `user-read-currently-playing` scope.

The card also has **capture chips** — one per input playlist — for the opposite
direction: when a new discovery is playing (album, radio, someone else's playlist),
tap `+ [Input]` to drop it into that input for proper sorting later. A ✓ chip means
the song is already in that input.

**Playlists** view holds the roles and the batch mode:

1. Mark your input playlists (**In**) and the playlists you sort
   into (**Home**). Liked Songs can be an input too. With no homes marked, every
   playlist you own is a candidate — but triage refuses that default beyond 40
   playlists, because profiling hundreds of playlists would trip Spotify's rate
   cooldown (dev-mode cooldowns can last many hours; sortify backs off and shows a
   countdown instead of retrying into one).
2. Hit **▶** on an input to triage it. Per song: tap a suggestion, or **More…** to
   pick any home, **Remove only** (discard from input without filing), **Skip**.
   Moving = add to home + remove from input, atomically from your point of view.
   **Undo** reverses the last actions (restored songs re-enter at the playlist end —
   Spotify's API can't restore position).
3. Keyboard on laptop: `1`/`2`/`3` pick a suggestion, `m` more, `r` remove, `s` skip,
   `u` undo.

## Folders

Spotify's Web API doesn't expose playlist folders, so the hierarchy comes from the
desktop client's local cache via
[mikez/spotify-folders](https://github.com/mikez/spotify-folders), run on a machine
with the Spotify app and piped into `POST /api/folders`. House conventions, all in
`data/config.json`: inputs are bracketed `[Name]` playlists (`input_name_pattern`);
homes are playlists under the folders in `home_folder_prefixes` (currently `ROOT`),
minus `home_folder_exclude` segments (ARCHIVED, OLD), minus emoji-prefixed names
(`home_exclude_emoji_names` — 🐾/🧸 super/subset playlists are derived, not filing
destinations), and minus marker-shaped names matching `home_name_exclude_patterns`
(`__start__`-style dunders, `{…}`, `<…>`). Folder paths show up
in the playlist list, suggestion reasons, and the More… picker. Re-run the pipe
after reorganizing folders.

## How suggestions work

Per home playlist we build a profile from its tracks: artist counts and a genre
vector (via artist genres — Spotify's audio-features endpoint is deprecated for new
apps). A song scores by artist overlap (primary signal — a single artist match beats
any pure genre match) plus cosine genre similarity (tiebreaker). Songs already in a
home playlist are flagged so you can just clear them from the input.

The client speaks the **February 2026 dev-mode API**: playlist entries live under
`items`/`item` (not `tracks`/`track`), mutations go to `/playlists/{id}/items`,
Liked Songs mutations to `PUT|DELETE /me/library` with URIs, and batch
`GET /artists?ids=` is gone. See the [migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).

**Rate-limit posture (learned the hard way — two multi-hour cooldowns):** the app
enforces a local budget of 1200 API calls/day and 30/min (`usage.json`), refuses all
calls during a persisted cooldown, and serves every open tab from one shared
currently-playing call (5s TTL playing / 20s idle; the frontend also slows itself
when idle or erroring). Profiles are **genre-lazy**: scoring works from artist
overlap in playlist data alone, needing zero artist lookups; a background enricher
backfills genres a few artists at a time inside budget headroom, so suggestions
sharpen over the first days.

Playlists are cached in `data/cache.json` keyed by Spotify's `snapshot_id`, so only
changed playlists are refetched. First triage of the day fetches every home playlist
and is the slow one.

## Layout

- `sortify/app.py` — FastAPI routes, cache orchestration, undo
- `sortify/spotify.py` — PKCE auth + thin Web API client
- `sortify/suggest.py` — scoring (pure, tested)
- `sortify/store.py` — plain-JSON persistence in `data/` (gitignored)
- `sortify/static/` — vanilla JS/CSS frontend, no build step

Env knobs: `SORTIFY_HOST` (default `0.0.0.0`), `SORTIFY_PORT` (default `8800`),
`SORTIFY_DATA_DIR` (default `./data`).

## Ideas later

- Claude-powered suggestions for songs with no artist/genre signal
- Auto-apply above a confidence threshold, with a review log
- History log of every move (who went where, when)
- systemd unit for always-on serving
