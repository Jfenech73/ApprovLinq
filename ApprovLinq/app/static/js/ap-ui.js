/* ============================================================================
   Approvlinq — UI helpers (v2 shell)
   ----------------------------------------------------------------------------
   Self-contained, framework-free. Safe to load alongside the legacy app.js.
   ============================================================================ */
(function () {
  "use strict";

  // ── Theme toggle ──────────────────────────────────────────────────────────
  // Persisted to localStorage so the choice survives reloads. On first visit
  // we respect the OS preference via @media (prefers-color-scheme) in CSS —
  // we do NOT write a theme attribute until the user explicitly toggles.
  const STORAGE_KEY = "ap_theme";

  function applyTheme(t) {
    if (t === "light" || t === "dark") {
      document.documentElement.setAttribute("data-theme", t);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme")
      || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }

  // Apply saved choice as early as possible (ideally the loader calls
  // applySavedThemeEarly() from an inline <script> in <head> to avoid flash).
  function applySavedThemeEarly() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "light" || saved === "dark") applyTheme(saved);
    } catch (e) { /* storage disabled */ }
  }

  function toggleTheme() {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
    renderThemeToggle();
  }

  const ICON_SUN = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  const ICON_MOON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function renderThemeToggle() {
    const btn = document.querySelector("[data-ap-theme-toggle]");
    if (!btn) return;
    const isDark = currentTheme() === "dark";
    btn.innerHTML = isDark ? ICON_SUN : ICON_MOON;
    btn.setAttribute("aria-label", isDark ? "Switch to light theme" : "Switch to dark theme");
    btn.title = btn.getAttribute("aria-label");
  }

  function wireThemeToggle() {
    const btn = document.querySelector("[data-ap-theme-toggle]");
    if (!btn) return;
    btn.addEventListener("click", toggleTheme);
    renderThemeToggle();
  }

  // ── Logo snippets ─────────────────────────────────────────────────────────
  // Inline SVG so the mark always renders, even if the static image route is
  // slow or the page is offline. Colours are CSS variables that track theme.
  // Two-tone wordmark, works in both light and dark modes because both
  // halves use CSS variables that shift with the theme.
  const LOGO_WORDMARK = '<svg width="140" height="26" viewBox="0 0 600 110" fill="none" aria-label="Approvlinq"><text x="0" y="80" font-family="Inter,Helvetica,Arial,sans-serif" font-weight="700" font-size="84" letter-spacing="-0.025em"><tspan fill="var(--ap-navy)">Approv</tspan><tspan fill="var(--ap-accent)">linq</tspan></text></svg>';

  // Larger version for the login page
  const LOGO_WORDMARK_LG = '<svg width="180" height="34" viewBox="0 0 600 110" fill="none" aria-label="Approvlinq"><text x="0" y="80" font-family="Inter,Helvetica,Arial,sans-serif" font-weight="700" font-size="84" letter-spacing="-0.025em"><tspan fill="var(--ap-navy)">Approv</tspan><tspan fill="var(--ap-accent)">linq</tspan></text></svg>';

  function renderLogos() {
    document.querySelectorAll("[data-ap-logo=wordmark]").forEach(el => { el.innerHTML = LOGO_WORDMARK; });
    document.querySelectorAll("[data-ap-logo=wordmark-lg]").forEach(el => { el.innerHTML = LOGO_WORDMARK_LG; });
    // Legacy: any remaining monogram slots render the wordmark instead, so
    // un-migrated pages with data-ap-logo="monogram" still show the brand.
    document.querySelectorAll("[data-ap-logo=monogram]").forEach(el => { el.innerHTML = LOGO_WORDMARK; });
  }

  // ── Shared shell renderer ─────────────────────────────────────────────────
  // Pages that use the v2 UI only need to declare which nav item is active
  // and what the breadcrumb should read. The sidebar + topbar are identical
  // across the app, so maintaining them in one place is the point.
  //
  // Usage in a page: add <div data-ap-shell='{"active":"scanner","crumb":[["Work",""],["Scanner","cur"]]}'></div>
  // as the first child of <body class="ap-app">, and put your page content in
  // <div data-ap-page-body>...</div>. This function wraps them in .ap-shell.
  const NAV = [
    { section: "Work" },
    { id: "scanner",   href: "/static/scanner.html",          label: "Scanner",          icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7V5a1 1 0 0 1 1-1h2M17 4h2a1 1 0 0 1 1 1v2M20 17v2a1 1 0 0 1-1 1h-2M7 20H5a1 1 0 0 1-1-1v-2M8 12h8"/></svg>' },
    { id: "analytics", href: "/static/analytics.html",        label: "Analytics",        icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18M7 14l4-4 4 4 5-5"/></svg>' },
    { section: "Configure" },
    { id: "templates", href: "/static/export_templates.html", label: "Export templates", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>' },
    { id: "tenant",    href: "/static/tenant.html",           label: "Tenant admin",     icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18M5 21V7l8-4 8 4v14M9 9h1M14 9h1M9 13h1M14 13h1M9 17h1M14 17h1"/></svg>' },
    { id: "admin",     href: "/static/admin.html",            label: "Platform admin",  hideIfNotAdmin: true, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v6M12 17v6M4.22 4.22l4.24 4.24M15.54 15.54l4.24 4.24M1 12h6M17 12h6M4.22 19.78l4.24-4.24M15.54 8.46l4.24-4.24"/></svg>' },
  ];

  function renderShell() {
    const root = document.querySelector("[data-ap-shell]");
    if (!root) return;
    let opts = {};
    try { opts = JSON.parse(root.getAttribute("data-ap-shell") || "{}"); } catch {}
    const active = opts.active || "";
    const crumb = opts.crumb || [];
    const body = document.querySelector("[data-ap-page-body]");
    const bodyHtml = body ? body.outerHTML : "";

    const navHtml = NAV.map(n => {
      if (n.section) return `<div class="ap-nav-section">${n.section}</div>`;
      const cls = "ap-nav-item" + (n.id === active ? " active" : "") + (n.hideIfNotAdmin ? " hidden" : "");
      return `<a class="${cls}" id="nav-${n.id}" href="${n.href}">${n.icon}${n.label}</a>`;
    }).join("");

    const crumbHtml = crumb.map((c, i) => {
      const [label, state] = c;
      const sep = i > 0 ? '<span class="sep">/</span>' : '';
      const inner = state === "cur" ? `<span class="cur">${label}</span>` : `<span>${label}</span>`;
      return sep + inner;
    }).join("");

    root.outerHTML = `
      <div class="ap-shell">
        <aside class="ap-side">
          <a class="ap-brand" href="/static/scanner.html" aria-label="Approvlinq">
            <span data-ap-logo="wordmark"></span>
          </a>
          <nav class="ap-nav">${navHtml}</nav>
          <div class="ap-user">
            <div class="ap-avatar" id="userAvatar">·</div>
            <div style="min-width:0; flex:1">
              <div class="ap-user-name" id="userName">—</div>
              <div id="userTenant" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis">—</div>
            </div>
            <button id="logoutBtn" class="ap-btn ap-btn-ghost ap-btn-sm" type="button" title="Sign out" style="padding:0 8px">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>
            </button>
          </div>
        </aside>
        <div class="ap-main">
          <div class="ap-topbar">
            <div class="ap-crumb">${crumbHtml}</div>
            <div class="ap-top-actions">
              <button class="ap-theme-toggle" data-ap-theme-toggle type="button" aria-label="Toggle theme"></button>
            </div>
          </div>
          ${bodyHtml}
        </div>
      </div>`;
    // Re-render logos & rewire theme toggle since we replaced the DOM
    renderLogos();
    wireThemeToggle();
    // Populate user block from /auth/me if available
    populateUserBlock();
    // Wire logout if common.js didn't
    const lo = document.getElementById("logoutBtn");
    if (lo && typeof window.logout === "function") lo.onclick = window.logout;
  }

  async function populateUserBlock() {
    const nameEl = document.getElementById("userName");
    const tenantEl = document.getElementById("userTenant");
    const avEl = document.getElementById("userAvatar");
    if (!nameEl || typeof window.api !== "function") return;
    try {
      const me = await window.api("/auth/me");
      if (me && me.email) {
        nameEl.textContent = me.name || me.email;
        if (tenantEl) tenantEl.textContent = me.tenant_name || me.tenant || "";
        if (avEl) {
          const initials = (me.name || me.email).split(/[ @]/).map(s => s[0]).filter(Boolean).slice(0, 2).join("").toUpperCase();
          avEl.textContent = initials || "·";
        }
      }
    } catch { /* silent — not authed or endpoint missing */ }
  }

  // ── Public API ────────────────────────────────────────────────────────────
  window.ApprovlinqUI = {
    applyTheme, toggleTheme, currentTheme, applySavedThemeEarly,
    renderThemeToggle, renderLogos, wireThemeToggle, renderShell,
    LOGO_WORDMARK,
  };

  applySavedThemeEarly();

  function init() { renderShell(); renderLogos(); wireThemeToggle(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
