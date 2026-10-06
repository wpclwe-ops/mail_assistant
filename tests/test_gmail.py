import base64
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, PropertyMock
from urllib.parse import parse_qs, urlparse
from streamlit.testing.v1 import AppTest
import requests
import storage
import gmail_service as gmail
import gmail_auth
import mail_provider
import mail_service
from all_messages import action_preview
from session_store import SessionStore, COOKIE

CONFIG = {"installed": {
    "client_id": "fake-test.apps.googleusercontent.com", "client_secret": "test-client-secret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"],
}}


def data(uid, sender="news@amazon.com", name="Amazon", labels=None):
    return dict(id=uid, internalDate="1791300000000", labelIds=labels or ["INBOX", "UNREAD"], snippet="Preview text",
                payload={"headers": [dict(name=k, value=v) for k, v in {
                    "From": f"{name} <{sender}>", "Subject": "Sale " + uid, "Message-ID": f"<{uid}@test>",
                    "List-ID": "offers", "List-Unsubscribe": "<https://example.com/unsubscribe>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                }.items()]})


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
    def json(self):
        return copy.deepcopy(self.payload)


class FakeAPI:
    def __init__(self):
        self.items = {uid: data(uid) for uid in ["1a", "2b", "3c"]}
        self.calls = []
        self.fail = None
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def request(self, method, url, **kwargs):
        path = url.removeprefix(gmail.BASE)
        self.calls.append((method, path, kwargs))
        if self.fail and method == "POST":
            raise requests.Timeout()
        if path == "/profile":
            return Response({"emailAddress": "test@gmail.com"})
        if path == "/messages":
            params = kwargs.get("params", {})
            if params.get("pageToken"):
                return Response({"messages": [{"id": "3c"}]})
            return Response({"messages": [{"id": "1a"}, {"id": "2b"}], "nextPageToken": "next"})
        uid = path.split("/")[2]
        if uid not in self.items:
            return Response({}, 404)
        item = self.items[uid]
        if method == "GET":
            if kwargs.get("params", {}).get("format") == "raw":
                return Response({"raw": base64.urlsafe_b64encode(b"Subject: Sale\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello <script>literal</script>").decode().rstrip("=")})
            return Response(item)
        if path.endswith("/trash"):
            item["labelIds"] = [x for x in item["labelIds"] if x != "INBOX"] + ["TRASH"]
        elif path.endswith("/untrash"):
            item["labelIds"] = [x for x in item["labelIds"] if x != "TRASH"]
        elif path.endswith("/modify"):
            body = kwargs["json"]
            item["labelIds"] = list((set(item["labelIds"]) | set(body["addLabelIds"])) - set(body["removeLabelIds"]))
        else:
            raise AssertionError("Unexpected mutating endpoint: " + path)
        return Response(item)
    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "gmail.db"
        self.db_patch = patch.object(storage, "DB_PATH", self.db)
        self.db_patch.start()
        self.store = storage.Store("gmail:test@gmail.com")
        self.api = FakeAPI()
        self.credential = gmail_auth.GmailCredential(None, "test@gmail.com")
        self.api_patch = patch.object(gmail_auth.GmailCredential, "api", lambda credential: self.api)
        self.api_patch.start()
        self.progress = lambda *args: None
    def tearDown(self):
        self.api_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()
    def scan(self):
        return mail_provider.scan(self.store, self.credential, 0, self.progress)
    def app(self, signed_in=True):
        at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10)
        if signed_in:
            at.session_state["account"] = self.store.account
            at.session_state["password"] = self.credential
        at.run()
        self.assertFalse(at.exception)
        return at

    def test_scan_pagination_metadata_and_account_isolation(self):
        self.assertEqual(self.scan(), {"scanned": 3, "total": 3})
        messages = self.store.scan()["messages"]
        self.assertEqual({m["uid"] for m in messages}, {"1a", "2b", "3c"})
        self.assertTrue(all(m["unread"] for m in messages))
        self.assertIsNone(storage.Store("test@gmail.com").scan())
        self.assertTrue(all(c[2].get("params", {}).get("labelIds") == "INBOX" for c in self.api.calls if c[1] == "/messages"))

    def test_reader_keeps_unread_and_renders_literal_text(self):
        self.scan()
        item = self.store.scan()["messages"][0]
        result = mail_provider.read_message(self.store, self.credential, self.store.scan()["validity"], item, self.progress)
        self.assertIn("<script>literal</script>", result["text"])
        self.assertTrue("UNREAD" in self.api.items[item["uid"]]["labelIds"])
        self.assertFalse(any(method != "GET" for method, path, args in self.api.calls))

    def test_delete_exact_messages_history_and_undo(self):
        self.scan()
        preview = action_preview(self.store, self.store.scan()["validity"], ["1a"])
        result = mail_provider.execute(self.store, self.credential, preview, ["1a"], self.progress)
        self.assertEqual(result["moved"], 1)
        self.assertIn("TRASH", self.api.items["1a"]["labelIds"])
        self.assertIn("INBOX", self.api.items["2b"]["labelIds"])
        self.assertEqual(len(self.store.scan()["messages"]), 2)
        self.assertEqual(self.store.history()[0]["status"], "done")
        restored = mail_provider.undo(self.store, self.credential, self.progress)
        self.assertEqual(restored["restored"], 1)
        self.assertIn("INBOX", self.api.items["1a"]["labelIds"])
        self.assertNotIn("TRASH", self.api.items["1a"]["labelIds"])
        self.assertIsNone(self.store.scan())

    def test_stale_identity_and_account_stop_before_side_effect(self):
        self.scan()
        preview = action_preview(self.store, self.store.scan()["validity"], ["1a"], "unsubscribe_delete")
        self.api.items["1a"]["payload"]["headers"][1]["value"] = "Changed"
        with patch.object(mail_service, "one_click") as unsub:
            result = gmail.execute(self.store, self.credential, preview, ["1a"], self.progress)
            self.assertEqual(result["error"], "stale")
            unsub.assert_not_called()
        self.assertFalse(any(method == "POST" for method, path, args in self.api.calls))
        with self.assertRaisesRegex(mail_service.MailError, "stale"):
            gmail.scan(storage.Store("gmail:other@gmail.com"), self.credential, 0, self.progress)

    def test_whitelist_and_uncertain_network_result(self):
        self.scan()
        preview = action_preview(self.store, self.store.scan()["validity"], ["1a"])
        self.store.policy(["news@amazon.com"], "white")
        with self.assertRaisesRegex(mail_service.MailError, "protected"):
            gmail.execute(self.store, self.credential, preview, ["1a"], self.progress)
        self.store.policy(["news@amazon.com"], "")
        self.api.fail = True
        result = gmail.execute(self.store, self.credential, preview, ["1a"], self.progress)
        self.assertEqual(result["error"], "gmail_network")
        self.assertEqual(result["moved"], 0)
        self.assertEqual(self.store.last_moves()[0]["state"], "uncertain")
        self.assertEqual(self.store.history()[0]["status"], "partial")

    def test_unsubscribe_only_does_not_trash(self):
        self.scan()
        preview = action_preview(self.store, self.store.scan()["validity"], ["1a"], "unsubscribe_only")
        with patch.object(mail_service, "one_click", return_value=("requested", "HTTP 200")):
            result = gmail.execute(self.store, self.credential, preview, [], self.progress)
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["moved"], 0)
        self.assertFalse(any(method == "POST" for method, path, args in self.api.calls))

    def test_gmail_ui_hex_ids_grouping_selection_and_delete_no_replay(self):
        self.scan()
        at = self.app()
        at.checkbox(key="inbox_uid_1a").check().run()
        at.selectbox(key="mail_display").set_value("company_count").run()
        group_button = next(b for b in at.button if b.key and b.key.startswith("company_open_"))
        group_button.click().run()
        at.button(key="inbox_review").click().run()
        at.button(key="inbox_confirm").click().run()
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(len([x for x in self.api.calls if x[1].endswith("/trash")]), 1)

    def test_provider_choice_has_separate_instructions_and_no_google_password(self):
        at = self.app(False)
        self.assertEqual(len(at.text_input), 0)
        at.selectbox(key="login_provider").set_value("gmail").run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.text_input), 0)
        self.assertTrue(at.button(key="gmail_signin").disabled)
        self.assertTrue(any("Desktop app" in m.value for m in at.markdown))
        at.selectbox(key="login_provider").set_value("icloud").run()
        self.assertEqual(len(at.text_input), 2)

    def test_gmail_sign_in_refresh_and_sign_out(self):
        class Attempt:
            url = "https://accounts.google.com/o/oauth2/auth?client_id=fake"
            def authenticate(inner, progress):
                return self.credential
        with patch("gmail_login_ui.Path") as path, patch("gmail_login_ui.OAuthAttempt", return_value=Attempt()):
            path.return_value.with_name.return_value.read_bytes.return_value = json.dumps(CONFIG).encode()
            path.return_value.with_name.return_value.stat.return_value.st_size = 100
            at = self.app(False)
            at.selectbox(key="login_provider").set_value("gmail").run()
            at.button(key="gmail_signin").click().run()
            if at.session_state["job"]:
                at.session_state["job"].thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state["account"], self.store.account)
            token = at.session_state["session_token"]
            with patch("streamlit.runtime.context.ContextProxy.cookies", new_callable=PropertyMock) as cookies:
                cookies.return_value = {COOKIE: token}
                again = self.app(False)
            self.assertEqual(again.session_state["account"], self.store.account)
            self.assertIs(again.session_state["password"], self.credential)
            at.button(key="signout").click().run()
            self.assertNotIn("account", at.session_state)
            self.assertEqual(at.selectbox(key="login_provider").value, "choose")

    def test_client_config_validation_pkce_cancel_and_oauth_state(self):
        for config in [{}, {"web": CONFIG["installed"]}, {"installed": []}, dict(installed=dict(CONFIG["installed"], token_uri="https://evil.example/token"))]:
            with self.assertRaises(mail_service.MailError):
                gmail_auth.validate_client_config(config)
        attempt = gmail_auth.OAuthAttempt(CONFIG)
        query = parse_qs(urlparse(attempt.url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(urlparse(attempt.flow.redirect_uri).hostname, "127.0.0.1")
        status = []
        body = attempt._callback({"PATH_INFO": attempt.path, "QUERY_STRING": "state=wrong&code=secret"}, lambda code, headers: status.append(code))
        self.assertEqual(status, ["400 Bad Request"])
        self.assertIsNone(attempt.callback)
        attempt.cancel()
        with self.assertRaisesRegex(mail_service.MailError, "gmail_cancelled"):
            attempt.authenticate(self.progress)
        self.assertIsNone(attempt.flow)

    def test_oauth_loopback_callback_exchanges_code_with_pkce_and_memory_credentials(self):
        attempt = gmail_auth.OAuthAttempt(CONFIG)
        captured = []
        def token_exchange(*args, **kwargs):
            captured.append((args, kwargs))
            token = dict(access_token="test-access-memory", refresh_token="test-refresh-memory", token_type="Bearer", scope=[gmail_auth.SCOPE], expires_in=3600, expires_at=time.time() + 3600)
            attempt.flow.oauth2session.token = token
            return token
        result, errors = [], []
        def run():
            try:
                result.append(attempt.authenticate(self.progress))
            except Exception as exc:
                errors.append(exc)
        with patch.object(attempt.flow.oauth2session, "fetch_token", side_effect=token_exchange):
            worker = threading.Thread(target=run)
            worker.start()
            with requests.Session() as local:
                local.trust_env = False
                bad = local.get(attempt.flow.redirect_uri, params={"state": "forged", "code": "untrusted"}, timeout=5)
                self.assertEqual(bad.status_code, 400)
                valid = local.get(attempt.flow.redirect_uri, params={"state": attempt.state, "code": "test-code"}, timeout=5)
                self.assertEqual(valid.status_code, 200)
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0].account, "test@gmail.com")
        self.assertTrue(captured[0][1]["code_verifier"])
        self.assertEqual(captured[0][0][0], "https://oauth2.googleapis.com/token")
        sessions = SessionStore()
        cookie = sessions.create(self.store.account, result[0])
        self.assertIs(sessions.lookup(cookie)[1], result[0])
        raw = self.db.read_bytes()
        self.assertNotIn(b"test-access-memory", raw)
        self.assertNotIn(b"test-refresh-memory", raw)

    def test_gmail_access_and_rate_limit_errors_do_not_expose_payloads(self):
        for status, code in [(403, "gmail_access"), (429, "gmail_rate_limit"), (500, "gmail_api")]:
            with patch.object(self.api, "request", return_value=Response({"secret": "private-token"}, status)):
                with self.assertRaisesRegex(mail_service.MailError, code):
                    gmail.request(self.api, "GET", "/profile")
