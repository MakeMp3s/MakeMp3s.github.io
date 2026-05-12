import os
import time
import json
import jwt
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

TEAM_ID = os.environ.get("APPLE_MUSIC_TEAM_ID", "")
KEY_ID  = os.environ.get("APPLE_MUSIC_KEY_ID", "")
P8_KEY  = os.environ.get("APPLE_MUSIC_P8_KEY", "").replace("\\n", "\n")

def _generate_token():
    now = int(time.time())
    payload = {
        "iss": TEAM_ID,
        "iat": now,
        "exp": now + 15777000  # ~6 months
    }
    return jwt.encode(payload, P8_KEY, algorithm="ES256",
                      headers={"kid": KEY_ID})

def _api_get(url, token):
    """Make a single authenticated GET to the Apple Music API."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def _fetch_all_tracks(storefront, playlist_id):
    """
    Fetch all tracks for a playlist, following pagination via the `next` field.
    Apple Music returns max 100 tracks per page; we keep fetching until `next`
    is absent, mirroring the Spotify proxy's _fetch_paginated_playlist pattern.
    """
    token = _generate_token()

    # First page
    first_url = (
        f"https://api.music.apple.com/v1/catalog/{storefront}"
        f"/playlists/{playlist_id}?include=tracks"
    )
    data       = _api_get(first_url, token)
    tracks_rel = data["data"][0]["relationships"]["tracks"]
    all_tracks = list(tracks_rel.get("data", []))

    # Paginate via `next`
    next_path = tracks_rel.get("next")
    while next_path:
        next_url  = f"https://api.music.apple.com{next_path}"
        page      = _api_get(next_url, token)
        all_tracks.extend(page.get("data", []))
        next_path = page.get("next")

    results = []
    for t in all_tracks:
        attr = t.get("attributes", {})
        results.append({
            "title":  attr.get("name", ""),
            "artist": attr.get("artistName", "")
        })
    return results


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default logging

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
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
