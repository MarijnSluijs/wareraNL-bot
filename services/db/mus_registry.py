"""MusRegistryMixin — stores all game MUs fetched via mu.getManyPaginated."""

from __future__ import annotations


class MusRegistryMixin:
    """Mixin for the ``known_mus`` table."""

    async def upsert_known_mu(
        self,
        mu_id: str,
        mu_name: str,
        updated_at: str,
        country_id: str | None = None,
        avatar_url: str | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO known_mus (mu_id, mu_name, updated_at, country_id, avatar_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(mu_id) DO UPDATE SET
                mu_name    = excluded.mu_name,
                updated_at = excluded.updated_at,
                country_id = COALESCE(excluded.country_id, known_mus.country_id),
                avatar_url = COALESCE(excluded.avatar_url, known_mus.avatar_url)
            """,
            (mu_id, mu_name, updated_at, country_id, avatar_url),
        )

    async def flush_known_mus(self) -> None:
        await self._conn.commit()

    async def get_known_mu_names(self, search: str = "") -> list[str]:
        if search:
            sql = (
                "SELECT mu_name FROM known_mus"
                " WHERE mu_name LIKE ? ORDER BY mu_name LIMIT 25"
            )
            params: tuple = (f"%{search}%",)
        else:
            sql = "SELECT mu_name FROM known_mus ORDER BY mu_name LIMIT 25"
            params = ()
        names: list[str] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                names.append(row[0])
        return names

    async def get_all_known_mu_ids(
        self, country_id: str | None = None
    ) -> list[tuple[str, str, str | None]]:
        """Return [(mu_id, mu_name, country_id)] for every row in known_mus.

        If *country_id* is given, only rows tagged with that country are returned.
        """
        rows: list[tuple[str, str, str | None]] = []
        if country_id is not None:
            sql = (
                "SELECT mu_id, mu_name, country_id FROM known_mus"
                " WHERE country_id = ? ORDER BY mu_name"
            )
            params: tuple = (country_id,)
        else:
            sql = "SELECT mu_id, mu_name, country_id FROM known_mus ORDER BY mu_name"
            params = ()
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append((row[0], row[1], row[2]))
        return rows

    async def get_known_mu_by_name(
        self, query: str
    ) -> tuple[str | None, str | None]:
        """Return (mu_id, mu_name) for the best name match, or (None, None)."""
        # Exact match first
        async with self._conn.execute(
            "SELECT mu_id, mu_name FROM known_mus WHERE lower(mu_name) = lower(?)",
            (query,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0], row[1]
        # Fuzzy match
        async with self._conn.execute(
            "SELECT mu_id, mu_name FROM known_mus"
            " WHERE lower(mu_name) LIKE lower(?) ORDER BY mu_name LIMIT 1",
            (f"%{query}%",),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0], row[1]
        return None, None
