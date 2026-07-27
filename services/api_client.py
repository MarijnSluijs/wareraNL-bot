import asyncio
import json as _json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence

import aiohttp
import aiosqlite

logger = logging.getLogger("discord_bot")


# ── shared API usage tracker ──────────────────────────────────────────────
# Every real HTTP attempt made by any APIClient instance in this process is
# recorded here (endpoint, which key was used, status, success/failure) so a
# hidden dashboard can show call volume, endpoint breakdown and per-key
# fairness across discord-bot / data-fetcher / rijksoverheid-web. Buffered in
# memory and flushed periodically so logging never adds latency to real calls.
_USAGE_DB_PATH = os.getenv("RW_API_USAGE_DB_PATH", "database/api_usage.db")
_USAGE_FLUSH_INTERVAL = 5.0
_USAGE_RETENTION_DAYS = 30
_usage_buffer: list[tuple] = []
_usage_flush_task: Optional[asyncio.Task] = None
_usage_last_prune_at: float = 0.0

_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    key_index INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    status INTEGER,
    ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts);
CREATE INDEX IF NOT EXISTS idx_api_calls_endpoint ON api_calls(endpoint);
CREATE INDEX IF NOT EXISTS idx_api_calls_source ON api_calls(source);
"""


def _record_api_call(
    source: str, key_index: int, endpoint: str, status: Optional[int], ok: bool
) -> None:
    _usage_buffer.append((
        datetime.now(timezone.utc).isoformat(),
        source,
        key_index,
        endpoint,
        status,
        1 if ok else 0,
    ))
    global _usage_flush_task
    if _usage_flush_task is None or _usage_flush_task.done():
        try:
            loop = asyncio.get_running_loop()
            _usage_flush_task = loop.create_task(_usage_flush_loop())
        except RuntimeError:
            pass  # no running loop (e.g. called outside async context) — will flush on next call


async def _usage_flush_loop() -> None:
    while True:
        await asyncio.sleep(_USAGE_FLUSH_INTERVAL)
        await _flush_usage_buffer()


async def _flush_usage_buffer() -> None:
    global _usage_buffer, _usage_last_prune_at
    if not _usage_buffer:
        return
    batch, _usage_buffer = _usage_buffer, []
    try:
        os.makedirs(os.path.dirname(_USAGE_DB_PATH) or ".", exist_ok=True)
        async with aiosqlite.connect(_USAGE_DB_PATH) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.executescript(_USAGE_SCHEMA)
            await conn.executemany(
                "INSERT INTO api_calls (ts, source, key_index, endpoint, status, ok) VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            now = time.monotonic()
            if now - _usage_last_prune_at > 3600:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=_USAGE_RETENTION_DAYS)).isoformat()
                await conn.execute("DELETE FROM api_calls WHERE ts < ?", (cutoff,))
                _usage_last_prune_at = now
            await conn.commit()
    except Exception:
        logger.warning("api_client: failed to flush usage stats", exc_info=True)


class APIClient:
    """
    Async API client with retries, exponential backoff and API-key rotation.

    - Supports passing an ordered list of API keys (`api_keys`) which will be rotated
      when the server returns rate-limit / auth errors.
    - Implements retries with exponential backoff for transient errors (5xx, 429, network).
    """

    def __init__(
        self,
        base_url: str,
        concurrency: int = 10,
        timeout: int = 30,
        headers: Optional[Dict[str, str]] = None,
        api_keys: Optional[Sequence[str]] = None,
        source: str = "unknown",
    ) -> None:
        # Labels every call this client makes in the shared usage tracker
        # (see _record_api_call below) — e.g. "discord-bot", "data-fetcher",
        # "rijksoverheid-web". Purely for the /api-usage dashboard.
        self._source = source
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        # fallback headers provided by caller
        self._base_headers: Dict[str, str] = dict(headers or {})
        self.last_success_at: Optional[datetime] = None
        self.last_failure_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.last_status: Optional[int] = None
        self.is_available: Optional[bool] = None

        # API key rotation
        self._api_keys = list(api_keys) if api_keys else []
        self._key_index = 0
        self._key_rate_limited_until: Dict[int, float] = {}
        if self._api_keys:
            # ensure header contains the active API key
            self._base_headers["x-api-key"] = self._api_keys[self._key_index]

    def _set_active_key_index(self, key_index: int) -> None:
        self._key_index = key_index
        self._base_headers["x-api-key"] = self._api_keys[self._key_index]

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            logger.info("APIClient session started with %d API key(s)", len(self._api_keys))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("APIClient session closed")

    def _record_success(self, status: int) -> None:
        self.last_success_at = datetime.now(timezone.utc)
        self.last_status = status
        self.last_error = None
        self.is_available = True

    def _record_failure(self, error: object, status: Optional[int] = None) -> None:
        self.last_failure_at = datetime.now(timezone.utc)
        self.last_status = status
        self.last_error = str(error)
        self.is_available = False

    def status_snapshot(self) -> dict[str, object]:
        return {
            "available": self.is_available,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
            "last_status": self.last_status,
            "base_url": self.base_url,
        }

    def _rotate_key(self) -> None:
        if not self._api_keys:
            return
        next_index = (self._key_index + 1) % len(self._api_keys)
        self._set_active_key_index(next_index)
        logger.warning(
            "Rotated API key: now using key %d/%d",
            self._key_index + 1, len(self._api_keys),
        )

    def _mark_key_rate_limited(
        self, key_index: int, retry_after: Optional[float], fallback_wait: float
    ) -> None:
        if not self._api_keys:
            return
        wait_seconds = retry_after if retry_after is not None else fallback_wait
        wait_seconds = max(wait_seconds, 0.0)
        now = asyncio.get_running_loop().time()
        limited_until = now + wait_seconds
        current_until = self._key_rate_limited_until.get(key_index, 0.0)
        if limited_until > current_until:
            self._key_rate_limited_until[key_index] = limited_until

    def _next_available_key_index(self) -> Optional[int]:
        if not self._api_keys:
            return None
        now = asyncio.get_running_loop().time()
        key_count = len(self._api_keys)
        for offset in range(1, key_count + 1):
            idx = (self._key_index + offset) % key_count
            if self._key_rate_limited_until.get(idx, 0.0) <= now:
                return idx
        return None

    def _seconds_until_next_key_available(self) -> float:
        if not self._api_keys:
            return 0.0
        now = asyncio.get_running_loop().time()
        soonest = min(
            self._key_rate_limited_until.get(i, 0.0) for i in range(len(self._api_keys))
        )
        return max(soonest - now, 0.0)

    def _acquire_key_for_attempt(self) -> int:
        """Round-robin: return the next non-rate-limited key index and advance the pointer.

        Each concurrent request gets a different key so all keys are used at equal rate.
        If all keys are currently rate-limited, returns the current index (caller will sleep).
        """
        if not self._api_keys:
            return 0
        now = asyncio.get_running_loop().time()
        key_count = len(self._api_keys)
        for offset in range(key_count):
            idx = (self._key_index + offset) % key_count
            if self._key_rate_limited_until.get(idx, 0.0) <= now:
                # Advance pointer past this key so the next caller gets a different one
                self._key_index = (idx + 1) % key_count
                self._base_headers["x-api-key"] = self._api_keys[idx]
                return idx
        # All keys are rate-limited — return current without advancing
        return self._key_index

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        if self._session is None:
            raise RuntimeError("APIClient not started; call start() first")

        url = f"{self.base_url}{path}"

        attempts = 0
        max_attempts = 5
        backoff = 1.0

        while attempts < max_attempts:
            attempts += 1
            # Round-robin: pick next available API key for this attempt
            attempt_key_index = self._acquire_key_for_attempt()
            try:
                # merge default headers with per-call headers
                call_kwargs = dict(kwargs)
                call_headers = dict(self._base_headers)
                # Pin the key for this specific attempt so concurrent requests
                # don't interfere with each other's key selection.
                if self._api_keys:
                    call_headers["x-api-key"] = self._api_keys[attempt_key_index]
                # Always pop "headers" (may be None from default args) before merging,
                # so setdefault / direct assignment is never blocked by a None value.
                per_call_headers = call_kwargs.pop("headers", None)
                if per_call_headers:
                    call_headers.update(per_call_headers)
                if call_headers:
                    call_kwargs["headers"] = call_headers

                async with self._semaphore:
                    async with self._session.request(
                        method, url, **call_kwargs
                    ) as resp:
                        status = resp.status
                        _record_api_call(
                            self._source, attempt_key_index, path, status,
                            200 <= status < 300,
                        )
                        # success
                        if 200 <= status < 300:
                            self._record_success(status)
                            try:
                                return await resp.json()
                            except Exception:
                                return await resp.text()

                        # handle rate limiting: respect Retry-After if provided
                        if status == 429:
                            retry_after = None
                            try:
                                ra = resp.headers.get("Retry-After")
                                if ra is not None:
                                    retry_after = float(ra)
                            except Exception:
                                retry_after = None

                            logger.warning(
                                "Rate-limited on %s (429, key %d/%d). Retry-after=%s, attempt %d/%d",
                                url,
                                attempt_key_index + 1,
                                len(self._api_keys) if self._api_keys else 0,
                                retry_after,
                                attempts,
                                max_attempts,
                            )

                            if self._api_keys:
                                self._mark_key_rate_limited(
                                    attempt_key_index, retry_after, backoff
                                )
                                next_idx = self._next_available_key_index()
                                if next_idx is not None:
                                    logger.warning(
                                        "Rate-limited: key %d/%d → switching to key %d/%d",
                                        attempt_key_index + 1, len(self._api_keys),
                                        next_idx + 1, len(self._api_keys),
                                    )
                                    if attempts < max_attempts:
                                        continue

                                sleep_for = self._seconds_until_next_key_available()
                                if sleep_for <= 0:
                                    sleep_for = (
                                        retry_after
                                        if retry_after is not None
                                        else backoff
                                    )
                                now_t = asyncio.get_running_loop().time()
                                n_limited = sum(
                                    1 for i in range(len(self._api_keys))
                                    if self._key_rate_limited_until.get(i, 0.0) > now_t
                                )
                                logger.warning(
                                    "Rate-limited: %d/%d API keys exhausted; waiting %.2fs before retry",
                                    n_limited, len(self._api_keys), sleep_for,
                                )
                                await asyncio.sleep(sleep_for)
                            else:
                                await asyncio.sleep(
                                    retry_after if retry_after is not None else backoff
                                )

                            backoff = min(backoff * 2, 30.0)
                            if attempts < max_attempts:
                                continue
                            # fallthrough to raise after loop

                        # rotate on auth failures and retry once
                        if status in (401, 403):
                            logger.warning(
                                "Auth error %s on %s - rotating key if possible",
                                status,
                                url,
                            )
                            if self._api_keys:
                                self._rotate_key()
                                await asyncio.sleep(0.5)
                                if attempts < max_attempts:
                                    continue

                        # retry on server errors
                        if 500 <= status < 600 and attempts < max_attempts:
                            logger.warning(
                                "Server error %s on %s - retrying after %s seconds",
                                status,
                                url,
                                backoff,
                            )
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2, 30.0)
                            continue

                        if status == 400:
                            try:
                                error_data = await resp.json()
                                logger.error("Bad request to %s: %s", url, error_data)
                            except Exception:
                                logger.error(
                                    "Bad request to %s with status 400 and non-JSON response",
                                    url,
                                )
                            self._record_failure(f"HTTP {status}", status=status)
                            resp.raise_for_status()

                        # otherwise raise the status error
                        self._record_failure(f"HTTP {status}", status=status)
                        resp.raise_for_status()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # ClientResponseError (from resp.raise_for_status() above) was
                # already recorded at the `status = resp.status` point; only
                # genuine network-level failures (no response at all) are new here.
                if not isinstance(e, aiohttp.ClientResponseError):
                    _record_api_call(self._source, attempt_key_index, path, None, False)
                # 4xx errors are definitive (not transient) — don't retry them
                if isinstance(e, aiohttp.ClientResponseError) and 400 <= e.status < 500:
                    raise
                logger.warning(
                    "API request error %s - attempt %d/%d",
                    str(e),
                    attempts,
                    max_attempts,
                )
                if attempts < max_attempts:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                status = (
                    e.status if isinstance(e, aiohttp.ClientResponseError) else None
                )
                self._record_failure(e, status=status)
                raise
        self._record_failure("Max API attempts exhausted")
        raise RuntimeError("Max API attempts exhausted")

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        return await self._request(
            "GET", path, params=params, json=json, headers=headers
        )

    async def post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        return await self._request("POST", path, json=json, headers=headers)

    async def ping(self) -> dict[str, object]:
        """Check whether the WarEra API responds to a lightweight request."""
        started = asyncio.get_running_loop().time()
        try:
            await self.get("/country.getAllCountries")
            elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            return {
                "ok": True,
                "latency_ms": elapsed_ms,
                **self.status_snapshot(),
            }
        except Exception as exc:
            elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            self._record_failure(exc)
            return {
                "ok": False,
                "latency_ms": elapsed_ms,
                **self.status_snapshot(),
            }

    @staticmethod
    def _unwrap_trpc_batch_item(item: Any) -> Any:
        """Extract the data payload from one tRPC batch response element."""
        if not isinstance(item, dict):
            return item
        result = item.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            return data if data is not None else result
        if "error" in item:
            return None
        return item

    async def batch_get(
        self,
        procedure: str,
        inputs: list[Dict[str, Any]],
        *,
        batch_size: int = 100,
        chunk_sleep: float = 0.0,
    ) -> list[Any]:
        """Call one tRPC procedure for many inputs using tRPC HTTP batching.

        Each chunk of up to *batch_size* inputs is sent as a single HTTP request:
                GET /proc,proc,...?batch=1&input={"0":{...},"1":{...},...}
        If the server doesn't return a list of the right length the whole chunk
        falls back to individual ``get()`` calls automatically.

        Returns a flat list of unwrapped results in the same order as *inputs*.
        *chunk_sleep* seconds are awaited between HTTP requests (default 0 — rely
        on 429 backoff instead of artificial delays).
        """
        proc = procedure.lstrip("/")
        all_results: list[Any] = []

        for chunk_start in range(0, len(inputs), batch_size):
            if chunk_start > 0 and chunk_sleep > 0:
                await asyncio.sleep(chunk_sleep)

            chunk = inputs[chunk_start : chunk_start + batch_size]
            path = "/" + ",".join(proc for _ in chunk)
            input_map = {str(j): inp for j, inp in enumerate(chunk)}
            params = {"batch": "1", "input": _json.dumps(input_map)}

            chunk_results: list[Any] | None = None
            try:
                resp = await self._request("GET", path, params=params)
                if isinstance(resp, list) and len(resp) == len(chunk):
                    chunk_results = [
                        self._unwrap_trpc_batch_item(item) for item in resp
                    ]
                else:
                    logger.warning(
                        "batch_get: unexpected response shape for chunk %d (got %s), falling back",
                        chunk_start // batch_size,
                        type(resp).__name__,
                    )
            except Exception as exc:
                logger.warning(
                    "batch_get: batch of %d failed (%s), falling back to individual calls",
                    len(chunk),
                    exc,
                )

            if chunk_results is None:
                chunk_results = []
                for inp in chunk:
                    try:
                        result = await self._request(
                            "GET", f"/{proc}", params={"input": _json.dumps(inp)}
                        )
                        chunk_results.append(result)
                    except Exception:
                        chunk_results.append(None)
                    # await asyncio.sleep(0.3)

            all_results.extend(chunk_results)

        return all_results


__all__ = ["APIClient"]
