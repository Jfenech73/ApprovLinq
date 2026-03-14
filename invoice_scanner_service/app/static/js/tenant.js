ensureAuth();
document.getElementById("logoutBtn").addEventListener("click", logoutAndGo);

async function loadProfile() {
  const profile = await apiFetch("/tenant/profile");
  profileTenantName.value = profile.tenant_name || "";
  profileContactName.value = profile.contact_name || "";
  profileContactEmail.value = profile.contact_email || "";
  profileNotes.value = profile.notes || "";
}

async function loadCompanies() {
  const rows = await apiFetch("/tenant/companies");
  companiesTableBody.innerHTML = rows.map(r => `<tr><td>${escapeHtml(r.company_code)}</td><td>${escapeHtml(r.company_name)}</td><td>${escapeHtml(r.registration_number || "-")}</td><td>${escapeHtml(r.vat_number || "-")}</td><td>${r.is_active ? "Yes" : "No"}</td></tr>`).join("");
}
async function loadSuppliers() {
  const rows = await apiFetch("/tenant/suppliers");
  suppliersTableBody.innerHTML = rows.map(r => `<tr><td>${escapeHtml(r.supplier_name)}</td><td>${escapeHtml(r.posting_account)}</td></tr>`).join("");
}
async function loadAccounts() {
  const rows = await apiFetch("/tenant/nominal-accounts");
  accountsTableBody.innerHTML = rows.map(r => `<tr><td>${escapeHtml(r.account_code)}</td><td>${escapeHtml(r.account_name)}</td></tr>`).join("");
}
async function loadIssues() {
  const rows = await apiFetch("/tenant/issues");
  issueTenantTableBody.innerHTML = rows.map(r => `<tr><td>${r.id}</td><td>${escapeHtml(r.title)}</td><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.priority)}</td><td>${escapeHtml(r.resolution_notes || "-")}</td><td>${fmtDate(r.updated_at)}</td></tr>`).join("");
}

profileForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await apiFetch("/tenant/profile", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tenant_name: profileTenantName.value, contact_name: profileContactName.value || null, contact_email: profileContactEmail.value || null, notes: profileNotes.value || null }) });
    setMessage("profileMessage", "Details updated", "success");
  } catch (error) { setMessage("profileMessage", error.message); }
});
passwordForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await apiFetch("/auth/change-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_password: currentPassword.value, new_password: newPassword.value }) });
    setMessage("passwordMessage", "Password updated", "success");
    e.target.reset();
  } catch (error) { setMessage("passwordMessage", error.message); }
});
companyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await apiFetch("/tenant/companies", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ company_code: companyCode.value, company_name: companyName.value, registration_number: companyReg.value || null, vat_number: companyVat.value || null, is_active: true }) });
    setMessage("companyMessage", "Company added", "success");
    e.target.reset();
    await loadCompanies();
  } catch (error) { setMessage("companyMessage", error.message); }
});
supplierForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  await apiFetch("/tenant/suppliers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ supplier_name: supplierName.value, posting_account: supplierPostingAccount.value, is_active: true }) });
  e.target.reset();
  await loadSuppliers();
});
accountForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  await apiFetch("/tenant/nominal-accounts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account_code: accountCode.value, account_name: accountName.value, is_active: true }) });
  e.target.reset();
  await loadAccounts();
});
issueForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await apiFetch("/tenant/issues", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: issueTitle.value, priority: issuePriority.value, description: issueDescription.value }) });
    setMessage("issueMessage", "Issue submitted", "success");
    e.target.reset();
    await loadIssues();
  } catch (error) { setMessage("issueMessage", error.message); }
});

Promise.all([loadProfile(), loadCompanies(), loadSuppliers(), loadAccounts(), loadIssues()]);
