let approvlinqSessionCache = null;

function getToken() {
  return localStorage.getItem("approvlinq_token") || "";
}

function getTenantId() {
  return localStorage.getItem("approvlinq_tenant_id") || "";
}

function setTenantId(id) {
  if (id) {
    localStorage.setItem("approvlinq_tenant_id", id);
  } else {
    localStorage.removeItem("approvlinq_tenant_id");
  }
}

function logoutAndGo() {
  localStorage.removeItem("approvlinq_token");
  localStorage.removeItem("approvlinq_tenant_id");
  window.location.href = "/";
}

function authHeaders(extra = {}) {
  const token = getToken();
  const tenantId = getTenantId();
  const headers = { ...extra };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (tenantId) headers["X-Tenant-Id"] = tenantId;
  return headers;
}

async function apiFetch(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data?.detail) {
        message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      }
    } catch (_) {}
    throw new Error(message);
  }

  const ct = response.headers.get("content-type") || "";
  if (ct.includes("application/json")) return response.json();
  return response;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text || "";
}

function setMessage(id, text, kind = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = `message ${kind}`.trim();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (m) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[m]));
}

function fmtDate(v) {
  if (!v) return "-";
  return new Date(v).toLocaleString();
}

function ensureAuth() {
  if (!getToken()) {
    window.location.href = "/";
    return false;
  }
  return true;
}

async function getSessionInfo(force = false) {
  if (approvlinqSessionCache && !force) return approvlinqSessionCache;
  approvlinqSessionCache = await apiFetch("/auth/me");
  return approvlinqSessionCache;
}

function normalizeTenantRow(row) {
  return {
    tenant_id: row.tenant_id || row.id,
    tenant_name: row.tenant_name,
    tenant_code: row.tenant_code,
    tenant_role: row.tenant_role || "",
    is_default: Boolean(row.is_default),
    status: row.status || (row.is_active === false ? "inactive" : "active"),
    is_active: row.is_active !== false,
  };
}

async function getAvailableTenants() {
  const me = await getSessionInfo();
  let tenants = (me.tenants || []).map(normalizeTenantRow);

  if (me.role === "admin" && !tenants.length) {
    tenants = (await apiFetch("/admin/tenants")).map(normalizeTenantRow);
  }

  return tenants;
}

async function populateTenantSelector(selectorId, options = {}) {
  const { includePlaceholder = false } = options;
  const select = document.getElementById(selectorId);
  if (!select) return [];

  const tenants = await getAvailableTenants();
  const currentTenantId = getTenantId();
  const selectedTenant = tenants.find((t) => t.tenant_id === currentTenantId) || tenants.find((t) => t.is_default) || tenants[0] || null;

  if (selectedTenant) setTenantId(selectedTenant.tenant_id);

  select.innerHTML = "";
  if (includePlaceholder) {
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = tenants.length ? "Select tenant" : "No tenants available";
    select.appendChild(placeholder);
  }

  for (const tenant of tenants) {
    const option = document.createElement("option");
    option.value = tenant.tenant_id;
    option.textContent = `${tenant.tenant_name} (${tenant.tenant_code})`;
    if (selectedTenant && tenant.tenant_id === selectedTenant.tenant_id) {
      option.selected = true;
    }
    select.appendChild(option);
  }

  return tenants;
}
