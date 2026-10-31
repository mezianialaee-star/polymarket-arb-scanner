"""One scan = refresh the event list if needed, read every order book, record opportunities."""

from __future__ import annotations

import csv
import gzip
import io
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .api import PolymarketClient
from .basket import D0, D1, BasketResult
from .evaluate import EventEvaluation, evaluate_event, is_suspicious, parse_books
from .httpclient import ApiError
from .universe import SchemaError, Universe, UniverseEvent, build_universe, classify_event

OPPORTUNITY_FIELDS = [
    "scan_time", "event_id", "event_slug", "event_title", "category", "n_outcomes", "eliminated",
    "basket", "payout_per_unit", "sum_best_ask", "gross_edge_top", "fee_top", "net_edge_top",
    "top_size_min", "max_size", "max_profit", "min_size_required", "exec_size", "exec_profit",
    "exec_cost", "roi_pct", "suspicious", "verified", "fee_sources", "book_age_max_s", "url",
]

SCAN_FIELDS = [
    "scan_time", "status", "duration_s", "universe_generated_at", "universe_age_min",
    "universe_refreshed", "universe_note", "universe_events", "universe_markets", "gamma_endpoint",
    "gamma_pages", "gamma_complete", "tokens_requested", "books_received", "books_rejected",
    "levels_dropped", "events_evaluated", "skipped_missing_book", "skipped_other",
    "yes_evaluated", "yes_skipped_incomplete", "opportunities_yes", "opportunities_no",
    "yes_rejected_by_verification", "best_profit", "http_requests", "error",
]

SNAPSHOT_FIELDS = [
    "scan_time", "event_id", "category", "n_outcomes", "yes_skip",
    "yes_complete", "yes_sum_best_ask", "yes_net_edge_top", "yes_top_size_min", "yes_max_profit",
    "no_complete", "no_sum_best_ask", "no_net_edge_top", "no_top_size_min", "no_max_profit",
]

STALE_UNIVERSE_LIMIT_MIN = 6 * 60


# --------------------------------------------------------------------------- utils

def fmt(value: Any, places: int = 6) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        q = Decimal(1).scaleb(-places)
        return format(value.quantize(q).normalize(), "f") if value != 0 else "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def append_csv(path: Path, fields: List[str], rows: Iterable[Dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="raise")
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})
    return len(rows)


def write_gz_csv(path: Path, fields: List[str], rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fields})
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)


def load_universe(path: Path) -> Optional[Universe]:
    if not path.exists():
        return None
    try:
        return Universe.from_json(path.read_text(encoding="utf-8"))
    except (ValueError, KeyError, TypeError):
        return None  # unreadable or old format: rebuild it


def save_universe(path: Path, universe: Universe) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(universe.to_json(), encoding="utf-8")
    os.replace(tmp, path)


def refresh_universe(client: PolymarketClient, now: datetime, time_budget_s: float) -> Universe:
    events = client.iter_open_events(time_budget_s=time_budget_s)
    universe = build_universe(events, now)
    stats = client.last_fetch_stats
    universe.stats.update({
        "gamma_endpoint": stats.get("endpoint", ""),
        "gamma_pages": stats.get("pages", 0),
        "gamma_complete": bool(stats.get("complete", False)),
        "gamma_stopped": stats.get("stopped", ""),
    })
    return universe


def ensure_universe(
    client: PolymarketClient,
    path: Path,
    now: datetime,
    max_age_min: float,
    force: bool,
    time_budget_s: float,
) -> Tuple[Universe, bool, str]:
    existing = load_universe(path)
    if not force and existing is not None and 0 <= existing.age_minutes(now) <= max_age_min:
        return existing, False, ""
    try:
        universe = refresh_universe(client, now, time_budget_s)
    except ApiError as exc:
        if existing is not None and not exc.is_blocked and existing.age_minutes(now) <= STALE_UNIVERSE_LIMIT_MIN:
            return existing, False, f"refresh failed, kept previous list ({exc.status})"
        raise
    save_universe(path, universe)
    return universe, True, ""


def verify_outcome_list(client: PolymarketClient, event: UniverseEvent) -> str:
    """Re-fetch the event on its own and check that our list of outcomes is complete.

    A YES basket is only riskless if it covers every outcome. The event listing could
    in theory omit some markets, so before recording a YES opportunity we compare it
    with GET /events/{id}. Returns "1" (identical), "0" (different) or "error".
    """
    try:
        raw = client.get_event(event.event_id)
    except (ApiError, SchemaError):
        return "error"
    fresh, _reason = classify_event(raw)
    if fresh is None:
        return "0"
    ours = {(m.yes_id, m.no_id) for m in event.markets}
    theirs = {(m.yes_id, m.no_id) for m in fresh.markets}
    return "1" if ours == theirs else "0"


def _fee_sources(evaluation: EventEvaluation) -> str:
    return "+".join(sorted({m.fee_source for m in evaluation.event.markets}))


def opportunity_row(scan_time: str, ev: EventEvaluation, basket: str, r: BasketResult, verified: str = "") -> Dict[str, Any]:
    e = ev.event
    roi = (r.exec_profit / r.exec_cost * 100) if r.exec_cost > 0 else None
    return {
        "scan_time": scan_time,
        "event_id": e.event_id,
        "event_slug": e.slug,
        "event_title": e.title,
        "category": e.category,
        "n_outcomes": len(e.markets),
        "eliminated": e.eliminated,
        "basket": basket,
        "payout_per_unit": fmt(r.payout_per_unit),
        "sum_best_ask": fmt(r.sum_best_ask),
        "gross_edge_top": fmt(r.gross_edge_top),
        "fee_top": fmt(r.fee_top),
        "net_edge_top": fmt(r.net_edge_top),
        "top_size_min": fmt(r.top_size_min, 4),
        "max_size": fmt(r.max_size, 4),
        "max_profit": fmt(r.max_profit),
        "min_size_required": fmt(r.min_size_required, 4),
        "exec_size": fmt(r.exec_size, 4),
        "exec_profit": fmt(r.exec_profit),
        "exec_cost": fmt(r.exec_cost),
        "roi_pct": fmt(roi, 4),
        "suspicious": fmt(is_suspicious(r)),
        "verified": verified,
        "fee_sources": _fee_sources(ev),
        "book_age_max_s": fmt(ev.book_age_max_s, 1),
        "url": f"https://polymarket.com/event/{e.slug}" if e.slug else "",
    }


def snapshot_row(scan_time: str, ev: EventEvaluation) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "scan_time": scan_time,
        "event_id": ev.event.event_id,
        "category": ev.event.category,
        "n_outcomes": len(ev.event.markets),
        "yes_skip": ev.yes_skip,
    }
    for name, r in (("yes", ev.yes), ("no", ev.no)):
        if r is None:
            continue
        row[f"{name}_complete"] = fmt(r.complete)
        row[f"{name}_sum_best_ask"] = fmt(r.sum_best_ask)
        row[f"{name}_net_edge_top"] = fmt(r.net_edge_top)
        row[f"{name}_top_size_min"] = fmt(r.top_size_min, 4)
        row[f"{name}_max_profit"] = fmt(r.max_profit)
    return row


# ---------------------------------------------------------------------------- scan

def run_scan(
    client: PolymarketClient,
    data_dir: Path,
    now: Optional[datetime] = None,
    universe_max_age_min: float = 60.0,
    force_refresh: bool = False,
    refresh_budget_s: float = 300.0,
    min_notional: Decimal = D1,
    write_snapshot: bool = True,
) -> Dict[str, Any]:
    """Run one scan and append its results under `data_dir`. Returns the scan summary.

    Temporary network problems are recorded and do not raise. Being refused by the
    API (401/403) or receiving data in an unknown format raises after recording.
    """
    now = now or datetime.now(timezone.utc)
    scan_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.monotonic()
    requests_before = client.http.request_count
    summary: Dict[str, Any] = {"scan_time": scan_time, "status": "ok"}
    to_raise: Optional[BaseException] = None
    try:
        universe, refreshed, note = ensure_universe(
            client, data_dir / "universe.json", now, universe_max_age_min, force_refresh, refresh_budget_s,
        )
        summary.update({
            "universe_generated_at": universe.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "universe_age_min": fmt(universe.age_minutes(now), 1),
            "universe_refreshed": fmt(refreshed),
            "universe_note": note,
            "universe_events": len(universe.events),
            "universe_markets": sum(len(e.markets) for e in universe.events),
            "gamma_endpoint": universe.stats.get("gamma_endpoint", ""),
            "gamma_pages": universe.stats.get("gamma_pages", ""),
            "gamma_complete": fmt(bool(universe.stats.get("gamma_complete", False))),
        })

        raw_books, book_stats = client.fetch_books(universe.token_ids())
        books, dropped = parse_books(raw_books)
        summary.update({
            "tokens_requested": book_stats["requested"],
            "books_received": book_stats["received"],
            "books_rejected": book_stats["rejected_tokens"],
            "levels_dropped": dropped,
        })

        now_ts = now.timestamp()
        evaluations = [evaluate_event(ev, books, now_ts, min_notional) for ev in universe.events]
        evaluated = [e for e in evaluations if e.status == "ok"]
        opp_rows: List[Dict[str, Any]] = []
        best = D0
        opp_yes = opp_no = yes_rejected = 0
        for ev in evaluated:
            for basket, r in (("YES", ev.yes), ("NO", ev.no)):
                if r is None or not r.is_opportunity:
                    continue
                verified = ""
                if basket == "YES":
                    verified = verify_outcome_list(client, ev.event)
                    if verified == "0":
                        yes_rejected += 1   # outcome list incomplete: not a real arbitrage
                        continue
                opp_rows.append(opportunity_row(scan_time, ev, basket, r, verified))
                best = max(best, r.exec_profit)
                if basket == "YES":
                    opp_yes += 1
                else:
                    opp_no += 1
        append_csv(data_dir / "opportunities" / f"{now:%Y-%m-%d}.csv", OPPORTUNITY_FIELDS, opp_rows)

        if write_snapshot:
            snap_path = data_dir / "snapshots" / f"{now:%Y-%m-%d}" / f"{now:%H}.csv.gz"
            if not snap_path.exists():
                write_gz_csv(snap_path, SNAPSHOT_FIELDS, (snapshot_row(scan_time, e) for e in evaluated))

        summary.update({
            "events_evaluated": len(evaluated),
            "skipped_missing_book": sum(1 for e in evaluations if e.status == "missing_book"),
            "skipped_other": sum(1 for e in evaluations if e.status not in ("ok", "missing_book")),
            "yes_evaluated": sum(1 for e in evaluated if e.yes is not None),
            "yes_skipped_incomplete": sum(1 for e in evaluated if e.yes_skip),
            "opportunities_yes": opp_yes,
            "opportunities_no": opp_no,
            "yes_rejected_by_verification": yes_rejected,
            "best_profit": fmt(best),
        })
    except ApiError as exc:
        summary["status"] = "blocked" if exc.is_blocked else "network_error"
        summary["error"] = str(exc)[:300]
        if exc.is_blocked:
            to_raise = exc
    except SchemaError as exc:
        summary["status"] = "schema_error"
        summary["error"] = str(exc)[:300]
        to_raise = exc
    summary["duration_s"] = fmt(time.monotonic() - t0, 1)
    summary["http_requests"] = client.http.request_count - requests_before
    append_csv(data_dir / "scans" / f"{now:%Y-%m}.csv", SCAN_FIELDS, [summary])
    if to_raise is not None:
        raise to_raise
    return summary
