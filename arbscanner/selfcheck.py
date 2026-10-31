"""Live self-check: proves, on real Polymarket data, that the scanner reads it correctly.

1. Lists every open event (Gamma) and builds the list of eligible events.
2. Runs a real two-stage scan on that list (into a temporary folder).
3. Cross-checks a sample against the full order books: YES/NO identity, condition ids,
   stage-1 prices = full-book prices, NO book = mirror of the YES book, completeness
   of the event listing, and basket costs as theory predicts.

Every check prints PASS / WARN / FAIL with numbers. Any FAIL -> exit code 1.
"""

from __future__ import annotations

import statistics
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, List, Tuple

from .api import PolymarketClient
from .basket import D1
from .evaluate import COMPLETE_SET_MIN_SUM, evaluate_event, parse_books
from .httpclient import ApiError
from .scan import run_scan, save_universe
from .screen import build_price_table, screen_event
from .universe import SchemaError, build_universe, classify_event

MARKET_FIELDS = ("outcomes", "outcomePrices", "acceptingOrders", "orderMinSize", "feesEnabled", "feeSchedule")


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def check(self, ok: bool, label: str, detail: str = "", warn_only: bool = False) -> bool:
        tag = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        if not ok:
            if warn_only:
                self.warnings += 1
            else:
                self.failures += 1
        print(f"[{tag}] {label}" + (f" — {detail}" if detail else ""), flush=True)
        return ok


def _ratio(n: int, d: int) -> str:
    return f"{n}/{d} ({(100.0 * n / d) if d else 0:.1f}%)"


def info(text: str) -> None:
    print(f"       {text}", flush=True)


def run_selfcheck(client: PolymarketClient, max_events: int = 300, time_budget_s: float = 1500.0) -> int:
    rep = Report()
    now = datetime.now(timezone.utc)
    print(f"Self-check started {now:%Y-%m-%d %H:%M:%S} UTC", flush=True)
    print(f"Gamma: {client.gamma_url} | CLOB: {client.clob_url}", flush=True)

    # 1) Gamma: list every open event ------------------------------------------------
    t0 = time.monotonic()
    counts: Counter = Counter()

    def tally(events: Iterator[Any]) -> Iterator[Any]:
        for ev in events:
            counts["events"] += 1
            if isinstance(ev, dict) and ev.get("negRisk") is True:
                counts["neg_events"] += 1
                counts["neg_events_with_aug_flag"] += int("negRiskAugmented" in ev)
                counts["neg_events_with_end"] += int(bool(ev.get("endDate")))
                for m in ev.get("markets") or []:
                    if isinstance(m, dict):
                        counts["neg_markets"] += 1
                        counts["ids"] += int(bool(m.get("clobTokenIds") or m.get("positionIds")))
                        for key in MARKET_FIELDS:
                            counts[key] += int(m.get(key) is not None)
            yield ev

    try:
        universe = build_universe(tally(client.iter_open_events(time_budget_s=time_budget_s)), now)
    except ApiError as exc:
        rep.check(False, "Gamma API reachable", str(exc)[:300])
        return _finish(rep)
    except SchemaError as exc:
        rep.check(False, "Gamma API format understood", str(exc)[:300])
        return _finish(rep)
    listing_s = time.monotonic() - t0
    stats = client.last_fetch_stats
    rep.check(counts["events"] > 0, "Gamma API reachable and returns open events",
              f"{counts['events']} events, {stats.get('pages')} pages via '{stats.get('endpoint')}' in {listing_s:.0f}s "
              f"({counts['events'] / max(listing_s, 1e-9):.0f} events/s)")
    rep.check(bool(stats.get("complete")), "Full list of open events read",
              "" if stats.get("complete") else f"stopped early: {stats.get('stopped')}", warn_only=True)
    rep.check(counts["neg_events"] > 0, "Neg-risk (multi-outcome) events found", f"{counts['neg_events']} events with negRisk=true")
    if counts["neg_markets"]:
        for key in MARKET_FIELDS:
            info(f"field '{key}' present on {_ratio(counts[key], counts['neg_markets'])} of neg-risk markets")
        rep.check(counts["ids"] >= 0.9 * counts["neg_markets"], "Token ids present on neg-risk markets",
                  _ratio(counts["ids"], counts["neg_markets"]))
        info(f"field 'negRiskAugmented' present on {_ratio(counts['neg_events_with_aug_flag'], counts['neg_events'])} of neg-risk events")
        info(f"field 'endDate' (when the event ends) present on {_ratio(counts['neg_events_with_end'], counts['neg_events'])} of neg-risk events")
    info(f"eligibility reasons: {universe.stats['reasons']}")
    info(f"fee sources: {universe.stats['fee_sources']}")
    rep.check(len(universe.events) > 0, "Eligible standard neg-risk events found",
              f"{len(universe.events)} events / {universe.stats['eligible_markets']} outcomes")
    if not universe.events:
        return _finish(rep)
    universe.stats.update({"gamma_endpoint": stats.get("endpoint", ""), "gamma_pages": stats.get("pages", 0),
                           "gamma_complete": bool(stats.get("complete", False))})

    # 2) A real two-stage scan on this list ----------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "universe.json.gz"
        save_universe(state, universe)
        info(f"event list saved compressed: {state.stat().st_size / 1e6:.1f} MB")
        try:
            s = run_scan(client, Path(tmp) / "data", now=datetime.now(timezone.utc), universe_path=state,
                         universe_max_age_min=10 ** 6)
        except (ApiError, SchemaError) as exc:
            rep.check(False, "Two-stage scan on the full list", str(exc)[:400])
            return _finish(rep)
    ok = rep.check(s.get("status") == "ok", "Two-stage scan on the full list",
                   f"status {s.get('status')} {s.get('error', '')}".strip())
    if not ok:
        return _finish(rep)
    tokens, priced = int(s["prices_tokens"]), int(s["prices_priced"])
    rep.check(priced >= tokens - max(25, 0.02 * tokens), "Stage 1: best prices received for (almost) every YES token",
              f"{_ratio(priced, tokens)} in {s['prices_requests']} requests, {s['stage1_s']}s; "
              f"uncertain {s['prices_uncertain']}, refused {s['prices_rejected']}")
    info(f"which /prices side is the ask, decided from the data: {s['side_mapping']} (agreeing tokens {s['side_votes']})")
    info(f"candidates: {s['candidate_events']} events of {s['screened_events']} "
         f"(YES {s['candidates_yes']}, NO {s['candidates_no']}, uncertain {s['candidates_uncertain']})")
    rep.check(int(s["candidate_events"]) <= 0.25 * int(s["screened_events"]), "Stage 1 keeps the full-book reads small",
              f"{s['candidate_events']} candidate events of {s['screened_events']}", warn_only=True)
    # Markets that close while the list is being read (a match ends) have no book any more:
    # stage 1 already flags them, so only those events may lack a book.
    req = int(s["tokens_requested"])
    missing, closed, other = int(s["skipped_missing_book"]), int(s["candidates_uncertain"]), int(s["skipped_other"])
    rep.check(missing <= closed and other == 0, "Stage 2: full order books read for every candidate still open",
              f"books {_ratio(int(s['books_received']), req)} in {s['stage2_s']}s; events evaluated {s['events_evaluated']}, "
              f"without a book {missing} (markets closed since the list was built: {closed} events), other problems {other}")
    n_chk = int(s["check_tokens"])
    if n_chk:
        info(f"stage 1 vs stage 2 (seconds apart) on candidates: ask equal {_ratio(int(s['check_ask_equal']), n_chk)}, "
             f"bid equal {_ratio(int(s['check_bid_equal']), n_chk)}, NO = 1 - YES bid {_ratio(int(s['check_mirror_equal']), n_chk)}")
    info(f"events created during the listing, added by the newest-first check: {s.get('new_events_added', 0)} "
         f"(of {s.get('new_events_seen', 0)} new) {s.get('universe_note', '')}".rstrip())
    info(f"opportunities: YES {s['opportunities_yes']}, NO {s['opportunities_no']}, best ${s['best_profit']}; "
         f"total requests {s['http_requests']}, duration {s['duration_s']}s")

    # 3) Cross-check a sample against the full order books ---------------------------------
    sample = sorted(universe.events, key=lambda e: e.event_id)[:max_events]
    yes_tokens = [m.yes_id for e in sample for m in e.markets]
    try:
        raw_prices, _ = client.fetch_prices(yes_tokens)
        raw_books, bstats = client.fetch_books([t for e in sample for m in e.markets for t in (m.yes_id, m.no_id)])
        table = build_price_table(raw_prices, yes_tokens)
        books, dropped = parse_books(raw_books)
    except ApiError as exc:
        rep.check(False, "CLOB prices and order books reachable", str(exc)[:300])
        return _finish(rep)
    except SchemaError as exc:
        rep.check(False, "CLOB prices and order book formats understood", str(exc)[:300])
        return _finish(rep)
    n_tok = 2 * len(yes_tokens)
    rep.check(bstats["received"] >= 0.9 * n_tok, "Order books received for the sampled outcomes",
              f"{_ratio(bstats['received'], n_tok)}, refused ids: {bstats['rejected_tokens']}, requests: {bstats['requests']}")
    info(f"price levels dropped as invalid: {dropped}")
    raw_sorted = sum(1 for b in raw_books.values() if _is_sorted(b.get("asks"), True) and _is_sorted(b.get("bids"), False))
    info(f"books already sorted as documented: {_ratio(raw_sorted, len(raw_books))} (we sort them ourselves anyway)")
    neg_flags = [b.neg_risk for b in books.values() if b.neg_risk is not None]
    rep.check(all(neg_flags) if neg_flags else True, "Books are flagged neg_risk=true",
              f"{sum(1 for f in neg_flags if f)}/{len(neg_flags)} flagged true")

    matched_cond = total_cond = close_mid = total_mid = 0
    ask_eq = bid_eq = compared = mirror_eq = mirror_near = mirror_total = 0
    for e in sample:
        for m in e.markets:
            by, bn = books.get(m.yes_id), books.get(m.no_id)
            if by is None or bn is None:
                continue
            for b in (by, bn):
                if m.condition_id and b.condition_id:
                    total_cond += 1
                    matched_cond += int(m.condition_id.lower().removeprefix("0x") == b.condition_id.lower().removeprefix("0x"))
            if by.asks and by.bids and m.yes_price is not None:
                total_mid += 1
                close_mid += int(abs((by.asks[0].price + by.bids[0].price) / 2 - m.yes_price) <= Decimal("0.10"))
            top = table.tops.get(m.yes_id)
            if top is not None:
                compared += 1
                ask_eq += int(top.ask == (by.asks[0].price if by.asks else None))
                bid_eq += int(top.bid == (by.bids[0].price if by.bids else None))
            if by.bids and bn.asks:
                mirror_total += 1
                diff = abs(bn.asks[0].price - (D1 - by.bids[0].price))
                mirror_eq += int(diff == 0)
                mirror_near += int(diff <= Decimal("0.011"))
    rep.check(total_cond == 0 or matched_cond == total_cond, "Book condition ids match Gamma", _ratio(matched_cond, total_cond))
    rep.check(total_mid == 0 or close_mid >= 0.8 * total_mid, "YES token price matches Gamma's displayed YES price (±0.10)",
              _ratio(close_mid, total_mid))
    info(f"sample: /prices says {table.mapping.label} (agreeing tokens {table.mapping.agree}/{table.mapping.votes})")
    rep.check(compared > 0 and ask_eq >= 0.9 * compared, "Stage-1 best ask = best ask of the full book",
              _ratio(ask_eq, compared))
    rep.check(compared > 0 and bid_eq >= 0.9 * compared, "Stage-1 best bid = best bid of the full book",
              _ratio(bid_eq, compared))
    rep.check(mirror_total == 0 or mirror_eq >= 0.95 * mirror_total, "NO best ask = 1 − YES best bid, exactly (shared book)",
              f"exact {_ratio(mirror_eq, mirror_total)}, within 0.011: {_ratio(mirror_near, mirror_total)}")

    # Is the listing complete? Compare the biggest events with GET /events/{id}.
    biggest = sorted(sample, key=lambda e: len(e.markets), reverse=True)[:5]
    same = 0
    for e in biggest:
        try:
            fresh, _ = classify_event(client.get_event(e.event_id))
        except (ApiError, SchemaError) as exc:
            info(f"could not fetch event {e.event_id}: {str(exc)[:120]}")
            continue
        if fresh is not None and {(m.yes_id, m.no_id) for m in fresh.markets} == {(m.yes_id, m.no_id) for m in e.markets}:
            same += 1
        else:
            info(f"event {e.event_id} '{e.title[:60]}': listing has {len(e.markets)} outcomes, "
                 f"single fetch has {len(fresh.markets) if fresh else 'n/a'}")
    rep.check(same == len(biggest), "Event listing contains every outcome (checked on the largest events)",
              _ratio(same, len(biggest)))

    # Screening can never hide an opportunity: every basket that the full books show as
    # profitable must have been a stage-1 candidate.
    evals = [evaluate_event(e, books, now.timestamp()) for e in sample]
    ok_evals = [x for x in evals if x.status == "ok"]
    rep.check(len(ok_evals) > 0, "Events fully evaluated", _ratio(len(ok_evals), len(evals)))
    info(f"evaluation status: {dict(Counter(x.status for x in evals))}")
    missed = 0
    for x in ok_evals:
        sc = screen_event(x.event, table)
        if (x.yes is not None and x.yes.is_opportunity and not (sc.yes_candidate or sc.uncertain)) or \
           (x.no is not None and x.no.is_opportunity and not (sc.no_candidate or sc.uncertain)):
            missed += 1
    rep.check(missed == 0, "No profitable basket in the sample was screened out by stage 1", f"missed {missed}")

    yes_costs = [x.yes.sum_best_ask for x in ok_evals if x.yes is not None and x.yes.sum_best_ask is not None]
    no_ratios = [x.no.sum_best_ask / x.no.payout_per_unit for x in ok_evals
                 if x.no is not None and x.no.sum_best_ask is not None and x.no.payout_per_unit > 0]
    if yes_costs:
        med = statistics.median(yes_costs)
        rep.check(med >= Decimal("0.97"), "Median cost of a YES basket ≈ or above $1 (as theory predicts)",
                  f"median {med:.4f}, min {min(yes_costs):.4f}, below $1: {_ratio(sum(1 for v in yes_costs if v < 1), len(yes_costs))}")
    if no_ratios:
        med = statistics.median(no_ratios)
        rep.check(med >= Decimal("0.97"), "Median cost of a NO basket ≈ or above its payout",
                  f"median {med:.4f} of payout, below payout: {_ratio(sum(1 for v in no_ratios if v < 1), len(no_ratios))}")

    rows: List[Tuple[Decimal, str]] = []
    for x in ok_evals:
        for name, r in (("YES", x.yes), ("NO", x.no)):
            if r is not None and r.net_edge_top is not None:
                rows.append((r.net_edge_top / r.payout_per_unit, f"{name:3} n={len(x.event.markets):3} "
                             f"cost/payout={r.sum_best_ask / r.payout_per_unit:.4f} net_edge={r.net_edge_top:.4f} "
                             f"exec_profit=${r.exec_profit:.4f} | {x.event.title[:70]}"))
    rows.sort(key=lambda t: t[0], reverse=True)
    print("\nClosest baskets to an arbitrage in the sample (best first):", flush=True)
    for _, line in rows[:10]:
        print("   " + line, flush=True)
    print(f"Completeness guard: YES baskets skipped when displayed prices sum < {COMPLETE_SET_MIN_SUM}: "
          f"{sum(1 for x in ok_evals if x.yes_skip)}", flush=True)

    _diagnostics(client, sample)
    return _finish(rep)


def _diagnostics(client: PolymarketClient, sample) -> None:
    """Facts about API behaviour, printed for information only (never a failure)."""
    print("\nAPI behaviour (information):", flush=True)
    good = sample[0].markets[0].yes_id
    for path, payload in (
        ("/prices", [{"token_id": good, "side": "BUY"}, {"token_id": "1", "side": "BUY"}]),
        ("/books", [{"token_id": good}, {"token_id": "1"}]),
    ):
        try:
            resp = client.post_json(client.clob_url, path, payload)
            kind = f"{type(resp).__name__} with {len(resp)} entries" if isinstance(resp, (list, dict)) else type(resp).__name__
            info(f"POST {path} with one valid and one unknown token id: HTTP 200, {kind}")
        except ApiError as exc:
            info(f"POST {path} with one valid and one unknown token id: HTTP {exc.status} {exc.body[:120]}")
    try:
        page = client.get_json(client.gamma_url, "/events/keyset",
                               {"closed": False, "limit": 20, "order": "id", "ascending": False})
        ids = [int(e["id"]) for e in page.get("events", [])] if isinstance(page, dict) else []
        info(f"newest-first listing (order=id, ascending=false): first ids {ids[:5]}, "
             f"descending={ids == sorted(ids, reverse=True)}, next_cursor={'yes' if isinstance(page, dict) and page.get('next_cursor') else 'no'}")
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        info(f"newest-first listing not available: {str(exc)[:160]}")


def _is_sorted(levels: Any, ascending: bool) -> bool:
    try:
        prices = [Decimal(str(l["price"])) for l in levels]
    except Exception:  # noqa: BLE001
        return False
    return prices == sorted(prices, reverse=not ascending)


def _finish(rep: Report) -> int:
    print(f"\nRESULT: {'FAIL' if rep.failures else 'PASS'} — {rep.failures} failure(s), {rep.warnings} warning(s)", flush=True)
    return 1 if rep.failures else 0
