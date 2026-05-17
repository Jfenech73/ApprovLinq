// Analytics page JS
// Depends on: common.js (apiFetch, escapeHtml, setMessage, ensureAuth,
//             getSessionInfo, populateTenantSelector)
// Script load order in analytics.html: common.js → ap-ui.js → analytics.js
// ap-ui.js runs first so the shell (and the live [data-ap-page-body] node
// containing tenantSelector / companySelector) is fully in the DOM by the
// time this file executes.

let monthlyChart = null;
let suppliersChart = null;

function fmt(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (isNaN(n)) return "—";
  if (n >= 1_000_000) return `€${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `€${(n / 1_000).toFixed(1)}K`;
  return new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR", minimumFractionDigits: 0 }).format(n);
}

function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return `${Math.round(Number(v) * 100)}%`;
}

async function loadCompaniesAnalytics() {
  const sel = document.getElementById("companySelector");
  if (!sel) return [];
  try {
    const companies = await apiFetch("/tenant/companies");
    sel.innerHTML = companies.length
      ? companies.map((c) => `<option value="${c.id}">${escapeHtml(c.company_name)}</option>`).join("")
      : '<option value="">No companies</option>';
    return companies;
  } catch (_) {
    sel.innerHTML = '<option value="">No companies</option>';
    return [];
  }
}

async function loadAnalytics(companyId) {
  if (!companyId) return;
  setMessage("pageMessage", "Loading analytics…");
  try {
    const [summary, monthly, top] = await Promise.all([
      apiFetch(`/analytics/summary?company_id=${companyId}`),
      apiFetch(`/analytics/monthly?company_id=${companyId}&months=13`),
      apiFetch(`/analytics/top-suppliers?company_id=${companyId}&limit=10`),
    ]);

    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set("statTotalRows",        Number(summary.total_rows).toLocaleString());
    set("statTotalSpend",       fmt(summary.total_spend));
    set("statDistinctSuppliers",Number(summary.distinct_suppliers).toLocaleString());
    set("statNeedsReview",      Number(summary.needs_review).toLocaleString());
    set("statAvgConfidence",    fmtPct(summary.avg_confidence));

    renderMonthlyChart(monthly);
    renderSuppliersChart(top);
    setMessage("pageMessage", "");
  } catch (err) {
    setMessage("pageMessage", err.message || "Failed to load analytics.", "server-error");
  }
}

function renderMonthlyChart(data) {
  const emptyEl = document.getElementById("monthlyEmpty");
  const canvas  = document.getElementById("monthlyChart");
  if (!canvas) return;
  if (!data || data.length === 0) {
    canvas.style.display = "none";
    if (emptyEl) emptyEl.style.display = "block";
    return;
  }
  canvas.style.display = "";
  if (emptyEl) emptyEl.style.display = "none";

  if (monthlyChart) monthlyChart.destroy();
  monthlyChart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: data.map((d) => d.month),
      datasets: [
        {
          label: "Total (incl. VAT)",
          data: data.map((d) => Number(d.total) || 0),
          backgroundColor: "rgba(37,99,235,0.75)",
          borderColor: "rgba(37,99,235,1)",
          borderWidth: 1,
          borderRadius: 4,
        },
        {
          label: "Net (excl. VAT)",
          data: data.map((d) => Number(d.net) || 0),
          backgroundColor: "rgba(37,99,235,0.25)",
          borderColor: "rgba(37,99,235,0.6)",
          borderWidth: 1,
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: "top" },
        tooltip: { callbacks: { label: (c) => ` ${fmt(c.parsed.y)}` } },
      },
      scales: {
        y: { beginAtZero: true, ticks: { callback: (v) => fmt(v) } },
      },
    },
  });
}

function renderSuppliersChart(data) {
  const emptyEl = document.getElementById("suppliersEmpty");
  const canvas  = document.getElementById("suppliersChart");
  if (!canvas) return;
  if (!data || data.length === 0) {
    canvas.style.display = "none";
    if (emptyEl) emptyEl.style.display = "block";
    return;
  }
  canvas.style.display = "";
  if (emptyEl) emptyEl.style.display = "none";

  if (suppliersChart) suppliersChart.destroy();
  suppliersChart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: data.map((d) => d.supplier_name || "Unknown"),
      datasets: [
        {
          label: "Total Spend",
          data: data.map((d) => Number(d.total) || 0),
          backgroundColor: "rgba(16,185,129,0.75)",
          borderColor: "rgba(16,185,129,1)",
          borderWidth: 1,
          borderRadius: 4,
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => ` ${fmt(c.parsed.x)}` } },
      },
      scales: {
        x: { beginAtZero: true, ticks: { callback: (v) => fmt(v) } },
      },
    },
  });
}

async function initAnalyticsPage() {
  if (!ensureAuth()) return;
  try {
    // populateTenantSelector sets the active X-Tenant-Id in localStorage so
    // subsequent apiFetch calls (/tenant/companies, /analytics/*) are scoped
    // to the right tenant automatically via authHeaders().
    await populateTenantSelector("tenantSelector");
    const companies = await loadCompaniesAnalytics();
    if (companies.length) {
      await loadAnalytics(companies[0].id);
    } else {
      setMessage("pageMessage", "No companies found for this tenant.", "");
    }
  } catch (err) {
    setMessage("pageMessage", err.message || "Failed to initialise page.", "server-error");
  }
}

// ── Selector change handlers ──────────────────────────────────────────────────
// These elements are guaranteed to exist at this point because:
//   1. They are declared in analytics.html inside [data-ap-page-body]
//   2. ap-ui.js runs before this script, moves the live node into the shell
//      (preserving it), so getElementById() finds the real nodes here.

const _companySel = document.getElementById("companySelector");
if (_companySel) {
  _companySel.addEventListener("change", (e) => {
    if (e.target.value) loadAnalytics(e.target.value);
  });
}

const _tenantSel = document.getElementById("tenantSelector");
if (_tenantSel) {
  _tenantSel.addEventListener("change", async () => {
    const companies = await loadCompaniesAnalytics();
    if (companies.length) loadAnalytics(companies[0].id);
    else setMessage("pageMessage", "No companies found for this tenant.", "");
  });
}

// logoutBtn is injected by ap-ui.js shell — wired there via logoutAndGo

initAnalyticsPage();
