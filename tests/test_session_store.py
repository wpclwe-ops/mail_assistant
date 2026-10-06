import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

import streamlit as st
from streamlit.testing.v1 import AppTest

import mail_service as svc
import storage
from session_store import COOKIE, IDLE_TIMEOUT, SessionStore
from test_mail import FakeIMAP, msg


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = SessionStore(clock=lambda: self.now)

    def test_create_and_lookup(self):
        token = self.store.create("a@icloud.com", "secret")
        self.assertGreaterEqual(len(token), 40)
        self.assertNotEqual(token, self.store.create("a@icloud.com", "secret"))
        self.assertEqual(self.store.lookup(token), ("a@icloud.com", "secret"))
        self.assertIsNone(self.store.lookup("unknown"))
        self.assertIsNone(self.store.lookup(None))

    def test_idle_expiry_and_refresh(self):
        token = self.store.create("a@icloud.com", "secret")
        self.now += IDLE_TIMEOUT - 1
        self.assertIsNotNone(self.store.lookup(token))  # restarts the idle timer
        self.now += IDLE_TIMEOUT - 1
        self.assertIsNotNone(self.store.lookup(token))
        self.now += IDLE_TIMEOUT + 1
        self.assertIsNone(self.store.lookup(token))
        self.assertEqual(self.store._entries, {})  # pruned

    def test_revoke(self):
        token = self.store.create("a@icloud.com", "secret")
        other = self.store.create("b@icloud.com", "other")
        self.store.revoke(token)
        self.store.revoke(token)
        self.store.revoke(None)
        self.assertIsNone(self.store.lookup(token))
        self.assertEqual(self.store.lookup(other), ("b@icloud.com", "other"))


class SessionRestoreUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.patcher = patch.object(storage, "DB_PATH", self.path)
        self.patcher.start()
        storage.Store("test@icloud.com").save_scan("123", 1, [msg()])

        @contextlib.contextmanager
        def conn(*args):
            yield FakeIMAP()

        self.conn = patch.object(svc, "connection", conn)
        self.conn.start()

    def tearDown(self):
        self.conn.stop()
        self.patcher.stop()
        self.tmp.cleanup()

    def run_app(self, cookies=None):
        """A fresh browser session (as after a refresh) sending these cookies."""
        at = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10
        )
        with patch.object(type(st.context), "cookies", new_callable=PropertyMock) as c:
            c.return_value = cookies or {}
            at.run()
        self.assertFalse(at.exception)
        return at

    def cookie_scripts(self, at):
        return [h.proto.body for h in at.get("html") if COOKIE in h.proto.body]

    def sign_in(self):
        at = self.run_app()
        at.selectbox(key="login_provider").set_value("icloud").run()
        at.text_input[0].input("test@icloud.com")
        at.text_input[1].input("password-only-in-memory").run()
        next(b for b in at.button if b.label == "Подключиться к iCloud").click().run()
        if at.session_state["job"]:
            at.session_state["job"].thread.join(5)
        at.run()
        self.assertEqual(at.session_state["account"], "test@icloud.com")
        return at

    def test_sign_in_sets_cookie_and_refresh_restores(self):
        at = self.sign_in()
        token = at.session_state["session_token"]
        scripts = self.cookie_scripts(at)
        self.assertEqual(len(scripts), 1)
        self.assertIn(f"{COOKIE}={token}; Path=/; SameSite=Strict", scripts[0])
        self.assertNotIn("Max-Age", scripts[0])
        self.assertNotIn(token, self.path.read_bytes().decode(errors="ignore"))

        again = self.run_app({COOKIE: token})
        self.assertEqual(again.session_state["account"], "test@icloud.com")
        self.assertEqual(again.session_state["password"], "password-only-in-memory")
        self.assertEqual(self.cookie_scripts(again), [])  # cookie already set

    def test_invalid_cookie_shows_login_and_clears_cookie(self):
        at = self.run_app({COOKIE: "forged"})
        self.assertNotIn("account", at.session_state)
        self.assertEqual(len(at.text_input), 0)
        self.assertIn("Max-Age=0", self.cookie_scripts(at)[0])

    def test_sign_out_revokes_token(self):
        at = self.sign_in()
        token = at.session_state["session_token"]
        at.button(key="signout").click().run()
        self.assertEqual(len(at.text_input), 0)
        self.assertIn("Max-Age=0", self.cookie_scripts(at)[0])
        again = self.run_app({COOKIE: token})
        self.assertNotIn("account", again.session_state)
        self.assertEqual(len(again.text_input), 0)


if __name__ == "__main__":
    unittest.main()
