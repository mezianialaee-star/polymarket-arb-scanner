"""Stage 1: screen every event using only the best prices (cheap), to find candidates.

Reading every full order book of every outcome every 10 minutes would mean
~130,000 books per scan. Instead:

1. One POST /prices call per 250 YES tokens gives each token's best bid and best ask.
2. A basket can only be profitable if the sum of its best asks is below its payout:
   fees and deeper price levels only add cost. So every other event is safely ruled
   out, and only the "candidates" get their full order books read (stage 2).

Stage 1 never decides that something IS an arbitrage; stage 2 (full books, fees,
order minimums, all safety checks) does. Anything stage 1 is unsure about is sent to
stage 2, so screening can never hide an opportunity because of bad data.

Two facts are used, and both are checked live by the self-check:
- Polymarket's docs disagree on which /prices side is the ask ("BUY returns the
  lowest ask" in the guide, "best bid price for BUY side" in the API reference).
  We do not guess: on a live order book the best bid is always strictly below the
  best ask, so the higher of the two prices is the ask. This is checked on every
  scan over thousands of tokens; if the data does not agree with itself, we stop.
- NO and YES share one order book: NO best ask = 1 - YES best bid.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .basket import D0, D1, fee_per_share, to_decimal
from .evaluate import COMPLETE_SET_MIN_SUM
from .universe import SchemaError, UniverseEvent

# Candidates: baskets whose best asks add up to less than payout + margin ($ per basket).
DEFAULT_MARGIN = Decimal("0.02")

# Side-mapping vote: share of two-sided tokens that must agree. (The two possible
# meanings give ~100% vs ~0%; tokens that disagree are sent to stage 2.)
SIDE_AGREEMENT = 0.95
MIN_SIDE_VOTES = 5

# Markets close all the time (a match ends, a daily market resolves): their tokens drop
# out of the price answers until the hourly rebuild. A large share missing means
# something else is wrong.
MAX_MISSING_SHARE = 0.2


@dataclass(frozen=True)
class SideMapping:
    buy_is_ask: bool
    agree: int      # tokens consistent with the mapping
    votes: int      # tokens with two different, valid prices

    @property
    def label(self) -> str:
        return "BUY=ask" if self.buy_is_ask else "BUY=bid"


@dataclass(frozen=True)
class Top:
    ask: Optional[Decimal]   # None: no ask (nobody sells)
    bid: Optional[Decimal]   # None: no bid (nobody buys)


def _valid_price(value: Any) -> Tuple[Optional[Decimal], bool]:
    """(price, malformed). Prices outside (0, 1) mean 'no order on that side'."""
    if value is None:
        return None, False
    try:
        p = to_decimal(value)
    except ValueError:
        return None, True
    if p <= 0 or p >= 1:
        return None, False
    return p, False


def infer_side_mapping(raw: Dict[str, Any]) -> SideMapping:
    buy_higher = sell_higher = 0
    for sides in raw.values():
        if not isinstance(sides, dict):
            continue
        buy, _ = _valid_price(sides.get("BUY"))
        sell, _ = _valid_price(sides.get("SELL"))
        if buy is None or sell is None or buy == sell:
            continue
        if buy > sell:
            buy_higher += 1
        else:
            sell_higher += 1
    votes = buy_higher + sell_higher
    if votes < MIN_SIDE_VOTES:
        raise SchemaError(f"POST /prices: only {votes} tokens with a bid and an ask — cannot tell which side is the ask")
    if buy_higher >= SIDE_AGREEMENT * votes:
        return SideMapping(True, buy_higher, votes)
    if sell_higher >= SIDE_AGREEMENT * votes:
        return SideMapping(False, sell_higher, votes)
    raise SchemaError(
        f"POST /prices: the BUY price is the higher one for {buy_higher}/{votes} tokens — "
        "the two sides do not behave like bid/ask, refusing to guess"
    )


@dataclass
class PriceTable:
    mapping: SideMapping
    tops: Dict[str, Top]          # cleanly priced tokens
    uncertain: set                # tokens stage 1 cannot judge (sent to stage 2)
    stats: Dict[str, int]


def build_price_table(raw: Dict[str, Any], requested: List[str], rejected: int = 0) -> PriceTable:
    """Interpret POST /prices answers. Raises SchemaError if the answers look wrong overall."""
    mapping = infer_side_mapping(raw)
    tops: Dict[str, Top] = {}
    uncertain = set()
    malformed = crossed = missing = 0
    for token in requested:
        sides = raw.get(token)
        if not isinstance(sides, dict):
            if token in raw:
                malformed += 1
            else:
                missing += 1   # refused by the API or left out of the answer
            uncertain.add(token)
            continue
        buy, bad_buy = _valid_price(sides.get("BUY"))
        sell, bad_sell = _valid_price(sides.get("SELL"))
        if bad_buy or bad_sell:
            malformed += 1
            uncertain.add(token)
            continue
        ask, bid = (buy, sell) if mapping.buy_is_ask else (sell, buy)
        if ask is not None and bid is not None and bid >= ask:
            crossed += 1          # contradicts the side mapping: let stage 2 look at the book
            uncertain.add(token)
            continue
        tops[token] = Top(ask, bid)
    n = max(len(requested), 1)
    stats = {
        "tokens": len(requested), "priced": len(tops), "missing": missing, "rejected": rejected,
        "malformed": malformed, "crossed": crossed,
        "no_ask": sum(1 for t in tops.values() if t.ask is None),
        "no_bid": sum(1 for t in tops.values() if t.bid is None),
    }
    # Tokens left out of the answer are normal: the API omits markets that closed since
    # the list was built (checked live). Odd answers are not: a few are tolerated, many
    # mean the API changed. Either way, too many and we stop instead of guessing.
    odd = malformed + crossed
    if odd > max(25, 0.02 * n) or missing > max(25, MAX_MISSING_SHARE * n):
        raise SchemaError(
            f"POST /prices: {len(uncertain)}/{len(requested)} tokens without usable prices "
            f"(missing {missing}, malformed {malformed}, crossed {crossed}) — refusing to screen"
        )
    return PriceTable(mapping, tops, uncertain, stats)


@dataclass
class EventScreen:
    event: UniverseEvent
    yes_cost: Optional[Decimal] = None   # sum of YES best asks (None: a leg has no ask)
    yes_fee: Optional[Decimal] = None    # taker fees for one basket at those prices
    no_cost: Optional[Decimal] = None    # sum of NO best asks = sum(1 - YES best bid)
    no_fee: Optional[Decimal] = None
    yes_guarded: bool = False            # outcome list looks incomplete: no YES basket
    uncertain: bool = False              # some token could not be judged
    yes_candidate: bool = False
    no_candidate: bool = False

    @property
    def candidate(self) -> bool:
        return self.yes_candidate or self.no_candidate or self.uncertain


def screen_event(event: UniverseEvent, table: PriceTable, margin: Decimal = DEFAULT_MARGIN) -> EventScreen:
    s = EventScreen(event)
    s.yes_guarded = event.yes_price_sum is not None and event.yes_price_sum < COMPLETE_SET_MIN_SUM
    yes_cost = yes_fee = no_cost = no_fee = D0
    yes_ok = no_ok = True
    for m in event.markets:
        if m.yes_id in table.uncertain:
            s.uncertain = True
            continue
        top = table.tops.get(m.yes_id)
        if top is None:      # not requested: should not happen, be safe
            s.uncertain = True
            continue
        if top.ask is None:
            yes_ok = False
        else:
            yes_cost += top.ask
            yes_fee += fee_per_share(top.ask, m.fee_rate, m.fee_exponent)
        if top.bid is None:
            no_ok = False
        else:
            no_ask = D1 - top.bid
            no_cost += no_ask
            no_fee += fee_per_share(no_ask, m.fee_rate, m.fee_exponent)
    if s.uncertain:
        return s             # stage 2 will read every book of this event
    n = len(event.markets)
    if yes_ok:
        s.yes_cost, s.yes_fee = yes_cost, yes_fee
        s.yes_candidate = (not s.yes_guarded) and yes_cost < D1 + margin
    if no_ok:
        s.no_cost, s.no_fee = no_cost, no_fee
        s.no_candidate = no_cost < Decimal(n - 1) + margin
    return s
