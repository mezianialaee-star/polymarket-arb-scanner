"""Read-only access to Polymarket's public APIs (no account, no key, no orders)."""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .httpclient import ApiError, HttpClient
from .universe import SchemaError

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


class PolymarketClient:
    def __init__(
        self,
        gamma_url: str = GAMMA_URL,
        clob_url: str = CLOB_URL,
        http: Optional[HttpClient] = None,
        page_limit: int = 100,
        max_pages: int = 1000,
        books_batch: int = 200,
    ):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.http = http or HttpClient()
        self.page_limit = page_limit
        self.max_pages = max_pages
        self.books_batch = books_batch
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

    def get_event(self, event_id: str) -> Dict[str, Any]:
        """Fetch one event with its full list of markets (GET /events/{id})."""
        resp = self.http.get_json(self.gamma_url, f"/events/{event_id}")
        if isinstance(resp, list) and len(resp) == 1:
            resp = resp[0]
        if not isinstance(resp, dict) or not isinstance(resp.get("markets"), list):
            raise SchemaError(f"/events/{event_id} returned an unexpected shape: {type(resp).__name__}")
        return resp

    # ------------------------------------------------------------------- books

    def fetch_books(self, token_ids: Sequence[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        """Fetch order books for many tokens with POST /books.

        If a batch is rejected (400), it is split in two until the bad token is
        isolated, so one bad id can never hide the other books.
        """
        books: Dict[str, Dict[str, Any]] = {}
        stats = {"requested": len(token_ids), "received": 0, "rejected_tokens": 0, "requests": 0}
        unique = list(dict.fromkeys(str(t) for t in token_ids))
        for i in range(0, len(unique), self.books_batch):
            self._fetch_batch(unique[i : i + self.books_batch], books, stats)
        stats["received"] = len(books)
        return books, stats

    def _fetch_batch(self, batch: List[str], books: Dict[str, Dict[str, Any]], stats: Dict[str, Any]) -> None:
        if not batch:
            return
        stats["requests"] += 1
        try:
            resp = self.http.post_json(self.clob_url, "/books", [{"token_id": t} for t in batch])
        except ApiError as exc:
            if exc.status in (400, 404):
                if len(batch) == 1:
                    stats["rejected_tokens"] += 1
                    return
                mid = len(batch) // 2
                self._fetch_batch(batch[:mid], books, stats)
                self._fetch_batch(batch[mid:], books, stats)
                return
            raise
        if not isinstance(resp, list):
            raise SchemaError(f"POST /books returned an unexpected shape: {type(resp).__name__}")
        wanted = set(batch)
        for book in resp:
            if not isinstance(book, dict) or "asset_id" not in book or "asks" not in book or "bids" not in book:
                raise SchemaError(f"POST /books returned a book without asset_id/asks/bids: {str(book)[:200]}")
            asset = str(book["asset_id"])
            if asset in wanted:
                books[asset] = book
