"""
api/applemusic.py  —  Vercel serverless function
-------------------------------------------------
Proxies Apple Music catalog requests so the app never needs to hold
Apple Music credentials. Deploy in your Vercel project's api/ folder.

Environment variables to set in Vercel dashboard (Settings → Environment):
    APPLE_MUSIC_TEAM_ID   — your Apple Developer Team ID
    APPLE_MUSIC_KEY_ID    — your MusicKit Key ID
    APPLE_MUSIC_P8_KEY    — full contents of your .p8 private key file
    APP_API_KEY           — same shared secret used by spotify_proxy

Endpoint (GET):
    /api/applemusic?storefront=us&id=pl.abc123
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import time
import threading
import urllib.request
import urllib.error
import jwt


# ── Credentials ───────────────────────────────────────────────────────────────
TEAM_ID = os.environ.get("APPLE_MUSIC_TEAM_ID", "")
KEY_ID  = os.environ.get("APPLE_MUSIC_KEY_ID", "")
P8_KEY  = os.environ.get("APPLE_MUSIC_P8_KEY", "").replace("\\n", "\n")

# ── In-process token cache ────────────────────────────────────────────────────
# Apple Music JWTs last up to 6 months — no need to regenerate per request.
_token_lock   = threading.Lock()
_cached_token: str | None = None
_token_expiry: float = 0.0

# ── Short-TTL playlist cache — mirrors Spotify proxy pattern ──────────────────
# Keyed by "storefront:playlist_id" -> {"data": list, "expires": float}
# 60-second TTL absorbs bursts of the same playlist without hammering Apple's API.
_playlist_cache: dict = {}
_playlist_cache_lock  = threading.Lock()
PLAYLIST_CACHE_TTL    = 60  # seconds

# ── Retry budget for rate-limited requests ────────────────────────────────────
RETRY_MAX_SECONDS = 40


def _get_token() -> str:
    """Return a cached JWT developer token, generating a new one only when expired."""
    global _cached_token, _token_expiry

    with _token_lock:
        if _cached_token and time.time() < _token_expiry - 60:
            return _cached_token

        exp = int(time.time()) + 15777000  # ~6 months
        payload = {
            "iss": TEAM_ID,
            "iat": int(time.time()),
            "exp": exp,
        }
        _cached_token = jwt.encode(
            payload, P8_KEY, algorithm="ES256", headers={"kid": KEY_ID}
        )
        _token_expiry = exp
        return _cached_token


def _api_get(url: str) -> dict:
    """
    Make a single authenticated GET to the Apple Music API.
    Retries on 429 using Retry-After header, up to RETRY_MAX_SECONDS total.
    All other errors are raised immediately.
    """
    deadline = time.time() + RETRY_MAX_SECONDS

    while True:
        token = _get_token()
        req   = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise  # surface non-429 errors immediately

            # 429: read Retry-After, check budget, sleep and retry
            retry_after = int(e.headers.get("Retry-After", 2))
            remaining   = deadline - time.time()

            if remaining <= 0 or retry_after > remaining:
                raise  # out of retry budget — surface the 429

            time.sleep(retry_after)


def _fetch_all_tracks(storefront: str, playlist_id: str) -> list:
    """
    Fetch all tracks for a playlist, following pagination via the `next` field.
    Uses in-process cache to absorb concurrent requests for the same playlist.
    Mirrors _fetch_paginated_playlist in spotify_proxy.py.
    """
    cache_key = f"{storefront}:{playlist_id}"

    # Return cached response if still fresh
    with _playlist_cache_lock:
        cached = _playlist_cache.get(cache_key)
        if cached and time.time() < cached["expires"]:
            return cached["data"]

    # Cache miss — fetch from Apple Music API
    first_url  = (
        f"https://api.music.apple.com/v1/catalog/{storefront}"
        f"/playlists/{playlist_id}?include=tracks"
    )
    data       = _api_get(first_url)
    tracks_rel = data["data"][0]["relationships"]["tracks"]
    all_tracks = list(tracks_rel.get("data", []))

    # Follow pagination
    next_path = tracks_rel.get("next")
    while next_path:
        page       = _api_get(f"https://api.music.apple.com{next_path}")
        all_tracks.extend(page.get("data", []))
        next_path  = page.get("next")

    # Normalise to title/artist/id dicts
    results = []
    for t in all_tracks:
        attr = t.get("attributes", {})
        results.append({
            "id":     t.get("id", ""),
            "title":  attr.get("name", ""),
            "artist": attr.get("artistName", "")
        })

    # Store in cache
    with _playlist_cache_lock:
        _playlist_cache[cache_key] = {
            "data":    results,
            "expires": time.time() + PLAYLIST_CACHE_TTL,
        }

    return results


# ── Vercel handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default logging

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # ── Auth check (mirrors spotify_proxy) ────────────────────────────────
        import hmac as _hmac
        expected_key = os.environ.get("APP_API_KEY", "")
        received_key = self.headers.get("X-App-Key", "")
        if not _hmac.compare_digest(expected_key, received_key):
            self._send_json(401, {"error": "Unauthorized"})
            return

        # ── Parse params ──────────────────────────────────────────────────────
        qs          = parse_qs(urlparse(self.path).query)
        storefront  = qs.get("storefront", ["us"])[0]
        playlist_id = qs.get("id", [""])[0]

        if not playlist_id:
            self._send_json(400, {"error": "missing id"})
            return

        try:
            tracks = _fetch_all_tracks(storefront, playlist_id)
            self._send_json(200, {"tracks": tracks})
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            self._send_json(e.code, {"error": str(e.code), "detail": body})
        except Exception as e:
            self._send_json(500, {"error": str(e)})
