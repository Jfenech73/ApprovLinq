/* rules.js — Rules management page
 *
 * Defines its own api() wrapper over apiFetch() (from common.js) so it does
 * not depend on app.js being present on this page.
 */
"use strict";

// ── Local api() — apiFetch is provided by common.js ──────────────────────────
async function api(path, options = {}) {
  return apiFetch(path, options);
}

// ── State ─────────────────────────────────────────────────────────────────────
let _allRules  = [];
let _editingId = null;
let _companies = [];

// ── Helpers ───────────────────────────────────────────────────────────────────
function setMsg(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.className   = "message" + (kind ? " " + kind : "");
}

function fmtDate(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" }); }
  catch { return iso.slice(0, 10); }
}

function fieldLabel(fn) {
  return ({ supplier_name: "Supplier name", nominal_account_code: "Nominal code",
            supplier_posting_account: "Posting account", invoice_number: "Invoice number",
            invoice_date: "Invoice date", description: "Description",
            tax_code: "Tax code", currency: "Currency" })[fn] || (fn || "—");
}

function typeLabel(rt) {
  return ({ supplier_alias: "Supplier alias", nominal_remap: "Nominal remap" })[rt] || (rt || "");
}

function escHtml(s) {
  return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;")
                        .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ── Data loading ──────────────────────────────────────────────────────────────
async function loadCompanies() {
  try {
    const list = await api("/tenant/companies");
    _companies = (list || []).map(c => ({ id: c.id, name: c.company_name || c.company_code }));
    const sel  = document.getElementById("companyFilter");
    _companies.forEach(c => {
      const o = document.createElement("option");
      o.value = c.id; o.textContent = c.name; sel.appendChild(o);

      // Also populate the edit modal company picker
      const o2 = document.createElement("option");
      o2.value = c.id; o2.textContent = c.name;
      document.getElementById("editCompany").appendChild(o2);
    });
  } catch (_) {}
}

async function loadRules() {
  setMsg(document.getElementById("pageMessage"), "");
  document.getElementById("rulesTableBody").innerHTML =
    '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">Loading…</td></tr>';

  const cid = document.getElementById("companyFilter").value;
  const url = "/review/rules" + (cid ? "?company_id=" + encodeURIComponent(cid) : "");

  try {
    _allRules = await api(url);
    renderTable();
  } catch (e) {
    setMsg(document.getElementById("pageMessage"),
      "Failed to load rules: " + (e.message || e), "error");
    document.getElementById("rulesTableBody").innerHTML =
      '<tr><td colspan="6" class="muted" style="text-align:center">Error loading rules.</td></tr>';
  }
}

// ── Render ────────────────────────────────────────────────────────────────────
function renderTable() {
  const sf  = document.getElementById("statusFilter").value;
  const q   = document.getElementById("searchInput").value.trim().toLowerCase();

  const rows = _allRules.filter(r => {
    if (sf === "active"   && !r.active) return false;
    if (sf === "disabled" &&  r.active) return false;
    if (q && !((r.source_pattern + " " + r.target_value + " " + (r.field_name||"")).toLowerCase().includes(q))) return false;
    return true;
  });

  document.getElementById("ruleCount").textContent = rows.length + " rule" + (rows.length !== 1 ? "s" : "");

  if (!rows.length) {
    document.getElementById("rulesTableBody").innerHTML =
      '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">No rules match the current filters.</td></tr>';
    return;
  }

  const coName = id => (_companies.find(c => c.id === id) || {}).name || (id ? "Company" : "All companies");

  document.getElementById("rulesTableBody").innerHTML = rows.map(r => {
    const pill  = r.active
      ? '<span class="pill ok" style="font-size:11px">active</span>'
      : '<span class="pill"    style="font-size:11px;background:var(--ap-bg-sub)">disabled</span>';
    const tog   = r.active
      ? `<button class="btn btn-sm" data-action="disable" data-id="${r.id}" style="color:var(--ap-warn-fg)">Disable</button>`
      : `<button class="btn btn-sm" data-action="enable"  data-id="${r.id}" style="color:var(--ap-ok-fg)">Enable</button>`;
    return `<tr data-id="${r.id}" class="${r.active?"":"muted"}">
      <td>
        <strong style="font-size:var(--ap-fs-13)">${escHtml(fieldLabel(r.field_name))}</strong>
        ${r.rule_type ? `<br><span class="muted" style="font-size:11px">${escHtml(typeLabel(r.rule_type))}</span>` : ""}
      </td>
      <td><code style="font-size:var(--ap-fs-12)">${escHtml(r.source_pattern)}</code></td>
      <td><strong>${escHtml(r.target_value)}</strong></td>
      <td><span class="muted" style="font-size:12px">${escHtml(coName(r.company_id))}</span></td>
      <td>${pill}</td>
      <td><span class="muted" style="font-size:12px">${fmtDate(r.created_at)}</span></td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn btn-sm" data-action="edit"   data-id="${r.id}">Edit</button>
        ${tog}
        <button class="btn btn-sm" data-action="delete" data-id="${r.id}"
                style="color:var(--ap-err-fg);border-color:var(--ap-err-fg)">Delete</button>
      </td>
    </tr>`;
  }).join("");
}

// ── Table actions ─────────────────────────────────────────────────────────────
document.getElementById("rulesTable").addEventListener("click", async e => {
  const btn    = e.target.closest("[data-action]");
  if (!btn) return;
  const id     = parseInt(btn.dataset.id, 10);
  const action = btn.dataset.action;

  if (action === "edit") { openEditModal(id); return; }

  if (action === "delete") {
    const rule = _allRules.find(r => r.id === id);
    if (!confirm(`Delete rule:\n  "${rule.source_pattern}"  →  "${rule.target_value}"\n\nThis cannot be undone.`)) return;
    try {
      await api(`/review/rules/${id}`, { method: "DELETE" });
      _allRules = _allRules.filter(r => r.id !== id);
      renderTable();
      setMsg(document.getElementById("pageMessage"), "Rule deleted.", "success");
    } catch (err) {
      setMsg(document.getElementById("pageMessage"), "Delete failed: " + (err.message||err), "error");
    }
    return;
  }

  if (action === "enable" || action === "disable") {
    try {
      const updated = await api(`/review/rules/${id}/${action}`, { method: "POST" });
      const idx = _allRules.findIndex(r => r.id === id);
      if (idx >= 0) _allRules[idx] = updated;
      renderTable();
      setMsg(document.getElementById("pageMessage"), `Rule ${action}d.`, "success");
    } catch (err) {
      setMsg(document.getElementById("pageMessage"), `Failed to ${action}: ` + (err.message||err), "error");
    }
  }
});

// ── Edit modal ────────────────────────────────────────────────────────────────
function openEditModal(id) {
  const rule = _allRules.find(r => r.id === id);
  if (!rule) return;
  _editingId = id;
  document.getElementById("editSource").value = rule.source_pattern || "";
  document.getElementById("editTarget").value = rule.target_value   || "";
  document.getElementById("editField").textContent =
    fieldLabel(rule.field_name) + (rule.rule_type ? " · " + typeLabel(rule.rule_type) : "");

  // Populate scope controls
  const appliesTo = rule.applies_to || (rule.company_id ? "this_company" : "all_companies");
  document.getElementById("editAppliesTo").value = appliesTo;
  const showCo = appliesTo === "this_company";
  document.getElementById("editCompanyRow").style.display = showCo ? "" : "none";
  if (rule.company_id) {
    document.getElementById("editCompany").value = rule.company_id;
  }

  setMsg(document.getElementById("editMessage"), "");
  document.getElementById("editModal").style.display = "flex";
  document.getElementById("editSource").focus();
}

function closeEditModal() {
  document.getElementById("editModal").style.display = "none";
  _editingId = null;
}

// Show/hide the company picker based on "Applies to" selection
document.getElementById("editAppliesTo").addEventListener("change", () => {
  const show = document.getElementById("editAppliesTo").value === "this_company";
  document.getElementById("editCompanyRow").style.display = show ? "" : "none";
});

document.getElementById("editCancelBtn").addEventListener("click", closeEditModal);
document.getElementById("editModal").addEventListener("click", e => {
  if (e.target === document.getElementById("editModal")) closeEditModal();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeEditModal(); });

document.getElementById("editSaveBtn").addEventListener("click", async () => {
  if (!_editingId) return;
  const src = document.getElementById("editSource").value.trim();
  const tgt = document.getElementById("editTarget").value.trim();
  setMsg(document.getElementById("editMessage"), "");
  if (!src) { setMsg(document.getElementById("editMessage"), "Source pattern cannot be blank.", "error"); return; }
  if (!tgt) { setMsg(document.getElementById("editMessage"), "Target value cannot be blank.",   "error"); return; }
  if (src.toLowerCase() === tgt.toLowerCase()) {
    setMsg(document.getElementById("editMessage"), "Source and target are identical — no effect.", "error"); return;
  }
  const btn = document.getElementById("editSaveBtn");
  btn.disabled = true;
  try {
    const appliesTo = document.getElementById("editAppliesTo").value;
    const companyId  = appliesTo === "this_company"
      ? document.getElementById("editCompany").value || null
      : null;
    if (appliesTo === "this_company" && !companyId) {
      setMsg(document.getElementById("editMessage"), "Please select a company.", "error");
      btn.disabled = false;
      return;
    }
    const updated = await api(`/review/rules/${_editingId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_pattern: src, target_value: tgt, applies_to: appliesTo, company_id: companyId }),
    });
    const idx = _allRules.findIndex(r => r.id === _editingId);
    if (idx >= 0) _allRules[idx] = updated;
    closeEditModal();
    renderTable();
    setMsg(document.getElementById("pageMessage"), "Rule updated.", "success");
  } catch (err) {
    setMsg(document.getElementById("editMessage"), "Save failed: " + (err.message || "unknown error"), "error");
  } finally { btn.disabled = false; }
});

// ── Filters ───────────────────────────────────────────────────────────────────
document.getElementById("companyFilter").addEventListener("change", loadRules);
document.getElementById("statusFilter" ).addEventListener("change", renderTable);
document.getElementById("searchInput"  ).addEventListener("input",  renderTable);
document.getElementById("refreshBtn"   ).addEventListener("click",  loadRules);

// ── Init ──────────────────────────────────────────────────────────────────────
(async () => { await loadCompanies(); await loadRules(); })();
