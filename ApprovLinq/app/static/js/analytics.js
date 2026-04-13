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

    document.getElementById("statTotalRows").textContent = Number(summary.total_rows).toLocaleString();
    document.getElementById("statTotalSpend").textContent = fmt(summary.total_spend);
    document.getElementById("statDistinctSuppliers").textContent = Number(summary.distinct_suppliers).toLocaleString();
    document.getElementById("statNeedsReview").textContent = Number(summary.needs_review).toLocaleString();
    document.getElementById("statAvgConfidence").textContent = fmtPct(summary.avg_confidence);

    renderMonthlyChart(monthly);
    renderSuppliersChart(top);
    setMessage("pageMessage", "");
  } catch (err) {
    setMessage("pageMessage", err.message || "Failed to load analytics.", "server-error");
  }
}

function renderMonthlyChart(data) {
  const emptyEl = document.getElementById("monthlyEmpty");
  const canvas = document.getElementById("monthlyChart");
  if (!data || data.length === 0) {
    canvas.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  canvas.style.display = "";
  emptyEl.style.display = "none";

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
  const canvas = document.getElementById("suppliersChart");
  if (!data || data.length === 0) {
    canvas.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  canvas.style.display = "";
  emptyEl.style.display = "none";

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
    const session = await getSessionInfo();
    const platformLink = document.getElementById("platformAdminLink");
    if (platformLink && String(session.role || "").toLowerCase() === "admin") {
      platformLink.classList.remove("hidden");
    }

    await populateTenantSelector("tenantSelector");
    const companies = await loadCompaniesAnalytics();
    if (companies.length) {
      await loadAnalytics(companies[0].id);
    }
  } catch (err) {
    setMessage("pageMessage", err.message || "Failed to initialise page.", "server-error");
  }
}

document.getElementById("companySelector").addEventListener("change", (e) => {
  if (e.target.value) loadAnalytics(e.target.value);
});

document.getElementById("tenantSelector").addEventListener("change", async () => {
  const companies = await loadCompaniesAnalytics();
  if (companies.length) loadAnalytics(companies[0].id);
});

// logoutBtn is injected by ap-ui.js shell — wired there via logoutAndGo

initAnalyticsPage();
