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
let state = { batch: null, rows: [], filter: "all", selected: null, page: 1, fileId: null };

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
    if (state.selected != null) { loadAudit(state.selected); refreshPreview(); }
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
    d.onclick = () => {
      state.selected = r.id; state.fileId = r.source_file_id; state.page = r.page_no || 1;
      render(); loadAudit(r.id); refreshPreview();
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

function refreshPreview() {
  if (!state.fileId) { $("previewImg").src = ""; return; }
  $("previewImg").src = `/review/files/${state.fileId}/preview?page=${state.page}&t=${Date.now()}`;
  $("pageLabel").textContent = "page " + state.page;
}

$("prevPageBtn").onclick = () => { if (state.page > 1) { state.page--; refreshPreview(); } };
$("nextPageBtn").onclick = () => { state.page++; refreshPreview(); };

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

// Remap mode: drag a region on the preview to mark it for a field
let dragStart = null;
$("previewImg").addEventListener("mousedown", (e) => {
  if (!$("remapMode").checked) return;
  const r = e.target.getBoundingClientRect();
  dragStart = { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
});
$("previewImg").addEventListener("mouseup", async (e) => {
  if (!dragStart) return;
  const r = e.target.getBoundingClientRect();
  const x2 = (e.clientX - r.left) / r.width, y2 = (e.clientY - r.top) / r.height;
  const field = prompt("Which field is this region for?");
  if (!field) { dragStart = null; return; }
  const row = state.rows.find(x => x.id === state.selected);
  await fetch(`/review/batches/${batchId}/rows/${row.id}/remap`, {
    method: "POST", headers: hdrs(),
    body: JSON.stringify({
      field_name: field, page_no: state.page,
      x: Math.min(dragStart.x, x2), y: Math.min(dragStart.y, y2),
      w: Math.abs(x2 - dragStart.x), h: Math.abs(y2 - dragStart.y),
      file_id: state.fileId,
    }),
  });
  dragStart = null;
  msg("Remap saved", "success");
});

if (typeof ensureAuth === "function" && !ensureAuth()) {
  // ensureAuth() will redirect to /login
} else {
  load();
}
