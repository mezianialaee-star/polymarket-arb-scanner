"""A fake Polymarket (Gamma + CLOB) HTTP server for end-to-end tests.

Responses follow the formats documented by Polymarket (field names, JSON-encoded
string lists, string prices, keyset pagination with next_cursor, POST /books).
"""

import json
import threading
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

COND = "0x" + "ab" * 32


def cond(i):
    return "0x" + f"{i:064x}"


def market(mid, title, yes_id, no_id, yes_price, version="v1", fees=("0.04", 1), fees_enabled=True,
           closed=False, accepting=True, outcomes=("Yes", "No"), extra=None):
    m = {
        "id": str(mid),
        "question": f"Will {title} win?",
        "groupItemTitle": title,
        "conditionId": cond(mid),
        "slug": f"m-{mid}",
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps([str(yes_price), str(D(1) - D(str(yes_price)))]),
        "active": True,
        "closed": closed,
        "archived": False,
        "acceptingOrders": accepting,
        "enableOrderBook": True,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "version": version,
        "description": "long text " * 50,
    }
    if version == "v2":
        m["clobTokenIds"] = None
        m["positionIds"] = [yes_id, no_id]
    else:
        m["clobTokenIds"] = json.dumps([yes_id, no_id])
        m["positionIds"] = None
    if fees_enabled is not None:
        m["feesEnabled"] = fees_enabled
    if fees is not None:
        m["feeSchedule"] = {"rate": fees[0], "exponent": fees[1], "takerOnly": True, "rebateRate": 0.25}
    if extra:
        m.update(extra)
    return m


def event(eid, title, markets, neg_risk=True, augmented=None, category="Politics"):
    e = {
        "id": str(eid),
        "slug": f"event-{eid}",
        "title": title,
        "active": True,
        "closed": False,
        "archived": False,
        "negRisk": neg_risk,
        "enableNegRisk": bool(neg_risk),
        "category": category,
        "markets": markets,
    }
    if augmented is not None:
        e["negRiskAugmented"] = augmented
    return e


def book(asset_id, condition, bids, asks, neg_risk=True, min_size="5", reverse=False):
    lv = lambda levels: [{"price": p, "size": s} for p, s in levels]
    b, a = lv(bids), lv(asks)
    if reverse:  # deliberately badly ordered, to prove we sort ourselves
        b, a = list(reversed(b)), list(reversed(a))
    return {
        "market": condition, "asset_id": asset_id, "timestamp": "1793440000000", "hash": "h",
        "bids": b, "asks": a, "min_order_size": min_size, "tick_size": "0.01",
        "neg_risk": neg_risk, "last_trade_price": "0.5",
    }


def mirror(levels):
    """NO book side from the opposite YES side: price -> 1 - price, same size."""
    return [(str(D(1) - D(p)), s) for p, s in levels]


def build_world(truncated=False):
    """Events + books with known answers.

    E1001 (3 outcomes, fee 0.04): YES basket arbitrage, exec profit 0.964 at 40 units.
    E1002 (4 outcomes, v2 ids, fees disabled): NO basket arbitrage, exec profit 0.50 at 10 units.
    E1003 (2 outcomes, no fee info -> pessimistic 0.07): 0.99 before fees, loses after fees.
    E1004 augmented neg-risk: excluded.   E1005 not neg-risk: excluded.
    E1006 one outcome already resolved NO: evaluated on the 2 remaining, no arbitrage.
    E1007 one id rejected by the CLOB: 'missing_book'.
    E1008 displayed prices sum to 0.40 (incomplete list): YES basket refused.
    E1009 a market not accepting orders: excluded.
    """
    events = []
    books = {}

    def add_market(mid, title, yes_bids, yes_asks, yes_price, reverse=False, **kw):
        y, n = f"{mid}1", f"{mid}2"
        events_markets.append(market(mid, title, y, n, yes_price, **kw))
        books[y] = book(y, cond(mid), yes_bids, yes_asks, reverse=reverse)
        books[n] = book(n, cond(mid), mirror(yes_asks), mirror(yes_bids), reverse=reverse)

    events_markets = []
    add_market(11, "A1", [("0.28", "100")], [("0.30", "50"), ("0.33", "100")], "0.29")
    add_market(12, "A2", [("0.27", "100")], [("0.30", "40"), ("0.34", "100")], "0.285", reverse=True)
    add_market(13, "A3", [("0.33", "100")], [("0.35", "60"), ("0.40", "100")], "0.34")
    events.append(event(1001, "Who wins A?", events_markets))

    events_markets = []
    for mid, bid, ask, size in ((21, "0.30", "0.32", "20"), (22, "0.28", "0.30", "30"),
                                (23, "0.25", "0.27", "25"), (24, "0.22", "0.24", "10")):
        add_market(mid, f"B{mid}", [(bid, size)], [(ask, "100")], str((D(bid) + D(ask)) / 2),
                   version="v2", fees=None, fees_enabled=False)
    events.append(event(1002, "Who wins B?", events_markets, category="Sports"))

    events_markets = []
    add_market(31, "C1", [("0.47", "100")], [("0.49", "100")], "0.48", fees=None, fees_enabled=None)
    add_market(32, "C2", [("0.48", "100")], [("0.50", "100")], "0.49", fees=None, fees_enabled=None)
    events.append(event(1003, "Who wins C?", events_markets))

    events_markets = []
    add_market(41, "D1", [("0.40", "100")], [("0.30", "100")], "0.35")
    add_market(42, "Other", [("0.40", "100")], [("0.30", "100")], "0.35", extra={"negRiskOther": True})
    events.append(event(1004, "Augmented D", events_markets, augmented=True))

    events_markets = []
    add_market(51, "E1", [("0.10", "100")], [("0.12", "100")], "0.11")
    add_market(52, "E2", [("0.10", "100")], [("0.12", "100")], "0.11")
    events.append(event(1005, "Independent E", events_markets, neg_risk=False))

    events_markets = []
    add_market(61, "F1", [("0.53", "100")], [("0.55", "100")], "0.54")
    add_market(62, "F2", [("0.45", "100")], [("0.47", "100")], "0.46")
    events_markets.append(market(63, "F3", "631", "632", "0", closed=True, accepting=False))
    events.append(event(1006, "Who wins F?", events_markets))

    events_markets = []
    add_market(71, "G1", [("0.45", "100")], [("0.47", "100")], "0.46")
    events_markets.append(market(72, "G2", "BAD-TOKEN", "722", "0.5"))
    books["722"] = book("722", cond(72), [("0.48", "100")], [("0.50", "100")])
    events.append(event(1007, "Who wins G?", events_markets))

    events_markets = []
    add_market(81, "H1", [("0.18", "100")], [("0.20", "100")], "0.20")
    add_market(82, "H2", [("0.18", "100")], [("0.20", "100")], "0.20")
    events.append(event(1008, "Incomplete H", events_markets))

    events_markets = []
    add_market(91, "I1", [("0.18", "100")], [("0.20", "100")], "0.5")
    add_market(92, "I2", [("0.18", "100")], [("0.20", "100")], "0.5", accepting=False)
    events.append(event(1009, "Paused I", events_markets))
    if truncated:
        # E1010: the listing shows 3 outcomes, the single-event endpoint shows 4.
        events_markets = []
        add_market(101, "J1", [("0.38", "100")], [("0.40", "100")], "0.39")
        add_market(102, "J2", [("0.28", "100")], [("0.30", "100")], "0.29")
        add_market(103, "J3", [("0.25", "100")], [("0.27", "100")], "0.26")
        listed = event(1010, "Truncated J", list(events_markets))
        add_market(104, "J4", [("0.02", "100")], [("0.03", "100")], "0.025")
        full = event(1010, "Truncated J", events_markets)
        events.append(listed)
        return events, books, {"1010": full}
    return events, books


class FakePolymarket:
    def __init__(self, events, books, page_size=3, full_events=None):
        self.events = events
        self.books = books
        self.full_events = full_events or {}
        self.page_size = page_size
        self.keyset_enabled = True
        self.keyset_returns_list = False
        self.fail_first_books = False
        self.force_status = None          # e.g. 403 for every request
        self.books_bad_shape = False
        self.requests = []
        self._books_calls = 0
        self._server = None
        self._thread = None

    @property
    def url(self):
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep test output clean
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.requests.append(("GET", u.path, q, self.headers.get("User-Agent")))
                if fake.force_status:
                    return self._send(fake.force_status, {"error": "forced"})
                if u.path == "/events/keyset":
                    if not fake.keyset_enabled:
                        return self._send(404, {"error": "not found"})
                    if fake.keyset_returns_list:
                        return self._send(200, fake.events[:2])
                    if "offset" in q:
                        return self._send(422, {"error": "offset not allowed"})
                    assert q.get("closed") == "false", q
                    limit = int(q.get("limit", 20))
                    assert 1 <= limit <= 100
                    start = int(q.get("after_cursor", "0") or 0)
                    page = fake.events[start:start + min(limit, fake.page_size)]
                    resp = {"events": page}
                    if start + len(page) < len(fake.events):
                        resp["next_cursor"] = str(start + len(page))
                    return self._send(200, resp)
                if u.path.startswith("/events/") and u.path.count("/") == 2 and u.path != "/events/keyset":
                    eid = u.path.rsplit("/", 1)[1]
                    if eid in fake.full_events:
                        return self._send(200, fake.full_events[eid])
                    for ev in fake.events:
                        if ev["id"] == eid:
                            return self._send(200, ev)
                    return self._send(404, {"error": "not found"})
                if u.path == "/events":
                    assert q.get("closed") == "false", q
                    limit = int(q.get("limit", 20))
                    off = int(q.get("offset", 0))
                    return self._send(200, fake.events[off:off + limit])
                return self._send(404, {"error": "unknown path"})

            def do_POST(self):
                u = urlparse(self.path)
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"null")
                fake.requests.append(("POST", u.path, payload, self.headers.get("User-Agent")))
                if fake.force_status:
                    return self._send(fake.force_status, {"error": "forced"})
                if u.path != "/books":
                    return self._send(404, {"error": "unknown path"})
                fake._books_calls += 1
                if fake.fail_first_books and fake._books_calls == 1:
                    return self._send(503, {"error": "temporarily unavailable"})
                if fake.books_bad_shape:
                    return self._send(200, {"unexpected": True})
                if not isinstance(payload, list) or len(payload) > 500:
                    return self._send(400, {"error": "Invalid payload"})
                ids = [p.get("token_id") for p in payload]
                if "BAD-TOKEN" in ids:
                    return self._send(400, {"error": "Invalid payload"})
                return self._send(200, [fake.books[t] for t in ids if t in fake.books])

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
