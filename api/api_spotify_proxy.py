"""
api/spotify_proxy.py  —  Vercel serverless function
----------------------------------------------------
Proxies Spotify search and metadata requests so the app never needs to
hold Spotify credentials.  Deploy this in your Vercel project's api/ folder.

Environment variables to set in Vercel dashboard (Settings → Environment):
    SPOTIFY_CLIENT_ID      — your Spotify app client ID
    SPOTIFY_CLIENT_SECRET  — your Spotify app client secret
    APP_API_KEY            — any long random string you choose, e.g. from:
                             python -c "import secrets; print(secrets.token_hex(32))"
                             Copy the same value into APP_API_KEY in the app.

Endpoints (all GET):
    /api/spotify_proxy?action=search&q=Bohemian+Rhapsody&type=track
    /api/spotify_proxy?action=track&id=<spotify_track_id>
    /api/spotify_proxy?action=playlist&id=<spotify_playlist_id>
    /api/spotify_proxy?action=album&id=<spotify_album_id>

All requests must include the header:
    X-App-Key: <APP_API_KEY>
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import time
import threading
import requests as _requests


# ── In-process token cache (survives warm lambda reuse on Vercel) ─────────────
_token_lock   = threading.Lock()
_access_token: str | None = None
_token_expiry: float = 0.0


def _get_spotify_token() -> str:
    """Fetch (or return cached) Spotify client-credentials token."""
    global _access_token, _token_expiry

    with _token_lock:
        if _access_token and time.time() < _token_expiry - 30:
            return _access_token

        client_id     = os.environ["SPOTIFY_CLIENT_ID"]
        client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]

        resp = _requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        _access_token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 3600)
        return _access_token


def _spotify_get(path: str, params: dict | None = None) -> dict:
    """Make an authenticated GET request to the Spotify Web API."""
    token = _get_spotify_token()
    resp = _requests.get(
        f"https://api.spotify.com/v1/{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


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
        # ── Auth check ────────────────────────────────────────────────────────
        expected_key = os.environ.get("APP_API_KEY", "")
        received_key = self.headers.get("X-App-Key", "")

        # Constant-time comparison prevents timing attacks
        import hmac as _hmac
        if not _hmac.compare_digest(expected_key, received_key):
            self._send_json(401, {"error": "Unauthorized"})
            return

        # ── Route ─────────────────────────────────────────────────────────────
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        action = (qs.get("action") or [""])[0]

        try:
            if action == "search":
                q         = (qs.get("q") or [""])[0]
                item_type = (qs.get("type") or ["track"])[0]
                limit     = int((qs.get("limit") or ["10"])[0])

                if not q:
                    self._send_json(400, {"error": "Missing q parameter"})
                    return

                data = _spotify_get("search", {
                    "q": q, "type": item_type, "limit": min(limit, 50)
                })
                self._send_json(200, data)

            elif action == "track":
                track_id = (qs.get("id") or [""])[0]
                if not track_id:
                    self._send_json(400, {"error": "Missing id parameter"})
                    return
                self._send_json(200, _spotify_get(f"tracks/{track_id}"))

            elif action == "playlist":
                playlist_id = (qs.get("id") or [""])[0]
                if not playlist_id:
                    self._send_json(400, {"error": "Missing id parameter"})
                    return
                # Fetch all tracks via pagination
                playlist  = _spotify_get(f"playlists/{playlist_id}")
                tracks    = playlist.get("tracks", {})
                all_items = list(tracks.get("items", []))

                next_url = tracks.get("next")
                while next_url:
                    token = _get_spotify_token()
                    page  = _requests.get(
                        next_url,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                    ).json()
                    all_items.extend(page.get("items", []))
                    next_url = page.get("next")

                playlist["tracks"]["items"] = all_items
                self._send_json(200, playlist)

            elif action == "album":
                album_id = (qs.get("id") or [""])[0]
                if not album_id:
                    self._send_json(400, {"error": "Missing id parameter"})
                    return
                self._send_json(200, _spotify_get(f"albums/{album_id}"))

            else:
                self._send_json(400, {"error": f"Unknown action '{action}'"})

        except _requests.HTTPError as e:
            self._send_json(
                e.response.status_code,
                {"error": f"Spotify API error: {e.response.status_code}"}
            )
        except Exception as e:
            self._send_json(500, {"error": str(e)})
