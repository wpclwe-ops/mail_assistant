import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch
from streamlit.runtime.context import ContextProxy, StreamlitTheme
from streamlit.testing.v1 import AppTest
import storage
import mail_service as svc
from test_mail import msg, FakeIMAP


class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.patcher = patch.object(storage, "DB_PATH", self.path)
        self.patcher.start()
        self.store = storage.Store("test@icloud.com")
        self.store.save_scan("123", 2, [msg(), msg("11", "offers@bolt.eu", "Bolt")])

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def app(self, login=True):
        at = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10
        )
        if login:
            at.session_state["account"] = "test@icloud.com"
            at.session_state["password"] = "test-only"
        at.run()
        self.assertFalse(at.exception)
        return at

    def button(self, at, label):
        return next(b for b in at.button if b.label == label)

    def test_login_screen_and_languages(self):
        at = self.app(False)
        at.selectbox(key="login_provider").set_value("icloud").run()
        self.assertEqual(len(at.text_input), 2)
        self.assertTrue(self.button(at, "Подключиться к iCloud").disabled)
        at.selectbox(key="language").set_value("English").run()
        self.assertFalse(at.exception)
        self.assertTrue(self.button(at, "Connect to iCloud").disabled)
        self.assertEqual(storage.Store().get("language"), "English")

    def theme_scripts(self, at):
        return [e.proto.body for e in at.get("html") if "stActiveTheme" in e.proto.body]

    def test_theme_toggle_persists(self):
        # Light (or unknown) active theme: toggle off; switching on stores the
        # browser's theme choice ("Dark") and reloads the page.
        at = self.app()
        self.assertFalse(at.toggle(key="dark_theme").value)
        self.assertEqual(self.theme_scripts(at), [])
        at.toggle(key="dark_theme").set_value(True).run()
        self.assertFalse(at.exception)
        [script] = self.theme_scripts(at)
        self.assertIn('"stActiveTheme-" + window.location.pathname + "-v2"', script)
        self.assertIn('JSON.stringify("Dark")', script)
        self.assertIn("window.location.reload()", script)
        self.assertIsNone(storage.Store().get("theme_mode"))

    def test_theme_toggle_follows_active_dark_theme(self):
        dark = PropertyMock(return_value=StreamlitTheme({"type": "dark"}))
        with patch.object(ContextProxy, "theme", new_callable=lambda: dark):
            at = self.app()
            self.assertTrue(at.toggle(key="dark_theme").value)
            at.toggle(key="dark_theme").set_value(False).run()
            self.assertFalse(at.exception)
            [script] = self.theme_scripts(at)
            self.assertIn('JSON.stringify("Light")', script)
            # Once the switch is sent, later reruns track the active theme again.
            at.run()
            self.assertTrue(at.toggle(key="dark_theme").value)
            self.assertEqual(self.theme_scripts(at), [])

    def test_font_size_setting_persists(self):
        at = self.app()
        at.button(key="nav_settings").click().run()
        at.slider(key="font_size").set_value(18).run()
        self.assertFalse(at.exception)
        self.assertEqual(storage.Store().get("font_size"), 18)
        self.assertEqual(at.slider(key="font_size").value, 18)

    def test_all_pages_ru_en(self):

        at = self.app()
        for lang in ["Русский", "English"]:
            at.button(key="nav_settings").click().run()
            at.selectbox(key="language").set_value(lang).run()
            for page in ["white", "black", "settings", "mail"]:
                at.button(key="nav_" + page).click().run()
                self.assertFalse(at.exception)
                self.assertEqual(len(at.sidebar.button), 5)
            at.button(key="nav_settings").click().run()
            self.assertIsNotNone(at.slider(key="font_size"))
            self.assertTrue(any(e.label in ("История", "History") for e in at.expander))
            self.assertTrue(any(e.label in ("Объединение компаний", "Company groups") for e in at.expander))


    def test_read_filter_switches_visible_companies(self):

        self.store.save_scan("123", 2, [msg("10", "news@auchan.pl", "Auchan", unread=True), msg("11", "offers@bolt.eu", "Bolt", unread=False)])
        at = self.app()
        at.selectbox(key="mail_display").set_value("company_count").run()
        at.radio(key="inbox_filter").set_value("unread").run()
        self.assertIsNotNone(at.button(key="company_open_auchan"))
        self.assertNotIn("company_open_bolt", [b.key for b in at.button])
        at.radio(key="inbox_filter").set_value("read").run()
        self.assertIsNotNone(at.button(key="company_open_bolt"))
        self.assertNotIn("company_open_auchan", [b.key for b in at.button])


    def test_select_all_excludes_whitelist(self):

        self.store.policy(["news@auchan.pl"], "white")
        at = self.app()
        at.button(key="inbox_select_page").click().run()
        self.assertEqual(at.session_state["inbox_selected"], ["11"])
        at.button(key="inbox_clear").click().run()
        self.assertEqual(at.session_state["inbox_selected"], [])


    def test_prepare_delete_and_no_replay(self):

        fake = FakeIMAP([msg(), msg("11", "offers@bolt.eu", "Bolt")])
        @contextlib.contextmanager
        def conn(*args):
            yield fake
        with patch.object(svc, "connection", conn):
            at = self.app()
            at.button(key="inbox_select_page").click().run()
            at.button(key="inbox_review").click().run()
            self.assertFalse(any(c == "MOVE" for c, a in fake.calls))
            at.button(key="inbox_confirm").click().run()
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual(len([c for c, a in fake.calls if c == "MOVE"]), 2)


    def test_unchecking_one_message_keeps_preview_and_other_selection(self):

        at = self.app()
        at.button(key="inbox_select_page").click().run()
        at.checkbox(key="inbox_uid_10").uncheck().run()
        self.assertEqual(at.session_state["inbox_selected"], ["11"])
        at.button(key="inbox_review").click().run()
        self.assertEqual([m["uid"] for m in at.session_state["inbox_confirmation"]["targets"]], ["11"])


    def test_prepared_preview_is_frozen_from_outer_selection(self):

        at = self.app()
        at.checkbox(key="inbox_uid_10").check().run()
        at.button(key="inbox_review").click().run()
        at.selectbox(key="mail_display").set_value("company_name").run()
        at.text_input(key="inbox_search").input("Bolt").run()
        self.assertEqual([m["uid"] for m in at.session_state["inbox_confirmation"]["targets"]], ["10"])
        self.assertFalse(at.button(key="inbox_confirm").disabled)


    def test_queued_execute_runs_even_after_preview_context_is_gone(self):

        from all_messages import deletion_preview
        fake = FakeIMAP()
        @contextlib.contextmanager
        def conn(*args):
            yield fake
        with patch.object(svc, "connection", conn):
            at = self.app()
            at.session_state["execute_request"] = {"preview": deletion_preview(self.store, "123", ["10"]), "selected_uids": ["10"]}
            at.session_state["page"] = "settings"
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual(len([c for c, a in fake.calls if c == "MOVE"]), 1)
            self.assertNotIn("execute_request", at.session_state)


    def test_slow_preview_preserves_selection_and_consent(self):

        import threading
        release = threading.Event()
        def slow(*args):
            release.wait(5)
            return {"uid": "10", "text": "Test body", "truncated": False}
        self.store.policy(["news@auchan.pl"], "white")
        with patch.object(svc, "read_message", slow):
            at = self.app()
            at.checkbox(key="inbox_uid_10").check().run()
            at.button(key="inbox_review").click().run()
            at.checkbox(key="inbox_allow_white").check().run()
            at.button(key="inbox_open_10").click().run()
            at.button(key="inbox_read").click().run()
            self.assertTrue(all(b.disabled for b in at.sidebar.button))
            release.set()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertTrue(at.checkbox(key="inbox_allow_white").value)
            self.assertEqual(at.session_state["inbox_selected"], ["10"])


    def test_login_job_completes(self):
        fake = FakeIMAP()

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn):
            at = self.app(False)
            at.selectbox(key="login_provider").set_value("icloud").run()
            at.text_input[0].input("test@icloud.com")
            at.text_input[1].input("password-only-in-memory").run()
            self.button(at, "Подключиться к iCloud").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state["account"], "test@icloud.com")
            self.assertEqual(at.session_state["password"], "password-only-in-memory")
            self.assertNotIn(
                "password-only-in-memory",
                self.path.read_bytes().decode(errors="ignore"),
            )

    def test_search_preserves_hidden_selection(self):

        at = self.app()
        at.checkbox(key="inbox_uid_10").check().run()
        at.text_input(key="inbox_search").input("Bolt").run()
        self.assertEqual(at.session_state["inbox_selected"], ["10"])
        self.assertFalse(at.button(key="inbox_review").disabled)
        at.text_input(key="inbox_search").input("").run()
        self.assertTrue(at.checkbox(key="inbox_uid_10").value)


    def test_company_message_dialog_has_subjects_and_reader(self):

        at = self.app()
        at.selectbox(key="mail_display").set_value("company_count").run()
        at.button(key="company_open_auchan").click().run()
        self.assertIsNotNone(at.button(key="inbox_open_10"))
        self.assertNotIn("inbox_open_11", [b.key for b in at.button])
        at.button(key="inbox_open_10").click().run()
        self.assertIsNotNone(at.button(key="inbox_read"))
        at.button(key="company_back").click().run()
        self.assertNotIn("inbox_company", at.session_state)
        self.assertIsNotNone(at.button(key="company_open_bolt"))


    def test_empty_ad_filter_explains_no_deletion(self):

        at = self.app()
        at.checkbox(key="inbox_uid_10").check().run()
        item = msg()
        item["urls"] = []
        self.store.save_scan("123", 1, [item])
        at.run()
        at.button(key="inbox_unsubscribe_only").click().run()
        self.assertTrue(at.button(key="inbox_confirm").disabled)
        self.assertTrue(any("нет ссылок" in e.value for e in at.info))


    def test_controls_precede_bounded_company_list(self):

        many = [msg(str(i), f"news@brand{i}.test", f"Brand{i}") for i in range(10, 110)]
        self.store.save_scan("123", 100, many)
        at = self.app()
        at.selectbox(key="mail_display").set_value("company_count").run()
        keys = [b.key for b in at.main.button]
        self.assertLess(keys.index("inbox_review"), next(i for i, k in enumerate(keys) if k and k.startswith("company_open_")))
        self.assertEqual(len([c for c in at.checkbox if c.key.startswith("inbox_group_")]), 25)
        at.button(key="inbox_select_page").click().run()
        self.assertEqual(len(at.session_state["inbox_selected"]), 100)


    def test_reader_does_not_restore_unchecked_messages(self):

        with patch.object(svc, "read_message", return_value={"uid": "10", "text": "Body", "truncated": False}):
            at = self.app()
            at.button(key="inbox_select_page").click().run()
            at.button(key="inbox_clear").click().run()
            at.button(key="inbox_open_10").click().run()
            at.button(key="inbox_read").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertEqual(at.session_state["inbox_selected"], [])
            self.assertTrue(at.button(key="inbox_review").disabled)


    def test_deselect_all_clears_hidden_companies(self):

        at = self.app()
        at.button(key="inbox_select_page").click().run()
        at.text_input(key="inbox_search").input("Bolt").run()
        at.button(key="inbox_clear").click().run()
        at.text_input(key="inbox_search").input("").run()
        self.assertEqual(at.session_state["inbox_selected"], [])
        self.assertFalse(any(c.value for c in at.checkbox))



if __name__ == "__main__":
    unittest.main()
