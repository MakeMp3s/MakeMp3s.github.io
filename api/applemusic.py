import os
import time
import json
import jwt
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

TEAM_ID = os.environ.get("APPLE_MUSIC_TEAM_ID", "")
KEY_ID = os.environ.get("APPLE_MUSIC_KEY_ID", "")
P8_KEY = os.environ.get("APPLE_MUSIC_P8_KEY", "").replace("\\n", "\n")

def _generate_token():
    now = int(time.time())
    payload = {
        "iss": TEAM_ID,
        "iat": now,
        "exp": now + 15777000  # ~6 months
    }
    return jwt.encode(payload, P8_KEY, algorithm="ES256",
                      headers={"kid": KEY_ID})

def _fetch_tracks(storefront, playlist_id):
    token = _generate_token()
    url = f"https://api.music.apple.com/v1/catalog/{storefront}/playlists/{playlist_id}?include=tracks&limit=100"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    tracks = data["data"][0]["relationships"]["tracks"]["data"]
    results = []
    for t in tracks:
        attr = t.get("attributes", {})
        results.append({
            "title": attr.get("name", ""),
            "artist": attr.get("artistName", "")
        })
    return results

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        storefront = qs.get("storefront", ["us"])[0]
        playlist_id = qs.get("id", [""])[0]

        if not playlist_id:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "missing id"}')
            return

        try:
            tracks = _fetch_tracks(storefront, playlist_id)
            body = json.dumps({"tracks": tracks}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e.code), "detail": body}).encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
