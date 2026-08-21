"""Slash command /nederlandse-mus — every MU in the game owned by a Dutch citizen.

"Dutch MU" means the MU's *owner* currently holds Dutch citizenship — not the
MU's own ``country`` tag (which is set once and can go stale if the owner
later switches nationality). So this walks the full, live MU list rather
than relying on any pre-registered/template list (unlike /muplek, which only
covers the known division MUs from templates/mus.json).

Data comes straight from the live API, no DB cache involved:
  1. ``mu.getManyPaginated`` (limit=100, paginated) — every MU in the game.
     Confirmed live: each item already carries the full MU object, including
     ``user`` (owner ID) and ``members`` (full member ID list), so no
     ``mu.getById`` follow-up is needed per MU.
  2. ``user.getUserById``, batched, for every distinct owner ID — to read
     their current ``country``, ``username`` and ``isActive`` status.
  3. ``upgrade.getUpgradeByTypeAndEntity`` (params: ``upgradeType``,
     ``muId``), batched, headquarters + dormitories for every *Dutch* MU
     only (cheap — there are far fewer of those than MUs overall). This is
     the actual level, regardless of whether the upgrade is currently
     active — the MU object's own ``activeUpgradeLevels`` field silently
     omits any upgrade whose ``status`` isn't ``"active"`` (e.g. disabled
     for unpaid upkeep), which made a built level-1 headquarters render as
     unknown instead of 1. Confirmed live against MU 69d25fc3ecdc50cc8b25a434:
     ``activeUpgradeLevels`` has no ``headquarters`` key at all, but
     ``upgrade.getUpgradeByTypeAndEntity`` returns
     ``{"level": 1, "status": "disabled"}`` for it.
"""

from __future__ import annotations

import json
import logging

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

_MU_PAGE_LIMIT = 100
_USER_BATCH = 100
_UPGRADE_BATCH = 100
_UPGRADE_TYPES = ("headquarters", "dormitories")


def _unwrap_trpc(resp: object) -> object:
    """Unwrap a trpc {"result":{"data":...}} envelope (one or two levels)."""
    data = resp
    if isinstance(data, dict):
        for key in ("result", "data"):
            v = data.get(key)
            if isinstance(v, dict):
                data = v.get("data", v)
                break
    return data


class DutchMusCog(CommandCogBase, name="dutch_mus"):
    """Cog for the /nederlandse-mus command."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _fetch_all_mus(self) -> list[dict]:
        """Return every MU in the game as {mu_id, name, owner_id, member_count}."""
        mus: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict = {"limit": _MU_PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            raw = await self._client.get(
                "/mu.getManyPaginated", params={"input": json.dumps(params)}
            )
            data = _unwrap_trpc(raw)
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            if not isinstance(items, list):
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                mu_id = str(item.get("_id") or item.get("id") or "").strip()
                owner_id = str(item.get("user") or "").strip()
                if not mu_id or not owner_id:
                    continue
                members = item.get("members")
                mus.append({
                    "mu_id": mu_id,
                    "name": str(item.get("name") or mu_id),
                    "owner_id": owner_id,
                    "member_count": len(members) if isinstance(members, list) else 0,
                })
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not items:
                break
        return mus

    async def _fetch_upgrade_levels(self, mu_ids: list[str]) -> dict[tuple[str, str], int]:
        """Return {(mu_id, upgrade_type): level} for every *mu_ids* × _UPGRADE_TYPES.

        Uses the actual level regardless of ``status`` (active/disabled) —
        see the module docstring for why ``activeUpgradeLevels`` alone isn't
        enough.
        """
        inputs = [
            {"upgradeType": utype, "muId": mu_id}
            for mu_id in mu_ids
            for utype in _UPGRADE_TYPES
        ]
        if not inputs:
            return {}

        levels: dict[tuple[str, str], int] = {}
        for start in range(0, len(inputs), _UPGRADE_BATCH):
            chunk = inputs[start : start + _UPGRADE_BATCH]
            results = await self._client.batch_get(
                "/upgrade.getUpgradeByTypeAndEntity",
                chunk,
                batch_size=_UPGRADE_BATCH,
            )
            for inp, raw in zip(chunk, results):
                data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
                if not isinstance(data, dict):
                    continue
                level = data.get("level")
                if level is None:
                    continue
                try:
                    levels[(inp["muId"], inp["upgradeType"])] = int(level)
                except (TypeError, ValueError):
                    continue
        return levels

    @commands.hybrid_command(
        name="nederlandse-mus",
        description="Toon alle MU's in de game waarvan de eigenaar Nederlander is.",
    )
    async def nederlandse_mus(self, ctx: Context) -> None:
        """/nederlandse-mus — alle MU's met een Nederlandse eigenaar, met ledenaantal."""
        if not self._db or not self._client:
            await ctx.send("Database of API-client nog niet gereed.")
            return

        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            await ctx.send("❌ `nl_country_id` niet geconfigureerd.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        try:
            all_mus = await self._fetch_all_mus()
        except Exception as exc:
            await ctx.send(f"❌ Kon MU-lijst niet ophalen: {exc}")
            return

        if not all_mus:
            await ctx.send("Geen MU's gevonden.")
            return

        owner_ids = sorted({mu["owner_id"] for mu in all_mus})
        owner_country: dict[str, str] = {}
        owner_username: dict[str, str] = {}
        owner_active: dict[str, bool] = {}
        for start in range(0, len(owner_ids), _USER_BATCH):
            chunk = owner_ids[start : start + _USER_BATCH]
            results = await self._client.batch_get(
                "/user.getUserById",
                [{"userId": uid} for uid in chunk],
                batch_size=_USER_BATCH,
            )
            for uid, raw in zip(chunk, results):
                user = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
                if not isinstance(user, dict):
                    continue
                if user.get("country"):
                    owner_country[uid] = str(user["country"])
                if user.get("username"):
                    owner_username[uid] = str(user["username"])
                if "isActive" in user:
                    owner_active[uid] = bool(user["isActive"])

        dutch_mus = [
            mu for mu in all_mus if owner_country.get(mu["owner_id"]) == nl_country_id
        ]
        dutch_mus.sort(key=lambda mu: mu["member_count"], reverse=True)

        if not dutch_mus:
            await ctx.send(
                f"Geen MU's met een Nederlandse eigenaar gevonden "
                f"({len(all_mus)} MU's gecheckt, {len(owner_ids)} eigenaren gecontroleerd)."
            )
            return

        try:
            upgrade_levels = await self._fetch_upgrade_levels(
                [mu["mu_id"] for mu in dutch_mus]
            )
        except Exception:
            logger.exception("nederlandse_mus: failed to fetch upgrade levels")
            upgrade_levels = {}
        for mu in dutch_mus:
            mu["hq_level"] = upgrade_levels.get((mu["mu_id"], "headquarters"))
            mu["dorm_level"] = upgrade_levels.get((mu["mu_id"], "dormitories"))

        # Fixed-width monospace table so nothing wraps, same style as /muplek.
        # Single-space gaps between columns to keep each row as narrow as
        # possible — a wide row is what was wrapping on narrower clients.
        name_w = min(max((len(mu["name"]) for mu in dutch_mus), default=4), 18)
        owner_w = min(
            max((len(owner_username.get(mu["owner_id"], mu["owner_id"])) for mu in dutch_mus), default=7),
            14,
        )
        header = (
            f"{'MU':<{name_w}} {'Eigenaar':<{owner_w}} {'Act':<3} "
            f"{'Led':>3} {'HQ':>2} {'Slp':>3}"
        )
        sep = "─" * len(header)

        def _row(mu: dict) -> str:
            active = owner_active.get(mu["owner_id"])
            active_str = "Ja" if active else ("Nee" if active is False else "?")
            owner_name = owner_username.get(mu["owner_id"], mu["owner_id"])
            hq = mu["hq_level"] if mu["hq_level"] is not None else "?"
            dorm = mu["dorm_level"] if mu["dorm_level"] is not None else "?"
            return (
                f"{mu['name'][:name_w]:<{name_w}} {owner_name[:owner_w]:<{owner_w}} "
                f"{active_str:<3} {mu['member_count']:>3} {hq!s:>2} {dorm!s:>3}"
            )

        rows = [_row(mu) for mu in dutch_mus]

        # Chunk rows so each code block stays under the embed description limit.
        embed_limit = 3900
        overhead = len("```\n") + len(header) + 1 + len(sep) + 1 + len("\n```")
        chunks: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for row in rows:
            row_len = len(row) + 1
            if current and overhead + current_len + row_len > embed_limit:
                chunks.append(current)
                current, current_len = [], 0
            current.append(row)
            current_len += row_len
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            block = "```\n" + header + "\n" + sep + "\n" + "\n".join(chunk) + "\n```"
            embed = discord.Embed(
                title=(
                    f"🇳🇱 Nederlandse MU's ({len(dutch_mus)})"
                    if i == 0
                    else f"🇳🇱 Nederlandse MU's (vervolg {i + 1}/{len(chunks)})"
                ),
                description=block,
                colour=self._embed_colour(),
            )
            if i == 0:
                embed.set_footer(
                    text=f"{len(all_mus)} MU's gecheckt · {len(owner_ids)} eigenaren gecontroleerd"
                )
            await ctx.send(embed=embed)


async def setup(bot) -> None:
    """Add the DutchMusCog to the bot."""
    await bot.add_cog(DutchMusCog(bot))
