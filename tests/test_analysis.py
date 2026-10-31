"""The analysis script must work on the exact files the scanner writes."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import matplotlib  # noqa: F401
    import pandas  # noqa: F401
    HAVE_PANDAS = True
except ImportError:  # the scanner itself never needs pandas
    HAVE_PANDAS = False

from arbscanner.api import PolymarketClient
from arbscanner.httpclient import HttpClient
from arbscanner.scan import run_scan
from tests.fakepoly import FakePolymarket, build_world


@unittest.skipUnless(HAVE_PANDAS, "pandas/matplotlib not installed")
class AnalysisTests(unittest.TestCase):
    def test_report_from_real_scanner_output(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
        import analyze

        events, books = build_world()
        fake = FakePolymarket(events, books).start()
        tmp = Path(tempfile.mkdtemp())
        try:
            client = PolymarketClient(fake.url, fake.url, http=HttpClient(timeout=5, retries=1, backoff=0.01, pause=0))
            t = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
            # 6 scans with the arbitrage present, then it disappears for 2 scans, then comes back.
            for i in range(10):
                if i in (6, 7):
                    fake.books["111"]["asks"] = [{"price": "0.50", "size": "100"}]
                elif i == 8:
                    fake.books["111"]["asks"] = [{"price": "0.30", "size": "50"}, {"price": "0.33", "size": "100"}]
                run_scan(client, tmp / "data", now=t + timedelta(minutes=10 * i))
            # A failed scan in the middle of the next day
            http_bad = HttpClient(timeout=1, retries=0, backoff=0.01, pause=0)
            run_scan(PolymarketClient("http://127.0.0.1:9", "http://127.0.0.1:9", http=http_bad),
                     tmp / "data2", now=t)
            analyze.main(["--data", str(tmp / "data"), "--out", str(tmp / "report")])
            summary = json.loads((tmp / "report" / "summary.json").read_text())
            self.assertEqual(summary["scans_ok"], 10)
            self.assertEqual(summary["median_scan_interval_min"], 10.0)
            # YES on 1001: scans 0-5 then 8-9 -> 2 episodes; NO on 1002: one episode of 10 scans.
            self.assertEqual(summary["episodes_yes"], 2)
            self.assertEqual(summary["episodes_no"], 1)
            self.assertEqual(summary["episodes"], 3)
            self.assertAlmostEqual(summary["total_profit_usd"], round(0.964 * 2 + 0.5, 2))
            self.assertTrue((tmp / "report" / "report.md").exists())
            import pandas as pd
            ep = pd.read_csv(tmp / "report" / "episodes.csv", dtype=str)
            self.assertEqual(set(ep["end_date"]), {"2026-12-31T12:00:00Z"})
            self.assertIn("A1 | A2 | A3", set(ep["outcomes"]))
            self.assertIn("## YES baskets to check by hand", (tmp / "report" / "report.md").read_text())
            self.assertTrue((tmp / "report" / "episodes_per_day.png").exists())
            self.assertTrue((tmp / "report" / "profit_distribution.png").exists())
            self.assertTrue((tmp / "report" / "basket_cost_distribution.png").exists())
            self.assertEqual(summary["events_monitored"], 6)          # every event screened at stage 1
            self.assertEqual(summary["snapshots"], 1)                  # one per 3-hour slot
            self.assertEqual(summary["median_events_per_scan"], 6)
            self.assertEqual(summary["median_candidate_events_per_scan"], 4)
            self.assertEqual(summary["stage1_vs_books_ask_equal"], 1.0)
            self.assertEqual(summary["no_book_mirror_equal"], 1.0)
            self.assertEqual(summary["listing_refreshes"], 2)            # at 12:00 and 13:10 (list older than 60 min)
            self.assertEqual(summary["listing_complete_share"], 1.0)
            # first snapshot scan: YES 1001 (0.95, executable) and 1003 (0.99, killed by fees) are below $1,
            # NO 1002 is below its payout and executable; 1008 looks cheap but its list is incomplete.
            self.assertEqual(summary["baskets_below_payout_at_snapshots"], 3)
            self.assertEqual(summary["baskets_executable_at_snapshots"], 2)
            self.assertIn("Stage-1 best ask = full-book best ask", (tmp / "report" / "report.md").read_text())
            # Empty data folder: no crash
            analyze.main(["--data", str(tmp / "nothing"), "--out", str(tmp / "report_empty")])
            self.assertIn("No scan data", (tmp / "report_empty" / "report.md").read_text())
            analyze.main(["--data", str(tmp / "data2"), "--out", str(tmp / "report_failed")])
        finally:
            fake.stop()
            shutil.rmtree(tmp, True)


if __name__ == "__main__":
    unittest.main()
