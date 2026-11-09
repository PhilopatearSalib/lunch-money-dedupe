#!/usr/bin/env python3
"""
Lunch Money Dedupe
==================
Find transactions you entered by hand that later came in again from your bank,
show each pair side by side, and merge them or delete one.

Quick start:
    python dedupe.py review            go through the duplicates one by one
    python dedupe.py review --dry-run  practise: shows what each choice would do
    python dedupe.py scan              list the duplicates found; changes nothing
    python dedupe.py auto              preview automatic merging of exact matches
    python dedupe.py auto --apply      merge exact matches without asking
    python dedupe.py sources           show where your recent transactions came from
    python dedupe.py explain           show why your entries weren't matched

https://github.com/PhilopatearSalib/lunch-money-dedupe
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import getpass
import itertools
import json
import os
import platform
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Lunch Money Dedupe needs Python 3.11 or newer. "
             "Download it from https://www.python.org/downloads/")

import tomllib

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("The 'requests' package is missing. Install it with:\n"
             "    python -m pip install requests")

__version__ = "1.2.0"

API_BASE = "https://api.lunchmoney.dev/v2"
MAX_PAGES = 50
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.toml"
TOKEN_FILE = HERE / ".lunchmoney-token"         # same token file as Lunch Money Auto-Split
HISTORY_FILE = HERE / "dedupe-history.jsonl"
IGNORED_FILE = HERE / "dedupe-ignored.json"
TOKEN_ENV = "LUNCHMONEY_TOKEN"
CENT = Decimal("0.01")

DEFAULTS = {
    "lookback_days": 60,
    # A bank copy may show up this many days after (or before) the day you entered it.
    "days_after": 10,
    "days_before": 3,
    # Amounts must match exactly unless you allow a difference (tips, gas pre-authorizations).
    "amount_tolerance": 0.00,
    "amount_tolerance_percent": 0.0,
    # Where bank copies come from (the "source" of a transaction). Everything else counts
    # as entered by you: in the Lunch Money app, through the API, by another tool.
    "imported_sources": ["plaid", "csv"],
    # Several entries (one per category, e.g. from a receipt app) can add up to one bank charge.
    "max_group_parts": 6,
    # Whose payee name to keep when merging: "entered" (yours) or "imported" (the bank's).
    "keep_payee": "entered",
    # Pairs scoring at least this much are merged by `auto --apply`.
    "auto_min_score": 90,
    # Also look for the same bank charge imported twice (for example after reconnecting
    # a bank), and how many days apart the two copies' dates can be.
    "find_double_imports": True,
    "double_import_days": 1,
}

PAYEE_NOISE = {"inc", "ltd", "llc", "the", "store", "stores", "pos", "purchase", "payment",
               "debit", "credit", "visa", "mastercard", "interac", "www", "com", "ca", "on",
               "toronto", "canada", "us", "usa", "and"}


class ApiError(Exception):
    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# Lunch Money API (v2)
# --------------------------------------------------------------------------
class LunchMoney:
    def __init__(self, token: str):
        retry = Retry(total=4, backoff_factor=5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods={"GET"})      # never auto-repeat a change
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._accounts: dict | None = None

    def _call(self, method: str, path: str, **kwargs):
        try:
            r = self.session.request(method, API_BASE + path, timeout=(10, 60), **kwargs)
        except requests.exceptions.RequestException as e:
            raise ApiError(f"Could not reach Lunch Money ({method} {path}). "
                           f"Check your internet connection or try again later.\n"
                           f"  Details: {e}") from e
        if r.status_code == 401:
            raise ApiError(f"Lunch Money rejected the access token ({error_text(r)}).", auth=True)
        if not r.ok:
            raise ApiError(f"Lunch Money returned HTTP {r.status_code} for "
                           f"{method} {path}: {error_text(r)}")
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
        except ValueError as e:
            raise ApiError(f"Unexpected reply from Lunch Money for {method} {path}.") from e
        if isinstance(data, dict) and data.get("error"):
            raise ApiError(f"Lunch Money error for {method} {path}: {data['error']}")
        return data

    def me(self) -> dict:
        return self._call("GET", "/me")

    def categories(self) -> dict[int, str]:
        cats = self._call("GET", "/categories", params={"format": "flattened"})["categories"]
        return {c["id"]: c["name"] for c in cats}

    def tags(self) -> dict[int, str]:
        return {t["id"]: t["name"] for t in self._call("GET", "/tags").get("tags", [])}

    def accounts(self) -> dict:
        """Maps ("plaid" | "manual", id) to a readable account name."""
        if self._accounts is None:
            names = {}
            for kind, path, key in (("plaid", "/plaid_accounts", "plaid_accounts"),
                                    ("manual", "/manual_accounts", "manual_accounts")):
                for acc in self._call("GET", path).get(key, []):
                    names[(kind, acc["id"])] = acc.get("display_name") or " ".join(
                        x for x in (acc.get("institution_name"), acc.get("name")) if x)
            self._accounts = names
        return self._accounts

    def transactions(self, start: dt.date, end: dt.date) -> list[dict]:
        """Every non-pending transaction in the date range, following pagination."""
        params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            data = self._call("GET", "/transactions", params={**params, "offset": len(out)})
            batch = data.get("transactions", [])
            out.extend(batch)
            if not data.get("has_more") or not batch:
                break
        else:
            raise ApiError(f"Too many transactions to page through ({len(out)}+). "
                           "Use a smaller --days.")
        names = self.accounts()
        for t in out:
            if t.get("plaid_account_id"):
                t["_account"] = names.get(("plaid", t["plaid_account_id"]), "")
            elif t.get("manual_account_id"):
                t["_account"] = names.get(("manual", t["manual_account_id"]), "")
            else:
                t["_account"] = ""
        return out

    def update(self, transaction_id: int, changes: dict) -> dict:
        return self._call("PUT", f"/transactions/{transaction_id}", json=changes)

    def delete(self, transaction_id: int) -> dict:
        return self._call("DELETE", f"/transactions/{transaction_id}")

    def split(self, transaction_id: int, parts: list[dict]) -> dict:
        return self._call("POST", f"/transactions/split/{transaction_id}",
                          json={"child_transactions": parts})


def error_text(r) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:300] or r.reason or "no details"
    if isinstance(data, dict):
        details = [e.get("errMsg") for e in data.get("errors") or []
                   if isinstance(e, dict) and e.get("errMsg")]
        text = data.get("message") or data.get("error") or ""
        if details:
            text = f"{text}: {'; '.join(details)}" if text else "; ".join(details)
        if text:
            return str(text)
    return r.text[:300]


# --------------------------------------------------------------------------
# Token (shared with Lunch Money Auto-Split)
# --------------------------------------------------------------------------
TOKEN_HELP = """
To connect, you need a Lunch Money access token:
  1. In Lunch Money, click the gear icon and open the Developers tab.
  2. Type a name such as "dedupe", choose "I'm developing a personal project",
     and request a new access token.
  3. Copy the token (Lunch Money shows it only once).
Keep the token private: anyone who has it can see and change your budget.
"""


def get_token() -> str | None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    return None


def ask_for_token() -> str:
    print(TOKEN_HELP)
    while True:
        token = getpass.getpass("Paste your token and press Enter "
                                "(nothing appears while you paste - that's normal): ").strip()
        if not token:
            continue
        try:
            LunchMoney(token).me()
        except ApiError as e:
            print(f"  {e}")
            if not e.auth:
                raise
            continue
        break
    windows = platform.system() == "Windows"
    print("\nWhere should the token be kept on this computer?")
    print(f"  1) Environment variable {TOKEN_ENV} (recommended)")
    print(f"  2) Token file in this folder ({TOKEN_FILE.name}) - convenient, but plain text")
    print("  3) Don't keep it - ask me again next time")
    choice = ask_choice("Choose 1, 2 or 3", ["1", "2", "3"], "1")
    if choice == "1":
        if windows and subprocess.run(["setx", TOKEN_ENV, token],
                                      capture_output=True).returncode == 0:
            print(f"  Saved as {TOKEN_ENV}. New terminal windows will use it.")
        else:
            profile = "~/.zshrc" if platform.system() == "Darwin" else "~/.bashrc"
            print(f"  Add this line to {profile}, then open a new terminal:\n"
                  f"      export {TOKEN_ENV}=\"your-token\"")
    elif choice == "2":
        TOKEN_FILE.write_text(token, encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
        print(f"  Saved to {TOKEN_FILE}. Keep it private and never share or commit it.")
    return token


def get_client(interactive: bool) -> LunchMoney:
    token = get_token()
    if not token:
        if not interactive:
            raise ConfigError(f"No Lunch Money access token found. Set {TOKEN_ENV}, "
                              "or run  python dedupe.py review  once to enter it.")
        token = ask_for_token()
    return LunchMoney(token)


# --------------------------------------------------------------------------
# Config, ignored pairs, history
# --------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            with path.open("rb") as f:
                cfg.update(tomllib.load(f))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path.name} has a formatting error: {e}") from e
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"Unknown setting(s) in {path.name}: {', '.join(sorted(unknown))}")
    if cfg["keep_payee"] not in ("entered", "imported"):
        raise ConfigError('keep_payee must be "entered" or "imported"')
    cfg["imported_sources"] = [str(s).lower() for s in cfg["imported_sources"]]
    for key in ("days_after", "days_before", "max_group_parts", "lookback_days",
                "double_import_days"):
        if not isinstance(cfg[key], int) or cfg[key] < 0:
            raise ConfigError(f"{key} must be a whole number of 0 or more")
    return cfg


def pair_key(imported: dict, entered: list[dict]) -> str:
    return ":".join(str(i) for i in [imported["id"], *sorted(t["id"] for t in entered)])


def load_ignored(path: Path = IGNORED_FILE) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError):
        return set()


def save_ignored(keys: set[str], path: Path = IGNORED_FILE) -> None:
    path.write_text(json.dumps(sorted(keys), indent=1), encoding="utf-8")


def record(action: str, match: "Match", path: Path = HISTORY_FILE, **extra) -> None:
    """Keep a full copy of what was changed or deleted, so it can be re-created by hand."""
    entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), "action": action,
             "imported": strip_private(match.imported),
             "entered": [strip_private(t) for t in match.entered], **extra}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def strip_private(t: dict) -> dict:
    return {k: v for k, v in t.items() if not k.startswith("_")}


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def money(t: dict) -> Decimal:
    try:
        return Decimal(str(t.get("amount", "0"))).quantize(CENT)
    except InvalidOperation:
        return Decimal(0)


def day(t: dict) -> dt.date:
    return dt.date.fromisoformat(str(t["date"])[:10])


def account_key(t: dict):
    if t.get("plaid_account_id"):
        return ("plaid", t["plaid_account_id"])
    if t.get("manual_account_id"):
        return ("manual", t["manual_account_id"])
    return None


def tag_ids(t: dict) -> list:
    if t.get("tag_ids") is not None:
        return list(t["tag_ids"])
    return [x["id"] for x in t.get("tags") or [] if isinstance(x, dict) and "id" in x]


def is_imported(t: dict, cfg: dict) -> bool:
    source = str(t.get("source") or "").lower()
    if source:
        return source in cfg["imported_sources"]
    return bool(t.get("external_id"))           # older data without a source


def skip(t: dict) -> bool:
    """Split pieces, groups and pending charges are left alone."""
    return bool(t.get("is_pending") or t.get("split_parent_id") or t.get("is_split_parent")
                or t.get("group_parent_id") or t.get("is_group_parent"))


def payee_words(text: str) -> list[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return [w for w in words if len(w) > 1 and w not in PAYEE_NOISE]


def payee_similarity(a: str, b: str) -> float:
    """0..1. 'Esso gas' vs 'ESSO 1234 TORONTO ON' scores high; unrelated names score low."""
    if (a or "").strip() and (a or "").strip().lower() == (b or "").strip().lower():
        return 1.0
    wa, wb = payee_words(a), payee_words(b)
    if not wa or not wb:
        return 0.0
    shared = sum(1 for x in wa if any(y.startswith(x) or x.startswith(y)
                                      for y in wb if min(len(x), len(y)) >= 3))
    overlap = shared / min(len(wa), len(wb))
    ratio = difflib.SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio()
    return max(overlap, ratio)


def same_currency(a: dict, b: dict) -> bool:
    return str(a.get("currency") or "").lower() == str(b.get("currency") or "").lower()


def date_ok(entered: dict, imported: dict, cfg: dict) -> bool:
    gap = (day(imported) - day(entered)).days
    return -cfg["days_before"] <= gap <= cfg["days_after"]


def account_ok(entered: dict, imported: dict) -> bool:
    """An entry made without an account can match any account."""
    ek = account_key(entered)
    return ek is None or ek == account_key(imported)


def amount_ok(a: Decimal, b: Decimal, cfg: dict) -> bool:
    if (a > 0) != (b > 0):
        return False
    allowed = max(Decimal(str(cfg["amount_tolerance"])),
                  abs(b) * Decimal(str(cfg["amount_tolerance_percent"])) / 100)
    return abs(a - b) <= allowed + Decimal("0.004")


@dataclass
class Match:
    imported: dict
    entered: list[dict]
    score: int
    reasons: list[str] = field(default_factory=list)
    # "entry": your entry + its bank copy (imported = bank copy, entered = your entries)
    # "double": the same charge imported twice (imported = older copy, kept;
    #           entered = [newer copy])
    kind: str = "entry"

    @property
    def is_double(self) -> bool:
        return self.kind == "double"

    @property
    def is_group(self) -> bool:
        return len(self.entered) > 1

    @property
    def label(self) -> str:
        if self.is_double:
            return "likely double import" if self.score >= 90 else "possible double import"
        if self.score >= 90:
            return "exact match"
        return "likely match" if self.score >= 70 else "possible match"


def score_pair(entered: list[dict], imported: dict) -> tuple[int, list[str]]:
    reasons = []
    score = 100
    total = sum(money(t) for t in entered)
    diff = abs(total - money(imported))
    if diff:
        score -= min(30, 5 + int(diff / max(abs(money(imported)), Decimal(1)) * 100))
        reasons.append(f"amounts differ by {diff:.2f}")
    gaps = {(day(imported) - day(t)).days for t in entered}
    gap = max(gaps, key=abs)
    if gap:
        score -= min(25, 2 * abs(gap))
        reasons.append(f"{abs(gap)} day{'s' if abs(gap) != 1 else ''} apart")
    if any(account_key(t) is None for t in entered):
        score -= 5
        reasons.append("no account on your entry")
    sim = max(payee_similarity(t.get("payee", ""), imported.get("payee", "")) for t in entered)
    if imported.get("original_name"):
        sim = max(sim, *(payee_similarity(t.get("payee", ""), imported["original_name"])
                         for t in entered))
    if sim < 0.5:
        score -= int((0.5 - sim) * 40)          # up to -20
        reasons.append("payee names look different")
    if len(entered) > 1:
        score -= 3
        if len({day(t) for t in entered}) > 1:
            score -= 10
            reasons.append("your entries are on different days")
    return max(score, 0), reasons


def find_matches(transactions: list[dict], cfg: dict,
                 ignored: set[str] | None = None) -> list[Match]:
    ignored = ignored or set()
    usable = [t for t in transactions if not skip(t) and money(t) != 0]
    imported = [t for t in usable if is_imported(t, cfg)]
    entered = [t for t in usable if not is_imported(t, cfg)]

    candidates: list[Match] = []
    for imp in imported:
        pool = [e for e in entered if same_currency(e, imp) and date_ok(e, imp, cfg)
                and account_ok(e, imp)]
        for e in pool:
            if amount_ok(money(e), money(imp), cfg):
                candidates.append(Match(imp, [e], *score_pair([e], imp)))
        # Several entries (e.g. one per category from a receipt) adding up to one charge.
        target = money(imp)
        parts = [e for e in pool if (money(e) > 0) == (target > 0)
                 and abs(money(e)) < abs(target)]
        parts.sort(key=lambda e: abs((day(imp) - day(e)).days))
        parts = parts[:12]
        for size in range(2, min(cfg["max_group_parts"], len(parts)) + 1):
            for combo in itertools.combinations(parts, size):
                if sum(money(e) for e in combo) == target:
                    candidates.append(Match(imp, list(combo), *score_pair(list(combo), imp)))

    # Best first; each transaction ends up in at most one match.
    candidates.sort(key=lambda m: (-m.score, len(m.entered), day(m.imported)))
    used: set = set()
    chosen: list[Match] = []
    for m in candidates:
        ids = {m.imported["id"], *(t["id"] for t in m.entered)}
        if ids & used or pair_key(m.imported, m.entered) in ignored:
            continue
        used |= ids
        chosen.append(m)
    chosen.sort(key=lambda m: day(m.imported))
    return chosen


def created(t: dict) -> str:
    return str(t.get("created_at") or "")


def older_first(a: dict, b: dict) -> tuple[dict, dict]:
    """The copy that was imported first (it usually has your edits), then the other."""
    ka, kb = (created(a), a["id"]), (created(b), b["id"])
    return (a, b) if ka <= kb else (b, a)


def bank_name(t: dict) -> str:
    return t.get("original_name") or t.get("payee") or ""


def hours_between(a: str, b: str) -> float | None:
    try:
        ta, tb = (dt.datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b))
    except ValueError:
        return None
    return abs((ta - tb).total_seconds()) / 3600


def same_bank_id(a: dict, b: dict) -> bool:
    return bool(a.get("external_id")) and a.get("external_id") == b.get("external_id")


def same_name(a: dict, b: dict) -> bool:
    """The bank's own description, exactly (reference numbers and tickers included)."""
    na, nb = (" ".join(bank_name(t).lower().split()) for t in (a, b))
    return bool(na) and na == nb


def score_double(old: dict, new: dict) -> tuple[int, list[str]] | None:
    """None when the pair doesn't look like a double import at all."""
    if same_bank_id(old, new):
        return 100, ["same bank ID"]
    if not same_name(old, new):
        return None
    hours = hours_between(created(old), created(new))
    if hours is not None and hours < 24:
        # Imported in the same sync: that's two real charges, not a repeat import.
        return None
    score, reasons = 100, []
    gap = abs((day(new) - day(old)).days)
    if gap:
        score -= 15 * gap
        reasons.append(f"{gap} day{'s' if gap != 1 else ''} apart")
    if hours is None:
        score -= 30
        reasons.append("couldn't tell when each copy was imported")
    else:
        reasons.append(f"second copy imported {int(hours // 24)} days later")
    return max(score, 0), reasons


def find_double_imports(transactions: list[dict], cfg: dict, ignored: set[str] | None = None,
                        used: set | None = None) -> list[Match]:
    """The same bank charge imported twice: same account, amount and bank description,
    dates close, and the second copy brought in by a later sync (e.g. after a reconnect)."""
    ignored, used = ignored or set(), used or set()
    pool = [t for t in transactions if is_imported(t, cfg) and not skip(t)
            and money(t) != 0 and account_key(t) is not None and t["id"] not in used]
    groups: dict = {}
    for t in pool:
        groups.setdefault((account_key(t), money(t), str(t.get("currency") or "").lower()),
                          []).append(t)
    candidates = []
    for items in groups.values():
        for a, b in itertools.combinations(items, 2):
            if abs((day(a) - day(b)).days) > cfg["double_import_days"]:
                continue
            old, new = older_first(a, b)
            scored = score_double(old, new)
            if scored:
                candidates.append(Match(old, [new], *scored, kind="double"))
    candidates.sort(key=lambda m: (-m.score, day(m.imported)))
    taken: set = set()
    chosen = []
    for m in candidates:
        ids = {m.imported["id"], m.entered[0]["id"]}
        if ids & taken or pair_key(m.imported, m.entered) in ignored:
            continue
        taken |= ids
        chosen.append(m)
    return chosen


def find_all(transactions: list[dict], cfg: dict, ignored: set[str] | None = None) -> list[Match]:
    """Your entries with their bank copies, then charges imported twice."""
    matches = find_matches(transactions, cfg, ignored)
    if cfg["find_double_imports"]:
        used = {i for m in matches for i in (m.imported["id"], *(t["id"] for t in m.entered))}
        matches += find_double_imports(transactions, cfg, ignored, used)
    return matches


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def merged_changes(m: Match, cfg: dict) -> dict:
    """What the bank copy gets from your entry when a single pair is merged.

    For a double import, the older copy only gets what it's missing from the newer one.
    """
    e, imp = m.entered[0], m.imported
    changes: dict = {}
    if m.is_double:
        if e.get("category_id") and not imp.get("category_id"):
            changes["category_id"] = e["category_id"]
        mine, theirs = (e.get("notes") or "").strip(), (imp.get("notes") or "").strip()
        if mine and mine not in theirs:
            changes["notes"] = f"{theirs} | {mine}" if theirs else mine
        tags = list(dict.fromkeys([*tag_ids(imp), *tag_ids(e)]))
        if tags != tag_ids(imp):
            changes["tag_ids"] = tags
        return changes
    if e.get("category_id") and e.get("category_id") != imp.get("category_id"):
        changes["category_id"] = e["category_id"]
    if cfg["keep_payee"] == "entered" and e.get("payee") and e["payee"] != imp.get("payee"):
        changes["payee"] = e["payee"]
    mine, theirs = (e.get("notes") or "").strip(), (imp.get("notes") or "").strip()
    if mine and mine not in theirs:
        changes["notes"] = f"{theirs} | {mine}" if theirs else mine
    tags = list(dict.fromkeys([*tag_ids(imp), *tag_ids(e)]))
    if tags != tag_ids(imp):
        changes["tag_ids"] = tags
    return changes


def split_parts(m: Match, cfg: dict) -> list[dict]:
    parts = []
    for e in m.entered:
        p = {"amount": str(money(e)), "category_id": e.get("category_id")}
        if cfg["keep_payee"] == "entered" and e.get("payee"):
            p["payee"] = e["payee"]
        if e.get("notes"):
            p["notes"] = e["notes"]
        parts.append({k: v for k, v in p.items() if v is not None})
    return parts


def attachments(t: dict) -> int:
    files = t.get("files") or t.get("attachments") or []
    return len(files) if isinstance(files, list) else 0


def do_merge(lm: LunchMoney, m: Match, cfg: dict) -> str:
    """Copy your details onto the bank copy, then delete your entry (or entries)."""
    record("merge", m)
    if m.is_group:
        lm.split(m.imported["id"], split_parts(m, cfg))
        done = f"split the bank charge into {len(m.entered)} parts"
    else:
        changes = merged_changes(m, cfg)
        if changes:
            lm.update(m.imported["id"], changes)
        if m.is_double:
            done = "updated the older copy" if changes else "older copy already had everything"
        else:
            done = "updated the bank copy" if changes else "bank copy already had your details"
    failed = []
    for e in m.entered:
        try:
            lm.delete(e["id"])
        except ApiError as err:
            failed.append(f"{e['id']} ({err})")
    if failed:
        what = "the newer copy" if m.is_double else "your entry"
        return (f"{done}, but couldn't delete {what}: {', '.join(failed)}. "
                "Delete it in Lunch Money by hand.")
    if m.is_double:
        return f"{done} and deleted the newer copy"
    return f"{done} and deleted your entr{'ies' if m.is_group else 'y'}"


def do_delete(lm: LunchMoney, m: Match, which: str) -> str:
    targets = [m.imported] if which == "imported" else m.entered
    record(f"delete_{which}", m)
    for t in targets:
        lm.delete(t["id"])
    if m.is_double:
        return "deleted the older copy" if which == "imported" else "deleted the newer copy"
    return "deleted the bank copy" if which == "imported" else \
        f"deleted your entr{'ies' if m.is_group else 'y'}"


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
class Names:
    def __init__(self, categories: dict[int, str], tags: dict[int, str]):
        self.categories, self.tags = categories, tags

    def category(self, cid) -> str:
        return self.categories.get(cid, f"#{cid}") if cid else "(none)"

    def tag_list(self, ids) -> str:
        return ", ".join(self.tags.get(i, f"#{i}") for i in ids or []) or ""


def cut(text, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def show_match(m: Match, names: Names, n: int, total: int, cfg: dict) -> None:
    imp = m.imported
    print(f"\n[{n}/{total}]  {m.label} (score {m.score})"
          + (f" - {', '.join(m.reasons)}" if m.reasons else ""))
    if m.is_group:
        print(f"  BANK      {imp['date']}  {money(imp):>10}  {cut(imp.get('payee'), 30)}"
              f"  [{cut(imp.get('_account'), 22)}]  {names.category(imp.get('category_id'))}")
        print(f"  YOURS     {len(m.entered)} entries adding up to the same amount:")
        for e in m.entered:
            print(f"            {e['date']}  {money(e):>10}  {cut(e.get('payee'), 30)}"
                  f"  {names.category(e.get('category_id'))}"
                  + (f"  ({cut(e.get('notes'), 30)})" if e.get("notes") else ""))
        return
    e = m.entered[0]
    gap = (day(imp) - day(e)).days
    if m.is_double:
        rows = [
            ("Date", imp["date"], e["date"]),
            ("Amount", money(imp), money(e)),
            ("Payee", imp.get("payee"), e.get("payee")),
            ("Bank name", imp.get("original_name"), e.get("original_name")),
            ("Account", imp.get("_account"), e.get("_account")),
            ("Category", names.category(imp.get("category_id")),
             names.category(e.get("category_id"))),
            ("Notes", imp.get("notes"), e.get("notes")),
            ("Tags", names.tag_list(tag_ids(imp)), names.tag_list(tag_ids(e))),
            ("Imported", created(imp)[:10], created(e)[:10]),
        ]
        print(f"  {'':10} {'OLDER COPY (kept)':34} NEWER COPY")
        for label, a, b in rows:
            if a in (None, "") and b in (None, ""):
                continue
            print(f"  {label:10} {cut(a, 34):34} {cut(b, 40)}")
        if attachments(e):
            print(f"  ! The newer copy has {attachments(e)} attached file(s). Merging can't move "
                  "them; choose [w] to keep the newer copy instead.")
        return
    rows = [
        ("Date", e["date"], f"{imp['date']}" + (f"  ({gap:+d} days)" if gap else "")),
        ("Amount", money(e), money(imp)),
        ("Payee", e.get("payee"), imp.get("payee")),
        ("Account", e.get("_account") or "(none)", imp.get("_account")),
        ("Category", names.category(e.get("category_id")), names.category(imp.get("category_id"))),
        ("Notes", e.get("notes"), imp.get("notes")),
        ("Tags", names.tag_list(tag_ids(e)), names.tag_list(tag_ids(imp))),
        ("Source", e.get("source"), imp.get("source")),
    ]
    print(f"  {'':10} {'YOURS (entered)':34} BANK (imported)")
    for label, a, b in rows:
        if a in (None, "") and b in (None, ""):
            continue
        print(f"  {label:10} {cut(a, 34):34} {cut(b, 40)}")
    if attachments(e):
        print(f"  ! Your entry has {attachments(e)} attached file(s). Merging can't move them "
              "and deletes your entry; choose [e] to keep yours instead.")


def preview_merge(m: Match, names: Names, cfg: dict) -> str:
    if m.is_group:
        parts = ", ".join(f"{names.category(p.get('category_id'))} {p['amount']}"
                          for p in split_parts(m, cfg))
        return f"would split the bank charge into: {parts}; then delete your entries"
    changes = merged_changes(m, cfg)
    if m.is_double:
        what = ", ".join(f"{k.replace('_id', '').replace('_ids', '')} = "
                         f"{names.category(v) if k == 'category_id' else names.tag_list(v) if k == 'tag_ids' else v!r}"
                         for k, v in changes.items())
        return (f"would set on the older copy: {what}; then delete the newer copy" if what
                else "would delete the newer copy (the older one already has everything)")
    shown = {k: (names.category(v) if k == "category_id" else
                 names.tag_list(v) if k == "tag_ids" else v) for k, v in changes.items()}
    what = ", ".join(f"{k.replace('_id', '').replace('_ids', '')} = {v!r}"
                     for k, v in shown.items()) or "nothing to copy"
    return f"would set on the bank copy: {what}; then delete your entry"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def fetch(lm: LunchMoney, days: int) -> list[dict]:
    end = dt.date.today()
    return lm.transactions(end - dt.timedelta(days=days), end)


def ask_choice(prompt: str, options: list[str], default: str) -> str:
    while True:
        s = input(f"{prompt} [{default}]: ").strip().lower() or default
        if s in options:
            return s
        print(f"  Please type one of: {', '.join(options)}")


DOUBLE_MENU = """  [m] merge: keep the older copy, adding anything only the newer one has; delete the newer
  [o] keep the older copy, delete the newer [w] keep the newer copy, delete the older
  [n] not a duplicate (don't show again)    [s] skip for now      [q] quit"""

REVIEW_MENU = """  [m] merge: keep the bank copy with your category, payee, notes and tags; delete yours
  [e] keep yours, delete the bank copy      [b] keep the bank copy, delete yours
  [n] not a duplicate (don't show again)    [s] skip for now      [q] quit"""


def cmd_review(args, cfg: dict) -> int:
    lm = get_client(interactive=True)
    days = args.days or cfg["lookback_days"]
    print(f"Looking for duplicates in the last {days} days…")
    names = Names(lm.categories(), safe_tags(lm))
    ignored = load_ignored()
    matches = find_all(fetch(lm, days), cfg, ignored)
    if not matches:
        print("No duplicates found.")
        return 0
    print(f"Found {len(matches)} possible duplicate{'s' if len(matches) != 1 else ''}."
          + ("  (dry run: nothing will change)" if args.dry_run else ""))
    counts = Counter()
    for n, m in enumerate(matches, 1):
        show_match(m, names, n, len(matches), cfg)
        if m.is_double:
            print(DOUBLE_MENU)
            default = "m" if m.score >= 90 and not attachments(m.entered[0]) else "s"
            choice = ask_choice("Choose", ["m", "o", "w", "n", "s", "q"], default)
            choice = {"o": "b", "w": "e"}.get(choice, choice)   # same actions as below
        else:
            print(REVIEW_MENU)
            default = "s" if attachments(m.entered[0]) else "m"
            choice = ask_choice("Choose", ["m", "e", "b", "n", "s", "q"], default)
        if choice == "q":
            break
        if choice == "s":
            counts["skipped"] += 1
            continue
        if choice == "n":
            ignored.add(pair_key(m.imported, m.entered))
            if not args.dry_run:
                save_ignored(ignored)
            counts["marked not duplicate"] += 1
            print("  OK, won't show this pair again.")
            continue
        if args.dry_run:
            if m.is_double:
                what = ("would delete the older copy" if choice == "e" else
                        "would delete the newer copy")
            else:
                what = "would delete the bank copy" if choice == "e" else "would delete your entry"
            print("  (dry run) " + (preview_merge(m, names, cfg) if choice == "m" else what))
            continue
        try:
            if choice == "m":
                print("  " + do_merge(lm, m, cfg))
                counts["merged"] += 1
            else:
                print("  " + do_delete(lm, m, "imported" if choice == "e" else "entered"))
                counts["deleted"] += 1
        except ApiError as err:
            print(f"  Didn't work: {err}")
            counts["failed"] += 1
    if counts:
        print("\nDone: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + ".")
    if counts["merged"] or counts["deleted"]:
        print(f"A copy of everything changed or deleted is in {HISTORY_FILE.name}.")
    return 1 if counts["failed"] else 0


def cmd_scan(args, cfg: dict) -> int:
    lm = get_client(interactive=False)
    days = args.days or cfg["lookback_days"]
    names = Names(lm.categories(), safe_tags(lm))
    matches = find_all(fetch(lm, days), cfg, load_ignored())
    for n, m in enumerate(matches, 1):
        show_match(m, names, n, len(matches), cfg)
        print("  -> " + preview_merge(m, names, cfg))
    print(f"\n{len(matches)} possible duplicate(s) in the last {days} days. Nothing was changed.")
    return 0


def cmd_auto(args, cfg: dict) -> int:
    """Merge only clear, single-entry matches; everything else is left for review."""
    lm = get_client(interactive=False)
    days = args.days or cfg["lookback_days"]
    names = Names(lm.categories(), safe_tags(lm))
    matches = find_matches(fetch(lm, days), cfg, load_ignored())
    sure = [m for m in matches if m.score >= cfg["auto_min_score"] and not m.is_group
            and not attachments(m.entered[0])]
    failed = 0
    for m in sure:
        e = m.entered[0]
        line = f"{m.imported['date']}  {money(m.imported):>10}  {cut(m.imported.get('payee'), 30)}" \
               f"  <=  {cut(e.get('payee'), 30)} ({names.category(e.get('category_id'))})"
        if not args.apply:
            print(f"{line}\n    {preview_merge(m, names, cfg)}")
            continue
        try:
            print(f"{line}\n    {do_merge(lm, m, cfg)}")
        except ApiError as err:
            failed += 1
            print(f"{line}\n    Didn't work: {err}")
    left = len(matches) - len(sure)
    verb = "Merged" if args.apply else "Would merge"
    print(f"\n{verb} {len(sure) - failed} exact match(es)."
          + (f" {left} other possible duplicate(s) need a look: python dedupe.py review"
             if left else "")
          + ("" if args.apply else "\nThis was a preview. Add --apply to merge them."))
    return 1 if failed else 0


def near_misses(transactions: list[dict], cfg: dict, window: int = 31) -> list[tuple]:
    """Your entries that have a bank transaction with the same amount nearby, but
    weren't matched, with the reasons why."""
    entered = [t for t in transactions if not is_imported(t, cfg) and not skip(t)
               and money(t) != 0]
    imported = [t for t in transactions if is_imported(t, cfg)]
    out = []
    for e in entered:
        for imp in imported:
            if abs(money(imp)) != abs(money(e)) or abs((day(imp) - day(e)).days) > window:
                continue
            why = []
            if imp.get("is_pending"):
                why.append("bank copy is still pending")
            if imp.get("split_parent_id") or imp.get("is_split_parent"):
                why.append("bank copy is split")
            if imp.get("group_parent_id") or imp.get("is_group_parent"):
                why.append("bank copy is in a group")
            if (money(e) > 0) != (money(imp) > 0):
                why.append("one is a charge, the other a refund")
            if not same_currency(e, imp):
                why.append(f"currency {e.get('currency')} vs {imp.get('currency')}")
            if not account_ok(e, imp):
                why.append(f"different account ({e.get('_account') or '?'} vs "
                           f"{imp.get('_account') or '?'})")
            if not date_ok(e, imp, cfg):
                why.append(f"{(day(imp) - day(e)).days:+d} days apart "
                           f"(allowed -{cfg['days_before']} to +{cfg['days_after']})")
            if why:
                out.append((e, imp, why))
    return out


def cmd_explain(args, cfg: dict) -> int:
    """Why entries weren't matched: where they live and the closest bank transactions."""
    lm = get_client(interactive=False)
    days = args.days or cfg["lookback_days"]
    txs = fetch(lm, days)
    matches = find_matches(txs, cfg, load_ignored())
    entered = [t for t in txs if not is_imported(t, cfg) and not skip(t)]
    print(f"{len(txs)} transactions in the last {days} days; {len(entered)} entered by you; "
          f"{len(matches)} matched as duplicates.\n")
    print("Your entries by account:")
    for name, n in Counter(t.get("_account") or "(no account)" for t in entered).most_common():
        print(f"  {n:5}  {name}")
    print("Bank copies by account:")
    for name, n in Counter(t.get("_account") or "(no account)" for t in txs
                           if is_imported(t, cfg)).most_common():
        print(f"  {n:5}  {name}")
    misses = near_misses(txs, cfg)
    print(f"\nEntries with a same-amount bank transaction within a month that didn't match "
          f"({len(misses)}):")
    for e, imp, why in misses[: args.limit]:
        print(f"  YOURS {e['date']} {money(e):>9}  {cut(e.get('payee'), 24):24} "
              f"[{cut(e.get('_account') or 'no account', 18)}]")
        print(f"  BANK  {imp['date']} {money(imp):>9}  {cut(imp.get('payee'), 24):24} "
              f"[{cut(imp.get('_account'), 18)}]")
        print(f"        -> {'; '.join(why)}")
    if len(misses) > args.limit:
        print(f"  … and {len(misses) - args.limit} more (use --limit)")
    if not misses:
        print("  none - your entries don't have bank transactions with the same amount nearby.")
    return 0


def cmd_sources(args, cfg: dict) -> int:
    lm = get_client(interactive=False)
    days = args.days or cfg["lookback_days"]
    txs = fetch(lm, days)
    counts = Counter(str(t.get("source") or "(none)") for t in txs)
    print(f"Where your {len(txs)} transactions from the last {days} days came from:")
    for source, n in counts.most_common():
        if source == "(none)":
            kind = "bank copy if it has a bank ID, otherwise entered by you"
        else:
            kind = "bank copy" if source.lower() in cfg["imported_sources"] else "entered by you"
        print(f"  {source:12} {n:5}   treated as: {kind}")
    print("\nIf a source is in the wrong group, change imported_sources in config.toml.")
    return 0


def safe_tags(lm: LunchMoney) -> dict[int, str]:
    try:
        return lm.tags()
    except ApiError:
        return {}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"path to the config file (default: {DEFAULT_CONFIG.name})")
    common.add_argument("--days", type=int, help="how many days back to look")
    parser = argparse.ArgumentParser(
        prog="dedupe.py",
        description="Find, show and merge duplicate Lunch Money transactions "
                    "(your manual entry + the copy from your bank).",
        epilog="New here? Start with:  python dedupe.py review --dry-run")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    r = sub.add_parser("review", parents=[common], help="go through duplicates one by one")
    r.add_argument("--dry-run", action="store_true", help="show what each choice would do")
    sub.add_parser("scan", parents=[common], help="list duplicates; changes nothing")
    a = sub.add_parser("auto", parents=[common], help="merge exact matches (preview unless --apply)")
    a.add_argument("--apply", action="store_true", help="actually merge them")
    sub.add_parser("sources", parents=[common], help="show where recent transactions came from")
    ex = sub.add_parser("explain", parents=[common], help="show why entries weren't matched")
    ex.add_argument("--limit", type=int, default=25, help="how many near misses to show")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handlers = {"review": cmd_review, "scan": cmd_scan, "auto": cmd_auto, "sources": cmd_sources,
                "explain": cmd_explain}
    try:
        cfg = load_config(Path(args.config))
        return handlers[args.command](args, cfg) or 0
    except (ApiError, ConfigError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
