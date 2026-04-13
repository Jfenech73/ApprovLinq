ensureAuth();

// ─── Admin-only access gate ─────────────────────────────────────────────────
// Export templates are a platform feature; tenant users should not reach this
// page. Check role before any DOM wiring so a non-admin is bounced early.
(async function enforceAdminOnly() {
  try {
    const me = await apiFetch("/auth/me");
    if (me && me.role !== "admin") {
      window.location.href = "/static/tenant.html";
    }
  } catch {
    window.location.href = "/static/login.html";
  }
})();

const logoutBtn          = document.getElementById("logoutBtn");
const templateForm       = document.getElementById("templateForm");
const assignmentForm     = document.getElementById("assignmentForm");
const addColumnBtn       = document.getElementById("addColumnBtn");
const previewBtn         = document.getElementById("previewBtn");
const saveNewColBtn      = document.getElementById("saveNewColBtn");
const cancelNewColBtn    = document.getElementById("cancelNewColBtn");
const closeEditorBtn     = document.getElementById("closeEditorBtn");
const applyFilterBtn     = document.getElementById("applyFilterBtn");
const refreshTemplatesBtn = document.getElementById("refreshTemplatesBtn");
const refreshAuditBtn    = document.getElementById("refreshAuditBtn");
const tplCancelBtn       = document.getElementById("tplCancelBtn");

let _editingTemplateId   = null;
let _availableFields     = [];
let _allTemplates        = [];
let _allTenants          = [];
let _allAssignments      = [];
let _currentCompanies    = [];

logoutBtn.addEventListener("click", logoutAndGo);

// ── Init ────────────────────────────────────────────────────────────────────

(async function init() {
  try {
    await Promise.all([
      loadAvailableFields(),
      loadTemplates(),
      loadTenantsForAssignment(),
      loadAssignments(),
      loadAudit(),
    ]);
  } catch (err) {
    setMessage("pageMessage", err.message);
  }
})();

// ── Field Catalog ────────────────────────────────────────────────────────────

async function loadAvailableFields() {
  _availableFields = await apiFetch("/admin/export-templates/fields");
  const sel = document.getElementById("newColSourceField");
  sel.innerHTML = '<option value="">— select field —</option>';
  for (const f of _availableFields) {
    const opt = document.createElement("option");
    opt.value = f;
    opt.textContent = f;
    sel.appendChild(opt);
  }
}

// ── Template CRUD ────────────────────────────────────────────────────────────

async function loadTemplates(params = {}) {
  const qs = new URLSearchParams();
  if (params.search)           qs.set("search", params.search);
  if (params.is_active != null) qs.set("is_active", params.is_active);
  if (params.accounting_system) qs.set("accounting_system", params.accounting_system);
  const url = "/admin/export-templates" + (qs.toString() ? "?" + qs.toString() : "");
  _allTemplates = await apiFetch(url);
  renderTemplatesTable(_allTemplates);
  refreshAssignmentTemplateSelect();
}

function renderTemplatesTable(list) {
  const tbody = document.getElementById("templatesTableBody");
  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">No templates found.</td></tr>';
    return;
  }
  tbody.innerHTML = list.map((t) => `
    <tr>
      <td><strong>${escapeHtml(t.name)}</strong>${t.is_system_default ? ' <span class="tpl-badge active">default</span>' : ""}</td>
      <td>${escapeHtml(t.accounting_system || "—")}</td>
      <td>${escapeHtml(t.version_label)}</td>
      <td><span class="tpl-badge ${t.is_active ? "active" : "inactive"}">${t.is_active ? "Active" : "Inactive"}</span></td>
      <td id="col-count-${t.id}">—</td>
      <td>${fmtDate(t.created_at)}</td>
      <td style="white-space:nowrap;display:flex;gap:4px;flex-wrap:wrap">
        <button class="btn btn-secondary" style="font-size:12px;padding:4px 10px" onclick="startEdit('${t.id}')">Edit</button>
        <button class="btn btn-secondary" style="font-size:12px;padding:4px 10px" onclick="editColumns('${t.id}','${escapeHtml(t.name)}')">Columns</button>
        <button class="btn btn-secondary" style="font-size:12px;padding:4px 10px" onclick="duplicateTemplate('${t.id}')">Duplicate</button>
        <button class="btn btn-secondary" style="font-size:12px;padding:4px 10px" onclick="toggleStatus('${t.id}',${!t.is_active})">${t.is_active ? "Deactivate" : "Activate"}</button>
      </td>
    </tr>
  `).join("");
}

applyFilterBtn.addEventListener("click", () => {
  const search = document.getElementById("searchInput").value.trim();
  const is_active_raw = document.getElementById("filterActive").value;
  const accounting_system = document.getElementById("filterSystem").value.trim();
  const params = {};
  if (search) params.search = search;
  if (is_active_raw) params.is_active = is_active_raw;
  if (accounting_system) params.accounting_system = accounting_system;
  loadTemplates(params).catch((e) => setMessage("pageMessage", e.message));
});

refreshTemplatesBtn.addEventListener("click", () =>
  loadTemplates().catch((e) => setMessage("pageMessage", e.message))
);

templateForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const isEdit = !!document.getElementById("tplId").value;
  const payload = {
    name: document.getElementById("tplName").value.trim(),
    accounting_system: document.getElementById("tplAccountingSystem").value.trim() || null,
    version_label: document.getElementById("tplVersionLabel").value.trim() || "v1",
    description: document.getElementById("tplDescription").value.trim() || null,
    is_active: document.getElementById("tplIsActive").checked,
    is_system_default: document.getElementById("tplIsSystemDefault").checked,
  };
  try {
    if (isEdit) {
      await apiFetch(`/admin/export-templates/${document.getElementById("tplId").value}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setMessage("templateFormMessage", "Template updated.", "success");
    } else {
      const tpl = await apiFetch("/admin/export-templates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setMessage("templateFormMessage", "Template created. Click Columns to add columns.", "success");
      _editingTemplateId = tpl.id;
    }
    resetTemplateForm();
    await Promise.all([loadTemplates(), loadAssignments()]);
  } catch (err) {
    setMessage("templateFormMessage", err.message);
  }
});

function resetTemplateForm() {
  document.getElementById("tplId").value = "";
  document.getElementById("tplName").value = "";
  document.getElementById("tplAccountingSystem").value = "";
  document.getElementById("tplVersionLabel").value = "v1";
  document.getElementById("tplDescription").value = "";
  document.getElementById("tplIsActive").checked = true;
  document.getElementById("tplIsSystemDefault").checked = false;
  document.getElementById("formTitle").textContent = "Create Template";
  document.getElementById("tplSubmitBtn").textContent = "Create Template";
  document.getElementById("tplCancelBtn").style.display = "none";
  _editingTemplateId = null;
}

tplCancelBtn.addEventListener("click", resetTemplateForm);

async function startEdit(id) {
  try {
    const tpl = await apiFetch(`/admin/export-templates/${id}`);
    document.getElementById("tplId").value = tpl.id;
    document.getElementById("tplName").value = tpl.name;
    document.getElementById("tplAccountingSystem").value = tpl.accounting_system || "";
    document.getElementById("tplVersionLabel").value = tpl.version_label;
    document.getElementById("tplDescription").value = tpl.description || "";
    document.getElementById("tplIsActive").checked = tpl.is_active;
    document.getElementById("tplIsSystemDefault").checked = tpl.is_system_default;
    document.getElementById("formTitle").textContent = "Edit Template";
    document.getElementById("tplSubmitBtn").textContent = "Save Changes";
    document.getElementById("tplCancelBtn").style.display = "inline-block";
    _editingTemplateId = id;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (err) {
    setMessage("pageMessage", err.message);
  }
}

async function duplicateTemplate(id) {
  try {
    const tpl = await apiFetch(`/admin/export-templates/${id}/duplicate`, { method: "POST" });
    setMessage("pageMessage", `Template duplicated as "${tpl.name}". Activate it when ready.`, "success");
    await loadTemplates();
  } catch (err) {
    setMessage("pageMessage", err.message);
  }
}

async function toggleStatus(id, newStatus) {
  try {
    await apiFetch(`/admin/export-templates/${id}/status?is_active=${newStatus}`, { method: "PATCH" });
    await loadTemplates();
  } catch (err) {
    setMessage("pageMessage", err.message);
  }
}

// ── Column Editor ─────────────────────────────────────────────────────────────

async function editColumns(templateId, templateName) {
  _editingTemplateId = templateId;
  document.getElementById("editingTemplateName").textContent = templateName;
  document.getElementById("columnEditorSection").classList.add("visible");
  document.getElementById("addColumnForm").style.display = "none";
  document.getElementById("previewPanel").style.display = "none";
  document.getElementById("columnEditorMessage").textContent = "";
  await loadColumns(templateId);
  document.getElementById("columnEditorSection").scrollIntoView({ behavior: "smooth" });
}

closeEditorBtn.addEventListener("click", () => {
  document.getElementById("columnEditorSection").classList.remove("visible");
  _editingTemplateId = null;
  loadTemplates();
});

addColumnBtn.addEventListener("click", () => {
  const form = document.getElementById("addColumnForm");
  form.style.display = form.style.display === "none" ? "block" : "none";
  if (form.style.display === "block") {
    updateColFormVisibility();
    document.getElementById("newColHeading").focus();
  }
});

cancelNewColBtn.addEventListener("click", () => {
  document.getElementById("addColumnForm").style.display = "none";
});

document.getElementById("newColType").addEventListener("change", updateColFormVisibility);

document.getElementById("newColTransformPreset").addEventListener("change", function () {
  const customInput = document.getElementById("newColTransformRule");
  if (this.value === "custom") {
    customInput.style.display = "";
    customInput.focus();
  } else {
    customInput.style.display = "none";
    customInput.value = "";
  }
});

// ── Condition Builder ─────────────────────────────────────────────────────────

const COND_OPERATORS = [
  { value: "is_null",      label: "is blank/null" },
  { value: "is_not_null",  label: "is not blank" },
  { value: "eq",           label: "= equals" },
  { value: "ne",           label: "≠ not equals" },
  { value: "gt",           label: "> greater than" },
  { value: "gte",          label: "≥ greater or equal" },
  { value: "lt",           label: "< less than" },
  { value: "lte",          label: "≤ less or equal" },
  { value: "contains",     label: "contains" },
  { value: "not_contains", label: "does not contain" },
  { value: "starts_with",  label: "starts with" },
];

function _buildFieldSel(selected) {
  const opts = ['<option value="">— field —</option>']
    .concat((_availableFields || []).map(f =>
      `<option value="${f}"${f === selected ? " selected" : ""}>${f}</option>`
    ));
  return `<select class="cond-field" style="width:100%;font-size:12px;padding:2px 4px">${opts.join("")}</select>`;
}

function _buildOpSel(selected) {
  const opts = COND_OPERATORS.map(o =>
    `<option value="${o.value}"${o.value === selected ? " selected" : ""}>${o.label}</option>`
  );
  return `<select class="cond-op" style="width:100%;font-size:12px;padding:2px 4px">${opts.join("")}</select>`;
}

function _addCondRow(tbodyId, rule) {
  const tbody = document.getElementById(tbodyId);
  const tr = document.createElement("tr");
  const noVal   = rule && (rule.operator === "is_null" || rule.operator === "is_not_null");
  const useSkip = rule && rule.output === "__skip__";
  const useField = rule && rule.output === "__field__";
  const outHide  = useField || useSkip;
  tr.innerHTML = `
    <td style="padding:3px 4px">${_buildFieldSel(rule ? rule.if_field : "")}</td>
    <td style="padding:3px 4px">${_buildOpSel(rule ? rule.operator : "")}</td>
    <td style="padding:3px 4px">
      <input class="cond-val" type="text" value="${rule && rule.value != null ? escapeHtml(String(rule.value)) : ""}"
        placeholder="value" style="width:100%;font-size:12px;padding:2px 4px;display:${noVal ? "none" : ""}" />
    </td>
    <td style="padding:3px 4px;display:flex;align-items:center;gap:4px">
      <input class="cond-out" type="text" value="${(useField || useSkip) ? "" : (rule ? escapeHtml(rule.output || "") : "")}"
        placeholder="output value" style="flex:1;font-size:12px;padding:2px 4px;display:${outHide ? "none" : ""}" />
      <label style="font-size:11px;white-space:nowrap;display:flex;align-items:center;gap:3px" title="Output the source field value">
        <input type="checkbox" class="cond-use-field" style="width:13px;height:13px"${useField ? " checked" : ""}> field val
      </label>
      <label style="font-size:11px;white-space:nowrap;display:flex;align-items:center;gap:3px" title="Condition matched but skip to next rule">
        <input type="checkbox" class="cond-pass-thru" style="width:13px;height:13px"${useSkip ? " checked" : ""}> pass-thru
      </label>
    </td>
    <td style="padding:3px 4px;text-align:center">
      <button type="button" class="cond-remove-btn" style="background:none;border:none;cursor:pointer;color:var(--danger-text);font-size:16px;line-height:1">&times;</button>
    </td>`;
  tbody.appendChild(tr);

  const outEl       = tr.querySelector(".cond-out");
  const useFieldCb  = tr.querySelector(".cond-use-field");
  const passThruCb  = tr.querySelector(".cond-pass-thru");

  function _syncOutputVis() {
    const skip = passThruCb.checked;
    const fld  = useFieldCb.checked;
    outEl.style.display      = (skip || fld) ? "none" : "";
    useFieldCb.disabled      = skip;
    if (skip) useFieldCb.checked = false;
  }

  tr.querySelector(".cond-op").addEventListener("change", function () {
    const v = this.value;
    tr.querySelector(".cond-val").style.display = (v === "is_null" || v === "is_not_null") ? "none" : "";
  });
  useFieldCb.addEventListener("change", _syncOutputVis);
  passThruCb.addEventListener("change", _syncOutputVis);
  tr.querySelector(".cond-remove-btn").addEventListener("click", () => tr.remove());
}

function _buildConditionRules(tbodyId, defaultId, useFieldId) {
  const rules = [];
  const tbody = document.getElementById(tbodyId);
  for (const tr of tbody.querySelectorAll("tr")) {
    const ifField   = tr.querySelector(".cond-field")?.value || "";
    const operator  = tr.querySelector(".cond-op")?.value || "";
    const value     = tr.querySelector(".cond-val")?.value?.trim() ?? null;
    const useField  = tr.querySelector(".cond-use-field")?.checked;
    const passThru  = tr.querySelector(".cond-pass-thru")?.checked;
    const outTxt    = tr.querySelector(".cond-out")?.value?.trim() || "";
    const output    = passThru ? "__skip__" : (useField ? "__field__" : outTxt);
    const rule = { if_field: ifField, operator, output };
    if (!["is_null", "is_not_null"].includes(operator)) rule.value = value || null;
    rules.push(rule);
  }
  const defUseField = document.getElementById(useFieldId)?.checked;
  const defOut = defUseField ? "__field__" : (document.getElementById(defaultId)?.value?.trim() || "");
  rules.push({ output: defOut });
  return rules;
}

function _populateConditionBuilder(tbodyId, defaultId, useFieldId, rules) {
  document.getElementById(tbodyId).innerHTML = "";
  document.getElementById(defaultId).value = "";
  if (document.getElementById(useFieldId)) document.getElementById(useFieldId).checked = false;
  if (!rules || !rules.length) return;
  const condRules = rules.filter(r => r.if_field && r.operator);
  const defaultRule = rules.find(r => !r.if_field || !r.operator);
  for (const r of condRules) _addCondRow(tbodyId, r);
  if (defaultRule) {
    if (defaultRule.output === "__field__") {
      document.getElementById(useFieldId).checked = true;
      document.getElementById(defaultId).value = "";
    } else {
      document.getElementById(defaultId).value = defaultRule.output || "";
    }
  }
}

function _clearConditionBuilder(tbodyId, defaultId, useFieldId) {
  document.getElementById(tbodyId).innerHTML = "";
  document.getElementById(defaultId).value = "";
  if (document.getElementById(useFieldId)) document.getElementById(useFieldId).checked = false;
}

// wire up "Add Condition" buttons
document.getElementById("addNewCondRowBtn").addEventListener("click", () => _addCondRow("newCondRows", null));
document.getElementById("addEditCondRowBtn").addEventListener("click", () => _addCondRow("editCondRows", null));

// ── Column form visibility ─────────────────────────────────────────────────────

function updateColFormVisibility() {
  const t = document.getElementById("newColType").value;
  const sfEl = document.getElementById("newColSourceField");
  const svEl = document.getElementById("newColStaticValue");
  const trEl = document.getElementById("newColTransformRule");
  sfEl.style.display = (t === "mapped_field" || t === "derived_value" || t === "conditional_value") ? "" : "none";
  svEl.style.display = (t === "static_text") ? "" : "none";
  trEl.style.display = (t !== "empty_column" && t !== "static_text" && t !== "conditional_value") ? "" : "none";
  document.getElementById("newColTransformWrap").style.display = (t === "conditional_value") ? "none" : "";
  document.getElementById("newConditionBuilder").style.display = (t === "conditional_value") ? "" : "none";
}

saveNewColBtn.addEventListener("click", async () => {
  if (!_editingTemplateId) return;
  const heading = document.getElementById("newColHeading").value.trim();
  if (!heading) { setMessage("columnEditorMessage", "Column heading is required."); return; }

  const colType = document.getElementById("newColType").value;
  const payload = {
    column_heading: heading,
    column_type: colType,
    source_field: (colType !== "static_text" && colType !== "empty_column")
      ? document.getElementById("newColSourceField").value || null
      : null,
    static_value: (colType === "static_text")
      ? document.getElementById("newColStaticValue").value.trim() || null
      : null,
    transform_rule: colType === "conditional_value" ? null : (() => {
      const preset = document.getElementById("newColTransformPreset").value;
      if (!preset) return null;
      if (preset === "custom") return document.getElementById("newColTransformRule").value.trim() || null;
      return preset;
    })(),
    condition_rules: colType === "conditional_value"
      ? _buildConditionRules("newCondRows", "newCondDefault", "newCondDefaultUseField")
      : null,
    notes: document.getElementById("newColNotes").value.trim() || null,
    column_order: 999,
    is_active: true,
  };
  try {
    await apiFetch(`/admin/export-templates/${_editingTemplateId}/columns`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    document.getElementById("addColumnForm").style.display = "none";
    document.getElementById("newColHeading").value = "";
    document.getElementById("newColStaticValue").value = "";
    document.getElementById("newColTransformPreset").value = "";
    document.getElementById("newColTransformRule").value = "";
    document.getElementById("newColTransformRule").style.display = "none";
    document.getElementById("newColNotes").value = "";
    _clearConditionBuilder("newCondRows", "newCondDefault", "newCondDefaultUseField");
    setMessage("columnEditorMessage", "Column added.", "success");
    await loadColumns(_editingTemplateId);
  } catch (err) {
    setMessage("columnEditorMessage", err.message);
  }
});

async function loadColumns(templateId) {
  const tpl = await apiFetch(`/admin/export-templates/${templateId}`);
  renderColumnsTable(tpl.columns || []);
  const countEl = document.getElementById(`col-count-${templateId}`);
  if (countEl) countEl.textContent = (tpl.columns || []).length;
}

function renderColumnsTable(cols) {
  const tbody = document.getElementById("columnTableBody");
  if (!cols.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:16px">No columns yet. Click + Add Column to start.</td></tr>';
    return;
  }
  tbody.innerHTML = cols.map((col, idx) => {
    const sourceDesc = col.column_type === "static_text"
      ? `<em style="color:var(--muted)">"${escapeHtml(col.static_value || "")}"</em>`
      : col.column_type === "empty_column"
        ? '<em style="color:var(--muted)">— blank —</em>'
        : escapeHtml(col.source_field || "—");
    return `
      <tr id="col-row-${col.id}">
        <td style="white-space:nowrap">
          <button class="move-btn" onclick="moveColumn(${col.id},'up',${idx})" title="Move up">↑</button>
          <button class="move-btn" onclick="moveColumn(${col.id},'down',${idx})" title="Move down">↓</button>
        </td>
        <td><strong>${escapeHtml(col.column_heading)}</strong>${col.notes ? `<br><small style="color:var(--muted)">${escapeHtml(col.notes)}</small>` : ""}</td>
        <td><span class="col-type-badge">${escapeHtml(col.column_type)}</span></td>
        <td>${sourceDesc}</td>
        <td style="font-size:12px;color:var(--muted)">${escapeHtml(col.transform_rule || "—")}</td>
        <td style="text-align:center"><input type="checkbox" ${col.is_active ? "checked" : ""} onchange="toggleColActive(${col.id},this.checked)" /></td>
        <td style="white-space:nowrap">
          <button class="btn btn-secondary" style="font-size:11px;padding:3px 7px" onclick="openEditColumn(${col.id})">Edit</button>
          <button class="btn btn-secondary" style="font-size:11px;padding:3px 7px;color:var(--danger-text)" onclick="deleteColumn(${col.id})">Remove</button>
        </td>
      </tr>
    `;
  }).join("");
}

let _editingColId = null;

function openEditColumn(colId) {
  const row = document.getElementById(`col-row-${colId}`);
  if (!row) return;

  const tpl = apiFetch(`/admin/export-templates/${_editingTemplateId}`).then((t) => {
    const col = (t.columns || []).find((c) => c.id === colId);
    if (!col) return;

    _editingColId = colId;

    document.getElementById("editColHeading").value = col.column_heading || "";
    document.getElementById("editColType").value = col.column_type || "mapped_field";

    const sfSel = document.getElementById("editColSourceField");
    sfSel.innerHTML = '<option value="">— select field —</option>';
    for (const f of _availableFields) {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      if (f === col.source_field) opt.selected = true;
      sfSel.appendChild(opt);
    }

    document.getElementById("editColStaticValue").value = col.static_value || "";

    const preset = document.getElementById("editColTransformPreset");
    const customInput = document.getElementById("editColTransformRule");
    const knownPresets = Array.from(preset.options).map((o) => o.value).filter(Boolean).filter((v) => v !== "custom");
    if (!col.transform_rule) {
      preset.value = "";
      customInput.style.display = "none";
      customInput.value = "";
    } else if (knownPresets.includes(col.transform_rule)) {
      preset.value = col.transform_rule;
      customInput.style.display = "none";
      customInput.value = "";
    } else {
      preset.value = "custom";
      customInput.style.display = "";
      customInput.value = col.transform_rule;
    }

    document.getElementById("editColNotes").value = col.notes || "";

    _populateConditionBuilder("editCondRows", "editCondDefault", "editCondDefaultUseField", col.condition_rules || null);

    updateEditColFormVisibility();
    document.getElementById("addColumnForm").style.display = "none";
    const editForm = document.getElementById("editColumnForm");
    editForm.style.display = "block";
    editForm.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }).catch((e) => setMessage("columnEditorMessage", e.message));
}

function updateEditColFormVisibility() {
  const t = document.getElementById("editColType").value;
  const sfEl = document.getElementById("editColSourceField");
  const svEl = document.getElementById("editColStaticValue");
  document.getElementById("editColMappedRow").style.display = "";
  sfEl.style.display = (t === "mapped_field" || t === "derived_value" || t === "conditional_value") ? "" : "none";
  svEl.style.display = (t === "static_text") ? "" : "none";
  const isCondVal = t === "conditional_value";
  document.getElementById("editConditionBuilder").style.display = isCondVal ? "" : "none";
  const editTransWrap = document.getElementById("editColTransformPreset")?.parentElement;
  if (editTransWrap) editTransWrap.style.display = isCondVal ? "none" : "";
}

document.getElementById("editColType").addEventListener("change", updateEditColFormVisibility);

document.getElementById("editColTransformPreset").addEventListener("change", function () {
  const ci = document.getElementById("editColTransformRule");
  if (this.value === "custom") { ci.style.display = ""; ci.focus(); }
  else { ci.style.display = "none"; ci.value = ""; }
});

document.getElementById("cancelEditColBtn").addEventListener("click", () => {
  document.getElementById("editColumnForm").style.display = "none";
  _clearConditionBuilder("editCondRows", "editCondDefault", "editCondDefaultUseField");
  _editingColId = null;
});

document.getElementById("saveEditColBtn").addEventListener("click", async () => {
  if (!_editingColId || !_editingTemplateId) return;
  const heading = document.getElementById("editColHeading").value.trim();
  if (!heading) { setMessage("columnEditorMessage", "Column heading is required."); return; }

  const colType = document.getElementById("editColType").value;
  const presetVal = document.getElementById("editColTransformPreset").value;
  const transformRule = !presetVal ? null
    : presetVal === "custom" ? (document.getElementById("editColTransformRule").value.trim() || null)
    : presetVal;

  const payload = {
    column_heading: heading,
    column_type: colType,
    source_field: (colType !== "static_text" && colType !== "empty_column")
      ? document.getElementById("editColSourceField").value || null : null,
    static_value: colType === "static_text"
      ? document.getElementById("editColStaticValue").value.trim() || null : null,
    transform_rule: colType === "conditional_value" ? null : transformRule,
    condition_rules: colType === "conditional_value"
      ? _buildConditionRules("editCondRows", "editCondDefault", "editCondDefaultUseField")
      : null,
    notes: document.getElementById("editColNotes").value.trim() || null,
  };

  try {
    await apiFetch(`/admin/export-templates/${_editingTemplateId}/columns/${_editingColId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    document.getElementById("editColumnForm").style.display = "none";
    _editingColId = null;
    setMessage("columnEditorMessage", "Column updated.", "success");
    await loadColumns(_editingTemplateId);
  } catch (err) {
    setMessage("columnEditorMessage", err.message);
  }
});

async function toggleColActive(colId, isActive) {
  try {
    await apiFetch(`/admin/export-templates/${_editingTemplateId}/columns/${colId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: isActive }),
    });
  } catch (err) {
    setMessage("columnEditorMessage", err.message);
  }
}

async function deleteColumn(colId) {
  if (!confirm("Remove this column?")) return;
  try {
    await apiFetch(`/admin/export-templates/${_editingTemplateId}/columns/${colId}`, { method: "DELETE" });
    setMessage("columnEditorMessage", "Column removed.", "success");
    await loadColumns(_editingTemplateId);
  } catch (err) {
    setMessage("columnEditorMessage", err.message);
  }
}

async function moveColumn(colId, direction, currentIdx) {
  const rows = Array.from(document.getElementById("columnTableBody").querySelectorAll("tr"));
  const colIds = rows.map((r) => parseInt(r.id.replace("col-row-", "")));
  const pos = colIds.indexOf(colId);
  if (direction === "up" && pos <= 0) return;
  if (direction === "down" && pos >= colIds.length - 1) return;

  const swapPos = direction === "up" ? pos - 1 : pos + 1;
  [colIds[pos], colIds[swapPos]] = [colIds[swapPos], colIds[pos]];

  const reorderPayload = colIds.map((id, idx) => ({ id, column_order: idx }));
  try {
    await apiFetch(`/admin/export-templates/${_editingTemplateId}/columns/reorder`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reorderPayload),
    });
    await loadColumns(_editingTemplateId);
  } catch (err) {
    setMessage("columnEditorMessage", err.message);
  }
}

previewBtn.addEventListener("click", async () => {
  if (!_editingTemplateId) return;
  try {
    const result = await apiFetch(`/admin/export-templates/${_editingTemplateId}/preview`, { method: "POST" });
    document.getElementById("previewSheetName").textContent = result.sheet_name;
    const hdr = document.getElementById("previewHeaders");
    hdr.innerHTML = result.columns.map((c) => `<span>${escapeHtml(c)}</span>`).join("");
    const row = document.getElementById("previewRow");
    const sampleRow = result.sample_rows[0] || {};
    row.innerHTML = result.columns.map((c) => `<span>${escapeHtml(sampleRow[c] || "")}</span>`).join("");
    document.getElementById("previewPanel").style.display = "block";
  } catch (err) {
    setMessage("columnEditorMessage", err.message);
  }
});

// ── Assignments ───────────────────────────────────────────────────────────────

async function loadTenantsForAssignment() {
  const data = await apiFetch("/admin/tenants");
  _allTenants = data;
  const sel = document.getElementById("assignTenantId");
  sel.innerHTML = '<option value="">— Select tenant —</option>';
  for (const t of data) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `${t.tenant_name} (${t.tenant_code})`;
    sel.appendChild(opt);
  }
}

document.getElementById("assignTenantId").addEventListener("change", async function () {
  const tenantId = this.value;
  const companySel = document.getElementById("assignCompanyId");
  companySel.innerHTML = '<option value="">Tenant-level (no company)</option>';
  _currentCompanies = [];
  if (!tenantId) return;
  try {
    const companies = await apiFetch(`/admin/companies?tenant_id=${tenantId}`);
    _currentCompanies = companies;
    for (const c of companies) {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = `${c.company_name} (${c.company_code})`;
      companySel.appendChild(opt);
    }
    await checkEffective(tenantId, null);
  } catch (_) {}
});

document.getElementById("assignCompanyId").addEventListener("change", async function () {
  const tenantId = document.getElementById("assignTenantId").value;
  await checkEffective(tenantId, this.value || null);
});

async function checkEffective(tenantId, companyId) {
  const el = document.getElementById("assignEffective");
  if (!tenantId) { el.style.display = "none"; return; }
  try {
    const qs = `tenant_id=${tenantId}${companyId ? "&company_id=" + companyId : ""}`;
    const effective = await apiFetch(`/admin/export-templates/assignments/effective?${qs}`);
    if (effective) {
      const tplName = (_allTemplates.find((t) => t.id === effective.template_id) || {}).name || effective.template_id;
      el.textContent = `Current effective template: "${tplName}" (${companyId ? "company" : "tenant"} level)`;
      el.style.display = "block";
    } else {
      el.textContent = "No template currently assigned at this level.";
      el.style.display = "block";
    }
  } catch (_) {
    el.style.display = "none";
  }
}

function refreshAssignmentTemplateSelect() {
  const sel = document.getElementById("assignTemplateId");
  const current = sel.value;
  sel.innerHTML = '<option value="">— Select template —</option>';
  for (const t of _allTemplates.filter((t) => t.is_active)) {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = `${t.name} (${t.accounting_system || "no system"})`;
    if (t.id === current) opt.selected = true;
    sel.appendChild(opt);
  }
}

assignmentForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const tenantId = document.getElementById("assignTenantId").value;
  const companyId = document.getElementById("assignCompanyId").value || null;
  const templateId = document.getElementById("assignTemplateId").value;
  if (!tenantId || !templateId) {
    setMessage("assignmentMessage", "Select a tenant and a template.");
    return;
  }
  try {
    await apiFetch("/admin/export-templates/assignments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template_id: templateId, tenant_id: tenantId, company_id: companyId }),
    });
    setMessage("assignmentMessage", "Assignment saved.", "success");
    await Promise.all([loadAssignments(), loadAudit()]);
  } catch (err) {
    setMessage("assignmentMessage", err.message);
  }
});

async function loadAssignments() {
  _allAssignments = await apiFetch("/admin/export-templates/assignments");
  renderAssignmentsTable(_allAssignments);
}

function renderAssignmentsTable(list) {
  const tbody = document.getElementById("assignmentsTableBody");
  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px">No assignments yet.</td></tr>';
    return;
  }
  tbody.innerHTML = list.map((a) => {
    const tpl = _allTemplates.find((t) => t.id === a.template_id);
    const tplName = tpl ? tpl.name : a.template_id;
    const tenant = _allTenants.find((t) => t.id === a.tenant_id);
    const tenantName = tenant ? tenant.tenant_name : a.tenant_id;
    const level = a.company_id ? "Company" : "Tenant";
    const companyName = a.company_id
      ? (_currentCompanies.find((c) => c.id === a.company_id) || {}).company_name || a.company_id
      : "—";
    return `
      <tr>
        <td>${escapeHtml(tplName)}</td>
        <td>${escapeHtml(tenantName)}</td>
        <td>${escapeHtml(companyName)}</td>
        <td><span class="tpl-badge ${level === "Company" ? "active" : "inactive"}">${level}</span></td>
        <td><span class="tpl-badge ${a.is_active ? "active" : "inactive"}">${a.is_active ? "Active" : "Inactive"}</span></td>
        <td>${fmtDate(a.assigned_at)}</td>
        <td><button class="btn btn-secondary" style="font-size:11px;padding:3px 8px;color:var(--danger-text)" onclick="removeAssignment(${a.id})">Remove</button></td>
      </tr>
    `;
  }).join("");
}

async function removeAssignment(id) {
  if (!confirm("Remove this assignment?")) return;
  try {
    await apiFetch(`/admin/export-templates/assignments/${id}`, { method: "DELETE" });
    setMessage("assignmentMessage", "Assignment removed.", "success");
    await Promise.all([loadAssignments(), loadAudit()]);
  } catch (err) {
    setMessage("assignmentMessage", err.message);
  }
}

// ── Audit Log ────────────────────────────────────────────────────────────────

async function loadAudit() {
  const data = await apiFetch("/admin/export-templates/audit?limit=50");
  const tbody = document.getElementById("auditTableBody");
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">No audit entries yet.</td></tr>';
    return;
  }
  tbody.innerHTML = data.map((a) => `
    <tr>
      <td><code>${escapeHtml(a.event_type)}</code></td>
      <td>${escapeHtml(a.entity_type)}</td>
      <td style="font-size:11px;color:var(--muted)">${escapeHtml(a.entity_id || "—")}</td>
      <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(a.notes || "—")}</td>
      <td>${fmtDate(a.created_at)}</td>
    </tr>
  `).join("");
}

refreshAuditBtn.addEventListener("click", () =>
  loadAudit().catch((e) => setMessage("pageMessage", e.message))
);

// ── Help ─────────────────────────────────────────────────────────────────────

initPageHelp({
  title: "Export Templates",
  subtitle: "Define custom column layouts for accounting system imports",
  sections: [
    {
      heading: "Column Types",
      items: [
        "mapped_field — pulls a value from extracted invoice data",
        "static_text — repeats a fixed value on every exported row",
        "empty_column — outputs a blank column with just the heading",
        "derived_value — mapped field with a transform applied",
        "conditional_value — mapped field with a fallback default",
      ],
    },
    {
      heading: "Transform Rules",
      items: [
        "uppercase — convert value to UPPER CASE",
        "lowercase — convert value to lower case",
        "number_format — coerce to numeric float",
        "date_format:%d/%m/%Y — reformat a date field",
        "default:N/A — use N/A if the field is blank",
      ],
    },
    {
      heading: "Assignment Precedence",
      items: [
        "Company-level assignment takes priority over tenant-level",
        "Tenant-level applies when no company assignment exists",
        "No assignment = export works as normal, no extra sheet",
        "Inactive templates cannot be assigned",
      ],
    },
  ],
  quickChecks: [
    "Activate a template before assigning it",
    "Verify mapped fields exist in the field catalogue",
    "Use Preview to check column layout before exporting",
    "Duplicate a template to safely create a new version",
  ],
});
