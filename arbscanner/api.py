"""Read-only access to Polymarket's public APIs (no account, no key, no orders)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .httpclient import ApiError, HttpClient
from .universe import SchemaError

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

SIDES = ("BUY", "SELL")


class _RejectBudget:
    """Counts token ids the API refused. Many refusals means something changed: stop,
    rather than spend thousands of requests isolating them one by one."""

    def __init__(self, n_tokens: int, path: str):
        self.limit = max(25, min(200, int(0.02 * n_tokens)))
        self.used = 0
        self.path = path
        self._lock = threading.Lock()

    def spend(self) -> None:
        with self._lock:
            self.used += 1
            if self.used > self.limit:
                raise SchemaError(
                    f"POST {self.path}: more than {self.limit} token ids were refused — "
                    "the event list may be stale or the API changed"
                )


class PolymarketClient:
    def __init__(
        self,
        gamma_url: str = GAMMA_URL,
        clob_url: str = CLOB_URL,
        http: Optional[HttpClient] = None,
        page_limit: int = 100,
        max_pages: int = 2000,
        books_batch: int = 200,
        prices_batch: int = 250,
        workers: int = 4,
    ):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.http = http or HttpClient()
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.books_batch = books_batch      # tokens per POST /books
        self.prices_batch = prices_batch    # tokens per POST /prices (2 items each: BUY and SELL; docs: max 500 items)
        self.workers = max(1, workers)      # parallel requests (well below the documented rate limits)
        self.last_fetch_stats: Dict[str, Any] = {}

    # ------------------------------------------------------------------ events

    def iter_open_events(self, time_budget_s: float = 300.0) -> Iterator[Dict[str, Any]]:
        """Yield every open event (with its markets).

        Uses the documented keyset endpoint (/events/keyset + next_cursor). If that
        endpoint is unavailable, falls back to the classic /events?offset= listing.
        `self.last_fetch_stats` tells whether the listing was complete.
        """
        start = time.monotonic()
        stats: Dict[str, Any] = {"endpoint": "keyset", "pages": 0, "complete": False}
        self.last_fetch_stats = stats
        cursor: Optional[str] = None
        try:
            first = self.http.get_json(self.gamma_url, "/events/keyset", {"closed": False, "limit": self.page_limit})
        except ApiError as exc:
            if exc.status in (400, 404, 405, 410, 422):
                yield from self._iter_events_offset(start, time_budget_s, stats)
                return
            raise
        if not isinstance(first, dict) or not isinstance(first.get("events"), list):
            # Keyset endpoint answered in an unknown format: use the classic listing instead.
            stats["keyset_unexpected"] = type(first).__name__
            yield from self._iter_events_offset(start, time_budget_s, stats)
            return
        page: Any = first
        while True:
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise SchemaError(f"/events/keyset returned an unexpected shape: {type(page).__name__} keys={list(page)[:10] if isinstance(page, dict) else ''}")
            stats["pages"] += 1
            for ev in page["events"]:
                yield ev
            cursor = page.get("next_cursor")
            if not cursor or not page["events"]:
                stats["complete"] = True
                return
            if stats["pages"] >= self.max_pages:
                stats["stopped"] = "max_pages"
                return
            if time.monotonic() - start > time_budget_s:
                stats["stopped"] = "time_budget"
                return
            page = self.http.get_json(
                self.gamma_url, "/events/keyset",
                {"closed": False, "limit": self.page_limit, "after_cursor": cursor},
            )

    def _iter_events_offset(self, start: float, time_budget_s: float, stats: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        stats["endpoint"] = "offset"
        offset = 0
        while True:
            try:
                page = self.http.get_json(
                    self.gamma_url, "/events",
                    {"closed": False, "limit": self.page_limit, "offset": offset},
                )
            except ApiError as exc:
                if offset > 0 and exc.status in (400, 422):
                    stats["stopped"] = f"offset_rejected_{exc.status}"
                    return
                raise
            if not isinstance(page, list):
                raise SchemaError(f"/events returned an unexpected shape: {type(page).__name__}")
            stats["pages"] += 1
            for ev in page:
                yield ev
            if len(page) < self.page_limit:
                stats["complete"] = True
                return
            offset += self.page_limit
            if stats["pages"] >= self.max_pages:
                stats["stopped"] = "max_pages"
                return
            if time.monotonic() - start > time_budget_s:
                stats["stopped"] = "time_budget"
                return

    def iter_newest_events(self, max_pages: int = 5) -> Iterator[Dict[str, Any]]:
        """Open events, newest first (keyset listing ordered by id, descending).

        The caller stops reading as soon as it reaches events it already knows, so
        usually only one page is requested.
        """
        params: Dict[str, Any] = {"closed": False, "limit": self.page_limit, "order": "id", "ascending": False}
        page = self.http.get_json(self.gamma_url, "/events/keyset", params)
        for n in range(max_pages):
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise SchemaError(f"/events/keyset (newest first) returned an unexpected shape: {type(page).__name__}")
            for ev in page["events"]:
                yield ev
            cursor = page.get("next_cursor")
            if not cursor or not page["events"] or n + 1 >= max_pages:
                return
            page = self.http.get_json(self.gamma_url, "/events/keyset", dict(params, after_cursor=cursor))

    def get_event(self, event_id: str) -> Dict[str, Any]:
        """Fetch one event with its full list of markets (GET /events/{id})."""
        resp = self.http.get_json(self.gamma_url, f"/events/{event_id}")
        if isinstance(resp, list) and len(resp) == 1:
            resp = resp[0]
        if not isinstance(resp, dict) or not isinstance(resp.get("markets"), list):
            raise SchemaError(f"/events/{event_id} returned an unexpected shape: {type(resp).__name__}")
        return resp

    # --------------------------------------------------------- batched CLOB reads

    def _run_batches(self, batches: List[List[str]], fn: Callable[[List[str]], Any]) -> List[Any]:
        """Run fn on every batch, a few in parallel. The first error stops the rest."""
        if self.workers <= 1 or len(batches) <= 1:
            return [fn(b) for b in batches]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(fn, b) for b in batches]
            try:
                return [f.result() for f in futures]
            except BaseException:
                for f in futures:
                    f.cancel()
                raise

    def _post_split(
        self,
        path: str,
        batch: List[str],
        payload: Callable[[List[str]], Any],
        collect: Callable[[Any, List[str]], None],
        stats: Dict[str, int],
        budget: _RejectBudget,
    ) -> None:
        """POST one batch. If the API refuses it (400/404), split it in two until the
        refused id is isolated, so one bad id can never hide the others."""
        if not batch:
            return
        stats["requests"] += 1
        try:
            resp = self.http.post_json(self.clob_url, path, payload(batch))
        except ApiError as exc:
            if exc.status in (400, 404):
                if len(batch) == 1:
                    stats["rejected_tokens"] += 1
                    budget.spend()
                    return
                mid = len(batch) // 2
                self._post_split(path, batch[:mid], payload, collect, stats, budget)
                self._post_split(path, batch[mid:], payload, collect, stats, budget)
                return
            raise
        collect(resp, batch)

    def _fetch_many(
        self,
        path: str,
        token_ids: Sequence[str],
        batch_size: int,
        payload: Callable[[List[str]], Any],
        parse: Callable[[Any, List[str], Dict[str, Any]], None],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        unique = list(dict.fromkeys(str(t) for t in token_ids))
        batches = [unique[i : i + batch_size] for i in range(0, len(unique), batch_size)]
        budget = _RejectBudget(len(unique), path)

        def run(batch: List[str]) -> Tuple[Dict[str, Any], Dict[str, int]]:
            out: Dict[str, Any] = {}
            stats = {"requests": 0, "rejected_tokens": 0}
            self._post_split(path, batch, payload, lambda resp, b: parse(resp, b, out), stats, budget)
            return out, stats

        merged: Dict[str, Any] = {}
        total = {"requested": len(unique), "received": 0, "rejected_tokens": 0, "requests": 0}
        for out, stats in self._run_batches(batches, run):
            merged.update(out)
            total["requests"] += stats["requests"]
            total["rejected_tokens"] += stats["rejected_tokens"]
        total["received"] = len(merged)
        return merged, total

    # ------------------------------------------------------------------- books

    def fetch_books(self, token_ids: Sequence[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        """Full order books for many tokens (POST /books)."""

        def parse(resp: Any, batch: List[str], out: Dict[str, Any]) -> None:
            if not isinstance(resp, list):
                raise SchemaError(f"POST /books returned an unexpected shape: {type(resp).__name__}")
            wanted = set(batch)
            for book in resp:
                if not isinstance(book, dict) or "asset_id" not in book or "asks" not in book or "bids" not in book:
                    raise SchemaError(f"POST /books returned a book without asset_id/asks/bids: {str(book)[:200]}")
                asset = str(book["asset_id"])
                if asset in wanted:
                    out[asset] = book

        return self._fetch_many("/books", token_ids, self.books_batch,
                                lambda b: [{"token_id": t} for t in b], parse)

    # ------------------------------------------------------------------ prices

    def fetch_prices(self, token_ids: Sequence[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        """Best price on BOTH sides for many tokens (POST /prices).

        Returns {token: {"BUY": value, "SELL": value}} as received (values not parsed;
        a side can be missing). Which side is the ask is decided from the data later:
        Polymarket's own docs disagree on it (see screen.py).
        """

        def parse(resp: Any, batch: List[str], out: Dict[str, Any]) -> None:
            if not isinstance(resp, dict):
                raise SchemaError(f"POST /prices returned an unexpected shape: {type(resp).__name__}")
            for t in batch:
                if t in resp:
                    out[t] = resp[t]

        return self._fetch_many("/prices", token_ids, self.prices_batch,
                                lambda b: [{"token_id": t, "side": s} for t in b for s in SIDES], parse)

    # ------------------------------------------------------------- diagnostics

    def get_json(self, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.http.get_json(base, path, params)

    def post_json(self, base: str, path: str, payload: Any) -> Any:
        return self.http.post_json(base, path, payload)
