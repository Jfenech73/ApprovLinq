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
    tbody.innerHTML = '<tr><td colspan="5" class="muted">No files uploaded yet.</td></tr>';
    return;
  }

  for (const file of files) {
    const tr = document.createElement("tr");
    const errorText = file.error_message ? truncate(file.error_message, 160) : "-";
    tr.innerHTML = `
      <td>${escapeHtml(file.original_filename)}</td>
      <td><span class="pill">${escapeHtml(file.status)}</span></td>
      <td>${file.page_count ?? "-"}</td>
      <td title="${escapeHtml(file.error_message || "")}">${escapeHtml(errorText)}</td>
      <td>${formatDate(file.uploaded_at)}</td>
    `;
    tbody.appendChild(tr);
  }
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
    tr.innerHTML = `
      <td>${escapeHtml(row.source_filename || "-")}</td>
      <td>${row.page_no ?? "-"}</td>
      <td title="${escapeHtml(row.supplier_name || "")}">${escapeHtml(supplier)}</td>
      <td title="${escapeHtml(row.invoice_number || "")}">${escapeHtml(invoiceNo)}</td>
      <td>${escapeHtml(row.invoice_date || "-")}</td>
      <td title="${escapeHtml(row.description || "")}">${escapeHtml(description)}</td>
      <td>${row.total_amount ?? "-"}</td>
      <td>${confidenceDisplay(row.confidence_score)}</td>
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
    if (progress.status !== "processing") {
      stopProgressPolling();
      await selectBatch(state.selectedBatchId, { preservePolling: true });
      await loadBatches();
    }
  }, 3000);
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
    await api(`/batches/${state.selectedBatchId}/files`, { method: "POST", body: form });
    input.value = "";
    setInlineMessage(message, "Files uploaded.", "success");
    await selectBatch(state.selectedBatchId);
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
    setInlineMessage(message, "Batch processing started.", "success");
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
    const response = await api(`/batches/${state.selectedBatchId}/export`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `batch_${state.selectedBatchId}.xlsx`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setInlineMessage(message, "Export downloaded.", "success");
  } catch (error) {
    setInlineMessage(message, normalizeUiErrorMessage(error.message), "server-error");
  }
});

$("refreshRowsBtn").addEventListener("click", loadRows);
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
