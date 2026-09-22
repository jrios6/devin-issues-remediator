"""Freshness ledger for upstream reads.

The dashboard reloading every 10s only proves the dashboard is alive — it says
nothing about whether the dispatcher's calls to GitHub/Devin are working. An
expired token otherwise leaves stale data looking current.

Dispatcher wraps each upstream call in `observe(provider, resource)` (e.g.
`github:pr:11`, `github:issues`, `devin:session:<id>`), which records the last
success time or a sanitized error (HTTP status only — never a response body).
`snapshot(expected)` compares those readings against the resources the
dispatcher expects to have read within `max_age` seconds and reports each as
healthy / stale / pending / error; a provider with no readings is "idle".
State is in-memory and resets on restart.

Consumers: `GET /api/integrations` exposes the snapshot; the dashboard's
`checksFresh()` uses it to show CI as "Unverified" rather than trusting a
result fetched before the reads started failing.
"""
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import httpx


@dataclass
class SyncResult:
    last_success: float | None = None
    error: str | None = None


class SyncHealth:
    def __init__(self):
        self._lock = threading.Lock()
        self._results: dict[tuple[str, str], SyncResult] = {}

    @contextmanager
    def observe(self, provider: str, resource: str):
        try:
            yield
        except Exception as error:
            if isinstance(error, httpx.HTTPStatusError):
                message = f"HTTP {error.response.status_code}"
            elif isinstance(error, httpx.TimeoutException):
                message = "Request timed out"
            else:
                message = "Sync request failed"
            with self._lock:
                result = self._results.setdefault((provider, resource), SyncResult())
                result.error = message
            raise
        else:
            with self._lock:
                self._results[(provider, resource)] = SyncResult(time.time())

    def snapshot(self, expected: Mapping[str, Mapping[str, float]]) -> dict:
        now = time.time()
        providers = {}
        with self._lock:
            for provider, resources in expected.items():
                readings = {}
                for resource, max_age in resources.items():
                    result = self._results.get((provider, resource), SyncResult())
                    if result.error:
                        status = "error"
                    elif result.last_success is None:
                        status = "pending"
                    elif now - result.last_success > max_age:
                        status = "stale"
                    else:
                        status = "healthy"
                    readings[resource] = {
                        "status": status,
                        "last_success": result.last_success,
                        "error": result.error,
                    }
                statuses = {r["status"] for r in readings.values()}
                status = next((s for s in ("error", "stale", "pending", "healthy")
                               if s in statuses), "idle")
                successes = [r["last_success"] for r in readings.values()]
                providers[provider] = {
                    "status": status,
                    "last_success": min(successes) if successes and
                    all(t is not None for t in successes) else None,
                    "resources": readings,
                }
        return providers
