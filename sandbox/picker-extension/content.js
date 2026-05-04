// Cloak File Picker — content script (ISOLATED world).
//
// Goal: when the SaaS opens an OS file picker (via user click on
// <input type="file"> or programmatic .click()), suppress the GTK dialog
// and show our own modal listing files from ~/uploads/. User picks one,
// we fetch it from http://127.0.0.1:6902/file/<name>, wrap it in a File,
// and assign it to the input via DataTransfer. SaaS sees a native
// 'change' event and gets a real File object — no idea anything was
// substituted.
//
// Why this exists: if we let Chromium open the GTK picker, the streamed
// pixels expose the entire kasm desktop (~/Recent, ~/Desktop, side panel
// with Other Locations, etc). The whole point of cloak is the user only
// ever sees the SaaS — not the sandbox host.

const INBOX = "http://127.0.0.1:6902";
const STYLE_ID = "cloak-picker-style";
const MODAL_ID = "cloak-picker-modal";

let activeInput = null;

// --- intercept clicks on file inputs in capture phase --------------------
// Chromium fires a 'click' event on the input BEFORE opening the OS picker,
// for both user clicks and programmatic .click() calls. Calling
// preventDefault() in the capture phase stops the dialog from opening.
function shouldIntercept(target) {
  if (!target) return null;
  // direct input
  if (target.tagName === "INPUT" && target.type === "file") return target;
  // <label> wrapping or [for=]-targeting an input
  if (target.tagName === "LABEL") {
    const lbl = target;
    if (lbl.htmlFor) {
      const el = document.getElementById(lbl.htmlFor);
      if (el && el.tagName === "INPUT" && el.type === "file") return el;
    }
    const inner = lbl.querySelector('input[type="file"]');
    if (inner) return inner;
  }
  // any ancestor up to a few levels (some sites wrap the input deeply)
  let cur = target;
  for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
    if (cur.tagName === "INPUT" && cur.type === "file") return cur;
  }
  return null;
}

document.addEventListener(
  "click",
  (ev) => {
    const input = shouldIntercept(ev.target);
    if (!input) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    activeInput = input;
    openModal();
  },
  true,
);

// Some pages call input.click() programmatically without a user click
// reaching the input. The above handler still catches it because click()
// dispatches a synthetic click event that bubbles. Just in case a page
// somehow bypasses it (e.g., dispatchEvent on a detached input), also
// patch HTMLInputElement.prototype.showPicker which is the modern API.
try {
  const proto = HTMLInputElement.prototype;
  if (proto.showPicker) {
    const orig = proto.showPicker;
    proto.showPicker = function () {
      if (this.type === "file") {
        activeInput = this;
        openModal();
        return;
      }
      return orig.apply(this, arguments);
    };
  }
} catch (_) {
  /* prototype patch may fail in strict modes; capture-phase listener still works */
}

// --- modal UI ------------------------------------------------------------
function ensureStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const s = document.createElement("style");
  s.id = STYLE_ID;
  s.textContent = `
    #${MODAL_ID} {
      position: fixed; inset: 0; z-index: 2147483647;
      background: rgba(15, 18, 26, 0.72);
      display: flex; align-items: center; justify-content: center;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #1a1f2c;
    }
    #${MODAL_ID} .cl-card {
      width: min(560px, 92vw);
      max-height: 80vh;
      background: #fff;
      border-radius: 14px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.4);
      display: flex; flex-direction: column; overflow: hidden;
    }
    #${MODAL_ID} .cl-hd {
      padding: 18px 20px 12px;
      border-bottom: 1px solid #e6e8ee;
    }
    #${MODAL_ID} .cl-hd h2 { margin: 0; font-size: 16px; font-weight: 600; }
    #${MODAL_ID} .cl-hd p { margin: 6px 0 0; font-size: 12px; color: #5b6273; }
    #${MODAL_ID} .cl-list {
      flex: 1; overflow-y: auto; padding: 8px 0;
    }
    #${MODAL_ID} .cl-empty {
      padding: 32px 20px; text-align: center; color: #6b7280; font-size: 13px;
    }
    #${MODAL_ID} .cl-row {
      display: flex; align-items: center; gap: 12px;
      padding: 10px 20px; cursor: pointer;
      border: none; background: none; width: 100%; text-align: left;
      font: inherit; color: inherit;
    }
    #${MODAL_ID} .cl-row:hover, #${MODAL_ID} .cl-row:focus {
      background: #f3f5fa; outline: none;
    }
    #${MODAL_ID} .cl-icon {
      width: 28px; height: 28px; flex: 0 0 28px;
      border-radius: 6px;
      background: #eef1f7; color: #4b5366;
      display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 600;
    }
    #${MODAL_ID} .cl-meta { flex: 1; min-width: 0; }
    #${MODAL_ID} .cl-name {
      font-size: 13px; font-weight: 500;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    #${MODAL_ID} .cl-sub { font-size: 11px; color: #6b7280; margin-top: 2px; }
    #${MODAL_ID} .cl-ft {
      padding: 12px 20px; border-top: 1px solid #e6e8ee;
      display: flex; justify-content: space-between; align-items: center;
      gap: 10px;
    }
    #${MODAL_ID} .cl-hint { font-size: 11px; color: #6b7280; }
    #${MODAL_ID} button.cl-btn {
      padding: 8px 14px; border-radius: 8px; border: 1px solid #d4d8e1;
      background: #fff; cursor: pointer; font: inherit; font-size: 13px;
    }
    #${MODAL_ID} button.cl-btn.cl-cancel { color: #5b6273; }
    #${MODAL_ID} .cl-err { color: #b42318; font-size: 12px; }
  `;
  document.documentElement.appendChild(s);
}

function fmtSize(n) {
  if (!Number.isFinite(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function fmtTime(epochSec) {
  if (!epochSec) return "";
  try {
    const d = new Date(epochSec * 1000);
    return d.toLocaleString();
  } catch {
    return "";
  }
}

function ext(name) {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1, i + 5).toUpperCase() : "F";
}

function closeModal() {
  const el = document.getElementById(MODAL_ID);
  if (el) el.remove();
}

async function openModal() {
  ensureStyles();
  closeModal();

  const root = document.createElement("div");
  root.id = MODAL_ID;
  root.innerHTML = `
    <div class="cl-card" role="dialog" aria-modal="true" aria-label="Choose file to attach">
      <div class="cl-hd">
        <h2>Choose a file to attach</h2>
        <p>Files you sent through "Send file to sandbox" appear here.</p>
      </div>
      <div class="cl-list" id="cl-list-body">
        <div class="cl-empty">Loading…</div>
      </div>
      <div class="cl-ft">
        <span class="cl-hint" id="cl-hint">Click "Send file to sandbox" first if the list is empty.</span>
        <button class="cl-btn cl-cancel" id="cl-cancel">Cancel</button>
      </div>
    </div>
  `;
  document.documentElement.appendChild(root);

  root.addEventListener("click", (e) => {
    if (e.target === root) {
      closeModal();
    }
  });
  root.querySelector("#cl-cancel").addEventListener("click", closeModal);
  document.addEventListener("keydown", escClose, { once: true });

  const body = root.querySelector("#cl-list-body");
  let files = [];
  try {
    const r = await fetch(`${INBOX}/list`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    files = j.files || [];
  } catch (e) {
    body.innerHTML = `<div class="cl-empty cl-err">Couldn't reach the sandbox inbox: ${String(e)}</div>`;
    return;
  }

  if (files.length === 0) {
    body.innerHTML = `<div class="cl-empty">No files yet. Use "Send file to sandbox" in the toolbar to upload one.</div>`;
    return;
  }

  body.innerHTML = "";
  files.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  for (const f of files) {
    const btn = document.createElement("button");
    btn.className = "cl-row";
    btn.type = "button";
    btn.innerHTML = `
      <span class="cl-icon">${escapeHtml(ext(f.name))}</span>
      <span class="cl-meta">
        <span class="cl-name">${escapeHtml(f.name)}</span>
        <span class="cl-sub">${fmtSize(f.size)}${f.mtime ? " · " + escapeHtml(fmtTime(f.mtime)) : ""}</span>
      </span>
    `;
    btn.addEventListener("click", () => onPick(f.name));
    body.appendChild(btn);
  }
}

function escClose(e) {
  if (e.key === "Escape") closeModal();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

async function onPick(name) {
  const input = activeInput;
  const hint = document.getElementById("cl-hint");
  if (hint) hint.textContent = "Loading file…";
  try {
    const r = await fetch(`${INBOX}/file/${encodeURIComponent(name)}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    // Pick the best MIME we can. The SaaS's preview/validation keys off
    // file.type, so 'application/octet-stream' breaks PDF preview etc.
    // Order: server-provided -> extension lookup -> octet-stream.
    let mime = blob.type;
    if (!mime || mime === "application/octet-stream") {
      mime = guessMime(name);
    }
    const file = new File([blob.slice(0, blob.size, mime)], name, {
      type: mime,
      lastModified: Date.now(),
    });
    if (!input) {
      throw new Error("no input element to attach to");
    }
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    // Fire events the SaaS framework expects.
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    closeModal();
  } catch (e) {
    if (hint) {
      hint.textContent = `Failed: ${String(e)}`;
      hint.className = "cl-err";
    }
  }
}

// Minimal extension -> MIME map; covers the formats users actually attach.
const MIME_BY_EXT = {
  pdf: "application/pdf",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
  bmp: "image/bmp",
  tif: "image/tiff",
  tiff: "image/tiff",
  txt: "text/plain",
  log: "text/plain",
  md: "text/markdown",
  csv: "text/csv",
  tsv: "text/tab-separated-values",
  json: "application/json",
  xml: "application/xml",
  html: "text/html",
  htm: "text/html",
  yml: "application/x-yaml",
  yaml: "application/x-yaml",
  zip: "application/zip",
  gz: "application/gzip",
  tar: "application/x-tar",
  doc: "application/msword",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xls: "application/vnd.ms-excel",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ppt: "application/vnd.ms-powerpoint",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  mp3: "audio/mpeg",
  mp4: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
};

function guessMime(name) {
  const i = name.lastIndexOf(".");
  if (i < 0) return "application/octet-stream";
  const ext = name.slice(i + 1).toLowerCase();
  return MIME_BY_EXT[ext] || "application/octet-stream";
}
