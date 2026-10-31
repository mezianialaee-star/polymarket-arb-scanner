"""Turn raw order books + one eligible event into basket results, with safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .basket import D0, D1, BasketResult, Leg, Level, optimize_basket, to_decimal
from .universe import SchemaError, UniverseEvent

# A YES basket can only be trusted if the listed outcomes really cover everything.
# For a complete set, displayed YES prices add up to about 1. Far below that means an
# outcome is probably missing from the list: we refuse to call that an arbitrage.
COMPLETE_SET_MIN_SUM = Decimal("0.90")

# Edges bigger than this are flagged for manual review (could be stale/odd data).
SUSPICIOUS_EDGE = Decimal("0.05")


@dataclass
class ParsedBook:
    asset_id: str
    condition_id: str
    asks: List[Level]          # cheapest first
    bids: List[Level]          # highest first
    min_order_size: Decimal
    neg_risk: Optional[bool]
    timestamp: Optional[float]  # seconds since epoch
    dropped_levels: int


def _norm_hex(value: str) -> str:
    v = (value or "").strip().lower()
    if v.startswith("0x"):
        v = v[2:]
    return v


def parse_book(raw: Dict[str, Any]) -> ParsedBook:
    """Validate one CLOB book. Malformed numbers raise SchemaError (never guessed)."""
    dropped = 0

    def levels(key: str) -> List[Level]:
        nonlocal dropped
        side = raw.get(key)
        if not isinstance(side, list):
            raise SchemaError(f"book {raw.get('asset_id')}: '{key}' is not a list")
        out: List[Level] = []
        for lvl in side:
            if not isinstance(lvl, dict) or "price" not in lvl or "size" not in lvl:
                raise SchemaError(f"book {raw.get('asset_id')}: bad level {str(lvl)[:80]}")
            try:
                price = to_decimal(lvl["price"])
                size = to_decimal(lvl["size"])
            except ValueError as exc:
                raise SchemaError(f"book {raw.get('asset_id')}: {exc}") from exc
            if size <= 0 or price <= 0 or price >= 1:
                dropped += 1
                continue
            out.append(Level(price, size))
        return out

    asks = sorted(levels("asks"), key=lambda l: l.price)
    bids = sorted(levels("bids"), key=lambda l: l.price, reverse=True)
    try:
        min_size = to_decimal(raw.get("min_order_size")) if raw.get("min_order_size") not in (None, "") else D0
    except ValueError:
        min_size = D0
    ts: Optional[float] = None
    try:
        t = float(raw.get("timestamp"))
        ts = t / 1000.0 if t > 1e11 else t
    except (TypeError, ValueError):
        ts = None
    neg = raw.get("neg_risk")
    return ParsedBook(
        asset_id=str(raw.get("asset_id")),
        condition_id=str(raw.get("market") or ""),
        asks=asks,
        bids=bids,
        min_order_size=max(min_size, D0),
        neg_risk=neg if isinstance(neg, bool) else None,
        timestamp=ts,
        dropped_levels=dropped,
    )


@dataclass
class EventEvaluation:
    event: UniverseEvent
    status: str                               # "ok" or the reason it was skipped
    yes: Optional[BasketResult] = None
    no: Optional[BasketResult] = None
    yes_skip: str = ""                        # why the YES basket was not evaluated
    book_age_max_s: Optional[float] = None


def evaluate_event(
    event: UniverseEvent,
    books: Dict[str, ParsedBook],
    now_ts: float,
    min_notional: Decimal = D1,
) -> EventEvaluation:
    yes_legs: List[Leg] = []
    no_legs: List[Leg] = []
    ages: List[float] = []
    for m in event.markets:
        by = books.get(m.yes_id)
        bn = books.get(m.no_id)
        if by is None or bn is None:
            return EventEvaluation(event, "missing_book")
        for b in (by, bn):
            if b.neg_risk is False:
                return EventEvaluation(event, "book_not_neg_risk")
            if m.condition_id and b.condition_id and _norm_hex(m.condition_id) != _norm_hex(b.condition_id):
                return EventEvaluation(event, "condition_id_mismatch")
            if b.timestamp is not None:
                ages.append(max(0.0, now_ts - b.timestamp))
        yes_legs.append(Leg(by.asks, m.fee_rate, m.fee_exponent, max(m.min_size, by.min_order_size), min_notional))
        no_legs.append(Leg(bn.asks, m.fee_rate, m.fee_exponent, max(m.min_size, bn.min_order_size), min_notional))

    n = len(event.markets)
    evaluation = EventEvaluation(event, "ok", book_age_max_s=max(ages) if ages else None)
    if event.yes_price_sum is not None and event.yes_price_sum < COMPLETE_SET_MIN_SUM:
        evaluation.yes_skip = "incomplete_set_suspected"
    else:
        evaluation.yes = optimize_basket(yes_legs, D1)
    # The NO basket stays safe even if an outcome were missing: then all NO legs pay.
    evaluation.no = optimize_basket(no_legs, Decimal(n - 1))
    return evaluation


def is_suspicious(result: BasketResult) -> bool:
    if result.gross_edge_top is None:
        return False
    return result.gross_edge_top / result.payout_per_unit > SUSPICIOUS_EDGE


def parse_books(raw_books: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, ParsedBook], int]:
    """Parse every book. One odd book is left out (its event is then skipped);
    many odd books means the format changed: raise instead of guessing."""
    parsed: Dict[str, ParsedBook] = {}
    dropped = 0
    malformed = 0
    first_error = ""
    for asset, raw in raw_books.items():
        try:
            pb = parse_book(raw)
        except SchemaError as exc:
            malformed += 1
            first_error = first_error or str(exc)
            continue
        parsed[asset] = pb
        dropped += pb.dropped_levels
    if malformed > max(5, 0.05 * len(raw_books)):
        raise SchemaError(f"{malformed}/{len(raw_books)} order books in an unknown format, e.g. {first_error[:200]}")
    return parsed, dropped
