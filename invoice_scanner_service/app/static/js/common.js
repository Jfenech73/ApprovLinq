function getToken() { return localStorage.getItem("approvlinq_token") || ""; }
function getTenantId() { return localStorage.getItem("approvlinq_tenant_id") || ""; }
function setTenantId(id) { localStorage.setItem("approvlinq_tenant_id", id); }
function logoutAndGo() { localStorage.removeItem("approvlinq_token"); localStorage.removeItem("approvlinq_tenant_id"); window.location.href = "/"; }
function authHeaders(extra = {}) {
  const token = getToken();
  const tenantId = getTenantId();
  const headers = { ...extra };
  if (token) headers["Authorization"] = `Bearer ${token}`;
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
      if (data?.detail) message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch (_) {}
    throw new Error(message);
  }
  const ct = response.headers.get("content-type") || "";
  if (ct.includes("application/json")) return response.json();
  return response;
}
function setText(id, text) { const el = document.getElementById(id); if (el) el.textContent = text || ""; }
function setMessage(id, text, kind = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = `message ${kind}`.trim();
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
}
function fmtDate(v) { if (!v) return "-"; return new Date(v).toLocaleString(); }
function ensureAuth() { if (!getToken()) window.location.href = "/"; }
