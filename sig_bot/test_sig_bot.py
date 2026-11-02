"""Offline tests for sig_bot.py (no network): title parsing, grouping, arbitrage maths,
order payloads, and one full pass of the bot against a fake API."""
import unittest

import sig_bot


MARKETS = [
    ("1", "Will the Republican Party win the New Hampshire Governor?", "11"),
    ("2", "Will the Democratic Party win the New Hampshire Governor?", "12"),
    ("3", "Will the Republican Party win the Texas Senate?", "13"),
    ("4", "Will the Democratic Party win the Texas Senate?", "14"),
    ("5", "Will the Republican Party win the NH-01 House race?", "15"),
    ("6", "Will the Democratic Party win the NH-01 House race?", "16"),
    ("7", "Will the Republican Party win 52+ Senate seats?", "17"),
    ("8", "Will the Democratic Party win 52+ Senate seats?", "18"),
    ("9", "Will an Independent win the Nebraska Senate?", "19"),
    ("10", "Will the Republican Party win the Nebraska Senate?", "20"),
    ("11", "Bitcoin above 100k on election day?", "21"),
]

QUOTES = {
    "11": (0.885, 0.895), "12": (0.14, 0.145),   # S = 1.025 -> riskless edge 0.025
    "13": (0.285, 0.295), "14": (0.72, 0.73),    # S = 1.005 -> below MIN_EDGE
    "15": (0.125, 0.13), "16": (0.865, 0.875),   # S = 0.99
    "17": (0.6, 0.62), "18": (0.6, 0.62),        # would look like an arbitrage, must be ignored
    "19": (0.27, 0.28), "20": (0.70, 0.71),      # S = 0.97
    "21": (0.5, 0.52),
}

BOOKS = {
    "1": {"exchangeId": "11", "bids": [{"price": 0.88, "quantity": 1000}, {"price": 0.885, "quantity": 400}], "asks": []},
    "2": {"exchangeId": "12", "bids": [{"price": 0.14, "quantity": 1000}], "asks": []},
}


class FakeApi:
    def __init__(self, balance=5000):
        self.balance = balance
        self.calls = {False: 0, True: 0}
        self.posted = []

    def call(self, method, path, params=None, body=None, max_attempts=5):
        self.calls[method != "GET"] += 1
        if path == "/account":
            return {"id": "p1", "username": "secret-name", "email": "secret@example.com", "createdAt": "2026-10-01T00:00:00Z"}
        if path == "/tournaments":
            return {"data": [
                {"id": "t-ongoing", "slug": "ongoing-play", "name": "Ongoing Play", "myBalance": 1000},
                {"id": "t-cup", "slug": "midterm-elections", "name": "Midterm Elections", "myBalance": self.balance},
            ]}
        if path == "/tournaments/midterm-elections":
            return {"id": "t-cup", "slug": "midterm-elections", "myBalance": self.balance}
        if path == "/tournaments/midterm-elections/markets":
            return {"data": [
                {"id": mid, "title": title, "status": "open", "isComposite": False, "isMultiOutcome": False,
                 "exchanges": [{"id": ex, "option": "YES"}]}
                for mid, title, ex in MARKETS
            ], "pagination": {"hasMore": False, "nextCursor": None}}
        if path == "/exchanges/prices":
            assert params["tournamentId"] == "t-cup"
            ids = params["ids"].split(",")
            return {"data": [{"exchangeId": ex, "bestBid": QUOTES[ex][0], "bestAsk": QUOTES[ex][1]} for ex in ids],
                    "missingIds": []}
        if path.startswith("/markets/") and path.endswith("/orderbook"):
            market_id = path.split("/")[2]
            return {"exchanges": [BOOKS[market_id]], "contexts": []}
        if path == "/orders/multi-leg":
            self.posted.append(body)
            return {"results": [{"orderId": "1", "quantityTraded": leg["quantity"]} for leg in body["legs"]]}
        raise AssertionError(f"unexpected call {method} {path}")


class ParsingTests(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(sig_bot.parse_title("Will the Republican Party win the Texas Senate?"),
                         ("republican", "texas senate"))
        self.assertEqual(sig_bot.parse_title("Will the Democratic Party win the NH-01 House race?"),
                         ("democratic", "nh-01 house race"))
        self.assertEqual(sig_bot.parse_title("Will an Independent win the Nebraska Senate?"),
                         ("independent", "nebraska senate"))
        self.assertIsNone(sig_bot.parse_title("Bitcoin above 100k on election day?"))

    def test_single_seat_filter(self):
        for race in ["texas senate", "new hampshire governor", "nh-01 house race",
                     "ohio senate special election", "senate", "house"]:
            self.assertTrue(sig_bot.is_single_seat_race(race), race)
        for race in ["52+ senate seats", "a majority of governorships", "50 senate seats or more"]:
            self.assertFalse(sig_bot.is_single_seat_race(race), race)

    def test_groups(self):
        markets = [{"id": m, "title": t, "status": "open", "exchanges": [{"id": e}]} for m, t, e in MARKETS]
        groups, unparsed, skipped = sig_bot.build_groups(markets)
        self.assertEqual(set(groups), {"new hampshire governor", "texas senate", "nh-01 house race", "nebraska senate"})
        self.assertEqual([leg["party"] for leg in groups["nebraska senate"]], ["independent", "republican"])
        self.assertIn("52+ senate seats", skipped)
        self.assertEqual(unparsed, ["Bitcoin above 100k on election day?"])


class MathsTests(unittest.TestCase):
    def test_no_price(self):
        self.assertEqual(sig_bot.no_price(0.885), 0.115)
        self.assertEqual(sig_bot.no_price(0.14), 0.86)
        self.assertEqual(sig_bot.no_price(0.995), 0.005)

    def test_size(self):
        qty, edge, cost = sig_bot.size_trade([(0.885, 400), (0.14, 1000)], 3000)
        self.assertEqual(qty, 400)
        self.assertAlmostEqual(edge, 0.025)
        self.assertAlmostEqual(cost, 0.975)
        qty, _, _ = sig_bot.size_trade([(0.885, 400), (0.14, 1000)], 97.5)
        self.assertEqual(qty, 100)
        qty, _, _ = sig_bot.size_trade([(0.885, 400), (0.14, 1000)], -5)
        self.assertEqual(qty, 0)

    def test_ranking_by_return_on_cash(self):
        groups = {
            "three legs": [{"exchangeId": "a"}, {"exchangeId": "b"}, {"exchangeId": "c"}],
            "two legs": [{"exchangeId": "d"}, {"exchangeId": "e"}],
        }
        quotes = {"a": {"bestBid": 0.93}, "b": {"bestBid": 0.025}, "c": {"bestBid": 0.12},   # +0.075 for 1.925
                  "d": {"bestBid": 0.92}, "e": {"bestBid": 0.145}}                           # +0.065 for 0.935
        ranked = sig_bot.candidates_from_quotes(groups, quotes)
        self.assertEqual([c["race"] for c in ranked], ["two legs", "three legs"])
        self.assertAlmostEqual(ranked[0]["return"], 0.065 / 0.935)

    def test_payload(self):
        legs = [{"exchangeId": "11"}, {"exchangeId": "12"}]
        body = sig_bot.multi_leg_body("t-cup", legs, [0.885, 0.14], 400)
        self.assertEqual(body["executionMode"], "allOrNone")
        self.assertEqual([leg["price"] for leg in body["legs"]], [0.115, 0.86])
        self.assertTrue(all(leg["side"] == "no" and leg["action"] == "buy" and leg["quantity"] == 400
                            and leg["tournamentId"] == "t-cup" for leg in body["legs"]))
        self.assertTrue(body["idempotencyKey"])


class BotPassTests(unittest.TestCase):
    def make_bot(self, api):
        bot = sig_bot.Bot(api)
        bot.tournament = bot.pick_tournament()
        bot.balance = float(bot.tournament["myBalance"])
        return bot

    def test_picks_the_cup_not_ongoing_play(self):
        bot = self.make_bot(FakeApi())
        self.assertEqual(bot.tournament["slug"], "midterm-elections")

    def test_dry_run_buys_nothing(self):
        sig_bot.LIVE = False
        api = FakeApi()
        bot = self.make_bot(api)
        bot.step()
        self.assertEqual(api.posted, [])
        self.assertEqual(list(bot.dry_seen), ["new hampshire governor"])
        self.assertEqual(bot.dry_seen["new hampshire governor"]["qty"], 400)

    def test_live_places_one_atomic_order(self):
        sig_bot.LIVE = True
        try:
            api = FakeApi()
            bot = self.make_bot(api)
            bot.step()
            self.assertEqual(len(api.posted), 1)
            body = api.posted[0]
            self.assertEqual([leg["exchangeId"] for leg in body["legs"]], ["12", "11"])
            self.assertEqual({leg["quantity"] for leg in body["legs"]}, {400})
            self.assertAlmostEqual(bot.guaranteed_profit, 10.0)
            self.assertAlmostEqual(bot.spent, 390.0)
            bot.step()  # same race is on cooldown: no second order
            self.assertEqual(len(api.posted), 1)
        finally:
            sig_bot.LIVE = False

    def test_budget_respects_cash_reserve(self):
        sig_bot.LIVE = True
        try:
            api = FakeApi(balance=150)   # 150 - reserve 100 = 50 -> 51 sets of 0.975
            bot = self.make_bot(api)
            bot.step()
            self.assertEqual(api.posted[0]["legs"][0]["quantity"], 51)
        finally:
            sig_bot.LIVE = False

    def test_logs_never_print_identity(self):
        import contextlib
        import io
        api = FakeApi()
        bot = sig_bot.Bot(api)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            bot.tournament = bot.pick_tournament()
            account = api.call("GET", "/account")
            sig_bot.log(f"API key OK (account created {str(account.get('createdAt', '?'))[:10]}).")
            bot.refresh_markets()
        text = buffer.getvalue()
        self.assertNotIn("secret-name", text)
        self.assertNotIn("secret@example.com", text)


if __name__ == "__main__":
    unittest.main()
