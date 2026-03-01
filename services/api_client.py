import asyncio
import logging
import json as _json
from typing import Any, Dict, Optional, Sequence

import aiohttp

logger = logging.getLogger("discord_bot")


class APIClient:
	"""Async API client with retries, exponential backoff and API-key rotation.

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
	) -> None:
		self.base_url = base_url.rstrip("/")
		self._session: Optional[aiohttp.ClientSession] = None
		self._semaphore = asyncio.Semaphore(concurrency)
		self._timeout = aiohttp.ClientTimeout(total=timeout)
		# fallback headers provided by caller
		self._base_headers: Dict[str, str] = dict(headers or {})

		# API key rotation
		self._api_keys = list(api_keys) if api_keys else []
		self._key_index = 0
		if self._api_keys:
			# ensure header contains the active API key
			self._base_headers["x-api-key"] = self._api_keys[self._key_index]

	async def start(self) -> None:
		if self._session is None:
			self._session = aiohttp.ClientSession(timeout=self._timeout)
			logger.info("APIClient session started")

	async def close(self) -> None:
		if self._session:
			await self._session.close()
			self._session = None
			logger.info("APIClient session closed")

	def _rotate_key(self) -> None:
		if not self._api_keys:
			return
		self._key_index = (self._key_index + 1) % len(self._api_keys)
		self._base_headers["x-api-key"] = self._api_keys[self._key_index]
		logger.info("Rotated API key to index %d", self._key_index)

	async def _request(self, method: str, path: str, **kwargs) -> Any:
		if self._session is None:
			raise RuntimeError("APIClient not started; call start() first")

		url = f"{self.base_url}{path}"

		attempts = 0
		max_attempts = 5
		backoff = 1.0

		while attempts < max_attempts:
			attempts += 1
			try:
				# merge default headers with per-call headers
				call_kwargs = dict(kwargs)
				call_headers = dict(self._base_headers)
				# Always pop "headers" (may be None from default args) before merging,
				# so setdefault / direct assignment is never blocked by a None value.
				per_call_headers = call_kwargs.pop("headers", None)
				if per_call_headers:
					call_headers.update(per_call_headers)
				if call_headers:
					call_kwargs["headers"] = call_headers

				async with self._semaphore:
					async with self._session.request(method, url, **call_kwargs) as resp:
						status = resp.status
						# success
						if 200 <= status < 300:
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

							logger.warning("Rate limited on %s (429). Retry-after=%s attempt %d/%d", url, retry_after, attempts, max_attempts)
							# rotate key if available
							if self._api_keys:
								self._rotate_key()
							# wait either server-specified time or backoff
							await asyncio.sleep(retry_after if retry_after is not None else backoff)
							backoff = min(backoff * 2, 30.0)
							if attempts < max_attempts:
								continue
							# fallthrough to raise after loop

						# rotate on auth failures and retry once
						if status in (401, 403):
							logger.warning("Auth error %s on %s - rotating key if possible", status, url)
							if self._api_keys:
								self._rotate_key()
								await asyncio.sleep(0.5)
								if attempts < max_attempts:
									continue

						# retry on server errors
						if 500 <= status < 600 and attempts < max_attempts:
							logger.warning("Server error %s on %s - retrying after %s seconds", status, url, backoff)
							await asyncio.sleep(backoff)
							backoff = min(backoff * 2, 30.0)
							continue

						if status == 400:
							try:
								error_data = await resp.json()
								logger.error("Bad request to %s: %s", url, error_data)
							except Exception:
							 	logger.error("Bad request to %s with status 400 and non-JSON response", url)
							continue

						# otherwise raise the status error
						resp.raise_for_status()
			except (aiohttp.ClientError, asyncio.TimeoutError) as e:
				logger.warning("API request error %s - attempt %d/%d", str(e), attempts, max_attempts)
				if attempts < max_attempts:
					await asyncio.sleep(backoff)
					backoff = min(backoff * 2, 30.0)
					continue
				raise

	async def get(self, path: str, params: Optional[Dict[str, Any]] = None, json: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
		return await self._request("GET", path, params=params, json=json, headers=headers)

	async def post(self, path: str, json: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
		return await self._request("POST", path, json=json, headers=headers)

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
		batch_size: int = 30,
		chunk_sleep: float = 1.0,
	) -> list[Any]:
		"""Call one tRPC procedure for many inputs using tRPC HTTP batching.

		Each chunk of up to *batch_size* inputs is sent as a single HTTP request:
			GET /proc,proc,...?batch=1&input={"0":{...},"1":{...},...}
		If the server doesn't return a list of the right length the whole chunk
		falls back to individual ``get()`` calls automatically.

		Returns a flat list of unwrapped results in the same order as *inputs*.
		*chunk_sleep* seconds are awaited between HTTP requests.
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
					chunk_results = [self._unwrap_trpc_batch_item(item) for item in resp]
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
						result = await self._request("GET", f"/{proc}", params={"input": _json.dumps(inp)})
						chunk_results.append(result)
					except Exception:
						chunk_results.append(None)
					await asyncio.sleep(0.3)

			all_results.extend(chunk_results)

		return all_results


__all__ = ["APIClient"]
