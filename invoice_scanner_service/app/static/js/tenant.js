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
    [...form.querySelectorAll("input, button")].forEach((el) => { el.disabled = !enabled; });
  });
  ["supplierImportFile", "supplierImportBtn", "nominalImportFile", "nominalImportBtn"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = !enabled;
  });
  const note = enabled ? "" : "Select a company to view or import supplier and nominal data.";
  document.getElementById("masterDataHint").textContent = note;
}

document.getElementById("logoutBtn").addEventListener("click", logoutAndGo);
document.getElementById("tenantSelector").addEventListener("change", async (event) => {
  setTenantId(event.target.value);
  selectedCompanyId = "";
  await reloadTenantAdmin();
});
document.getElementById("companySelector").addEventListener("change", async (event) => {
  selectedCompanyId = event.target.value || "";
  setMasterDataState(!!selectedCompanyId);
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
  try {
    const result = await apiFetch(`/tenant/suppliers/import?company_id=${encodeURIComponent(selectedCompanyId)}`, {method: "POST", body: formData});
    const summary = `Suppliers imported: ${result.imported}. Skipped: ${result.skipped}.`;
    setMessage("supplierMessage", result.errors?.length ? `${summary} ${result.errors.join(" ")}` : summary, "success");
    fileInput.value = "";
    await loadSuppliers();
  } catch (error) { setMessage("supplierMessage", error.message); }
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
  try {
    const result = await apiFetch(`/tenant/nominal-accounts/import?company_id=${encodeURIComponent(selectedCompanyId)}`, {method: "POST", body: formData});
    const summary = `Nominal accounts imported: ${result.imported}. Skipped: ${result.skipped}.`;
    setMessage("accountMessage", result.errors?.length ? `${summary} ${result.errors.join(" ")}` : summary, "success");
    fileInput.value = "";
    await loadAccounts();
  } catch (error) { setMessage("accountMessage", error.message); }
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
  if (previous && rows.some((r) => r.id === previous)) {
    selector.value = previous;
  } else {
    selector.value = rows.length === 1 ? rows[0].id : "";
  }
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
  if (!selectedCompanyId) { tbody.innerHTML = '<tr><td colspan="2" class="muted">Select a company to view nominal accounts.</td></tr>'; return; }
  const rows = await apiFetch(`/tenant/nominal-accounts?company_id=${encodeURIComponent(selectedCompanyId)}`);
  tbody.innerHTML = rows.length ? rows.map((row) => `
        <tr>
          <td>${escapeHtml(row.account_code)}</td>
          <td>${escapeHtml(row.account_name)}</td>
        </tr>`).join("") : '<tr><td colspan="2" class="muted">No nominal accounts found for this company.</td></tr>';
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
  try { await Promise.all([loadProfile(), loadCompanies(), loadIssues()]); await Promise.all([loadSuppliers(), loadAccounts()]); } catch (error) { setMessage("pageMessage", error.message); }
}

async function initTenantPage() {
  try {
    const me = await getSessionInfo();
    const platformAdminLink = document.getElementById("platformAdminLink");
    if (platformAdminLink) {
      if (String(me.role || "").toLowerCase() === "admin") {
        platformAdminLink.classList.remove("hidden");
      } else {
        platformAdminLink.classList.add("hidden");
      }
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
    { heading: "Suppliers and nominal accounts", items: ["Suppliers and nominal accounts are now company-specific.", "Use the company selector to refresh the lists and import data into the correct company.", "Keep names and codes consistent with your finance system for the best matching results."] },
    { heading: "Password and issues", items: ["Use Change Password to rotate credentials.", "Use Report Issue to raise a support request and track progress and resolution directly in the system."] }
  ],
  quickChecks: ["Create at least one company before using the Scanning Tool.", "Select the correct company before importing supplier or nominal CSV files.", "Raise issues with enough detail for support to reproduce the problem."]
});
