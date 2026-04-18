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
    tbody.innerHTML = '<tr><td colspan="5" class="muted">No batches found.</td></tr>';
    return;
  }

  for (const batch of state.batches) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(batch.batch_name)}</strong><br /><span class="muted">${escapeHtml(batch.id)}</span></td>
      <td><span class="pill">${escapeHtml(batch.status || "-")}</span></td>
      <td>${batch.page_count ?? "-"}</td>
      <td>${formatDate(batch.created_at)}</td>
      <td>${formatDate(batch.processed_at)}</td>
    `;
    tr.addEventListener("click", () => selectBatch(batch.id));
    tbody.appendChild(tr);
  }
}

async function selectBatch(batchId, options = {}) {
  state.selectedBatchId = batchId;
  const batch = await api(`/batches/${batchId}`);

  // Clear any stale action messages when the user selects a different batch
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
  await loadRows();

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
      if (m.includes("azure_di") || m.includes("_di")) return "DI";
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
      // Clear the stale "Batch processing started." banner now that processing is done
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
    cell.innerHTML = renderReviewCell(fs, state.selectedBatchId);
    tr.classList.toggle("needs-review", fs.review_state === "needs_review");
    // Row flash on needs_review transition (no popup toast)
    const key = `${state.selectedBatchId}|${fs.file_id}`;
    if (fs.review_state === "needs_review" && !_announcedReviewFiles.has(key)) {
      _announcedReviewFiles.add(key);
      tr.classList.add("row-flash");
      setTimeout(() => tr.classList.remove("row-flash"), 2500);
    }
  });
}

function renderReviewCell(fs, batchId) {
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
    $("rowsTableBody").innerHTML = '<tr><td colspan="9" class="muted">Select a batch first.</td></tr>';
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
    // Show briefly, then clear — the progress poller updates status/notes directly
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

$("exportBtn").addEventListener("click", async () => {
  const message = $("actionMessage");
  if (!state.selectedBatchId) {
    setInlineMessage(message, "Select a batch first.");
    return;
  }

  setInlineMessage(message, "Preparing export...");
  try {
    // Use fetch directly (not api()) so we can read response headers for the
    // Content-Disposition filename — matches the Review page export behavior.
    const token = typeof getToken === "function" ? getToken() : null;
    const headers = token ? { "Authorization": `Bearer ${token}` } : {};
    const response = await fetch(`/batches/${state.selectedBatchId}/export`, { headers });
    if (!response.ok) {
      const text = await response.text();
      setInlineMessage(message, normalizeUiErrorMessage(text), "server-error");
      return;
    }
    const blob = await response.blob();
    const cd = response.headers.get("Content-Disposition") || "";
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
    const filename = m ? decodeURIComponent(m[1]) : `batch_${state.selectedBatchId}.xlsx`;
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

$("refreshRowsBtn").addEventListener("click", loadRows);

$("reviewBtn").addEventListener("click", () => {
  if (!state.selectedBatchId) {
    alert("Select a batch first.");
    return;
  }
  window.location.href = `/static/review.html?batch_id=${state.selectedBatchId}`;
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
    setInlineMessage($("createBatchMessage"), normalizeUiErrorMessage(error.message), "server-error");
  }
}

initScannerPage();

// ── Collapsible scanner sections ─────────────────────────────────────────────
(function wireCollapsible() {
  const SECTIONS = [
    { toggleId: "batchesSectionToggle", bodyId: "batchesSectionBody", key: "ap_batches_collapsed" },
    { toggleId: "rowsSectionToggle",    bodyId: "rowsSectionBody",    key: "ap_rows_collapsed"   },
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
    { heading: "Process and review", items: ["Use Process Batch to trigger extraction.", "Watch status and notes while processing is running.", "Use Extracted Rows to spot-check supplier, invoice number, dates, totals and review flags."] },
    { heading: "Export", items: ["Use Export Excel after processing finishes.", "Check posting account and nominal account suggestions before posting into the ERP if your process requires review."] }
  ],
  quickChecks: ["Confirm the correct tenant and company before creating the batch.", "Use clear batch names such as month plus supplier or business purpose.", "Do not export until the batch status is no longer processing."]
});
