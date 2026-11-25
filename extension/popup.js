const root = document.getElementById("app");
const el = (html) => { const d = document.createElement("div"); d.innerHTML = html; return d; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  root.textContent = "";

  // A tab the app opened that was stuck on a human check: finish capturing it for its link.
  const waiting = await chrome.runtime.sendMessage({ type: "waiting-info", tabId: tab.id });
  if (waiting?.waiting) {
    const box = el(`<div class="box"><div>Capture this page for <b>${esc(waiting.name || "the link")}</b></div>
      <button id="finish">Capture now</button><div class="msg" id="finishMsg"></div></div>`);
    root.append(box);
    box.querySelector("#finish").onclick = async (e) => {
      e.target.disabled = true;
      const r = await chrome.runtime.sendMessage({ type: "capture-waiting", tabId: tab.id });
      const m = box.querySelector("#finishMsg");
      m.className = "msg " + (r.ok ? "ok" : "bad");
      m.textContent = r.ok ? `Saved as ${r.saved_as}` : r.error;
      if (r.ok) setTimeout(() => window.close(), 1200);
      else e.target.disabled = false;
    };
  }

  root.append(el(`<div class="page" title="${esc(tab.url || "")}">${esc(tab.title || tab.url || "")}</div>`));
  const list = await chrome.runtime.sendMessage({ type: "courses" });
  if (!list.ok) { root.append(el(`<div class="msg bad">${esc(list.error)}</div>`)); return; }
  if (!list.courses.length) {
    root.append(el(`<div class="msg">No courses are shown here yet. In the LMS Sync app, give a course a folder and tick “Show in the Chrome extension”.</div>`));
    return;
  }
  // One button per course: a single click saves this tab into that course.
  root.append(el(`<div class="label">Save this tab to:</div>`));
  const msg = el(`<div class="msg" id="msg"></div>`);
  const buttons = list.courses.map((c) => {
    const b = el(`<button class="course" title="${esc(c.name)}"><span class="code">${esc(c.code)}</span>${esc(c.name)}</button>`).firstChild;
    b.onclick = async () => {
      buttons.forEach((x) => (x.disabled = true));
      msg.className = "msg"; msg.textContent = `Saving to ${c.code}…`;
      const r = await chrome.runtime.sendMessage({ type: "save-tab", tabId: tab.id, courseId: c.id });
      msg.className = "msg " + (r.ok ? "ok" : "bad");
      msg.textContent = r.ok ? `Saved to ${c.code} › ${r.folder}${r.saved_as}` : r.error;
      buttons.forEach((x) => (x.disabled = false));
    };
    return b;
  });
  root.append(...buttons, msg);
})();
