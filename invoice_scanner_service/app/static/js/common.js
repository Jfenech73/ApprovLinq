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
    let message = "Something went wrong on the server. Please refresh the page or try again.";

    try {
      const data = await response.json();

      if (response.status === 401) {
        message = "Your session has expired. Please log in again.";
      } else if (response.status === 403) {
        message = "You do not have permission to use this feature.";
      } else if (response.status === 404) {
        message = "The requested item could not be found.";
      } else if (response.status >= 500) {
        message = "Something went wrong on the server. Please refresh the page or try again.";
      } else if (data?.detail) {
        if (typeof data.detail === "string") {
          message = data.detail;
        } else {
          message = "The request could not be completed.";
        }
      }
    } catch (_) {
      if (response.status >= 500) {
        message = "Something went wrong on the server. Please refresh the page or try again.";
      }
    }

    const error = new Error(message);
    error.status = response.status;
    throw error;
  }

  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text || "";
}

function normalizeUiErrorMessage(message) {
  if (!message) return "";
  const text = String(message).trim();

  if (text === "500" || text.startsWith("500 ") || text.includes("Internal Server Error")) {
    return "Something went wrong on the server. Please refresh the page or try again.";
  }

  if (text === "401" || text.startsWith("401 ")) {
    return "Your session has expired. Please log in again.";
  }

  if (text === "403" || text.startsWith("403 ")) {
    return "You do not have permission to use this feature.";
  }

  return text;
}

function setMessage(id, text, kind = "") {
  const el = document.getElementById(id);
  if (!el) return;

  const clean = normalizeUiErrorMessage(text);
  el.textContent = clean || "";
  el.className = `message ${kind || (clean && clean.includes("server") ? "server-error" : "")}`.trim();
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


function closeHelpModal() {
  const overlay = document.getElementById("helpModalOverlay");
  if (overlay) overlay.remove();
}

function renderHelpCard(section) {
  const bullets = (section.items || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `
    <section class="help-card">
      <h3>${escapeHtml(section.heading || "")}</h3>
      ${section.body ? `<p>${escapeHtml(section.body)}</p>` : ""}
      ${bullets ? `<ul>${bullets}</ul>` : ""}
    </section>
  `;
}

function openHelpModal(config) {
  closeHelpModal();
  const overlay = document.createElement("div");
  overlay.id = "helpModalOverlay";
  overlay.className = "help-modal-overlay";
  const sections = (config.sections || []).map(renderHelpCard).join("");
  const checklist = (config.quickChecks || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  overlay.innerHTML = `
    <div class="help-modal" role="dialog" aria-modal="true" aria-labelledby="helpModalTitle">
      <div class="help-modal-header">
        <div>
          <h2 id="helpModalTitle" class="help-modal-title">${escapeHtml(config.title || "Help")}</h2>
          ${config.subtitle ? `<p class="help-modal-subtitle">${escapeHtml(config.subtitle)}</p>` : ""}
        </div>
        <button class="help-modal-close" type="button" aria-label="Close help">×</button>
      </div>
      <div class="help-modal-body">
        <div class="help-grid">${sections}</div>
        ${checklist ? `<div class="help-callout"><strong>Quick checks before you continue</strong><ul class="help-checklist">${checklist}</ul></div>` : ""}
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeHelpModal();
  });
  overlay.querySelector(".help-modal-close").addEventListener("click", closeHelpModal);
  document.addEventListener("keydown", helpModalEscHandler, { once: true });
}

function helpModalEscHandler(event) {
  if (event.key === "Escape") closeHelpModal();
}

function initPageHelp(config) {
  const btn = document.getElementById("pageHelpBtn") || document.getElementById("helpBtn");
  if (!btn) return;
  btn.onclick = () => openHelpModal(config);
}
