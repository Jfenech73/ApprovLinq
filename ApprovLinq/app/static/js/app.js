const state = {
  batches: [],
  selectedBatchId: null,
  progressTimer: null,
  companies: [],
  tenants: [],
};

function $(id) {
  return document.getElementById(id);
}

function truncate(value, maxLength = 80) {
  const text = String(value ?? "");
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : "-";
}

function setInlineMessage(element, text, kind = "") {
  const clean = normalizeUiErrorMessage(text);
  element.textContent = clean || "";
  element.className = `message ${kind}`.trim();
}

function confidenceDisplay(value) {
  return value == null ? "-" : `${(Number(value) * 100).toFixed(0)}%`;
}

function reviewBadge(row) {
  return row.review_required ? "Review" : "OK";
}

function batchScanComplete(batch) {
  const status = String(batch && batch.status || "").toLowerCase();
  if (["processed", "partial", "in_review", "approved", "exported"].includes(status)) return true;
  return Boolean(batch && batch.processed_at);
}

function batchReviewLocked(batch) {
  const status = String(batch && batch.status || "").toLowerCase();
  return ["approved", "exported"].includes(status);
}

function batchActionDisabledAttr(batch, action) {
  if (!batchScanComplete(batch)) return "disabled";
  if (action === "review" && batchReviewLocked(batch)) return "disabled";
  return "";
}

function batchActionTitle(batch, action) {
  if (!batchScanComplete(batch)) return "Batch actions are enabled once scanning is complete.";
  if (action === "review" && batchReviewLocked(batch)) return "Batch is already marked reviewed.";
  if (action === "view") return "Preview this batch using the assigned export template.";
  if (action === "review") return "Open the review workspace for this batch.";
  return "Export this batch to Excel.";
}

function hideProgress() {}

function stopProgressPolling() {
  if (state.progressTimer) {
    clearInterval(state.progressTimer);
    state.progressTimer = null;
  }
}

async function api(path, options = {}) {
  return apiFetch(path, options);
}
// Expose for ap-ui.js populateUserBlock (scanner page loads app.js before ap-ui.js)
window.api = api;

function setWorkspaceLink(role) {
  const platformAdminLink = $("platformAdminLink");
  const tenantAdminLink = $("tenantAdminLink");

  if (tenantAdminLink) {
    tenantAdminLink.href = "/static/tenant.html";
    tenantAdminLink.textContent = "Tenant Admin";
  }

  if (!platformAdminLink) return;

  if (String(role || "").toLowerCase() === "admin") {
    platformAdminLink.classList.remove("hidden");
  } else {
    platformAdminLink.classList.add("hidden");
  }
}

async function loadTenantOptions() {
  state.tenants = await getAvailableTenants();
  const selector = $("tenantSelector");
  selector.innerHTML = "";

  if (!state.tenants.length) {
    selector.innerHTML = '<option value="">No tenants available</option>';
    $("companySelector").innerHTML = '<option value="">No companies available</option>';
    setInlineMessage($("createBatchMessage"), "No tenants are available for this user.");
    return;
  }

  const currentTenantId = getTenantId();
  const selectedTenant = state.tenants.find((tenant) => tenant.tenant_id === currentTenantId) || state.tenants.find((tenant) => tenant.is_default) || state.tenants[0];
  if (selectedTenant) setTenantId(selectedTenant.tenant_id);

  for (const tenant of state.tenants) {
    const option = document.createElement("option");
    option.value = tenant.tenant_id;
    option.textContent = `${tenant.tenant_name} (${tenant.tenant_code})`;
    if (selectedTenant && tenant.tenant_id === selectedTenant.tenant_id) option.selected = true;
    selector.appendChild(option);
  }
}

async function loadCompanies() {
  state.companies = await api("/tenant/companies");
  const select = $("companySelector");
  if (!state.companies.length) {
    select.innerHTML = '<option value="">No companies available</option>';
    return;
  }

  select.innerHTML = state.companies
    .map((company) => `<option value="${company.id}">${escapeHtml(company.company_name)} (${escapeHtml(company.company_code)})</option>`)
    .join("");
}

async function loadBatches() {
  const companyId = $("companySelector")?.value;
  const path = companyId ? `/batches?company_id=${encodeURIComponent(companyId)}` : "/batches";
  state.batches = await api(path);
  const tbody = $("batchesTableBody");
  tbody.innerHTML = "";

  if (!state.batches.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">No batches found.</td></tr>';
    return;
  }

  for (const batch of state.batches) {
    const tr = document.createElement("tr");
    const actionCell = `
      <div class="row gap-sm" style="justify-content:flex-end;flex-wrap:wrap">
        <button class="btn btn-sm" type="button" data-batch-action="view" data-batch-id="${escapeHtml(batch.id)}" ${batchActionDisabledAttr(batch, "view")} title="${escapeHtml(batchActionTitle(batch, "view"))}">View</button>
        <button class="btn btn-sm" type="button" data-batch-action="review" data-batch-id="${escapeHtml(batch.id)}" ${batchActionDisabledAttr(batch, "review")} title="${escapeHtml(batchActionTitle(batch, "review"))}">Review</button>
        <button class="btn btn-sm btn-primary" type="button" data-batch-action="export" data-batch-id="${escapeHtml(batch.id)}" ${batchActionDisabledAttr(batch, "export")} title="${escapeHtml(batchActionTitle(batch, "export"))}">Export</button>
      </div>`;
    tr.innerHTML = `
      <td><strong>${escapeHtml(batch.batch_name)}</strong><br /><span class="muted">${escapeHtml(batch.id)}</span></td>
      <td><span class="pill">${escapeHtml(batch.status || "-")}</span></td>
      <td>${batch.page_count ?? "-"}</td>
      <td>${formatDate(batch.created_at)}</td>
      <td>${formatDate(batch.processed_at)}</td>
      <td>${actionCell}</td>
    `;
    tr.addEventListener("click", (event) => {
      if (event.target.closest("[data-batch-action]")) return;
      selectBatch(batch.id);
    });
    tr.querySelectorAll("[data-batch-action]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (button.disabled) return;
        await handleBatchRowAction(button.dataset.batchAction, batch.id);
      });
    });
    tbody.appendChild(tr);
  }
}

async function selectBatch(batchId, options = {}) {
  state.selectedBatchId = batchId;
  const batch = await api(`/batches/${batchId}`);

  // Clear any stale action message when selecting a different batch
  if (!options.preservePolling) setInlineMessage($("actionMessage"), "");

  $("selectedBatchEmpty").classList.add("hidden");
  $("selectedBatchPanel").classList.remove("hidden");
  $("selectedBatchId").textContent = batch.id;
  $("selectedBatchName").textContent = batch.batch_name;
  $("selectedBatchStatus").textContent = batch.status;
  $("selectedBatchNotes").textContent = batch.notes || "-";

  // Sync the scan mode radio buttons to the batch's stored mode
  const currentMode = batch.scan_mode || "summary";
  document.querySelectorAll('input[name="batchScanMode"]').forEach((radio) => {
    radio.checked = radio.value === currentMode;
    radio.disabled = batch.status === "processing";
  });
  setInlineMessage($("scanModeMessage"), "");

  renderFiles(batch.files || []);

  // One-shot paint of review cells for batches that aren't actively polling
  // (already completed batches). Safe even when mid-scan — the poller will
  // keep updating on its own.
  try {
    const progress = await api(`/batches/${batchId}/progress`);
    applyReviewStates(progress.files || []);
  } catch {}

  if (batch.status === "processing") {
    startProgressPolling();
  } else if (!options.preservePolling) {
    stopProgressPolling();
    hideProgress();
  }
}

function renderFiles(files) {
  const tbody = $("filesTableBody");
  tbody.innerHTML = "";

  if (!files.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">No files uploaded yet.</td></tr>';
    return;
  }

  for (const file of files) {
    const tr = document.createElement("tr");
    tr.setAttribute("data-filename", file.original_filename);
    const errorText = file.error_message ? truncate(file.error_message, 160) : "-";
    tr.innerHTML = `
      <td>${escapeHtml(file.original_filename)}</td>
      <td><span class="pill">${escapeHtml(file.status)}</span></td>
      <td>${file.page_count ?? "-"}</td>
      <td title="${escapeHtml(file.error_message || "")}">${escapeHtml(errorText)}</td>
      <td>${formatDate(file.uploaded_at)}</td>
      <td class="review-cell">-</td>
    `;
    tbody.appendChild(tr);
  }
  // Review cells start as "-" and are populated by startProgressPolling()'s
  // applyReviewStates call, or by selectBatch() which also triggers a poll.
}

async function loadRows() {
  const tbody = $("rowsTableBody");
  if (!tbody) return;
  tbody.innerHTML = "";

  if (!state.selectedBatchId) {
    tbody.innerHTML = '<tr><td colspan="9" class="muted">Select a batch first.</td></tr>';
    return;
  }

  const rows = await api(`/batches/${state.selectedBatchId}/rows`);
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="muted">No extracted rows yet.</td></tr>';
    return;
  }

  for (const row of rows) {
    const description = truncate(row.description || "-", 80);
    const supplier = truncate(row.supplier_name || "-", 60);
    const invoiceNo = truncate(row.invoice_number || "-", 40);
    const tr = document.createElement("tr");
    const toolBadge = (() => {
      const m = (row.method_used || "").toLowerCase();
      const directDi = /(^|[+|,])di($|[+|,])/.test(m);
      if (!m.startsWith("di_failed") && (directDi || m.includes("azure_di") || m.includes("_di"))) return "DI";
      if (m.includes("openai") || m.includes("vision") || m.includes("_ai")) return "AI";
      if (m.includes("ocr")) return "OCR";
      if (m) return "TXT";
      return "-";
    })();
    tr.innerHTML = `
      <td>${escapeHtml(row.source_filename || "-")}</td>
      <td>${row.page_no ?? "-"}</td>
      <td title="${escapeHtml(row.supplier_name || "")}">${escapeHtml(supplier)}</td>
      <td title="${escapeHtml(row.invoice_number || "")}">${escapeHtml(invoiceNo)}</td>
      <td>${escapeHtml(row.invoice_date || "-")}</td>
      <td title="${escapeHtml(row.description || "")}">${escapeHtml(description)}</td>
      <td>${row.total_amount ?? "-"}</td>
      <td>${confidenceDisplay(row.confidence_score)}</td>
      <td>${escapeHtml(toolBadge)}</td>
      <td title="Posting: ${escapeHtml(row.supplier_posting_account || "-")} | Nominal: ${escapeHtml(row.nominal_account_code || "-")}">${reviewBadge(row)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function startProgressPolling() {
  stopProgressPolling();
  state.progressTimer = setInterval(async () => {
    if (!state.selectedBatchId) return;
    const progress = await api(`/batches/${state.selectedBatchId}/progress`);
    $("selectedBatchStatus").textContent = progress.status;
    $("selectedBatchNotes").textContent = `${progress.notes || ""} (${progress.percent}%)`;
    // Review-as-you-go: update per-file review badges and fire toast on any
    // new file transitioning to "needs_review".
    applyReviewStates(progress.files || []);
    if (progress.status !== "processing") {
      stopProgressPolling();
      // Clear stale "processing started" banner now that processing is done
      setInlineMessage($("actionMessage"), "");
      await selectBatch(state.selectedBatchId, { preservePolling: true });
      await loadBatches();
      // After processing ends, re-fetch once more so the UI reflects final
      // per-file review states even if the user hasn't clicked anything.
      try {
        const final = await api(`/batches/${state.selectedBatchId}/progress`);
        applyReviewStates(final.files || []);
      } catch {}
    }
  }, 3000);
}

// Track which files have already been announced via toast so each transition
// to needs_review fires exactly once, no matter how many poll cycles run.
const _announcedReviewFiles = new Set();

function applyReviewStates(fileStates) {
  const tbody = $("filesTableBody");
  if (!tbody) return;
  const byFilename = new Map();
  for (const fs of fileStates) byFilename.set(fs.filename, fs);
  const rows = tbody.querySelectorAll("tr[data-filename]");
  rows.forEach((tr) => {
    const fn = tr.getAttribute("data-filename");
    const fs = byFilename.get(fn);
    if (!fs) return;
    const cell = tr.querySelector(".review-cell");
    if (!cell) return;
    const reviewReady = isFileReviewReady(fs);
    cell.innerHTML = renderReviewCell(fs, state.selectedBatchId);
    tr.classList.toggle("needs-review", reviewReady && fs.review_state === "needs_review");
    // Row flash on needs_review transition (no popup toast)
    const key = `${state.selectedBatchId}|${fs.file_id}`;
    if (reviewReady && fs.review_state === "needs_review" && !_announcedReviewFiles.has(key)) {
      _announcedReviewFiles.add(key);
      tr.classList.add("row-flash");
      setTimeout(() => tr.classList.remove("row-flash"), 2500);
    }
  });
}

function isFileReviewReady(fs) {
  const status = String(fs && (fs.status || fs.file_status || "") || "").toLowerCase();
  if (["processed", "partial", "failed", "completed", "complete", "done", "error"].includes(status)) return true;
  if (fs && (fs.finished_at || fs.processed_at || fs.completed_at)) return true;
  return false;
}

function renderReviewCell(fs, batchId) {
  if (!isFileReviewReady(fs)) return "-";
  if (fs.review_state === "needs_review") {
    const url = reviewUrl(batchId, fs.file_id);
    const fields = fs.flagged_fields && fs.flagged_fields.length
      ? ` (${fs.flagged_fields.slice(0, 3).join(", ")}${fs.flagged_fields.length > 3 ? "…" : ""})` : "";
    return `<a class="btn btn-primary btn-sm" href="${url}" target="_blank" rel="noopener">Review now</a>` +
           `<div class="muted" style="font-size:11px;margin-top:2px">${fs.outstanding_row_count} row(s) low conf${escapeHtml(fields)}</div>`;
  }
  if (fs.review_state === "reviewed") return '<span class="pill pill-ok">reviewed</span>';
  if (fs.review_state === "clean") return '<span class="pill pill-ok">ok</span>';
  return "-";
}

function reviewUrl(batchId, fileId) {
  return `/static/review.html?batch_id=${encodeURIComponent(batchId)}&file=${encodeURIComponent(fileId)}`;
}

function batchReviewUrl(batchId) {
  return `/static/review.html?batch_id=${encodeURIComponent(batchId)}`;
}

function popupDocumentShell(title, body) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${escapeHtml(title)}</title>
  <style>
    body{font-family:Arial,sans-serif;margin:0;background:#f5f7fb;color:#0f172a}
    header{position:sticky;top:0;background:#fff;border-bottom:1px solid #d7e0ea;padding:14px 18px;z-index:2}
    h1{font-size:18px;margin:0 0 4px}
    .muted{color:#64748b;font-size:13px}
    main{padding:16px 18px}
    .table-wrap{overflow:auto;border:1px solid #d7e0ea;border-radius:8px;background:#fff}
    table{border-collapse:collapse;width:100%;font-size:13px}
    th,td{border-bottom:1px solid #e5edf5;padding:8px 10px;text-align:left;vertical-align:top;white-space:nowrap}
    th{position:sticky;top:0;background:#eaf3fb;color:#415a77;text-transform:uppercase;font-size:12px;letter-spacing:.04em}
    tr:last-child td{border-bottom:0}
    .empty{padding:18px;color:#64748b}
  </style>
</head>
<body>${body}</body>
</html>`;
}

function renderPreviewTable(preview) {
  const columns = preview.columns && preview.columns.length
    ? preview.columns
    : Object.keys((preview.rows || [])[0] || {});
  const rows = preview.rows || [];
  const header = `<header>
    <h1>${escapeHtml(preview.batch_name || "Batch preview")}</h1>
    <div class="muted">Template: ${escapeHtml(preview.template_name || "Default export")} &middot; Sheet: ${escapeHtml(preview.sheet_name || "Invoices")} &middot; ${escapeHtml(preview.row_count || rows.length)} row(s)</div>
  </header>`;
  if (!rows.length || !columns.length) {
    return popupDocumentShell("Batch preview", `${header}<main><div class="table-wrap"><div class="empty">No rows are available to preview.</div></div></main>`);
  }
  const thead = `<thead><tr>${columns.map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>`;
  const tbody = `<tbody>${rows.map(row =>
    `<tr>${columns.map(c => `<td>${escapeHtml(row[c] ?? "")}</td>`).join("")}</tr>`
  ).join("")}</tbody>`;
  return popupDocumentShell("Batch preview", `${header}<main><div class="table-wrap"><table>${thead}${tbody}</table></div></main>`);
}

async function viewBatchPreview(batchId) {
  const popup = window.open("", `approvlinq_batch_preview_${batchId}`, "width=1180,height=760,resizable=yes,scrollbars=yes");
  if (!popup) {
    setInlineMessage($("actionMessage"), "Popup blocked. Allow popups for this site to use View.", "server-error");
    return;
  }
  popup.document.open();
  popup.document.write(popupDocumentShell("Batch preview", "<header><h1>Loading batch preview...</h1><div class=\"muted\">Rendering assigned template.</div></header>"));
  popup.document.close();
  try {
    const preview = await api(`/batches/${batchId}/preview`);
    popup.document.open();
    popup.document.write(renderPreviewTable(preview));
    popup.document.close();
    popup.focus();
  } catch (error) {
    popup.document.open();
    popup.document.write(popupDocumentShell("Batch preview", `<header><h1>Preview unavailable</h1><div class="muted">${escapeHtml(normalizeUiErrorMessage(error.message))}</div></header>`));
    popup.document.close();
  }
}

async function exportBatch(batchId) {
  const message = $("actionMessage");
  setInlineMessage(message, "Preparing export...");
  try {
    const headers = typeof authHeaders === "function" ? authHeaders() : {};
    const response = await fetch(`/batches/${batchId}/export`, { headers });
    if (!response.ok) {
      const text = await response.text();
      setInlineMessage(message, normalizeUiErrorMessage(text), "server-error");
      return;
    }
    const blob = await response.blob();
    const cd = response.headers.get("Content-Disposition") || "";
    const m = /filename\*?=(?:UTF-8'')?"?([^";\r\n]+)"?/i.exec(cd);
    const filename = m ? decodeURIComponent(m[1].trim()) : `batch_${batchId}.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    setInlineMessage(message, "Export downloaded.", "success");
    await loadBatches();
  } catch (error) {
    setInlineMessage(message, normalizeUiErrorMessage(error.message), "server-error");
  }
}

async function handleBatchRowAction(action, batchId) {
  if (action === "view") {
    await viewBatchPreview(batchId);
  } else if (action === "review") {
    window.location.href = batchReviewUrl(batchId);
  } else if (action === "export") {
    await exportBatch(batchId);
  }
}

// Minimal toast implementation that stacks, auto-dismisses, and supports an
// action link. Uses a single container that we create on demand.
function showToast(message, kind, action) {
  let host = document.getElementById("toastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "toastHost";
    host.style.cssText = "position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;";
    document.body.appendChild(host);
  }
  const t = document.createElement("div");
  t.className = `toast toast-${kind || "info"}`;
  t.style.cssText = "background:#fffbea;border:1px solid #f0c36d;color:#663c00;padding:10px 14px;border-radius:8px;box-shadow:0 4px 10px rgba(0,0,0,.08);min-width:260px;max-width:360px;font-size:13px;display:flex;gap:10px;align-items:center;";
  const msg = document.createElement("div");
  msg.style.flex = "1"; msg.textContent = message;
  t.appendChild(msg);
  if (action && action.href) {
    const a = document.createElement("a");
    a.href = action.href; a.target = "_blank"; a.rel = "noopener";
    a.textContent = action.label || "Open";
    a.style.cssText = "font-weight:600;color:#1a46b8;text-decoration:underline;";
    t.appendChild(a);
  }
  const x = document.createElement("button");
  x.type = "button"; x.textContent = "×";
  x.style.cssText = "background:none;border:0;font-size:18px;cursor:pointer;color:#663c00;";
  x.onclick = () => t.remove();
  t.appendChild(x);
  host.appendChild(t);
  setTimeout(() => t.remove(), 12000);
}

$("createBatchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("batchName");
  const message = $("createBatchMessage");
  setInlineMessage(message, "Creating batch...");

  try {
    const companyId = $("companySelector").value;
    if (!companyId) throw new Error("Select a company first.");

    const scanMode = "summary";
    const batch = await api("/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_name: input.value.trim(), company_id: companyId, scan_mode: scanMode }),
    });
    input.value = "";
    setInlineMessage(message, `Batch created: ${batch.id}`, "success");
    await loadBatches();
    await selectBatch(batch.id);
  } catch (error) {
    setInlineMessage(message, normalizeUiErrorMessage(error.message), "server-error");
  }
});

$("uploadBtn").addEventListener("click", async () => {
  const input = $("pdfFiles");
  const message = $("actionMessage");
  if (!state.selectedBatchId) {
    setInlineMessage(message, "Select a batch first.");
    return;
  }
  if (!input.files.length) {
    setInlineMessage(message, "Choose at least one PDF file.");
    return;
  }

  const form = new FormData();
  for (const file of input.files) form.append("files", file);

  setInlineMessage(message, "Uploading files...");
  try {
    const result = await api(`/batches/${state.selectedBatchId}/files`, { method: "POST", body: form });
    input.value = "";
    const msg = result.warning ? `Files uploaded. ${result.warning}` : "Files uploaded.";
    setInlineMessage(message, msg, result.warning ? "" : "success");
    await selectBatch(state.selectedBatchId);
    await loadBatches();
  } catch (error) {
    setInlineMessage(message, normalizeUiErrorMessage(error.message), "server-error");
  }
});

$("deleteBatchBtn").addEventListener("click", async () => {
  const message = $("actionMessage");
  if (!state.selectedBatchId) {
    setInlineMessage(message, "Select a batch first.");
    return;
  }
  const batchId = state.selectedBatchId;
  const batchName = $("selectedBatchName")?.textContent || batchId;
  const confirmed = window.confirm(`Delete batch "${batchName}" permanently? This removes uploaded files, rows, and batch review/export records.`);
  if (!confirmed) return;
  setInlineMessage(message, "Deleting batch...");
  try {
    await api(`/batches/${batchId}`, { method: "DELETE" });
    state.selectedBatchId = null;
    $("selectedBatchPanel").classList.add("hidden");
    $("selectedBatchEmpty").classList.remove("hidden");
    $("filesTableBody").innerHTML = '<tr><td colspan="6" class="muted">No files uploaded yet.</td></tr>';
    if ($("rowsTableBody")) $("rowsTableBody").innerHTML = '<tr><td colspan="9" class="muted">Select a batch first.</td></tr>';
    setInlineMessage(message, "Batch deleted.", "success");
    await loadBatches();
  } catch (error) {
    setInlineMessage(message, normalizeUiErrorMessage(error.message), "server-error");
  }
});

$("processBtn").addEventListener("click", async () => {
  const message = $("actionMessage");
  if (!state.selectedBatchId) {
    setInlineMessage(message, "Select a batch first.");
    return;
  }

  setInlineMessage(message, "Starting processing...");
  try {
    await api(`/batches/${state.selectedBatchId}/process`, { method: "POST" });
    // Show briefly then clear — the poller updates status/notes directly
    setInlineMessage(message, "Processing started — monitoring progress…", "success");
    setTimeout(() => {
      if (message.textContent === "Processing started — monitoring progress…") {
        setInlineMessage(message, "");
      }
    }, 3500);
    await selectBatch(state.selectedBatchId);
    await loadBatches();
    startProgressPolling();
  } catch (error) {
    setInlineMessage(message, normalizeUiErrorMessage(error.message), "server-error");
  }
});

const selectedExportBtn = $("exportBtn");
if (selectedExportBtn) selectedExportBtn.addEventListener("click", async () => {
  const message = $("actionMessage");
  if (!state.selectedBatchId) {
    setInlineMessage(message, "Select a batch first.");
    return;
  }

  setInlineMessage(message, "Preparing export...");
  try {
    // Use fetch directly (not api()) so we can read response headers for the
    // Content-Disposition filename — matches the Review page export behavior.
    const headers = typeof authHeaders === "function" ? authHeaders() : {};
    const response = await fetch(`/batches/${state.selectedBatchId}/export`, { headers });
    if (!response.ok) {
      const text = await response.text();
      setInlineMessage(message, normalizeUiErrorMessage(text), "server-error");
      return;
    }
    const blob = await response.blob();
    const cd = response.headers.get("Content-Disposition") || "";
    const m = /filename\*?=(?:UTF-8'')?"?([^";\r\n]+)"?/i.exec(cd);
    const filename = m ? decodeURIComponent(m[1].trim()) : `batch_${state.selectedBatchId}.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    setInlineMessage(message, "Export downloaded.", "success");
  } catch (error) {
    setInlineMessage(message, normalizeUiErrorMessage(error.message), "server-error");
  }
});

const refreshRowsBtn = $("refreshRowsBtn");
if (refreshRowsBtn) refreshRowsBtn.addEventListener("click", loadRows);

const selectedReviewBtn = $("reviewBtn");
if (selectedReviewBtn) selectedReviewBtn.addEventListener("click", () => {
  if (!state.selectedBatchId) {
    alert("Select a batch first.");
    return;
  }
  window.location.href = batchReviewUrl(state.selectedBatchId);
});
const logoutBtn = document.getElementById("logoutBtn");
if (logoutBtn) {
  logoutBtn.addEventListener("click", logoutAndGo);
}
$("refreshBatchesBtn").addEventListener("click", loadBatches);
$("companySelector").addEventListener("change", async () => {
  state.selectedBatchId = null;
  $("selectedBatchPanel").classList.add("hidden");
  $("selectedBatchEmpty").classList.remove("hidden");
  await loadBatches();
});

$("tenantSelector").addEventListener("change", async (event) => {
  setTenantId(event.target.value);
  state.selectedBatchId = null;
  $("selectedBatchPanel").classList.add("hidden");
  $("selectedBatchEmpty").classList.remove("hidden");
  try {
    await loadCompanies();
    await loadBatches();
  } catch (error) {
    setInlineMessage($("createBatchMessage"), normalizeUiErrorMessage(error.message), "server-error");
  }
});

document.querySelectorAll('input[name="batchScanMode"]').forEach((radio) => {
  radio.addEventListener("change", async () => {
    if (!state.selectedBatchId) return;
    const msg = $("scanModeMessage");
    try {
      await api(`/batches/${state.selectedBatchId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scan_mode: radio.value }),
      });
      setInlineMessage(msg, `Mode set to: ${radio.value === "lines" ? "Separate line items" : "Total invoice"}`, "success");
    } catch (error) {
      setInlineMessage(msg, normalizeUiErrorMessage(error.message), "server-error");
    }
  });
});

async function initScannerPage() {
  ensureAuth();
  try {
    const session = await getSessionInfo();
    setWorkspaceLink(session.role);
    await loadTenantOptions();
    await loadCompanies();
    await loadBatches();
  } catch (error) {
    const msgEl = $("createBatchMessage");
    if (msgEl) setInlineMessage(msgEl, normalizeUiErrorMessage(error.message), "server-error");
  }
}

// ap-ui.js loads before app.js (see scanner.html), so renderShell() has
// already run synchronously by the time this executes.
initScannerPage();

// ── Collapsible scanner sections ─────────────────────────────────────────────
(function wireCollapsible() {
  const SECTIONS = [
    { toggleId: "batchesSectionToggle", bodyId: "batchesSectionBody", key: "ap_batches_collapsed" },
  ];
  SECTIONS.forEach(({ toggleId, bodyId, key }) => {
    const toggle = document.getElementById(toggleId);
    const body   = document.getElementById(bodyId);
    if (!toggle || !body) return;
    const collapsed = sessionStorage.getItem(key) === "1";
    body.classList.toggle("section-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.textContent = collapsed ? "▶" : "▼";
    toggle.addEventListener("click", () => {
      const nowCollapsed = !body.classList.contains("section-collapsed");
      body.classList.toggle("section-collapsed", nowCollapsed);
      toggle.setAttribute("aria-expanded", String(!nowCollapsed));
      toggle.textContent = nowCollapsed ? "▶" : "▼";
      try { sessionStorage.setItem(key, nowCollapsed ? "1" : "0"); } catch {}
    });
  });
})();


initPageHelp({
  title: "Scanning Tool help",
  subtitle: "Use this page to create batches, upload PDFs, process them and export structured output.",
  sections: [
    { heading: "Tenant and company selection", items: ["Select the correct tenant first.", "Then select the company that should own the scanned invoices.", "Batches are company-specific, so changing company changes the batch list."] },
    { heading: "Create and upload", items: ["Create a new batch with a meaningful name.", "Upload one or more invoice PDFs into the selected batch.", "Review the uploaded files table for page counts and file status."] },
    { heading: "Process and review", items: ["Use Process Batch to trigger extraction.", "Watch status and notes while processing is running.", "Use View, Review and Export from the batch row once scanning is complete."] },
    { heading: "Export", items: ["Use the batch-row Export button after processing finishes.", "Check posting account and nominal account suggestions before posting into the ERP if your process requires review."] }
  ],
  quickChecks: ["Confirm the correct tenant and company before creating the batch.", "Use clear batch names such as month plus supplier or business purpose.", "Do not export until the batch status is no longer processing."]
});
