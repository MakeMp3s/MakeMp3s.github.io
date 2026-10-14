"""
api/session.py  —  Vercel serverless function
---------------------------------------------
"One computer per account" record keeper.  Same setup as api/trial.py:
same X-App-Key header, same FIREBASE_SERVICE_ACCOUNT_JSON env var, same
Firestore project.  Nothing new to configure in Vercel.

Endpoints (POST, JSON body):

    action: "register"          (called by the app right after sign-in)
        body: { "firebase_token": "<id token>", "session_token": "<random per-login secret>" }
        The server verifies the Firebase token, takes the uid FROM THE TOKEN,
        and stores a hash of session_token.  Any other computer that was signed in
        to this account now holds an old token and will be signed out on its next check.
        Returns: { "ok": true, "uid": "<uid>" }

    action: "check"             (called by the app about once a minute)
        body: { "uid": "<uid>", "session_token": "<this computer's token>" }
        Returns: { "valid": true/false, "reason": "ok" | "replaced" | "no_session" }

    action: "release"           (called by the app on sign-out)
        body: { "uid": "<uid>", "session_token": "<this computer's token>" }
        Deletes the record ONLY if it still holds this computer's token.
        Returns: { "ok": true, "released": true/false }

All requests must include header:  X-App-Key: <APP_API_KEY>

Firestore schema (collection: sessions):
    Document ID = uid
    Fields:
        session_token_hash : string  (sha256 hex of the session token)
        updated_at         : number  (unix seconds)
"""

from http.server import BaseHTTPRequestHandler
import hashlib
import hmac as _hmac
import json
import os
import time


# ── Lazy Firebase init (Vercel cold starts) ───────────────────────────────────
_fb_app    = None
_firestore = None


def _init_firebase():
    global _fb_app, _firestore
    if _fb_app is not None:
        return

    import firebase_admin
    from firebase_admin import credentials, firestore

    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON env var not set")

    cred       = credentials.Certificate(json.loads(raw))
    _fb_app    = firebase_admin.initialize_app(cred, name="session-api")
    _firestore = firestore.client(_fb_app)


def _verify_firebase_token(id_token: str) -> dict | None:
    """Verify a Firebase ID token and return the decoded payload, or None."""
    try:
        from firebase_admin import auth as fb_auth
        _init_firebase()
        return fb_auth.verify_id_token(id_token, app=_fb_app)
    except Exception:
        return None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_token_text(value) -> bool:
    """A real session token is a UUID-sized string. Reject anything odd or huge."""
    return isinstance(value, str) and 16 <= len(value) <= 200


def _valid_uid(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and "/" not in value


def _handle_register(body: dict) -> tuple[int, dict]:
    firebase_token = body.get("firebase_token", "")
    session_token  = body.get("session_token", "")
    if not firebase_token or not _valid_token_text(session_token):
        return 400, {"error": "firebase_token and session_token required"}

    decoded = _verify_firebase_token(firebase_token)
    if not decoded or not decoded.get("uid"):
        return 401, {"error": "Invalid or expired login token"}
    uid = decoded["uid"]

    _init_firebase()
    _firestore.collection("sessions").document(uid).set({
        "session_token_hash": _hash(session_token),
        "updated_at":         time.time(),
    })
    return 200, {"ok": True, "uid": uid}


def _handle_check(body: dict) -> tuple[int, dict]:
    uid           = body.get("uid", "")
    session_token = body.get("session_token", "")
    if not _valid_uid(uid) or not _valid_token_text(session_token):
        return 400, {"error": "uid and session_token required"}

    _init_firebase()
    doc = _firestore.collection("sessions").document(uid).get()
    if not doc.exists:
        return 200, {"valid": False, "reason": "no_session"}

    stored = doc.to_dict().get("session_token_hash", "")
    if _hmac.compare_digest(stored, _hash(session_token)):
        return 200, {"valid": True, "reason": "ok"}
    return 200, {"valid": False, "reason": "replaced"}


def _handle_release(body: dict) -> tuple[int, dict]:
    uid           = body.get("uid", "")
    session_token = body.get("session_token", "")
    if not _valid_uid(uid) or not _valid_token_text(session_token):
        return 400, {"error": "uid and session_token required"}

    _init_firebase()
    ref = _firestore.collection("sessions").document(uid)
    doc = ref.get()
    if not doc.exists:
        return 200, {"ok": True, "released": False}

    stored = doc.to_dict().get("session_token_hash", "")
    if _hmac.compare_digest(stored, _hash(session_token)):
        ref.delete()
        return 200, {"ok": True, "released": True}
    # Another computer has signed in since: leave its record alone.
    return 200, {"ok": True, "released": False}


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
        if not expected or not _hmac.compare_digest(expected, received):
            self._send_json(401, {"error": "Unauthorized"})
            return

        # ── Parse body ────────────────────────────────────────────────────────
        length = int(self.headers.get("Content-Length", 0))
        if length > 20000:
            self._send_json(413, {"error": "Body too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        action = body.get("action", "")

        try:
            if action == "register":
                status, result = _handle_register(body)
            elif action == "check":
                status, result = _handle_check(body)
            elif action == "release":
                status, result = _handle_release(body)
            else:
                status, result = 400, {"error": f"Unknown action '{action}'"}
        except Exception:
            # Don't leak internals to the caller; the app treats errors as "can't tell" (fail open).
            status, result = 500, {"error": "Server error"}

        self._send_json(status, result)
