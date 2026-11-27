"""The app's own web server, called the way the page calls it — and a check that
no module refers to something that doesn't exist (a refactor once deleted the
function behind every button on the announcements tab)."""
import builtins
import os
import symtable
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("LMS_SYNC_DATA", tempfile.mkdtemp())
SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

import requests  # noqa: E402

import board  # noqa: E402
import server  # noqa: E402
import state  # noqa: E402

CID = 6161
MODULE_NAMES = {"__file__", "__name__", "__doc__", "__spec__", "__package__", "__conditional_annotations__"}  # last: Python 3.14


def undefined_names(source: str, filename: str) -> set:
    top = symtable.symtable(source, filename, "exec")
    tables, pending = [], [top]
    while pending:
        table = pending.pop()
        tables.append(table)
        pending.extend(table.get_children())
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    defined |= {s.get_name() for t in tables for s in t.get_symbols() if s.is_declared_global() and s.is_assigned()}
    used = {s.get_name() for t in tables for s in t.get_symbols()
            if s.is_referenced() and (t is top or s.is_global())}
    return {n for n in used - defined - MODULE_NAMES if not hasattr(builtins, n)}


class NoMissingNamesTest(unittest.TestCase):
    def test_every_module_defines_what_it_uses(self):
        for path in sorted(SRC.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(undefined_names(path.read_text(encoding="utf-8"), str(path)), set())

    def test_the_check_notices_a_deleted_function(self):
        self.assertEqual(undefined_names("def f():\n    return gone(1)\n", "x.py"), {"gone"})


class BoardButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/api/course/{CID}/board/"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        fresh = {"known": [1, 2, 3], "read": [], "deadlines": {}, "dismissed": [], "checked_at": "2026-01-01T00:00:00"}
        state.update_course(CID, lambda c: c.__setitem__("board", fresh))

    def tearDown(self):
        state.update_course(CID, lambda c: c.__setitem__("board", {**c["board"], "known": [], "deadlines": {}}))

    def press(self, action, **body):
        response = requests.post(self.url + action, json=body, timeout=10)
        self.assertEqual(response.status_code, 200, response.text)

    def saved(self):
        return state.get_course(CID)["board"]  # read back from disk, as after a page refresh

    def test_mark_one_then_all_read_is_saved(self):
        self.press("read", id=2)
        self.assertEqual(self.saved()["read"], [2])
        self.press("read")
        self.assertEqual(self.saved()["read"], [1, 2, 3])
        self.assertEqual(board.counts(self.saved())["unread"], 0)

    def test_add_edit_tick_and_remove_a_deadline(self):
        self.press("add", title="Case note", due=2000000000, notes="n", links="example.com/a")
        (key,) = self.saved()["deadlines"]
        self.press("edit", key=key, title="Case note v2", due=2000000000, notes="", links="")
        self.press("done", key=key, done=True)
        entry = self.saved()["deadlines"][key]
        self.assertEqual((entry["title"], entry["done"]), ("Case note v2", True))
        self.press("remove", key=key)
        self.assertEqual(self.saved()["deadlines"], {})

    def test_dismissing_a_spotted_date_is_saved(self):
        self.press("dismiss", key="2:2000000000")
        self.assertEqual(self.saved()["dismissed"], ["2:2000000000"])

    def test_unknown_action_is_refused(self):
        self.assertEqual(requests.post(self.url + "nonsense", json={}, timeout=10).status_code, 400)


if __name__ == "__main__":
    unittest.main()
