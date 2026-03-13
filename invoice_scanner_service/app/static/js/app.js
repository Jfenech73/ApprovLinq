const state = {
  selectedBatchId: null,
  batches: [],
};

const $ = (id) => document.getElementById(id);

function setMessage(el, text, kind = "") {
  el.textContent = text || "";
  el.className = `message ${kind}`.trim();
}

function formatDate(value) {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function safeText(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function truncate(value, max = 140) {
  const text = safeText(value);
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const rawText = await response.text();

  let data = null;
  try {
    data = rawText ? JSON.parse(rawText) : null;
  } catch {
    data = null;
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;

    if (rawText.includes("<title>Your service is almost ready!</title>")) {
      detail = "The service restarted or became unavailable while processing. Check Koyeb runtime logs.";
    } else if (data && typeof data === "object") {
      detail = data.detail || data.message || JSON.stringify(data);
    } else if (rawText) {
      detail = rawText;
    }

    throw new Error(detail || "Request failed");
  }

  return data;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function reviewBadge(row) {
  if (row.review_required) {
    return `<span class="pill">Yes</span>`;
  }
  return "No";
}

function confidenceDisplay(value) {
  if (value === null || value === undefined || value === "") return "-";
  return Number(value).toFixed(2);
}

async function loadBatches() {
  const data = await api("/batches");
  state.batches = Array.isArray(data) ? data : [];

  const tbody = $("batchesTableBody");
  tbody.innerHTML = "";

  if (!state.batches.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">No batches yet.</td></tr>`;
    return;
  }

  for (const batch of state.batches) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.innerHTML = `
      <td>
        <strong>${escapeHtml(batch.batch_name)}</strong><br />
        <span class="muted">${escapeHtml(batch.id)}</span>
      </td>
      <td><span class="pill">${escapeHtml(batch.status || "-")}</span></td>
      <td>${batch.page_count ?? "-"}</td>
      <td>${formatDate(batch.created_at)}</td>
      <td>${formatDate(batch.processed_at)}</td>
    `;
    tr.addEventListener("click", () => selectBatch(batch.id));
    tbody.appendChild(tr);
  }
}

async function selectBatch(batchId) {
  state.selectedBatchId = batchId;
  const batch = await api(`/batches/${batchId}`);

  $("selectedBatchEmpty").classList.add("hidden");
  $("selectedBatchPanel").classList.remove("hidden");
  $("selectedBatchId").textContent = batch.id;
  $("selectedBatchName").textContent = batch.batch_name;
  $("selectedBatchStatus").textContent = batch.status;
  $("selectedBatchNotes").textContent = batch.notes || "-";

  renderFiles(batch.files || []);
  await loadRows();
}

function renderFiles(files) {
  const tbody = $("filesTableBody");
  tbody.innerHTML = "";

  if (!files.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">No files uploaded yet.</td></tr>`;
    return;
  }

  for (const file of files) {
    const tr = document.createElement("tr");
    const errorText = file.error_message ? truncate(file.error_message, 160) : "-";
    const statusClass =
      file.status === "failed"
        ? "pill"
        : file.status === "partial"
        ? "pill"
        : "pill";

    tr.innerHTML = `
      <td>${escapeHtml(file.original_filename)}</td>
      <td><span class="${statusClass}">${escapeHtml(file.status)}</span></td>
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
    tbody.innerHTML = `<tr><td colspan="9" class="muted">Select a batch first.</td></tr>`;
    return;
  }

  const rows = await api(`/batches/${state.selectedBatchId}/rows`);
  const safeRows = Array.isArray(rows) ? rows : [];

  if (!safeRows.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">No extracted rows yet.</td></tr>`;
    return;
  }

  for (const row of safeRows) {
    const description = truncate(row.description || "-", 80);
    const supplier = truncate(row.supplier_name || "-", 60);
    const invoiceNo = truncate(row.invoice_number || "-", 40);

    const debugParts = [];
    if (row.method_used) debugParts.push(`Method: ${row.method_used}`);
    if (row.validation_status) debugParts.push(`Validation: ${row.validation_status}`);
    if (row.page_text_raw) debugParts.push(`Text: ${truncate(row.page_text_raw, 220)}`);
    const rowTitle = debugParts.join(" | ");

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
      <td title="${escapeHtml(rowTitle)}">${reviewBadge(row)}</td>
    `;
    tbody.appendChild(tr);
  }
}

$("createBatchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("batchName");
  const message = $("createBatchMessage");

  setMessage(message, "Creating batch...");
  try {
    const batch = await api("/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_name: input.value.trim() }),
    });

    input.value = "";
    setMessage(message, `Batch created: ${batch.id}`, "success");
    await loadBatches();
    await selectBatch(batch.id);
  } catch (error) {
    setMessage(message, error.message, "error");
  }
});

$("uploadBtn").addEventListener("click", async () => {
  const message = $("actionMessage");
  const fileInput = $("pdfFiles");

  if (!state.selectedBatchId) {
    setMessage(message, "Select a batch first.", "warn");
    return;
  }

  if (!fileInput.files.length) {
    setMessage(message, "Choose at least one PDF file first.", "warn");
    return;
  }

  const formData = new FormData();
  for (const file of fileInput.files) {
    formData.append("files", file);
  }

  setMessage(message, `Uploading ${fileInput.files.length} file(s)...`);
  try {
    await api(`/batches/${state.selectedBatchId}/upload`, {
      method: "POST",
      body: formData,
    });

    fileInput.value = "";
    setMessage(message, "Upload completed.", "success");
    await loadBatches();
    await selectBatch(state.selectedBatchId);
  } catch (error) {
    setMessage(message, error.message, "error");
  }
});

$("processBtn").addEventListener("click", async () => {
  const message = $("actionMessage");

  if (!state.selectedBatchId) {
    setMessage(message, "Select a batch first.", "warn");
    return;
  }

  setMessage(message, "Processing batch...");
  try {
    const batch = await api(`/batches/${state.selectedBatchId}/process`, {
      method: "POST",
    });

    const extra =
      batch && batch.notes
        ? `Batch processing finished. ${batch.notes}`
        : "Batch processing finished.";

    setMessage(message, extra, "success");
    await loadBatches();
    await selectBatch(state.selectedBatchId);
  } catch (error) {
    setMessage(message, error.message, "error");
  }
});

$("exportBtn").addEventListener("click", () => {
  const message = $("actionMessage");

  if (!state.selectedBatchId) {
    setMessage(message, "Select a batch first.", "warn");
    return;
  }

  window.location.href = `/batches/${state.selectedBatchId}/export.xlsx`;
});

$("refreshBatchesBtn").addEventListener("click", async () => {
  try {
    await loadBatches();
    if (state.selectedBatchId) {
      await selectBatch(state.selectedBatchId);
    }
  } catch (error) {
    setMessage($("createBatchMessage"), error.message, "error");
  }
});

$("refreshRowsBtn").addEventListener("click", async () => {
  try {
    await loadRows();
  } catch (error) {
    setMessage($("actionMessage"), error.message, "error");
  }
});

loadBatches().catch((error) => {
  setMessage($("createBatchMessage"), error.message, "error");
});