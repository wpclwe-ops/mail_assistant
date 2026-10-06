"""Desktop OAuth with PKCE, a loopback callback and memory-only credentials."""
import json
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse
from wsgiref.simple_server import make_server, WSGIRequestHandler

from mail_service import MailError

SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def validate_client_config(config):
    if isinstance(config, (str, bytes)):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            raise MailError("gmail_config") from None
    installed = config.get("installed", {}) if isinstance(config, dict) else {}
    if not isinstance(installed, dict):
        raise MailError("gmail_config")
    if not str(installed.get("client_id", "")).endswith(".apps.googleusercontent.com") or not installed.get("client_secret"):
        raise MailError("gmail_config")
    if installed.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth" or installed.get("token_uri") != "https://oauth2.googleapis.com/token":
        raise MailError("gmail_config")
    return config


class GmailCredential:
    provider = "gmail"

    def __init__(self, credentials, account):
        self.credentials = credentials
        self.account = account.strip().lower()
        self.lock = threading.RLock()

    def __repr__(self):
        return "<GmailCredential: memory-only>"

    def api(self):
        from google.auth.transport.requests import AuthorizedSession
        session = AuthorizedSession(self.credentials)
        session.trust_env = False
        return session


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        # OAuth callback URLs contain an authorization code; never log them.
        pass


class OAuthAttempt:
    def __init__(self, config):
        config = validate_client_config(config)
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            raise MailError("gmail_dependencies") from None
        self.flow = InstalledAppFlow.from_client_config(config, [SCOPE], autogenerate_code_verifier=True)
        self.callback = None
        self.cancelled = threading.Event()
        self.path = "/oauth/" + secrets.token_urlsafe(20)
        self.server = make_server("127.0.0.1", 0, self._callback, handler_class=QuietHandler)
        self.server.timeout = 0.5
        self.flow.redirect_uri = f"http://127.0.0.1:{self.server.server_port}{self.path}"
        self.url, self.state = self.flow.authorization_url(access_type="offline", prompt="select_account consent")

    def _callback(self, environ, start_response):
        query = parse_qs(environ.get("QUERY_STRING", ""))
        if environ.get("PATH_INFO") != self.path or query.get("state", [""])[0] != self.state:
            start_response("400 Bad Request", [("Content-Type", "text/plain")])
            return [b"Invalid OAuth callback. Return to Mail Assistant."]
        self.callback = self.flow.redirect_uri + "?" + environ.get("QUERY_STRING", "")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
        return [b"<h2>Return to Mail Assistant</h2><p>The Google sign-in response was received. Check the app to finish.</p>"]

    def cancel(self):
        self.cancelled.set()

    def authenticate(self, progress):
        try:
            progress("gmail_auth", 0, 0)
            deadline = time.monotonic() + 180
            while not self.callback:
                if self.cancelled.is_set():
                    raise MailError("gmail_cancelled")
                if time.monotonic() > deadline:
                    raise MailError("gmail_timeout")
                self.server.handle_request()
            if "error" in parse_qs(urlparse(self.callback).query):
                raise MailError("gmail_denied")
            # Matches Google's official desktop flow: oauthlib requires HTTPS
            # for response parsing; the actual redirect_uri stays loopback HTTP.
            self.flow.fetch_token(authorization_response=self.callback.replace("http://", "https://", 1), timeout=30)
            granted = self.flow.credentials.granted_scopes
            if (granted is not None and SCOPE not in granted) or (granted is None and not self.flow.credentials.has_scopes([SCOPE])):
                raise MailError("gmail_scope")
            candidate = GmailCredential(self.flow.credentials, "")
            with candidate.api() as api:
                response = api.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", timeout=30)
                if response.status_code != 200:
                    raise MailError("gmail_auth")
                account = response.json().get("emailAddress", "")
            if "@" not in account:
                raise MailError("gmail_auth")
            candidate.account = account.lower()
            return candidate
        except MailError:
            raise
        except Exception:
            raise MailError("gmail_auth") from None
        finally:
            self.server.server_close()
            self.flow = None
            self.callback = None
