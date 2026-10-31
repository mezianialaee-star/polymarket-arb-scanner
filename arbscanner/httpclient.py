"""Small JSON-over-HTTP client built only on the Python standard library.

Why stdlib only: the GitHub Actions job then needs no `pip install`, which removes
a whole class of things that can break.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

USER_AGENT = "Mozilla/5.0 (compatible; polymarket-arb-scanner/1.0; read-only research)"

# Status codes worth retrying (temporary problems on the server side or throttling).
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ApiError(Exception):
    """Raised when a request keeps failing after all retries."""

    def __init__(self, message: str, status: Optional[int] = None, url: str = "", body: str = "",
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body
        self.retry_after = retry_after  # seconds the server asked us to wait (429), if given

    @property
    def is_blocked(self) -> bool:
        """True when the server refuses us (not a temporary failure)."""
        return self.status in (401, 403)


class HttpClient:
    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 4,
        backoff: float = 1.0,
        pause: float = 0.1,
        sleep=time.sleep,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.pause = pause  # politeness pause after every request
        self._sleep = sleep
        self._lock = threading.Lock()  # requests may be sent from several threads
        self.request_count = 0

    def get_json(self, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = base.rstrip("/") + path
        if params:
            clean = {k: _param_value(v) for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(clean)
        return self._request("GET", url, None)

    def post_json(self, base: str, path: str, payload: Any) -> Any:
        url = base.rstrip("/") + path
        return self._request("POST", url, payload)

    def _request(self, method: str, url: str, payload: Any) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Optional[ApiError] = None
        for attempt in range(self.retries + 1):
            if attempt > 0:
                wait = self.backoff * (2 ** (attempt - 1))
                if last_error is not None and last_error.status == 429:
                    wait = max(wait, 2.0, last_error.retry_after or 0.0)
                self._sleep(min(wait, 30.0))
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with self._lock:
                self.request_count += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                if self.pause:
                    self._sleep(self.pause)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    last_error = ApiError(f"Invalid JSON from {url}: {exc}", None, url, raw[:300].decode("utf-8", "replace"))
                    continue
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read()[:500].decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 - best effort only
                    pass
                last_error = ApiError(f"HTTP {exc.code} for {method} {url}: {body}", exc.code, url, body,
                                      _retry_after(exc, body))
                if exc.code not in RETRYABLE_STATUS:
                    raise last_error
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as exc:
                last_error = ApiError(f"Network error for {method} {url}: {exc}", None, url)
        assert last_error is not None
        raise last_error


def _retry_after(exc: urllib.error.HTTPError, body: str) -> Optional[float]:
    """Seconds to wait, from the Retry-After header or a JSON 'retry_after_seconds' field."""
    candidates = []
    try:
        candidates.append(exc.headers.get("Retry-After"))
    except Exception:  # noqa: BLE001 - headers may be missing
        pass
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            candidates.append(parsed.get("retry_after_seconds"))
    except (ValueError, TypeError):
        pass
    for value in candidates:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= seconds < 3600:
            return seconds
    return None


def _param_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
