"""/marktprijs — schat marktprijs van gear o.b.v. recente itemMarket trades.

Gebruiker geeft een itemCode + optioneel state/skills op; de command zoekt
vergelijkbare verkopen in ``item_trades`` (gevuld door de hourly trade_sync
task), rankt ze via weighted Euclidean distance en toont gemiddelde prijzen
over 24u / 7d / 30d + top-5 vergelijkbare trades.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import CommandCogBase
from services.db.trades import aggregate, rank_matches

logger = logging.getLogger("discord_bot")


def _unwrap(resp) -> dict | list:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _cutoff(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return "—"
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_coin_amount(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{_fmt_price(v)} c"


def _sell_advice(
    agg_24h: dict,
    agg_7d: dict,
    agg_30d: dict,
    *,
    any_stat_given: bool,
) -> str:
    """Return a compact asking-price recommendation for the embed."""
    candidates = (
        ("7 dagen", agg_7d),
        ("30 dagen", agg_30d),
        ("24 uur", agg_24h),
    )
    for label, agg in candidates:
        if agg.get("n", 0) >= 3:
            key = (
                "weighted"
                if any_stat_given and agg.get("weighted") is not None
                else "mean"
            )
            price = agg.get(key)
            if price is None:
                continue
            low = price * 0.9
            high = max(low, price * 1.1)
            return (
                f"Richtprijs: **{_fmt_coin_amount(price)}**\n"
                f"Snelle verkoop: **{_fmt_coin_amount(low)}** · "
                f"ambitieuze listing: **{_fmt_coin_amount(high)}**\n"
                f"Gebaseerd op {label} ({agg['n']} verkopen)."
            )
    return "Nog te weinig vergelijkbare verkopen voor een betrouwbaar advies."


def _fmt_stat_input(
    state: Optional[int],
    attack: Optional[int],
    critical_chance: Optional[int],
    critical_damages: Optional[int],
    armor: Optional[int],
    precision: Optional[int],
    dodge: Optional[int],
) -> str:
    parts = []
    if state is not None:
        parts.append(f"state={state}")
    if attack is not None:
        parts.append(f"atk={attack}")
    if critical_chance is not None:
        parts.append(f"cc={critical_chance}")
    if critical_damages is not None:
        parts.append(f"cd={critical_damages}")
    if armor is not None:
        parts.append(f"arm={armor}")
    if precision is not None:
        parts.append(f"prc={precision}")
    if dodge is not None:
        parts.append(f"dod={dodge}")
    return " ".join(parts) if parts else "(geen stats)"


def _short_ts(iso: str) -> str:
    """Convert an ISO timestamp to 'dd-mm HH:MM'."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d-%m %H:%M")
    except Exception:
        return iso[:16]


def _row_stats_compact(row: dict) -> str:
    parts = []
    for col, short in (
        ("state", "s"),
        ("attack", "atk"),
        ("critical_chance", "cc"),
        ("critical_damages", "cd"),
        ("armor", "arm"),
        ("precision_", "prc"),
        ("dodge", "dod"),
    ):
        v = row.get(col)
        if v is not None:
            parts.append(f"{short}={v}")
    return " ".join(parts) or "—"


def _listing_line(index: int, row: dict, dist: float, *, show_distance: bool) -> str:
    price = row["price"] / max(1, row.get("quantity") or 1)
    distance = f" d={dist:.3f}" if show_distance else ""
    return (
        f"{index:>3}. {_short_ts(row['created_at'])} — "
        f"{_fmt_coin_amount(price)}{distance} ({_row_stats_compact(row)})"
    )


class MarktprijsCog(CommandCogBase):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def _send_full_listings(
        self,
        interaction: discord.Interaction,
        item_code: str,
        ranked: list[tuple[dict, float]],
        *,
        show_distance: bool,
    ) -> None:
        if not ranked:
            return

        header = f"Alle gevonden listings voor {item_code} ({len(ranked)}):"
        chunks: list[list[str]] = []
        current: list[str] = []
        current_len = len(header) + 8
        for index, (row, dist) in enumerate(ranked, start=1):
            line = _listing_line(index, row, dist, show_distance=show_distance)
            line_len = len(line) + 1
            if current and current_len + line_len > 1900:
                chunks.append(current)
                current = []
                current_len = len(header) + 8
            current.append(line)
            current_len += line_len
        if current:
            chunks.append(current)

        for part, lines in enumerate(chunks, start=1):
            title = header if len(chunks) == 1 else f"{header} deel {part}/{len(chunks)}"
            await interaction.followup.send(
                f"{title}\n```text\n" + "\n".join(lines) + "\n```"
            )

    async def _refresh_item_trades(self, item_code: str) -> tuple[int, int, str | None]:
        """Fetch latest live itemMarket trades for this item and store new rows."""
        client = self._client
        db = self._db
        if client is None or db is None:
            return 0, 0, "live refresh overgeslagen"

        try:
            raw = await client.post(
                "/transaction.getPaginatedTransactions",
                json={
                    "limit": 100,
                    "itemCode": item_code,
                    "transactionType": "itemMarket",
                },
            )
            data = _unwrap(raw) if isinstance(raw, dict) else {}
            if isinstance(data, dict):
                items = (
                    data.get("items")
                    or data.get("transactions")
                    or data.get("results")
                    or []
                )
            elif isinstance(data, list):
                items = data
            else:
                items = []
            items = [
                tx
                for tx in items
                if isinstance(tx, dict) and tx.get("itemCode") == item_code
            ]
            inserted, seen = await db.upsert_trades(items)
            return inserted, seen, None
        except Exception as exc:
            logger.warning("marktprijs: live refresh failed for %s: %s", item_code, exc)
            return 0, 0, "live refresh mislukt"

    @app_commands.command(
        name="marktprijs",
        description="Schat marktprijs van een item o.b.v. recente itemMarket trades.",
    )
    @app_commands.describe(
        item_code="Item code (bv. boots4, sword3)",
        state="Durability (0-100)",
        attack="Attack stat",
        critical_chance="Critical chance stat",
        critical_damages="Critical damages stat",
        armor="Armor stat",
        precision="Precision stat",
        dodge="Dodge stat",
        full="Toon alle gevonden listings naast de samenvatting",
    )
    async def marktprijs(
        self,
        interaction: discord.Interaction,
        item_code: str,
        state: Optional[int] = None,
        attack: Optional[int] = None,
        critical_chance: Optional[int] = None,
        critical_damages: Optional[int] = None,
        armor: Optional[int] = None,
        precision: Optional[int] = None,
        dodge: Optional[int] = None,
        full: bool = False,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not self._db:
            await self._send_api_offline(interaction, "Database nog niet gereed.")
            return

        item_code = item_code.strip()
        inserted, seen, refresh_note = await self._refresh_item_trades(item_code)

        # 30-dagen window is onze buitengrens; smaller windows filteren we in-memory.
        since = _cutoff(24 * 30)
        try:
            rows = await self._db.fetch_trades_for_match(item_code, since_iso=since)
        except Exception:
            logger.exception("marktprijs: DB error")
            await self._send_api_offline(interaction, "DB-fout bij ophalen trades.")
            return

        query = {
            "state": state,
            "attack": attack,
            "critical_chance": critical_chance,
            "critical_damages": critical_damages,
            "armor": armor,
            "precision_": precision,
            "dodge": dodge,
        }

        any_stat_given = any(v is not None for v in query.values())
        if any_stat_given:
            ranked = rank_matches(query, rows, top_k=len(rows))
        else:
            # Zonder stats: behandel elk item als gelijk (distance 0) en gebruik
            # pure prijsstatistieken zodat de aggregate-functie nog werkt.
            ranked = [(r, 0.0) for r in rows]

        if len(rows) < 3:
            footer = f"Live refresh: {seen} gezien, {inserted} nieuw"
            if refresh_note:
                footer += f" • {refresh_note}"
            embed = discord.Embed(
                title=f"📉 Marktprijs: {item_code}",
                description=(
                    f"Te weinig data (n={len(rows)}) in de afgelopen 30 dagen.\n"
                    "De bot verzamelt per uur nieuwe trades — probeer later opnieuw."
                ),
                colour=self._embed_colour("warning"),
            )
            embed.set_footer(text=footer)
            await interaction.followup.send(embed=embed)
            if full:
                await self._send_full_listings(
                    interaction,
                    item_code,
                    ranked,
                    show_distance=any_stat_given,
                )
            return

        agg_24h = aggregate(ranked, _cutoff(24))
        agg_7d = aggregate(ranked, _cutoff(24 * 7))
        agg_30d = aggregate(ranked, _cutoff(24 * 30))

        embed = discord.Embed(
            title=f"💰 Marktprijs: {item_code}",
            description=_fmt_stat_input(
                state, attack, critical_chance, critical_damages,
                armor, precision, dodge,
            ),
            colour=self._embed_colour("primary"),
        )

        def _window_value(agg: dict) -> str:
            if not agg["n"]:
                return "—"
            weighted = (
                f"\ngewogen ≈ **{_fmt_price(agg['weighted'])}**"
                if any_stat_given and agg["weighted"] is not None
                else ""
            )
            return (
                f"n={agg['n']}\n"
                f"gem = {_fmt_price(agg['mean'])}\n"
                f"range = {_fmt_price(agg['min'])}..{_fmt_price(agg['max'])}"
                f"{weighted}"
            )

        embed.add_field(name="📅 24 uur", value=_window_value(agg_24h), inline=True)
        embed.add_field(name="📆 7 dagen", value=_window_value(agg_7d), inline=True)
        embed.add_field(name="🗓️ 30 dagen", value=_window_value(agg_30d), inline=True)
        embed.add_field(
            name="Advies",
            value=_sell_advice(
                agg_24h, agg_7d, agg_30d, any_stat_given=any_stat_given
            ),
            inline=False,
        )
        embed.add_field(
            name="Legenda",
            value=(
                "`c` = coins. `d` = afstand tot jouw opgegeven stats; "
                "lager betekent vergelijkbaarder."
            ),
            inline=False,
        )

        # Top 5 dichtstbijzijnde matches (alleen zinvol als er stats zijn opgegeven)
        if any_stat_given and ranked:
            top_lines: list[str] = []
            for row, dist in ranked[:5]:
                price = row["price"] / max(1, row.get("quantity") or 1)
                top_lines.append(
                    f"`{_short_ts(row['created_at'])}` — "
                    f"**{_fmt_coin_amount(price)}**  d={dist:.3f}  "
                    f"({_row_stats_compact(row)})"
                )
            embed.add_field(
                name="🔎 Top 5 vergelijkbare verkopen",
                value="\n".join(top_lines)[:1024],
                inline=False,
            )

        footer = (
            f"Bron: item_trades  •  {len(rows)} trades in 30 dagen voor {item_code}"
            f"  •  live refresh: {seen} gezien, {inserted} nieuw"
        )
        if refresh_note:
            footer += f"  •  {refresh_note}"
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)
        if full:
            await self._send_full_listings(
                interaction,
                item_code,
                ranked,
                show_distance=any_stat_given,
            )

    @marktprijs.autocomplete("item_code")
    async def _item_code_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not self._db:
            return []
        try:
            codes = await self._db.distinct_item_codes(prefix=current, limit=25)
        except Exception:
            return []
        return [app_commands.Choice(name=c, value=c) for c in codes]

    @app_commands.command(
        name="cleartradingrecords",
        description="Wis alle opgeslagen itemMarket trade records.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clear_trading_records(self, interaction: discord.Interaction) -> None:
        if not self._db:
            await self._send_api_offline(interaction, "Database nog niet gereed.")
            return

        try:
            deleted = await self._db.clear_item_trades()
        except Exception:
            logger.exception("cleartradingrecords: DB error")
            await self._send_api_offline(
                interaction,
                "DB-fout bij wissen van trading records.",
            )
            return

        embed = discord.Embed(
            title="Trading records gewist",
            description=f"Er zijn **{deleted}** itemMarket trade records verwijderd.",
            colour=self._embed_colour("success"),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(MarktprijsCog(bot))
