"""Updating an installed copy, against a stand-in for GitHub."""
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("LMS_SYNC_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import state  # noqa: E402
import updater  # noqa: E402
from fake_github import FakeGitHub, make_zip  # noqa: E402


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        """An "installed" copy at version 1.0.0 with the user's own files in it."""
        self.root = Path(tempfile.mkdtemp())
        with zipfile.ZipFile(io.BytesIO(make_zip("1.0.0"))) as z:
            z.extractall(self.root / "x")
        installed = next((self.root / "x").iterdir())
        for item in list(installed.iterdir()):
            item.rename(self.root / item.name)
        self.mine = {".env": "MOODLE_WSTOKEN=keep\n", "data/state.json": "{}", "logs/daily.log": "log", "notes.txt": "mine"}
        for name, text in self.mine.items():
            (self.root / name).parent.mkdir(exist_ok=True)
            (self.root / name).write_text(text, encoding="utf-8")
        (self.root / "src" / "gone_in_new_version.py").write_text("x = 1\n", encoding="utf-8")
        for patcher in (mock.patch.object(updater, "ROOT", self.root),
                        mock.patch.object(state, "DATA_DIR", self.root / "data")):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def serve(self, version, archive=None):
        fake = FakeGitHub(version, archive)
        self.addCleanup(fake.close)
        patcher = mock.patch.object(updater, "API", fake.url)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_untouched(self):
        for name, text in self.mine.items():
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), text, name)

    def test_installs_a_newer_release_keeps_user_files_and_removes_dropped_code(self):
        self.serve("1.2.0", make_zip("1.2.0", {"src/brand_new.py": "y = 2\n"}))
        self.assertTrue(updater.check(force=True)["available"])
        self.assertEqual(updater.install(), "1.2.0")
        self.assertEqual(updater.current_version(), "1.2.0")
        self.assertTrue((self.root / "src" / "brand_new.py").exists())
        self.assertFalse((self.root / "src" / "gone_in_new_version.py").exists())
        self.assertEqual((self.root / "data" / "backup" / "VERSION").read_text(encoding="utf-8").strip(), "1.0.0")
        self.assert_untouched()
        self.assertIsNone(updater.install())  # now up to date

    def test_same_or_older_release_does_nothing(self):
        self.serve("0.9.0")
        self.assertFalse(updater.check(force=True)["available"])
        self.assertIsNone(updater.install())

    def test_broken_or_incomplete_releases_are_refused_and_nothing_changes(self):
        for archive in (
            make_zip("2.0.0", {"src/server.py": "def broken(:\n"}),       # doesn't start
            make_zip("2.0.0", {"src/ui/index.html": None}),                # incomplete
            make_zip("1.5.0"),                                             # version doesn't match the tag
        ):
            self.serve("2.0.0", archive)
            with self.assertRaises(updater.UpdateError):
                updater.install()
            self.assertEqual(updater.current_version(), "1.0.0")
            self.assertTrue((self.root / "src" / "gone_in_new_version.py").exists())
            self.assert_untouched()

    def test_a_zip_that_escapes_its_folder_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("top/VERSION", "2.0.0")
            z.writestr("../../escaped.txt", "x")
        self.serve("2.0.0", buffer.getvalue())
        with self.assertRaises(updater.UpdateError):
            updater.install()

    def test_development_checkout_and_plugin_are_never_overwritten(self):
        self.serve("9.0.0")
        (self.root / ".git").mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.install()
        self.assertIsNone(updater.install_if_automatic())
        self.assertEqual(updater.current_version(), "1.0.0")

    def test_automatic_updates_can_be_switched_off(self):
        self.serve("1.1.0")
        updater.set_auto(False)
        self.assertIsNone(updater.install_if_automatic())
        updater.set_auto(True)
        self.assertEqual(updater.install_if_automatic(), "1.1.0")

    def test_no_release_yet_or_offline_is_not_an_error_for_the_app(self):
        with mock.patch.object(updater, "API", "http://127.0.0.1:1"):
            info = updater.check(force=True)
            self.assertFalse(info["available"])
            self.assertIn("Couldn't check", info["error"])
            self.assertIsNone(updater.install_if_automatic())


if __name__ == "__main__":
    unittest.main()
