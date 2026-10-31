"""Tests for the core math (fees, basket optimisation). Pure stdlib: run with
`python -m unittest discover tests` on any Python >= 3.9."""

import random
import unittest
from decimal import ROUND_HALF_UP, Decimal as D

from arbscanner.basket import (
    D0, D1, Leg, Level, cost_to_buy, fee_per_share, min_quantity_for_leg, optimize_basket, to_decimal,
)


def leg(asks, rate="0", exp="1", min_size="0", min_notional="0"):
    return Leg([Level(D(p), D(s)) for p, s in asks], D(rate), D(exp), D(min_size), D(min_notional))


class FeeTests(unittest.TestCase):
    # Official fee tables from Polymarket's docs (taker fee for 100 shares, in USDC).
    PRICES = ["0.01", "0.05", "0.10", "0.15", "0.20", "0.25", "0.30", "0.35", "0.40", "0.45", "0.50"]
    TABLES = {
        "0.07": ["0.07", "0.33", "0.63", "0.89", "1.12", "1.31", "1.47", "1.59", "1.68", "1.73", "1.75"],
        "0.05": ["0.05", "0.24", "0.45", "0.64", "0.80", "0.94", "1.05", "1.14", "1.20", "1.24", "1.25"],
        "0.04": ["0.04", "0.19", "0.36", "0.51", "0.64", "0.75", "0.84", "0.91", "0.96", "0.99", "1.00"],
    }

    def test_matches_official_fee_tables_both_halves(self):
        for rate, expected in self.TABLES.items():
            for price, fee100 in zip(self.PRICES, expected):
                for p in (D(price), D1 - D(price)):  # the table is symmetric around 0.50
                    got = (fee_per_share(p, D(rate), D1) * 100).quantize(D("0.01"), rounding=ROUND_HALF_UP)
                    self.assertEqual(got, D(fee100), f"rate {rate} price {p}")

    def test_exponent_two_matches_changelog_peak(self):
        # Changelog: 15-minute crypto fees "peak at 1.56% at 50% probability".
        self.assertEqual(fee_per_share(D("0.5"), D("0.25"), D(2)), D("0.015625"))

    def test_zero_rate_and_extremes(self):
        self.assertEqual(fee_per_share(D("0.5"), D0, D1), D0)
        self.assertEqual(fee_per_share(D("1"), D("0.07"), D1), D0)
        self.assertEqual(fee_per_share(D("0"), D("0.07"), D1), D0)


class ToDecimalTests(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(to_decimal("0.45"), D("0.45"))
        self.assertEqual(to_decimal(5), D(5))
        self.assertEqual(to_decimal(0.1), D("0.1"))
        for bad in (None, True, "abc", "NaN", "inf", [], {}):
            with self.assertRaises(ValueError):
                to_decimal(bad)


class BasketTests(unittest.TestCase):
    def test_simple_yes_arbitrage(self):
        r = optimize_basket([leg([("0.40", "10")]), leg([("0.50", "10")])], D1)
        self.assertEqual(r.sum_best_ask, D("0.90"))
        self.assertEqual(r.max_size, D(10))
        self.assertEqual(r.max_profit, D("1.00"))
        self.assertTrue(r.is_opportunity)
        self.assertEqual(r.exec_profit, D("1.00"))
        self.assertEqual(r.exec_cost, D("9.00"))

    def test_walks_several_levels_and_stops_at_the_right_place(self):
        r = optimize_basket([
            leg([("0.40", "10"), ("0.55", "10")]),
            leg([("0.50", "5"), ("0.52", "20")]),
        ], D1)
        # 5 units at 0.90, then 5 units at 0.92, then 0.55+0.52 = 1.07 > 1: stop.
        self.assertEqual(r.max_size, D(10))
        self.assertEqual(r.exec_cost, D("9.10"))
        self.assertEqual(r.exec_profit, D("0.90"))

    def test_fees_are_included(self):
        r = optimize_basket([leg([("0.40", "10")], rate="0.05"), leg([("0.50", "10")], rate="0.05")], D1)
        # fees per unit: 0.05*0.4*0.6 + 0.05*0.5*0.5 = 0.012 + 0.0125
        self.assertEqual(r.fee_top, D("0.0245"))
        self.assertEqual(r.exec_profit, D("0.755"))

    def test_fees_can_kill_an_apparent_arbitrage(self):
        r = optimize_basket([leg([("0.49", "100")], rate="0.07"), leg([("0.50", "100")], rate="0.07")], D1)
        self.assertEqual(r.gross_edge_top, D("0.01"))
        self.assertLess(r.net_edge_top, D0)
        self.assertFalse(r.is_opportunity)
        self.assertEqual(r.exec_profit, D0)

    def test_no_basket_pays_n_minus_one(self):
        r = optimize_basket([leg([("0.60", "10")]), leg([("0.65", "10")]), leg([("0.70", "10")])], D(2))
        self.assertEqual(r.exec_profit, D("0.50"))

    def test_empty_book_means_no_basket(self):
        r = optimize_basket([leg([("0.40", "10")]), leg([])], D1)
        self.assertFalse(r.complete)
        self.assertFalse(r.is_opportunity)

    def test_exactly_one_dollar_is_not_an_opportunity(self):
        r = optimize_basket([leg([("0.40", "10")]), leg([("0.60", "10")])], D1)
        self.assertFalse(r.is_opportunity)

    def test_minimum_order_size_makes_small_arbitrage_unexecutable(self):
        legs = [leg([("0.40", "3"), ("0.70", "100")], min_size="5"), leg([("0.50", "100")], min_size="5")]
        r = optimize_basket(legs, D1)
        self.assertEqual(r.max_size, D(3))           # optimum ignoring the minimum
        self.assertEqual(r.min_size_required, D(5))
        self.assertFalse(r.is_opportunity)           # buying 5 loses money

    def test_minimum_order_size_still_profitable(self):
        legs = [leg([("0.40", "3"), ("0.56", "100")], min_size="5"), leg([("0.45", "100")], min_size="5")]
        r = optimize_basket(legs, D1)
        self.assertEqual(r.max_size, D(3))
        self.assertTrue(r.is_opportunity)
        self.assertEqual(r.exec_size, D(5))
        self.assertEqual(r.exec_cost, D("4.57"))
        self.assertEqual(r.exec_profit, D("0.43"))

    def test_one_dollar_minimum_on_cheap_legs(self):
        # At 0.02 a share, $1 needs 50 shares.
        self.assertEqual(min_quantity_for_leg(leg([("0.02", "1000")], min_size="5", min_notional="1")), D(50))
        self.assertEqual(min_quantity_for_leg(leg([("0.90", "1000")], min_size="5", min_notional="1")), D(5))
        # Crosses two levels: 10 x 0.05 = $0.50, then (1 - 0.5) / 0.10 = 5 more shares.
        self.assertEqual(min_quantity_for_leg(leg([("0.05", "10"), ("0.10", "100")], min_notional="1")), D(15))
        # Not enough value in the whole book.
        self.assertIsNone(min_quantity_for_leg(leg([("0.02", "10")], min_notional="1")))
        r = optimize_basket([leg([("0.02", "10")], min_notional="1"), leg([("0.50", "10")], min_notional="1")], D1)
        self.assertGreater(r.max_profit, D0)
        self.assertIsNone(r.min_size_required)
        self.assertFalse(r.is_opportunity)

    def test_cost_to_buy(self):
        legs = [leg([("0.40", "3"), ("0.56", "100")]), leg([("0.45", "100")])]
        self.assertEqual(cost_to_buy(legs, D(5)), D("4.57"))
        self.assertIsNone(cost_to_buy(legs, D(200)))


class BruteForceComparison(unittest.TestCase):
    """Compare the fast algorithm with an exhaustive search on thousands of random books."""

    def _random_leg(self, rng):
        levels = []
        price = D(rng.randint(1, 60)) / 100
        for _ in range(rng.randint(1, 5)):
            levels.append(Level(price, D(rng.randint(1, 40)) / D(rng.choice([1, 2, 4]))))
            price += D(rng.randint(1, 10)) / 100
            if price >= 1:
                break
        return Leg(
            levels,
            D(rng.choice(["0", "0.04", "0.05", "0.07", "0.25"])),
            D(rng.choice([1, 1, 2])),
            D(rng.choice([0, 1, 5])),
            D(rng.choice([0, 1])),
        )

    def test_random_books(self):
        rng = random.Random(20261031)
        checked_positive = 0
        for _ in range(4000):
            legs = [self._random_leg(rng) for _ in range(rng.randint(2, 6))]
            payout = D1 if rng.random() < 0.6 else D(len(legs) - 1)
            r = optimize_basket(legs, payout)
            total = min(sum(l.size for l in g.asks) for g in legs)
            # Profit is piecewise linear; its kinks are the cumulative level sizes.
            kinks = {D0, total}
            for g in legs:
                acc = D0
                for l in g.asks:
                    acc += l.size
                    if acc <= total:
                        kinks.add(acc)

            def profit(q):
                c = cost_to_buy(legs, q)
                return q * payout - c

            best_q, best_p = D0, D0
            for q in sorted(kinks):
                p = profit(q)
                if p > best_p:
                    best_q, best_p = q, p
            self.assertEqual(r.max_profit, best_p, "unconstrained optimum differs")
            if best_p > 0:
                self.assertEqual(r.max_size, best_q)

            # With order minimums: optimum over q >= m is at m or at a kink above m.
            m = r.min_size_required
            if m is None:
                self.assertFalse(r.is_opportunity)
                continue
            candidates = [q for q in kinks if q >= m] + ([m] if m <= total else [])
            exec_best = max([profit(q) for q in candidates if q > 0] + [D0])
            if exec_best > 0:
                checked_positive += 1
                self.assertTrue(r.is_opportunity)
                self.assertEqual(r.exec_profit, exec_best)
                self.assertGreaterEqual(r.exec_size, m)
                self.assertEqual(profit(r.exec_size), r.exec_profit)
            else:
                self.assertFalse(r.is_opportunity)
        self.assertGreater(checked_positive, 200)  # the test really exercised profitable cases


if __name__ == "__main__":
    unittest.main()
