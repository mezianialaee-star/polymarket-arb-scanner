"""Tests for event eligibility (which events can hold a guaranteed arbitrage) and book checks."""

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D

from arbscanner.evaluate import evaluate_event, parse_book, parse_books
from arbscanner.universe import (
    DEFAULT_FEE_RATE, SchemaError, Universe, asset_ids, build_universe, classify_event, market_fee,
    market_status,
)
from tests.fakepoly import book, build_world, cond, event, market

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class AssetIdTests(unittest.TestCase):
    def test_v1_uses_clob_token_ids_json_string(self):
        m = market(1, "x", "111", "222", "0.5")
        self.assertEqual(asset_ids(m), ("111", "222"))

    def test_v2_uses_position_ids(self):
        m = market(1, "x", "333", "444", "0.5", version="v2")
        self.assertEqual(asset_ids(m), ("333", "444"))

    def test_v2_ignores_clob_ids_even_if_present(self):
        m = market(1, "x", "333", "444", "0.5", version="v2")
        m["clobTokenIds"] = json.dumps(["999", "998"])
        self.assertEqual(asset_ids(m), ("333", "444"))

    def test_missing_version_falls_back(self):
        m = market(1, "x", "111", "222", "0.5")
        del m["version"]
        self.assertEqual(asset_ids(m), ("111", "222"))

    def test_bad_cases(self):
        m = market(1, "x", "111", "222", "0.5")
        m["version"] = "v3"
        self.assertIsNone(asset_ids(m))
        m = market(1, "x", "111", "111", "0.5")
        self.assertIsNone(asset_ids(m))
        m = market(1, "x", "111", "222", "0.5")
        m["clobTokenIds"] = "not json"
        self.assertIsNone(asset_ids(m))
        m["clobTokenIds"] = json.dumps(["only-one"])
        self.assertIsNone(asset_ids(m))


class FeeAndStatusTests(unittest.TestCase):
    def test_fee_sources(self):
        self.assertEqual(market_fee(market(1, "x", "1", "2", "0.5")), (D("0.04"), D(1), "schedule"))
        self.assertEqual(market_fee(market(1, "x", "1", "2", "0.5", fees_enabled=False))[2], "disabled")
        self.assertEqual(market_fee(market(1, "x", "1", "2", "0.5", fees_enabled=False))[0], D(0))
        self.assertEqual(market_fee(market(1, "x", "1", "2", "0.5", fees=None, fees_enabled=None)),
                         (DEFAULT_FEE_RATE, D(1), "default"))
        self.assertEqual(market_fee(market(1, "x", "1", "2", "0.5", fees=("0.25", 2)))[:2], (D("0.25"), D(2)))
        bad = market(1, "x", "1", "2", "0.5")
        bad["feeSchedule"]["rate"] = "oops"
        self.assertEqual(market_fee(bad)[2], "default")

    def test_eliminated_needs_a_final_no_resolution(self):
        m = market(1, "x", "1", "2", "0", closed=True)
        m["outcomePrices"] = json.dumps(["0", "0.5"])        # closed, YES 0 but NO not 1
        self.assertEqual(market_status(m), "blocked")
        m["outcomePrices"] = json.dumps(["0.0", "1.0"])
        self.assertEqual(market_status(m), "eliminated")
        m["outcomePrices"] = None
        self.assertEqual(market_status(m), "blocked")

    def test_status(self):
        self.assertEqual(market_status(market(1, "x", "1", "2", "0.5")), "tradable")
        self.assertEqual(market_status(market(1, "x", "1", "2", "0", closed=True)), "eliminated")
        self.assertEqual(market_status(market(1, "x", "1", "2", "0.3", closed=True)), "blocked")
        self.assertEqual(market_status(market(1, "x", "1", "2", "1", closed=True)), "blocked")
        self.assertEqual(market_status(market(1, "x", "1", "2", "0.3", accepting=False)), "blocked")


class ClassifyTests(unittest.TestCase):
    def test_world(self):
        events, _ = build_world()
        results = {e["id"]: classify_event(e) for e in events}
        self.assertEqual(results["1001"][1], "ok")
        self.assertEqual(results["1002"][1], "ok")
        self.assertEqual(results["1003"][1], "ok")
        self.assertEqual(results["1004"][1], "augmented_neg_risk")
        self.assertEqual(results["1005"][1], "not_neg_risk")
        self.assertEqual(results["1006"][1], "ok")
        self.assertEqual(results["1006"][0].eliminated, 1)
        self.assertEqual(len(results["1006"][0].markets), 2)
        self.assertEqual(results["1007"][1], "ok")
        self.assertEqual(results["1008"][1], "ok")
        self.assertEqual(results["1009"][1], "market_not_tradable")
        e1 = results["1001"][0]
        self.assertEqual([m.yes_id for m in e1.markets], ["111", "121", "131"])
        self.assertEqual([m.no_id for m in e1.markets], ["112", "122", "132"])
        self.assertEqual(e1.yes_price_sum, D("0.915"))
        self.assertEqual(e1.end_date, "2026-12-31T12:00:00Z")

    def test_end_date_falls_back_to_the_markets(self):
        ev = event(5, "x", [market(1, "a", "1", "2", "0.5", extra={"endDate": "2026-11-30T00:00:00Z"}),
                            market(2, "b", "3", "4", "0.5", extra={"endDate": "2026-12-15T00:00:00Z"})])
        del ev["endDate"]
        self.assertEqual(classify_event(ev)[0].end_date, "2026-12-15T00:00:00Z")
        for m in ev["markets"]:
            del m["endDate"]
        self.assertEqual(classify_event(ev)[0].end_date, "")

    def test_list_saved_by_the_previous_version_still_loads(self):
        events, _ = build_world()
        u = build_universe(events, NOW)
        d = json.loads(u.to_json())
        for e in d["events"]:
            del e["end_date"]
        old = Universe.from_json(json.dumps(d))
        self.assertEqual([e.end_date for e in old.events], [""] * len(old.events))
        self.assertEqual(Universe.from_json(u.to_json()).events[0].end_date, "2026-12-31T12:00:00Z")

    def test_augmented_detected_by_other_market_only(self):
        ev = event(5, "x", [market(1, "a", "1", "2", "0.5"),
                            market(2, "Other", "3", "4", "0.5", extra={"negRiskOther": True})])
        self.assertEqual(classify_event(ev)[1], "augmented_neg_risk")

    def test_two_neg_risk_groups_in_one_event_are_refused(self):
        a = market(1, "a", "1", "2", "0.5", extra={"negRiskMarketID": "0xAAA"})
        b = market(2, "b", "3", "4", "0.5", extra={"negRiskMarketID": "0xBBB"})
        self.assertEqual(classify_event(event(5, "x", [a, b]))[1], "mixed_neg_risk_groups")
        b["negRiskMarketID"] = "0xaaa"  # same id, other case
        self.assertEqual(classify_event(event(5, "x", [a, b]))[1], "ok")
        ev = event(5, "x", [a, b])
        ev["negRiskMarketID"] = "0xCCC"
        self.assertEqual(classify_event(ev)[1], "mixed_neg_risk_groups")
        c = market(3, "c", "5", "6", "0.5", extra={"negRisk": False})
        self.assertEqual(classify_event(event(6, "y", [market(1, "a", "1", "2", "0.5"), c]))[1], "mixed_neg_risk_groups")

    def test_other_rejections(self):
        self.assertEqual(classify_event("nope")[1], "malformed_event")
        self.assertEqual(classify_event(event(5, "x", [market(1, "a", "1", "2", "0.5")]))[1], "too_few_markets")
        ev = event(5, "x", [market(1, "a", "1", "2", "0.5"), market(2, "b", "3", "4", "0.5", outcomes=("No", "Yes"))])
        self.assertEqual(classify_event(ev)[1], "unexpected_outcomes")
        ev = event(5, "x", [market(1, "a", "1", "2", "0.5"), market(2, "b", "1", "4", "0.5")])
        self.assertEqual(classify_event(ev)[1], "duplicate_token_ids")
        ev = event(5, "x", [market(1, "a", "1", "2", "0.5"), market(2, "b", "3", "4", "0", closed=True)])
        self.assertEqual(classify_event(ev)[1], "too_few_tradable")
        ev = event(5, "x", [market(1, "a", "1", "2", "0.5"), market(2, "b", "3", "4", "0.5")])
        ev["closed"] = True
        self.assertEqual(classify_event(ev)[1], "event_closed")

    def test_build_universe_stats_and_roundtrip(self):
        events, _ = build_world()
        u = build_universe(events + [events[0]], NOW)  # duplicate event ignored
        self.assertEqual(len(u.events), 6)
        self.assertEqual(u.stats["reasons"]["duplicate_event"], 1)
        self.assertEqual(u.stats["reasons"]["ok"], 6)
        u2 = Universe.from_json(u.to_json())
        self.assertEqual(u2.to_json(), u.to_json())
        self.assertEqual(u2.generated_at, NOW)
        self.assertEqual(len(u.token_ids()), 2 * u.stats["eligible_markets"])


class BookTests(unittest.TestCase):
    def test_parse_sorts_and_drops_invalid(self):
        raw = book("1", cond(1), [("0.40", "5"), ("0.45", "0"), ("0.47", "3")], [("0.60", "1"), ("1.00", "4"), ("0.55", "2")])
        b = parse_book(raw)
        self.assertEqual([l.price for l in b.asks], [D("0.55"), D("0.60")])
        self.assertEqual([l.price for l in b.bids], [D("0.47"), D("0.40")])
        self.assertEqual(b.dropped_levels, 2)
        self.assertEqual(b.min_order_size, D(5))
        self.assertTrue(b.neg_risk)
        self.assertAlmostEqual(b.timestamp, 1793440000.0)

    def test_parse_rejects_garbage(self):
        raw = book("1", cond(1), [], [("abc", "1")])
        with self.assertRaises(SchemaError):
            parse_book(raw)
        raw = book("1", cond(1), [], [])
        raw["asks"] = None
        with self.assertRaises(SchemaError):
            parse_book(raw)

    def test_one_odd_book_is_skipped_many_raise(self):
        good = {str(i): book(str(i), cond(i), [("0.4", "1")], [("0.5", "1")]) for i in range(200)}
        odd = dict(good)
        odd["0"] = book("0", cond(0), [], [("abc", "1")])
        parsed, _ = parse_books(odd)
        self.assertEqual(len(parsed), 199)
        self.assertNotIn("0", parsed)
        for i in range(20):
            odd[str(i)] = book(str(i), cond(i), [], [("abc", "1")])
        with self.assertRaises(SchemaError):
            parse_books(odd)

    def _world_eval(self):
        events, raw_books = build_world()
        u = build_universe(events, NOW)
        books, _ = parse_books({k: v for k, v in raw_books.items()})
        return {e.event_id: evaluate_event(e, books, NOW.timestamp()) for e in u.events}

    def test_evaluations_match_hand_computations(self):
        ev = self._world_eval()
        self.assertEqual(ev["1001"].status, "ok")
        self.assertTrue(ev["1001"].yes.is_opportunity)
        self.assertEqual(ev["1001"].yes.exec_size, D(40))
        self.assertEqual(ev["1001"].yes.exec_profit, D("0.964"))
        self.assertFalse(ev["1001"].no.is_opportunity)
        self.assertTrue(ev["1002"].no.is_opportunity)
        self.assertEqual(ev["1002"].no.payout_per_unit, D(3))
        self.assertEqual(ev["1002"].no.exec_profit, D("0.50"))
        self.assertFalse(ev["1002"].yes.is_opportunity)
        self.assertEqual(ev["1003"].yes.gross_edge_top, D("0.01"))
        self.assertFalse(ev["1003"].yes.is_opportunity)
        self.assertFalse(ev["1006"].yes.is_opportunity or ev["1006"].no.is_opportunity)
        self.assertEqual(ev["1007"].status, "missing_book")
        self.assertIsNone(ev["1008"].yes)
        self.assertEqual(ev["1008"].yes_skip, "incomplete_set_suspected")
        self.assertFalse(ev["1008"].no.is_opportunity)

    def test_safety_skips(self):
        events, raw_books = build_world()
        u = build_universe(events, NOW)
        e1 = [e for e in u.events if e.event_id == "1001"][0]
        books, _ = parse_books(raw_books)
        books["111"].neg_risk = False
        self.assertEqual(evaluate_event(e1, books, NOW.timestamp()).status, "book_not_neg_risk")
        books, _ = parse_books(raw_books)
        books["112"].condition_id = cond(999)
        self.assertEqual(evaluate_event(e1, books, NOW.timestamp()).status, "condition_id_mismatch")
        books, _ = parse_books(raw_books)
        books["112"].condition_id = cond(11).upper().replace("0X", "")  # same id, other formatting
        self.assertEqual(evaluate_event(e1, books, NOW.timestamp()).status, "ok")


if __name__ == "__main__":
    unittest.main()
