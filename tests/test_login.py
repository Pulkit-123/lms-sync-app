"""Token storage: log in verifies first, log out forgets everything local."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LMS_SYNC_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import browser  # noqa: E402
import moodle_client  # noqa: E402
import server  # noqa: E402


class LoginTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.env = d / ".env"
        self.env.write_text("OTHER=keep\nMOODLE_WSTOKEN=old\n", encoding="utf-8")
        self.patches = [
            mock.patch.object(moodle_client, "ENV_PATH", self.env),
            mock.patch.object(moodle_client, "SESSION_DIR", d / "profile"),
            mock.patch.object(browser, "COOKIES_PATH", d / "cookies.json"),
            mock.patch.dict(os.environ, {"MOODLE_WSTOKEN": "old"}),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(setattr, moodle_client, "WSTOKEN", moodle_client.WSTOKEN)
        moodle_client.reload_token()
        (d / "profile").mkdir()
        (d / "cookies.json").write_text("[]")
        self.dir = d

    def reply(self, body):
        return mock.patch.object(moodle_client.requests, "post", return_value=mock.Mock(json=lambda: body))

    def test_a_rejected_token_leaves_the_current_login_alone(self):
        with self.reply({"exception": "x", "errorcode": "invalidtoken"}), self.assertRaises(ValueError):
            moodle_client.save_token("bad")
        self.assertEqual(moodle_client.WSTOKEN, "old")
        self.assertIn("MOODLE_WSTOKEN=old", self.env.read_text(encoding="utf-8"))

    def test_log_in_replaces_the_token_and_keeps_other_lines(self):
        with self.reply({"userid": 1, "fullname": "A"}):
            moodle_client.save_token("new")
        self.assertEqual((moodle_client.WSTOKEN, self.env.read_text(encoding="utf-8")), ("new", "OTHER=keep\nMOODLE_WSTOKEN=new\n"))

    def test_log_out_removes_the_token_and_old_session_data(self):
        server.log_out()
        self.assertEqual((moodle_client.WSTOKEN, self.env.read_text(encoding="utf-8")), ("", "OTHER=keep\n"))
        self.assertFalse((self.dir / "profile").exists() or (self.dir / "cookies.json").exists())
        with self.assertRaises(moodle_client.NoTokenError):
            moodle_client._call("core_webservice_get_site_info")


if __name__ == "__main__":
    unittest.main()
