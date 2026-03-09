import asyncio
import logging
from typing import Any, Awaitable, Callable, Iterable, Optional

from .api_client import APIClient
from .db import Database

logger = logging.getLogger("services.worker")


ProgressCallback = Optional[Callable[[int, int], Awaitable[None]]]


class Worker:
    """Worker orchestration skeleton.

    Usage: create a Worker with an `APIClient` and optional `Database`, then call
    `await worker.run_job(job_id, items, progress_cb)` where `items` are the units
    of work (e.g., identifiers to fetch via the API).
    """

    def __init__(
        self,
        api_client: APIClient,
        db: Optional[Database] = None,
        concurrency: int = 10,
    ):
        self.api_client = api_client
        self.db = db
        self._semaphore = asyncio.Semaphore(concurrency)

    async def _process_item(self, item: Any) -> Any:
        # replace with the real request; example assumes item is an endpoint path
        async with self._semaphore:
            return await self.api_client.get(item)

    async def run_job(
        self, job_id: str, items: Iterable[Any], progress_cb: ProgressCallback = None
    ) -> None:
        items = list(items)
        total = len(items)
        if self.db:
            await self.db.create_job(job_id)

        completed = 0

        async def _run():
            nonlocal completed
            for item in items:
                try:
                    await self._process_item(item)
                except Exception:
                    logger.exception("Error processing item %s", item)
                completed += 1
                if self.db:
                    await self.db.update_job_progress(
                        job_id, int((completed / total) * 100)
                    )
                if progress_cb:
                    await progress_cb(completed, total)

        await _run()
        if self.db:
            await self.db.update_job_progress(job_id, 100, status="completed")


__all__ = ["Worker"]
