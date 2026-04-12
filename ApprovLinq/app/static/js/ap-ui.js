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
  const LOGO_MONOGRAM = '<svg width="22" height="22" viewBox="0 0 240 240" fill="none" aria-hidden="true"><g stroke="var(--ap-navy)" stroke-width="14" stroke-linecap="round" stroke-linejoin="round" fill="none"><line x1="60" y1="200" x2="120" y2="50"/><line x1="120" y1="50" x2="180" y2="200"/></g><polyline points="85,140 105,160 150,120" stroke="var(--ap-accent)" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>';

  const LOGO_WORDMARK = '<svg width="140" height="26" viewBox="0 0 600 110" fill="none" aria-label="Approvlinq"><text x="0" y="80" font-family="Inter,Helvetica,Arial,sans-serif" font-weight="700" font-size="84" letter-spacing="-0.025em"><tspan fill="var(--ap-navy)">Approv</tspan><tspan fill="var(--ap-accent)">linq</tspan></text></svg>';

  function renderLogos() {
    document.querySelectorAll("[data-ap-logo=monogram]").forEach(el => { el.innerHTML = LOGO_MONOGRAM; });
    document.querySelectorAll("[data-ap-logo=wordmark]").forEach(el => { el.innerHTML = LOGO_WORDMARK; });
  }

  // ── Public API ────────────────────────────────────────────────────────────
  window.ApprovlinqUI = {
    applyTheme, toggleTheme, currentTheme, applySavedThemeEarly,
    renderThemeToggle, renderLogos, wireThemeToggle,
    LOGO_MONOGRAM, LOGO_WORDMARK,
  };

  // Apply saved theme immediately (defensive — ideally <head> also did this)
  applySavedThemeEarly();

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { renderLogos(); wireThemeToggle(); });
  } else {
    renderLogos(); wireThemeToggle();
  }
})();
