# Public sortify behind a "Sign in with Spotify" gate — design

Date: 2026-10-16. Status: draft, for review before any code.

## Why

Sortify is reachable only over the tailnet (`tailscale serve` :9800 →
127.0.0.1:8800). On the phone, Tailscale sometimes misbehaves; once the
app's cache had to be cleared before it was responsive again. A Funnel
route would let the phone reach sortify with Tailscale switched off.

Today that would be unsafe: sortify has **no visitor login at all**. One
`Store` and one `Spotify` client serve every request with the owner's
token (`sortify/app.py:74-75`), so anyone with the URL would be inside,
already connected as the owner. A visitor pressing Reconnect would even
replace the owner's token with their own (`finish_auth`,
`sortify/spotify.py:351`).

The user is the only person who uses sortify and does not want a new
password. So the lock is their Spotify account: you are let in if Spotify
says you are the owner.

## What it is

A second, public listener on the same process. Requests arriving on it go
through a gate; the tailnet listener is untouched.

```
phone (no Tailscale) ─https─▶ Funnel :10000 ─▶ 127.0.0.1:8801 ─▶ sortify, GATED, no Claude button
phone (tailnet)      ─https─▶ serve  :9800  ─▶ 127.0.0.1:8800 ─▶ sortify, as today
```

### Why two ports, not one gated port

Both routes reach sortify from 127.0.0.1 (tailscaled is the proxy in both
cases), so the app cannot tell a tailnet request from a public one by
address. Headers can be forged by the client. The **local port a
connection arrived on** cannot: uvicorn records it per connection in the
ASGI `scope["server"]`. One process, two sockets
(`uvicorn.Server.serve(sockets=[s8800, s8801])`), shared state, and the
gate keys on `scope["server"][1] == PUBLIC_PORT`.

This keeps everything about the tailnet route exactly as it is: no login,
the Claude button, the existing Spotify Reconnect flow, boxdash's probe of
port 8800.

The simpler alternative is gating both routes. That costs one extra
login on the tailnet origin and the Claude button everywhere (see the
button rule below). Rejected, since the two-socket version is small.

## The gate (public port only)

### Sign-in flow

1. A request without a valid session cookie:
   - an HTML navigation gets `302 /gate/start`;
   - `/api/*` gets `401 {"gate": true}`. app.js, on that body, sends the
     page to `/gate/start` (distinct from the existing Spotify-reauth
     handling, which is about the owner's token, not the visitor).
2. `/gate/start` makes a PKCE pair and a `state`, puts both in a
   short-lived signed cookie (`sortify_gate_pending`, 10 min, HttpOnly,
   Secure, SameSite=Lax), and redirects to Spotify's authorize URL with
   the stored `client_id`, **no scopes**, and the public redirect URI
   `https://mbp.tail916fc5.ts.net:10000/gate/callback`.
   Nothing is written to `tokens.json`: the pending login state of the
   real Reconnect flow lives there, and the gate must never touch it.
3. `/gate/callback` checks `state` against the pending cookie, exchanges
   the code (accounts.spotify.com), and calls `GET /me` **once** with the
   visitor's access token.
4. If `me.id == gate_owner_id`: set the session cookie and redirect to
   `/`. Otherwise: a plain "this sortify belongs to someone else" page.
   Either way the visitor's tokens are discarded; nothing is stored.

### Session cookie

- `sortify_gate`, HttpOnly, Secure, SameSite=Lax, Path=/.
- Value: `base64(payload).base64(HMAC-SHA256(secret, payload))`, payload
  `{"uid": <spotify id>, "exp": <unix>}`. Stdlib only (`hmac`,
  `hashlib`, `secrets`), the same approach as skannonser's
  `web/guestauth.py`.
- Lifetime 90 days, **sliding**: re-issued when more than a day old, so
  a phone that uses sortify does not get logged out.
- Verification is constant-time (`hmac.compare_digest`), checks `exp`,
  and checks `uid == gate_owner_id` again — so changing the owner id
  invalidates old cookies.
- Revoking everything: rotate the secret.

### Owner identity: pinned, fail closed

`gate_owner_id` in `data/config.json`, set explicitly at deploy from
`cache.json`'s `me.id`. It is **not** read live from the cache: the cache
has been wiped before (the 2026-08-21 live-data clobber), and an empty
owner must mean "nobody gets in", never "anybody does".

### Secret

`SORTIFY_GATE_SECRET` (≥32 bytes, random), loaded by the unit from a
git-ignored `.env` in the repo (`EnvironmentFile=`). No secret, or a short
one → the public listener refuses to start and logs why; the tailnet
listener still runs.

### What the gate lets through without a cookie

- `/gate/start`, `/gate/callback`;
- `/static/manifest.webmanifest` and the icon PNGs — browsers fetch the
  manifest without cookies, so gating it breaks add-to-home-screen.

Everything else on the public port, including `/static/app.js` and the
real `/auth/callback`, is gated. Reconnecting the owner's Spotify login
stays a tailnet-only action (its redirect URI is the tailnet one).

### CSRF

SameSite=Lax keeps the session cookie off cross-site POSTs. As a second
layer, state-changing methods on the public port require an `Origin`
header equal to the public origin.

## Spotify API budget

- One `GET /me` per successful sign-in: a handful of calls a year with
  the sliding cookie.
- It goes through the same ledger, throttle and cooldown guard as every
  other call (a request path that takes an explicit bearer token rather
  than the owner's), so it is counted on the shared account ledger.
- During a cooldown the gate does not call; it shows "Spotify is cooling
  down — sign-in will work again at HH:MM, or use the tailnet address".
  An existing cookie is unaffected, since it needs no call.
- Strangers cannot get this far in practice: a Development Mode app only
  authorises accounts on its allowlist. A separate small cap (20 gate
  verifications per day) bounds it anyway.

## The Claude button

The standing rule says no page reachable from the internet carries the
button. With two ports, `/` on the public port strips the `rc-button.js`
line from index.html (the same place that already stamps asset versions),
and the tailnet port keeps it. The button's script lives on boxdash's
tailnet-only origin and refuses public addresses, so a stray line would
also stay inert. **Needs the user's ruling:** the rule talks about a
*service* reachable from the internet; this design reads it as "no public
page carries the line".

## Funnel port

Funnel allows only 443, 8443 and 10000 per node. 443 (→ :8899) and 8443
(soknatur, → :8830) are taken. **Decided 2026-10-16: sortify takes
10000.** It has been free since skannonser's guest copy was archived on
2026-10-08; if that copy is ever revived, it needs another route (a path
under an existing port, or its own node, below).

More public routes later do not need more ports on this node: each
Funnel port can serve several apps by path (`tailscale funnel
--set-path=/x ...`), at the cost of making the app prefix-aware, and a
tailscale sidecar container joins the tailnet as its own node with its
own hostname and its own three Funnel ports.

## Phone wrinkles

- The public URL is a different origin from the tailnet one, so it is a
  separate home-screen install, with its own `localStorage` (blind-mode
  toggle and the like start fresh).
- From an installed PWA, the trip to accounts.spotify.com opens in a
  browser tab. The cookie lands in the browser's shared cookie jar, so
  the PWA is signed in once that tab returns or is closed.

## Code changes

- `sortify/gate.py` (new): pure helpers. Cookie sign/verify, the PKCE and
  pending cookie, owner check, the route predicate (which paths skip the
  gate). No FastAPI imports, so all of it unit-tests directly.
- `sortify/app.py`: the middleware (no-op unless the request arrived on
  the public port), `/gate/start`, `/gate/callback`, the button strip in
  `index()`, and `main()` binding two sockets.
- `sortify/spotify.py`: the gate's single `/me` call through the existing
  request plumbing with an explicit token.
- `sortify/static/app.js`: on `401 {"gate": true}`, go to `/gate/start`.
- `sortify.service`: `EnvironmentFile=`, `SORTIFY_PUBLIC_PORT=8801`.
- CLAUDE.md: the public route, the gate, and the rule that the public
  port never serves the button.

## Tests (no live calls)

- `tests/test_gate.py`: cookie round-trip; tampered, expired and
  wrong-owner cookies refused; empty owner refuses everyone; sliding
  renewal; state mismatch and missing pending cookie refused; non-owner
  `/me` refused; `/me` not called during a cooldown.
- App-level (TestClient, `scope["server"]` set to the public port vs the
  tailnet port): public HTML → 302, public API → 401 gate body, manifest
  passes, tailnet requests untouched; `/` on the public port has no
  `rc-button.js` line, on the tailnet port it does; `tokens.json` is
  byte-identical after a full gate sign-in.
- A check that `main()`'s two-socket server really reports the arriving
  port in `scope["server"]` (bind two ephemeral ports, one request each).
- `tests/ui_harness.mjs`: a `401 {"gate": true}` from `/api/now` sends
  the page to `/gate/start`; the existing Spotify-reauth card is not
  shown for it.

## Rollout

1. User: add `https://mbp.tail916fc5.ts.net:10000/gate/callback` as a
   redirect URI on the Spotify developer dashboard app.
2. Generate the secret into `.env`; set `gate_owner_id`.
3. Restart sortify, coordinated with other sessions as usual.
4. `tailscale funnel --bg --https=10000 http://127.0.0.1:8801`.
5. Verify from the phone with Tailscale **off**: the gate, a sign-in
   (1 API call), the Now card; and from a private window, that the gate
   holds.

Rollback: `tailscale funnel --https=10000 off`. The public listener can
stay; nothing reaches it.

## Open questions for the user

1. ~~Funnel port 10000 for sortify: OK?~~ Yes (2026-10-16).
2. Is "no public page carries the button" the right reading of the
   button rule for a service with a tailnet port and a public port?
