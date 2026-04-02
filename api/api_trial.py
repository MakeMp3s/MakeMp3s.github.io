"""
api/trial.py  —  Vercel serverless function
--------------------------------------------
Server-side trial record keeper.  Uses your existing Firestore project so
that deleting settings.json on the user's machine has no effect — the trial
record lives here.

Environment variables to set in Vercel dashboard:
    APP_API_KEY                   — same shared key as spotify_proxy
    FIREBASE_SERVICE_ACCOUNT_JSON — paste the ENTIRE contents of your
                                    firebase-service-account.json as a
                                    single-line JSON string.
                                    (Vercel → Settings → Environment Variables
                                     → paste the raw JSON as the value)

Endpoints (POST, JSON body):

    action: "start"
        body: { "machine_id": "<sha256 hex>", "firebase_token": "<id token>" }
        Returns: { "started": true/false, "message": "..." }

    action: "check"
        body: { "machine_id": "<sha256 hex>", "firebase_token": "<id token>" }
        Returns: { "allowed": true/false, "downloads_used": N, "message": "..." }

    action: "increment"
        body: { "machine_id": "<sha256 hex>", "firebase_token": "<id token>" }
        Records one download.
        Returns: { "ok": true, "downloads_used": N }

All requests must include header:  X-App-Key: <APP_API_KEY>

Firestore schema (collection: trials):
    Document ID = machine_id
    Fields:
        started_at      : timestamp
        downloads_used  : number
        firebase_uid    : string   (from verified token — ties record to a real user)
        year            : number   (for annual reset)
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import time
import hmac as _hmac
from datetime import datetime, timezone


# ── Lazy Firebase init (Vercel cold starts) ───────────────────────────────────
_fb_app    = None
_firestore = None

def _init_firebase():
    global _fb_app, _firestore
    if _fb_app is not None:
        return

    import firebase_admin
    from firebase_admin import credentials, firestore, auth as fb_auth

    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON env var not set")

    sa_dict  = json.loads(raw)
    cred     = credentials.Certificate(sa_dict)
    _fb_app  = firebase_admin.initialize_app(cred, name="trial-api")
    _firestore = firestore.client(_fb_app)


def _verify_firebase_token(id_token: str) -> dict | None:
    """Verify a Firebase ID token and return the decoded payload, or None."""
    try:
        from firebase_admin import auth as fb_auth
        _init_firebase()
        return fb_auth.verify_id_token(id_token, app=_fb_app)
    except Exception:
        return None


TRIAL_LIMIT   = 100
TRIAL_SECONDS = 86400  # 24 hours


def _handle_start(body: dict) -> tuple[int, dict]:
    machine_id    = body.get("machine_id", "")
    firebase_token = body.get("firebase_token", "")

    # Verify the user is who they claim to be
    # For anonymous trial users, allow without a token but note the limitation
    uid = None
    if firebase_token:
        decoded = _verify_firebase_token(firebase_token)
        if decoded:
            uid = decoded.get("uid")

    if not machine_id:
        return 400, {"error": "machine_id required"}

    _init_firebase()
    doc_ref = _firestore.collection("trials").document(machine_id)
    doc     = doc_ref.get()

    current_year = datetime.now(timezone.utc).year

    if doc.exists:
        data = doc.to_dict()
        stored_year = data.get("year", 0)

        # Annual reset — a new year means a fresh trial
        if stored_year < current_year:
            doc_ref.set({
                "started_at":     time.time(),
                "downloads_used": 0,
                "firebase_uid":   uid,
                "year":           current_year,
            })
            return 200, {"started": True, "message": "Trial reset for new year. 100 downloads available."}

        # Already used this year
        return 200, {"started": False, "message": "Trial already used on this device"}

    # First time — create the record
    doc_ref.set({
        "started_at":     time.time(),
        "downloads_used": 0,
        "firebase_uid":   uid,
        "year":           current_year,
    })
    return 200, {"started": True, "message": "24-hour trial started! You have 100 downloads."}


def _handle_check(body: dict) -> tuple[int, dict]:
    machine_id = body.get("machine_id", "")
    if not machine_id:
        return 400, {"error": "machine_id required"}

    _init_firebase()
    doc = _firestore.collection("trials").document(machine_id).get()

    if not doc.exists:
        return 200, {"allowed": False, "downloads_used": 0,
                     "message": "No trial record found. Start a trial first."}

    data           = doc.to_dict()
    downloads_used = data.get("downloads_used", 0)
    started_at     = data.get("started_at", 0)
    elapsed        = time.time() - started_at

    if downloads_used >= TRIAL_LIMIT:
        return 200, {"allowed": False, "downloads_used": downloads_used,
                     "message": f"Trial limit reached ({TRIAL_LIMIT} downloads)."}

    if elapsed > TRIAL_SECONDS:
        return 200, {"allowed": False, "downloads_used": downloads_used,
                     "message": "Trial expired (24 hours elapsed)."}

    remaining = TRIAL_LIMIT - downloads_used
    return 200, {"allowed": True, "downloads_used": downloads_used,
                 "message": f"{remaining} trial downloads remaining."}


def _handle_increment(body: dict) -> tuple[int, dict]:
    machine_id = body.get("machine_id", "")
    if not machine_id:
        return 400, {"error": "machine_id required"}

    _init_firebase()
    from google.cloud import firestore as fs_module

    doc_ref = _firestore.collection("trials").document(machine_id)

    # Atomic increment — safe under concurrent downloads
    doc_ref.update({"downloads_used": fs_module.Increment(1)})
    updated = doc_ref.get().to_dict()
    return 200, {"ok": True, "downloads_used": updated.get("downloads_used", 0)}


# ── Vercel handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # ── Auth check ────────────────────────────────────────────────────────
        expected = os.environ.get("APP_API_KEY", "")
        received = self.headers.get("X-App-Key", "")
        if not _hmac.compare_digest(expected, received):
            self._send_json(401, {"error": "Unauthorized"})
            return

        # ── Parse body ────────────────────────────────────────────────────────
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        action = body.get("action", "")

        try:
            if action == "start":
                status, result = _handle_start(body)
            elif action == "check":
                status, result = _handle_check(body)
            elif action == "increment":
                status, result = _handle_increment(body)
            else:
                status, result = 400, {"error": f"Unknown action '{action}'"}
        except Exception as e:
            status, result = 500, {"error": str(e)}

        self._send_json(status, result)
