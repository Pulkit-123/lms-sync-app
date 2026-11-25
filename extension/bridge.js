// Runs only on the LMS Sync page (http://127.0.0.1:8765). Pairs with the app
// and relays capture requests between the page and the extension.
document.documentElement.dataset.lmsExt = "1"; // lets the page know the extension is installed
document.documentElement.dataset.lmsExtVersion = chrome.runtime.getManifest().version;

document.addEventListener("DOMContentLoaded", () => {
  const token = document.querySelector('meta[name="lms-token"]')?.content;
  if (token) chrome.runtime.sendMessage({ type: "token", token });
});

const post = (msg) => window.postMessage({ type: "lms-capture-result", ...msg }, window.location.origin);

window.addEventListener("message", async (e) => {
  if (e.source !== window || e.data?.type !== "lms-capture") return;
  const { requestId, payload } = e.data;
  try {
    post({ requestId, ...(await chrome.runtime.sendMessage({ type: "capture", requestId, ...payload })) });
  } catch (err) {
    post({ requestId, ok: false, error: String(err?.message || err) });
  }
});

// A capture finished later (the user clicked the toolbar button on a waiting tab).
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "capture-finished") post(msg.result);
});
