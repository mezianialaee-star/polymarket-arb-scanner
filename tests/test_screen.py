"""Stage-1 screening: side detection, price table, candidates, and proof that the
screen never hides a basket that the full order books show as profitable."""

import random
import unittest
from decimal import Decimal as D

from arbscanner.basket import Level
from arbscanner.evaluate import ParsedBook, evaluate_event
from arbscanner.screen import Top, PriceTable, SideMapping, build_price_table, infer_side_mapping, screen_event
from arbscanner.universe import SchemaError, UniverseEvent, UniverseMarket


def market(i, fee="0", exponent="1", min_size="0", yes_price=None):
    return UniverseMarket(
        market_id=str(i), condition_id=f"0x{i:064x}", title=f"o{i}", yes_id=f"Y{i}", no_id=f"N{i}",
        fee_rate=D(fee), fee_exponent=D(exponent), fee_source="schedule", min_size=D(min_size),
        yes_price=None if yes_price is None else D(yes_price),
    )


def event(markets, price_sum="1"):
    return UniverseEvent("E", "e", "E", "cat", markets, yes_price_sum=None if price_sum is None else D(price_sum))


def table(tops, uncertain=()):
    return PriceTable(SideMapping(True, 10, 10), dict(tops), set(uncertain), {})


class SideMappingTests(unittest.TestCase):
    def test_buy_higher_means_buy_is_the_ask(self):
        raw = {f"t{i}": {"BUY": "0.52", "SELL": "0.50"} for i in range(10)}
        self.assertTrue(infer_side_mapping(raw).buy_is_ask)

    def test_sell_higher_means_buy_is_the_bid(self):
        raw = {f"t{i}": {"BUY": 0.5, "SELL": 0.52} for i in range(10)}
        m = infer_side_mapping(raw)
        self.assertFalse(m.buy_is_ask)
        self.assertEqual((m.agree, m.votes), (10, 10))

    def test_one_sided_equal_and_invalid_prices_do_not_vote(self):
        raw = {f"t{i}": {"BUY": "0.6", "SELL": "0.4"} for i in range(6)}
        raw.update({"a": {"BUY": "0.5"}, "b": {"BUY": "0.5", "SELL": "0.5"}, "c": {"BUY": "1", "SELL": "0.2"},
                    "d": "garbage", "e": {"BUY": "0", "SELL": "0.3"}})
        m = infer_side_mapping(raw)
        self.assertEqual((m.buy_is_ask, m.agree, m.votes), (True, 6, 6))

    def test_inconsistent_or_too_little_data_refuses_to_guess(self):
        mixed = {f"t{i}": ({"BUY": "0.6", "SELL": "0.4"} if i % 2 else {"BUY": "0.4", "SELL": "0.6"}) for i in range(100)}
        with self.assertRaises(SchemaError):
            infer_side_mapping(mixed)
        with self.assertRaises(SchemaError):
            infer_side_mapping({"t": {"BUY": "0.6", "SELL": "0.4"}})
        # a few disagreeing tokens are tolerated (they go to stage 2), many are not
        almost = {f"t{i}": {"BUY": "0.6", "SELL": "0.4"} for i in range(96)}
        almost.update({f"odd{i}": {"BUY": "0.4", "SELL": "0.6"} for i in range(4)})
        self.assertTrue(infer_side_mapping(almost).buy_is_ask)
        almost.update({f"odd{i}": {"BUY": "0.4", "SELL": "0.6"} for i in range(4, 10)})
        with self.assertRaises(SchemaError):
            infer_side_mapping(almost)


class PriceTableTests(unittest.TestCase):
    def raw(self, n=40):
        return {f"t{i}": {"BUY": "0.30", "SELL": "0.28"} for i in range(n)}

    def test_clean_table(self):
        raw = self.raw()
        raw["t0"] = {"BUY": "0.30"}                  # nobody buys
        raw["t1"] = {"BUY": "0", "SELL": "0.28"}     # 0 means nobody sells
        t = build_price_table(raw, list(raw))
        self.assertEqual(t.tops["t2"], Top(D("0.30"), D("0.28")))
        self.assertEqual(t.tops["t0"], Top(D("0.30"), None))
        self.assertEqual(t.tops["t1"], Top(None, D("0.28")))
        self.assertEqual(t.uncertain, set())
        self.assertEqual((t.stats["no_ask"], t.stats["no_bid"]), (1, 1))

    def test_missing_malformed_and_crossed_tokens_go_to_stage_two(self):
        raw = self.raw()
        raw["t3"] = {"BUY": "abc", "SELL": "0.2"}
        raw["t4"] = {"BUY": "0.30", "SELL": "0.31"}  # crossed for the detected mapping
        raw["t5"] = []
        t = build_price_table(raw, list(raw) + ["missing"], rejected=1)
        self.assertEqual(t.uncertain, {"t3", "t4", "t5", "missing"})
        self.assertEqual((t.stats["malformed"], t.stats["crossed"], t.stats["missing"]), (2, 1, 1))

    def test_too_many_odd_answers_is_an_error(self):
        raw = {f"t{i}": {"BUY": "0.30", "SELL": "0.28"} for i in range(1000)}
        raw.update({f"x{i}": {"BUY": "oops"} for i in range(30)})
        with self.assertRaises(SchemaError):
            build_price_table(raw, list(raw))
        for i in range(10):
            del raw[f"x{i}"]
        self.assertEqual(len(build_price_table(raw, list(raw)).uncertain), 20)   # within tolerance

    def test_closed_markets_left_out_of_the_answer_are_tolerated(self):
        raw = {f"t{i}": {"BUY": "0.30", "SELL": "0.28"} for i in range(1000)}
        closed = [f"closed{i}" for i in range(150)]      # 13% of the list closed since the rebuild
        t = build_price_table(raw, list(raw) + closed)
        self.assertEqual(t.stats["missing"], 150)
        self.assertEqual(t.uncertain, set(closed))
        with self.assertRaises(SchemaError):             # but not a third of everything
            build_price_table(raw, list(raw) + [f"c{i}" for i in range(400)])


class ScreenEventTests(unittest.TestCase):
    def test_yes_candidate_needs_the_sum_of_asks_below_one_plus_margin(self):
        ev = event([market(1), market(2)])
        tops = {"Y1": Top(D("0.50"), D("0.40")), "Y2": Top(D("0.51"), D("0.40"))}
        s = screen_event(ev, table(tops), margin=D("0.02"))
        self.assertEqual(s.yes_cost, D("1.01"))
        self.assertTrue(s.yes_candidate)
        self.assertFalse(screen_event(ev, table(tops), margin=D("0")).yes_candidate)
        self.assertEqual(s.no_cost, D("1.20"))
        self.assertFalse(s.no_candidate)

    def test_no_candidate_uses_one_minus_the_yes_bid(self):
        ev = event([market(1), market(2), market(3)])
        tops = {"Y1": Top(D("0.6"), D("0.45")), "Y2": Top(D("0.4"), D("0.35")), "Y3": Top(D("0.3"), D("0.21"))}
        s = screen_event(ev, table(tops), margin=D("0"))
        self.assertEqual(s.no_cost, D("1.99"))      # 0.55 + 0.65 + 0.79 < 2 = payout
        self.assertTrue(s.no_candidate)

    def test_fees_are_reported_at_the_top(self):
        ev = event([market(1, fee="0.07"), market(2, fee="0.07")])
        tops = {"Y1": Top(D("0.5"), D("0.4")), "Y2": Top(D("0.5"), D("0.4"))}
        s = screen_event(ev, table(tops))
        self.assertEqual(s.yes_fee, D("0.035"))     # 2 x 0.07 x 0.5 x 0.5
        self.assertEqual(s.no_fee, D("0.07") * D("0.6") * D("0.4") * 2)

    def test_incomplete_guarded_and_uncertain(self):
        ev = event([market(1), market(2)])
        s = screen_event(ev, table({"Y1": Top(None, D("0.4")), "Y2": Top(D("0.3"), None)}))
        self.assertEqual((s.yes_cost, s.no_cost, s.candidate), (None, None, False))
        guarded = event([market(1), market(2)], price_sum="0.5")
        s = screen_event(guarded, table({"Y1": Top(D("0.2"), D("0.1")), "Y2": Top(D("0.2"), D("0.1"))}))
        self.assertTrue(s.yes_guarded)
        self.assertFalse(s.yes_candidate)
        s = screen_event(ev, table({"Y1": Top(D("0.9"), D("0.1"))}, uncertain={"Y2"}))
        self.assertTrue(s.uncertain and s.candidate)


def _grid(x):
    return min(max(D(round(x, 3)).quantize(D("0.001")), D("0.001")), D("0.999"))


class ScreeningNeverHidesAnOpportunity(unittest.TestCase):
    """Random markets whose NO book mirrors the YES book (as on Polymarket). Whatever
    the full books say is profitable must be a stage-1 candidate, even with margin 0."""

    def test_random_markets(self):
        rng = random.Random(20261031)
        found = {"YES": 0, "NO": 0}
        for case in range(3000):
            n = rng.randint(2, 7)
            weights = [rng.random() + 0.05 for _ in range(n)]
            probs = [w / sum(weights) for w in weights]
            shift = rng.uniform(-0.12, 0.2)          # how far the asks add up from $1
            markets, books, tops = [], {}, {}
            for i, p in enumerate(probs):
                fee, exponent = rng.choice([("0", "1"), ("0.02", "1"), ("0.07", "1"), ("0.25", "2")])
                m = market(i, fee=fee, exponent=exponent, min_size=rng.choice(["0", "5"]))
                markets.append(m)
                ask = _grid(p + shift / n + rng.uniform(-0.01, 0.01))
                bid = _grid(float(ask) - rng.uniform(0.001, 0.03))
                if bid >= ask:
                    bid = None
                asks = [Level(ask, D(rng.randint(1, 60)))] + [
                    Level(_grid(float(ask) + 0.01 * k), D(rng.randint(1, 60))) for k in range(1, rng.randint(1, 4))]
                bids = [] if bid is None else [Level(bid, D(rng.randint(1, 60)))] + [
                    Level(_grid(float(bid) - 0.01 * k), D(rng.randint(1, 60))) for k in range(1, rng.randint(1, 4))]
                asks = sorted({l.price: l for l in asks if l.price < 1}.values(), key=lambda l: l.price)
                bids = sorted({l.price: l for l in bids if l.price > 0}.values(), key=lambda l: l.price, reverse=True)
                if rng.random() < 0.05:
                    asks = []        # nobody sells this outcome
                books[m.yes_id] = ParsedBook(m.yes_id, m.condition_id, asks, bids, D(0), True, None, 0)
                # NO book = mirror of the YES book
                books[m.no_id] = ParsedBook(m.no_id, m.condition_id,
                                            [Level(1 - l.price, l.size) for l in bids],
                                            [Level(1 - l.price, l.size) for l in asks], D(0), True, None, 0)
                tops[m.yes_id] = Top(asks[0].price if asks else None, bids[0].price if bids else None)
            ev = event(markets, price_sum=str(round(sum(probs), 6)))
            full = evaluate_event(ev, books, 0.0)
            screen = screen_event(ev, table(tops), margin=D("0"))
            if full.yes is not None and full.yes.is_opportunity:
                found["YES"] += 1
                self.assertTrue(screen.yes_candidate, f"case {case}: YES opportunity screened out")
            if full.no is not None and full.no.is_opportunity:
                found["NO"] += 1
                self.assertTrue(screen.no_candidate, f"case {case}: NO opportunity screened out")
            # and the stage-1 cost equals the full-book top-of-book cost
            if full.yes is not None and full.yes.sum_best_ask is not None:
                self.assertEqual(screen.yes_cost, full.yes.sum_best_ask)
            if full.no is not None and full.no.sum_best_ask is not None:
                self.assertEqual(screen.no_cost, full.no.sum_best_ask)
        # the random markets really contain opportunities of both kinds
        self.assertGreater(found["YES"], 300, found)
        self.assertGreater(found["NO"], 300, found)


if __name__ == "__main__":
    unittest.main()
