"""Look at what the split-into-parts engine suggests for real homes.

Zero API calls: reads data/cache.json's cached track lists and the three
Last.fm files, and prints part sizes, names and cleanliness scores. Used to
tune the constants in sortify/splitparts.py once, with the user, before
they are frozen.

    .venv/bin/python scripts/eval_splitparts.py [--n 2 3] [--limit 5] [--id PID ...]
"""

from __future__ import annotations

import argparse
import time

from sortify import splitparts as spp
from sortify.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="+", default=[2, 3])
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--id", nargs="*", default=None)
    args = ap.parse_args()

    store = Store()
    cache = store.cache()
    cfg = store.config()
    listing = {p["id"]: p for p in (cache.get("playlist_list") or {}).get("items") or []}
    homes = args.id or [h for h in cfg.get("home_ids") or []
                        if h in cache["playlists"] and h in listing]
    homes = sorted(homes, key=lambda h: -len(cache["playlists"][h]["tracks"]))[:args.limit]
    tags, tmap, amap = store.tag_artists(), store.lastfm_track_map(), store.lastfm_artist_map()
    for pid in homes:
        tracks = cache["playlists"][pid]["tracks"]
        print(f"\n{listing.get(pid, {}).get('name', pid)} — {len(tracks)} songs")
        for n in args.n:
            t0 = time.monotonic()
            try:
                s = spp.suggest(tracks, n, tags, tmap, amap)
            except spp.SplitError as exc:
                print(f"  n={n}: {exc}")
                continue
            took = time.monotonic() - t0
            sizes = " / ".join(f"{len(p)} {name!r}" for p, name in zip(s.parts, s.names))
            print(f"  n={n} ({took:.1f}s): {sizes}; unknown {len(s.unknown)}; "
                  f"scores {s.scores}; hint {s.hint}")


if __name__ == "__main__":
    main()
