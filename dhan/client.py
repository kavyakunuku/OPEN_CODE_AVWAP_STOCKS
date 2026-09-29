"""DhanHQ REST client (API v2).

Auth (verified against the official dhanhq SDK, 2026-09):
  * The access token generated in the Dhan dashboard is passed AS-IS in the
    `access-token` header (it is already a JWT; do NOT re-sign it).
  * Every request also carries `client-id: <client id>`.
  * POST bodies include `dhanClientId` (as the official SDK does).

Also keeps lightweight per-process telemetry (request counts, 429s, errors,
latencies, last success/failure) exposed via `stats()` for the dashboard's
System / Data Health pages - making rate-limit and auth incidents visible
instead of discoverable only by reading log files.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("avwap.dhan.client")


class DhanAPIError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


class DhanREST:
    def __init__(
        self,
        client_id: str,
        access_token: str,
        base_url: str = "https://api.dhan.co",
        timeout: float = 10.0,
    ):
        if not client_id or not access_token:
            raise ValueError("Dhan client_id and access_token are required")
        self.client_id = str(client_id)
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "avwap-trader/1.0",
            }
        )
        # ------------------------------------------------- telemetry (V2)
        self._stats_lock = threading.Lock()
        self._stats = {
            "requests": 0,          # total HTTP attempts
            "h429": 0,              # attempts that hit HTTP 429 (rate limit)
            "errors": 0,            # final failures (after retries)
            "last_latency_ms": 0.0,
            "avg_latency_ms": 0.0,
            "last_success_ts": 0,
            "last_error_ts": 0,
            "last_error": "",
        }
        self._latency_sum_ms = 0.0
        self._by_path: dict[str, dict] = {}

    # ------------------------------------------------------------- telemetry
    def _note_attempt(self, path: str, latency_ms: float,
                      h429: bool = False, error: bool = False,
                      err_msg: str = "") -> None:
        with self._stats_lock:
            s = self._stats
            s["requests"] += 1
            s["last_latency_ms"] = latency_ms
            self._latency_sum_ms += latency_ms
            s["avg_latency_ms"] = self._latency_sum_ms / s["requests"]
            if h429:
                s["h429"] += 1
            if error:
                s["errors"] += 1
                s["last_error_ts"] = int(time.time())
                s["last_error"] = (err_msg or "error")[:300]
            else:
                s["last_success_ts"] = int(time.time())
            bp = self._by_path.get(path)
            if bp is None:
                if len(self._by_path) < 32:
                    bp = self._by_path[path] = {
                        "requests": 0, "h429": 0, "errors": 0,
                        "last_latency_ms": 0.0,
                    }
            if bp is not None:
                bp["requests"] += 1
                bp["last_latency_ms"] = latency_ms
                if h429:
                    bp["h429"] += 1
                if error:
                    bp["errors"] += 1

    def stats(self) -> dict:
        """Snapshot for the dashboard (safe copy)."""
        with self._stats_lock:
            out = dict(self._stats)
            out["by_path"] = {k: dict(v) for k, v in self._by_path.items()}
            return out

    # ------------------------------------------------------------------ auth
    def _headers(self) -> dict:
        return {
            "access-token": self.access_token,
            "client-id": self.client_id,
        }

    # --------------------------------------------------------------- request
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        retries: int = 2,
    ) -> dict:
        url = f"{self.base_url}{path}"
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            t0 = time.perf_counter()
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                latency_ms = (time.perf_counter() - t0) * 1000
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    self._note_attempt(path, latency_ms,
                                       h429=(resp.status_code == 429))
                    if resp.status_code == 429:
                        # expected: Dhan's shared rate limits; the retry
                        # normally succeeds - log quietly to avoid spam
                        log.info("Dhan %s %s -> HTTP 429 (rate limit); retrying in %.1fs",
                                 method, path, wait)
                    else:
                        log.warning(
                            "Dhan %s %s -> HTTP %s; retrying in %.1fs",
                            method, path, resp.status_code, wait,
                        )
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    self._note_attempt(
                        path, latency_ms, error=True,
                        err_msg=f"HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                    raise DhanAPIError(
                        f"Dhan API error {resp.status_code} for {method} {path}: "
                        f"{resp.text[:400]}",
                        status=resp.status_code,
                        body=resp.text[:2000],
                    )
                if not resp.text:
                    self._note_attempt(path, latency_ms)
                    return {}
                try:
                    out = resp.json()
                except ValueError as e:
                    self._note_attempt(
                        path, latency_ms, error=True,
                        err_msg=f"non-JSON response: {resp.text[:200]}",
                    )
                    raise DhanAPIError(
                        f"Non-JSON response from {method} {path}: {resp.text[:200]}",
                        status=resp.status_code,
                        body=resp.text[:2000],
                    ) from e
                self._note_attempt(path, latency_ms)
                return out
            except (requests.ConnectionError, requests.Timeout) as e:
                latency_ms = (time.perf_counter() - t0) * 1000
                last_err = e
                self._note_attempt(path, latency_ms, error=(attempt >= retries),
                                   err_msg=f"{e.__class__.__name__}: {e}")
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    log.warning("Dhan %s %s network error (%s); retrying in %.1fs",
                                method, path, e.__class__.__name__, wait)
                    time.sleep(wait)
                    continue
                raise DhanAPIError(f"Network error on {method} {path}: {e}") from e
        raise DhanAPIError(f"Exhausted retries on {method} {path}: {last_err}")

    def get(self, path: str, params: Optional[dict] = None, retries: int = 2) -> dict:
        return self._request("GET", path, params=params, retries=retries)

    def post(self, path: str, payload: Optional[dict] = None, params: Optional[dict] = None,
             retries: int = 2) -> dict:
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("dhanClientId", self.client_id)
        return self._request("POST", path, params=params, payload=payload, retries=retries)

    # ------------------------------------------------------- file downloads
    def get_text(self, url: str, timeout: float = 60.0) -> str:
        resp = self._session.get(url, timeout=timeout,
                                 headers={"User-Agent": "avwap-trader/1.0"})
        if resp.status_code >= 400:
            raise DhanAPIError(
                f"Download failed {resp.status_code} for {url}: {resp.text[:200]}",
                status=resp.status_code,
                body=resp.text[:2000],
            )
        return resp.text
