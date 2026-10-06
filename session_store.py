"""Keep a sign-in alive across browser refreshes, in this server process only.

Security model: the browser holds only a random token in a session cookie. The
token is meaningless without this process's memory: the account and the
app-specific password or Gmail OAuth credential stay here and never leave the server (no disk, database,
logs or URLs). Restarting the server forgets every entry, and an entry unused
for IDLE_TIMEOUT seconds expires.
"""

import secrets
import threading
import time

COOKIE = "mail_assistant_session"
IDLE_TIMEOUT = 12 * 3600


class SessionStore:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}

    def _prune(self, now):
        for token in [t for t, e in self._entries.items() if now - e["seen"] > IDLE_TIMEOUT]:
            del self._entries[token]

    def create(self, account, password):
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._entries[token] = {"account": account, "password": password, "seen": now}
        return token

    def lookup(self, token):
        """Return (account, password) for a live token and restart its idle timer."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            entry = self._entries.get(token)
            if entry is None:
                return None
            entry["seen"] = now
            return entry["account"], entry["password"]

    def revoke(self, token):
        with self._lock:
            self._entries.pop(token, None)
