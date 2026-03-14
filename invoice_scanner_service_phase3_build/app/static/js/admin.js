ensureAuth();
document.getElementById("logoutBtn").addEventListener("click", logoutAndGo);

async function loadTenants() {
  const tenants = await apiFetch("/admin/tenants");
  const tbody = document.getElementById("tenantsTableBody");
  const select = document.getElementById("userTenantIds");
  tbody.innerHTML = "";
  select.innerHTML = "";
  for (const t of tenants) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(t.tenant_name)}</td><td>${escapeHtml(t.tenant_code)}</td><td>${escapeHtml(t.status)}</td><td>${t.is_active ? "Yes" : "No"}</td><td>${escapeHtml(t.contact_email || "-")}</td><td><button class="btn btn-secondary" data-id="${t.id}" data-next="${t.status === "active" ? "inactive" : "active"}">${t.status === "active" ? "Set inactive" : "Set active"}</button></td>`;
    tbody.appendChild(tr);
    const option = document.createElement("option");
    option.value = t.id;
    option.textContent = `${t.tenant_name} (${t.tenant_code})`;
    select.appendChild(option);
  }
  tbody.querySelectorAll("button[data-id]").forEach(btn => btn.addEventListener("click", async () => {
    await apiFetch(`/admin/tenants/${btn.dataset.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: btn.dataset.next, is_active: btn.dataset.next === "active" }) });
    await loadTenants();
    await loadCapacity();
  }));
}

async function loadCapacity() {
  const rows = await apiFetch("/admin/capacity");
  const tbody = document.getElementById("capacityTableBody");
  tbody.innerHTML = rows.map(r => `<tr><td>${escapeHtml(r.tenant_name)} (${escapeHtml(r.tenant_code)})</td><td>${escapeHtml(r.status)}</td><td>${r.companies}</td><td>${r.batches}</td><td>${r.files}</td><td>${r.rows}</td><td>${r.storage_mb}</td></tr>`).join("");
}

async function loadIssues() {
  const issues = await apiFetch("/admin/issues");
  const tenants = await apiFetch("/admin/tenants");
  const lookup = Object.fromEntries(tenants.map(t => [t.id, t.tenant_name]));
  const tbody = document.getElementById("issuesTableBody");
  tbody.innerHTML = issues.map(i => `<tr><td>${i.id}</td><td>${escapeHtml(lookup[i.tenant_id] || i.tenant_id)}</td><td>${escapeHtml(i.title)}</td><td><select data-issue-status="${i.id}"><option ${i.status === "pending" ? "selected" : ""}>pending</option><option ${i.status === "in_progress" ? "selected" : ""}>in_progress</option><option ${i.status === "resolved" ? "selected" : ""}>resolved</option></select></td><td>${escapeHtml(i.priority)}</td><td><input data-issue-resolution="${i.id}" type="text" value="${escapeHtml(i.resolution_notes || "")}" /></td><td><button class="btn btn-secondary" data-save-issue="${i.id}">Save</button></td></tr>`).join("");
  tbody.querySelectorAll("button[data-save-issue]").forEach(btn => btn.addEventListener("click", async () => {
    const id = btn.dataset.saveIssue;
    const status = tbody.querySelector(`[data-issue-status='${id}']`).value;
    const resolution_notes = tbody.querySelector(`[data-issue-resolution='${id}']`).value;
    await apiFetch(`/admin/issues/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, resolution_notes }) });
    await loadIssues();
  }));
}

document.getElementById("tenantForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await apiFetch("/admin/tenants", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tenant_code: tenantCode.value, tenant_name: tenantName.value, contact_name: tenantContactName.value, contact_email: tenantContactEmail.value || null, notes: tenantNotes.value || null }) });
    setMessage("tenantMessage", "Tenant created", "success");
    e.target.reset();
    await loadTenants();
    await loadCapacity();
  } catch (error) { setMessage("tenantMessage", error.message); }
});

document.getElementById("userForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const tenant_ids = Array.from(document.getElementById("userTenantIds").selectedOptions).map(o => o.value);
    await apiFetch("/admin/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ full_name: userFullName.value, email: userEmail.value, password: userPassword.value, role: userRole.value, tenant_ids }) });
    setMessage("userMessage", "User created", "success");
    e.target.reset();
  } catch (error) { setMessage("userMessage", error.message); }
});

document.getElementById("refreshCapacityBtn").addEventListener("click", loadCapacity);
Promise.all([loadTenants(), loadCapacity(), loadIssues()]);
