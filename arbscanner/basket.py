"""Core math: fees and the optimal size of a basket trade.

A "basket" = buying the same number of shares q in every leg (one leg per outcome).

YES basket: buy YES on every outcome. Exactly one outcome wins, so the basket pays
            exactly q dollars.            -> payout per unit = 1
NO basket:  buy NO on every outcome. Exactly one outcome wins, so N-1 NO legs pay
            out.                          -> payout per unit = N - 1

Buying walks up each order book (cheapest asks first), so the marginal cost of one
more unit of basket only goes up. Profit(q) is therefore concave: we keep buying
while one more unit costs less than it pays, and stop as soon as it doesn't.

Polymarket also refuses marketable BUY orders below a minimum size (in shares) and
below $1 of notional. A basket only counts as executable if every leg can respect both.

Everything uses Decimal: no floating point rounding can create a fake profit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence

D0 = Decimal(0)
D1 = Decimal(1)


def to_decimal(value) -> Decimal:
    """Convert API values (strings, ints, floats) to Decimal. Raises ValueError if impossible."""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool) or value is None:
        raise ValueError(f"not a number: {value!r}")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    else:
        raise ValueError(f"not a number: {value!r}")
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


def fee_per_share(price: Decimal, rate: Decimal, exponent: Decimal) -> Decimal:
    """Polymarket taker fee for ONE share bought at `price`.

    Documented formula: fee = C x feeRate x p x (1 - p), where C = number of shares.
    The market's feeSchedule also has an exponent applied to the price component,
    i.e. fee = C x rate x (p(1-p))^exponent (exponent = 1 gives the formula above).
    """
    if rate <= 0:
        return D0
    base = price * (D1 - price)
    if base <= 0:
        return D0
    if exponent == 1:
        return rate * base
    return rate * (base ** exponent)


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Leg:
    """One outcome's order book side we buy from (asks sorted from cheapest)."""

    asks: Sequence[Level]
    fee_rate: Decimal
    fee_exponent: Decimal
    min_size: Decimal          # minimum order size, in shares
    min_notional: Decimal = D1  # minimum value of a marketable BUY order, in dollars


@dataclass(frozen=True)
class BasketResult:
    payout_per_unit: Decimal
    n_legs: int
    complete: bool                       # every leg has at least one ask
    sum_best_ask: Optional[Decimal]      # sum of the cheapest ask of every leg
    fee_top: Optional[Decimal]           # fees for one unit of basket at the best asks
    gross_edge_top: Optional[Decimal]    # payout - sum_best_ask (before fees)
    net_edge_top: Optional[Decimal]      # payout - sum_best_ask - fee_top
    top_size_min: Optional[Decimal]      # smallest quantity available at the best asks
    max_size: Decimal                    # size that maximises profit (ignoring order minimums)
    max_profit: Decimal                  # profit at max_size
    min_size_required: Optional[Decimal]  # smallest size allowed by the order minimums (None: impossible)
    exec_size: Decimal                   # best size that respects the order minimums (0 if none)
    exec_profit: Decimal                 # profit at exec_size (> 0 only for a real opportunity)
    exec_cost: Decimal                   # total spent at exec_size, fees included

    @property
    def is_opportunity(self) -> bool:
        return self.exec_profit > 0 and self.exec_size > 0


def _unit_cost(price: Decimal, leg: Leg) -> Decimal:
    return price + fee_per_share(price, leg.fee_rate, leg.fee_exponent)


def cost_to_buy(legs: Sequence[Leg], quantity: Decimal) -> Optional[Decimal]:
    """Total cost (fees included) of buying `quantity` shares in every leg.

    Returns None if one leg does not have enough shares for sale.
    """
    total = D0
    for leg in legs:
        remaining = quantity
        for level in leg.asks:
            if remaining <= 0:
                break
            take = level.size if level.size < remaining else remaining
            total += take * _unit_cost(level.price, leg)
            remaining -= take
        if remaining > 0:
            return None
    return total


def min_quantity_for_leg(leg: Leg) -> Optional[Decimal]:
    """Smallest number of shares one order on this leg may have.

    Both venue rules must hold: size >= min_size, and size x price >= min_notional
    (the price paid walks up the book). Returns None if the book is too thin.
    """
    q_notional = D0
    if leg.min_notional > 0:
        spent = D0
        bought = D0
        found = False
        for level in leg.asks:
            level_value = level.price * level.size
            if spent + level_value >= leg.min_notional:
                q_notional = bought + (leg.min_notional - spent) / level.price
                found = True
                break
            spent += level_value
            bought += level.size
        if not found:
            return None
    return max(leg.min_size, q_notional)


def optimize_basket(legs: Sequence[Leg], payout_per_unit: Decimal) -> BasketResult:
    n = len(legs)
    if n == 0 or any(len(leg.asks) == 0 for leg in legs):
        return BasketResult(
            payout_per_unit, n, False, None, None, None, None, None,
            D0, D0, None, D0, D0, D0,
        )

    sum_best = sum((leg.asks[0].price for leg in legs), D0)
    fee_top = sum((fee_per_share(leg.asks[0].price, leg.fee_rate, leg.fee_exponent) for leg in legs), D0)
    top_size_min = min(leg.asks[0].size for leg in legs)

    leg_minimums = [min_quantity_for_leg(leg) for leg in legs]
    min_required: Optional[Decimal] = None
    if all(m is not None for m in leg_minimums):
        min_required = max(leg_minimums)  # type: ignore[type-var]

    # Walk all books together, one "step" at a time.
    index: List[int] = [0] * n
    remaining: List[Decimal] = [leg.asks[0].size for leg in legs]
    quantity = D0
    cost = D0
    while True:
        marginal = sum((_unit_cost(legs[i].asks[index[i]].price, legs[i]) for i in range(n)), D0)
        if marginal >= payout_per_unit:
            break  # one more unit of basket would not be profitable
        step = min(remaining)
        if step <= 0:
            break
        quantity += step
        cost += step * marginal
        exhausted = False
        for i in range(n):
            remaining[i] -= step
            if remaining[i] == 0:
                index[i] += 1
                if index[i] >= len(legs[i].asks):
                    exhausted = True
                else:
                    remaining[i] = legs[i].asks[index[i]].size
        if exhausted:
            break  # one book is empty: cannot buy more complete baskets
    max_profit = quantity * payout_per_unit - cost

    exec_size, exec_profit, exec_cost = D0, D0, D0
    if quantity > 0 and max_profit > 0 and min_required is not None:
        if quantity >= min_required:
            exec_size, exec_profit, exec_cost = quantity, max_profit, cost
        else:
            # Profit is concave in size, so above the optimum the best allowed size is
            # exactly the minimum. Check that it is still profitable.
            cost_min = cost_to_buy(legs, min_required)
            if cost_min is not None:
                profit_min = min_required * payout_per_unit - cost_min
                if profit_min > 0:
                    exec_size, exec_profit, exec_cost = min_required, profit_min, cost_min

    return BasketResult(
        payout_per_unit=payout_per_unit,
        n_legs=n,
        complete=True,
        sum_best_ask=sum_best,
        fee_top=fee_top,
        gross_edge_top=payout_per_unit - sum_best,
        net_edge_top=payout_per_unit - sum_best - fee_top,
        top_size_min=top_size_min,
        max_size=quantity,
        max_profit=max_profit,
        min_size_required=min_required,
        exec_size=exec_size,
        exec_profit=exec_profit,
        exec_cost=exec_cost,
    )
