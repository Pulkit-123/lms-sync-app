"""Announcements & deadlines logic, with Moodle faked out."""
import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("LMS_SYNC_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import board  # noqa: E402
import moodle_client  # noqa: E402
import state  # noqa: E402


def ts(*args):
    return int(datetime.datetime(*args).timestamp())


POSTED = ts(2026, 11, 14, 21, 46)


class DateSpottingTest(unittest.TestCase):
    def spot(self, text, posted=POSTED):
        return [(d["due"], d["time_assumed"]) for d in board.spot_dates(text, posted)]

    def test_wordings_seen_in_real_announcements(self):
        self.assertEqual(self.spot("Due: Tuesday, 17 November 2026, 7:00 PM"), [(ts(2026, 11, 17, 19, 0), False)])
        self.assertEqual(self.spot("The assignment is due this Friday, 20 November 2026."), [(ts(2026, 11, 20, 23, 59), True)])
        # the date in brackets after the sentence still belongs to it
        self.assertEqual(self.spot("Submit it before the next class. (10:00 AM, 18th November 2026)"), [(ts(2026, 11, 18, 10, 0), False)])
        self.assertEqual(self.spot("Submit before the next class (10:00 AM, 18th November 2026)."), [(ts(2026, 11, 18, 10, 0), False)])
        self.assertEqual(self.spot("Submit your responses by 11:59 PM on 18th November 2026. Thank you."), [(ts(2026, 11, 18, 23, 59), False)])
        self.assertEqual(self.spot("Deadline: 21st November, 23:59 PM IST."), [(ts(2026, 11, 21, 23, 59), False)])
        self.assertEqual(self.spot("Deadline: Before Session 5, 7 PM"), [])  # no date given: nothing to guess

    def test_month_first_no_year_and_year_rollover(self):
        self.assertEqual(self.spot("Deadline: November 30 at 23:00"), [(ts(2026, 11, 30, 23, 0), False)])
        self.assertEqual(self.spot("Submission due 5 January", ts(2026, 12, 20)), [(ts(2027, 1, 5, 23, 59), True)])

    def test_ignores_dates_that_are_not_about_a_deadline_and_impossible_ones(self):
        self.assertEqual(self.spot("The class on 21 November 2026 moves to LT1."), [])
        self.assertEqual(self.spot("Due 31 November 2026"), [])
        self.assertEqual(self.spot("You may 2 submit"), [])


class TextTest(unittest.TestCase):
    def test_html_becomes_plain_text_and_only_web_links_survive(self):
        text, links = board.to_text(
            '<p>Dear Class,</p><script>alert(1)</script><ul><li>One &amp; two</li></ul>'
            '<a href="https://x.test/a">the sheet</a> <a href="javascript:alert(1)">bad</a>')
        self.assertIn("Dear Class,", text)
        self.assertIn("• One & two", text)
        self.assertNotIn("alert(1)", text)
        self.assertEqual(links, [{"label": "the sheet", "url": "https://x.test/a"}])


class MemoryTest(unittest.TestCase):
    def test_vanished_moodle_entry_is_kept_done_tick_survives_and_moved_dates_are_noted(self):
        stored = {
            "assign:1": {"source": "assignment", "title": "A1", "due": 100, "done": True},
            "quiz:2": {"source": "quiz", "title": "Q", "due": 50},
            "manual:x": {"source": "manual", "title": "mine", "due": 70},
        }
        merged = board.merge_deadlines(stored, {}, {"assignment"})  # quizzes couldn't be read this time
        self.assertTrue(merged["assign:1"]["gone"])
        self.assertFalse(merged["quiz:2"].get("gone", False))
        self.assertFalse(merged["manual:x"].get("gone", False))
        back = board.merge_deadlines(merged, {"assign:1": {"source": "assignment", "title": "A1", "due": 200}}, {"assignment"})
        self.assertEqual((back["assign:1"]["done"], back["assign:1"]["gone"], back["assign:1"]["previous_due"]), (True, False, 100))

    def test_status(self):
        now = 1_000_000
        s = lambda **e: board.status_of({"source": "assignment", **e}, now)  # noqa: E731
        self.assertEqual(s(due=now - 5), "overdue")
        self.assertEqual(s(due=now + 3600), "soon")
        self.assertEqual(s(due=now + 10 * 86400), "upcoming")
        self.assertEqual(s(due=now - 5, submitted=True), "submitted")
        self.assertEqual(s(due=now - 5, gone=True), "past")  # can't claim "not submitted" if Moodle hides it
        self.assertEqual(s(due=0), "no-date")


class RefreshTest(unittest.TestCase):
    CID = 777

    def setUp(self):
        state.update_course(self.CID, lambda c: c.update(name="Course", board={"known": [], "read": [], "deadlines": {}, "dismissed": [], "checked_at": None}))
        self.due = int(datetime.datetime.now().timestamp()) + 3600
        self.assignments = [{"id": 5, "cmid": 55, "name": "HW &amp; more", "duedate": self.due, "teamsubmission": 1}]
        self.discussions = [self.discussion(1, "Hello")]
        moodle_client.get_assignment_list = lambda cid: (self.assignments, 2)
        moodle_client.get_submission_status = lambda aid: {"lastattempt": {"teamsubmission": {"status": "new"}}}
        moodle_client.get_quizzes = lambda cid: []
        moodle_client.get_action_events = lambda cid, since: [{"id": 9, "modulename": "assign", "name": "dup", "timesort": self.due}]
        self.contents = [{"id": 1, "section": 0, "name": "General", "modules": [{"id": 30, "modname": "forum", "name": "Announcements", "instance": 3}]}]
        moodle_client.get_course_contents = lambda cid: self.contents
        moodle_client.get_forum_discussions = lambda fid: self.discussions

    def discussion(self, n, subject, message="<p>hi</p>"):
        return {"discussion": n, "name": subject, "userfullname": "TA", "created": POSTED, "message": message}

    def test_first_look_reports_nothing_new_then_later_posts_do(self):
        first = board.refresh(self.CID)
        self.assertEqual(first["new_announcements"], [])
        self.assertEqual([(d["title"], d["status"], d["team"]) for d in first["deadlines"]], [("HW & more", "soon", True)])
        self.assertEqual(first["hidden_assignments"], 2)
        self.assertTrue(first["announcements"][0]["unread"])
        self.discussions.append(self.discussion(2, "Newer"))
        self.assertEqual(board.refresh(self.CID)["new_announcements"], ["Newer"])
        self.assertEqual(board.counts(state.get_course(self.CID)["board"]), {"unread": 2, "due_soon": 1})
        board.mark_read(self.CID, None)
        self.assertEqual(board.counts(state.get_course(self.CID)["board"])["unread"], 0)

    def test_hidden_assignment_stays_tracked_and_one_failing_source_does_not_sink_the_rest(self):
        board.refresh(self.CID)
        self.assignments = []
        view = board.refresh(self.CID)
        self.assertEqual([(d["title"], d["gone"]) for d in view["deadlines"]], [("HW & more", True)])

        def broken(cid):
            raise RuntimeError("boom")

        moodle_client.get_quizzes = broken
        view = board.refresh(self.CID)
        self.assertTrue(any("quizzes" in p for p in view["problems"]))
        self.assertEqual(len(view["announcements"]), 1)

    def test_manual_deadlines_suggestions_and_removal_rules(self):
        later = datetime.datetime.now() + datetime.timedelta(days=9)
        self.discussions = [self.discussion(1, "Group Assignment", f"<p>Deadline: {later.day} {later:%B %Y}, 7:00 PM</p>")]
        view = board.refresh(self.CID)
        (spot,) = view["suggestions"]
        board.add_manual(self.CID, spot["title"], spot["due"])
        view = board.refresh(self.CID)
        self.assertEqual(view["suggestions"], [])  # already tracked
        manual = next(d for d in view["deadlines"] if d["source"] == "manual")
        board.set_done(self.CID, manual["key"], True)
        self.assertEqual(next(d for d in board.refresh(self.CID)["deadlines"] if d["key"] == manual["key"])["status"], "done")
        with self.assertRaises(ValueError):
            board.remove(self.CID, "assign:55")  # Moodle still shows it
        board.remove(self.CID, manual["key"])
        (spot,) = board.refresh(self.CID)["suggestions"]
        board.dismiss_suggestion(self.CID, spot["key"])
        self.assertEqual(board.refresh(self.CID)["suggestions"], [])
        with self.assertRaises(ValueError):
            board.add_manual(self.CID, "  ", self.due)

    def test_assignment_carries_instructions_its_files_and_the_files_beside_it(self):
        pdf = lambda name: {"type": "file", "filename": name, "fileurl": f"https://x/{name}", "filesize": 1}  # noqa: E731
        self.contents += [
            {"id": 2, "section": 1, "name": "Group Activities", "modules": [
                {"id": 56, "modname": "resource", "name": "Instructions", "contents": [pdf("Brief.pdf")]},
                {"id": 55, "modname": "assign", "name": "HW"},
                {"id": 57, "modname": "subsection", "name": "Data", "customdata": '{"sectionid":"3"}'},
                {"id": 58, "modname": "choicegroup", "name": "Group Formation",
                 "description": f"<p>Form your groups by Friday, {self.next_week()}, 2:45 PM.</p>"}]},
            {"id": 4, "section": 2, "name": "Elsewhere", "modules": [{"id": 60, "modname": "resource", "name": "N", "contents": [pdf("Other.pdf")]}]},
            {"id": 3, "section": 3, "name": "Data", "modules": [{"id": 59, "modname": "resource", "name": "D", "contents": [pdf("Sheet.xlsx")]}]},
        ]
        self.assignments[0].update(intro='<p>Read the brief. <a href="https://x.test/guide">Guide</a></p>',
                                   introattachments=[{"filename": "Template.docx", "fileurl": "https://x/t"}],
                                   introfiles=[{"filename": "chart.png", "fileurl": "https://x/c"}])
        state.update_course(self.CID, lambda c: c.update(downloaded={"mod:56:Brief.pdf": {"saved_as": "Brief.pdf"}}))
        view = board.refresh(self.CID)
        (hw,) = view["deadlines"]
        self.assertIn("Read the brief.", hw["instructions"])
        self.assertEqual(hw["links"], [{"label": "Guide", "url": "https://x.test/guide"}])
        self.assertEqual([f["name"] for f in hw["files"]], ["Template.docx", "chart.png"])
        self.assertEqual([(f["name"], f["saved"]) for f in hw["related"]], [("Brief.pdf", True), ("Sheet.xlsx", False)])
        (spot,) = view["suggestions"]  # the date only written in an activity's description
        self.assertEqual((spot["title"], spot["where"]), ("Group Formation", "activity description"))

    def next_week(self):
        d = datetime.datetime.now() + datetime.timedelta(days=7)
        return f"{d.day} {d:%B %Y}"

    def test_manual_notes_and_links_can_be_added_and_edited_and_reach_the_calendar(self):
        key = board.add_manual(self.CID, "Group work", self.due, " Split the slides\nby Tuesday ", "docs.google.com/d/1\nhttps://x.test/a")
        entry = state.get_course(self.CID)["board"]["deadlines"][key]
        self.assertEqual(entry["notes"], "Split the slides\nby Tuesday")
        self.assertEqual(entry["links"], ["https://docs.google.com/d/1", "https://x.test/a"])
        with self.assertRaises(ValueError):
            board.add_manual(self.CID, "x", self.due, "", "javascript:alert(1)")
        board.set_done(self.CID, key, True)
        board.edit_manual(self.CID, key, "Group work v2", self.due + 60, "new notes", [])
        entry = state.get_course(self.CID)["board"]["deadlines"][key]
        self.assertEqual((entry["title"], entry["notes"], entry["links"], entry["done"]), ("Group work v2", "new notes", [], True))
        board.refresh(self.CID)
        with self.assertRaises(ValueError):
            board.edit_manual(self.CID, "assign:55", "hijack", self.due)
        mine = [d for d in board.calendar()["deadlines"] if d["course_id"] == self.CID]
        self.assertEqual([(d["title"], d["status"]) for d in mine], [("HW & more", "soon"), ("Group work v2", "done")])


if __name__ == "__main__":
    unittest.main()
