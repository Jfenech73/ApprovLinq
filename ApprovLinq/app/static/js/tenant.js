ensureAuth();

let selectedCompanyId = "";

function getSelectedCompanyId() {
  return document.getElementById("companySelector").value || "";
}

function requireSelectedCompany(messageId = "pageMessage") {
  selectedCompanyId = getSelectedCompanyId();
  if (!selectedCompanyId) {
    setMessage(messageId, "Select a company first.");
    return false;
  }
  return true;
}

function setMasterDataState(enabled) {
  ["supplierForm", "accountForm"].forEach((id) => {
    const form = document.getElementById(id);
    if (!form) return;
    [...form.querySelectorAll("input, button")].forEach((el) => { el.disabled = !enabled; });
  });
  ["supplierImportFile", "supplierImportBtn", "nominalImportFile", "nominalImportBtn"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = !enabled;
  });
  const note = enabled ? "" : "Select a company to view or import supplier and nominal data.";
  const hint = document.getElementById("masterDataHint");
  if (hint) hint.textContent = note;
}

function setImportProgress(prefix, percent, text) {
  const wrapper = document.getElementById(`${prefix}ImportProgress`);
  const fill = document.getElementById(`${prefix}ImportProgressFill`);
  const label = document.getElementById(`${prefix}ImportProgressText`);
  if (wrapper) wrapper.classList.remove("hidden");
  if (fill) fill.style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
  if (label) label.textContent = text || "";
}

function hideImportProgress(prefix) {
  const wrapper = document.getElementById(`${prefix}ImportProgress`);
  const fill = document.getElementById(`${prefix}ImportProgressFill`);
  const label = document.getElementById(`${prefix}ImportProgressText`);
  if (fill) fill.style.width = "0%";
  if (label) label.textContent = "Waiting to start import.";
  if (wrapper) wrapper.classList.add("hidden");
}

function uploadFormDataWithProgress(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path, true);

    const token = getToken();
    const tenantId = getTenantId();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    if (tenantId) xhr.setRequestHeader("X-Tenant-Id", tenantId);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && typeof onProgress === "function") {
        onProgress(event.loaded, event.total);
      }
    };

    xhr.onerror = () => reject(new Error("The upload failed. Please try again."));
    xhr.onabort = () => reject(new Error("The upload was cancelled."));
    xhr.onload = () => {
      const contentType = xhr.getResponseHeader("content-type") || "";
      let payload = null;
      if (contentType.includes("application/json") && xhr.responseText) {
        try { payload = JSON.parse(xhr.responseText); } catch (_) { payload = null; }
      }

      if (xhr.status < 200 || xhr.status >= 300) {
        let message = "Something went wrong on the server. Please refresh the page or try again.";
        if (xhr.status === 401) message = "Your session has expired. Please log in again.";
        else if (xhr.status === 403) message = "You do not have permission to use this feature.";
        else if (xhr.status === 404) message = "The requested item could not be found.";
        else if (payload && typeof payload.detail === "string") message = payload.detail;
        const error = new Error(message);
        error.status = xhr.status;
        reject(error);
        return;
      }

      resolve(payload);
    };

    xhr.send(formData);
  });
}

// logoutBtn is injected by ap-ui.js shell — wired there via logoutAndGo
document.getElementById("tenantSelector").addEventListener("change", async (event) => {
  setTenantId(event.target.value);
  selectedCompanyId = "";
  hideImportProgress("supplier");
  hideImportProgress("nominal");
  await reloadTenantAdmin();
});
document.getElementById("companySelector").addEventListener("change", async (event) => {
  selectedCompanyId = event.target.value || "";
  setMasterDataState(!!selectedCompanyId);
  hideImportProgress("supplier");
  hideImportProgress("nominal");
  await Promise.all([loadSuppliers(), loadAccounts()]);
});

document.getElementById("profileForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/profile", {method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({tenant_name: document.getElementById("profileTenantName").value.trim(), contact_name: document.getElementById("profileContactName").value.trim() || null, contact_email: document.getElementById("profileContactEmail").value.trim() || null, notes: document.getElementById("profileNotes").value.trim() || null})});
    setMessage("profileMessage", "Details updated.", "success");
  } catch (error) { setMessage("profileMessage", error.message); }
});

document.getElementById("passwordForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/auth/change-password", {method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({current_password: document.getElementById("currentPassword").value, new_password: document.getElementById("newPassword").value})});
    setMessage("passwordMessage", "Password updated.", "success");
    event.target.reset();
  } catch (error) { setMessage("passwordMessage", error.message); }
});

document.getElementById("companyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/companies", {method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({company_code: document.getElementById("companyCode").value.trim(), company_name: document.getElementById("companyName").value.trim(), registration_number: document.getElementById("companyReg").value.trim() || null, vat_number: document.getElementById("companyVat").value.trim() || null, is_active: true})});
    setMessage("companyMessage", "Company added.", "success");
    event.target.reset();
    await loadCompanies();
    await Promise.all([loadSuppliers(), loadAccounts()]);
  } catch (error) { setMessage("companyMessage", error.message); }
});

document.getElementById("supplierForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireSelectedCompany("supplierMessage")) return;
  try {
    await apiFetch("/tenant/suppliers", {method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({company_id: selectedCompanyId, supplier_account_code: document.getElementById("supplierAccountCode").value.trim(), supplier_name: document.getElementById("supplierName").value.trim(), default_nominal: document.getElementById("supplierDefaultNominal").value.trim() || null, is_active: true})});
    setMessage("supplierMessage", "Supplier added.", "success");
    event.target.reset();
    await loadSuppliers();
  } catch (error) { setMessage("supplierMessage", error.message); }
});

document.getElementById("supplierImportBtn").addEventListener("click", async () => {
  if (!requireSelectedCompany("supplierMessage")) return;
  const fileInput = document.getElementById("supplierImportFile");
  const file = fileInput?.files?.[0];
  if (!file) { setMessage("supplierMessage", "Select a suppliers CSV file first."); return; }
  const formData = new FormData(); formData.append("file", file);
  setMessage("supplierMessage", "");
  setImportProgress("supplier", 5, "Starting supplier upload...");
  try {
    const result = await uploadFormDataWithProgress(`/tenant/suppliers/import?company_id=${encodeURIComponent(selectedCompanyId)}`, formData, (loaded, total) => {
      const percent = total ? Math.round((loaded / total) * 80) : 35;
      setImportProgress("supplier", Math.max(10, percent), `Uploading suppliers... ${Math.round((loaded / Math.max(total || 1, 1)) * 100)}%`);
    });
    setImportProgress("supplier", 95, "Processing supplier import...");
    const summary = `Suppliers imported: ${result.imported}. Updated: ${result.updated || 0}. Skipped: ${result.skipped}.`;
    setImportProgress("supplier", 100, result.errors?.length ? "Supplier import completed with warnings." : "Supplier import completed.");
    setMessage("supplierMessage", result.errors?.length ? `${summary} ${result.errors.join(" ")}` : summary, result.errors?.length ? "" : "success");
    fileInput.value = "";
    await loadSuppliers();
  } catch (error) {
    setImportProgress("supplier", 100, "Supplier import failed.");
    setMessage("supplierMessage", error.message);
  }
});

document.getElementById("accountForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireSelectedCompany("accountMessage")) return;
  try {
    await apiFetch("/tenant/nominal-accounts", {method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({company_id: selectedCompanyId, account_code: document.getElementById("accountCode").value.trim(), account_name: document.getElementById("accountName").value.trim(), is_active: true})});
    setMessage("accountMessage", "Nominal account added.", "success");
    event.target.reset();
    await loadAccounts();
  } catch (error) { setMessage("accountMessage", error.message); }
});

document.getElementById("nominalImportBtn").addEventListener("click", async () => {
  if (!requireSelectedCompany("accountMessage")) return;
  const fileInput = document.getElementById("nominalImportFile");
  const file = fileInput?.files?.[0];
  if (!file) { setMessage("accountMessage", "Select an AP nominal CSV file first."); return; }
  const formData = new FormData(); formData.append("file", file);
  setMessage("accountMessage", "");
  setImportProgress("nominal", 5, "Starting nominal upload...");
  try {
    const result = await uploadFormDataWithProgress(`/tenant/nominal-accounts/import?company_id=${encodeURIComponent(selectedCompanyId)}`, formData, (loaded, total) => {
      const percent = total ? Math.round((loaded / total) * 80) : 35;
      setImportProgress("nominal", Math.max(10, percent), `Uploading nominal accounts... ${Math.round((loaded / Math.max(total || 1, 1)) * 100)}%`);
    });
    setImportProgress("nominal", 95, "Processing nominal import...");
    const summary = `Nominal accounts imported: ${result.imported}. Skipped: ${result.skipped}.`;
    setImportProgress("nominal", 100, result.errors?.length ? "Nominal import completed with warnings." : "Nominal import completed.");
    setMessage("accountMessage", result.errors?.length ? `${summary} ${result.errors.join(" ")}` : summary, result.errors?.length ? "" : "success");
    fileInput.value = "";
    await loadAccounts();
  } catch (error) {
    setImportProgress("nominal", 100, "Nominal import failed.");
    setMessage("accountMessage", error.message);
  }
});

document.getElementById("issueForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await apiFetch("/tenant/issues", {method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({title: document.getElementById("issueTitle").value.trim(), priority: document.getElementById("issuePriority").value, description: document.getElementById("issueDescription").value.trim()})});
    setMessage("issueMessage", "Issue submitted.", "success");
    event.target.reset();
    await loadIssues();
  } catch (error) { setMessage("issueMessage", error.message); }
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
  const selector = document.getElementById("companySelector");
  const previous = selectedCompanyId;
  selector.innerHTML = ['<option value="">Select company for suppliers and nominals</option>'].concat(rows.map((row) => `<option value="${row.id}">${escapeHtml(row.company_code)} - ${escapeHtml(row.company_name)}</option>`)).join("");
  if (previous && rows.some((r) => r.id === previous)) selector.value = previous;
  else if (rows.length === 1) selector.value = rows[0].id;
  else selector.value = "";
  selectedCompanyId = selector.value || "";
  setMasterDataState(!!selectedCompanyId);

  const tbody = document.getElementById("companiesTableBody");
  tbody.innerHTML = rows.length ? rows.map((row) => `
        <tr>
          <td>${escapeHtml(row.company_code)}</td>
          <td>${escapeHtml(row.company_name)}</td>
          <td>${escapeHtml(row.registration_number || "-")}</td>
          <td>${escapeHtml(row.vat_number || "-")}</td>
          <td>${row.is_active ? "Yes" : "No"}</td>
        </tr>`).join("") : '<tr><td colspan="5" class="muted">No companies found.</td></tr>';
}

async function loadSuppliers() {
  const tbody = document.getElementById("suppliersTableBody");
  if (!selectedCompanyId) { tbody.innerHTML = '<tr><td colspan="3" class="muted">Select a company to view suppliers.</td></tr>'; return; }
  const rows = await apiFetch(`/tenant/suppliers?company_id=${encodeURIComponent(selectedCompanyId)}`);
  tbody.innerHTML = rows.length ? rows.map((row) => `
        <tr>
          <td>${escapeHtml(row.supplier_account_code || row.posting_account || "-")}</td>
          <td>${escapeHtml(row.supplier_name)}</td>
          <td>${escapeHtml(row.default_nominal || "-")}</td>
        </tr>`).join("") : '<tr><td colspan="3" class="muted">No suppliers found for this company.</td></tr>';
}

async function loadAccounts() {
  const tbody = document.getElementById("accountsTableBody");
  if (!selectedCompanyId) { tbody.innerHTML = '<tr><td colspan="3" class="muted">Select a company to view nominal accounts.</td></tr>'; return; }
  const rows = await apiFetch(`/tenant/nominal-accounts?company_id=${encodeURIComponent(selectedCompanyId)}`);
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted">No nominal accounts found for this company.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((row) => `
        <tr>
          <td>${escapeHtml(row.account_code)}</td>
          <td>${escapeHtml(row.account_name)}</td>
          <td style="text-align:center">
            <input type="checkbox" class="nominal-default-cb" data-id="${row.id}" ${row.is_default ? "checked" : ""}
              title="${row.is_default ? "This is the default account — click to remove default" : "Click to set as default fallback account"}" />
          </td>
        </tr>`).join("");

  tbody.querySelectorAll(".nominal-default-cb").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const accountId = cb.dataset.id;
      try {
        if (cb.checked) {
          await apiFetch(`/tenant/nominal-accounts/${accountId}/set-default`, { method: "PUT" });
          setMessage("accountMessage", "Default nominal account updated.", "success");
        } else {
          await apiFetch(`/tenant/nominal-accounts/${accountId}/clear-default`, { method: "PUT" });
          setMessage("accountMessage", "Default removed — no fallback account set.", "success");
        }
        await loadAccounts();
      } catch (err) {
        setMessage("accountMessage", err.message);
        await loadAccounts();
      }
    });
  });
}

async function loadIssues() {
  const rows = await apiFetch("/tenant/issues");
  const tbody = document.getElementById("issueTenantTableBody");
  tbody.innerHTML = rows.length ? rows.map((row) => `
        <tr>
          <td>${row.id}</td>
          <td>${escapeHtml(row.title)}</td>
          <td>${escapeHtml(row.status)}</td>
          <td>${escapeHtml(row.priority)}</td>
          <td>${escapeHtml(row.resolution_notes || "-")}</td>
          <td>${fmtDate(row.updated_at)}</td>
        </tr>`).join("") : '<tr><td colspan="6" class="muted">No issues logged.</td></tr>';
}

async function reloadTenantAdmin() {
  try {
    await Promise.all([loadProfile(), loadCompanies(), loadIssues()]);
    await Promise.all([loadSuppliers(), loadAccounts()]);
  } catch (error) { setMessage("pageMessage", error.message); }
}

async function initTenantPage() {
  try {
    const me = await getSessionInfo();
    const platformAdminLink = document.getElementById("platformAdminLink");
    if (platformAdminLink) {
      if (String(me.role || "").toLowerCase() === "admin") platformAdminLink.classList.remove("hidden");
      else platformAdminLink.classList.add("hidden");
    }
    await populateTenantSelector("tenantSelector");
    await reloadTenantAdmin();
  } catch (error) { setMessage("pageMessage", error.message); }
}

initTenantPage();

initPageHelp({
  title: "Tenant Admin help",
  subtitle: "Use this page to maintain your tenant profile, company master data and issue reporting.",
  sections: [
    { heading: "Tenant details", items: ["Update the display name, contact details and notes for the selected tenant.", "If you have access to more than one tenant, switch the tenant from the selector before editing details."] },
    { heading: "Companies", items: ["Create a company for each legal entity or reporting entity that will own scanned invoice data.", "Choose a company in the selector before maintaining supplier and nominal master data."] },
    { heading: "Suppliers and nominal accounts", items: ["Suppliers and nominal accounts are company-specific.", "Use the company selector to refresh the lists and import data into the correct company.", "Keep names and codes consistent with your finance system for the best matching results."] },
    { heading: "Password and issues", items: ["Use Change Password to rotate credentials.", "Use Report Issue to raise a support request and track progress and resolution directly in the system."] }
  ],
  quickChecks: ["Create at least one company before using the Scanning Tool.", "Select the correct company before importing supplier or nominal CSV files.", "Raise issues with enough detail for support to reproduce the problem."]
});
