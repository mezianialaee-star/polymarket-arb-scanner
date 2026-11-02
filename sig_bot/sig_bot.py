#!/usr/bin/env python3
"""Bot for the SIG Predictions Cup, using the official API.

The Official Rules (section 6) allow participants to use the API and to connect bots.

Strategy: riskless cross-market arbitrage.
    The tournament lists one binary market per party for the same race, for example
    "Will the Republican Party win the Texas Senate?" and
    "Will the Democratic Party win the Texas Senate?".
    At most one of them can resolve YES, so buying NO on all n of them pays at least n - 1.
    One set costs sum(1 - bid_i) = n - S, where bid_i is the best YES bid of market i
    and S = sum(bid_i). The guaranteed profit per set is therefore at least S - 1.
    The bot trades only when S - 1 >= MIN_EDGE, with one atomic multi-leg order in
    "allOrNone" mode: every leg fills completely at its limit price, or nothing happens.

Dry run by default: the bot only reports what it would do. It trades only when
MODE=live and LIVE_CONFIRM=yes are both set.

Logs are public on a public repository, so the bot never prints the account's
username or e-mail address.
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone


def env_float(name, default):
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else float(default)
    except ValueError:
        return float(default)


BASE_URL = os.environ.get("SIG_BASE_URL", "https://sig.thesuper.market/api/v1").rstrip("/")
API_KEY = os.environ.get("SIG_API_KEY", "").strip()
MODE = os.environ.get("MODE", "dry").strip().lower()
LIVE = MODE == "live" and os.environ.get("LIVE_CONFIRM", "").strip().lower() == "yes"
TOURNAMENT_SLUG = os.environ.get("TOURNAMENT_SLUG", "").strip()

MIN_EDGE = env_float("MIN_EDGE", 0.01)            # guaranteed profit per set, in SUSQies
MAX_TRADE_COST = env_float("MAX_TRADE_COST", 3000)  # cost cap for one arbitrage
MAX_RUN_COST = env_float("MAX_RUN_COST", 10000)     # cost cap for one run of the bot
CASH_RESERVE = env_float("CASH_RESERVE", 100)       # cash never spent
MIN_QTY = int(env_float("MIN_QTY", 10))             # smaller sets are not worth a write
POLL_SECONDS = env_float("POLL_SECONDS", 15)
RUN_MINUTES = env_float("RUN_MINUTES", 340)
STOP_AT_UTC = os.environ.get("STOP_AT_UTC", "2026-11-04T16:45:00Z")  # trading ends 12:00 EST = 17:00 UTC
MARKETS_REFRESH_S = env_float("MARKETS_REFRESH_S", 900)
REPORT_EVERY_S = env_float("REPORT_EVERY_S", 600)
STATUS_PATH = os.environ.get("STATUS_PATH", "").strip()  # optional Markdown status file

# Words that may follow the office in a market title, e.g. "Ohio Senate special election".
OFFICE_SUFFIXES = r"(?:\s+(?:special|election|seat|race|\(special\)))*"
TITLE_RE = re.compile(
    r"^\s*will\s+(?:the\s+|an\s+|a\s+)?(?P<party>[a-z]+)(?:\s+party|\s+candidate)?"
    r"\s+win\s+(?:the\s+)?(?P<race>.+?)\s*\?*\s*$",
    re.IGNORECASE,
)
# Only one-seat races are grouped. Seat-count markets ("win 50+ seats") are left out
# on purpose: two of those can resolve YES together, so NO on both is not riskless.
RACE_RE = re.compile(
    r"^(?P<place>(?:[a-z][a-z .'\-]*?)|(?:[a-z]{2}-\d{1,2})|(?:[a-z]{2}-at-large))\s+"
    r"(?P<office>senate|governor|gubernatorial|house)" + OFFICE_SUFFIXES + r"$"
)
CONTROL_RACES = {"senate", "house", "u.s. senate", "u.s. house", "us senate", "us house"}
PARTY_ALIASES = {
    "democrat": "democratic", "democrats": "democratic",
    "republicans": "republican", "gop": "republican",
    "independents": "independent",
}


def log(message):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {message}", flush=True)


def parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def write_status(lines):
    """Writes the Markdown status file (if STATUS_PATH is set), atomically."""
    if not STATUS_PATH:
        return
    folder = os.path.dirname(STATUS_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    stamp = f"_Updated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC_"
    with open(STATUS_PATH + ".tmp", "w", encoding="utf-8") as handle:
        handle.write("\n".join([lines[0], "", stamp] + lines[1:]) + "\n")
    os.replace(STATUS_PATH + ".tmp", STATUS_PATH)


class ApiError(Exception):
    def __init__(self, status, code, message="", details=None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class Api:
    """Small client for the Super Market API, within the per-account budget
    (100 reads and 30 writes per minute; the bot keeps a margin)."""

    def __init__(self, base_url, key, reads_per_min=80, writes_per_min=25):
        self.base_url = base_url
        self.key = key
        self.limits = {False: reads_per_min, True: writes_per_min}
        self.history = {False: deque(), True: deque()}
        self.calls = {False: 0, True: 0}

    def _wait_for_budget(self, is_write):
        window = self.history[is_write]
        now = time.time()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.limits[is_write]:
            time.sleep(max(0.0, 60 - (now - window[0])) + 0.5)
        window.append(time.time())
        self.calls[is_write] += 1

    def call(self, method, path, params=None, body=None, max_attempts=5):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
            "User-Agent": "sig-cup-arbitrage-bot/1.0",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        is_write = method not in ("GET", "HEAD")
        for attempt in range(1, max_attempts + 1):
            self._wait_for_budget(is_write)
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", "replace")
                code, message, details = "", raw[:300], {}
                try:
                    envelope = json.loads(raw).get("error") or {}
                    code = envelope.get("code", "")
                    message = envelope.get("message", message)
                    details = envelope.get("details") or {}
                except (ValueError, AttributeError):
                    pass
                # Retries are safe: reads are idempotent, and every order carries an
                # idempotency key that is reused unchanged on a retry.
                transient = (
                    error.status == 429
                    or error.status >= 500
                    or (error.status == 409 and code == "REQUEST_IN_FLIGHT")
                )
                if transient and attempt < max_attempts:
                    retry_after = error.headers.get("Retry-After")
                    try:
                        wait = float(retry_after)
                    except (TypeError, ValueError):
                        wait = 90.0 if code == "REQUEST_IN_FLIGHT" else min(2 ** attempt, 30)
                    wait += random.uniform(0, 1)
                    log(f"  API {method} {path} -> {error.status} {code}, retrying in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise ApiError(error.status, code, message, details)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt < max_attempts:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise ApiError(0, "NETWORK", str(error))
        raise ApiError(0, "RETRIES_EXHAUSTED", f"{method} {path}")


def parse_title(title):
    """'Will the Republican Party win the Texas Senate?' -> ('republican', 'texas senate')."""
    match = TITLE_RE.match(title or "")
    if not match:
        return None
    party = match.group("party").lower()
    party = PARTY_ALIASES.get(party, party)
    race = re.sub(r"\s+", " ", match.group("race").strip().lower())
    return party, race


def is_single_seat_race(race):
    return race in CONTROL_RACES or RACE_RE.match(race) is not None


def build_groups(markets):
    """Groups the open binary party markets by race. Returns (groups, unparsed, skipped)."""
    by_race, unparsed, skipped = {}, [], set()
    for market in markets:
        if market.get("status") != "open" or market.get("isComposite") or market.get("isMultiOutcome"):
            continue
        exchanges = market.get("exchanges") or []
        if len(exchanges) != 1:
            continue
        parsed = parse_title(market.get("title"))
        if not parsed:
            unparsed.append(market.get("title"))
            continue
        party, race = parsed
        if not is_single_seat_race(race):
            skipped.add(race)
            continue
        by_race.setdefault(race, {}).setdefault(party, []).append({
            "marketId": str(market["id"]),
            "exchangeId": str(exchanges[0]["id"]),
            "title": market.get("title"),
        })
    groups = {}
    for race, parties in by_race.items():
        if len(parties) < 2:
            continue
        if any(len(entries) != 1 for entries in parties.values()):
            skipped.add(race + " (duplicate markets)")
            continue
        groups[race] = [dict(party=party, **entries[0]) for party, entries in sorted(parties.items())]
    return groups, unparsed, skipped


def candidates_from_quotes(groups, quotes):
    """Every race with all its best YES bids known, sorted by edge S - 1 (best first)."""
    out = []
    for race, legs in groups.items():
        bids = []
        for leg in legs:
            quote = quotes.get(leg["exchangeId"]) or {}
            bid = quote.get("bestBid")
            if bid is None:
                bids = None
                break
            bids.append(float(bid))
        if bids is None:
            continue
        total = sum(bids)
        cost = len(bids) - total  # cost of one set of NO shares
        ret = (total - 1) / cost if cost > 0 else 0.0
        out.append({"race": race, "legs": legs, "bids": bids, "S": total, "edge": total - 1, "return": ret})
    # Best return on the cash spent first (cash is the limit, not the number of sets).
    out.sort(key=lambda c: c["return"], reverse=True)
    return out


def no_price(bid):
    """Price of a NO share bought against a YES bid, on the 0.005 tick."""
    return round(round((1 - bid) / 0.005) * 0.005, 3)


def size_trade(levels, budget):
    """levels: [(best YES bid, quantity at that bid)] for each leg. Returns (qty, edge, cost/set)."""
    bids = [price for price, _ in levels]
    edge = sum(bids) - 1
    cost_per_set = sum(no_price(bid) for bid in bids)
    depth = min(quantity for _, quantity in levels)
    if cost_per_set <= 0:
        return 0, edge, cost_per_set
    qty = int(min(depth, max(0.0, budget) / cost_per_set))
    return qty, edge, cost_per_set


def multi_leg_body(tournament_id, legs, bids, qty):
    return {
        "idempotencyKey": str(uuid.uuid4()),
        "executionMode": "allOrNone",
        "legs": [
            {
                "exchangeId": leg["exchangeId"],
                "side": "no",
                "action": "buy",
                "quantity": int(qty),
                "price": no_price(bid),
                "tournamentId": tournament_id,
            }
            for leg, bid in zip(legs, bids)
        ],
    }


class Bot:
    def __init__(self, api):
        self.api = api
        self.tournament = None
        self.groups = {}
        self.balance = 0.0
        self.spent = 0.0
        self.guaranteed_profit = 0.0
        self.trades = []
        self.dry_seen = {}
        self.cooldown = {}
        self.last_markets = 0.0
        self.last_report = 0.0
        self.last_balance = 0.0
        self.closest = []
        self.spreads = []
        self.market_info = {}
        self.last_error = ""
        self.state = "starting"

    # --- setup -----------------------------------------------------------------
    def pick_tournament(self):
        if TOURNAMENT_SLUG:
            return self.api.call("GET", f"/tournaments/{urllib.parse.quote(TOURNAMENT_SLUG)}")
        result = self.api.call("GET", "/tournaments", {"status": "active", "limit": 100})
        tournaments = (result or {}).get("data") or []
        if not tournaments:
            raise SystemExit("No active tournament is visible with this API key.")
        for t in tournaments:
            log(f"  tournament seen: '{t.get('name')}' (slug {t.get('slug')}, balance {t.get('myBalance')})")

        def preference(t):
            name = f"{t.get('name', '')} {t.get('slug', '')}".lower()
            return (
                "midterm" in name or "prediction" in name or "cup" in name,
                "ongoing" not in name,
                not t.get("isPendingEnrolment"),
            )

        tournaments.sort(key=preference, reverse=True)
        return tournaments[0]

    def refresh_markets(self):
        slug = self.tournament["slug"]
        markets, cursor = [], None
        for _ in range(50):
            params = {"status": "open", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.api.call("GET", f"/tournaments/{urllib.parse.quote(slug)}/markets", params) or {}
            markets += page.get("data") or []
            pagination = page.get("pagination") or {}
            cursor = pagination.get("nextCursor")
            if not pagination.get("hasMore") or not cursor:
                break
        groups, unparsed, skipped = build_groups(markets)
        first = not self.groups
        self.groups = groups
        self.last_markets = time.time()
        legs = sum(len(v) for v in groups.values())
        self.market_info = {"open": len(markets), "races": len(groups), "legs": legs,
                            "unparsed": unparsed[:10], "skipped": sorted(skipped)[:10]}
        log(f"Markets: {len(markets)} open, {len(groups)} races with 2+ party markets ({legs} markets).")
        if first:
            for race, legs_ in sorted(groups.items())[:60]:
                log(f"  race '{race}': " + ", ".join(leg["party"] for leg in legs_))
            if unparsed:
                log(f"  {len(unparsed)} titles not in the 'Will the X Party win ...' format, e.g.:")
                for title in unparsed[:15]:
                    log(f"    - {title}")
            if skipped:
                log(f"  left out (not one-seat races): {sorted(skipped)[:20]}")

    def refresh_balance(self):
        slug = self.tournament["slug"]
        info = self.api.call("GET", f"/tournaments/{urllib.parse.quote(slug)}") or {}
        if info.get("myBalance") is not None:
            self.balance = float(info["myBalance"])
        self.last_balance = time.time()

    # --- market data -------------------------------------------------------------
    def fetch_quotes(self):
        ids = [leg["exchangeId"] for legs in self.groups.values() for leg in legs]
        quotes = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            result = self.api.call(
                "GET", "/exchanges/prices",
                {"ids": ",".join(chunk), "tournamentId": self.tournament["id"]},
            ) or {}
            for quote in result.get("data") or []:
                quotes[str(quote.get("exchangeId"))] = quote
        return quotes

    def best_bid(self, market_id, exchange_id):
        """(price, quantity) of the best YES bid, read from the order book."""
        book = self.api.call(
            "GET", f"/markets/{market_id}/orderbook",
            {"tournamentId": self.tournament["id"], "depth": 5},
        ) or {}
        exchanges = book.get("exchanges") or []
        for context in book.get("contexts") or []:
            tournament = context.get("tournament") or {}
            if str(tournament.get("id")) == str(self.tournament["id"]):
                exchanges = (context.get("orderbook") or {}).get("exchanges") or exchanges
        for exchange in exchanges:
            if str(exchange.get("exchangeId")) == str(exchange_id):
                bids = exchange.get("bids") or []
                if not bids:
                    return None
                top = max(bids, key=lambda level: float(level["price"]))
                return float(top["price"]), float(top["quantity"])
        return None

    # --- one pass ------------------------------------------------------------------
    def step(self):
        now = time.time()
        if now - self.last_markets > MARKETS_REFRESH_S:
            self.refresh_markets()
        if now - self.last_balance > 300:
            self.refresh_balance()
        if not self.groups:
            return
        quotes = self.fetch_quotes()
        candidates = candidates_from_quotes(self.groups, quotes)
        self.closest = candidates[:8]
        self.spreads = [
            float(q["bestAsk"]) - float(q["bestBid"])
            for q in quotes.values()
            if q.get("bestAsk") is not None and q.get("bestBid") is not None
        ]
        budget = min(MAX_TRADE_COST, MAX_RUN_COST - self.spent, self.balance - CASH_RESERVE)
        if LIVE and budget < MIN_QTY:
            # No cash to use: skip the order-book reads until the balance changes.
            candidates = []
            self.state = "running, waiting for free cash (all cash above the reserve is used)"
        elif self.state.startswith("running"):
            self.state = "running"
        for candidate in candidates:
            if candidate["edge"] < MIN_EDGE:
                continue
            if self.cooldown.get(candidate["race"], 0) > now:
                continue
            self.try_arbitrage(candidate)
        if now - self.last_report > REPORT_EVERY_S:
            self.report()

    def try_arbitrage(self, candidate):
        race, legs = candidate["race"], candidate["legs"]
        levels = []
        for leg in legs:
            level = self.best_bid(leg["marketId"], leg["exchangeId"])
            if level is None:
                return
            levels.append(level)
        budget = min(MAX_TRADE_COST, MAX_RUN_COST - self.spent, self.balance - CASH_RESERVE)
        qty, edge, cost_per_set = size_trade(levels, budget)
        if edge < MIN_EDGE:
            return
        bids = [price for price, _ in levels]
        profit = qty * edge
        text = (f"{race}: YES bids {' + '.join(f'{b:.3f}' for b in bids)} = {sum(bids):.3f}, "
                f"edge {edge:.3f}/set, qty {qty}, cost {qty * cost_per_set:,.0f}, "
                f"guaranteed profit >= {profit:,.0f}")
        if qty < MIN_QTY:
            log(f"  too small or no budget left: {text}")
            self.cooldown[race] = time.time() + 300
            return
        if not LIVE:
            seen = self.dry_seen.setdefault(race, {"first": time.time(), "count": 0, "best_edge": 0.0,
                                                     "best_profit": 0.0, "qty": 0})
            seen["count"] += 1
            seen["best_edge"] = max(seen["best_edge"], edge)
            if profit > seen["best_profit"]:
                seen["best_profit"], seen["qty"] = profit, qty
            log(f"[DRY RUN] would buy NO on {len(legs)} markets: {text}")
            self.cooldown[race] = time.time() + 120
            return
        body = multi_leg_body(self.tournament["id"], legs, bids, qty)
        try:
            result = self.api.call("POST", "/orders/multi-leg", body=body)
        except ApiError as error:
            reason = (error.details or {}).get("rejectionReasonCode", "")
            if reason == "AllOrNoneNotFilled":
                log(f"  prices moved before the order, nothing was bought: {race}")
            else:
                log(f"  order refused ({error}): {race}")
                self.last_error = f"order refused for {race}: {error}"
            self.cooldown[race] = time.time() + 300
            return
        self.spent += qty * cost_per_set
        self.balance -= qty * cost_per_set
        self.guaranteed_profit += profit
        self.trades.append({"race": race, "qty": qty, "edge": edge, "cost": qty * cost_per_set, "profit": profit})
        log(f"[TRADE] bought NO on {len(legs)} markets: {text}")
        log(f"  response: {json.dumps(result)[:400]}")
        self.cooldown[race] = time.time() + 60
        write_status(self.status_lines())

    def report(self):
        self.last_report = time.time()
        log(f"Status: mode {'LIVE' if LIVE else 'DRY RUN'}, balance {self.balance:,.0f}, "
            f"spent {self.spent:,.0f}, guaranteed profit {self.guaranteed_profit:,.0f}, "
            f"API calls {self.api.calls[False]} reads / {self.api.calls[True]} writes.")
        if self.closest:
            log("  Races closest to an arbitrage (S = sum of the best YES bids; > 1 = riskless profit):")
            for c in self.closest:
                log(f"    S = {c['S']:.3f}  {c['race']}  ({' + '.join(f'{b:.3f}' for b in c['bids'])})")
        if self.spreads:
            spreads = sorted(self.spreads)
            median = spreads[len(spreads) // 2]
            wide = sum(1 for s in spreads if s >= 0.03)
            log(f"  Spreads (best ask - best bid): median {median:.3f}, {wide}/{len(spreads)} markets at 0.03 or more.")
        write_status(self.status_lines())

    def status_lines(self):
        t = self.tournament or {}
        info = self.market_info
        lines = [f"# SIG bot: {'LIVE' if LIVE else 'DRY RUN (buys nothing)'}", "",
                 f"- State: {self.state}"]
        if t:
            lines.append(f"- Tournament: {t.get('name')} (`{t.get('slug')}`)")
        lines += [
            f"- Cash: {self.balance:,.0f}",
            f"- Spent on arbitrage sets: {self.spent:,.0f}",
            f"- Guaranteed profit locked in: {self.guaranteed_profit:,.0f}",
            f"- Settings: MIN_EDGE {MIN_EDGE}, MAX_TRADE_COST {MAX_TRADE_COST:,.0f}, "
            f"MAX_RUN_COST {MAX_RUN_COST:,.0f}, CASH_RESERVE {CASH_RESERVE:,.0f}",
            f"- API calls: {self.api.calls[False]} reads, {self.api.calls[True]} writes",
        ]
        if self.last_error:
            lines.append(f"- Last error: {self.last_error}")
        if info:
            lines.append(f"- Markets: {info['open']} open, {info['races']} races with 2+ party markets "
                         f"({info['legs']} markets)")
            if info.get("unparsed"):
                lines.append("- Titles not understood (sample): " + " / ".join(map(str, info["unparsed"])))
            if info.get("skipped"):
                lines.append("- Left out on purpose: " + ", ".join(info["skipped"]))
        if self.closest:
            lines += ["", "## Races closest to an arbitrage", "",
                      "S = sum of the best YES bids. Above 1 = riskless profit of S - 1 per set.", "",
                      "| S | Race | Bids |", "|---|---|---|"]
            for c in self.closest:
                lines.append(f"| {c['S']:.3f} | {c['race']} | {' + '.join(f'{b:.3f}' for b in c['bids'])} |")
        if self.spreads:
            spreads = sorted(self.spreads)
            lines.append("")
            lines.append(f"Spreads: median {spreads[len(spreads) // 2]:.3f}, "
                         f"{sum(1 for s in spreads if s >= 0.03)}/{len(spreads)} markets at 0.03 or more.")
        if self.dry_seen:
            lines += ["", "## Riskless arbitrages seen (dry run)", "",
                      "| Race | Times seen | Best edge/set | Sets | Profit |", "|---|---|---|---|---|"]
            for race, s in sorted(self.dry_seen.items(), key=lambda kv: -kv[1]["best_profit"]):
                lines.append(f"| {race} | {s['count']} | {s['best_edge']:.3f} | {s['qty']} | {s['best_profit']:,.0f} |")
        if self.trades:
            lines += ["", "## Trades", "", "| Race | Sets | Edge/set | Cost | Guaranteed profit |",
                      "|---|---|---|---|---|"]
            for trade in self.trades[-30:]:
                lines.append(f"| {trade['race']} | {trade['qty']} | {trade['edge']:.3f} | "
                             f"{trade['cost']:,.0f} | {trade['profit']:,.0f} |")
        return lines

    # --- run -----------------------------------------------------------------------------
    def run(self):
        try:
            account = self.api.call("GET", "/account") or {}
            log(f"API key OK (account created {str(account.get('createdAt', '?'))[:10]}). "
                f"Mode: {'LIVE' if LIVE else 'DRY RUN (nothing is bought)'}.")
            self.tournament = self.pick_tournament()
        except ApiError as error:
            self.state = "stopped: the API key was refused"
            self.last_error = str(error)
            log(f"API error at start: {error}")
            log("Check the SIG_API_KEY secret, its scopes (read + trade), and that the e-mail is confirmed.")
            write_status(self.status_lines())
            return 1
        self.balance = float(self.tournament.get("myBalance") or 0)
        log(f"Tournament: '{self.tournament.get('name')}' (slug {self.tournament.get('slug')}), "
            f"cash {self.balance:,.2f}.")
        log(f"Settings: MIN_EDGE {MIN_EDGE}, MAX_TRADE_COST {MAX_TRADE_COST:,.0f}, "
            f"MAX_RUN_COST {MAX_RUN_COST:,.0f}, CASH_RESERVE {CASH_RESERVE:,.0f}.")
        self.state = "running"
        write_status(self.status_lines())
        deadline = min(time.time() + RUN_MINUTES * 60, parse_utc(STOP_AT_UTC))
        errors = 0
        while time.time() < deadline:
            started = time.time()
            try:
                self.step()
                errors = 0
            except ApiError as error:
                errors += 1
                self.last_error = str(error)
                log(f"API error: {error}")
                if error.status in (401, 403):
                    log("The key is refused (check the SIG_API_KEY secret and its scopes: read + trade).")
                    self.state = "stopped: the API key was refused"
                    break
                if errors >= 10:
                    log("Too many errors in a row, stopping.")
                    self.state = "stopped: too many errors in a row"
                    break
                time.sleep(30)
            time.sleep(max(1.0, POLL_SECONDS - (time.time() - started)))
        if self.state == "running":
            self.state = "session finished (the next one starts automatically)"
        self.report()
        self.summary()
        return 0

    def summary(self):
        lines = [f"## SIG bot run ({'LIVE' if LIVE else 'dry run'})", ""]
        lines.append(f"- Cash at the end: {self.balance:,.0f}")
        lines.append(f"- Spent on arbitrage sets: {self.spent:,.0f}")
        lines.append(f"- Guaranteed profit locked in: {self.guaranteed_profit:,.0f}")
        lines.append(f"- API calls: {self.api.calls[False]} reads, {self.api.calls[True]} writes")
        if self.trades:
            lines += ["", "| Race | Sets | Edge/set | Cost | Guaranteed profit |", "|---|---|---|---|---|"]
            for t in self.trades:
                lines.append(f"| {t['race']} | {t['qty']} | {t['edge']:.3f} | {t['cost']:,.0f} | {t['profit']:,.0f} |")
        if self.dry_seen:
            total = sum(s["best_profit"] for s in self.dry_seen.values())
            lines += ["", f"Dry run: {len(self.dry_seen)} races offered a riskless arbitrage "
                          f"(up to {total:,.0f} SUSQies at the best moment of each).", "",
                      "| Race | Times seen | Best edge/set | Sets | Profit |", "|---|---|---|---|---|"]
            for race, s in sorted(self.dry_seen.items(), key=lambda kv: -kv[1]["best_profit"]):
                lines.append(f"| {race} | {s['count']} | {s['best_edge']:.3f} | {s['qty']} | {s['best_profit']:,.0f} |")
        text = "\n".join(lines)
        print(text, flush=True)
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")


def main():
    if not API_KEY:
        print("SIG_API_KEY is missing: add it as a repository secret named SIG_API_KEY.", flush=True)
        write_status(["# SIG bot: waiting for its API key", "",
                      "- State: the repository secret SIG_API_KEY is missing, so the bot did not start."])
        return 2
    if MODE == "live" and not LIVE:
        log("MODE=live without LIVE_CONFIRM=yes: running as a dry run.")
    return Bot(Api(BASE_URL, API_KEY)).run()


if __name__ == "__main__":
    sys.exit(main())
