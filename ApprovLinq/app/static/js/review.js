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
let state = { batch: null, rows: [], filter: "all", selected: null, page: 1, fileId: null, pageCount: 1, candidatesByRow: {} };

const $ = (id) => document.getElementById(id);
// Use the existing app's auth helpers from common.js — token key is "approvlinq_token"
// and authHeaders() also adds the X-Tenant-Id header that tenant-scoped routes require.
const hdrs = () => authHeaders({ "Content-Type": "application/json" });
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let _msgClearTimer = null;
function msg(text, kind) {
  const m = $("pageMessage");
  if (!m) return;
  if (_msgClearTimer) { clearTimeout(_msgClearTimer); _msgClearTimer = null; }
  m.textContent = text || "";
  m.className = "message" + (kind ? " " + kind : "");
  // Auto-clear success and plain info messages; keep errors/warnings visible
  if (text && (kind === "success" || kind === "" || !kind)) {
    _msgClearTimer = setTimeout(() => {
      if (m.textContent === text) { m.textContent = ""; m.className = "message"; }
      _msgClearTimer = null;
    }, 4000);
  }
}

function setApplySavedRegionsStatus(text, kind) {
  const el = $("applySavedRegionsStatus");
  if (!el) return;
  el.textContent = text || "";
  el.className = "muted" + (kind ? " " + kind : "");
}

function sourceBadge(source, label) {
  const src = String(source || "raw_extraction").replace(/[^a-z0-9_-]/gi, "_");
  return `<span class="evidence-badge evidence-${esc(src)}">${esc(label || source || "Raw extraction")}</span>`;
}

function pct(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `${Math.round(n * 100)}%` : "";
}

function compactReason(text, maxLen = 180) {
  const t = String(text || "").replace(/_/g, " ").replace(/\s+/g, " ").trim();
  return t.length > maxLen ? t.slice(0, maxLen - 1) + "…" : t;
}

function candidateBadges(c) {
  const badges = [];
  if (c.selected) badges.push('<span class="candidate-badge selected">Selected</span>');
  if (c.applied) badges.push('<span class="candidate-badge applied">Applied</span>');
  if (c.conflict) badges.push('<span class="candidate-badge conflict">Conflict</span>');
  if (!c.selected && !c.applied && !c.conflict) {
    badges.push(c.rejected_reason ? '<span class="candidate-badge rejected">Rejected</span>' : '<span class="candidate-badge suggested">Suggested</span>');
  }
  return badges.join(' ');
}

function renderPersistedCandidate(c) {
  const source = sourceBadge(c.source_type, c.source_label || c.source_type);
  const conf = c.confidence != null ? ` <span class="muted">${esc(pct(c.confidence))}</span>` : "";
  const statusClass = c.conflict ? " conflict" : c.applied ? " applied" : c.selected ? " selected" : c.rejected_reason ? " rejected" : " suggested";
  const note = c.rejected_reason || c.reason || c.evidence || "";
  return `<div class="field-candidate persisted${statusClass}">
    <div>${source} <strong>${esc(c.candidate_value ?? "—")}</strong>${conf} ${candidateBadges(c)}</div>
    ${c.evidence ? `<div class="muted">Evidence: ${esc(compactReason(c.evidence, 180))}</div>` : ""}
    ${note ? `<div class="muted">${esc(compactReason(note, 180))}</div>` : ""}
  </div>`;
}

function renderRowExplainability(r) {
  const ex = r.explainability || {};
  const row = ex.row || {};
  const reasons = row.review_reasons || r.review_reasons || [];
  const tags = row.method_tags || (r.method_used || "").split(/[+|,]/).filter(Boolean);
  const totals = row.totals_reconciliation_status || r.totals_reconciliation_status || "";
  const bcrs = row.bcrs_or_discount || {};
  const parts = [];
  if (r.blocked_from_export || (r.row_status && r.row_status !== "active")) {
    parts.push(`<div><strong>Export status:</strong> ${esc((r.row_status || "blocked").replaceAll("_", " "))}</div>`);
    if (r.row_status_note) {
      parts.push(`<div><strong>Duplicate remark:</strong> ${esc(compactReason(r.row_status_note, 240))}</div>`);
    }
  }
  parts.push(`<div><strong>Confidence:</strong> ${r.confidence_score != null ? esc(pct(r.confidence_score)) : "—"}</div>`);
  parts.push(`<div><strong>Review:</strong> ${r.review_required ? "Required" : "Not required"}</div>`);
  if (totals) parts.push(`<div><strong>Totals:</strong> ${esc(totals)}</div>`);
  if (bcrs.bcrs_detected || bcrs.discount_detected) {
    parts.push(`<div><strong>BCRS/Discount:</strong> ${bcrs.bcrs_detected ? "BCRS/deposit evidence" : ""}${bcrs.bcrs_detected && bcrs.discount_detected ? " · " : ""}${bcrs.discount_detected ? "discount evidence" : ""}</div>`);
  }
  if (r.method_used) parts.push(`<div><strong>Method:</strong> <code>${esc(r.method_used)}</code></div>`);
  const duplicates = r.duplicate_candidates || ex.duplicates || row.cross_batch_duplicates || [];
  if (duplicates.length) {
    const duplicateHtml = duplicates.slice(0, 3).map(d => {
      const evidence = d.evidence || {};
      const bits = [];
      if (evidence.invoice_number_match) bits.push("invoice number");
      if (evidence.invoice_date_match) bits.push("date");
      if (evidence.total_match) bits.push("total");
      if (evidence.currency_match) bits.push("currency");
      if (evidence.supplier_match) bits.push("supplier/VAT");
      const label = d.candidate_batch_name || d.candidate_batch_id || "previous batch";
      const status = (d.match_status || "").replaceAll("_", " ");
      const conf = d.confidence != null ? ` ${esc(pct(d.confidence))}` : "";
      return `<div class="muted">Cross-batch duplicate ${esc(status)}${conf}: duplicate of ${esc(label)} (${esc(d.candidate_batch_id || "")}) row ${esc(d.candidate_row_id || "")}${bits.length ? ` (${esc(bits.join(", "))})` : ""}</div>`;
    }).join("");
    parts.push(`<div><strong>Duplicate check:</strong>${duplicateHtml}</div>`);
  }
  if (reasons.length) parts.push(`<div class="review-explain-reasons"><strong>Why review:</strong> ${reasons.map(x => esc(compactReason(x))).join(" · ")}</div>`);
  if (tags.length) {
    const tagHtml = tags.slice(0, 12).map(t => `<span class="evidence-tag">${esc(t)}</span>`).join(" ");
    parts.push(`<div class="evidence-tags"><strong>Evidence tags:</strong> ${tagHtml}</div>`);
  }
  return `<div class="review-explain-card">${parts.join("")}</div>`;
}

function renderFieldEvidence(r, field) {
  const fieldMap = r.field_evidence || (r.explainability && r.explainability.fields) || {};
  const ev = fieldMap[field];
  if (!ev) return "";
  const source = sourceBadge(ev.selected_source, ev.selected_source_label);
  const reason = compactReason(ev.reason || (ev.review_reasons || []).join(" · "));
  const auditCandidates = Array.isArray(ev.candidates) ? ev.candidates : [];
  const persisted = (r.persisted_candidates && Array.isArray(r.persisted_candidates[field])) ? r.persisted_candidates[field] : [];
  const persistedHtml = persisted.length ? persisted.map(renderPersistedCandidate).join("") : "";
  const auditHtml = auditCandidates.length ? auditCandidates.slice(-4).map(c => {
    const conf = c.confidence != null ? ` <span class="muted">${esc(pct(c.confidence))}</span>` : "";
    const applied = c.applied ? " selected" : "";
    return `<div class="field-candidate${applied}">${sourceBadge(c.source, c.label)} <span>${esc(c.value ?? "—")}</span>${conf}<div class="muted">${esc(compactReason(c.reason, 120))}</div></div>`;
  }).join("") : "";
  const reviewReason = (ev.review_reasons || []).length ? `<div class="field-reason">⚠ ${ev.review_reasons.map(x => esc(compactReason(x))).join(" · ")}</div>` : "";
  const open = ev.review_required || persisted.some(c => c.conflict || c.selected || c.applied);
  return `<details class="field-evidence" ${open ? "open" : ""}>
    <summary>${source} ${ev.confidence != null ? `<span class="muted">${esc(pct(ev.confidence))}</span>` : ""} ${reason ? `<span class="muted">— ${esc(reason)}</span>` : ""}</summary>
    ${reviewReason}
    ${persistedHtml ? `<div class="candidate-section-title">Persisted arbitration candidates</div>${persistedHtml}` : ""}
    ${auditHtml ? `<div class="candidate-section-title">Audit evidence</div>${auditHtml}` : ""}
    ${(!persistedHtml && !auditHtml) ? '<div class="muted">No extra candidate evidence recorded for this field.</div>' : ""}
  </details>`;
}


function renderSelectedExplainPanel() {
  const panel = $("selectedExplainPanel");
  const body = $("selectedExplainBody");
  if (!panel || !body) return;
  const r = state.rows.find(x => x.id === state.selected);
  if (!r) {
    panel.hidden = true;
    body.innerHTML = "";
    return;
  }
  body.innerHTML = renderRowExplainability(r);
  panel.hidden = false;
}

async function load() {
  if (!batchId) { msg("Missing batch_id in URL", "error"); return; }
  // Clear any stale non-error banner when refreshing data
  const _pm = $("pageMessage");
  if (_pm && _pm.className && !_pm.className.includes("error") && !_pm.className.includes("warning")) {
    _pm.textContent = ""; _pm.className = "message";
  }
  try {
    const r = await fetch(`/review/batches/${batchId}`, { headers: hdrs() });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    state.batch = d.batch;
    state.rows = d.rows;
    state.rows.forEach(row => { if (state.candidatesByRow[row.id]) row.persisted_candidates = state.candidatesByRow[row.id]; });
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
      await loadCandidateEvidence(state.selected);
      await ensurePageCount();
      refreshPreview(); // load preview on initial selection
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
  const rowScroll = document.querySelector(".row-list-scroll");
  const priorRowScrollTop = rowScroll ? rowScroll.scrollTop : 0;
  list.innerHTML = "";
  // Keep the natural scan/export order stable. Selecting a row must not move it
  // to the top because reviewers lose their place and cannot see which row is next.
  const visibleRows = state.rows.filter(rowMatches);
  visibleRows.forEach(r => {
    // Determine urgency: review_required + not yet reviewed/corrected = urgent
    const isUrgent = r.review_required && !r.row_reviewed && !r.is_corrected;
    const isHighPriority = r.review_priority === "high" || r.review_priority === "urgent";

    const d = document.createElement("div");
    d.className = "review-row" +
      (r.review_required ? " flagged" : "") +
      (r.is_corrected    ? " corrected" : "") +
      (r.row_reviewed    ? " reviewed" : "") +
      (r.blocked_from_export ? " blocked" : "") +
      ((isUrgent || isHighPriority) ? " urgent" : "") +
      (r.id === state.selected ? " selected" : "");

    // Badge line
    const badges = [];
    if (r.blocked_from_export) badges.push(`<span class="row-badge row-badge-blocked">Blocked</span>`);
    if (isUrgent || isHighPriority) badges.push(`<span class="row-badge row-badge-urgent">Needs review</span>`);
    else if (r.review_required && r.row_reviewed) badges.push(`<span class="row-badge row-badge-reviewed">Reviewed</span>`);
    else if (r.is_corrected) badges.push(`<span class="row-badge row-badge-corrected">Corrected</span>`);

    const conf = r.confidence_score != null
      ? `<span class="row-conf${r.confidence_score < 0.55 ? " row-conf-low" : r.confidence_score < 0.75 ? " row-conf-mid" : ""}">${(r.confidence_score * 100).toFixed(0)}%</span>`
      : "";

    const toolBadge = (() => {
      const m = (r.method_used || "").toLowerCase();
      const directDi = /(^|[+|,])di($|[+|,])/.test(m);
      if (!m.startsWith("di_failed") && (directDi || m.includes("azure_di") || m.includes("_di"))) return '<span class="tool-badge tool-di">DI</span>';
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
      render();
      const editorBody = document.querySelector(".review-col-edit .review-col-body");
      if (editorBody) editorBody.scrollTop = 0;
      loadAudit(r.id); await loadCandidateEvidence(r.id); await ensurePageCount();
      refreshPreview(); // always load preview in 3-col layout
    };
    list.appendChild(d);
  });

  renderSelectedExplainPanel();
  if (rowScroll) rowScroll.scrollTop = priorRowScrollTop;

  document.querySelectorAll(".filter-chips .btn").forEach(b => {
    b.classList.toggle("active", b.dataset.filter === state.filter);
  });

  renderEditor();
  updateRemapUI();
}


function ensureExplainabilityStyles() {
  if (document.getElementById("reviewExplainabilityStyles")) return;
  const st = document.createElement("style");
  st.id = "reviewExplainabilityStyles";
  st.textContent = `
    .review-rows-body{display:flex;flex-direction:column;gap:8px;padding:8px;height:100%;min-height:0;overflow:hidden}
    .row-list-scroll{min-height:0;height:100%;overflow-y:auto;overflow-x:hidden;flex:1 1 auto;border-bottom:0;padding-bottom:0}
    .selected-explain-panel{flex:0 0 auto;max-height:140px;overflow:auto;border-top:1px solid var(--ap-border,#d7e0ea);padding-top:8px}
    .selected-explain-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:6px}
    .selected-explain-head h3{margin:0;font-size:13px;font-weight:700;color:var(--ap-text-muted,#536476)}
    .review-explain-card{border:1px solid var(--ap-border,#d7e0ea);background:var(--ap-surface,#fff);border-radius:10px;padding:10px;margin-bottom:0;font-size:12px;line-height:1.45;overflow-wrap:anywhere}
    .review-explain-reasons{color:var(--ap-warning-text,#7a4b00)}
    .review-row.blocked{opacity:.78}
    .row-badge-blocked{background:#fff1f1;color:#7a1f1f;border-color:#f0c2c2}
    .evidence-badge{display:inline-block;border:1px solid var(--ap-border,#d7e0ea);border-radius:999px;padding:1px 6px;font-size:11px;background:var(--ap-surface-muted,#f5f7fa);margin-right:4px}
    .evidence-tag{display:inline-block;border-radius:999px;padding:1px 6px;font-size:11px;background:var(--ap-surface-muted,#eef3f8);margin:2px 2px 0 0}
    .field-evidence{grid-column:1 / -1;margin:-4px 0 6px 0;padding:6px 8px;border-left:3px solid var(--ap-border,#d7e0ea);background:rgba(90,120,160,.06);border-radius:6px;font-size:12px}
    .field-evidence summary{cursor:pointer;list-style:none}
    .field-candidate{margin-top:6px;padding:5px 6px;border-radius:6px;background:rgba(255,255,255,.65);border:1px solid rgba(140,160,180,.25)}
    .field-candidate.selected,.field-candidate.applied{border-color:var(--ap-accent,#315a8c);background:rgba(49,90,140,.08)}
    .field-candidate.conflict{border-color:#b42318;background:rgba(180,35,24,.08)}
    .field-candidate.rejected{opacity:.82}
    .candidate-section-title{font-weight:700;margin-top:8px;margin-bottom:4px;color:var(--ap-muted,#536476)}
    .candidate-badge{display:inline-block;border-radius:999px;padding:1px 6px;font-size:10px;margin-left:4px;background:#eef3f8}
    .candidate-badge.selected,.candidate-badge.applied{background:#dbeafe}
    .candidate-badge.conflict{background:#fee2e2;color:#7f1d1d}
    .candidate-badge.rejected{background:#f3f4f6;color:#4b5563}
    .candidate-badge.suggested{background:#ecfdf5;color:#065f46}
    .audit-source{font-size:11px;margin-left:4px}
    code{white-space:normal}
  `;
  document.head.appendChild(st);
}

function renderEditor() {
  ensureExplainabilityStyles();
  const r = state.rows.find(x => x.id === state.selected);
  const ed = $("rowEditor");
  const deleteBtn = $("deleteRowBtn");
  if (!r) {
    if (deleteBtn) {
      deleteBtn.textContent = "Delete / Block Export";
      deleteBtn.title = "Block this row from export, or restore it if already blocked";
    }
    ed.innerHTML = '<div class="muted">Select a row from the left.</div>';
    return;
  }
  if (deleteBtn) {
    const blocked = !!r.blocked_from_export;
    deleteBtn.textContent = blocked ? "Restore Export" : "Block Export";
    deleteBtn.title = blocked ? "Restore this row to export eligibility" : "Block this row from export without deleting evidence";
  }

  // ── Header block: tool label + reasons (rendered OUTSIDE .field-grid) ────
  let header = '';
  const toolLabel = (() => {
    const m = (r.method_used || "").toLowerCase();
    const directDi = /(^|[+|,])di($|[+|,])/.test(m);
    if (!m.startsWith("di_failed") && (directDi || m.includes("azure_di") || m.includes("_di"))) return "Azure Document Intelligence (DI)";
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
  // Row-level confidence/review/evidence details are shown in the
  // Transaction details panel below the row list to preserve editor and row-list space.

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
    $("auditList").innerHTML = list.map(a => {
      const conf = a.confidence != null ? ` · ${esc(pct(a.confidence))}` : "";
      const note = a.explanation || a.note || "";
      return `<div class="audit-entry">
        <strong>${esc(a.field)}</strong> ${sourceBadge(a.source, a.source_label)} ${esc(a.action)}: ${esc(a.old) || "∅"} → ${esc(a.new) || "∅"}
        <span class="muted">(${esc(a.username) || "?"}${conf})</span>
        ${a.rule_created ? '<span class="badge rule">+rule</span>' : ""}
        ${a.force_added ? '<span class="badge force">+force</span>' : ""}
        ${note ? `<div class="muted">${esc(compactReason(note, 220))}</div>` : ""}
      </div>`;
    }).join("") || '<div class="muted">No history yet.</div>';
  } catch (e) { /* ignore */ }
}

async function loadCandidateEvidence(rowId) {
  if (!rowId || !batchId) return;
  try {
    const r = await fetch(`/review/batches/${batchId}/rows/${rowId}/candidates`, { headers: hdrs() });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const grouped = data.fields || {};
    state.candidatesByRow[rowId] = grouped;
    const row = state.rows.find(x => x.id === rowId);
    if (row) row.persisted_candidates = grouped;
    if (state.selected === rowId) renderEditor();
  } catch (e) {
    // Candidate evidence is supplementary. Keep the existing review workflow usable.
    console.warn("Candidate evidence unavailable", e);
  }
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

// ── Delete/block row from export ─────────────────────────────────────────────
$("deleteRowBtn").onclick = async () => {
  if (state.selected == null) { msg("Select a row first.", "error"); return; }
  const row = state.rows.find(x => x.id === state.selected);
  const label = row ? `${row.current?.supplier_name || "row"} ${row.current?.invoice_number || ""}`.trim() : "selected row";
  const isBlocked = !!row?.blocked_from_export;
  const actionText = isBlocked ? "Restore this row to export eligibility?" : "Block this row from export?";
  const detailText = isBlocked
    ? "The row and its evidence remain available, and it will be included in export again."
    : "The row and its evidence remain available, but it will not be exported.";
  if (!confirm(`${actionText}\n\n${label}\n\n${detailText}`)) return;
  try {
    const url = isBlocked
      ? `/review/batches/${batchId}/rows/${state.selected}/restore`
      : `/review/batches/${batchId}/rows/${state.selected}`;
    const r = await fetch(url, { method: isBlocked ? "POST" : "DELETE", headers: hdrs() });
    if (!r.ok) { msg(await r.text(), "error"); return; }
    msg(isBlocked ? "Row restored to export." : "Row blocked from export.", "success");
    await load();
  } catch (e) { msg(String(e), "error"); }
};

// ── Duplicate row for BCRS/deposit manual entry ──────────────────────────────
$("duplicateRowBtn").onclick = async () => {
  if (state.selected == null) { msg("Select a row first.", "error"); return; }
  if (!confirm("Create a duplicate of this row for manual BCRS/deposit entry?\n\nThe duplicate will have zero amounts — edit it to enter the correct deposit value.")) return;
  try {
    const r = await fetch(`/review/batches/${batchId}/rows/${state.selected}/duplicate`, {
      method: "POST", headers: hdrs(),
    });
    if (!r.ok) { msg(await r.text(), "error"); return; }
    const data = await r.json();
    msg(`Duplicate row ${data.duplicate_id} created. ${data.message}`, "success");
    await load();
    // Auto-select the new duplicate so reviewer can edit it immediately
    const dup = state.rows.find(x => x.id === data.duplicate_id);
    if (dup) {
      state.selected = dup.id;
      state.fileId   = dup.source_file_id;
      state.page     = dup.page_no || 1;
      render();
      loadAudit(dup.id);
    }
  } catch (e) { msg(String(e), "error"); }
};

// ── BCRS split ────────────────────────────────────────────────────────────────
// Shows an inline panel where the reviewer types the BCRS amount, then
// POSTs to /bcrs_split which creates the BCRS row and adjusts the source total.
const bcrsSplitPanel  = $("bcrsSplitPanel");
const bcrsSplitAmount = $("bcrsSplitAmount");
const bcrsSplitMsg    = $("bcrsSplitMsg");

function _hideBcrsSplitPanel() {
  bcrsSplitPanel.hidden = true;
  bcrsSplitAmount.value = "";
  bcrsSplitMsg.textContent = "";
}

$("bcrsSplitBtn").onclick = () => {
  if (state.selected == null) { msg("Select a row first.", "error"); return; }
  bcrsSplitPanel.hidden = !bcrsSplitPanel.hidden;
  if (!bcrsSplitPanel.hidden) {
    bcrsSplitMsg.textContent = "";
    bcrsSplitAmount.focus();
  }
};

$("bcrsSplitCancelBtn").onclick = _hideBcrsSplitPanel;

$("bcrsSplitConfirmBtn").onclick = async () => {
  const raw = parseFloat(bcrsSplitAmount.value);
  if (!raw || raw <= 0) {
    bcrsSplitMsg.textContent = "Enter a positive amount.";
    bcrsSplitMsg.style.color = "var(--ap-danger, #dc2626)";
    return;
  }
  bcrsSplitMsg.textContent = "Applying…";
  bcrsSplitMsg.style.color = "var(--ap-text-muted)";
  try {
    const r = await fetch(`/review/batches/${batchId}/rows/${state.selected}/bcrs_split`, {
      method: "POST",
      headers: { ...hdrs(), "Content-Type": "application/json" },
      body: JSON.stringify({ bcrs_amount: raw }),
    });
    // Parse body safely — server always returns JSON, but guard against edge cases
    let data;
    try { data = await r.json(); } catch { data = {}; }
    if (!r.ok) {
      const errMsg = (data && (data.detail || data.message)) || `Server error (${r.status})`;
      bcrsSplitMsg.textContent = errMsg;
      bcrsSplitMsg.style.color = "var(--ap-danger, #dc2626)";
      return;
    }
    _hideBcrsSplitPanel();
    msg(`BCRS split applied: BCRS row ${data.bcrs_row_id} created for ${raw.toFixed(2)}. Source row total adjusted to ${data.adjusted_total.toFixed(2)}.`, "success");
    await load();
    // Select the new BCRS row so reviewer can inspect it immediately
    const bcrsRow = state.rows.find(x => x.id === data.bcrs_row_id);
    if (bcrsRow) {
      state.selected = bcrsRow.id;
      state.fileId   = bcrsRow.source_file_id;
      state.page     = bcrsRow.page_no || 1;
      render();
      loadAudit(bcrsRow.id);
    }
  } catch (e) {
    bcrsSplitMsg.textContent = "Unexpected error — check the browser console.";
    bcrsSplitMsg.style.color = "var(--ap-danger, #dc2626)";
    console.error("BCRS split error:", e);
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

// Update the remap checkbox appearance based on current lock state.
// Called after every render() so the UI always reflects batch/row state.
function updateRemapUI() {
  const cb     = $("remapMode");
  const reason = remapLockReason();
  if (!cb) return;
  const lbl = cb.closest("label");
  if (reason) {
    cb.disabled = true;
    cb.title    = reason;
    cb.checked  = false;
    if (lbl) { lbl.style.opacity = "0.4"; lbl.style.cursor = "not-allowed"; }
    const prev = $("prevPageBtn"); const next = $("nextPageBtn");
    if (prev) { prev.disabled = true;  prev.title = reason; }
    if (next) { next.disabled = true;  next.title = reason; }
    remapHint.hidden = true;
  } else {
    cb.disabled = false;
    cb.title    = "Enable remap mode to re-draw field regions on the source PDF";
    if (lbl) { lbl.style.opacity = ""; lbl.style.cursor = ""; }
    const prev = $("prevPageBtn"); const next = $("nextPageBtn");
    if (prev) { prev.disabled = false; prev.title = ""; }
    if (next) { next.disabled = false; next.title = ""; }
  }
}

$("remapMode").addEventListener("change", async (e) => {
  const on = e.target.checked;
  if (on) {
    const reason = remapLockReason();
    if (reason) {
      e.target.checked = false;
      msg(reason, "error");
      updateRemapUI();
      return;
    }
  }
  remapHint.hidden = !on;
  if (!on) {
    remapSel.hidden = true; dragStart = null;
    previewWrap.classList.remove("remap-active");
    return;
  }
  // Show remap-active styling when remap mode activates
  previewWrap.classList.add("remap-active");
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
  if (!r.width || !r.height) return { xPx: 0, yPx: 0, w: 1, h: 1 };
  const x = Math.min(r.width  - 1, Math.max(0, e.clientX - r.left));
  const y = Math.min(r.height - 1, Math.max(0, e.clientY - r.top));
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
  // Only start a drag when the image is actually loaded and has dimensions
  const r = previewImg.getBoundingClientRect();
  if (!r.width || !r.height || !previewImg.complete || !previewImg.naturalWidth) {
    msg("Preview is still loading — please wait a moment then try again.", "error");
    return;
  }
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
    return; // accidental click / too-small drag
  }
  const lockMsg = remapLockReason();
  if (lockMsg) { remapSel.hidden = true; msg(lockMsg, "error"); return; }
  const row = state.rows.find(x => x.id === state.selected);
  if (!row) { msg("Select a row first.", "error"); return; }

  // ── Capture any text-layer selection the user may have made ───────────────
  // The preview is an <img> so native text selection is unavailable.
  // However if the user selected text in the page (e.g. a native PDF viewer
  // embedded elsewhere) we can pick it up from window.getSelection().
  // In normal preview-image mode this will usually be empty — the backend
  // will fall back to OCR/text-layer extraction from the bounding box.
  const domSel = window.getSelection();
  const selectedText = (domSel && domSel.toString()) ? domSel.toString().trim() : "";
  if (selectedText) {
    // Clear selection so it does not linger visually
    domSel.removeAllRanges();
  }

  // Show what we captured before asking user to confirm
  const previewLabel = selectedText
    ? `Selected text: "${selectedText.slice(0, 60)}${selectedText.length > 60 ? "…" : ""}"`
    : `Region: x=${region.x.toFixed(3)}, y=${region.y.toFixed(3)}, w=${region.wN.toFixed(3)}, h=${region.hN.toFixed(3)}`;
  if (!confirm(`Apply remap for field "${remapField}" on page ${state.page}?\n\n${previewLabel}`)) {
    remapSel.hidden = true;
    return;
  }

  msg(`Reading region for "${remapField}"…`, "");

  let rResp;
  const currentInput = document.querySelector(`#rowEditor [data-field="${remapField}"]`);
  const currentValue = currentInput ? currentInput.value : null;
  try {
    rResp = await fetch(`/review/batches/${batchId}/rows/${row.id}/remap`, {
      method: "POST", headers: hdrs(),
      body: JSON.stringify({
        field_name:    remapField,
        page_no:       state.page,
        x: region.x, y: region.y, w: region.wN, h: region.hN,
        file_id:       state.fileId,
        apply_as_value: true,
        selected_text: selectedText || null,
        current_value: currentValue || null,
      }),
    });
  } catch (fetchErr) {
    msg(`Network error during remap: ${fetchErr}`, "error");
    remapSel.hidden = true;
    return;
  }

  if (!rResp.ok) {
    const errText = await rResp.text().catch(() => String(rResp.status));
    msg(`Remap failed (${rResp.status}): ${errText}`, "error");
    remapSel.hidden = true;
    return;
  }

  const data = await rResp.json().catch(() => ({}));
  const fieldLabel = remapField;

  if (data && data.error) {
    // Backend returned an explicit failure.
    msg(`Remap: ${data.error}`, "error");
    remapSel.hidden = true;
    return;
  }
  if (data && data.warning) {
    // Empty OCR should not feel like a total failure when coordinates were saved.
    msg(`Remap: ${data.warning}`, "warning");
  }

  if (data && data.read_text) {
    // Update the editor field immediately so the user sees the value
    const inp = document.querySelector(`#rowEditor [data-field="${fieldLabel}"]`);
    if (inp) {
      inp.value = data.read_text;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      inp.focus();
    }
    await load();
    const ruleNote = data.rule_created
      ? " Future rule saved — this supplier's invoices will auto-fill this field."
      : (data.saved_as_hint ? " Region saved as future remap hint." : "");
    const fallbackNote = data.used_current_value_fallback ? " Used editor value as OCR fallback." : "";
    msg(`Remap applied — "${data.read_text}" → ${fieldLabel}.${ruleNote}${fallbackNote}`, "success");
  } else {
    // Hint saved but no text resolved — give informative message
    const hintNote = data && data.saved_as_hint
      ? " Coordinates stored as future remap hint for this supplier."
      : "";
    msg(`Remap region saved for "${fieldLabel}" — no text detected in selected area.${hintNote}`, "warning");
  }

  // Keep selection rectangle visible briefly then hide
  setTimeout(() => { remapSel.hidden = true; }, 1800);
});


// ── Saved remap region maintenance ─────────────────────────────────────────
function setSavedRegionsPanelOpen(open) {
  const panel = $("savedRegionsPanel");
  const btn = $("savedRegionsBtn");
  if (!panel) return;
  // Use class + explicit display, not only the HTML hidden attribute. Some
  // component styles set display rules on panels and can make hidden toggles
  // look like a no-op in deployed browsers.
  panel.hidden = !open;
  panel.classList.toggle("ap-hidden", !open);
  panel.style.display = open ? "flex" : "none";
  if (btn) {
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.textContent = open ? "Hide saved rules" : "Manage saved rules";
  }
}

async function loadSavedRegions() {
  const panel = $("savedRegionsPanel");
  const list = $("savedRegionsList");
  if (!panel || !list) return;
  list.innerHTML = `<div class="muted">Loading saved regions…</div>`;
  try {
    const r = await fetch(`/review/remap-hints?include_inactive=true`, { headers: hdrs() });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = `<div class="muted">No saved regions found for this tenant yet.</div>`;
      return;
    }
    const groupSummary = (data.groups || []).length
      ? `<div class="muted" style="margin-bottom:6px">${esc((data.groups || []).length)} supplier/field group(s). Primary regions are tried before fallbacks; archived/deleted regions are not used.</div>`
      : "";
    list.innerHTML = groupSummary + items.slice(0, 160).map(h => {
      const role = h.archived ? "archived" : (h.is_primary ? "primary" : (h.active ? "fallback" : "disabled"));
      return `
      <div class="row gap-sm" style="align-items:center;justify-content:space-between;border-bottom:1px solid var(--ap-border);padding:4px 0;opacity:${h.active && !h.archived ? '1' : '0.55'}">
        <span title="${esc(h.supplier_name_snapshot || '')}">
          <strong>${esc(h.field_name)}</strong>
          <span class="muted">ref p${esc(h.reference_page_no || h.page_no || 1)}</span>
          <span class="pill ${h.is_primary ? 'ok' : h.archived ? 'warning' : ''}">${esc(role)}</span>
          ${h.duplicate_count > 1 ? `<span class="pill warning">group ${h.duplicate_count}</span>` : ""}
          <br><span class="muted">${esc((h.supplier_name_snapshot || 'no supplier snapshot').slice(0, 52))}</span>
          <br><span class="muted">${esc(h.coordinates || '')}${h.source_row_id ? ` · row ${esc(h.source_row_id)}` : ''}${h.source_batch_id ? ` · batch ${esc(String(h.source_batch_id).slice(0, 8))}` : ''}</span>
          <br><span class="muted">used ${esc(h.apply_count || 0)} · ok ${esc(h.success_count || 0)} · fail ${esc(h.failure_count || 0)} · conflict ${esc(h.conflict_count || 0)}</span>
          ${h.last_used_at ? `<br><span class="muted">last used ${esc(h.last_used_at)}${h.last_result ? ` · ${esc(h.last_result)}` : ''}${h.last_used_page_no ? ` · p${esc(h.last_used_page_no)}` : ''}</span>` : ''}
        </span>
        <span class="row gap-sm">
          ${!h.archived && !h.is_primary ? `<button class="btn btn-sm" type="button" data-primary-region="${h.id}">Set primary</button>` : ""}
          ${h.archived
            ? `<button class="btn btn-sm" type="button" data-restore-region="${h.id}">Restore</button><button class="btn btn-sm" type="button" data-hard-delete-region="${h.id}" style="color:var(--ap-err-fg);border-color:var(--ap-err-fg)">Delete permanently</button>`
            : `${h.active
                ? `<button class="btn btn-sm" type="button" data-disable-region="${h.id}">Disable</button>`
                : `<button class="btn btn-sm" type="button" data-enable-region="${h.id}">Enable</button>`}
               <button class="btn btn-sm" type="button" data-archive-region="${h.id}" data-delete-region="${h.id}" title="Archive saved region">Archive</button>`}
        </span>
      </div>`}).join("");
  } catch (e) {
    list.innerHTML = `<div class="message error">Could not load saved regions: ${esc(e.message)}</div>`;
    msg(`Could not load saved regions: ${e.message}`, "error");
  }
}

function loadSavedRegions() {
  return loadSavedRules();
}

function savedRuleTypeLabel(type) {
  const labels = {
    supplier_alias: "Supplier alias",
    nominal_remap: "Nominal remap",
    remap_field_value: "Field remap",
    text_correction: "Text correction",
    saved_region: "Saved region",
  };
  return labels[type] || String(type || "Rule").replace(/_/g, " ");
}

function savedRuleScopeLabel(item) {
  if (item.is_global) return "Global";
  return item.applies_to === "this_company" ? "This company" : "All companies";
}

function renderSavedRegionRule(h) {
  const hintId = h.hint_id || String(h.id || "").replace(/^hint-/, "");
  const role = h.archived ? "archived" : (h.is_primary ? "primary" : (h.active ? "fallback" : "disabled"));
  return `
    <div class="saved-rule-card" data-saved-region-id="${esc(hintId)}" style="border-bottom:1px solid var(--ap-border);padding:6px 0;opacity:${h.active && !h.archived ? "1" : "0.55"}">
      <div class="row gap-sm" style="align-items:center;justify-content:space-between">
        <strong>${esc(h.field_name)}</strong>
        <span class="row gap-sm">
          <span class="pill ${h.is_primary ? "ok" : h.archived ? "warning" : ""}">${esc(role)}</span>
          <span class="pill">${esc(savedRuleScopeLabel(h))}</span>
        </span>
      </div>
      <div class="muted">${esc(h.source_pattern || "supplier/layout saved region")}</div>
      <div class="muted">${esc(h.target_value || "")}</div>
      <div class="row gap-sm" style="margin-top:5px;flex-wrap:wrap">
        ${!h.archived && !h.is_primary ? `<button class="btn btn-sm" type="button" data-primary-region="${esc(hintId)}">Set primary</button>` : ""}
        ${h.archived
          ? `<button class="btn btn-sm" type="button" data-restore-region="${esc(hintId)}">Restore</button><button class="btn btn-sm" type="button" data-hard-delete-region="${esc(hintId)}" style="color:var(--ap-err-fg);border-color:var(--ap-err-fg)">Delete permanently</button>`
          : `${h.active
              ? `<button class="btn btn-sm" type="button" data-disable-region="${esc(hintId)}">Disable</button>`
              : `<button class="btn btn-sm" type="button" data-enable-region="${esc(hintId)}">Enable</button>`}
             <button class="btn btn-sm" type="button" data-archive-region="${esc(hintId)}" data-delete-region="${esc(hintId)}" title="Archive saved region">Archive</button>`}
      </div>
    </div>`;
}

function renderEditableSavedRule(rule) {
  const disabled = rule.is_global ? "disabled" : "";
  return `
    <div class="saved-rule-card" data-rule-id="${esc(rule.id)}" style="border-bottom:1px solid var(--ap-border);padding:6px 0">
      <div class="row gap-sm" style="align-items:center;justify-content:space-between">
        <strong>${esc(savedRuleTypeLabel(rule.rule_type))}: ${esc(rule.field_name || "")}</strong>
        <span class="pill">${esc(savedRuleScopeLabel(rule))}</span>
      </div>
      <label style="display:block;margin-top:5px;font-size:12px">
        Match
        <input class="ap-input saved-rule-source" type="text" value="${esc(rule.source_pattern || "")}" ${disabled} />
      </label>
      <label style="display:block;margin-top:5px;font-size:12px">
        Value
        <input class="ap-input saved-rule-target" type="text" value="${esc(rule.target_value || "")}" ${disabled} />
      </label>
      <label class="row gap-sm" style="align-items:center;margin-top:5px;font-size:12px">
        <input class="saved-rule-active" type="checkbox" ${rule.active ? "checked" : ""} ${disabled} /> Active
      </label>
      <div class="row gap-sm" style="margin-top:6px;flex-wrap:wrap">
        <button class="btn btn-sm" type="button" data-rule-save="${esc(rule.id)}" ${disabled}>Save</button>
        ${rule.company_id && !rule.is_global ? `<button class="btn btn-sm" type="button" data-rule-wide="${esc(rule.id)}">Make tenant-wide</button>` : ""}
        <button class="btn btn-sm" type="button" data-rule-delete="${esc(rule.id)}" ${disabled} style="color:var(--ap-err-fg);border-color:var(--ap-err-fg)">Delete</button>
      </div>
      ${rule.origin_batch_id ? `<div class="muted" style="margin-top:4px">Origin batch ${esc(String(rule.origin_batch_id).slice(0, 8))}${rule.origin_row_id ? ` &middot; row ${esc(rule.origin_row_id)}` : ""}</div>` : ""}
    </div>`;
}

async function loadSavedRules() {
  const panel = $("savedRegionsPanel");
  const list = $("savedRegionsList");
  if (!panel || !list) return;
  list.innerHTML = `<div class="muted">Loading saved rules...</div>`;
  try {
    const r = await fetch(`/review/rules?include_saved_regions=true&active_only=false`, { headers: hdrs() });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const items = Array.isArray(data) ? data : (data.items || []);
    if (!items.length) {
      list.innerHTML = `<div class="muted">No saved rules found for this tenant yet.</div>`;
      return;
    }
    const rules = items.filter(x => x.item_type !== "saved_region");
    const regions = items.filter(x => x.item_type === "saved_region");
    const summary = `<div class="muted" style="margin-bottom:6px">${esc(rules.length)} field rule(s), ${esc(regions.length)} saved region(s). Tenant-wide rules apply to all companies in the tenant.</div>`;
    list.innerHTML = summary + items.slice(0, 200).map(item =>
      item.item_type === "saved_region" ? renderSavedRegionRule(item) : renderEditableSavedRule(item)
    ).join("");
  } catch (e) {
    list.innerHTML = `<div class="message error">Could not load saved rules: ${esc(e.message)}</div>`;
    msg(`Could not load saved rules: ${e.message}`, "error");
  }
}

async function handleSavedRuleEditClick(btn, action) {
  const id = btn.getAttribute(`data-rule-${action}`);
  const card = btn.closest("[data-rule-id]");
  if (!id || !card) return;
  try {
    if (action === "delete") {
      if (!confirm(`Delete saved rule ${id}?`)) return;
      const r = await fetch(`/review/rules/${id}`, { method: "DELETE", headers: hdrs() });
      if (!r.ok) throw new Error(await r.text());
      msg("Saved rule deleted.", "success");
    } else if (action === "wide") {
      const r = await fetch(`/review/rules/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...hdrs() },
        body: JSON.stringify({ applies_to: "all_companies" }),
      });
      if (!r.ok) throw new Error(await r.text());
      msg("Saved rule is now available to all companies in this tenant.", "success");
    } else {
      const source = card.querySelector(".saved-rule-source");
      const target = card.querySelector(".saved-rule-target");
      const active = card.querySelector(".saved-rule-active");
      const r = await fetch(`/review/rules/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...hdrs() },
        body: JSON.stringify({
          source_pattern: source ? source.value : undefined,
          target_value: target ? target.value : undefined,
          active: active ? active.checked : undefined,
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      msg("Saved rule updated.", "success");
    }
    await loadSavedRules();
  } catch (e) {
    msg(`Saved rule ${action} failed: ${e && e.message ? e.message : e}`, "error");
  }
}


async function handleApplySavedRegionsClick(evt) {
  if (evt) { evt.preventDefault(); evt.stopPropagation(); }
  const btn = $("applySavedRegionsBtn");
  if (btn && btn.dataset.busy === "1") return;
  const oldText = btn ? btn.textContent : "";
  if (btn) { btn.dataset.busy = "1"; btn.disabled = true; btn.textContent = "Applying…"; }
  setApplySavedRegionsStatus("Checking selected row…", "");
  try {
    let row = state.rows.find(x => x.id === state.selected);
    // A common reviewer flow is to open Review and press this button before
    // explicitly selecting a row.  Make the action useful by selecting the
    // first visible row instead of silently doing nothing.
    if (!row && state.rows.length) {
      row = state.rows[0];
      state.selected = row.id;
      state.fileId = row.source_file_id;
      state.page = row.page_no || 1;
      render();
      loadAudit(row.id);
    }
    if (!row) { msg("No extracted row is available for saved-rule replay.", "error"); setApplySavedRegionsStatus("No row selected", "error"); return; }
    if (!batchId) { msg("Missing batch_id in review URL; cannot apply saved rules.", "error"); setApplySavedRegionsStatus("Missing batch", "error"); return; }

    msg("Applying saved rules to selected row...", "");
    const r = await fetch(`/review/batches/${batchId}/rows/${row.id}/apply-saved-regions`, {
      method: "POST",
      headers: hdrs(),
    });
    if (!r.ok) { msg(`Apply saved rules failed: ${await r.text()}`, "error"); return; }
    const data = await r.json().catch(() => ({}));
    const fields = data.changed_fields || [];
    const conflicts = data.conflict_fields || [];
    const checked = data.checked_regions || 0;
    const diag = data.diagnostics || {};
    const checkedFields = Array.isArray(diag.fields_checked) ? diag.fields_checked.join(", ") : "";
    const skipped = Array.isArray(diag.skipped_reasons) ? diag.skipped_reasons.join("; ") : "";
    if (fields.length) {
      const text = `Saved rules applied: ${fields.join(", ")}. Please verify before approval.`;
      msg(text, "success");
      setApplySavedRegionsStatus(`Changed: ${fields.join(", ")}${checkedFields ? ` | Checked: ${checkedFields}` : ""}`, "success");
      const keepSelected = row.id;
      await load();
      state.selected = keepSelected;
      const updated = state.rows.find(x => x.id === keepSelected);
      if (updated) { state.fileId = updated.source_file_id; state.page = updated.page_no || 1; render(); await loadAudit(keepSelected); await ensurePageCount(); refreshPreview(); }
    } else if (conflicts.length) {
      const text = `Saved rules checked ${checked ? `(${checked}) ` : ""}but conflicted with existing values: ${conflicts.join(", ")}. Field left unchanged.`;
      msg(text, "warning");
      setApplySavedRegionsStatus(`Conflict: ${conflicts.join(", ")}${checkedFields ? ` | Checked: ${checkedFields}` : ""}`, "warning");
      await loadAudit(row.id);
      renderSelectedExplainPanel();
    } else {
      const text = `Saved rules checked${checked ? ` (${checked})` : ""} - no field changed on the selected row.`;
      msg(text, "warning");
      setApplySavedRegionsStatus(`Checked; no change${skipped ? ` | ${skipped}` : ""}`, "warning");
      await loadAudit(row.id);
      renderSelectedExplainPanel();
    }
  } catch (e) {
    msg(`Apply saved rules failed: ${e && e.message ? e.message : e}`, "error");
    setApplySavedRegionsStatus("Apply failed", "error");
  } finally {
    if (btn) { btn.dataset.busy = "0"; btn.disabled = false; btn.textContent = oldText || "Apply saved rules to row"; }
  }
}

async function handleSavedRegionsToggleClick(evt) {
  if (evt) { evt.preventDefault(); evt.stopPropagation(); }
  const panel = $("savedRegionsPanel");
  if (!panel) { msg("Saved rules panel is not available on this page.", "error"); return; }
  const isClosed = panel.hidden || panel.classList.contains("ap-hidden") || getComputedStyle(panel).display === "none";
  setSavedRegionsPanelOpen(isClosed);
  if (isClosed) await loadSavedRules();
}

const applySavedRegionsBtn = $("applySavedRegionsBtn");
if (applySavedRegionsBtn) applySavedRegionsBtn.addEventListener("click", handleApplySavedRegionsClick);

const savedRegionsBtn = $("savedRegionsBtn");
if (savedRegionsBtn) savedRegionsBtn.addEventListener("click", handleSavedRegionsToggleClick);

const savedRulesCloseBtn = $("savedRulesCloseBtn");
if (savedRulesCloseBtn) savedRulesCloseBtn.addEventListener("click", (e) => {
  e.preventDefault();
  setSavedRegionsPanelOpen(false);
});

// Extra delegated fallback: if a later render or browser extension replaces the
// buttons, the actions still trigger.  This also makes failures visible instead
// of looking like a dead button.
document.addEventListener("click", async (e) => {
  const savedBtn = e.target.closest && e.target.closest("#savedRegionsBtn");
  const applyBtn = e.target.closest && e.target.closest("#applySavedRegionsBtn");
  if (savedBtn) await handleSavedRegionsToggleClick(e);
  if (applyBtn) await handleApplySavedRegionsClick(e);
}, true);

document.addEventListener("click", async (e) => {
  const saveBtn = e.target.closest && e.target.closest("[data-rule-save]");
  const deleteBtn = e.target.closest && e.target.closest("[data-rule-delete]");
  const wideBtn = e.target.closest && e.target.closest("[data-rule-wide]");
  const picked = saveBtn || deleteBtn || wideBtn;
  if (!picked) return;
  e.preventDefault();
  e.stopPropagation();
  if (saveBtn) await handleSavedRuleEditClick(saveBtn, "save");
  if (deleteBtn) await handleSavedRuleEditClick(deleteBtn, "delete");
  if (wideBtn) await handleSavedRuleEditClick(wideBtn, "wide");
});

document.addEventListener("click", async (e) => {
  const targets = [
    ["disable", e.target.closest("[data-disable-region]")],
    ["enable", e.target.closest("[data-enable-region]")],
    ["archive", e.target.closest("[data-archive-region]")],
    ["restore", e.target.closest("[data-restore-region]")],
    ["primary", e.target.closest("[data-primary-region]")],
    ["hard-delete", e.target.closest("[data-hard-delete-region]")],
  ];
  const picked = targets.find(x => x[1]);
  if (!picked) return;
  const action = picked[0];
  const btn = picked[1];
  const id = btn.getAttribute(`data-${action}-region`);
  const label = action === "hard-delete" ? "permanently delete" : action;
  if (!confirm(`${label} saved region ${id}?`)) return;
  const url = action === "hard-delete"
    ? `/review/remap-hints/${id}?hard_delete=true`
    : `/review/remap-hints/${id}/${action === "primary" ? "primary" : action}`;
  const method = action === "hard-delete" ? "DELETE" : "POST";
  const r = await fetch(url, { method, headers: hdrs() });
  if (!r.ok) { msg(`Saved region ${label} failed: ${await r.text()}`, "error"); return; }
  msg(`Saved region ${label} complete.`, "success");
  await loadSavedRules();
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

// Remap mode is intentionally NOT auto-enabled; reviewers opt in per action.
const remapDefault = $("remapMode"); if (remapDefault) remapDefault.checked = false;
