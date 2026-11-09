"""Tests for Lunch Money Dedupe.

Run them with:  python -m unittest discover -s tests -v
No network access or Lunch Money account is needed: the API is replaced by a fake.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dedupe as d  # noqa: E402

CFG = dict(d.DEFAULTS)
_next = iter(range(1000, 99999))


def tx(amount, date, payee, source="manual", account=7, **extra):
    t = {"id": next(_next), "amount": str(amount), "currency": "cad", "date": date,
         "payee": payee, "source": source, "plaid_account_id": account,
         "manual_account_id": None, "category_id": None, "notes": None, "tag_ids": [],
         "_account": "TD Visa" if account else ""}
    t.update(extra)
    return t


def bank(amount, date, payee, **extra):
    extra.setdefault("external_id", f"x{next(_next)}")
    return tx(amount, date, payee, source="plaid", **extra)


class FakeLM:
    def __init__(self):
        self.calls = []

    def update(self, i, changes):
        self.calls.append(("update", i, changes))

    def delete(self, i):
        self.calls.append(("delete", i))

    def split(self, i, parts):
        self.calls.append(("split", i, parts))


# ---------------------------------------------------------------------------
class TestMatching(unittest.TestCase):
    def test_simple_pair(self):
        mine = tx("45.20", "2026-10-30", "Esso gas", category_id=3)
        theirs = bank("45.20", "2026-11-01", "ESSO 1234 TORONTO ON")
        [m] = d.find_matches([mine, theirs], CFG)
        self.assertEqual(m.imported["id"], theirs["id"])
        self.assertEqual(m.entered, [mine])
        self.assertGreaterEqual(m.score, 90)

    def test_different_amount_no_match(self):
        self.assertEqual(d.find_matches([tx("45.20", "2026-10-30", "Esso"),
                                         bank("45.21", "2026-10-30", "ESSO")], CFG), [])

    def test_tolerance_allows_tip(self):
        cfg = {**CFG, "amount_tolerance_percent": 25}
        [m] = d.find_matches([tx("40.00", "2026-10-30", "Pizza place"),
                              bank("47.00", "2026-10-31", "PIZZA PLACE")], cfg)
        self.assertIn("amounts differ by 7.00", m.reasons)

    def test_outside_date_window(self):
        self.assertEqual(d.find_matches([tx("10", "2026-10-01", "A"),
                                         bank("10", "2026-10-20", "A")], CFG), [])

    def test_different_account_no_match(self):
        self.assertEqual(d.find_matches([tx("10", "2026-10-01", "A", account=1),
                                         bank("10", "2026-10-01", "A", account=2)], CFG), [])

    def test_entry_without_account_matches_any(self):
        mine = tx("10", "2026-10-01", "Coffee", account=None)
        self.assertEqual(len(d.find_matches([mine, bank("10", "2026-10-01", "COFFEE")], CFG)), 1)

    def test_refund_does_not_match_charge(self):
        self.assertEqual(d.find_matches([tx("-10", "2026-10-01", "A"),
                                         bank("10", "2026-10-01", "A")], CFG), [])

    def test_two_bank_copies_two_entries_pair_up_once(self):
        a1, a2 = tx("5.00", "2026-10-01", "Tim Hortons"), tx("5.00", "2026-10-03", "Tim Hortons")
        b1, b2 = bank("5.00", "2026-10-01", "TIM HORTONS"), bank("5.00", "2026-10-03", "TIM HORTONS")
        ms = d.find_matches([a1, a2, b1, b2], CFG)
        self.assertEqual({(m.imported["id"], m.entered[0]["id"]) for m in ms},
                         {(b1["id"], a1["id"]), (b2["id"], a2["id"])})

    def test_receipt_entries_add_up_to_one_charge(self):
        food = tx("30.10", "2026-10-05", "Costco", category_id=1)
        home = tx("19.90", "2026-10-05", "Costco", category_id=2)
        charge = bank("50.00", "2026-10-06", "COSTCO WHOLESALE #535")
        [m] = d.find_matches([food, home, charge], CFG)
        self.assertTrue(m.is_group)
        self.assertEqual({t["id"] for t in m.entered}, {food["id"], home["id"]})

    def test_pending_and_split_pieces_ignored(self):
        mine = tx("10", "2026-10-01", "A")
        self.assertEqual(d.find_matches([mine, bank("10", "2026-10-01", "A", is_pending=True)],
                                        CFG), [])
        self.assertEqual(d.find_matches([mine, bank("10", "2026-10-01", "A", split_parent_id=5)],
                                        CFG), [])

    def test_ignored_pair_not_shown(self):
        mine, theirs = tx("10", "2026-10-01", "A"), bank("10", "2026-10-01", "A")
        ignored = {d.pair_key(theirs, [mine])}
        self.assertEqual(d.find_matches([mine, theirs], CFG, ignored), [])

    def test_source_missing_falls_back_to_external_id(self):
        mine = tx("10", "2026-10-01", "A", source=None)
        theirs = tx("10", "2026-10-01", "A", source=None, external_id="x1")
        self.assertEqual(len(d.find_matches([mine, theirs], CFG)), 1)

    def test_payee_similarity(self):
        self.assertGreater(d.payee_similarity("Esso gas", "ESSO 1234 TORONTO ON"), 0.5)
        self.assertLess(d.payee_similarity("Netflix", "SHOPPERS DRUG MART"), 0.5)

    def test_many_transactions_is_fast(self):
        import time
        txs = []
        for i in range(400):
            date = f"2026-10-{1 + i % 28:02d}"
            txs.append(tx(f"{(i % 37) + 1}.{i % 100:02d}", date, f"Shop {i % 9}"))
            txs.append(bank(f"{(i % 41) + 1}.{i % 100:02d}", date, f"SHOP {i % 9}"))
        start = time.time()
        d.find_matches(txs, CFG)
        self.assertLess(time.time() - start, 20)


# ---------------------------------------------------------------------------
class TestActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hist = Path(self.tmp.name) / "h.jsonl"
        self.patch = mock.patch.object(d, "HISTORY_FILE", self.hist)
        self.patch.start()
        d.record.__defaults__ = (self.hist,)

    def tearDown(self):
        self.patch.stop()
        d.record.__defaults__ = (d.HISTORY_FILE,)
        self.tmp.cleanup()

    def test_merge_copies_details_then_deletes_entry(self):
        mine = tx("45.20", "2026-10-30", "Esso", category_id=3, notes="receipt", tag_ids=[9])
        theirs = bank("45.20", "2026-10-31", "ESSO 1234", tag_ids=[4], notes="bank note")
        m = d.Match(theirs, [mine], 95)
        lm = FakeLM()
        d.do_merge(lm, m, CFG)
        self.assertEqual(lm.calls[0], ("update", theirs["id"], {
            "category_id": 3, "payee": "Esso", "notes": "bank note | receipt", "tag_ids": [4, 9]}))
        self.assertEqual(lm.calls[1], ("delete", mine["id"]))
        saved = json.loads(self.hist.read_text().splitlines()[0])
        self.assertEqual(saved["entered"][0]["notes"], "receipt")
        self.assertNotIn("_account", saved["imported"])

    def test_merge_keep_bank_payee(self):
        mine = tx("1", "2026-10-30", "Esso", category_id=3)
        theirs = bank("1", "2026-10-30", "ESSO 1234")
        changes = d.merged_changes(d.Match(theirs, [mine], 95), {**CFG, "keep_payee": "imported"})
        self.assertNotIn("payee", changes)

    def test_group_merge_splits_bank_charge(self):
        a = tx("30.10", "2026-10-05", "Costco", category_id=1, notes="milk")
        b = tx("19.90", "2026-10-05", "Costco", category_id=2)
        charge = bank("50.00", "2026-10-06", "COSTCO")
        lm = FakeLM()
        d.do_merge(lm, d.Match(charge, [a, b], 90), CFG)
        self.assertEqual(lm.calls[0], ("split", charge["id"], [
            {"amount": "30.10", "category_id": 1, "payee": "Costco", "notes": "milk"},
            {"amount": "19.90", "category_id": 2, "payee": "Costco"}]))
        self.assertEqual(lm.calls[1:], [("delete", a["id"]), ("delete", b["id"])])

    def test_failed_delete_is_reported_not_raised(self):
        class Flaky(FakeLM):
            def delete(self, i):
                raise d.ApiError("nope")
        mine, theirs = tx("1", "2026-10-30", "A"), bank("1", "2026-10-30", "A")
        msg = d.do_merge(Flaky(), d.Match(theirs, [mine], 95), CFG)
        self.assertIn("couldn't delete", msg)

    def test_delete_bank_copy(self):
        mine, theirs = tx("1", "2026-10-30", "A"), bank("1", "2026-10-30", "A")
        lm = FakeLM()
        d.do_delete(lm, d.Match(theirs, [mine], 95), "imported")
        self.assertEqual(lm.calls, [("delete", theirs["id"])])


# ---------------------------------------------------------------------------
class TestReview(unittest.TestCase):
    def run_review(self, answers, txs, dry_run=False):
        lm = FakeLM()
        lm.categories = lambda: {3: "Gas"}
        lm.tags = lambda: {}
        lm.transactions = lambda start, end: txs
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ign = Path(tmp.name) / "i.json"
        args = mock.Mock(days=None, dry_run=dry_run)
        out = io.StringIO()
        with mock.patch.object(d, "get_client", return_value=lm), \
             mock.patch.object(d, "IGNORED_FILE", ign), \
             mock.patch.object(d, "load_ignored", lambda: set()), \
             mock.patch.object(d, "save_ignored", lambda keys: ign.write_text(json.dumps(sorted(keys)))), \
             mock.patch.object(d, "record", lambda *a, **k: None), \
             mock.patch("builtins.input", side_effect=answers), redirect_stdout(out):
            code = d.cmd_review(args, CFG)
        return lm, out.getvalue(), code, ign

    def pair(self):
        return [tx("45.20", "2026-10-30", "Esso", category_id=3),
                bank("45.20", "2026-10-31", "ESSO 1234")]

    def test_merge_choice(self):
        lm, out, code, _ = self.run_review(["m"], self.pair())
        self.assertEqual([c[0] for c in lm.calls], ["update", "delete"])
        self.assertIn("1 merged", out)

    def test_default_is_merge(self):
        lm, _, _, _ = self.run_review([""], self.pair())
        self.assertEqual([c[0] for c in lm.calls], ["update", "delete"])

    def test_dry_run_changes_nothing(self):
        lm, out, _, _ = self.run_review(["m"], self.pair(), dry_run=True)
        self.assertEqual(lm.calls, [])
        self.assertIn("would set on the bank copy", out)

    def test_not_duplicate_is_remembered(self):
        lm, _, _, ign = self.run_review(["n"], self.pair())
        self.assertEqual(lm.calls, [])
        self.assertEqual(len(json.loads(ign.read_text())), 1)

    def double_pair(self):
        return [imp("5.00", "2026-10-01", "TIM HORTONS", "2026-10-02T00:00:00Z", category_id=3),
                imp("5.00", "2026-10-01", "TIM HORTONS", "2026-10-20T00:00:00Z")]

    def test_double_keep_older_deletes_newer(self):
        txs = self.double_pair()
        lm, out, _, _ = self.run_review(["o"], txs)
        self.assertEqual(lm.calls, [("delete", txs[1]["id"])])
        self.assertIn("OLDER COPY", out)

    def test_double_keep_newer_deletes_older(self):
        txs = self.double_pair()
        lm, _, _, _ = self.run_review(["w"], txs)
        self.assertEqual(lm.calls, [("delete", txs[0]["id"])])

    def test_double_merge_default(self):
        txs = self.double_pair()
        lm, out, _, _ = self.run_review([""], txs)
        self.assertEqual(lm.calls, [("delete", txs[1]["id"])])
        self.assertIn("deleted the newer copy", out)

    def test_attachment_defaults_to_skip(self):
        txs = self.pair()
        txs[0]["files"] = [{"id": 1}]
        lm, out, _, _ = self.run_review([""], txs)
        self.assertEqual(lm.calls, [])
        self.assertIn("attached file", out)


def imp(amount, date, name, created, **extra):
    return bank(amount, date, name, original_name=name, created_at=created, **extra)


class TestDoubleImports(unittest.TestCase):
    def test_reconnect_copy_found_and_older_kept(self):
        old = imp("12.50", "2026-10-01", "TIM HORTONS #123", "2026-10-02T10:00:00Z", category_id=4)
        new = imp("12.50", "2026-10-01", "TIM HORTONS #123", "2026-10-20T08:00:00Z")
        [m] = d.find_all([new, old], CFG)
        self.assertTrue(m.is_double)
        self.assertEqual((m.imported["id"], m.entered[0]["id"]), (old["id"], new["id"]))
        self.assertGreaterEqual(m.score, 90)

    def test_same_sync_is_two_real_charges(self):
        a = imp("2.00", "2026-10-01", "TTC FARE", "2026-10-02T10:00:00Z")
        b = imp("2.00", "2026-10-01", "TTC FARE", "2026-10-02T10:00:05Z")
        self.assertEqual(d.find_all([a, b], CFG), [])

    def test_bank_description_must_match_exactly(self):
        pairs = [("QCN - Dividend", "QUU - Dividend"),
                 ("E-TRANSFER 011649334031 ERINY", "E-TRANSFER 011577801214 ERINY")]
        for x, y in pairs:
            a = imp("0.01", "2026-10-01", x, "2026-10-02T00:00:00Z")
            b = imp("0.01", "2026-10-01", y, "2026-10-20T00:00:00Z")
            self.assertEqual(d.find_all([a, b], CFG), [], x)

    def test_unknown_import_time_is_only_possible(self):
        a = imp("5", "2026-10-01", "NETFLIX", "")
        b = imp("5", "2026-10-01", "NETFLIX", "")
        [m] = d.find_all([a, b], CFG)
        self.assertEqual(m.label, "possible double import")

    def test_same_bank_id_is_certain(self):
        a = imp("5", "2026-10-01", "A", "2026-10-02T10:00:00Z")
        b = imp("5", "2026-10-01", "A", "2026-10-02T10:00:01Z")
        b["external_id"] = a["external_id"]
        [m] = d.find_all([a, b], CFG)
        self.assertEqual(m.score, 100)

    def test_not_matched_across_accounts_payees_or_dates(self):
        base = imp("9.99", "2026-10-01", "NETFLIX", "2026-10-02T00:00:00Z", account=1)
        others = [imp("9.99", "2026-10-01", "NETFLIX", "2026-10-20T00:00:00Z", account=2),
                  imp("9.99", "2026-10-01", "SPOTIFY", "2026-10-20T00:00:00Z", account=1),
                  imp("9.99", "2026-10-05", "NETFLIX", "2026-10-20T00:00:00Z", account=1)]
        for o in others:
            self.assertEqual(d.find_all([base, o], CFG), [], o["original_name"])

    def test_can_be_turned_off(self):
        a = imp("5", "2026-10-01", "A", "2026-10-02T00:00:00Z")
        b = imp("5", "2026-10-01", "A", "2026-10-20T00:00:00Z")
        self.assertEqual(d.find_all([a, b], {**CFG, "find_double_imports": False}), [])

    def test_entry_match_takes_priority(self):
        mine = tx("5", "2026-10-01", "A")
        a = imp("5", "2026-10-01", "A", "2026-10-02T00:00:00Z")
        b = imp("5", "2026-10-01", "A", "2026-10-20T00:00:00Z")
        ms = d.find_all([mine, a, b], CFG)
        self.assertEqual([m.kind for m in ms], ["entry"])

    def test_merge_only_fills_gaps(self):
        old = imp("5", "2026-10-01", "A", "2026-10-02T00:00:00Z", category_id=4, notes="mine")
        new = imp("5", "2026-10-01", "A", "2026-10-20T00:00:00Z", category_id=7, notes="new",
                  tag_ids=[3])
        m = d.Match(old, [new], 95, kind="double")
        self.assertEqual(d.merged_changes(m, CFG), {"notes": "mine | new", "tag_ids": [3]})

    def test_auto_never_touches_double_imports(self):
        a = imp("5", "2026-10-01", "A", "2026-10-02T00:00:00Z")
        b = imp("5", "2026-10-01", "A", "2026-10-20T00:00:00Z")
        lm = FakeLM()
        lm.categories, lm.tags = (lambda: {}), (lambda: {})
        lm.transactions = lambda start, end: [a, b]
        with mock.patch.object(d, "get_client", return_value=lm), \
             mock.patch.object(d, "load_ignored", lambda: set()), redirect_stdout(io.StringIO()):
            d.cmd_auto(mock.Mock(days=None, apply=True), CFG)
        self.assertEqual(lm.calls, [])


class TestNearMisses(unittest.TestCase):
    def test_reports_account_and_date_reasons(self):
        mine = tx("10", "2026-10-01", "A", account=1)
        theirs = bank("10", "2026-10-20", "A", account=2)
        [(e, imp, why)] = d.near_misses([mine, theirs], CFG)
        self.assertTrue(any("different account" in w for w in why))
        self.assertTrue(any("days apart" in w for w in why))

    def test_reports_split_bank_copy(self):
        mine = tx("10", "2026-10-01", "A")
        [(_, _, why)] = d.near_misses([mine, bank("10", "2026-10-01", "A", split_parent_id=3)], CFG)
        self.assertEqual(why, ["bank copy is split"])

    def test_real_match_is_not_a_near_miss(self):
        self.assertEqual(d.near_misses([tx("10", "2026-10-01", "A"),
                                        bank("10", "2026-10-02", "A")], CFG), [])


class TestConfig(unittest.TestCase):
    def test_unknown_setting_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.toml"
            p.write_text("days_aftr = 5\n")
            with self.assertRaises(d.ConfigError):
                d.load_config(p)

    def test_missing_file_uses_defaults(self):
        self.assertEqual(d.load_config(Path("/nonexistent.toml"))["days_after"], 10)


if __name__ == "__main__":
    unittest.main()
