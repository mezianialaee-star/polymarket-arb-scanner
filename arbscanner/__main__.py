"""Command line entry point.

    python -m arbscanner scan        # one scan, results appended under ./data
    python -m arbscanner selfcheck   # live verification on real Polymarket data
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

from .api import CLOB_URL, GAMMA_URL, PolymarketClient
from .httpclient import ApiError, HttpClient
from .scan import run_scan
from .selfcheck import run_selfcheck
from .universe import SchemaError


def build_client(args: argparse.Namespace) -> PolymarketClient:
    http = HttpClient(timeout=args.timeout, retries=args.retries, pause=args.pause)
    return PolymarketClient(
        gamma_url=os.environ.get("ARB_GAMMA_URL", GAMMA_URL),
        clob_url=os.environ.get("ARB_CLOB_URL", CLOB_URL),
        http=http,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="arbscanner", description="Polymarket basket arbitrage scanner (read-only).")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=4, help="retries per request")
    parser.add_argument("--pause", type=float, default=0.1, help="pause after each request, seconds")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="run one scan and append results")
    p_scan.add_argument("--data", default="data", help="output folder")
    p_scan.add_argument("--max-age", type=float, default=60.0, help="refresh the event list if older (minutes)")
    p_scan.add_argument("--force-refresh", action="store_true", help="always refresh the event list")
    p_scan.add_argument("--budget", type=float, default=300.0, help="max seconds to spend listing events")
    p_scan.add_argument("--min-notional", default="1", help="minimum $ value of one order (Polymarket rule)")
    p_scan.add_argument("--no-snapshot", action="store_true", help="do not write hourly snapshots")

    p_check = sub.add_parser("selfcheck", help="verify the scanner on live data")
    p_check.add_argument("--max-events", type=int, default=300, help="events whose books are checked")
    p_check.add_argument("--budget", type=float, default=240.0, help="max seconds to spend listing events")

    args = parser.parse_args(argv)
    client = build_client(args)

    if args.command == "selfcheck":
        return run_selfcheck(client, max_events=args.max_events, time_budget_s=args.budget)

    try:
        summary = run_scan(
            client,
            Path(args.data),
            universe_max_age_min=args.max_age,
            force_refresh=args.force_refresh,
            refresh_budget_s=args.budget,
            min_notional=Decimal(args.min_notional),
            write_snapshot=not args.no_snapshot,
        )
    except (ApiError, SchemaError) as exc:
        print(f"SCAN FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        f"scan {summary['scan_time']} status={summary['status']} "
        f"events={summary.get('events_evaluated', 0)} opportunities="
        f"{int(summary.get('opportunities_yes', 0) or 0) + int(summary.get('opportunities_no', 0) or 0)} "
        f"best_profit=${summary.get('best_profit', '0')} duration={summary.get('duration_s')}s"
        + (f" error={summary.get('error')}" if summary.get("error") else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
