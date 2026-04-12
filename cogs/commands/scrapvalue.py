"""
Slash command /scrapvalue — shows the scrap value for each equipment rarity tier.

Fetches the live scraps market price and best buy/sell orders, then multiplies
by the number of scraps each rarity tier yields when scrapped.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

# Scraps yielded when scrapping one item of each rarity tier
SCRAP_YIELDS: dict[str, int] = {
    "Common": 6,
    "Uncommon": 18,
    "Rare": 54,
    "Epic": 162,
    "Legendary": 486,
    "Mythic": 1460,
}


def _unwrap(resp: object) -> object:
    if isinstance(resp, dict):
        result = resp.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if data is not None:
                return data
        return resp
    return resp


class ScrapvalueCog(CommandCogBase, name="scrapvalue"):
    """Cog for the /scrapvalue command."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="scrapvalue",
        description="Toon de scrapwaarde per uitrustingsniveau op basis van de actuele marktprijs.",
    )
    async def scrapvalue(self, ctx: Context) -> None:
        """Calculate scrap values for each equipment rarity using live market prices."""
        if not self._client:
            await self._send_api_offline(ctx)
            return
        if hasattr(ctx, "defer"):
            await ctx.defer()

        # --- Fetch market prices (contains scraps key) -----------------------
        try:
            prices_resp = await self._client.get("/itemTrading.getPrices")
        except Exception:
            logger.exception("scrapvalue: failed to fetch item prices")
            await self._send_api_offline(ctx)
            return

        prices_data = _unwrap(prices_resp)
        if not isinstance(prices_data, dict):
            await ctx.send("Onverwacht formaat van marktprijzen.")
            return

        try:
            market_price = float(prices_data["scraps"])
        except (KeyError, TypeError, ValueError):
            await ctx.send("Scrapprijs niet gevonden in marktdata.")
            return

        # --- Fetch top orders for scraps -------------------------------------
        try:
            orders_resp = await self._client.post(
                "/tradingOrder.getTopOrders",
                json={"itemCode": "scraps", "limit": 1},
            )
        except Exception:
            logger.exception("scrapvalue: failed to fetch top orders")
            await self._send_api_offline(ctx)
            return

        orders_data = _unwrap(orders_resp)
        if not isinstance(orders_data, dict):
            await ctx.send("Onverwacht formaat van orderdata.")
            return

        buy_orders: list[dict] = orders_data.get("buyOrders") or []
        sell_orders: list[dict] = orders_data.get("sellOrders") or []

        best_buy: float | None = None
        best_sell: float | None = None

        if buy_orders:
            try:
                best_buy = float(buy_orders[0]["price"])
            except (KeyError, TypeError, ValueError):
                pass
        if sell_orders:
            try:
                best_sell = float(sell_orders[0]["price"])
            except (KeyError, TypeError, ValueError):
                pass

        # --- Build table -------------------------------------------------------
        col_rarity = 10
        col_scraps = 6
        col_val = 10

        def _fmt(val: float) -> str:
            return f"{val:,.2f} CC"

        def _col(val: float | None, scraps: int) -> str:
            if val is None:
                return "—"
            return _fmt(val * scraps)

        header = (
            f"{'Rarity':<{col_rarity}}  {'Scraps':>{col_scraps}}"
            f"  {'Marktprijs':>{col_val}}"
            f"  {'Buy':>{col_val}}"
            f"  {'Sell':>{col_val}}"
        )
        sep = "─" * len(header)

        rows = [header, sep]
        for rarity, scraps in SCRAP_YIELDS.items():
            market_val = _fmt(market_price * scraps)
            bod_val = _col(best_buy, scraps)
            vraag_val = _col(best_sell, scraps)
            rows.append(
                f"{rarity:<{col_rarity}}  {scraps:>{col_scraps}}"
                f"  {market_val:>{col_val}}"
                f"  {bod_val:>{col_val}}"
                f"  {vraag_val:>{col_val}}"
            )

        table = "\n".join(rows)

        # --- Build spread footer line ----------------------------------------
        spread_parts = [f"Marktprijs: **{market_price:.4f} CC**/scrap"]
        if best_buy is not None:
            spread_parts.append(f"Beste bod: **{best_buy:.4f} CC**")
        if best_sell is not None:
            spread_parts.append(f"Laagste vraag: **{best_sell:.4f} CC**")
        spread_line = "  •  ".join(spread_parts)

        # --- Send embed -------------------------------------------------------
        embed = discord.Embed(
            title="Scrapwaarde per uitrustingsniveau",
            colour=self._embed_colour(),
            timestamp=datetime.now(tz=timezone.utc),
        )
        embed.description = spread_line + f"\n```\n{table}\n```"
        embed.set_footer(text="Waarden = aantal scraps × prijs per scrap")

        await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(ScrapvalueCog(bot))
