/* ui-check.js
 *
 * Opt-in frontend sanity check. Runs only when a page URL contains ?ui-check=1.
 * Iterates a per-page manifest of required element IDs, reports any missing
 * via console.error AND a visible banner. Catches migration regressions
 * instantly without adding a test runner.
 *
 * Usage:
 *   /static/scanner.html?ui-check=1
 *   /static/review.html?batch_id=...&ui-check=1
 *   /static/tenant.html?ui-check=1
 *   /static/admin.html?ui-check=1
 *   /static/export_templates.html?ui-check=1
 *   /static/analytics.html?ui-check=1
 */
(function () {
  if (!/[?&]ui-check=1(&|$)/.test(location.search)) return;

  // Manifest: the IDs each page's JS assumes exist. Keep this in sync when
  // adding new required elements to the UI.
  const MANIFEST = {
    "scanner.html": [
      "tenantSelector", "companySelector", "refreshBatchesBtn", "createBatchForm",
      "batchName", "selectedBatchEmpty", "selectedBatchPanel", "selectedBatchId",
      "selectedBatchName", "selectedBatchStatus", "selectedBatchNotes",
      "batchScanModeGroup", "pdfFiles", "uploadBtn", "processBtn",
      "filesTableBody", "batchesTableBody", "rowsTableBody",
      "refreshRowsBtn", "reviewBtn", "exportBtn", "pageMessage",
      "logoutBtn", "userName", "userTenant", "userAvatar",
    ],
    "review.html": [
      "pageMessage", "batchTitle", "batchStatusPill", "statRows", "statCorrected",
      "statFlagged", "statVersion", "rowList", "rowEditor", "auditList",
      "approveBtn", "exportBtn", "reopenBtn", "markFileReviewedBtn",
      "prevPageBtn", "nextPageBtn", "pageLabel", "remapMode",
      "remapHint", "remapTargetLabel", "previewWrap", "previewImg", "remapSelection",
      "logoutBtn", "userName", "userTenant",
    ],
    "tenant.html": [
      "tenantSelector", "companySelector", "profileForm", "passwordForm",
      "companyForm", "supplierForm", "pageMessage", "logoutBtn",
    ],
    "admin.html": [
      "tenantForm", "userForm", "refreshCapacityBtn", "userRole", "userTenantId",
      "tenantsTableBody", "usersTableBody", "pageMessage", "logoutBtn",
    ],
    "export_templates.html": [
      "templateForm", "assignmentForm", "addColumnBtn", "previewBtn",
      "pageMessage", "logoutBtn",
    ],
    "analytics.html": [
      "companySelector", "statTotalRows", "statTotalSpend", "pageMessage",
    ],
  };

  function showBanner(kind, html) {
    const b = document.createElement("div");
    b.className = `ap-ui-banner ap-ui-banner-${kind}`;
    b.innerHTML = html;
    document.body.appendChild(b);
  }

  function run() {
    const page = (location.pathname.split("/").pop() || "").toLowerCase();
    const manifest = MANIFEST[page];
    if (!manifest) {
      console.warn("[ui-check] No manifest for page:", page);
      return;
    }
    const missing = manifest.filter(id => !document.getElementById(id));
    if (missing.length === 0) {
      console.log(`[ui-check] OK — all ${manifest.length} required elements present on ${page}`);
      showBanner("ok", `<strong>ui-check OK</strong> — ${manifest.length} elements found on ${page}`);
    } else {
      console.error(`[ui-check] ${page} is MISSING ${missing.length} required element(s):`, missing);
      showBanner("err",
        `<strong>ui-check FAILED</strong> on ${page}: missing ` +
        missing.map(id => `<code>#${id}</code>`).join(", "));
    }

    // Also watch for console errors in the next 3s
    const origError = console.error;
    const errors = [];
    console.error = function (...args) { errors.push(args); origError.apply(console, args); };
    setTimeout(() => {
      console.error = origError;
      if (errors.length > 0) {
        showBanner("err",
          `<strong>${errors.length} console error(s)</strong> in first 3 seconds. Check devtools.`);
      }
    }, 3000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => setTimeout(run, 400));
  } else {
    setTimeout(run, 400);
  }
})();
