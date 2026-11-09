# Lunch Money Dedupe

Find the transactions you entered by hand in [Lunch Money](https://lunchmoney.app) that later came in again from your bank, see each pair side by side, and merge them or delete one.

Lunch Money can't link a manual transaction to the synced one yet, so entering a purchase right away (to categorize it, add notes or tags, or split it) means a duplicate shows up when the bank syncs. This tool finds those pairs and cleans them up, keeping your details.

```
[1/2]  exact match (score 96) - 2 days apart
             YOURS (entered)                    BANK (imported)
  Date       2026-10-30                         2026-11-01 (+2 days)
  Amount     45.20                              45.20
  Payee      Esso                               ESSO 1234 TORONTO ON
  Account    TD Visa                            TD Visa
  Category   Gas                                (none)
  Notes      receipt in glovebox
  Tags       car
  [m] merge: keep the bank copy with your category, payee, notes and tags; delete yours
  [e] keep yours, delete the bank copy      [b] keep the bank copy, delete yours
  [n] not a duplicate (don't show again)    [s] skip for now      [q] quit
```

**What it does**
- Pairs each of your entries with the bank copy: same account, same amount, within a few days. Payee names don't have to match exactly ("Esso" finds "ESSO 1234 TORONTO ON").
- Also finds **several entries that add up to one charge**, for example a receipt you entered as one transaction per category. Merging splits the bank charge into those categories.
- **Merge** keeps the bank copy, so your balances still reconcile with the bank, and copies your category, payee, notes and tags onto it. Then it deletes your entry.
- Or keep either copy and delete the other, or mark a pair as "not a duplicate" so it's never shown again.

**Safe by default:** `scan` and `review --dry-run` change nothing, pending bank charges and split or grouped transactions are left alone, every change is saved with a full copy of both transactions in `dedupe-history.jsonl`, and your access token stays on your own computer.

---

## Before you start

You need a Lunch Money account and **Python 3.11 or newer** ([python.org/downloads](https://www.python.org/downloads/); on Windows, tick **"Add python.exe to PATH"**). **Mac users:** type `python3` wherever this guide says `python`.

## Step 1: Download and install

1. Click the green **Code** button, then **Download ZIP**, and unzip it into a folder you'll keep.
2. Open a terminal in that folder (Windows: right-click the folder in File Explorer → **Open in Terminal**; Mac: type `cd `, drag the folder into Terminal, press Enter).
3. Install the one requirement:

```
python -m pip install -r requirements.txt
```

## Step 2: Get a Lunch Money access token

In Lunch Money, click the **gear icon** → **Developers**, request a new access token (choose **"I'm developing a personal project"**), and copy it.

If you already use [Lunch Money Auto-Split](https://github.com/PhilopatearSalib/lunch-money-autosplit), you're set: both tools read the same `LUNCHMONEY_TOKEN` environment variable. Otherwise the first `review` asks for the token and where to keep it.

## Step 3: Practise, then review

```
python dedupe.py review --dry-run
```

This shows every duplicate it finds and what each choice *would* do, without changing anything. When you're happy with what it finds:

```
python dedupe.py review
```

Press Enter to accept the suggested choice (merge), or type a letter.

---

## Everyday commands

| Command | What it does |
|---|---|
| `python dedupe.py review` | Go through the duplicates one by one |
| `python dedupe.py review --dry-run` | Same, but only shows what each choice would do |
| `python dedupe.py scan` | List the duplicates and what merging would do; changes nothing |
| `python dedupe.py auto` | Preview merging the clear single pairs without asking |
| `python dedupe.py auto --apply` | Merge them |
| `python dedupe.py sources` | Show where your recent transactions came from |
| `python dedupe.py explain` | Show why your entries weren't matched to a bank copy |

Add `--days 90` to any command to look further back (the default is 60 days).

`auto` only merges single pairs that score 90 or more out of 100, and never touches receipt-style groups or entries with attached files. Everything else is left for `review`. You can run `auto --apply` on a schedule (Task Scheduler on Windows, cron on Mac/Linux) once you trust what it finds.

## Settings

You don't need a config file. To change the defaults, copy [`config.example.toml`](config.example.toml) to `config.toml` and edit it. The most useful settings:

- `days_after` / `days_before`: how far apart the two dates can be (default 10 after, 3 before).
- `amount_tolerance` / `amount_tolerance_percent`: allow a difference in amount, for tips or gas-station pre-authorizations. Exact amounts only by default.
- `imported_sources`: which transaction sources count as the bank copy (default `plaid` and `csv`). Run `python dedupe.py sources` to check your own.
- `keep_payee`: keep your payee name (`"entered"`, the default) or the bank's (`"imported"`).

## Good to know

- **Attached receipts can't be moved.** The Lunch Money API can't move files between transactions. If your entry has attachments, the tool warns you and suggests skipping; choose **[e]** to keep your entry and delete the bank copy instead.
- **Merged receipt groups** become a split of the bank charge. The split parts carry the category, payee and notes of each of your entries; tags on your entries are not copied to the parts.
- **Undoing:** Lunch Money has no undo for deletions through the API. `dedupe-history.jsonl` holds a full copy of every transaction the tool changed or deleted, so you can re-create one by hand if needed.
- **Your data stays local.** The tool talks only to Lunch Money. `dedupe-history.jsonl` and `dedupe-ignored.json` are kept in the tool's folder and are excluded from git.

## Running the tests

```
python -m unittest discover -s tests -v
```

No network access or Lunch Money account is needed: the API is replaced by a fake.

## License

MIT. See [LICENSE](LICENSE).
