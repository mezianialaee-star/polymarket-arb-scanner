"""One scan, in two stages.

Stage 1 (every event): best bid/ask of every YES token with POST /prices, then
screening (screen.py). Stage 2 (candidates only): full order books of every YES and
NO token of the event, then the exact evaluation with fees, order minimums and all
safety checks (evaluate.py). Only stage 2 can report an opportunity.
"""

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
from .evaluate import EventEvaluation, ParsedBook, evaluate_event, is_suspicious, parse_books
from .httpclient import ApiError
from .screen import DEFAULT_MARGIN, EventScreen, PriceTable, build_price_table, screen_event
from .universe import SchemaError, Universe, UniverseEvent, build_universe, classify_event, event_number

OPPORTUNITY_FIELDS = [
    "scan_time", "event_id", "event_slug", "event_title", "category", "end_date", "n_outcomes", "eliminated",
    "basket", "payout_per_unit", "sum_best_ask", "gross_edge_top", "fee_top", "net_edge_top",
    "top_size_min", "max_size", "max_profit", "min_size_required", "exec_size", "exec_profit",
    "exec_cost", "roi_pct", "suspicious", "verified", "fee_sources", "book_age_max_s", "url",
    "outcomes",
]

SCAN_FIELDS = [
    "scan_time", "status", "duration_s", "universe_generated_at", "universe_age_min",
    "universe_refreshed", "universe_note", "universe_events", "universe_markets", "gamma_endpoint",
    "gamma_pages", "gamma_complete", "new_events_seen", "new_events_added",
    # stage 1
    "prices_tokens", "prices_priced", "prices_uncertain", "prices_missing", "prices_odd",
    "prices_rejected", "prices_requests",
    "side_mapping", "side_votes", "stage1_s", "screened_events", "candidates_yes", "candidates_no",
    "candidates_uncertain", "candidate_events",
    # stage 2
    "tokens_requested", "books_received", "books_rejected", "levels_dropped", "events_evaluated",
    "skipped_missing_book", "skipped_other", "stage2_s",
    "check_tokens", "check_ask_equal", "check_bid_equal", "check_mirror_equal",
    # results
    "yes_evaluated", "yes_skipped_incomplete", "opportunities_yes", "opportunities_no",
    "yes_rejected_by_verification", "best_profit", "http_requests", "error",
]

SNAPSHOT_FIELDS = [
    "scan_time", "event_id", "category", "n_outcomes", "yes_cost", "yes_fee_top",
    "no_cost", "no_fee_top", "yes_guarded", "uncertain",
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


def _header(path: Path) -> Optional[List[str]]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return next(csv.reader(fh), None)
    except OSError:
        return None


def append_csv(path: Path, fields: List[str], rows: Iterable[Dict[str, Any]]) -> int:
    """Append rows. If the file was written with other columns (older version of the
    scanner), continue in a new file next to it instead of mixing formats."""
    rows = list(rows)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    target, k = path, 1
    while target.exists() and target.stat().st_size > 0 and _header(target) != fields:
        k += 1
        target = path.with_name(f"{path.stem}-v{k}{path.suffix}")
    new_file = not target.exists() or target.stat().st_size == 0
    with open(target, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="raise")
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k2: row.get(k2, "") for k2 in fields})
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


# ------------------------------------------------------------------------ universe

def load_universe(path: Path) -> Optional[Universe]:
    if not path.exists():
        return None
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = path.read_text(encoding="utf-8")
        return Universe.from_json(text)
    except (OSError, EOFError, ValueError, KeyError, TypeError):
        return None  # unreadable, truncated or old format: rebuild it


def save_universe(path: Path, universe: Universe) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if path.suffix == ".gz":
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(universe.to_json(compact=True))
    else:
        tmp.write_text(universe.to_json(), encoding="utf-8")
    os.replace(tmp, path)


def merge_with_previous(new: Universe, previous: Optional[Universe], now: datetime) -> Universe:
    """If the listing stopped early, keep the events it did not reach from the previous
    list (when that list is recent), so one slow listing does not shrink the scan."""
    if new.stats.get("gamma_complete") or previous is None:
        return new
    if previous.age_minutes(now) > STALE_UNIVERSE_LIMIT_MIN:
        return new
    seen_events = {e.event_id for e in new.events}
    seen_tokens = {t for e in new.events for m in e.markets for t in (m.yes_id, m.no_id)}
    kept = 0
    for e in previous.events:
        tokens = {t for m in e.markets for t in (m.yes_id, m.no_id)}
        if e.event_id in seen_events or tokens & seen_tokens:
            continue
        new.events.append(e)
        seen_events.add(e.event_id)
        seen_tokens |= tokens
        kept += 1
    new.stats["kept_from_previous"] = kept
    new.stats["max_event_id"] = max(int(new.stats.get("max_event_id") or 0), int(previous.stats.get("max_event_id") or 0))
    return new


def add_new_events(client: PolymarketClient, universe: Universe, max_pages: int = 5) -> Tuple[int, int, str]:
    """Add events created since the list was built (for this scan only), so a new event
    is scanned within 10 minutes instead of waiting for the hourly rebuild.

    Reads the listing newest first and stops at the first event the list already knew
    of. Returns (new events seen, eligible events added, note). Never fails the scan.
    """
    try:
        max_known = int(universe.stats.get("max_event_id") or 0)
    except (TypeError, ValueError):
        max_known = 0
    if max_known <= 0:
        return 0, 0, "no reference id"
    seen_events = {e.event_id for e in universe.events}
    seen_tokens = {t for e in universe.events for m in e.markets for t in (m.yes_id, m.no_id)}
    newer = added = 0
    try:
        for raw in client.iter_newest_events(max_pages):
            if event_number(raw) <= max_known:
                break
            newer += 1
            ev, _reason = classify_event(raw)
            if ev is None or ev.event_id in seen_events:
                continue
            tokens = {t for m in ev.markets for t in (m.yes_id, m.no_id)}
            if tokens & seen_tokens:
                continue
            universe.events.append(ev)
            seen_events.add(ev.event_id)
            seen_tokens |= tokens
            added += 1
    except (ApiError, SchemaError) as exc:
        return newer, added, f"new-event check stopped: {str(exc)[:120]}"
    return newer, added, ""


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
    universe = merge_with_previous(universe, existing, now)
    note = ""
    if not universe.stats.get("gamma_complete"):
        note = f"listing incomplete ({universe.stats.get('gamma_stopped')}), kept {universe.stats.get('kept_from_previous', 0)} events from the previous list"
    save_universe(path, universe)
    return universe, True, note


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


# ---------------------------------------------------------------------------- rows

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
        "end_date": e.end_date,
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
        # A YES basket is riskless only if these outcomes cover every possibility: the
        # scanner cannot read the rules, so YES opportunities must be checked by hand.
        "outcomes": " | ".join(m.title for m in e.markets)[:600],
    }


def snapshot_row(scan_time: str, s: EventScreen) -> Dict[str, Any]:
    return {
        "scan_time": scan_time,
        "event_id": s.event.event_id,
        "category": s.event.category,
        "n_outcomes": len(s.event.markets),
        "yes_cost": fmt(s.yes_cost, 4),
        "yes_fee_top": fmt(s.yes_fee, 4),
        "no_cost": fmt(s.no_cost, 4),
        "no_fee_top": fmt(s.no_fee, 4),
        "yes_guarded": fmt(s.yes_guarded),
        "uncertain": fmt(s.uncertain),
    }


def snapshot_path(data_dir: Path, now: datetime, every_hours: int) -> Path:
    slot = (now.hour // every_hours) * every_hours
    return data_dir / "snapshots" / f"{now:%Y-%m-%d}" / f"{slot:02d}.csv.gz"


# ------------------------------------------------------------------ stage 1 vs 2

def compare_stages(candidates: List[EventScreen], table: PriceTable, books: Dict[str, ParsedBook]) -> Dict[str, int]:
    """How often stage 1's best prices equal the best prices of the full books read a
    few seconds later (monitoring only: prices can move in between)."""
    out = {"check_tokens": 0, "check_ask_equal": 0, "check_bid_equal": 0, "check_mirror_equal": 0}
    for s in candidates:
        for m in s.event.markets:
            top, by, bn = table.tops.get(m.yes_id), books.get(m.yes_id), books.get(m.no_id)
            if top is None or by is None or bn is None:
                continue
            out["check_tokens"] += 1
            book_ask = by.asks[0].price if by.asks else None
            book_bid = by.bids[0].price if by.bids else None
            out["check_ask_equal"] += int(top.ask == book_ask)
            out["check_bid_equal"] += int(top.bid == book_bid)
            no_ask = bn.asks[0].price if bn.asks else None
            mirror = (D1 - book_bid) if book_bid is not None else None
            out["check_mirror_equal"] += int(no_ask == mirror)
    return out


# ---------------------------------------------------------------------------- scan

def run_scan(
    client: PolymarketClient,
    data_dir: Path,
    now: Optional[datetime] = None,
    universe_path: Optional[Path] = None,
    universe_max_age_min: float = 60.0,
    force_refresh: bool = False,
    refresh_budget_s: float = 1200.0,
    min_notional: Decimal = D1,
    margin: Decimal = DEFAULT_MARGIN,
    write_snapshot: bool = True,
    snapshot_every_hours: int = 3,
    discover_new: bool = True,
) -> Dict[str, Any]:
    """Run one scan and append its results under `data_dir`. Returns the scan summary.

    Temporary network problems are recorded and do not raise. Being refused by the
    API (401/403) or receiving data in an unknown format raises after recording.
    """
    now = now or datetime.now(timezone.utc)
    universe_path = universe_path or (data_dir / "universe.json.gz")
    scan_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.monotonic()
    requests_before = client.http.request_count
    summary: Dict[str, Any] = {"scan_time": scan_time, "status": "ok"}
    to_raise: Optional[BaseException] = None
    try:
        universe, refreshed, note = ensure_universe(
            client, universe_path, now, universe_max_age_min, force_refresh, refresh_budget_s,
        )
        if not refreshed and discover_new:
            newer, added, discovery_note = add_new_events(client, universe)
            summary.update({"new_events_seen": newer, "new_events_added": added})
            note = "; ".join(x for x in (note, discovery_note) if x)
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

        # ---- stage 1: best prices of every YES token, screening -------------------
        t1 = time.monotonic()
        yes_tokens = [m.yes_id for e in universe.events for m in e.markets]
        raw_prices, pstats = client.fetch_prices(yes_tokens)
        table = build_price_table(raw_prices, yes_tokens, pstats["rejected_tokens"])
        screens = [screen_event(e, table, margin) for e in universe.events]
        candidates = [s for s in screens if s.candidate]
        summary.update({
            "prices_tokens": len(yes_tokens),
            "prices_priced": table.stats["priced"],
            "prices_uncertain": len(table.uncertain),
            "prices_missing": table.stats["missing"],
            "prices_odd": table.stats["malformed"] + table.stats["crossed"],
            "prices_rejected": pstats["rejected_tokens"],
            "prices_requests": pstats["requests"],
            "side_mapping": table.mapping.label,
            "side_votes": f"{table.mapping.agree}/{table.mapping.votes}",
            "stage1_s": fmt(time.monotonic() - t1, 1),
            "screened_events": len(screens),
            "candidates_yes": sum(1 for s in screens if s.yes_candidate),
            "candidates_no": sum(1 for s in screens if s.no_candidate),
            "candidates_uncertain": sum(1 for s in screens if s.uncertain),
            "candidate_events": len(candidates),
        })
        if write_snapshot:
            snap = snapshot_path(data_dir, now, max(1, snapshot_every_hours))
            if not snap.exists():
                write_gz_csv(snap, SNAPSHOT_FIELDS, (snapshot_row(scan_time, s) for s in screens))

        # ---- stage 2: full order books of the candidates, exact evaluation ---------
        t2 = time.monotonic()
        tokens = [t for s in candidates for m in s.event.markets for t in (m.yes_id, m.no_id)]
        raw_books, book_stats = client.fetch_books(tokens) if tokens else ({}, {"requested": 0, "received": 0, "rejected_tokens": 0})
        books, dropped = parse_books(raw_books)
        summary.update({
            "tokens_requested": book_stats["requested"],
            "books_received": book_stats["received"],
            "books_rejected": book_stats["rejected_tokens"],
            "levels_dropped": dropped,
        })
        summary.update(compare_stages(candidates, table, books))

        now_ts = now.timestamp()
        evaluations = [evaluate_event(s.event, books, now_ts, min_notional) for s in candidates]
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

        summary.update({
            "events_evaluated": len(evaluated),
            "skipped_missing_book": sum(1 for e in evaluations if e.status == "missing_book"),
            "skipped_other": sum(1 for e in evaluations if e.status not in ("ok", "missing_book")),
            "stage2_s": fmt(time.monotonic() - t2, 1),
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
