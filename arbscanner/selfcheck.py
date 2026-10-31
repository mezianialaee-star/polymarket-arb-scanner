"""Live self-check: proves, on real Polymarket data, that the scanner reads it correctly.

Every check prints PASS / WARN / FAIL with numbers. Any FAIL -> exit code 1.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from .api import PolymarketClient
from .basket import D1
from .evaluate import COMPLETE_SET_MIN_SUM, evaluate_event, parse_books
from .httpclient import ApiError
from .universe import SchemaError, build_universe, classify_event


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
    return f"{n}/{d} ({(100.0 * n / d) if d else 0:.0f}%)"


def run_selfcheck(client: PolymarketClient, max_events: int = 300, time_budget_s: float = 240.0) -> int:
    rep = Report()
    now = datetime.now(timezone.utc)
    print(f"Self-check started {now:%Y-%m-%d %H:%M:%S} UTC", flush=True)
    print(f"Gamma: {client.gamma_url} | CLOB: {client.clob_url}", flush=True)

    # 1) Gamma: list open events --------------------------------------------------
    t0 = time.monotonic()
    neg: List[Dict[str, Any]] = []   # keep only neg-risk events in memory
    total_events = 0
    try:
        for ev in client.iter_open_events(time_budget_s=time_budget_s):
            total_events += 1
            if isinstance(ev, dict) and ev.get("negRisk") is True:
                neg.append(ev)
    except ApiError as exc:
        rep.check(False, "Gamma API reachable", str(exc)[:300])
        return _finish(rep)
    except SchemaError as exc:
        rep.check(False, "Gamma API format understood", str(exc)[:300])
        return _finish(rep)
    stats = client.last_fetch_stats
    rep.check(total_events > 0, "Gamma API reachable and returns open events",
              f"{total_events} events, {stats.get('pages')} pages via '{stats.get('endpoint')}' in {time.monotonic() - t0:.1f}s")
    rep.check(bool(stats.get("complete")), "Full list of open events read",
              f"stopped early: {stats.get('stopped')}" if not stats.get("complete") else "", warn_only=True)

    rep.check(len(neg) > 0, "Neg-risk (multi-outcome) events found", f"{len(neg)} events with negRisk=true")
    markets = [m for e in neg for m in (e.get("markets") or []) if isinstance(m, dict)]
    if markets:
        def present(key: str) -> int:
            return sum(1 for m in markets if m.get(key) is not None)
        for key in ("outcomes", "outcomePrices", "acceptingOrders", "orderMinSize", "feesEnabled", "feeSchedule"):
            n = present(key)
            print(f"       field '{key}' present on {_ratio(n, len(markets))} of neg-risk markets", flush=True)
        ids = sum(1 for m in markets if m.get("clobTokenIds") or m.get("positionIds"))
        rep.check(ids >= 0.9 * len(markets), "Token ids present on neg-risk markets", _ratio(ids, len(markets)))
        aug_flag = sum(1 for e in neg if "negRiskAugmented" in e)
        print(f"       field 'negRiskAugmented' present on {_ratio(aug_flag, len(neg))} of neg-risk events", flush=True)

    universe = build_universe(neg, now)
    print(f"       eligibility reasons: {universe.stats['reasons']}", flush=True)
    print(f"       fee sources: {universe.stats['fee_sources']}", flush=True)
    rep.check(len(universe.events) > 0, "Eligible standard neg-risk events found",
              f"{len(universe.events)} events / {universe.stats['eligible_markets']} outcomes")
    if not universe.events:
        return _finish(rep)

    # 2) CLOB: order books --------------------------------------------------------
    sample = sorted(universe.events, key=lambda e: e.event_id)[:max_events]
    tokens = [t for e in sample for m in e.markets for t in (m.yes_id, m.no_id)]
    try:
        raw_books, bstats = client.fetch_books(tokens)
        books, dropped = parse_books(raw_books)
    except ApiError as exc:
        rep.check(False, "CLOB order books reachable", str(exc)[:300])
        return _finish(rep)
    except SchemaError as exc:
        rep.check(False, "CLOB order book format understood", str(exc)[:300])
        return _finish(rep)
    rep.check(bstats["received"] >= 0.9 * len(set(tokens)), "Order books received for the sampled outcomes",
              f"{_ratio(bstats['received'], len(set(tokens)))}, rejected ids: {bstats['rejected_tokens']}, requests: {bstats['requests']}")
    print(f"       price levels dropped as invalid: {dropped}", flush=True)

    raw_sorted = sum(1 for b in raw_books.values() if _is_sorted(b.get("asks"), ascending=True) and _is_sorted(b.get("bids"), ascending=False))
    print(f"       books already sorted as documented: {_ratio(raw_sorted, len(raw_books))} (we sort them ourselves anyway)", flush=True)

    neg_flags = [b.neg_risk for b in books.values() if b.neg_risk is not None]
    rep.check(all(neg_flags) if neg_flags else True, "Books are flagged neg_risk=true",
              f"{sum(1 for f in neg_flags if f)}/{len(neg_flags)} flagged true")

    # 3) Identity checks: are YES/NO tokens really what we think? -------------------
    matched_cond = total_cond = 0
    close_mid = total_mid = 0
    mirror_ok = mirror_total = 0
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
                mid = (by.asks[0].price + by.bids[0].price) / 2
                close_mid += int(abs(mid - m.yes_price) <= Decimal("0.10"))
            if by.bids and bn.asks:
                mirror_total += 1
                mirror_ok += int(abs(bn.asks[0].price - (D1 - by.bids[0].price)) <= Decimal("0.011"))
    rep.check(total_cond == 0 or matched_cond == total_cond, "Book condition ids match Gamma", _ratio(matched_cond, total_cond))
    rep.check(total_mid == 0 or close_mid >= 0.8 * total_mid, "YES token price matches Gamma's displayed YES price (±0.10)",
              _ratio(close_mid, total_mid))
    rep.check(mirror_total == 0 or mirror_ok >= 0.8 * mirror_total, "NO best ask ≈ 1 − YES best bid (YES/NO pairing)",
              _ratio(mirror_ok, mirror_total), warn_only=True)

    # 3b) Is the listing complete? Compare the biggest events with GET /events/{id} ---------
    biggest = sorted(sample, key=lambda e: len(e.markets), reverse=True)[:5]
    same = 0
    for e in biggest:
        try:
            fresh, _ = classify_event(client.get_event(e.event_id))
        except (ApiError, SchemaError) as exc:
            print(f"       could not fetch event {e.event_id}: {str(exc)[:120]}", flush=True)
            continue
        if fresh is not None and {(m.yes_id, m.no_id) for m in fresh.markets} == {(m.yes_id, m.no_id) for m in e.markets}:
            same += 1
        else:
            print(f"       event {e.event_id} '{e.title[:60]}': listing has {len(e.markets)} outcomes, "
                  f"single fetch has {len(fresh.markets) if fresh else 'n/a'}", flush=True)
    rep.check(same == len(biggest), "Event listing contains every outcome (checked on the largest events)",
              _ratio(same, len(biggest)))

    # 4) Market-level sanity: baskets should almost always cost >= their payout -----------
    evals = [evaluate_event(e, books, now.timestamp()) for e in sample]
    ok_evals = [x for x in evals if x.status == "ok"]
    rep.check(len(ok_evals) > 0, "Events fully evaluated", _ratio(len(ok_evals), len(evals)))
    statuses: Dict[str, int] = {}
    for x in evals:
        statuses[x.status] = statuses.get(x.status, 0) + 1
    print(f"       evaluation status: {statuses}", flush=True)

    yes_ratios = [x.yes.sum_best_ask for x in ok_evals if x.yes is not None and x.yes.sum_best_ask is not None]
    no_ratios = [x.no.sum_best_ask / x.no.payout_per_unit for x in ok_evals
                 if x.no is not None and x.no.sum_best_ask is not None and x.no.payout_per_unit > 0]
    if yes_ratios:
        med = statistics.median(yes_ratios)
        below = sum(1 for v in yes_ratios if v < 1)
        rep.check(med >= Decimal("0.97"), "Median cost of a YES basket ≈ or above $1 (as theory predicts)",
                  f"median {med:.4f}, min {min(yes_ratios):.4f}, max {max(yes_ratios):.4f}, below $1: {_ratio(below, len(yes_ratios))}")
    if no_ratios:
        med = statistics.median(no_ratios)
        below = sum(1 for v in no_ratios if v < 1)
        rep.check(med >= Decimal("0.97"), "Median cost of a NO basket ≈ or above its payout",
                  f"median {med:.4f} of payout, below payout: {_ratio(below, len(no_ratios))}")

    # 5) Show what was found ----------------------------------------------------------
    rows: List[Tuple[Decimal, str]] = []
    for x in ok_evals:
        for name, r in (("YES", x.yes), ("NO", x.no)):
            if r is not None and r.net_edge_top is not None:
                rows.append((r.net_edge_top / r.payout_per_unit, f"{name:3} n={len(x.event.markets):3} "
                             f"cost/payout={r.sum_best_ask / r.payout_per_unit:.4f} net_edge={r.net_edge_top:.4f} "
                             f"exec_profit=${r.exec_profit:.4f} | {x.event.title[:70]}"))
    rows.sort(key=lambda t: t[0], reverse=True)
    print("\nClosest baskets to an arbitrage right now (best first):", flush=True)
    for _, line in rows[:15]:
        print("   " + line, flush=True)
    opps = sum(1 for x in ok_evals for r in (x.yes, x.no) if r is not None and r.is_opportunity)
    print(f"\nExecutable opportunities in this sample right now: {opps}", flush=True)
    print(f"Completeness guard: YES baskets skipped when displayed prices sum < {COMPLETE_SET_MIN_SUM}: "
          f"{sum(1 for x in ok_evals if x.yes_skip)}", flush=True)
    return _finish(rep)


def _is_sorted(levels: Any, ascending: bool) -> bool:
    try:
        prices = [Decimal(str(l["price"])) for l in levels]
    except Exception:  # noqa: BLE001
        return False
    return prices == sorted(prices, reverse=not ascending)


def _finish(rep: Report) -> int:
    print(f"\nRESULT: {'FAIL' if rep.failures else 'PASS'} — {rep.failures} failure(s), {rep.warnings} warning(s)", flush=True)
    return 1 if rep.failures else 0
