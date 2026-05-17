# ApprovLinq Changelog

## v3.61.0 — 2026-03-17

### Supplier name & code improvements
- OpenAI is now always consulted for supplier name extraction (previously only called when the rule-based result looked suspicious). AI result always wins, ensuring the invoice *sender* is captured rather than the *recipient*.
- Supplier-to-posting-account matching upgraded from exact `ilike` to three-level fuzzy matching:
  1. Exact case-insensitive match
  2. Normalised containment — e.g. "BP FUEL CARD" matches list entry "BP"
  3. Word-overlap ≥ 50% — e.g. "Acme Supplies" matches "Acme Supplies Limited"
- When a fuzzy match is found the supplier name is canonicalised to the list entry so all rows for the same supplier are consistent.

### Line items (Lines mode)
- Lines mode now uses a dedicated OpenAI call to extract individual goods/service line items as structured objects (description, quantity, unit price, amount).
- Totals, VAT, subtotals, and discount summary rows are explicitly excluded by the prompt.
- Each extracted line item becomes its own row in the output.
- If the sum of line amounts does not reconcile with the invoice total, affected rows are flagged for review.
- Falls back to the previous rule-based splitter if OpenAI is unavailable.

### Export (Excel)
- Fixed crash when `batch_name` is `None` — filename generation now safely defaults to `"batch"`.
- Fixed crash caused by openpyxl being unable to write Python `UUID` and `Decimal` objects to Excel cells — these are now converted to `str` and `float` respectively before export.
- Internal system columns (`id`, `batch_id`, `tenant_id`, etc.) excluded from the export sheet.
- `supplier_posting_account` and `nominal_account_code` added to the preferred column order.

### Version management
- `VERSION` file introduced at `ApprovLinq/VERSION`.
- `/version` FastAPI endpoint exposes the current version as JSON.
- All pages display a live version badge via `common.js`.
- Pre-commit hook at `.git/hooks/pre-commit` auto-increments the patch number on every commit.
- Helper scripts: `scripts/bump-version.sh` and `scripts/install-hooks.sh` for new clones.

---

## v3.60 and earlier

Pre-changelog. See git history for changes prior to v3.61.0.
