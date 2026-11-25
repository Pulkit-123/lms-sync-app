"""Engine behavior with Moodle faked out: python3 -m unittest discover tests"""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DATA = tempfile.mkdtemp()
os.environ["LMS_SYNC_DATA"] = DATA  # before state is imported
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import catalog  # noqa: E402
import desktop  # noqa: E402
import engine  # noqa: E402
import browser  # noqa: E402
import links  # noqa: E402
import moodle_client  # noqa: E402
import state  # noqa: E402

CID = 4242


def item(key, name, size=10, tm=1, kind="file", module="Mod", modname="resource", mid=1, **kw):
    kw.setdefault("fileurl", f"https://x/{key}")
    return catalog.Item(key, name, size, tm, kind, mid, module, modname, **kw)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.dest = Path(tempfile.mkdtemp())
        self.sections = [catalog.Section("s1", "Week 1", [item("a", "Slides.pdf")])]
        self.files = {}  # fileurl -> bytes
        self.trash = Path(tempfile.mkdtemp())
        self.opened: list[tuple[str, str]] = []
        # Nothing in these tests may reach the real desktop: no Trash, no opening files.
        fakes = {
            "move_to_trash": lambda p: shutil.move(p, str(self.trash / Path(p).name)),
            "open_path": lambda p: self.opened.append(("open", p)),
            "reveal": lambda p: self.opened.append(("reveal", p)),
        }
        for name, fake in fakes.items():
            patcher = mock.patch.object(desktop, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        engine.catalog.fetch = lambda cid: self.sections
        moodle_client.download_via_wstoken = lambda url: self.files.get(url, b"x" * 10)
        state.update_course(CID, lambda c: c.update(name="Test Course", folder=str(self.dest), downloaded={}, flags={}, organize=False))
        self.select(course=True)

    def select(self, **sel):
        base = {"course": False, "sections": [], "items": [], "excluded": []}
        state.update_course(CID, lambda c: c["selection"].update({**base, **sel}))

    def test_selection_rules(self):
        i = item("a", "x")
        sel = lambda **k: {"course": False, "sections": [], "items": [], "excluded": [], **k}  # noqa: E731
        self.assertFalse(engine.is_selected(i, "s1", sel()))
        self.assertTrue(engine.is_selected(i, "s1", sel(course=True)))
        self.assertTrue(engine.is_selected(i, "s1", sel(sections=["s1"])))
        self.assertFalse(engine.is_selected(i, "s2", sel(sections=["s1"])))
        self.assertTrue(engine.is_selected(i, "s2", sel(items=["a"])))
        self.assertFalse(engine.is_selected(i, "s1", sel(course=True, excluded=["a"])))

    def test_default_is_section_folders_and_flat_is_opt_out(self):
        self.assertTrue(state.get_course(987654)["organize"])

    def test_sections_become_folders_under_moodle(self):
        state.update_course(CID, lambda c: c.update(organize=True))
        self.sections = [
            catalog.Section("s0", "General", [item("a", "Syllabus.pdf")]),
            catalog.Section("s1", "Annotated slides", [item("b", "Week 1.pdf", mid=2)]),
            catalog.Section("s2", "Odd / name: here", [item("c", "X.pdf", mid=3)]),
        ]
        s = engine.sync_course(CID)
        self.assertEqual(sorted(s.new), ["Syllabus.pdf", "Week 1.pdf", "X.pdf"])
        self.assertTrue((self.dest / "Moodle" / "General" / "Syllabus.pdf").exists())
        self.assertTrue((self.dest / "Moodle" / "Annotated slides" / "Week 1.pdf").exists())
        self.assertTrue((self.dest / "Moodle" / "Odd - name- here" / "X.pdf").exists())
        self.assertTrue((self.dest / "Claude" / "CLAUDE.md").exists())  # Claude/ sits beside Moodle/

    def test_moved_file_in_section_layout_is_not_redownloaded_or_recreated(self):
        state.update_course(CID, lambda c: c.update(organize=True))
        engine.sync_course(CID)
        (self.dest / "Keep").mkdir()
        (self.dest / "Moodle" / "Week 1" / "Slides.pdf").rename(self.dest / "Keep" / "s.pdf")
        s = engine.sync_course(CID)
        self.assertEqual((s.new, s.updated), ([], []))
        self.assertFalse((self.dest / "Moodle" / "Week 1" / "Slides.pdf").exists())

    def test_url_modules_become_link_items(self):
        mod = {"id": 1, "modname": "url", "name": "Course Pack", "url": "https://m/mod/url/view.php?id=1",
               "contents": [{"type": "url", "fileurl": "https://x.sharepoint.com/pack.pdf"}]}
        (it,) = catalog._module_items(mod, {})
        self.assertEqual((it.kind, it.key, it.name, it.fileurl), ("link", "link:1", "Course Pack", "https://x.sharepoint.com/pack.pdf"))
        self.assertEqual(catalog._module_items({**mod, "contents": []}, {}), [])  # no target -> nothing to save

    def test_book_chapters_and_duplicates_get_distinct_names(self):
        names = catalog._local_names("Videos", ["index.html", "index.html", "Data.xlsx", "Data.xlsx", "Solo.pdf"])
        self.assertEqual(names, ["Videos (1).html", "Videos (2).html", "Data (1).xlsx", "Data (2).xlsx", "Solo.pdf"])
        self.assertEqual(catalog._local_names("One page", ["index.html"]), ["One page.html"])

    def test_first_sync_downloads_flat_and_prepares_claude_folder(self):
        s = engine.sync_course(CID)
        self.assertEqual(s.new, ["Slides.pdf"])
        self.assertTrue((self.dest / "Slides.pdf").exists())
        self.assertTrue((self.dest / "Claude" / "CLAUDE.md").exists())
        self.assertEqual((self.dest / "CLAUDE.md").read_text(encoding="utf-8"), "@Claude/CLAUDE.md\n")

    def test_second_sync_and_moved_file_never_redownload(self):
        engine.sync_course(CID)
        self.assertEqual(engine.sync_course(CID).new, [])
        (self.dest / "Notes").mkdir()
        (self.dest / "Slides.pdf").rename(self.dest / "Notes" / "my slides.pdf")
        s = engine.sync_course(CID)
        self.assertEqual((s.new, s.updated), ([], []))
        self.assertFalse((self.dest / "Slides.pdf").exists())

    def test_upstream_change_fetches_alongside_and_leaves_original_alone(self):
        engine.sync_course(CID)
        (self.dest / "Slides.pdf").write_bytes(b"my annotated copy")
        self.sections[0].items[0] = item("a", "Slides.pdf", size=20, tm=2)
        self.files["https://x/a"] = b"y" * 20
        s = engine.sync_course(CID)
        today = datetime.date.today().isoformat()
        self.assertEqual(s.updated, [f"Slides (updated {today}).pdf"])
        self.assertEqual((self.dest / "Slides.pdf").read_bytes(), b"my annotated copy")
        self.assertIn("a", state.get_course(CID)["flags"])
        self.assertEqual(engine.sync_course(CID).updated, [])  # not re-flagged every run

    def test_same_name_from_different_modules_never_overwrites(self):
        self.sections[0].items = [
            item("a", "Slides.pdf", module="Session 1"),
            item("b", "Slides.pdf", module="Session 2", mid=2),
            item("c", "Slides.pdf", module="Session 2", mid=3),
        ]
        s = engine.sync_course(CID)
        self.assertEqual(sorted(s.new), ["Session 2 - Slides (2).pdf", "Session 2 - Slides.pdf", "Slides.pdf"])

    def test_drm_video_embed_becomes_placeholder(self):
        chapter = item("v", "Videos (1).html", size=100, modname="book")
        self.sections[0].items = [chapter]
        self.files["https://x/v"] = b'<h3>1. Intro</h3><iframe src="https://player.vdocipher.com/v2/?otp=1"></iframe>'
        s = engine.sync_course(CID)
        self.assertEqual(s.new, ["Videos (1).txt"])
        self.assertIn("DRM-protected", (self.dest / "Videos (1).txt").read_text(encoding="utf-8"))
        self.assertEqual(engine.sync_course(CID).updated, [])  # tracked by Moodle's size, not the placeholder's

    def test_online_text_submission_saved_as_html(self):
        self.sections[0].items = [
            item("t", "Submitted - Essay.html", kind="submission-text", inline="<p>my answer</p>", size=19)
        ]
        s = engine.sync_course(CID)
        self.assertEqual(s.new, ["Submitted - Essay.html"])

    def test_unticking_never_deletes_and_nothing_ticked_does_nothing(self):
        engine.sync_course(CID)
        self.select()
        s = engine.sync_course(CID)
        self.assertEqual(s.skipped, "nothing ticked")
        self.assertTrue((self.dest / "Slides.pdf").exists())

    def test_only_ticked_section_is_downloaded(self):
        self.sections = [
            catalog.Section("s1", "Week 1", [item("a", "A.pdf")]),
            catalog.Section("s2", "Week 2", [item("b", "B.pdf", mid=2)]),
        ]
        self.select(sections=["s2"])
        self.assertEqual(engine.sync_course(CID).new, ["B.pdf"])
        self.select(sections=["s2"], items=["a"])
        self.assertEqual(engine.sync_course(CID).new, ["A.pdf"])

    def test_no_folder_is_skipped(self):
        state.update_course(CID, lambda c: c.update(folder=None))
        self.assertEqual(engine.sync_course(CID).skipped, "no folder chosen")

    def _locked_item(self, mid):
        return item(f"wm:{mid}", f"Locked case {mid}", size=0, tm=0, kind="watermarked", mid=mid, modname="filewithwatermark")

    def _no_browser(self):
        """Browser calls that fail the test if anything reaches for a real browser."""
        def boom(*a, **k):
            raise AssertionError("browser used unexpectedly")
        browser.fetch_public = browser.fetch_all = boom

    def test_locked_files_wait_unattended_then_the_extension_completes_them(self):
        self.sections[0].items = [self._locked_item(7)]
        browser.fetch_public = lambda jobs: browser.Outcome()
        browser.fetch_all = lambda jobs: browser.Outcome(needs_login={j.key for j in jobs})  # saved session is stale

        s = engine.sync_course(CID)  # no window is ever opened
        self.assertEqual((s.new, s.needs_login), ([], 1))
        self.assertEqual(state.get_course(CID)["locked_waiting"], 1)
        (pending,) = s.pending_login  # what the UI hands to the extension
        self.assertEqual((pending["courseId"], pending["key"], pending["kind"]), (CID, "wm:7", "watermarked"))
        self.assertIn("filewithwatermark/view.php?id=7", pending["url"])

        r = engine.save_link_file(CID, "wm:7", "Case.pdf", b"%PDF-1.4 " + b"x" * 20)  # the extension's upload
        self.assertEqual(r["saved_as"], "Case.pdf")
        self.assertEqual(state.get_course(CID)["locked_waiting"], 0)

        self._no_browser()
        s = engine.sync_course(CID)  # already have it: nothing pending, no browser at all
        self.assertEqual((s.new, s.needs_login, s.pending_login), ([], 0, []))

    def test_a_still_valid_saved_session_fetches_locked_files_unattended(self):
        self.sections[0].items = [self._locked_item(7)]
        browser.fetch_public = lambda jobs: browser.Outcome()
        browser.fetch_all = lambda jobs: browser.Outcome(results={jobs[0].key: ("A.pdf", b"%PDF-1" + b"a" * 20)})
        s = engine.sync_course(CID)
        self.assertEqual((s.new, s.pending_login), (["A.pdf"], []))

    def test_pending_logins_are_collected_across_courses(self):
        other = CID + 1
        dest2 = Path(tempfile.mkdtemp())
        by_course = {
            CID: [catalog.Section("s1", "W", [self._locked_item(7)])],
            other: [catalog.Section("s9", "W", [self._locked_item(8)])],
        }
        engine.catalog.fetch = lambda cid: by_course[cid]
        state.update_course(other, lambda c: c.update(name="Other", folder=str(dest2), downloaded={}, flags={}, organize=False))
        state.update_course(other, lambda c: c["selection"].update(course=True))
        browser.fetch_public = lambda jobs: browser.Outcome()
        browser.fetch_all = lambda jobs: browser.Outcome(needs_login={j.key for j in jobs})
        ours = [s for s in engine.sync_all() if s.course_id in (CID, other)]
        self.assertEqual(sorted((p["courseId"], p["key"]) for s in ours for p in s.pending_login), [(CID, "wm:7"), (other, "wm:8")])
        state.update_course(other, lambda c: c.update(folder=None))  # don't leak into other tests

    def test_link_classification(self):
        sp = "https://x.sharepoint.com/sites/S/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FS%2FShared%20Documents%2FPack.pdf&p=true"
        t = links.classify(sp)
        self.assertEqual((t.kind, t.filename, t.path), ("sharepoint", "Pack.pdf", "/sites/S/Shared Documents/Pack.pdf"))
        self.assertEqual(
            links.sharepoint_download_url(sp, t.path),
            "https://x.sharepoint.com/sites/S/_layouts/15/download.aspx?SourceUrl=%2Fsites%2FS%2FShared%20Documents%2FPack.pdf",
        )
        self.assertEqual(links.classify("https://example.com/papers/Case%20One.pdf").kind, "file")
        d = links.classify("https://www.dropbox.com/scl/fi/abc/Data.xlsx?rlkey=k&st=s&dl=0")
        self.assertEqual((d.kind, d.filename), ("file", "Data.xlsx"))
        self.assertEqual(d.download_url, "https://www.dropbox.com/scl/fi/abc/Data.xlsx?rlkey=k&st=s&dl=1")  # real file, not the preview
        self.assertEqual(links.classify("https://www.dropbox.com/scl/fo/x/y?rlkey=k&dl=0").kind, "file")  # shared folder -> zip
        self.assertEqual(links.classify("https://www.bain.com/insights/x/").kind, "webpage")
        self.assertEqual(links.classify("https://x.sharepoint.com/sites/S/SitePages/Home.aspx").kind, "webpage")
        self.assertEqual(links.classify("mailto:a@b.c").kind, "webpage")
        self.assertTrue(links.needs_login(sp) and not links.needs_login("https://www.bain.com/x"))

    def test_web_article_is_saved_as_pdf_and_failure_keeps_a_shortcut(self):
        good = item("link:1", "Value Elements", kind="link", modname="url", fileurl="https://www.bain.com/a/")
        bad = item("link:2", "Blocked Site", kind="link", modname="url", mid=2, fileurl="https://blocked.example/")
        self.sections[0].items = [good, bad]
        browser.fetch_public = lambda jobs: browser.Outcome(
            results={"link:1": ("Value Elements.pdf", b"%PDF-1.7 snapshot")}, errors={"link:2": "the site answered 403"})
        browser.fetch_all = lambda jobs: self.fail("no login needed for public pages")
        s = engine.sync_course(CID)
        shortcut = "Blocked Site" + desktop.shortcut("https://x")[0]
        self.assertEqual(sorted(s.new), [shortcut, "Value Elements.pdf"])
        self.assertIn("https://blocked.example/", (self.dest / shortcut).read_text(encoding="utf-8"))
        self.assertTrue(any("Blocked Site" in e and "403" in e for e in s.errors))
        self._no_browser()
        self.assertEqual(engine.sync_course(CID).new, [])  # links are fetched once, not every run

    def test_sharepoint_link_is_handed_to_the_extension_with_its_download_url(self):
        sp = "https://x.sharepoint.com/sites/S/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FS%2FShared%20Documents%2FPack.pdf"
        self.sections[0].items = [item("link:3", "Course Pack", kind="link", modname="url", fileurl=sp)]
        browser.fetch_public = lambda jobs: browser.Outcome()
        browser.fetch_all = lambda jobs: browser.Outcome(needs_login={j.key for j in jobs})
        s = engine.sync_course(CID)
        self.assertEqual(s.needs_login, 1)
        (p,) = s.pending_login
        self.assertEqual((p["kind"], p["filename"]), ("sharepoint", "Pack.pdf"))
        self.assertTrue(p["downloadUrl"].startswith("https://x.sharepoint.com/sites/S/_layouts/15/download.aspx?SourceUrl="))
        browser.fetch_all = lambda jobs: browser.Outcome(results={"link:3": ("Pack.pdf", b"%PDF-1.4 real")})
        self.assertEqual(engine.sync_course(CID).new, ["Pack.pdf"])  # a still-valid session needs no extension

    def test_uploaded_file_is_filed_in_the_section_folder_and_replaces_a_shortcut_record(self):
        sp = "https://x.sharepoint.com/sites/S/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FS%2FShared%20Documents%2FPack.pdf"
        self.sections[0].items = [item("link:5", "Course Pack", kind="link", modname="url", fileurl=sp)]
        state.update_course(CID, lambda c: c.update(organize=True, locked_waiting=1))
        r = engine.save_link_file(CID, "link:5", "Pack.pdf", b"%PDF-1.4 captured")
        self.assertEqual(r["saved_as"], "Pack.pdf")
        self.assertTrue((self.dest / "Moodle" / "Week 1" / "Pack.pdf").exists())
        self.assertEqual(state.get_course(CID)["downloaded"]["link:5"]["saved_as"], "Pack.pdf")
        self.assertEqual(state.get_course(CID)["locked_waiting"], 0)
        with self.assertRaises(LookupError):
            engine.save_link_file(CID, "nope", "x.pdf", b"x")

    def test_imported_file_is_copied_in_and_the_original_is_left_alone(self):
        self.sections[0].items = [item("link:6", "Bain page", kind="link", modname="url", fileurl="https://www.bain.com/a/")]
        src = Path(tempfile.mkdtemp()) / "Bain B2B.pdf"
        src.write_bytes(b"%PDF-1.7 saved by the user")
        r = engine.import_file(CID, "link:6", src)
        self.assertEqual(r["saved_as"], "Bain B2B.pdf")
        self.assertTrue(src.exists())  # copied, not moved
        self.assertEqual((self.dest / "Bain B2B.pdf").read_bytes(), src.read_bytes())
        self.assertEqual(state.get_course(CID)["downloaded"]["link:6"]["saved_as"], "Bain B2B.pdf")
        with self.assertRaises(ValueError):
            engine.import_file(CID, "link:6", src.parent / "missing.pdf")

    def test_extra_file_from_the_extension_lands_in_saved_from_browser_without_tracking(self):
        r = engine.save_extra_file(CID, "Some Article.pdf", b"%PDF-1.4 hi")
        self.assertEqual(r, {"saved_as": "Some Article.pdf", "folder": "Saved from browser/"})
        self.assertTrue((self.dest / "Saved from browser" / "Some Article.pdf").exists())
        r2 = engine.save_extra_file(CID, "Some Article.pdf", b"%PDF-1.4 again")  # same name: never overwrites
        self.assertEqual(r2["saved_as"], "Some Article (2).pdf")
        self.assertEqual(state.get_course(CID)["downloaded"], {})
        state.update_course(CID, lambda c: c.update(folder=None))
        with self.assertRaises(ValueError):
            engine.save_extra_file(CID, "x.pdf", b"x")

    def test_delete_and_redownload_trashes_the_file_forgets_it_and_the_next_sync_fetches_it_again(self):
        engine.sync_course(CID)
        (self.dest / "Notes").mkdir()
        (self.dest / "Slides.pdf").rename(self.dest / "Notes" / "Slides.pdf")  # the user moved it
        info = engine.locate_download(CID, "a")
        self.assertEqual(info, {"name": "Slides.pdf", "found": ["Notes/Slides.pdf"]})
        with self.assertRaises(ValueError):  # only the file locate_download found, never an invented path
            engine.forget_download(CID, "a", "../elsewhere.pdf")
        self.assertEqual(engine.forget_download(CID, "a", "Notes/Slides.pdf"), {"trashed": "Notes/Slides.pdf"})
        self.assertTrue((self.trash / "Slides.pdf").exists())
        self.assertFalse((self.dest / "Notes" / "Slides.pdf").exists())
        self.assertNotIn("a", state.get_course(CID)["downloaded"])
        self.assertEqual(engine.sync_course(CID).new, ["Slides.pdf"])  # back, fresh

    def test_forget_only_leaves_files_alone_and_a_missing_file_can_still_be_redownloaded(self):
        engine.sync_course(CID)
        (self.dest / "Slides.pdf").unlink()
        self.assertEqual(engine.locate_download(CID, "a")["found"], [])
        self.assertEqual(engine.forget_download(CID, "a", None), {"trashed": None})
        self.assertEqual(engine.sync_course(CID).new, ["Slides.pdf"])
        (self.dest / "Claude" / "Slides.pdf").write_bytes(b"x")  # our notes folder is never searched
        self.assertEqual(engine.locate_download(CID, "a")["found"], ["Slides.pdf"])
        with self.assertRaises(LookupError):
            engine.locate_download(CID, "never-downloaded")

    def test_open_hands_documents_to_their_app_but_only_reveals_anything_executable(self):
        engine.sync_course(CID)
        (self.dest / "Notes").mkdir()
        (self.dest / "Slides.pdf").rename(self.dest / "Notes" / "Slides.pdf")
        self.assertEqual(engine.open_download(CID, "a"), {"opened": "Notes/Slides.pdf", "revealed": False})
        self.assertEqual(self.opened[-1], ("open", str(self.dest / "Notes" / "Slides.pdf")))
        self.sections[0].items = [item("b", "setup.command", mid=2)]
        engine.sync_course(CID)
        self.assertTrue(engine.open_download(CID, "b")["revealed"])
        self.assertEqual(self.opened[-1], ("reveal", str(self.dest / "setup.command")))
        engine.open_course_folder(CID)
        self.assertEqual(self.opened[-1], ("open", str(self.dest)))
        (self.dest / "setup.command").unlink()
        with self.assertRaises(LookupError):
            engine.open_download(CID, "b")

    def test_ticking_a_section_covers_its_subsections_and_they_sit_under_it(self):
        pdf = lambda name: {"type": "file", "filename": name, "fileurl": f"https://x/{name}", "filesize": 10}  # noqa: E731
        contents = [
            {"id": 1, "section": 1, "name": "Session Materials", "modules": [
                {"id": 9, "modname": "subsection", "name": "Session 1", "customdata": '{"sectionid":"5"}'}]},
            {"id": 2, "section": 2, "name": "Other", "modules": [{"id": 8, "modname": "resource", "name": "O", "contents": [pdf("o.pdf")]}]},
            {"id": 5, "section": 3, "name": "Session 1", "modules": [
                {"id": 7, "modname": "resource", "name": "S", "description": "<p>Read first</p>", "contents": [pdf("s.pdf")]}]},
        ]
        sections = catalog.sections_from(contents, {})
        self.assertEqual([(s.name, s.parent) for s in sections], [("Session Materials", ""), ("Session 1", "1"), ("Other", "")])
        self.assertEqual(sections[1].items[0].note, "Read first")
        self.sections = sections
        self.select(sections=["1"])
        self.assertEqual(engine.sync_course(CID).new, ["s.pdf"])

    def test_assignment_files_embedded_in_the_description_are_downloadable_too(self):
        a = {"filename": "A.pdf", "fileurl": "https://x/A"}
        assigns = {3: {"introattachments": [a], "introfiles": [{"filename": "fig.png", "fileurl": "https://x/fig"}, a],
                       "activityattachments": [{"filename": "steps.docx", "fileurl": "https://x/steps"}]}}
        names = [i.name for i in catalog._module_items({"id": 3, "modname": "assign", "name": "HW"}, assigns)]
        self.assertEqual(names, ["A.pdf", "fig.png", "steps.docx"])

    def test_download_one_file_ticks_it_and_fetches_only_that_one(self):
        self.sections = [catalog.Section("s1", "W", [item("a", "A.pdf"), item("b", "B.pdf", mid=2)])]
        self.select(excluded=["b"])
        s = engine.fetch_item(CID, "b")
        self.assertEqual(s.new, ["B.pdf"])
        self.assertFalse((self.dest / "A.pdf").exists())
        sel = state.get_course(CID)["selection"]
        self.assertEqual((sel["items"], sel["excluded"]), (["b"], []))

    def test_direct_file_link_downloads_without_a_browser(self):
        self._no_browser()
        self.sections[0].items = [item("link:4", "Paper", kind="link", modname="url", fileurl="https://example.com/Paper.pdf")]
        links.download_direct = lambda url: ("Paper.pdf", b"%PDF-1.4 hi")
        self.assertEqual(engine.sync_course(CID).new, ["Paper.pdf"])


if __name__ == "__main__":
    unittest.main()
