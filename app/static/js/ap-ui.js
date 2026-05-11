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

  // Ensure the legacy app.css loads alongside the new tokens/components for
  // any migrated page that still emits legacy class names (.stat-grid, .row,
  // .pill, etc.). Kept as a link tag so it can be removed in one place later.
  function ensureLegacyStylesheet() {
    if (document.querySelector('link[data-ap-legacy-css]')) return;
    const existing = Array.from(document.styleSheets || []).some(s => (s.href || "").endsWith("/static/css/app.css"));
    if (existing) return;
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = "/static/css/app.css";
    l.setAttribute("data-ap-legacy-css", "true");
    // Insert BEFORE any other <link> so components.css wins in cascade
    const firstLink = document.head.querySelector('link[rel="stylesheet"]');
    if (firstLink) document.head.insertBefore(l, firstLink);
    else document.head.appendChild(l);
  }

  function toggleTheme() {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
    renderThemeToggle();
  }

  const ICON_CHEVRON_LEFT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>';
  const ICON_CHEVRON_RIGHT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';
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

  // ── Sidebar collapse ─────────────────────────────────────────────────────
  const SIDEBAR_KEY = "ap_sidebar_collapsed";

  function isSidebarCollapsed() {
    try { return localStorage.getItem(SIDEBAR_KEY) === "1"; } catch { return false; }
  }

  function applySidebarState(collapsed) {
    const shell = document.querySelector(".ap-shell");
    const side  = document.querySelector(".ap-side");
    const btn   = document.querySelector("[data-ap-sidebar-toggle]");
    if (!shell) return;
    if (collapsed) {
      shell.classList.add("sidebar-collapsed");
      side && side.classList.add("collapsed");
    } else {
      shell.classList.remove("sidebar-collapsed");
      side && side.classList.remove("collapsed");
    }
    if (btn) {
      btn.innerHTML = collapsed ? ICON_CHEVRON_RIGHT : ICON_CHEVRON_LEFT;
      btn.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      btn.title = btn.getAttribute("aria-label");
    }
  }

  function wireSidebarToggle() {
    const btn = document.querySelector("[data-ap-sidebar-toggle]");
    if (!btn) return;
    // Guard: only wire once per button instance. wireSidebarToggle() is called
    // from both renderShell() and init(), so without this guard each click fires
    // two handlers that toggle opposite directions — net result: no change.
    if (btn.dataset.apSidebarWired) {
      applySidebarState(isSidebarCollapsed());
      return;
    }
    btn.dataset.apSidebarWired = "1";
    applySidebarState(isSidebarCollapsed());
    btn.addEventListener("click", () => {
      const nowCollapsed = !document.querySelector(".ap-shell")?.classList.contains("sidebar-collapsed");
      try { localStorage.setItem(SIDEBAR_KEY, nowCollapsed ? "1" : "0"); } catch {}
      applySidebarState(nowCollapsed);
    });
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
  // Two-tone wordmark. Uses brand-specific --ap-logo-1 / --ap-logo-2
  // so the two halves stay visually distinct in BOTH light and dark modes.
  const LOGO_WORDMARK = '<svg width="140" height="26" viewBox="0 0 600 110" fill="none" aria-label="Approvlinq"><text x="0" y="80" font-family="Inter,Helvetica,Arial,sans-serif" font-weight="700" font-size="84" letter-spacing="-0.025em"><tspan fill="var(--ap-logo-1)">Approv</tspan><tspan fill="var(--ap-logo-2)">linq</tspan></text></svg>';

  const LOGO_WORDMARK_LG = '<svg width="180" height="34" viewBox="0 0 600 110" fill="none" aria-label="Approvlinq"><text x="0" y="80" font-family="Inter,Helvetica,Arial,sans-serif" font-weight="700" font-size="84" letter-spacing="-0.025em"><tspan fill="var(--ap-logo-1)">Approv</tspan><tspan fill="var(--ap-logo-2)">linq</tspan></text></svg>';

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
    { id: "tenant",    href: "/static/tenant.html",           label: "Tenant admin",     icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18M5 21V7l8-4 8 4v14M9 9h1M14 9h1M9 13h1M14 13h1M9 17h1M14 17h1"/></svg>' },
    { id: "rules",     href: "/static/rules.html",            label: "Rules",            icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2M9 12h6M9 16h4"/></svg>' },
    { id: "templates", href: "/static/export_templates.html", label: "Export templates", adminOnly: true, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>' },
    { id: "admin",     href: "/static/admin.html",            label: "Platform admin",   adminOnly: true, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v6M12 17v6M4.22 4.22l4.24 4.24M15.54 15.54l4.24 4.24M1 12h6M17 12h6M4.22 19.78l4.24-4.24M15.54 8.46l4.24-4.24"/></svg>' },
  ];

  function renderShell() {
    const root = document.querySelector("[data-ap-shell]");
    if (!root) return;
    let opts = {};
    try { opts = JSON.parse(root.getAttribute("data-ap-shell") || "{}"); } catch {}
    const active = opts.active || "";
    const crumb = opts.crumb || [];

    // ── KEY FIX ──────────────────────────────────────────────────────────────
    // Detach the live [data-ap-page-body] node BEFORE we touch the DOM so we
    // can re-attach it inside the new shell. Using the live node (rather than
    // body.innerHTML) preserves all event listeners that page scripts already
    // bound to elements inside it.
    const body = document.querySelector("[data-ap-page-body]");
    if (body && body.parentNode) body.parentNode.removeChild(body);
    // ─────────────────────────────────────────────────────────────────────────

    const navHtml = NAV.map(n => {
      if (n.section) return `<div class="ap-nav-section">${n.section}</div>`;
      const cls = "ap-nav-item" + (n.id === active ? " active" : "") + (n.adminOnly ? " hidden" : "");
      const attr = n.adminOnly ? ' data-admin-only="true"' : '';
      return `<a class="${cls}"${attr} id="nav-${n.id}" href="${n.href}" title="${n.label}">${n.icon}<span class="ap-nav-label">${n.label}</span></a>`;
    }).join("");

    const crumbHtml = crumb.map((c, i) => {
      const [label, state] = c;
      const sep = i > 0 ? '<span class="sep">/</span>' : '';
      const inner = state === "cur" ? `<span class="cur">${label}</span>` : `<span>${label}</span>`;
      return sep + inner;
    }).join("");

    // Build the shell scaffold via innerHTML (no page-content goes in here).
    root.outerHTML = `
      <div class="ap-shell">
        <aside class="ap-side">
          <button class="ap-sidebar-toggle" data-ap-sidebar-toggle type="button" aria-label="Collapse sidebar" title="Collapse sidebar"></button>
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
        <div class="ap-main" id="_apMainRegion">
          <div class="ap-topbar">
            <div class="ap-crumb">${crumbHtml}</div>
            <div class="ap-top-actions">
              <button class="ap-theme-toggle" data-ap-theme-toggle type="button" aria-label="Toggle theme"></button>
            </div>
          </div>
        </div>
      </div>`;

    // Re-attach the live page-body node into the new .ap-main container so
    // all previously-bound listeners remain intact.
    const main = document.getElementById("_apMainRegion");
    if (main && body) {
      main.appendChild(body);
    }
    // Re-render logos & rewire theme toggle since we replaced the DOM
    renderLogos();
    wireThemeToggle();
    wireSidebarToggle();
    // Populate user block from /auth/me if available
    populateUserBlock();
    // Wire logout — use logoutAndGo from common.js
    const lo = document.getElementById("logoutBtn");
    if (lo) {
      const logoutFn = typeof logoutAndGo === "function" ? logoutAndGo
                     : typeof window.logoutAndGo === "function" ? window.logoutAndGo
                     : null;
      if (logoutFn) lo.addEventListener("click", logoutFn);
    }
  }

  async function populateUserBlock() {
    const nameEl = document.getElementById("userName");
    const tenantEl = document.getElementById("userTenant");
    const avEl = document.getElementById("userAvatar");
    if (!nameEl) return;
    // apiFetch is defined globally by common.js (always loaded before ap-ui.js)
    const fetcher = typeof apiFetch === "function" ? apiFetch
                  : typeof window.apiFetch === "function" ? window.apiFetch
                  : typeof window.api === "function" ? window.api
                  : null;
    if (!fetcher) return;
    try {
      // /auth/me returns: { user_id, email, full_name, role, tenants:[{tenant_name,...}] }
      const me = await fetcher("/auth/me");
      if (!me) return;
      const displayName = me.full_name || me.email || "User";
      nameEl.textContent = displayName;
      if (tenantEl) {
        const defaultTenant = (me.tenants || []).find(t => t.is_default) || (me.tenants || [])[0];
        tenantEl.textContent = defaultTenant ? defaultTenant.tenant_name
                                : (me.role === "admin" ? "Platform admin" : "");
      }
      if (avEl) {
        const initials = String(displayName).split(/[\s@._-]+/)
          .map(s => s[0]).filter(Boolean).slice(0, 2).join("").toUpperCase();
        avEl.textContent = initials || "·";
      }
      // Reveal nav items marked admin-only when the user is a platform admin.
      if (me.role === "admin") {
        document.querySelectorAll(".ap-nav-item[data-admin-only]").forEach(el => el.classList.remove("hidden"));
      }
    } catch { /* silent — not authed or endpoint missing */ }
  }

  // ── Public API ────────────────────────────────────────────────────────────
  window.ApprovlinqUI = {
    applyTheme, toggleTheme, currentTheme, applySavedThemeEarly,
    renderThemeToggle, renderLogos, wireThemeToggle, renderShell, wireSidebarToggle,
    LOGO_WORDMARK,
  };

  applySavedThemeEarly();

  function init() {
    ensureLegacyStylesheet();
    renderShell();
    renderLogos();
    wireThemeToggle();
    wireSidebarToggle();
    // Opt-in UI sanity check when URL has ?ui-check=1
    if (/[?&]ui-check=1(&|$)/.test(location.search)) {
      const s = document.createElement("script");
      s.src = "/static/js/ui-check.js";
      s.async = true;
      document.head.appendChild(s);
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
