"""The syncing list, and the rules that differ between Windows and macOS."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LMS_SYNC_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import desktop  # noqa: E402
import download  # noqa: E402
import engine  # noqa: E402
import moodle_client  # noqa: E402
import server  # noqa: E402
import state  # noqa: E402

CID = 5150


class SyncingListTest(unittest.TestCase):
    def setUp(self):
        self.course = moodle_client.Course(id=CID, shortname="ABC_X_T8", fullname="A Course", startdate=1, enddate=int(time.time()) - 10)
        patcher = mock.patch.object(server, "_course", lambda cid: self.course)
        patcher.start()
        self.addCleanup(patcher.stop)
        state.update_course(CID, lambda c: c.update(folder=None, syncing=None, selection={"course": False, "sections": [], "items": [], "excluded": []}))

    def tearDown(self):
        state.update_course(CID, lambda c: c.update(folder=None, syncing=False))

    def test_until_switched_it_follows_folder_plus_ticks(self):
        cfg = state.get_course(CID)
        self.assertFalse(state.is_syncing(cfg))
        cfg["folder"] = "/x"
        self.assertFalse(state.is_syncing(cfg))
        cfg["selection"]["items"] = ["a"]
        self.assertTrue(state.is_syncing(cfg))
        self.assertFalse(state.is_syncing({**cfg, "syncing": False}))
        self.assertFalse(state.is_syncing({**cfg, "folder": None, "syncing": True}))  # a folder is always required

    def test_switching_on_needs_a_folder_and_takes_the_whole_course_if_nothing_is_ticked(self):
        with self.assertRaises(ValueError):
            server.set_syncing(CID, True)
        state.update_course(CID, lambda c: c.update(folder=tempfile.mkdtemp()))
        r = server.set_syncing(CID, True)
        self.assertEqual((r["syncing"], r["took_whole_course"], r["ended"]), (True, True, True))
        self.assertTrue(state.get_course(CID)["selection"]["course"])

    def test_switching_off_keeps_everything_but_leaves_the_daily_sync(self):
        folder = tempfile.mkdtemp()
        state.update_course(CID, lambda c: c.update(folder=folder, downloaded={"a": {"saved_as": "a"}},
                                                    selection={"course": False, "sections": ["s"], "items": [], "excluded": []}))
        server.set_syncing(CID, False)
        cfg = state.get_course(CID)
        self.assertEqual((cfg["folder"], cfg["selection"]["sections"], len(cfg["downloaded"])), (folder, ["s"], 1))
        planned = []
        with mock.patch.object(engine, "_make_plan", lambda cid, only="": planned.append(cid) or engine._Plan(engine.Summary(cid, "x"))), \
             mock.patch.object(engine, "_fetch_browser", lambda *a: None), mock.patch.object(engine, "_run_plan", lambda *a: None):
            engine.sync_all()
        self.assertNotIn(CID, planned)
        server.set_syncing(CID, True)
        self.assertEqual(state.get_course(CID)["selection"]["sections"], ["s"])  # ticks weren't replaced


class PlatformRulesTest(unittest.TestCase):
    def test_names_windows_cannot_store_are_made_safe(self):
        self.assertEqual(download.safe_filename("CON.pdf"), "_CON.pdf")
        self.assertEqual(download.safe_filename('a<b>:c"?*|.pdf'), download.safe_filename('a<b>:c"?*|.pdf'))
        self.assertFalse(set('\\/:*?"<>|') & set(download.safe_filename('x\\y/z:*?"|.pdf')))
        self.assertFalse(download.safe_filename("notes. ").endswith((" ", ".")))
        self.assertLessEqual(len(download.safe_filename("é" * 400 + ".pdf").encode()), desktop.MAX_NAME_BYTES)

    def test_link_shortcut_for_this_system_and_only_web_url_files_may_be_opened(self):
        suffix, content = desktop.shortcut("https://x.test/a?b=1&c=2")
        self.assertEqual(suffix, ".url" if desktop.WINDOWS else ".webloc")
        self.assertIn(b"x.test", content)
        d = Path(tempfile.mkdtemp())
        (d / "ok.url").write_text("[InternetShortcut]\nURL=https://x.test\n", encoding="utf-8")
        (d / "bad.url").write_text("[InternetShortcut]\nURL=file:///C:/Windows/System32/cmd.exe\n", encoding="utf-8")
        self.assertTrue(desktop.is_web_shortcut(d / "ok.url"))
        self.assertFalse(desktop.is_web_shortcut(d / "bad.url"))

    def test_the_file_lock_is_exclusive_and_reusable(self):
        path = Path(tempfile.mkdtemp()) / "l"
        with desktop.file_lock(path):
            pass
        with desktop.file_lock(path):
            pass


if __name__ == "__main__":
    unittest.main()
