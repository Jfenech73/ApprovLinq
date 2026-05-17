ensureAuth();

const tenantForm = document.getElementById("tenantForm");
const userForm = document.getElementById("userForm");
const refreshCapacityBtn = document.getElementById("refreshCapacityBtn");
const userRoleSelect = document.getElementById("userRole");
const userTenantIdSelect = document.getElementById("userTenantId");
const refreshAdminRulesBtn = document.getElementById("refreshAdminRulesBtn");

// logoutBtn is injected by ap-ui.js shell — wired there via logoutAndGo
refreshCapacityBtn.addEventListener("click", loadCapacity);
if (refreshAdminRulesBtn) refreshAdminRulesBtn.addEventListener("click", loadAdminRules);
userRoleSelect.addEventListener("change", syncUserTenantSelectState);

tenantForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const status = document.getElementById("tenantStatus").value;
    await apiFetch("/admin/tenants", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_code: document.getElementById("tenantCode").value.trim(),
        tenant_name: document.getElementById("tenantName").value.trim(),
        contact_name: document.getElementById("tenantContactName").value.trim() || null,
        contact_email: document.getElementById("tenantContactEmail").value.trim() || null,
        notes: document.getElementById("tenantNotes").value.trim() || null,
        status,
      }),
    });

    setMessage("tenantMessage", "Tenant created successfully.", "success");
    tenantForm.reset();
    document.getElementById("tenantStatus").value = "active";
    await Promise.all([loadTenants(), loadCapacity()]);
  } catch (error) {
    setMessage("tenantMessage", error.message);
  }
});

userForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const role = userRoleSelect.value;
    const tenantId = userTenantIdSelect.value;

    if (role !== "admin" && !tenantId) {
      throw new Error("Select a tenant for a tenant user.");
    }

    await apiFetch("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        full_name: document.getElementById("userFullName").value.trim(),
        email: document.getElementById("userEmail").value.trim(),
        password: document.getElementById("userPassword").value,
        role,
        is_active: document.getElementById("userIsActive").value === "true",
        tenant_ids: tenantId ? [tenantId] : [],
      }),
    });

    setMessage("userMessage", "User created successfully.", "success");
    userForm.reset();
    document.getElementById("userRole").value = "tenant";
    document.getElementById("userIsActive").value = "true";
    syncUserTenantSelectState();
    await loadUsers();
  } catch (error) {
    setMessage("userMessage", error.message);
  }
});

function syncUserTenantSelectState() {
  const isAdmin = userRoleSelect.value === "admin";
  userTenantIdSelect.disabled = isAdmin;
  if (isAdmin) userTenantIdSelect.value = "";
}

function populateTenantSelect(tenants) {
  userTenantIdSelect.innerHTML = '<option value="">No tenant selected</option>';
  const analyticsTenant = document.getElementById("candidateTenantFilter");
  const currentAnalyticsTenant = analyticsTenant ? analyticsTenant.value : "";
  if (analyticsTenant) analyticsTenant.innerHTML = '<option value="">All tenants</option>';
  for (const tenant of tenants) {
    const option = document.createElement("option");
    option.value = tenant.id;
    option.textContent = `${tenant.tenant_name} (${tenant.tenant_code})`;
    userTenantIdSelect.appendChild(option);
    if (analyticsTenant) {
      const analyticsOption = document.createElement("option");
      analyticsOption.value = tenant.id;
      analyticsOption.textContent = `${tenant.tenant_name} (${tenant.tenant_code})`;
      analyticsTenant.appendChild(analyticsOption);
    }
  }
  if (analyticsTenant && currentAnalyticsTenant) analyticsTenant.value = currentAnalyticsTenant;
  syncUserTenantSelectState();
}

async function loadTenants() {
  try {
    const tenants = await apiFetch("/admin/tenants");
    const tbody = document.getElementById("tenantsTableBody");
    tbody.innerHTML = "";

    if (!tenants.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">No tenants found.</td></tr>';
      populateTenantSelect([]);
      return;
    }

    for (const tenant of tenants) {
      const nextStatus = tenant.status === "active" ? "inactive" : "active";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(tenant.tenant_name)}</td>
        <td>${escapeHtml(tenant.tenant_code)}</td>
        <td>${escapeHtml(tenant.status)}</td>
        <td>${tenant.is_active ? "Yes" : "No"}</td>
        <td>${escapeHtml(tenant.contact_email || tenant.contact_name || "-")}</td>
        <td><button class="btn btn-secondary" data-tenant-id="${tenant.id}" data-next-status="${nextStatus}">${nextStatus === "active" ? "Set Active" : "Set Inactive"}</button></td>
      `;
      tbody.appendChild(tr);
    }

    tbody.querySelectorAll("button[data-tenant-id]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          const nextStatus = button.dataset.nextStatus;
          await apiFetch(`/admin/tenants/${button.dataset.tenantId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              status: nextStatus,
              is_active: nextStatus === "active",
            }),
          });
          await Promise.all([loadTenants(), loadCapacity()]);
        } catch (error) {
          setMessage("pageMessage", error.message);
        }
      });
    });

    populateTenantSelect(tenants);
  } catch (error) {
    setMessage("pageMessage", error.message);
  }
}

async function loadUsers() {
  try {
    const users = await apiFetch("/admin/users");
    const tbody = document.getElementById("usersTableBody");
    tbody.innerHTML = "";

    if (!users.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">No users found.</td></tr>';
      return;
    }

    for (const user of users) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(user.full_name)}</td>
        <td>${escapeHtml(user.email)}</td>
        <td>${escapeHtml(user.role)}</td>
        <td>${user.is_active ? "Yes" : "No"}</td>
        <td>${fmtDate(user.created_at)}</td>
        <td><button class="btn btn-secondary" data-user-id="${user.id}" data-next-active="${user.is_active ? "false" : "true"}">${user.is_active ? "Set Inactive" : "Set Active"}</button></td>
      `;
      tbody.appendChild(tr);
    }

    tbody.querySelectorAll("button[data-user-id]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await apiFetch(`/admin/users/${button.dataset.userId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              is_active: button.dataset.nextActive === "true",
            }),
          });
          await loadUsers();
        } catch (error) {
          setMessage("pageMessage", error.message);
        }
      });
    });
  } catch (error) {
    setMessage("pageMessage", error.message);
  }
}

async function loadCapacity() {
  try {
    const rows = await apiFetch("/admin/capacity");
    const tbody = document.getElementById("capacityTableBody");
    tbody.innerHTML = rows.length
      ? rows.map((row) => `
          <tr>
            <td>${escapeHtml(row.tenant_name)} (${escapeHtml(row.tenant_code)})</td>
            <td>${escapeHtml(row.status)}</td>
            <td>${row.companies}</td>
            <td>${row.batches}</td>
            <td>${row.files}</td>
            <td>${row.rows}</td>
            <td>${row.storage_mb}</td>
          </tr>
        `).join("")
      : '<tr><td colspan="7" class="muted">No capacity data found.</td></tr>';
  } catch (error) {
    setMessage("pageMessage", error.message);
  }
}

function ruleTypeLabel(type) {
  return ({
    supplier_alias: "Supplier alias",
    nominal_remap: "Nominal remap",
    remap_field_value: "Saved region replay",
    text_correction: "Text correction",
  })[type] || type || "-";
}

function fieldLabel(field) {
  return ({
    supplier_name: "Supplier name",
    supplier_posting_account: "Posting account",
    nominal_account_code: "Nominal code",
    invoice_number: "Invoice number",
    invoice_date: "Invoice date",
    description: "Description",
    net_amount: "Net",
    vat_amount: "VAT",
    total_amount: "Total",
    tax_code: "Tax code",
    currency: "Currency",
  })[field] || field || "-";
}

async function loadAdminRules() {
  const tbody = document.getElementById("adminRulesTableBody");
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="8" class="muted">Loading rules…</td></tr>';
  try {
    const rules = await apiFetch("/review/admin/rules");
    tbody.innerHTML = rules.length
      ? rules.map((rule) => {
          const tenant = rule.tenant_name
            ? `${escapeHtml(rule.tenant_name)}${rule.tenant_code ? " (" + escapeHtml(rule.tenant_code) + ")" : ""}`
            : escapeHtml(rule.tenant_id || "-");
          const scope = rule.is_global ? "Global — all tenants" : (rule.company_id ? "Tenant company" : "Tenant all companies");
          const status = `${rule.active ? "active" : "disabled"}${rule.is_global ? " / global" : ""}`;
          const action = rule.is_global
            ? `<button class="btn btn-secondary" data-tenant-rule="${rule.id}">Make tenant-scoped</button>`
            : `<button class="btn btn-secondary" data-global-rule="${rule.id}">Make global</button>`;
          return `
            <tr>
              <td>${tenant}</td>
              <td>${escapeHtml(fieldLabel(rule.field_name))}</td>
              <td>${escapeHtml(ruleTypeLabel(rule.rule_type))}</td>
              <td><code>${escapeHtml(rule.source_pattern)}</code></td>
              <td>${escapeHtml(rule.target_value)}</td>
              <td>${escapeHtml(scope)}</td>
              <td>${escapeHtml(status)}</td>
              <td>${action}</td>
            </tr>
          `;
        }).join("")
      : '<tr><td colspan="8" class="muted">No rules found.</td></tr>';

    tbody.querySelectorAll("button[data-global-rule]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!confirm("Make this a global background rule for all tenants? Only do this for supplier-independent rules.")) return;
        try {
          await apiFetch(`/review/admin/rules/${button.dataset.globalRule}/global`, { method: "POST" });
          await loadAdminRules();
          setMessage("pageMessage", "Rule converted to global.", "success");
        } catch (error) {
          setMessage("pageMessage", error.message);
        }
      });
    });
    tbody.querySelectorAll("button[data-tenant-rule]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await apiFetch(`/review/admin/rules/${button.dataset.tenantRule}/tenant-scoped`, { method: "POST" });
          await loadAdminRules();
          setMessage("pageMessage", "Rule changed back to tenant-scoped.", "success");
        } catch (error) {
          setMessage("pageMessage", error.message);
        }
      });
    });
  } catch (error) {
    tbody.innerHTML = '<tr><td colspan="8" class="muted">Failed to load rules.</td></tr>';
    setMessage("pageMessage", error.message);
  }
}

async function loadIssues() {
  try {
    const [issues, tenants] = await Promise.all([
      apiFetch("/admin/issues"),
      apiFetch("/admin/tenants"),
    ]);

    const tenantLookup = Object.fromEntries(tenants.map((tenant) => [tenant.id, tenant.tenant_name]));
    const tbody = document.getElementById("issuesTableBody");
    tbody.innerHTML = issues.length
      ? issues.map((issue) => `
          <tr>
            <td>${issue.id}</td>
            <td>${escapeHtml(tenantLookup[issue.tenant_id] || issue.tenant_id)}</td>
            <td>${escapeHtml(issue.title)}</td>
            <td>
              <select data-issue-status="${issue.id}">
                <option value="pending" ${issue.status === "pending" ? "selected" : ""}>pending</option>
                <option value="in_progress" ${issue.status === "in_progress" ? "selected" : ""}>in progress</option>
                <option value="resolved" ${issue.status === "resolved" ? "selected" : ""}>resolved</option>
              </select>
            </td>
            <td>${escapeHtml(issue.priority)}</td>
            <td><input type="text" data-issue-resolution="${issue.id}" value="${escapeHtml(issue.resolution_notes || "")}" /></td>
            <td><button class="btn btn-secondary" data-save-issue="${issue.id}">Save</button></td>
          </tr>
        `).join("")
      : '<tr><td colspan="7" class="muted">No issues logged.</td></tr>';

    tbody.querySelectorAll("button[data-save-issue]").forEach((button) => {
      button.addEventListener("click", async () => {
        const issueId = button.dataset.saveIssue;
        const status = tbody.querySelector(`[data-issue-status='${issueId}']`).value;
        const resolution_notes = tbody.querySelector(`[data-issue-resolution='${issueId}']`).value.trim();

        try {
          await apiFetch(`/admin/issues/${issueId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status, resolution_notes }),
          });
          await loadIssues();
        } catch (error) {
          setMessage("pageMessage", error.message);
        }
      });
    });
  } catch (error) {
    setMessage("pageMessage", error.message);
  }
}

async function initAdminPage() {
  try {
    const me = await getSessionInfo();
    if (me.role !== "admin") {
      window.location.href = "/static/tenant.html";
      return;
    }
    await Promise.all([loadTenants(), loadUsers(), loadCapacity(), loadAdminRules(), loadIssues()]);
  } catch (error) {
    setMessage("pageMessage", error.message);
  }
}

initAdminPage();


initPageHelp({
  title: "Platform Admin help",
  subtitle: "This page is used to set up tenants, create users, monitor usage and manage support issues.",
  sections: [
    { heading: "Create tenant", items: ["Create the tenant with a unique tenant code and display name.", "Use Active for paying or approved clients.", "Use Inactive to block operational access without deleting data."] },
    { heading: "Create user", items: ["Create a platform admin or tenant user.", "Tenant users should normally be assigned to one tenant during setup.", "Use a temporary password and ask the client to change it after first login."] },
    { heading: "Tenant and user lists", items: ["Use Set Active or Set Inactive to control access quickly.", "Inactive tenants should not use the scanning tool.", "Inactive users remain stored for audit and reactivation later."] },
    { heading: "Capacity, rules and support", items: ["Capacity shows a high-level view of companies, batches, files, rows and storage by tenant.", "Rule Governance shows tenant rules and lets admins promote safe rules to global background rules.", "Support Tickets are tenant-raised issues only; scan review lines stay on the Review page."] }
  ],
  quickChecks: ["Create the tenant before the first tenant user.", "Keep tenant codes short and unique.", "Set inactive immediately if a client should no longer use the service."]
});

function candidateFieldLabel(field) {
  return ({
    supplier_name: "Supplier name",
    supplier_posting_account: "Posting account",
    nominal_account_code: "Nominal code",
    invoice_number: "Invoice number",
    invoice_date: "Invoice date",
    description: "Description",
    net_amount: "Net",
    vat_amount: "VAT",
    total_amount: "Total",
    tax_code: "Tax code",
    currency: "Currency",
  })[field] || field || "-";
}

function candidateSourceLabel(source) {
  return ({
    raw_extraction: "Raw extraction",
    correction_rule: "Correction rule",
    saved_region: "Saved region",
    supplier_history: "Supplier history",
    accepted_correction: "Accepted correction",
    supplier_master: "Supplier master",
    nominal_master: "Nominal master",
    totals_reconciliation: "Totals reconciliation",
    admin_global_rule: "Admin global rule",
  })[source] || source || "-";
}

function populateCandidateAnalyticsFilters(data) {
  const fieldSelect = document.getElementById("candidateFieldFilter");
  const sourceSelect = document.getElementById("candidateSourceFilter");
  if (fieldSelect && fieldSelect.options.length <= 1) {
    const fields = (data.by_field || []).map((row) => row.label).filter(Boolean).sort();
    fieldSelect.insertAdjacentHTML("beforeend", fields.map((f) => `<option value="${escapeHtml(f)}">${escapeHtml(candidateFieldLabel(f))}</option>`).join(""));
  }
  if (sourceSelect && sourceSelect.options.length <= 1) {
    const sources = (data.by_source_type || []).map((row) => row.label).filter(Boolean).sort();
    sourceSelect.insertAdjacentHTML("beforeend", sources.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(candidateSourceLabel(s))}</option>`).join(""));
  }
}

function renderAnalyticsRows(rows, type) {
  if (!rows || !rows.length) {
    const span = type === "source" ? 8 : type === "field" ? 7 : 6;
    return `<tr><td colspan="${span}" class="muted">No analytics data found.</td></tr>`;
  }
  return rows.map((row) => {
    if (type === "source") {
      return `<tr><td>${escapeHtml(candidateSourceLabel(row.label))}</td><td>${row.candidate_count}</td><td>${row.selected_count}</td><td>${row.applied_count}</td><td>${row.accepted_count}</td><td>${row.corrected_count}</td><td>${row.conflict_count}</td><td>${row.accuracy}%</td></tr>`;
    }
    if (type === "field") {
      return `<tr><td>${escapeHtml(candidateFieldLabel(row.label))}</td><td>${row.candidate_count}</td><td>${row.selected_count}</td><td>${row.accepted_count}</td><td>${row.corrected_count}</td><td>${row.conflict_count}</td><td>${row.accuracy}%</td></tr>`;
    }
    return `<tr><td>${escapeHtml(row.label)}</td><td>${row.candidate_count}</td><td>${row.accepted_count}</td><td>${row.corrected_count}</td><td>${row.conflict_count}</td><td>${row.correction_rate}%</td></tr>`;
  }).join("");
}

async function loadCandidateAnalytics() {
  const summaryEl = document.getElementById("candidateAnalyticsSummary");
  const sourceBody = document.getElementById("candidateSourceTableBody");
  const fieldBody = document.getElementById("candidateFieldTableBody");
  const supplierBody = document.getElementById("candidateSupplierTableBody");
  if (!summaryEl || !sourceBody || !fieldBody || !supplierBody) return;
  summaryEl.textContent = "Loading candidate analytics…";
  try {
    const params = new URLSearchParams();
    const tenantFilter = document.getElementById("candidateTenantFilter")?.value || "";
    const fieldFilter = document.getElementById("candidateFieldFilter")?.value || "";
    const sourceFilter = document.getElementById("candidateSourceFilter")?.value || "";
    if (tenantFilter) params.set("tenant_id", tenantFilter);
    if (fieldFilter) params.set("field_name", fieldFilter);
    if (sourceFilter) params.set("source_type", sourceFilter);
    const data = await apiFetch(`/review/candidate-analytics${params.toString() ? "?" + params.toString() : ""}`);
    populateCandidateAnalyticsFilters(data);
    const s = data.summary || {};
    summaryEl.textContent = `Candidates: ${s.candidate_count || 0}. Selected: ${s.selected_count || 0}. Accepted: ${s.accepted_count || 0}. Corrected: ${s.corrected_count || 0}. Conflicts: ${s.conflict_count || 0}. Accuracy on labelled candidates: ${s.accuracy || 0}%.`;
    sourceBody.innerHTML = renderAnalyticsRows(data.by_source_type || [], "source");
    fieldBody.innerHTML = renderAnalyticsRows(data.by_field || [], "field");
    supplierBody.innerHTML = renderAnalyticsRows(data.top_corrected_suppliers || [], "supplier");
  } catch (error) {
    summaryEl.textContent = "Failed to load candidate analytics.";
    setMessage("pageMessage", error.message);
  }
}

(function wireCandidateAnalytics() {
  const refresh = document.getElementById("refreshCandidateAnalyticsBtn");
  if (refresh) refresh.addEventListener("click", loadCandidateAnalytics);
  const tenantFilter = document.getElementById("candidateTenantFilter");
  if (tenantFilter) tenantFilter.addEventListener("change", loadCandidateAnalytics);
  const fieldFilter = document.getElementById("candidateFieldFilter");
  if (fieldFilter) fieldFilter.addEventListener("change", loadCandidateAnalytics);
  const sourceFilter = document.getElementById("candidateSourceFilter");
  if (sourceFilter) sourceFilter.addEventListener("change", loadCandidateAnalytics);
})();
setTimeout(loadCandidateAnalytics, 0);
