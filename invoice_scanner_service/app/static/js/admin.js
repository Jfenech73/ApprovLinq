ensureAuth();

const logoutBtn = document.getElementById("logoutBtn");
const tenantForm = document.getElementById("tenantForm");
const userForm = document.getElementById("userForm");
const refreshCapacityBtn = document.getElementById("refreshCapacityBtn");
const userRoleSelect = document.getElementById("userRole");
const userTenantIdSelect = document.getElementById("userTenantId");

logoutBtn.addEventListener("click", logoutAndGo);
refreshCapacityBtn.addEventListener("click", loadCapacity);
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
  for (const tenant of tenants) {
    const option = document.createElement("option");
    option.value = tenant.id;
    option.textContent = `${tenant.tenant_name} (${tenant.tenant_code})`;
    userTenantIdSelect.appendChild(option);
  }
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
    await Promise.all([loadTenants(), loadUsers(), loadCapacity(), loadIssues()]);
  } catch (error) {
    setMessage("pageMessage", error.message);
  }
}

initAdminPage();
