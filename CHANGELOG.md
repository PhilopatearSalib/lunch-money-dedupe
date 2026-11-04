# Changelog

## 1.0.0 (2026-11-04)

- First release.
- Finds transactions you entered by hand that later came in again from your bank (or a CSV import), within a date window and on the same account.
- Matches several entries to one bank charge when they add up exactly, for example one entry per category from a receipt; merging splits the bank charge into those categories.
- `review`: shows each pair side by side; merge, keep either copy, mark "not a duplicate", or skip. `--dry-run` to practise.
- `scan`: lists the duplicates and what merging would do, without changing anything.
- `auto`: merges only clear single pairs; preview unless `--apply`.
- `sources`: shows where your recent transactions came from, to check the settings.
- Every change is recorded with full copies of the transactions in `dedupe-history.jsonl`.
