// LMS Sync capture — does the things that need the user's own Chrome: their
// sign-ins, their passkeys, a site's "are you human" check. It opens links in
// normal tabs, saves pages as PDFs, fetches signed-in files (Moodle locked
// PDFs, ISB SharePoint, Dropbox) and renews the Moodle login, handing results to
// the local LMS Sync app. Nothing leaves this computer.
const APP = "http://127.0.0.1:8765";
const MOODLE = "https://elearn.isb.edu";
const AUTH_SCHEME = "lmssyncauth";
const NOT_READY = ["just a moment", "sign in to your account", "attention required", "verify you are human", "access denied"];
const SIGN_IN_WAIT_MS = 240000;
const waiting = new Map(); // tabId -> request that still needs the user (bot check on a page)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const notReady = (title) => NOT_READY.some((p) => (title || "").toLowerCase().startsWith(p));
const safeName = (s) => (s || "page").replace(/[\\/:*?"<>|\x00-\x1f]/g, "-").replace(/\s+/g, " ").trim().slice(0, 150) || "page";

async function appToken() {
  const { token } = await chrome.storage.local.get("token");
  if (!token) throw new Error("Open the LMS Sync page once so the extension can pair with it, then try again.");
  return token;
}

async function appPost(path, query, headers, body) {
  const url = `${APP}${path}${query ? "?" + new URLSearchParams(query) : ""}`;
  const r = await fetch(url, { method: "POST", headers: { "X-LMS-Token": await appToken(), ...headers }, body });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || `The app answered ${r.status}`);
  return out;
}

const upload = (req, filename, bytes) =>
  appPost("/api/ext/upload", { course: req.courseId, key: req.key, filename }, { "Content-Type": "application/octet-stream" }, bytes);

function b64ToBytes(b64) {
  const bin = atob(b64), out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function captureTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
    try {
      const r = await chrome.debugger.sendCommand(target, "Page.printToPDF", { printBackground: true });
      return { name: safeName(tab.title) + ".pdf", bytes: b64ToBytes(r.data) };
    } finally {
      await chrome.debugger.detach(target).catch(() => {});
    }
  } catch (e) {
    const blob = await chrome.pageCapture.saveAsMHTML({ tabId }); // no PDF? keep a single-file archive
    return { name: safeName(tab.title) + ".mhtml", bytes: await blob.arrayBuffer() };
  }
}

function waitComplete(tabId, ms = 45000) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(timer); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    const timer = setTimeout(done, ms);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") done(); }).catch(done);
  });
}

// ---- fetching files in the user's signed-in Chrome ----

async function tryFetch(url) {
  try {
    const r = await fetch(url, { credentials: "include" });
    const type = r.headers.get("content-type") || "";
    if (!r.ok || type.includes("text/html") || /login\.microsoftonline\.com|\/login\/index\.php/.test(r.url)) return null;
    return { bytes: await r.arrayBuffer(), url: r.url, disposition: r.headers.get("content-disposition") || "" };
  } catch (e) {
    return null;
  }
}

function fileNameOf(got, fallback) {
  const m = got.disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const fromUrl = decodeURIComponent((got.url.split("?")[0].split("/").pop() || ""));
  return safeName(m ? decodeURIComponent(m[1]) : fromUrl || fallback);
}

// Fetch with the user's cookies. Signed out? Open `openUrl` so they can sign in
// (passkey and all), and keep retrying until it works or time runs out.
async function fetchSignedIn(fetchUrl, openUrl) {
  let got = await tryFetch(fetchUrl);
  if (got) return got;
  const tab = await chrome.tabs.create({ url: openUrl, active: true });
  for (let waited = 0; waited < SIGN_IN_WAIT_MS && !got; waited += 2000) {
    await sleep(2000);
    got = await tryFetch(fetchUrl);
  }
  chrome.tabs.remove(tab.id).catch(() => {});
  return got;
}

// ---- renewing the Moodle login (replaces the separate login window) ----

function decodeToken(redirectUrl) {
  const encoded = redirectUrl.slice(`${AUTH_SCHEME}://token=`.length);
  const padded = encoded + "=".repeat((4 - (encoded.length % 4)) % 4);
  const parts = atob(padded).split(":::");
  if (parts.length !== 3) throw new Error("Moodle sent an unexpected token format.");
  return parts[1]; // privatetoken:::wstoken:::signature
}

function handleRenew() {
  const launch = `${MOODLE}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=lmssync&urlscheme=${AUTH_SCHEME}`;
  return new Promise(async (resolve) => {
    let tabId, retries = 0, finished = false;
    const done = (result) => {
      if (finished) return;
      finished = true;
      chrome.webRequest.onBeforeRedirect.removeListener(onRedirect);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      clearTimeout(timer);
      if (tabId) chrome.tabs.remove(tabId).catch(() => {});
      resolve(result);
    };
    // Moodle answers launch.php with a redirect to our own URL scheme, carrying the token.
    const onRedirect = async (d) => {
      if (!d.redirectUrl || !d.redirectUrl.startsWith(`${AUTH_SCHEME}://token=`)) return;
      try {
        await appPost("/api/ext/token", null, { "Content-Type": "application/json" }, JSON.stringify({ wstoken: decodeToken(d.redirectUrl) }));
        done({ ok: true });
      } catch (e) {
        done({ ok: false, error: String(e.message || e) });
      }
    };
    // After signing in, Moodle may land on the dashboard or home page instead of
    // returning here: once it's on an ordinary Moodle page, ask for the token again.
    const onUpdated = (id, info, tab) => {
      if (id !== tabId || info.status !== "complete" || retries >= 2) return;
      const url = tab.url || "";
      if (!url.startsWith(`${MOODLE}/`)) return; // still at Microsoft's sign-in
      const path = url.slice(MOODLE.length);
      if (/^\/(login|auth|admin\/tool\/mobile)\//.test(path)) return; // still signing in
      retries += 1;
      chrome.tabs.update(tabId, { url: launch });
    };
    const timer = setTimeout(() => done({ ok: false, error: "Timed out — sign in to Moodle in the tab that opened and try again." }), SIGN_IN_WAIT_MS);
    chrome.webRequest.onBeforeRedirect.addListener(onRedirect, { urls: [`${MOODLE}/*`] });
    chrome.tabs.onUpdated.addListener(onUpdated);
    tabId = (await chrome.tabs.create({ url: launch, active: true })).id;
  });
}

// ---- capture requests from the app ----

async function handleCapture(req) {
  if (req.kind === "renew") return handleRenew();
  if (req.kind === "reload") { // the app was updated: load the new extension files from disk
    setTimeout(() => chrome.runtime.reload(), 300);
    return { ok: true };
  }

  if (req.kind === "sharepoint" && req.downloadUrl) {
    const got = await fetchSignedIn(req.downloadUrl, req.url);
    if (!got) return { ok: false, error: "Couldn't get the file — sign in to ISB SharePoint in the tab and try again." };
    return { ok: true, ...(await upload(req, req.filename || fileNameOf(got, "file.pdf"), got.bytes)) };
  }

  if (req.kind === "watermarked") {
    const got = await fetchSignedIn(req.url, `${MOODLE}/my/`);
    if (!got) return { ok: false, error: "Couldn't get the locked file — sign in to Moodle in the tab and try again." };
    return { ok: true, ...(await upload(req, fileNameOf(got, req.name || "locked.pdf"), got.bytes)) };
  }

  if (req.kind === "file" && req.downloadUrl) { // a real file (Dropbox share link, direct link)
    const got = await tryFetch(req.downloadUrl);
    if (got) return { ok: true, ...(await upload(req, req.filename || fileNameOf(got, "file"), got.bytes)) };
    // not fetchable directly (needs a sign-in, or the host won't allow it): fall through to the page
  }

  const tab = await chrome.tabs.create({ url: req.url, active: true });
  await waitComplete(tab.id);
  let current = await chrome.tabs.get(tab.id);
  for (let i = 0; i < 10 && notReady(current.title); i++) { // a bot check can clear itself in a real browser
    await sleep(2500);
    current = await chrome.tabs.get(tab.id);
  }
  await sleep(1500); // let late content settle
  if (notReady((await chrome.tabs.get(tab.id)).title)) {
    waiting.set(tab.id, req);
    chrome.action.setBadgeText({ tabId: tab.id, text: "!" });
    return {
      ok: false,
      needsUser: true,
      error: "The site is showing a check. Finish it in the tab that opened, then click the LMS Sync button in Chrome's toolbar to capture the page.",
    };
  }
  const { name, bytes } = await captureTab(tab.id);
  const saved = await upload(req, name, bytes);
  chrome.tabs.remove(tab.id).catch(() => {});
  return { ok: true, ...saved };
}

// The user finished a check in a waiting tab and chose "Capture now" in the popup.
async function captureWaiting(tabId) {
  const req = waiting.get(tabId);
  if (!req) return { ok: false, error: "This tab isn't waiting for a capture." };
  let result;
  try {
    const { name, bytes } = await captureTab(tabId);
    result = { requestId: req.requestId, ok: true, ...(await upload(req, name, bytes)) };
    waiting.delete(tabId);
    chrome.action.setBadgeText({ tabId, text: "" });
    chrome.tabs.remove(tabId).catch(() => {});
  } catch (e) {
    result = { requestId: req.requestId, ok: false, error: String(e.message || e) };
  }
  for (const t of await chrome.tabs.query({ url: `${APP}/*` })) {
    chrome.tabs.sendMessage(t.id, { type: "capture-finished", result }).catch(() => {});
  }
  return result;
}

// ---- "add this page to a course" (from the popup, any tab) ----

// Runs inside the page, so its own cookies/sign-in are used. If the tab is a file
// (a PDF in Chrome's viewer, an image…) return its bytes; a normal page returns {file:false}.
async function probePageFile() {
  const ct = document.contentType || "";
  if (ct.startsWith("text/html") || ct.startsWith("application/xhtml")) return { file: false };
  const r = await fetch(location.href, { credentials: "include" });
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  const cd = r.headers.get("content-disposition") || "";
  const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  return { file: true, name: decodeURIComponent(m ? m[1] : location.pathname.split("/").pop() || "file"), b64: btoa(bin) };
}

async function saveTabToCourse(tabId, courseId) {
  const tab = await chrome.tabs.get(tabId);
  if (!/^https?:/.test(tab.url || "")) throw new Error("Only normal web pages and files can be saved from here.");
  const [probe] = await chrome.scripting.executeScript({ target: { tabId }, func: probePageFile });
  let name, bytes;
  if (probe?.result?.file) {
    name = safeName(probe.result.name);
    bytes = b64ToBytes(probe.result.b64);
  } else {
    ({ name, bytes } = await captureTab(tabId));
  }
  return { ok: true, ...(await appPost("/api/ext/add", { course: courseId, filename: name }, { "Content-Type": "application/octet-stream" }, bytes)) };
}

async function listCourses() {
  let token;
  try { token = await appToken(); } catch (e) { return { ok: false, error: String(e.message) }; }
  try {
    const r = await fetch(`${APP}/api/ext/courses`, { headers: { "X-LMS-Token": token } });
    if (!r.ok) return { ok: false, error: "The app didn't accept this extension — open the LMS Sync page once to pair it." };
    return { ok: true, courses: await r.json() };
  } catch (e) {
    return { ok: false, error: "LMS Sync isn't running. Double-click “LMS Sync” on your Desktop, then try again." };
  }
}

chrome.tabs.onRemoved.addListener((id) => waiting.delete(id));

const answer = (promise, sendResponse) =>
  promise.then(sendResponse, (e) => sendResponse({ ok: false, error: String(e?.message || e) }));

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  switch (msg?.type) {
    case "token": chrome.storage.local.set({ token: msg.token }); return;
    case "capture": answer(handleCapture(msg), sendResponse); return true;
    case "courses": answer(listCourses(), sendResponse); return true;
    case "save-tab": answer(saveTabToCourse(msg.tabId, msg.courseId), sendResponse); return true;
    case "capture-waiting": answer(captureWaiting(msg.tabId), sendResponse); return true;
    case "waiting-info": {
      const req = waiting.get(msg.tabId);
      sendResponse(req ? { waiting: true, name: req.name } : { waiting: false });
      return;
    }
  }
});
