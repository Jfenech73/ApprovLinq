/* rules.js — Rules management page
 *
 * API surface used:
 *   GET    /review/rules?company_id=&active_only=
 *   PATCH  /review/rules/{id}          { source_pattern, target_value, active }
 *   POST   /review/rules/{id}/enable
 *   POST   /review/rules/{id}/disable
 *   DELETE /review/rules/{id}
 *
 * Relies on common.js for:  authHeaders(), api(), $(id), showToast() if present
 */

"use strict";

// ── State ─────────────────────────────────────────────────────────────────────
let _allRules = [];         // raw array from the last successful fetch
let _editingId = null;      // rule id currently open in the edit modal
let _companies = [];        // [{id, name}] for the company filter

// ── DOM shortcuts ─────────────────────────────────────────────────────────────
const $tbody     = () => document.getElementById("rulesTableBody");
const $count     = () => document.getElementById("ruleCount");
const $msg       = () => document.getElementById("pageMessage");
const $editModal = () => document.getElementById("editModal");
const $editSrc   = () => document.getElementById("editSource");
const $editTgt   = () => document.getElementById("editTarget");
const $editMsg   = () => document.getElementById("editMessage");

// ── Helpers ───────────────────────────────────────────────────────────────────
function setMsg(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.className = "message" + (kind ? " " + kind : "");
}

function fmtDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
  } catch { return iso.slice(0, 10); }
}

function typeLabel(rule) {
  if (rule.rule_type === "supplier_alias") return "Supplier alias";
  if (rule.rule_type === "nominal_remap")  return "Nominal remap";
  return rule.rule_type || "—";
}

function fieldLabel(rule) {
  const map = { supplier_name: "supplier_name", nominal_account_code: "nominal code" };
  return map[rule.field_name] || rule.field_name || "—";
}

function escHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Fetch & render ────────────────────────────────────────────────────────────
async function loadCompanies() {
  try {
    const companies = await api("/tenant/companies");
    _companies = (companies || []).map(c => ({ id: c.id, name: c.company_name || c.company_code }));
    const sel = document.getElementById("companyFilter");
    _companies.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
  } catch (_) { /* non-fatal — company filter just stays blank */ }
}

async function loadRules() {
  setMsg($msg(), "");
  $tbody().innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">Loading…</td></tr>';

  const companyId = document.getElementById("companyFilter").value;
  let url = "/review/rules";
  if (companyId) url += "?company_id=" + encodeURIComponent(companyId);

  try {
    _allRules = await api(url);
    renderTable();
  } catch (e) {
    setMsg($msg(), "Failed to load rules: " + e.message, "error");
    $tbody().innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center">Error loading rules.</td></tr>';
  }
}

function renderTable() {
  const typeFilter   = document.getElementById("typeFilter").value;
  const statusFilter = document.getElementById("statusFilter").value;
  const search       = document.getElementById("searchInput").value.trim().toLowerCase();

  let rows = _allRules.filter(r => {
    if (typeFilter   && r.rule_type !== typeFilter)              return false;
    if (statusFilter === "active"   && !r.active)               return false;
    if (statusFilter === "disabled" && r.active)                return false;
    if (search && !r.source_pattern.includes(search) && !r.target_value.toLowerCase().includes(search)) return false;
    return true;
  });

  $count().textContent = rows.length + " rule" + (rows.length !== 1 ? "s" : "");

  if (!rows.length) {
    $tbody().innerHTML = '<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">No rules match the current filters.</td></tr>';
    return;
  }

  const companyName = id => {
    const c = _companies.find(c => c.id === id);
    return c ? c.name : (id ? id.slice(0, 8) + "…" : "All companies");
  };

  $tbody().innerHTML = rows.map(r => {
    const statusPill = r.active
      ? '<span class="pill ok" style="font-size:11px">active</span>'
      : '<span class="pill" style="font-size:11px;background:var(--ap-bg-sub)">disabled</span>';

    const toggleBtn = r.active
      ? `<button class="btn btn-sm" data-action="disable" data-id="${r.id}" style="color:var(--ap-warn-fg)">Disable</button>`
      : `<button class="btn btn-sm" data-action="enable"  data-id="${r.id}" style="color:var(--ap-ok-fg)">Enable</button>`;

    return `<tr data-id="${r.id}" class="${r.active ? "" : "muted"}">
      <td>
        <strong style="font-size:var(--ap-fs-13)">${escHtml(typeLabel(r))}</strong>
        <br><span class="muted" style="font-size:11px">${escHtml(fieldLabel(r))}</span>
      </td>
      <td><code style="font-size:var(--ap-fs-12)">${escHtml(r.source_pattern)}</code></td>
      <td><strong>${escHtml(r.target_value)}</strong></td>
      <td><span class="muted" style="font-size:12px">${escHtml(companyName(r.company_id))}</span></td>
      <td>${statusPill}</td>
      <td><span class="muted" style="font-size:12px">${fmtDate(r.created_at)}</span></td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn btn-sm" data-action="edit"   data-id="${r.id}">Edit</button>
        ${toggleBtn}
        <button class="btn btn-sm" data-action="delete" data-id="${r.id}"
                style="color:var(--ap-err-fg);border-color:var(--ap-err-fg)">Delete</button>
      </td>
    </tr>`;
  }).join("");
}

// ── Table action delegation ───────────────────────────────────────────────────
document.getElementById("rulesTable").addEventListener("click", async e => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const id     = parseInt(btn.dataset.id, 10);
  const action = btn.dataset.action;

  if (action === "edit") {
    openEditModal(id);
    return;
  }

  if (action === "delete") {
    const rule = _allRules.find(r => r.id === id);
    if (!rule) return;
    if (!confirm(`Delete rule "${rule.source_pattern}" → "${rule.target_value}"?\n\nThis cannot be undone.`)) return;
    try {
      await api(`/review/rules/${id}`, { method: "DELETE" });
      _allRules = _allRules.filter(r => r.id !== id);
      renderTable();
      setMsg($msg(), "Rule deleted.", "success");
    } catch (err) {
      setMsg($msg(), "Delete failed: " + err.message, "error");
    }
    return;
  }

  if (action === "enable" || action === "disable") {
    const endpoint = `/review/rules/${id}/${action}`;
    try {
      const updated = await api(endpoint, { method: "POST" });
      const idx = _allRules.findIndex(r => r.id === id);
      if (idx >= 0) _allRules[idx] = updated;
      renderTable();
      setMsg($msg(), `Rule ${action}d.`, "success");
    } catch (err) {
      setMsg($msg(), `Failed to ${action} rule: ` + err.message, "error");
    }
  }
});

// ── Edit modal ────────────────────────────────────────────────────────────────
function openEditModal(id) {
  const rule = _allRules.find(r => r.id === id);
  if (!rule) return;
  _editingId = id;
  $editSrc().value = rule.source_pattern || "";
  $editTgt().value = rule.target_value   || "";
  setMsg($editMsg(), "");
  $editModal().style.display = "flex";
  $editSrc().focus();
}

function closeEditModal() {
  $editModal().style.display = "none";
  _editingId = null;
}

document.getElementById("editCancelBtn").addEventListener("click", closeEditModal);

// Close on backdrop click
$editModal().addEventListener("click", e => { if (e.target === $editModal()) closeEditModal(); });

// Keyboard: Escape closes
document.addEventListener("keydown", e => { if (e.key === "Escape") closeEditModal(); });

document.getElementById("editSaveBtn").addEventListener("click", async () => {
  if (!_editingId) return;
  const newSrc = $editSrc().value.trim();
  const newTgt = $editTgt().value.trim();
  setMsg($editMsg(), "");

  if (!newSrc) { setMsg($editMsg(), "Source pattern cannot be blank.", "error"); return; }
  if (!newTgt) { setMsg($editMsg(), "Target value cannot be blank.",   "error"); return; }
  if (newSrc.toLowerCase() === newTgt.toLowerCase()) {
    setMsg($editMsg(), "Source and target are identical — rule would have no effect.", "error");
    return;
  }

  document.getElementById("editSaveBtn").disabled = true;
  try {
    const updated = await api(`/review/rules/${_editingId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_pattern: newSrc, target_value: newTgt }),
    });
    const idx = _allRules.findIndex(r => r.id === _editingId);
    if (idx >= 0) _allRules[idx] = updated;
    closeEditModal();
    renderTable();
    setMsg($msg(), "Rule updated.", "success");
  } catch (err) {
    setMsg($editMsg(), "Save failed: " + (err.message || "unknown error"), "error");
  } finally {
    document.getElementById("editSaveBtn").disabled = false;
  }
});

// ── Filter wiring ─────────────────────────────────────────────────────────────
["companyFilter", "typeFilter", "statusFilter"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => {
    if (id === "companyFilter") loadRules(); else renderTable();
  });
});
document.getElementById("searchInput").addEventListener("input", renderTable);
document.getElementById("refreshBtn").addEventListener("click", loadRules);

// ── Initialise ────────────────────────────────────────────────────────────────
(async function init() {
  await loadCompanies();
  await loadRules();
})();
