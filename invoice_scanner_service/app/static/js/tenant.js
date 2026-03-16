ensureAuth();

document.getElementById("logoutBtn").addEventListener("click", logoutAndGo);
const tenantSelectorEl = document.getElementById("tenantSelector");
if (tenantSelectorEl) {
  tenantSelectorEl.addEventListener("change", async (event) => {
    setTenantId(event.target.value);
    await reloadTenantAdmin();
  });
}

function setImportProgress(prefix, percent, label, active = true) {
  const wrap = document.getElementById(`${prefix}ImportProgress`);
  const bar = document.getElementById(`${prefix}ImportProgressBar`);
  const text = document.getElementById(`${prefix}ImportProgressText`);
  if (!wrap || !bar || !text) return;

  wrap.classList.toggle("active", active);
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  bar.style.width = `${safePercent}%`;
  text.textContent = label || "";
}

function resetImportProgress(prefix) {
  setImportProgress(prefix, 0, "", false);
}

function uploadWithProgress(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path, true);

    const headers = authHeaders();
    Object.entries(headers).forEach(([key, value]) => {
      if (value) xhr.setRequestHeader(key, value);
    });

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) {
        const percent = Math.round((event.loaded / event.total) * 100);
        onProgress(percent, true);
      }
    });

    xhr.onreadystatechange = () => {
      if (xhr.readyState !== XMLHttpRequest.DONE) return;

      const contentType = xhr.getResponseHeader("content-type") || "";
      let payload = null;
      if (contentType.includes("application/json") && xhr.responseText) {
        try {
          payload = JSON.parse(xhr.responseText);
        } catch (_) {
          payload = null;
        }
      }

      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload || {});
        return;
      }

      let message = "Something went wrong on the server. Please refresh the page or try again.";
      if (xhr.status === 401) {
        message = "Your session has expired. Please log in again.";
      } else if (xhr.status === 403) {
        message = "You do not have permission to use this feature.";
      } else if (xhr.status === 404) {
        message = "The requested item could not be found.";
      } else if (payload?.detail) {
        message = typeof payload.detail === "string" ? payload.detail : "The request could not be completed.";
      }

      const error = new Error(message);
      error.status = xhr.status;
      reject(error);
    };

    xhr.onerror = () => reject(new Error("Something went wrong on the server. Please refresh the page or try again."));
    xhr.send(formData);
  });
}

document.getElementById("profileForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_name: document.getElementById("profileTenantName").value.trim(),
        contact_name: document.getElementById("profileContactName").value.trim() || null,
        contact_email: document.getElementById("profileContactEmail").value.trim() || null,
        notes: document.getElementById("profileNotes").value.trim() || null,
      }),
    });
    setMessage("profileMessage", "Details updated.", "success");
  } catch (error) {
    setMessage("profileMessage", error.message);
  }
});

document.getElementById("passwordForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: document.getElementById("currentPassword").value,
        new_password: document.getElementById("newPassword").value,
      }),
    });
    setMessage("passwordMessage", "Password updated.", "success");
    event.target.reset();
  } catch (error) {
    setMessage("passwordMessage", error.message);
  }
});

document.getElementById("companyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/companies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        company_code: document.getElementById("companyCode").value.trim(),
        company_name: document.getElementById("companyName").value.trim(),
        registration_number: document.getElementById("companyReg").value.trim() || null,
        vat_number: document.getElementById("companyVat").value.trim() || null,
        is_active: true,
      }),
    });
    setMessage("companyMessage", "Company added.", "success");
    event.target.reset();
    await loadCompanies();
  } catch (error) {
    setMessage("companyMessage", error.message);
  }
});

document.getElementById("supplierForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/suppliers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        supplier_account_code: document.getElementById("supplierAccountCode").value.trim(),
        supplier_name: document.getElementById("supplierName").value.trim(),
        default_nominal: document.getElementById("supplierDefaultNominal").value.trim() || null,
        is_active: true,
      }),
    });
    setMessage("supplierMessage", "Supplier added.", "success");
    event.target.reset();
    await loadSuppliers();
  } catch (error) {
    setMessage("supplierMessage", error.message);
  }
});


document.getElementById("supplierImportBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("supplierImportFile");
  const btn = document.getElementById("supplierImportBtn");
  const file = fileInput?.files?.[0];
  if (!file) {
    setMessage("supplierMessage", "Select a suppliers CSV file first.");
    return;
  }

  const formData = new FormData();
  formData.append("file", file);

  try {
    btn.disabled = true;
    setMessage("supplierMessage", "");
    setImportProgress("supplier", 3, "Preparing supplier import…", true);
    const result = await uploadWithProgress("/tenant/suppliers/import", formData, (percent) => {
      const visual = Math.min(percent, 95);
      setImportProgress("supplier", visual, visual < 100 ? `Uploading supplier CSV… ${visual}%` : "Processing supplier import…", true);
    });
    setImportProgress("supplier", 100, "Supplier import complete.", true);
    const summary = `Suppliers imported: ${result.imported}. Skipped: ${result.skipped}.`;
    setMessage("supplierMessage", result.errors?.length ? `${summary} ${result.errors.join(" ")}` : summary, "success");
    fileInput.value = "";
    await loadSuppliers();
  } catch (error) {
    setMessage("supplierMessage", error.message);
  } finally {
    btn.disabled = false;
    setTimeout(() => resetImportProgress("supplier"), 1200);
  }
});

document.getElementById("accountForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/nominal-accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_code: document.getElementById("accountCode").value.trim(),
        account_name: document.getElementById("accountName").value.trim(),
        is_active: true,
      }),
    });
    setMessage("accountMessage", "Nominal account added.", "success");
    event.target.reset();
    await loadAccounts();
  } catch (error) {
    setMessage("accountMessage", error.message);
  }
});

document.getElementById("issueForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/issues", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: document.getElementById("issueTitle").value.trim(),
        priority: document.getElementById("issuePriority").value,
        description: document.getElementById("issueDescription").value.trim(),
      }),
    });
    setMessage("issueMessage", "Issue submitted.", "success");
    event.target.reset();
    await loadIssues();
  } catch (error) {
    setMessage("issueMessage", error.message);
  }
});


document.getElementById("nominalImportBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("nominalImportFile");
  const btn = document.getElementById("nominalImportBtn");
  const file = fileInput?.files?.[0];
  if (!file) {
    setMessage("accountMessage", "Select an AP nominal CSV file first.");
    return;
  }

  const formData = new FormData();
  formData.append("file", file);

  try {
    btn.disabled = true;
    setMessage("accountMessage", "");
    setImportProgress("nominal", 3, "Preparing nominal import…", true);
    const result = await uploadWithProgress("/tenant/nominal-accounts/import", formData, (percent) => {
      const visual = Math.min(percent, 95);
      setImportProgress("nominal", visual, visual < 100 ? `Uploading nominal CSV… ${visual}%` : "Processing nominal import…", true);
    });
    setImportProgress("nominal", 100, "Nominal import complete.", true);
    const summary = `Nominal accounts imported: ${result.imported}. Skipped: ${result.skipped}.`;
    setMessage("accountMessage", result.errors?.length ? `${summary} ${result.errors.join(" ")}` : summary, "success");
    fileInput.value = "";
    await loadAccounts();
  } catch (error) {
    setMessage("accountMessage", error.message);
  } finally {
    btn.disabled = false;
    setTimeout(() => resetImportProgress("nominal"), 1200);
  }
});

async function loadProfile() {
  const profile = await apiFetch("/tenant/profile");
  document.getElementById("profileTenantName").value = profile.tenant_name || "";
  document.getElementById("profileContactName").value = profile.contact_name || "";
  document.getElementById("profileContactEmail").value = profile.contact_email || "";
  document.getElementById("profileNotes").value = profile.notes || "";
}

async function loadCompanies() {
  const rows = await apiFetch("/tenant/companies");
  const tbody = document.getElementById("companiesTableBody");
  tbody.innerHTML = rows.length
    ? rows.map((row) => `
        <tr>
          <td>${escapeHtml(row.company_code)}</td>
          <td>${escapeHtml(row.company_name)}</td>
          <td>${escapeHtml(row.registration_number || "-")}</td>
          <td>${escapeHtml(row.vat_number || "-")}</td>
          <td>${row.is_active ? "Yes" : "No"}</td>
        </tr>
      `).join("")
    : '<tr><td colspan="5" class="muted">No companies found.</td></tr>';
}

async function loadSuppliers() {
  const rows = await apiFetch("/tenant/suppliers");
  const tbody = document.getElementById("suppliersTableBody");
  tbody.innerHTML = rows.length
    ? rows.map((row) => `
        <tr>
          <td>${escapeHtml(row.supplier_account_code || row.posting_account || "-")}</td>
          <td>${escapeHtml(row.supplier_name)}</td>
          <td>${escapeHtml(row.default_nominal || "-")}</td>
        </tr>
      `).join("")
    : '<tr><td colspan="3" class="muted">No suppliers found.</td></tr>';
}

async function loadAccounts() {
  const rows = await apiFetch("/tenant/nominal-accounts");
  const tbody = document.getElementById("accountsTableBody");
  tbody.innerHTML = rows.length
    ? rows.map((row) => `
        <tr>
          <td>${escapeHtml(row.account_code)}</td>
          <td>${escapeHtml(row.account_name)}</td>
        </tr>
      `).join("")
    : '<tr><td colspan="2" class="muted">No nominal accounts found.</td></tr>';
}

async function loadIssues() {
  const rows = await apiFetch("/tenant/issues");
  const tbody = document.getElementById("issueTenantTableBody");
  tbody.innerHTML = rows.length
    ? rows.map((row) => `
        <tr>
          <td>${row.id}</td>
          <td>${escapeHtml(row.title)}</td>
          <td>${escapeHtml(row.status)}</td>
          <td>${escapeHtml(row.priority)}</td>
          <td>${escapeHtml(row.resolution_notes || "-")}</td>
          <td>${fmtDate(row.updated_at)}</td>
        </tr>
      `).join("")
    : '<tr><td colspan="6" class="muted">No issues logged.</td></tr>';
}

async function reloadTenantAdmin() {
  try {
    await Promise.all([loadProfile(), loadCompanies(), loadSuppliers(), loadAccounts(), loadIssues()]);
  } catch (error) {
    setMessage("pageMessage", error.message);
  }
}

async function initTenantPage() {
  try {
    if (tenantSelectorEl) {
      await populateTenantSelector("tenantSelector");
    }
    await reloadTenantAdmin();
  } catch (error) {
    setMessage("pageMessage", error.message);
  }

  initPageHelp({
    title: "Tenant Admin help",
    subtitle: "Manage the master data your tenant uses during invoice processing.",
    sections: [
      {
        heading: "Tenant details",
        body: "Keep the tenant profile and contact details up to date so the workspace remains identifiable.",
        items: [
          "Use Tenant Details to maintain the tenant name and notes.",
          "Change Password updates the current user password for this account.",
        ],
      },
      {
        heading: "Companies",
        body: "Companies let you store invoice data against the correct legal entity.",
        items: [
          "Add company code and company name before scanning live invoices.",
          "Registration and VAT numbers are optional reference fields.",
        ],
      },
      {
        heading: "Suppliers and nominals",
        body: "These master lists improve supplier matching and nominal code assignment.",
        items: [
          "Supplier CSV must contain supplier account code, supplier name and default nominal.",
          "Nominal CSV must contain nominal code and nominal account name.",
          "During import you will now see a progress bar while the file uploads and processes.",
        ],
      },
      {
        heading: "Issues",
        body: "Use Report Issue to capture problems for follow-up and resolution.",
        items: [
          "Set a higher priority for processing issues that block invoice work.",
          "Review the issue table to track the current status and any resolution notes.",
        ],
      },
    ],
    quickChecks: [
      "Confirm the correct tenant is selected before importing master data.",
      "Use UTF-8 CSV files for supplier and nominal imports.",
      "Check the success message after each import to confirm imported and skipped rows.",
    ],
  });
}

initTenantPage();
