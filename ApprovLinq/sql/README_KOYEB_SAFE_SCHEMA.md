# Koyeb-safe schema notes

The application applies idempotent startup schema checks from `app/main.py`.
Koyeb should not be configured to run every file in this folder on each reload.

`one_time_rebuild_invoice_read_tables.sql.disabled` is intentionally disabled
because it contains `DROP TABLE` statements for the read snapshot tables. Use it
only manually, once, after backing up any read snapshot data that must be kept.
