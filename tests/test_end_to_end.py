"""End-to-end: the real scanner code talks HTTP to a fake Polymarket server."""

import csv
import gzip
import io
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
from arbscanner.scan import load_universe, run_scan
from arbscanner.selfcheck import run_selfcheck
from arbscanner.universe import SchemaError
from tests.fakepoly import FakePolymarket, build_world

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 2, 12, 3, tzinfo=timezone.utc)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
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

    # ------------------------------------------------------------------ happy path

    def test_full_scan_finds_exactly_the_known_opportunities(self):
        summary = run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["universe_events"], 6)
        self.assertEqual(summary["events_evaluated"], 5)          # 1007 has a rejected id
        self.assertEqual(summary["skipped_missing_book"], 1)
        self.assertEqual(summary["books_rejected"], 1)            # BAD-TOKEN isolated by splitting
        self.assertEqual(summary["opportunities_yes"], 1)
        self.assertEqual(summary["opportunities_no"], 1)
        self.assertEqual(summary["gamma_endpoint"], "keyset")
        self.assertEqual(summary["gamma_pages"], 3)

        opps = read_csv(self.tmp / "opportunities" / "2026-11-02.csv")
        got = {(r["event_id"], r["basket"]): r for r in opps}
        self.assertEqual(set(got), {("1001", "YES"), ("1002", "NO")})
        self.assertEqual(D(got[("1001", "YES")]["exec_profit"]), D("0.964"))
        self.assertEqual(D(got[("1001", "YES")]["exec_size"]), D(40))
        self.assertEqual(D(got[("1002", "NO")]["exec_profit"]), D("0.5"))
        self.assertEqual(got[("1002", "NO")]["fee_sources"], "disabled")
        self.assertEqual(got[("1001", "YES")]["url"], "https://polymarket.com/event/event-1001")
        self.assertEqual(got[("1001", "YES")]["suspicious"], "0")
        self.assertEqual(got[("1001", "YES")]["verified"], "1")   # full outcome list confirmed
        self.assertEqual(got[("1002", "NO")]["verified"], "")     # not needed for NO baskets
        self.assertEqual(summary["yes_rejected_by_verification"], 0)

        scans = read_csv(self.tmp / "scans" / "2026-11.csv")
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]["status"], "ok")

        snap = self.tmp / "snapshots" / "2026-11-02" / "12.csv.gz"
        with gzip.open(snap, "rt", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["event_id"] for r in rows}, {"1001", "1002", "1003", "1006", "1008"})

        uni = load_universe(self.tmp / "universe.json")
        self.assertEqual(len(uni.events), 6)

        # A browser-like User-Agent was sent on every request.
        self.assertTrue(all("polymarket-arb-scanner" in (r[3] or "") for r in self.fake.requests))

    def test_second_scan_reuses_event_list_and_appends(self):
        run_scan(self.client(), self.tmp, now=T0)
        n_requests = len(self.fake.requests)
        s2 = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=10))
        listing_calls = [r for r in self.fake.requests[n_requests:] if r[0] == "GET" and r[1] in ("/events/keyset", "/events")]
        self.assertEqual(listing_calls, [])              # event list reused, not downloaded again
        verify_calls = [r[1] for r in self.fake.requests[n_requests:] if r[0] == "GET"]
        self.assertEqual(verify_calls, ["/events/1001"])  # only the YES-opportunity double check
        self.assertEqual(s2["universe_refreshed"], "0")
        self.assertEqual(len(read_csv(self.tmp / "scans" / "2026-11.csv")), 2)
        self.assertEqual(len(read_csv(self.tmp / "opportunities" / "2026-11-02.csv")), 4)
        # same hour: the snapshot is not rewritten
        self.assertEqual(len(list((self.tmp / "snapshots" / "2026-11-02").iterdir())), 1)
        # 70 minutes later the list is refreshed and a new hourly snapshot is written
        s3 = run_scan(self.client(), self.tmp, now=T0 + timedelta(minutes=70))
        self.assertEqual(s3["universe_refreshed"], "1")
        self.assertTrue((self.tmp / "snapshots" / "2026-11-02" / "13.csv.gz").exists())

    def test_offset_fallback_when_keyset_missing(self):
        self.fake.keyset_enabled = False
        summary = run_scan(self.client(page_limit=3), self.tmp, now=T0)
        self.assertEqual(summary["gamma_endpoint"], "offset")
        self.assertEqual(summary["gamma_complete"], "1")
        self.assertEqual(summary["opportunities_yes"] + summary["opportunities_no"], 2)

    def test_offset_fallback_when_keyset_answers_in_another_format(self):
        self.fake.keyset_returns_list = True
        summary = run_scan(self.client(page_limit=3), self.tmp, now=T0)
        self.assertEqual(summary["gamma_endpoint"], "offset")
        self.assertEqual(summary["opportunities_yes"] + summary["opportunities_no"], 2)

    def test_retry_after_temporary_server_error(self):
        self.fake.fail_first_books = True
        summary = run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["opportunities_yes"] + summary["opportunities_no"], 2)

    def test_small_batches_give_the_same_result(self):
        summary = run_scan(self.client(books_batch=1), self.tmp, now=T0)
        self.assertEqual(summary["opportunities_yes"] + summary["opportunities_no"], 2)
        self.assertEqual(summary["books_rejected"], 1)

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
        opps = read_csv(self.tmp / "opportunities" / "2026-11-02.csv")
        self.assertNotIn("1010", {r["event_id"] for r in opps})
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(client)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] Event listing contains every outcome", out.getvalue())

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

    def test_unknown_format_raises_after_recording(self):
        self.fake.books_bad_shape = True
        with self.assertRaises(SchemaError):
            run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(read_csv(self.tmp / "scans" / "2026-11.csv")[0]["status"], "schema_error")

    def test_corrupt_universe_file_is_rebuilt(self):
        (self.tmp / "universe.json").write_text("{not json", encoding="utf-8")
        s = run_scan(self.client(), self.tmp, now=T0)
        self.assertEqual(s["universe_refreshed"], "1")
        self.assertEqual(s["opportunities_yes"] + s["opportunities_no"], 2)

    # -------------------------------------------------------------------- selfcheck

    def test_selfcheck_passes_on_documented_formats(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(self.client())
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("RESULT: PASS", text)
        self.assertIn("Executable opportunities in this sample right now: 2", text)

    def test_selfcheck_fails_if_yes_and_no_were_swapped(self):
        for ev in self.fake.events:
            for m in ev["markets"]:
                if m.get("clobTokenIds"):
                    import json
                    ids = json.loads(m["clobTokenIds"])
                    m["clobTokenIds"] = json.dumps(list(reversed(ids)))
                elif m.get("positionIds"):
                    m["positionIds"] = list(reversed(m["positionIds"]))
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_selfcheck(self.client())
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("[FAIL]", out.getvalue())

    # ------------------------------------------------------------------------ CLI

    def test_cli_on_every_python_version_available(self):
        env = dict(os.environ, ARB_GAMMA_URL=self.fake.url, ARB_CLOB_URL=self.fake.url)
        pythons = [p for p in ("python3.11", "python3.12", "python3.13") if shutil.which(p)] or [sys.executable]
        for py in pythons:
            out_dir = self.tmp / Path(py).name  # py can be an absolute path (sys.executable)
            proc = subprocess.run(
                [py, "-m", "arbscanner", "--pause", "0", "scan", "--data", str(out_dir)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, f"{py}: {proc.stdout} {proc.stderr}")
            self.assertIn("status=ok", proc.stdout)
            self.assertIn("opportunities=2", proc.stdout)
            proc = subprocess.run(
                [py, "-m", "arbscanner", "--pause", "0", "selfcheck"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, f"{py}: {proc.stdout} {proc.stderr}")

    def test_cli_exit_code_when_blocked(self):
        self.fake.force_status = 403
        env = dict(os.environ, ARB_GAMMA_URL=self.fake.url, ARB_CLOB_URL=self.fake.url)
        proc = subprocess.run(
            [sys.executable, "-m", "arbscanner", "--pause", "0", "--retries", "0", "scan", "--data", str(self.tmp / "x")],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SCAN FAILED", proc.stderr)


if __name__ == "__main__":
    unittest.main()
