# LMS Sync capture (Chrome extension)

Lets the LMS Sync app save links that can't be downloaded in the background —
ISB SharePoint files and web pages (as PDF) — **using your own Chrome**, so your
sign-ins and a site's "are you human" checks work normally. Nothing leaves your
computer: the extension only talks to the app at `http://127.0.0.1:8765` and
(for SharePoint files) `*.sharepoint.com`.

## Install (once)
1. Open `chrome://extensions` and switch on **Developer mode** (top right).
2. Click **Load unpacked** and choose this `extension` folder.
3. Pin the extension (puzzle-piece menu → pin) so its button is in the toolbar.
4. Reload the LMS Sync page. Link rows now say **Capture in my Chrome**.

## Use
- Click **Capture in my Chrome** on a link. A tab opens, the page is saved as a
  PDF into the course's section folder, and the tab closes.
- A site showing a human check gets one try. If it's blocked, the tab stays open
  with a **!** on the extension icon: pass the check, then click the extension's
  toolbar button on that tab to capture it.
- A brief "…started debugging this browser" bar is Chrome's own notice while the
  page is being turned into a PDF.

## Add any page to a course
Open the extension from the toolbar on any tab and click a course's button —
that's it, the tab is saved straight into that course. A web page is saved as a PDF; if the tab is
itself a file (a PDF in Chrome's viewer…) the file is saved. It lands in the
course folder's `Saved from browser/` folder. Which courses appear in the list
is set per course in the app (**Show in the Chrome extension's list**).

## Also handles
- Locked Moodle PDFs and ISB SharePoint files (when Download needs a sign-in)
- **Renew login** (Moodle token) — in your own Chrome, no separate window
