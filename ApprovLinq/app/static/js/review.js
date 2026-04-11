// Review workspace JS — talks to /review/* endpoints. Uses element IDs from the
// restyled review.html. Auth token comes from common.js (window.api/getToken)
// when available; otherwise falls back to localStorage.
const FIELDS = [
  "supplier_name", "supplier_posting_account", "nominal_account_code",
  "invoice_number", "invoice_date", "description",
  "net_amount", "vat_amount", "total_amount", "currency", "tax_code",
];
const params = new URLSearchParams(location.search);
const batchId = params.get("batch_id");
let state = { batch: null, rows: [], filter: "all", selected: null, page: 1, fileId: null, pageCount: 1 };

const $ = (id) => document.getElementById(id);
// Use the existing app's auth helpers from common.js — token key is "approvlinq_token"
// and authHeaders() also adds the X-Tenant-Id header that tenant-scoped routes require.
const hdrs = () => authHeaders({ "Content-Type": "application/json" });
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function msg(text, kind) {
  const m = $("pageMessage");
  if (!m) return;
  m.textContent = text || "";
  m.className = "message" + (kind ? " " + kind : "");
}

async function load() {
  if (!batchId) { msg("Missing batch_id in URL", "error"); return; }
  try {
    const r = await fetch(`/review/batches/${batchId}`, { headers: hdrs() });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    state.batch = d.batch;
    state.rows = d.rows;
    if (state.rows.length && state.selected == null) {
      state.selected = state.rows[0].id;
      state.fileId = state.rows[0].source_file_id;
      state.page = state.rows[0].page_no || 1;
    }
    render();
    if (state.selected != null) { loadAudit(state.selected); await ensurePageCount(); refreshPreview(); }
  } catch (e) { msg("Load failed: " + e.message, "error"); }
}

function rowMatches(r) {
  if (state.filter === "needs_review") return r.review_required;
  if (state.filter === "corrected")    return r.is_corrected;
  if (state.filter === "low_conf")     return r.confidence_score != null && r.confidence_score < 0.7;
  return true;
}

function render() {
  const b = state.batch;
  $("batchTitle").textContent = b.name;
  const pill = $("batchStatusPill");
  pill.textContent = b.status;
  pill.className = "version-badge pill " + b.status;
  $("statRows").textContent      = b.row_count;
  $("statCorrected").textContent = b.corrected_count;
  $("statFlagged").textContent   = b.flagged_count;
  $("statVersion").textContent   = "v" + (b.current_export_version || 0);

  const list = $("rowList");
  list.innerHTML = "";
  state.rows.filter(rowMatches).forEach(r => {
    const d = document.createElement("div");
    d.className = "review-row" +
      (r.review_required ? " flagged" : "") +
      (r.is_corrected ? " corrected" : "") +
      (r.id === state.selected ? " selected" : "");
    d.innerHTML =
      `<div><strong>${esc(r.current.supplier_name) || "<no supplier>"}</strong> · ${esc(r.current.total_amount) || ""}</div>
       <div class="meta">${esc(r.source_filename || "file")} · page ${r.page_no} · row #${r.id}${r.confidence_score != null ? " · conf " + r.confidence_score.toFixed(2) : ""}</div>`;
    d.onclick = async () => {
      state.selected = r.id; state.fileId = r.source_file_id; state.page = r.page_no || 1;
      render(); loadAudit(r.id); await ensurePageCount(); refreshPreview();
    };
    list.appendChild(d);
  });

  document.querySelectorAll(".filter-chips .btn").forEach(b => {
    b.classList.toggle("active", b.dataset.filter === state.filter);
  });

  renderEditor();
}

function renderEditor() {
  const r = state.rows.find(x => x.id === state.selected);
  const ed = $("rowEditor");
  if (!r) { ed.innerHTML = '<div class="muted">Select a row from the left.</div>'; return; }
  let html = '<div class="field-grid">';
  FIELDS.forEach(f => {
    const cur = r.current[f] == null ? "" : r.current[f];
    const orig = r.original[f] == null ? "" : r.original[f];
    const flagged = (r.review_fields || []).includes(f);
    html +=
      `<label>${esc(f)}${flagged ? " ⚠" : ""}</label>
       <input data-field="${esc(f)}" value="${esc(cur)}" />
       <label class="rule-cb"><input type="checkbox" data-rule="${esc(f)}" /> rule</label>
       <button class="btn btn-secondary" data-revert="${esc(f)}" type="button" title="Revert to original">↶</button>
       <div class="orig">original: ${esc(orig) || "—"}</div>`;
  });
  html += "</div>";
  html +=
    `<div class="stack" style="margin-top:10px">
      <label class="row gap-sm" style="align-items:center">
        <input type="checkbox" id="forceAdd" /> Force add new supplier/nominal (note required)
      </label>
      <textarea id="note" class="message" placeholder="Reason / note (required for force-add)" style="min-height:50px"></textarea>
      <div class="row gap-sm wrap">
        <button id="saveBtn" class="btn btn-primary" type="button">Save corrections</button>
      </div>
    </div>`;
  ed.innerHTML = html;
  $("saveBtn").onclick = saveRow;
  ed.querySelectorAll("[data-revert]").forEach(b => b.onclick = () => revertField(b.dataset.revert));
}

async function saveRow() {
  const r = state.rows.find(x => x.id === state.selected);
  const changes = {}; const ruleFields = [];
  document.querySelectorAll("#rowEditor [data-field]").forEach(i => {
    const f = i.dataset.field;
    const v = i.value === "" ? null : i.value;
    if (String(v == null ? "" : v) !== String(r.current[f] == null ? "" : r.current[f])) changes[f] = v;
  });
  document.querySelectorAll("#rowEditor [data-rule]:checked").forEach(c => ruleFields.push(c.dataset.rule));
  const body = {
    changes,
    note: $("note").value || null,
    force_add: $("forceAdd").checked,
    save_as_rule_fields: ruleFields,
  };
  try {
    const res = await fetch(`/review/batches/${batchId}/rows/${r.id}`, {
      method: "PATCH", headers: hdrs(), body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    msg("Saved", "success");
    await load();
  } catch (e) { msg("Save failed: " + e.message, "error"); }
}

async function revertField(f) {
  const r = state.rows.find(x => x.id === state.selected);
  await fetch(`/review/batches/${batchId}/rows/${r.id}/revert/${f}`, { method: "POST", headers: hdrs() });
  await load();
}

async function loadAudit(rowId) {
  try {
    const r = await fetch(`/review/batches/${batchId}/rows/${rowId}/audit`, { headers: hdrs() });
    const list = await r.json();
    $("auditList").innerHTML = list.map(a =>
      `<div class="audit-entry">
        <strong>${esc(a.field)}</strong> ${esc(a.action)}: ${esc(a.old) || "∅"} → ${esc(a.new) || "∅"}
        <span class="muted">(${esc(a.username) || "?"})</span>
        ${a.rule_created ? '<span class="badge rule">+rule</span>' : ""}
        ${a.force_added ? '<span class="badge force">+force</span>' : ""}
      </div>`).join("") || '<div class="muted">No history yet.</div>';
  } catch (e) { /* ignore */ }
}

async function fetchPageCount() {
  if (!state.fileId) { state.pageCount = 1; return; }
  try {
    const r = await fetch(`/review/files/${state.fileId}/info`, { headers: hdrs() });
    if (r.ok) {
      const d = await r.json();
      state.pageCount = Math.max(1, d.page_count || 1);
    } else { state.pageCount = 1; }
  } catch { state.pageCount = 1; }
  if (state.page > state.pageCount) state.page = state.pageCount;
  if (state.page < 1) state.page = 1;
  updatePageControls();
}

function updatePageControls() {
  $("pageLabel").textContent = `page ${state.page} / ${state.pageCount}`;
  $("prevPageBtn").disabled = state.page <= 1;
  $("nextPageBtn").disabled = state.page >= state.pageCount;
}

function refreshPreview() {
  if (!state.fileId) { $("previewImg").src = ""; $("pageLabel").textContent = "page — / —"; return; }
  $("previewImg").src = `/review/files/${state.fileId}/preview?page=${state.page}&t=${Date.now()}`;
  updatePageControls();
}

// Re-fetch page count whenever the file changes
let _lastFileId = null;
async function ensurePageCount() {
  if (state.fileId !== _lastFileId) {
    _lastFileId = state.fileId;
    await fetchPageCount();
  }
}

$("prevPageBtn").onclick = async () => {
  await ensurePageCount();
  if (state.page > 1) { state.page--; refreshPreview(); }
};
$("nextPageBtn").onclick = async () => {
  await ensurePageCount();
  if (state.page < state.pageCount) { state.page++; refreshPreview(); }
};



document.querySelectorAll(".filter-chips .btn").forEach(b => {
  b.onclick = () => { state.filter = b.dataset.filter; render(); };
});

$("approveBtn").onclick = async () => {
  const r = await fetch(`/review/batches/${batchId}/transition`, {
    method: "POST", headers: hdrs(), body: JSON.stringify({ target: "approved" }),
  });
  if (!r.ok) msg(await r.text(), "error"); else load();
};
$("exportBtn").onclick = () => {
  // Trigger the existing /batches/{id}/export endpoint (now corrected-aware)
  window.location.href = `/batches/${batchId}/export`;
};
$("reopenBtn").onclick = async () => {
  const r = await fetch(`/review/batches/${batchId}/reopen`, { method: "POST", headers: hdrs() });
  if (!r.ok) msg(await r.text(), "error"); else load();
};

// ── Remap mode ──────────────────────────────────────────────────────────────
// Track which field the user last clicked/focused in the row editor so we
// don't have to prompt for a name on every drag.
let remapField = null;
const remapHint = $("remapHint");
const remapTargetLabel = $("remapTargetLabel");
const previewWrap = $("previewWrap");
const previewImg = $("previewImg");
const remapSel = $("remapSelection");

function setRemapField(name) {
  remapField = name || null;
  remapTargetLabel.textContent = remapField ? `field: ${remapField}` : "";
}

// Any input/select/textarea inside the row editor with a data-field attribute
// becomes a remap target when focused or clicked.
document.addEventListener("focusin", (e) => {
  const el = e.target.closest("#rowEditor [data-field]");
  if (el) setRemapField(el.getAttribute("data-field"));
});
document.addEventListener("click", (e) => {
  const el = e.target.closest("#rowEditor [data-field]");
  if (el) setRemapField(el.getAttribute("data-field"));
});

$("remapMode").addEventListener("change", async (e) => {
  const on = e.target.checked;
  previewWrap.classList.toggle("remap-active", on);
  remapHint.hidden = !on;
  if (!on) { remapSel.hidden = true; dragStart = null; return; }
  // If no row / file loaded yet, auto-select first row so the preview opens.
  if (!state.fileId && state.rows.length) {
    const r0 = state.rows[0];
    state.selected = r0.id; state.fileId = r0.source_file_id; state.page = r0.page_no || 1;
    render(); loadAudit(r0.id); await ensurePageCount(); refreshPreview();
  } else if (state.fileId && !previewImg.src) {
    refreshPreview();
  }
  if (!remapField) msg("Click a field in the editor, then drag on the preview.", "");
});

let dragStart = null;        // {xPx, yPx} pixel coords relative to image top-left
let imgRectCache = null;     // cached image getBoundingClientRect

function imgPxFromEvent(e) {
  const r = previewImg.getBoundingClientRect();
  const x = Math.min(r.width,  Math.max(0, e.clientX - r.left));
  const y = Math.min(r.height, Math.max(0, e.clientY - r.top));
  return { xPx: x, yPx: y, w: r.width, h: r.height };
}
function drawSel(a, b) {
  // Position the overlay in pixel coordinates relative to the WRAPPER,
  // by computing the image's offset inside the wrapper. This guarantees the
  // rectangle aligns to the rendered image regardless of wrapper padding/margins.
  const imgRect = previewImg.getBoundingClientRect();
  const wrapRect = previewWrap.getBoundingClientRect();
  const offX = imgRect.left - wrapRect.left;
  const offY = imgRect.top  - wrapRect.top;
  const x = Math.min(a.xPx, b.xPx);
  const y = Math.min(a.yPx, b.yPx);
  const w = Math.abs(b.xPx - a.xPx);
  const h = Math.abs(b.yPx - a.yPx);
  remapSel.style.left   = (offX + x) + "px";
  remapSel.style.top    = (offY + y) + "px";
  remapSel.style.width  = w + "px";
  remapSel.style.height = h + "px";
  remapSel.hidden = false;
  return { x: x / a.w, y: y / a.h, wN: w / a.w, hN: h / a.h };
}

previewImg.addEventListener("mousedown", (e) => {
  if (!$("remapMode").checked) return;
  if (!remapField) { msg("Click a field in the editor first, then drag on the preview.", "error"); return; }
  e.preventDefault();
  dragStart = imgPxFromEvent(e);
  drawSel(dragStart, dragStart);
});
previewWrap.addEventListener("mousemove", (e) => {
  if (!dragStart) return;
  drawSel(dragStart, imgPxFromEvent(e));
});
window.addEventListener("mouseup", async (e) => {
  if (!dragStart) return;
  const end = imgPxFromEvent(e);
  const region = drawSel(dragStart, end);
  dragStart = null;
  if (region.wN < 0.01 || region.hN < 0.01) {
    remapSel.hidden = true;
    return; // accidental click / too-small
  }
  const row = state.rows.find(x => x.id === state.selected);
  if (!row) { msg("Select a row first.", "error"); return; }
  if (!confirm(`Save region for field "${remapField}" on page ${state.page}?`)) {
    remapSel.hidden = true;
    return;
  }
  const r = await fetch(`/review/batches/${batchId}/rows/${row.id}/remap`, {
    method: "POST", headers: hdrs(),
    body: JSON.stringify({
      field_name: remapField,
      page_no: state.page,
      x: region.x, y: region.y, w: region.wN, h: region.hN,
      file_id: state.fileId,
    }),
  });
  if (!r.ok) { msg(await r.text(), "error"); return; }
  msg(`Remap saved for ${remapField}`, "success");
  setTimeout(() => { remapSel.hidden = true; }, 800);
});

if (typeof ensureAuth === "function" && !ensureAuth()) {
  // ensureAuth() will redirect to /login
} else {
  load();
}
