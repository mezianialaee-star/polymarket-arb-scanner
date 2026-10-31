"""End-to-end: the real scanner code talks HTTP to a fake Polymarket server."""

import csv
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from arbscanner.api import PolymarketClient
from arbscanner.httpclient import ApiError, HttpClient
from arbscanner.scan import append_csv, load_universe, run_scan
from arbscanner.selfcheck import run_selfcheck
from arbscanner.universe import SchemaError
from tests.fakepoly import FakePolymarket, book, build_world, cond, event, market, mirror

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 2, 12, 3, tzinfo=timezone.utc)
EXPECTED = {("1001", "YES"): (D("0.964"), D(40)), ("1002", "NO"): (D("0.5"), D(10))}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_gz_csv(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        events, books = build_world()
        self.fake = FakePolymarket(events, books).start()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.fake.stop)

    def client(self, **kw):
        http = HttpClient(timeout=5, retries=2, backoff=0.01, pause=0)
        return PolymarketClient(self.fake.url, self.fake.url, http=http, **kw)

    def opportunities(self, day="2026-11-02"):
        path = self.tmp / "opportunities" / f"{day}.csv"
        return read_csv(path) if path.exists() else []

    def assert_known_opportunities(self, summary):
        self.assertEqual(summary["status"], "ok", summary.get("error"))
        self.assertEqual(summary["opportunities_yes"], 1)
        self.assertEqual(summary["opportunities_no"], 1)
        got = {(r["event_id"], r["basket"]): (D(r["exec_profit"]), D(r["exec_size"])) for r in self.opportunities()}
        self.assertEqual(got, EXPECTED)

    # ------------------------------------------------------------------ happy path

    def test_full_scan_finds_exactly_the_known_opportunities(self):
        summary = run_scan(self.client(), self.tmp, now=T0)
        self.assert_known_opportunities(summary)
        self.assertEqual(summary["universe_events"], 6)
        self.assertEqual(summary["gamma_endpoint"], "keyset")
        self.assertEqual(summary["gamma_pages"], 3)
        # stage 1: every YES token priced except the one the CLOB refuses
        self.assertEqual(summary["prices_tokens"], 15)
        self.assertEqual(summary["prices_priced"], 14)
        self.assertEqual(summary["prices_rejected"], 1)
        self.assertEqual(summary["prices_uncertain"], 1)
        self.assertEqual((summary["prices_missing"], summary["prices_odd"]), (1, 0))
        self.assertEqual(summary["side_mapping"], "BUY=ask")
        self.assertEqual(summary["side_votes"], "14/14")
        self.assertEqual(summary["screened_events"], 6)
        # candidates: 1001 (YES 0.95), 1003 (YES 0.99), 1002 (NO), 1007 (refused id -> stage 2 decides)
        self.assertEqual(summary["candidates_yes"], 2)
        self.assertEqual(summary["candidates_no"], 1)
        self.assertEqual(summary["candidates_uncertain"], 1)
        self.assertEqual(summary["candidate_events"], 4)
        # stage 2: full books only for the 4 candidates (22 tokens), 1007 has no book
        self.assertEqual(summary["tokens_requested"], 22)
        self.assertEqual(summary["books_rejected"], 1)
        self.assertEqual(summary["events_evaluated"], 3)
        self.assertEqual(summary["skipped_missing_book"], 1)
        self.assertEqual(summary["check_tokens"], 10)
        for k in ("check_ask_equal", "check_bid_equal", "check_mirror_equal"):
            self.assertEqual(summary[k], 10, k)
        # no book of a non-candidate event was read
        books_asked = {p["token_id"] for m, path, p_list, _ in self.fake.requests if path == "/books" for p in p_list}
        self.assertFalse(books_asked & {"611", "612", "621", "622", "811", "812", "821", "822"})

        opps = {(r["event_id"], r["basket"]): r for r in self.opportunities()}
        self.assertEqual(opps[("1002", "NO")]["fee_sources"], "disabled")
        self.assertEqual(opps[("1001", "YES")]["url"], "https://polymarket.com/event/event-1001")
        self.assertEqual(opps[("1001", "YES")]["end_date"], "2026-12-31T12:00:00Z")   # when the basket pays
        self.assertEqual(opps[("1001", "YES")]["outcomes"], "A1 | A2 | A3")
        self.assertEqual(opps[("1001", "YES")]["suspicious"], "0")
        self.assertEqual(opps[("1001", "YES")]["verified"], "1")   # full outcome list confirmed
        self.assertEqual(opps[("1002", "NO")]["verified"], "")     # not needed for NO baskets
        self.assertEqual(summary["yes_rejected_by_verification"], 0)

        scans = read_csv(self.tmp / "scans" / "2026-11.csv")
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]["status"], "ok")

        rows = {r["event_id"]: r for r in read_gz_csv(self.tmp / "snapshots" / "2026-11-02" / "12.csv.gz")}
        self.assertEqual(set(rows), {"1001", "1002", "1003", "1006", "1007", "1008"})
        self.assertEqual(D(rows["1001"]["yes_cost"]), D("0.95"))
        self.assertEqual(D(rows["1001"]["no_cost"]), D("2.12"))
        self.assertEqual(rows["1007"]["uncertain"], "1")
        self.assertEqual(rows["1008"]["yes_guarded"], "1")

        uni = load_universe(self.tmp / "universe.json.gz")
        self.assertEqual(len(uni.events), 6)
        self.assertTrue(all("polymarket-arb-scanner" in (r[3] or "") for r in self.fake.requests))

    def test_both_meanings_of_the_prices_sides(self):
        self.fake.prices_buy_is_ask = False   # what Polymarket's API reference says
        summary = run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(summary["side_mapping"], "BUY=bid")
        self.assertEqual(summary["candidate_events"], 4)
        self.assert_known_opportunities(summary)

    def test_numbers_zero_sides_and_omitted_unknown_ids(self):
        self.fake.prices_as_numbers = True
        self.fake.prices_empty_side = "zero"
        self.fake.prices_unknown = "omit"
        # one outcome of 1006 has nobody selling YES: its YES basket cannot be bought
        self.fake.books["611"]["asks"] = []
        summary = run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(summary["prices_rejected"], 0)
        self.assertEqual(summary["prices_uncertain"], 1)     # BAD-TOKEN left out of the answer
        self.assertEqual(summary["candidate_events"], 4)
        self.assert_known_opportunities(summary)
        rows = {r["event_id"]: r for r in read_gz_csv(self.tmp / "snapshots" / "2026-11-02" / "12.csv.gz")}
        self.assertEqual(rows["1006"]["yes_cost"], "")         # incomplete basket: no cost
        self.assertEqual(D(rows["1006"]["no_cost"]), D("1.02"))

    def test_second_scan_reuses_event_list_and_appends(self):
        run_scan(self.client(), self.tmp, now=T0)
        n_requests = len(self.fake.requests)
        s2 = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=10))
        later = self.fake.requests[n_requests:]
        # no full listing: one "newest events" page, then the YES double check
        self.assertEqual([(r[1], r[2].get("order")) for r in later if r[0] == "GET"],
                         [("/events/keyset", "id"), ("/events/1001", None)])
        self.assertEqual((s2["new_events_seen"], s2["new_events_added"]), (0, 0))
        self.assertEqual(s2["universe_refreshed"], "0")
        self.assertEqual(len(read_csv(self.tmp / "scans" / "2026-11.csv")), 2)
        self.assertEqual(len(self.opportunities()), 4)
        self.assertEqual(len(list((self.tmp / "snapshots" / "2026-11-02").iterdir())), 1)
        s3 = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=70))
        self.assertEqual(s3["universe_refreshed"], "1")          # list older than 60 min
        self.assertEqual(len(list((self.tmp / "snapshots" / "2026-11-02").iterdir())), 1)  # same 3-hour slot
        run_scan(self.client(), self.tmp, now=T0 + timedelta(hours=3))
        self.assertTrue((self.tmp / "snapshots" / "2026-11-02" / "15.csv.gz").exists())

    def test_offset_fallback_when_keyset_missing(self):
        self.fake.keyset_enabled = False
        summary = run_scan(self.client(page_limit=3), self.tmp, now=T0)
        self.assertEqual(summary["gamma_endpoint"], "offset")
        self.assertEqual(summary["gamma_complete"], "1")
        self.assert_known_opportunities(summary)

    def test_offset_fallback_when_keyset_answers_in_another_format(self):
        self.fake.keyset_returns_list = True
        summary = run_scan(self.client(page_limit=3), self.tmp, now=T0)
        self.assertEqual(summary["gamma_endpoint"], "offset")
        self.assert_known_opportunities(summary)

    def test_retry_after_temporary_server_errors(self):
        self.fake.fail_first_books = True
        self.fake.fail_first_prices = True
        summary = run_scan(self.client(), self.tmp, now=T0)
        self.assert_known_opportunities(summary)

    def test_batch_sizes_and_parallelism_do_not_change_the_result(self):
        for i, kw in enumerate(({"books_batch": 1, "prices_batch": 1, "workers": 1},
                                {"books_batch": 3, "prices_batch": 2, "workers": 8},
                                {"books_batch": 500, "prices_batch": 250, "workers": 2})):
            tmp = self.tmp / f"run{i}"
            s = run_scan(self.client(**kw), tmp, now=T0)
            self.assertEqual(s["status"], "ok")
            got = {(r["event_id"], r["basket"]): (D(r["exec_profit"]), D(r["exec_size"]))
                   for r in read_csv(tmp / "opportunities" / "2026-11-02.csv")}
            self.assertEqual(got, EXPECTED, kw)
            self.assertEqual((s["prices_rejected"], s["books_rejected"], s["candidate_events"]), (1, 1, 4), kw)

    def test_yes_basket_on_an_incomplete_listing_is_rejected(self):
        events, books, full = build_world(truncated=True)
        fake = FakePolymarket(events, books, full_events=full).start()
        self.addCleanup(fake.stop)
        http = HttpClient(timeout=5, retries=1, backoff=0.01, pause=0)
        client = PolymarketClient(fake.url, fake.url, http=http)
        summary = run_scan(client, self.tmp, now=T0)
        # 1010 looks like a YES arbitrage (0.97 + fees < 1) but one outcome is missing.
        self.assertEqual(summary["yes_rejected_by_verification"], 1)
        self.assertEqual(summary["opportunities_yes"], 1)          # only 1001
        self.assertNotIn("1010", {r["event_id"] for r in self.opportunities()})
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(client)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] Event listing contains every outcome", out.getvalue())

    def test_listing_cut_short_keeps_the_rest_of_the_previous_list(self):
        run_scan(self.client(), self.tmp, now=T0)
        short = self.client(max_pages=1)          # 3 events per page: only 1001-1003 are listed
        s = run_scan(short, self.tmp, now=T0 + timedelta(minutes=5), force_refresh=True)
        self.assertEqual(s["gamma_complete"], "0")
        self.assertEqual(s["universe_events"], 6)
        self.assertIn("kept 3 events from the previous list", s["universe_note"])
        self.assert_known_opportunities(s)

    def add_event(self, eid, legs):
        """A new event appears on Polymarket after the list was built."""
        markets = []
        for mid, bid, ask in legs:
            y, n = f"{mid}1", f"{mid}2"
            markets.append(market(mid, f"N{mid}", y, n, str((D(bid) + D(ask)) / 2), fees=("0", 1)))
            self.fake.books[y] = book(y, cond(mid), [(bid, "100")], [(ask, "100")])
            self.fake.books[n] = book(n, cond(mid), mirror([(ask, "100")]), mirror([(bid, "100")]))
        self.fake.events.append(event(eid, f"New {eid}", markets))

    def test_new_event_is_scanned_before_the_next_rebuild(self):
        run_scan(self.client(), self.tmp, now=T0)
        self.add_event(1011, [(201, "0.40", "0.42"), (202, "0.50", "0.52")])   # YES basket at 0.94
        s = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=10))
        self.assertEqual(s["universe_refreshed"], "0")
        self.assertEqual((s["new_events_seen"], s["new_events_added"]), (1, 1))
        self.assertEqual(s["universe_events"], 7)
        self.assertEqual(s["opportunities_yes"], 2)
        new = [r for r in self.opportunities() if r["event_id"] == "1011"]
        self.assertEqual(len(new), 1)
        self.assertEqual((D(new[0]["exec_profit"]), new[0]["verified"]), (D(6), "1"))
        self.assertEqual(len(load_universe(self.tmp / "universe.json.gz").events), 6)   # file untouched
        s = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=70))         # hourly rebuild
        self.assertEqual((s["universe_refreshed"], s["universe_events"]), ("1", 7))
        self.assertEqual(load_universe(self.tmp / "universe.json.gz").stats["max_event_id"], 1011)

    def test_new_event_check_is_harmless_when_it_cannot_work(self):
        run_scan(self.client(), self.tmp, now=T0)
        self.add_event(1011, [(201, "0.40", "0.42"), (202, "0.50", "0.52")])
        self.fake.order_supported = False      # listing ignores the order: oldest first
        s = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=10))
        self.assertEqual((s["status"], s["new_events_added"]), ("ok", 0))
        self.fake.keyset_returns_list = True   # unknown format
        s = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=20))
        self.assertEqual(s["status"], "ok")
        self.assertIn("new-event check stopped", s["universe_note"])
        self.assertEqual(s["opportunities_yes"] + s["opportunities_no"], 2)

    # -------------------------------------------------------------------- failures

    def test_network_down_is_recorded_not_raised(self):
        http = HttpClient(timeout=2, retries=1, backoff=0.01, pause=0)
        client = PolymarketClient("http://127.0.0.1:9", "http://127.0.0.1:9", http=http)
        summary = run_scan(client, self.tmp, now=T0)
        self.assertEqual(summary["status"], "network_error")
        self.assertEqual(read_csv(self.tmp / "scans" / "2026-11.csv")[0]["status"], "network_error")

    def test_failed_refresh_keeps_previous_list(self):
        run_scan(self.client(), self.tmp, now=T0)
        http = HttpClient(timeout=2, retries=0, backoff=0.01, pause=0)
        broken_gamma = PolymarketClient("http://127.0.0.1:9", self.fake.url, http=http)
        s = run_scan(broken_gamma, self.tmp, now=T0 + timedelta(minutes=90))
        self.assertEqual(s["status"], "ok")
        self.assertIn("kept previous list", s["universe_note"])
        self.assertEqual(s["opportunities_yes"] + s["opportunities_no"], 2)

    def test_blocked_raises_after_recording(self):
        self.fake.force_status = 403
        with self.assertRaises(ApiError):
            run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(read_csv(self.tmp / "scans" / "2026-11.csv")[0]["status"], "blocked")

    def test_unknown_book_format_raises_after_recording(self):
        self.fake.books_bad_shape = True
        with self.assertRaises(SchemaError):
            run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(read_csv(self.tmp / "scans" / "2026-11.csv")[0]["status"], "schema_error")

    def test_unknown_prices_format_raises_after_recording(self):
        self.fake.prices_bad_shape = True
        with self.assertRaises(SchemaError):
            run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(read_csv(self.tmp / "scans" / "2026-11.csv")[0]["status"], "schema_error")

    def test_prices_that_do_not_behave_like_bid_ask_stop_the_scan(self):
        # BUY above SELL for half the tokens, below for the other half: refuse to guess.
        for i, ev in enumerate(self.fake.events):
            for m in ev["markets"]:
                ids = json.loads(m["clobTokenIds"]) if m.get("clobTokenIds") else m["positionIds"]
                hi, lo = ("0.6", "0.4") if i % 2 else ("0.4", "0.6")
                self.fake.prices_override[ids[0]] = {"BUY": hi, "SELL": lo}
        with self.assertRaises(SchemaError):
            run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(read_csv(self.tmp / "scans" / "2026-11.csv")[0]["status"], "schema_error")

    def test_many_refused_ids_stop_instead_of_flooding_the_api(self):
        client = self.client(prices_batch=250)
        with self.assertRaises(SchemaError):
            client.fetch_prices([f"bogus-{i}" for i in range(100)])
        self.assertLess(len(self.fake.requests), 120)

    def test_corrupt_universe_file_is_rebuilt(self):
        (self.tmp / "universe.json.gz").write_bytes(b"\x1f\x8b not really gzip")
        s = run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(s["universe_refreshed"], "1")
        self.assert_known_opportunities(s)

    def test_csv_written_by_an_older_version_is_not_mixed(self):
        path = self.tmp / "scans" / "2026-11.csv"
        path.parent.mkdir(parents=True)
        path.write_text("scan_time,status\n2026-11-01T00:00:00Z,ok\n", encoding="utf-8")
        run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(path.read_text(encoding="utf-8"), "scan_time,status\n2026-11-01T00:00:00Z,ok\n")
        self.assertEqual(read_csv(self.tmp / "scans" / "2026-11-v2.csv")[0]["status"], "ok")
        append_csv(path, ["scan_time", "status"], [{"scan_time": "x", "status": "ok"}])
        self.assertEqual(len(read_csv(path)), 2)    # same columns: same file

    # -------------------------------------------------------------------- selfcheck

    def test_selfcheck_passes_on_documented_formats(self):
        for buy_is_ask in (True, False):
            self.fake.prices_buy_is_ask = buy_is_ask
            out = io.StringIO()
            with redirect_stdout(out):
                code = run_selfcheck(self.client())
            text = out.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("RESULT: PASS", text)
            self.assertIn("[PASS] No profitable basket in the sample was screened out by stage 1", text)
            self.assertIn("opportunities: YES 1, NO 1, best $0.964", text)

    def test_selfcheck_fails_if_yes_and_no_were_swapped(self):
        for ev in self.fake.events:
            for m in ev["markets"]:
                if m.get("clobTokenIds"):
                    ids = json.loads(m["clobTokenIds"])
                    m["clobTokenIds"] = json.dumps(list(reversed(ids)))
                elif m.get("positionIds"):
                    m["positionIds"] = list(reversed(m["positionIds"]))
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(self.client())
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("[FAIL]", out.getvalue())

    def test_selfcheck_accepts_an_outcome_that_closed_after_the_listing(self):
        import copy
        later = copy.deepcopy(next(e for e in self.fake.events if e["id"] == "1002"))
        later["markets"][3].update({"closed": True, "acceptingOrders": False, "outcomePrices": '["0", "1"]'})
        self.fake.full_events["1002"] = later        # what GET /events/1002 shows a few minutes later
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(self.client())
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("[PASS] Event listing contains every outcome", out.getvalue())

    def test_selfcheck_fails_if_an_open_candidate_has_no_book(self):
        del self.fake.books["112"]      # NO book of a market that is still open and priced
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(self.client())
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("[FAIL] Stage 2: full order books read for every candidate still open", out.getvalue())

    def test_selfcheck_fails_if_stage_one_prices_are_wrong(self):
        # /prices answers a stale, higher ask for every YES token: screening would miss things.
        for t, b in self.fake.books.items():
            if t.endswith("1"):
                bid = max((D(l["price"]) for l in b["bids"]), default=None)
                self.fake.prices_override[t] = {"BUY": "0.99", "SELL": str(bid) if bid else "0"}
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(self.client())
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("[FAIL] Stage-1 best ask = best ask of the full book", out.getvalue())

    # ------------------------------------------------------------------------ CLI

    def test_cli_on_every_python_version_available(self):
        env = dict(os.environ, ARB_GAMMA_URL=self.fake.url, ARB_CLOB_URL=self.fake.url)
        pythons = [p for p in ("python3.11", "python3.12", "python3.13", "python3.14") if shutil.which(p)] or [sys.executable]
        for py in pythons:
            out_dir = self.tmp / Path(py).name  # py can be an absolute path (sys.executable)
            proc = subprocess.run(
                [py, "-m", "arbscanner", "--pause", "0", "scan", "--data", str(out_dir / "data"),
                 "--universe", str(out_dir / "state" / "universe.json.gz")],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, f"{py}: {proc.stdout} {proc.stderr}")
            self.assertIn("status=ok", proc.stdout)
            self.assertIn("opportunities=2", proc.stdout)
            self.assertIn("candidates=4", proc.stdout)
            self.assertTrue((out_dir / "state" / "universe.json.gz").exists())
            self.assertFalse((out_dir / "data" / "universe.json.gz").exists())
            proc = subprocess.run(
                [py, "-m", "arbscanner", "--pause", "0", "selfcheck"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, f"{py}: {proc.stdout} {proc.stderr}")

    def test_cli_exit_code_when_blocked(self):
        self.fake.force_status = 403
        env = dict(os.environ, ARB_GAMMA_URL=self.fake.url, ARB_CLOB_URL=self.fake.url)
        proc = subprocess.run(
            [sys.executable, "-m", "arbscanner", "--pause", "0", "--retries", "0", "scan",
             "--data", str(self.tmp / "x"), "--universe", str(self.tmp / "u.json.gz")],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SCAN FAILED", proc.stderr)


if __name__ == "__main__":
    unittest.main()
