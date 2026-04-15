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
const fileFilterId = params.get("file") ? parseInt(params.get("file"), 10) : null;
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
    // If we arrived with ?file=... pre-select the first flagged row of that
    // file so the editor and preview land on the spot that needs attention.
    let initial = null;
    if (fileFilterId) {
      const fileRows = state.rows.filter(r => r.source_file_id === fileFilterId);
      initial = fileRows.find(r => r.review_required)
             || fileRows.find(r => r.confidence_score != null && r.confidence_score < 0.55)
             || fileRows[0];
    } else if (state.rows.length) {
      initial = state.rows[0];
    }
    if (initial && state.selected == null) {
      state.selected = initial.id;
      state.fileId = initial.source_file_id;
      state.page = initial.page_no || 1;
    }
    render();
    if (state.selected != null) {
      loadAudit(state.selected);
      await ensurePageCount();
    }
  } catch (e) { msg("Load failed: " + e.message, "error"); }
}

function rowMatches(r) {
  if (fileFilterId && r.source_file_id !== fileFilterId) return false;
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
    // Determine urgency: review_required + not yet reviewed/corrected = urgent
    const isUrgent = r.review_required && !r.row_reviewed && !r.is_corrected;
    const isHighPriority = r.review_priority === "high" || r.review_priority === "urgent";

    const d = document.createElement("div");
    d.className = "review-row" +
      (r.review_required ? " flagged" : "") +
      (r.is_corrected    ? " corrected" : "") +
      (r.row_reviewed    ? " reviewed" : "") +
      ((isUrgent || isHighPriority) ? " urgent" : "") +
      (r.id === state.selected ? " selected" : "");

    // Badge line
    const badges = [];
    if (isUrgent || isHighPriority) badges.push(`<span class="row-badge row-badge-urgent">Needs review</span>`);
    else if (r.review_required && r.row_reviewed) badges.push(`<span class="row-badge row-badge-reviewed">Reviewed</span>`);
    else if (r.is_corrected) badges.push(`<span class="row-badge row-badge-corrected">Corrected</span>`);

    const conf = r.confidence_score != null
      ? `<span class="row-conf${r.confidence_score < 0.55 ? " row-conf-low" : r.confidence_score < 0.75 ? " row-conf-mid" : ""}">${(r.confidence_score * 100).toFixed(0)}%</span>`
      : "";

    const toolBadge = (() => {
      const m = (r.method_used || "").toLowerCase();
      if (m.includes("azure_di") || m.includes("_di")) return '<span class="tool-badge tool-di">DI</span>';
      if (m.includes("openai") || m.includes("vision") || m.includes("_ai")) return '<span class="tool-badge tool-ai">AI</span>';
      if (m.includes("ocr")) return '<span class="tool-badge tool-ocr">OCR</span>';
      if (m) return '<span class="tool-badge tool-native">TXT</span>';
      return "";
    })();

    d.innerHTML =
      `<div class="row-primary">
         <span class="row-supplier">${esc(r.current.supplier_name) || "<em>no supplier</em>"}</span>
         <span class="row-amount">${r.current.total_amount != null ? esc(String(r.current.total_amount)) : ""}</span>
         ${toolBadge}
       </div>
       <div class="row-meta">
         <span>${esc(r.source_filename || "file")}</span>
         <span>p.${r.page_no}</span>
         <span>#${r.id}</span>
         ${conf}
       </div>
       ${badges.length ? `<div class="row-badges">${badges.join("")}</div>` : ""}`;

    d.onclick = async () => {
      state.selected = r.id; state.fileId = r.source_file_id; state.page = r.page_no || 1;
      render(); loadAudit(r.id); await ensurePageCount();
      if ($("remapMode").checked) refreshPreview();
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

  // ── Header block: tool label + reasons (rendered OUTSIDE .field-grid) ────
  let header = '';
  const toolLabel = (() => {
    const m = (r.method_used || "").toLowerCase();
    if (m.includes("azure_di") || m.includes("_di"))           return "Azure Document Intelligence (DI)";
    if (m.includes("openai") || m.includes("vision") || m.includes("_ai")) return "AI (OpenAI / Vision)";
    if (m.includes("ocr"))  return "OCR";
    if (m)                  return "Native text extraction";
    return "";
  })();
  if (toolLabel) header += `<div class="editor-source-label"><strong>Source:</strong> ${esc(toolLabel)}</div>`;

  const REASON_LABELS = {
    no_supplier:             "Supplier unclear",
    invoice_number_missing:  "Invoice number missing",
    no_amount:               "No amount found",
    ambiguous_date_locale:   "Date format ambiguous",
    vat_missing:             "VAT amount missing",
    vat_anomaly:             "VAT rate unusual",
    totals_mismatch:         "Totals do not reconcile",
    low_confidence:          "Low extraction confidence",
    deposit_component_detected: "Deposit/BCRS detected",
    subtotal_not_found:      "Sub-total not found",
  };
  const globalReasons = [];
  const reasonMap = {};
  (r.review_reasons || []).forEach(raw => {
    const s2 = String(raw || "");
    const ci = s2.indexOf(":");
    if (ci > 0) {
      const field = s2.slice(ci + 1);
      const code  = s2.slice(0, ci);
      if (!reasonMap[field]) reasonMap[field] = [];
      reasonMap[field].push(REASON_LABELS[code] || code.replace(/_/g, " "));
    } else {
      globalReasons.push(REASON_LABELS[s2] || s2.replace(/_/g, " "));
    }
  });
  if (r.review_required && globalReasons.length) {
    header += `<div class="review-reasons-banner">&#9888; ${globalReasons.map(esc).join(" &middot; ")}</div>`;
  }

  // ── Field grid ────────────────────────────────────────────────────────────
  let html = '<div class="field-grid">';
  FIELDS.forEach(f => {
    const cur = r.current[f] == null ? "" : r.current[f];
    const orig = r.original[f] == null ? "" : r.original[f];
    const flagged = (r.review_fields || []).includes(f);
    const fieldReasons = reasonMap[f] || [];
    const reasonHtml = fieldReasons.length
      ? `<div class="field-reason">&#9888; ${fieldReasons.map(esc).join(" &middot; ")}</div>` : "";
    html +=
      `<label>${esc(f)}${flagged ? " \u26a0" : ""}</label>
       <input data-field="${esc(f)}"${flagged ? ' class="flagged-field"' : ''} value="${esc(cur)}" />
       <label class="rule-cb"><input type="checkbox" data-rule="${esc(f)}" /> rule</label>
       <button class="btn btn-secondary" data-revert="${esc(f)}" type="button" title="Revert to original">&#8630;</button>
       <div class="orig">original: ${esc(orig) || "\u2014"}${reasonHtml}</div>`;
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
  ed.innerHTML = header + html;
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

let _previewBlobUrl = null;
function _showPreviewUnavailable(message) {
  const img = $("previewImg");
  const ph  = $("previewUnavailable");
  img.src = "";
  img.hidden = true;
  if (ph) {
    const msgEl = $("previewUnavailableMsg");
    if (msgEl && message) msgEl.textContent = message;
    ph.hidden = false;
  }
}
function _showPreviewImage(blobUrl) {
  const img = $("previewImg");
  const ph  = $("previewUnavailable");
  if (ph) ph.hidden = true;
  img.hidden = false;
  img.src = blobUrl;
}
async function refreshPreview() {
  const img = $("previewImg");
  if (!state.fileId) {
    img.src = ""; img.hidden = true;
    const ph = $("previewUnavailable"); if (ph) ph.hidden = true;
    $("pageLabel").textContent = "page — / —";
    return;
  }
  // Reset to loading state: hide placeholder, show (empty) img
  const ph = $("previewUnavailable"); if (ph) ph.hidden = true;
  img.hidden = false;
  updatePageControls();
  try {
    const r = await fetch(`/review/files/${state.fileId}/preview?page=${state.page}`, { headers: hdrs() });
    if (!r.ok) {
      let detail = `${r.status} ${r.statusText}`;
      try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch {}
      // Surface friendly message both in banner and in the preview panel
      const friendly = detail.includes("missing from disk") || detail.includes("not found") || detail.includes("404")
        ? "Source PDF is no longer available on disk."
        : detail.includes("out of range")
        ? "This page is out of range for the source file."
        : `Preview unavailable: ${detail}`;
      msg(friendly, "error");
      _showPreviewUnavailable(friendly);
      return;
    }
    const blob = await r.blob();
    if (_previewBlobUrl) { URL.revokeObjectURL(_previewBlobUrl); }
    _previewBlobUrl = URL.createObjectURL(blob);
    _showPreviewImage(_previewBlobUrl);
  } catch (e) {
    const friendly = e && e.message ? `Preview error: ${e.message}` : "Preview could not be loaded.";
    msg(friendly, "error");
    _showPreviewUnavailable(friendly);
  }
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
$("exportBtn").onclick = async () => {
  // Use fetch with auth headers so the Bearer token is sent; <a href>/location
  // cannot carry Authorization headers and would return "Missing Bearer token".
  try {
    const r = await fetch(`/batches/${batchId}/export`, { headers: hdrs() });
    if (!r.ok) { msg(await r.text(), "error"); return; }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
    const name = m ? decodeURIComponent(m[1]) : `batch_${batchId}.xlsx`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click();
    a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000);
    msg("Export downloaded.", "success");
    load();  // refresh batch state (status, version)
  } catch (e) {
    msg(String(e), "error");
  }
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

// refreshPreview() now surfaces exact server errors via msg(); no <img> onerror needed.

function setRemapField(name) {
  remapField = name || null;
  remapTargetLabel.textContent = remapField ? `field: ${remapField}` : "";
  // Only now (remap mode on + field chosen) do we load the preview image.
  if ($("remapMode").checked && remapField && state.fileId && !previewImg.src) {
    refreshPreview();
  }
}

// Any input/select/textarea inside the row editor with a data-field attribute
// becomes a remap target when focused or clicked.
document.addEventListener("focusin", (e) => {
  const el = e.target.closest("#rowEditor [data-field]");
  if (el) setRemapField(el.getAttribute("data-field"));
});
document.addEventListener("click", (e) => {
  const el = e.target.closest("#rowEditor [data-field]");
  if (!el) return;
  setRemapField(el.getAttribute("data-field"));
});

// Returns null if remap is allowed, or a string reason why it is locked
function remapLockReason() {
  if (!state.batch) return "Batch not loaded";
  const st = (state.batch.status || "").toLowerCase();
  if (st === "exported") return "Batch is exported — reopen to remap";
  if (st === "approved") return "Batch is approved — reopen to remap";
  if (state.selected != null) {
    const row = state.rows.find(x => x.id === state.selected);
    if (row && row.row_reviewed) return "This row is marked reviewed — reopen to remap";
  }
  return null;
}

$("remapMode").addEventListener("change", async (e) => {
  const on = e.target.checked;
  if (on) {
    const reason = remapLockReason();
    if (reason) {
      e.target.checked = false;
      msg(reason, "error");
      return;
    }
  }
  previewWrap.classList.toggle("remap-active", on);
  remapHint.hidden = !on;
  if (!on) {
    remapSel.hidden = true; dragStart = null;
    previewImg.src = ""; previewImg.hidden = true;
    const ph = $("previewUnavailable"); if (ph) ph.hidden = true;
    return;
  }
  if (!state.fileId && state.rows.length) {
    const r0 = state.rows[0];
    state.selected = r0.id; state.fileId = r0.source_file_id; state.page = r0.page_no || 1;
    render(); loadAudit(r0.id); await ensurePageCount();
  }
  if (!remapField) msg("Click a field in the editor to activate remap for that field.", "");
  else if (state.fileId) refreshPreview();
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
  const lockMsg = remapLockReason();
  if (lockMsg) { remapSel.hidden = true; msg(lockMsg, "error"); return; }
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
      apply_as_value: true,
    }),
  });
  if (!r.ok) { msg(await r.text(), "error"); return; }
  const data = await r.json().catch(() => ({}));
  // If the backend was able to read text from the region, drop it straight
  // into the field input so the correction can be saved normally.
  if (data && data.read_text) {
    const inp = document.querySelector(`#rowEditor [data-field="${remapField}"]`);
    if (inp) {
      inp.value = data.read_text;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      inp.focus();
    }
    msg(`Remap saved — read "${data.read_text}" into ${remapField}. Click Save corrections to apply.`, "success");
  } else {
    msg(`Remap saved for ${remapField} (no text detected — region stored for future learning).`, "success");
  }
  setTimeout(() => { remapSel.hidden = true; }, 1200);
});

if (typeof ensureAuth === "function" && !ensureAuth()) {
  // ensureAuth() will redirect to /login
} else {
  load();
}

// ── File-scoped "Mark file reviewed" (review-as-you-go) ─────────────────────
// When the review page was opened from the scanner's "Review now" button
// (?file=<id>), we show a dedicated button that flips all flagged rows in
// that single file to reviewed=true in one shot, then closes the tab so the
// user can return to the scanner and tackle the next invoice.
(function wireMarkFileReviewed() {
  const btn = $("markFileReviewedBtn");
  if (!btn) return;
  if (fileFilterId) btn.hidden = false;
  btn.onclick = async () => {
    if (!fileFilterId) return;
    if (!confirm("Mark every flagged row in this file as reviewed?")) return;
    try {
      const r = await fetch(`/review/batches/${batchId}/files/${fileFilterId}/reviewed`,
                            { method: "POST", headers: hdrs() });
      if (!r.ok) { msg(await r.text(), "error"); return; }
      const d = await r.json().catch(() => ({}));
      msg(`File marked reviewed (${d.marked_rows || 0} row(s) updated).`, "success");
      setTimeout(() => { try { window.close(); } catch {} }, 900);
    } catch (e) {
      msg(String(e), "error");
    }
  };
})();

const remapDefault = $("remapMode"); if (remapDefault) remapDefault.checked = false;
