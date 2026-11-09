# Changelog

## 1.2.0 (2026-11-09)

- Finds the same bank charge imported twice, for example after reconnecting a bank: same account and amount, exactly the same bank description, dates at most a day apart (`double_import_days`), and the second copy brought in by a later sync. `review` and `scan` show these pairs with the older copy on the left. Merge keeps the older copy, which usually has your edits, adds anything only the newer copy has (a category if the older one has none, notes, tags), and deletes the newer copy. You can also keep either copy, or mark the pair as not a duplicate.
- Copies imported in the same sync are never treated as double imports: those are real separate charges, like two transfers of the same amount on the same day. Copies with the same bank ID are always treated as duplicates.
- `auto` never touches double imports.
- Turn it off with `find_double_imports = false`.

## 1.1.0 (2026-11-09)

- New `explain` command: shows how many of your entries and bank copies are on each account, and lists entries that have a same-amount bank transaction within a month but weren't matched, with the reason (different account, too many days apart, bank copy pending or split, currency, refund vs charge).

## 1.0.0 (2026-11-04)

- First release.
- Finds transactions you entered by hand that later came in again from your bank (or a CSV import), within a date window and on the same account.
- Matches several entries to one bank charge when they add up exactly, for example one entry per category from a receipt; merging splits the bank charge into those categories.
- `review`: shows each pair side by side; merge, keep either copy, mark "not a duplicate", or skip. `--dry-run` to practise.
- `scan`: lists the duplicates and what merging would do, without changing anything.
- `auto`: merges only clear single pairs; preview unless `--apply`.
- `sources`: shows where your recent transactions came from, to check the settings.
- Every change is recorded with full copies of the transactions in `dedupe-history.jsonl`.
