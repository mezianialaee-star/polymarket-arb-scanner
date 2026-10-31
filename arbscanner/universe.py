"""Which Polymarket events can hold a *guaranteed* basket arbitrage?

Only "standard" negative-risk events: a fixed, complete list of mutually exclusive
outcomes where exactly one resolves YES (Polymarket docs: "Exactly one market in the
group resolves Yes, while every other market resolves No").

Excluded on purpose (safety first):
- non neg-risk events (outcomes are independent, nothing is guaranteed);
- *augmented* neg-risk events: new outcomes can be added later and "Other" changes
  meaning, so buying every listed outcome is NOT guaranteed to pay;
- events where a market is paused / closed without a clear NO resolution;
- markets whose YES/NO token ids cannot be identified with certainty.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .basket import D0, to_decimal

UNIVERSE_SCHEMA = 1

# Fee rate used when a market does not publish its fee configuration.
# 0.07 is the highest documented taker rate: being pessimistic can only hide
# opportunities, never invent one.
DEFAULT_FEE_RATE = Decimal("0.07")
DEFAULT_FEE_EXPONENT = Decimal(1)


class SchemaError(Exception):
    """The API answered with a shape we do not understand. Never guess: stop loudly."""


@dataclass
class UniverseMarket:
    market_id: str
    condition_id: str
    title: str
    yes_id: str
    no_id: str
    fee_rate: Decimal
    fee_exponent: Decimal
    fee_source: str            # "schedule" | "disabled" | "default"
    min_size: Decimal          # orderMinSize from Gamma (shares)
    yes_price: Optional[Decimal]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "title": self.title,
            "yes_id": self.yes_id,
            "no_id": self.no_id,
            "fee_rate": str(self.fee_rate),
            "fee_exponent": str(self.fee_exponent),
            "fee_source": self.fee_source,
            "min_size": str(self.min_size),
            "yes_price": None if self.yes_price is None else str(self.yes_price),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UniverseMarket":
        return cls(
            market_id=str(d["market_id"]),
            condition_id=str(d["condition_id"]),
            title=str(d["title"]),
            yes_id=str(d["yes_id"]),
            no_id=str(d["no_id"]),
            fee_rate=Decimal(d["fee_rate"]),
            fee_exponent=Decimal(d["fee_exponent"]),
            fee_source=str(d["fee_source"]),
            min_size=Decimal(d["min_size"]),
            yes_price=None if d.get("yes_price") is None else Decimal(d["yes_price"]),
        )


@dataclass
class UniverseEvent:
    event_id: str
    slug: str
    title: str
    category: str
    markets: List[UniverseMarket]
    eliminated: int = 0                    # outcomes already resolved NO
    yes_price_sum: Optional[Decimal] = None  # sum of displayed YES prices (completeness check)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "slug": self.slug,
            "title": self.title,
            "category": self.category,
            "eliminated": self.eliminated,
            "yes_price_sum": None if self.yes_price_sum is None else str(self.yes_price_sum),
            "markets": [m.to_dict() for m in self.markets],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UniverseEvent":
        return cls(
            event_id=str(d["event_id"]),
            slug=str(d["slug"]),
            title=str(d["title"]),
            category=str(d["category"]),
            markets=[UniverseMarket.from_dict(m) for m in d["markets"]],
            eliminated=int(d.get("eliminated", 0)),
            yes_price_sum=None if d.get("yes_price_sum") is None else Decimal(d["yes_price_sum"]),
        )


@dataclass
class Universe:
    generated_at: datetime
    events: List[UniverseEvent]
    stats: Dict[str, Any] = field(default_factory=dict)

    def token_ids(self) -> List[str]:
        ids: List[str] = []
        for ev in self.events:
            for m in ev.markets:
                ids.append(m.yes_id)
                ids.append(m.no_id)
        return ids

    def age_minutes(self, now: datetime) -> float:
        return (now - self.generated_at).total_seconds() / 60.0

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": UNIVERSE_SCHEMA,
                "generated_at": self.generated_at.isoformat(),
                "stats": self.stats,
                "events": [e.to_dict() for e in self.events],
            },
            indent=1,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "Universe":
        d = json.loads(text)
        if d.get("schema") != UNIVERSE_SCHEMA:
            raise ValueError("universe file has an old schema")
        generated = datetime.fromisoformat(d["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        return cls(
            generated_at=generated,
            events=[UniverseEvent.from_dict(e) for e in d["events"]],
            stats=d.get("stats", {}),
        )


# ---------------------------------------------------------------- parsing helpers

def _parse_list(value: Any) -> Optional[List[Any]]:
    """Gamma encodes some lists as JSON strings ("[\"Yes\", \"No\"]")."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return parsed
    return None


def asset_ids(market: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(YES id, NO id) following the docs: v2 -> positionIds, v1 -> clobTokenIds; index 0 = YES."""
    version = market.get("version")
    clob = _parse_list(market.get("clobTokenIds"))
    pos = _parse_list(market.get("positionIds"))
    if version == "v2":
        ids = pos
    elif version == "v1":
        ids = clob
    elif version in (None, ""):
        ids = clob if clob else pos
    else:
        return None  # docs: treat other versions as unsupported
    if not ids or len(ids) != 2:
        return None
    yes, no = str(ids[0]).strip(), str(ids[1]).strip()
    if not yes or not no or yes == no or yes in ("None", "null") or no in ("None", "null"):
        return None
    return yes, no


def outcomes_are_yes_no(market: Dict[str, Any]) -> bool:
    outcomes = _parse_list(market.get("outcomes"))
    if outcomes is None:
        return market.get("outcomes") is None  # absent: docs say index 0 is YES
    return len(outcomes) == 2 and str(outcomes[0]).strip().lower() == "yes" and str(outcomes[1]).strip().lower() == "no"


def yes_price(market: Dict[str, Any]) -> Optional[Decimal]:
    prices = _parse_list(market.get("outcomePrices"))
    if not prices:
        return None
    try:
        return to_decimal(prices[0])
    except ValueError:
        return None


def market_fee(market: Dict[str, Any]) -> Tuple[Decimal, Decimal, str]:
    enabled = market.get("feesEnabled")
    schedule = market.get("feeSchedule")
    if enabled is False:
        return D0, DEFAULT_FEE_EXPONENT, "disabled"
    if isinstance(schedule, dict) and schedule.get("rate") is not None:
        try:
            rate = to_decimal(schedule["rate"])
            exponent = to_decimal(schedule.get("exponent", 1))
        except ValueError:
            return DEFAULT_FEE_RATE, DEFAULT_FEE_EXPONENT, "default"
        if rate < 0 or exponent <= 0:
            return DEFAULT_FEE_RATE, DEFAULT_FEE_EXPONENT, "default"
        return rate, exponent, "schedule"
    return DEFAULT_FEE_RATE, DEFAULT_FEE_EXPONENT, "default"


def _resolved_no(market: Dict[str, Any]) -> bool:
    """True only if the displayed prices are exactly YES = 0 and NO = 1 (final NO)."""
    prices = _parse_list(market.get("outcomePrices"))
    if not prices or len(prices) != 2:
        return False
    try:
        return to_decimal(prices[0]) == 0 and to_decimal(prices[1]) == 1
    except ValueError:
        return False


def market_status(market: Dict[str, Any]) -> str:
    """'tradable', 'eliminated' (already resolved NO) or 'blocked'."""
    closed = market.get("closed") is True or market.get("archived") is True
    if closed:
        return "eliminated" if _resolved_no(market) else "blocked"
    if market.get("active") is False:
        return "blocked"
    if market.get("acceptingOrders") is False or market.get("enableOrderBook") is False:
        return "blocked"
    return "tradable"


def _category(event: Dict[str, Any]) -> str:
    cat = event.get("category")
    if isinstance(cat, str) and cat.strip():
        return cat.strip()
    tags = event.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and isinstance(tag.get("label"), str) and tag["label"].strip():
                return tag["label"].strip()
    return ""


def _short(text: Any, limit: int = 140) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def classify_event(event: Any) -> Tuple[Optional[UniverseEvent], str]:
    """Return (eligible event, 'ok') or (None, reason it is excluded)."""
    if not isinstance(event, dict):
        return None, "malformed_event"
    if event.get("negRisk") is not True:
        return None, "not_neg_risk"
    if event.get("closed") is True or event.get("archived") is True:
        return None, "event_closed"
    if event.get("negRiskAugmented") is True:
        return None, "augmented_neg_risk"
    markets = event.get("markets")
    if not isinstance(markets, list) or len(markets) < 2:
        return None, "too_few_markets"
    if any(isinstance(m, dict) and m.get("negRiskOther") is True for m in markets):
        return None, "augmented_neg_risk"
    groups = {str(m.get("negRiskMarketID")).lower() for m in markets
              if isinstance(m, dict) and m.get("negRiskMarketID")}
    if event.get("negRiskMarketID"):
        groups.add(str(event["negRiskMarketID"]).lower())
    if len(groups) > 1 or any(isinstance(m, dict) and m.get("negRisk") is False for m in markets):
        # Two separate "exactly one wins" groups in one event (or a market outside the
        # group) would break the payout math.
        return None, "mixed_neg_risk_groups"

    legs: List[UniverseMarket] = []
    eliminated = 0
    price_sum = D0
    price_known = True
    for m in markets:
        if not isinstance(m, dict):
            return None, "malformed_market"
        status = market_status(m)
        if status == "eliminated":
            eliminated += 1
            continue
        if status == "blocked":
            return None, "market_not_tradable"
        ids = asset_ids(m)
        if ids is None:
            return None, "missing_token_ids"
        if not outcomes_are_yes_no(m):
            return None, "unexpected_outcomes"
        rate, exponent, source = market_fee(m)
        try:
            min_size = to_decimal(m.get("orderMinSize")) if m.get("orderMinSize") is not None else D0
        except ValueError:
            min_size = D0
        price = yes_price(m)
        if price is None:
            price_known = False
        else:
            price_sum += price
        legs.append(
            UniverseMarket(
                market_id=str(m.get("id", "")),
                condition_id=str(m.get("conditionId") or ""),
                title=_short(m.get("groupItemTitle") or m.get("question") or m.get("slug") or ""),
                yes_id=ids[0],
                no_id=ids[1],
                fee_rate=rate,
                fee_exponent=exponent,
                fee_source=source,
                min_size=max(min_size, D0),
                yes_price=price,
            )
        )
    if len(legs) < 2:
        return None, "too_few_tradable"
    all_ids = [x for leg in legs for x in (leg.yes_id, leg.no_id)]
    if len(set(all_ids)) != len(all_ids):
        return None, "duplicate_token_ids"
    return (
        UniverseEvent(
            event_id=str(event.get("id", "")),
            slug=str(event.get("slug") or ""),
            title=_short(event.get("title") or event.get("slug") or ""),
            category=_category(event),
            markets=legs,
            eliminated=eliminated,
            yes_price_sum=price_sum if price_known else None,
        ),
        "ok",
    )


def build_universe(events: Iterable[Any], now: datetime, fetch_stats: Optional[Dict[str, Any]] = None) -> Universe:
    reasons: Counter = Counter()
    eligible: List[UniverseEvent] = []
    seen_events = set()
    seen_tokens = set()
    total = 0
    for raw in events:
        total += 1
        ev, reason = classify_event(raw)
        if ev is None:
            reasons[reason] += 1
            continue
        if ev.event_id in seen_events:
            reasons["duplicate_event"] += 1
            continue
        tokens = {x for m in ev.markets for x in (m.yes_id, m.no_id)}
        if tokens & seen_tokens:
            reasons["token_shared_with_other_event"] += 1
            continue
        seen_events.add(ev.event_id)
        seen_tokens |= tokens
        reasons["ok"] += 1
        eligible.append(ev)
    stats: Dict[str, Any] = {
        "events_fetched": total,
        "eligible_events": len(eligible),
        "eligible_markets": sum(len(e.markets) for e in eligible),
        "reasons": dict(sorted(reasons.items())),
        "fee_sources": dict(Counter(m.fee_source for e in eligible for m in e.markets)),
    }
    if fetch_stats:
        stats.update(fetch_stats)
    return Universe(generated_at=now, events=eligible, stats=stats)
