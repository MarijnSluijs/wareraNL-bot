"""
This module defines the BonusCog, which provides commands related to production bonuses in the WarEraNL bot.
- /bonus
- /topbonus
- /verhuiskosten [current_bonus target_bonus]
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")


class BonusCog(CommandCogBase, name="bonus"):
    """Production bonus commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # /bonus                                                               #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="bonus", description="Toon productieleiders voor elk item."
    )
    async def bonus(self, ctx: Context):
        """Display the current production leaders for each specialization."""
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return
        if hasattr(ctx, "defer"):
            await ctx.defer()
        try:
            tops = await self._db.get_all_tops()
        except Exception:
            logger.exception("Failed to fetch production leaders")
            await ctx.send("Ophalen van productieleiders mislukt; zie logs.")
            return

        deposit_tops: list[dict] = []
        try:
            deposit_tops = await self._db.get_all_deposit_tops()
        except Exception:
            pass

        if not tops and not deposit_tops:
            await ctx.send("Geen productieleiders opgeslagen.")
            return

        prices: dict[str, float] = {}
        pp_costs: dict[str, float] = {}   # item_code -> PP needed per unit
        pp_needs: dict[str, dict[str, float]] = {}  # item_code -> {mat: qty}
        if self._client:
            try:
                prices_resp = await self._client.get("/itemTrading.getPrices")
                prices = self._unwrap_prices(prices_resp)
            except Exception:
                pass
            try:
                cfg_resp = await self._client.get("/gameConfig.getGameConfig", params={"input": "{}"})
                cfg_data: dict = {}
                if isinstance(cfg_resp, dict):
                    _inner = (cfg_resp.get("result") or {}).get("data")
                    cfg_data = _inner if isinstance(_inner, dict) else cfg_resp
                items_cfg = cfg_data.get("items", {}) if isinstance(cfg_data, dict) else {}
                for code, info in items_cfg.items():
                    if isinstance(info, dict):
                        try:
                            pp = float(info.get("productionPoints") or 1)
                            pp_costs[code] = pp if pp > 0 else 1.0
                        except (TypeError, ValueError):
                            pass
                        raw_needs = info.get("productionNeeds") or {}
                        if isinstance(raw_needs, dict) and raw_needs:
                            pp_needs[code] = {
                                mat: float(qty) for mat, qty in raw_needs.items()
                            }
            except Exception:
                pass

        dep_by_item = {d.get("item"): d for d in deposit_tops}
        top_by_item = {t.get("item"): t for t in tops}
        all_items = sorted(set(top_by_item) | set(dep_by_item))

        # Fetch highest buy order and lowest sell order for each item in parallel
        buy_prices: dict[str, float] = {}
        sell_prices: dict[str, float] = {}
        if self._client and all_items:
            async def _fetch_order(code: str) -> tuple[str, float, float]:
                try:
                    resp = await self._client.post(
                        "/tradingOrder.getTopOrders",
                        json={"itemCode": code, "limit": 1},
                    )
                    b, s = self._parse_order_prices(resp)
                    return code, b, s
                except Exception:
                    return code, 0.0, 0.0
            _order_results = await asyncio.gather(*[_fetch_order(c) for c in all_items])
            for _code, _buy, _sell in _order_results:
                if _buy > 0:
                    buy_prices[_code] = _buy
                if _sell > 0:
                    sell_prices[_code] = _sell
        # Fall back to getPrices for items missing from the order book
        for code in all_items:
            if code not in buy_prices and code in prices:
                buy_prices[code] = prices[code]
            if code not in sell_prices and code in prices:
                sell_prices[code] = prices[code]

        def _fmt_price(p: float) -> str:
            if p <= 0:
                return "?"
            return f"{p:.4f}" if p < 1 else f"{p:.2f}"

        def _per_pp(price: float, item: str) -> float:
            """Convert price-per-item to price-per-total-PP invested.

            Total PP = direct productionPoints + sum(input_qty × input_PP)
            e.g. steel: 10 PP direct + 10 iron × 1 PP = 20 PP total.
            """
            direct = pp_costs.get(item, 0.0)
            material_pp = sum(
                qty * pp_costs.get(mat, 1.0)
                for mat, qty in pp_needs.get(item, {}).items()
            )
            total = direct + material_pp
            if price <= 0 or total <= 0:
                return 0.0
            return price / total

        long_rows = [
            (item, top_by_item[item]) for item in all_items if item in top_by_item
        ]
        # Exclude expired deposit bonuses — they no longer apply
        short_rows = [
            (item, dep_by_item[item])
            for item in all_items
            if item in dep_by_item
            and self._format_duration(dep_by_item[item].get("deposit_end_at") or "") != "verlopen"
        ]

        best_l_idx: int | None = None
        best_s_idx: int | None = None
        best_l_maxpp_idx: int | None = None
        best_s_maxpp_idx: int | None = None
        l_max_vals: list[float] = []
        s_max_vals: list[float] = []

        colour = self._embed_colour()

        _WI = 10   # max item name display chars
        _WC = 7    # max country/region display chars

        if long_rows:
            l_bonus_pcts  = [float(t.get("production_bonus") or 0) for _, t in long_rows]
            l_buy_pp      = [_per_pp(buy_prices.get(item, 0.0), item) for item, _ in long_rows]
            l_sell_pp     = [_per_pp(sell_prices.get(item, 0.0), item) for item, _ in long_rows]
            l_max_buy_pp  = [
                _per_pp(buy_prices.get(item, 0.0) * (1 + b / 100), item)
                if buy_prices.get(item, 0.0) > 0 else 0.0
                for (item, _), b in zip(long_rows, l_bonus_pcts)
            ]
            l_max_sell_pp = [
                _per_pp(sell_prices.get(item, 0.0) * (1 + b / 100), item)
                if sell_prices.get(item, 0.0) > 0 else 0.0
                for (item, _), b in zip(long_rows, l_bonus_pcts)
            ]
            # Sort by MK/PP (max buy-order per PP) descending
            _l_zipped = sorted(
                zip(long_rows, l_bonus_pcts, l_buy_pp, l_sell_pp, l_max_buy_pp, l_max_sell_pp),
                key=lambda x: x[4], reverse=True,
            )
            long_rows     = [x[0] for x in _l_zipped]
            l_bonus_pcts  = [x[1] for x in _l_zipped]
            l_buy_pp      = [x[2] for x in _l_zipped]
            l_sell_pp     = [x[3] for x in _l_zipped]
            l_max_buy_pp  = [x[4] for x in _l_zipped]
            l_max_sell_pp = [x[5] for x in _l_zipped]
            l_max_vals    = l_max_buy_pp
            wb = max(max(len(self._pct(t.get("production_bonus"))) for _, t in long_rows), 5)
            bpp_strs  = [_fmt_price(v) for v in l_buy_pp]
            spp_strs  = [_fmt_price(v) for v in l_sell_pp]
            mbpp_strs = [_fmt_price(v) for v in l_max_buy_pp]
            mspp_strs = [_fmt_price(v) for v in l_max_sell_pp]
            wkp  = max(max(len(s) for s in bpp_strs),  4)
            wvp  = max(max(len(s) for s in spp_strs),  4)
            wmk  = max(max(len(s) for s in mbpp_strs), len("MK/PP▲"))
            wmv  = max(max(len(s) for s in mspp_strs), 5)
            best_l_idx = max(range(len(long_rows)), key=lambda i: float(long_rows[i][1].get("production_bonus") or 0))
            best_l_maxpp_idx = 0
            hdr_l = (
                f"  {'Item':<{_WI}}  {'Land':<{_WC}}  {'Bonus':>{wb}}"
                f"  {'K/PP':>{wkp}}  {'V/PP':>{wvp}}  {'MK/PP▲':>{wmk}}  {'MV/PP':>{wmv}}"
            )
            sep_l = "  " + "-" * (len(hdr_l) - 2)
            rows_l = [
                f"  {item[:_WI]:<{_WI}}  {(t.get('country_name') or 'Onbekend')[:_WC]:<{_WC}}"
                f"  {self._pct(t.get('production_bonus')):>{wb}}"
                f"  {bpp_strs[i]:>{wkp}}  {spp_strs[i]:>{wvp}}  {mbpp_strs[i]:>{wmk}}  {mspp_strs[i]:>{wmv}}"
                for i, (item, t) in enumerate(long_rows)
            ]
            table_l = "\n".join([hdr_l, sep_l] + rows_l)
        else:
            table_l = "(geen)"

        if short_rows:
            wb2 = max(max(len(self._pct(d.get("bonus"))) for _, d in short_rows), 5)
            durs = [
                self._format_duration(d.get("deposit_end_at") or "") or ""
                for _, d in short_rows
            ]
            wdur = max(max(len(dur) for dur in durs), 7)
            s_bonus_pcts  = [float(d.get("bonus") or 0) for _, d in short_rows]
            s_buy_pp      = [_per_pp(buy_prices.get(item, 0.0), item) for item, _ in short_rows]
            s_sell_pp     = [_per_pp(sell_prices.get(item, 0.0), item) for item, _ in short_rows]
            s_max_buy_pp  = [
                _per_pp(buy_prices.get(item, 0.0) * (1 + b / 100), item)
                if buy_prices.get(item, 0.0) > 0 else 0.0
                for (item, _), b in zip(short_rows, s_bonus_pcts)
            ]
            s_max_sell_pp = [
                _per_pp(sell_prices.get(item, 0.0) * (1 + b / 100), item)
                if sell_prices.get(item, 0.0) > 0 else 0.0
                for (item, _), b in zip(short_rows, s_bonus_pcts)
            ]
            # Sort by MK/PP (max buy-order per PP) descending
            _s_zipped = sorted(
                zip(short_rows, s_bonus_pcts, s_buy_pp, s_sell_pp, s_max_buy_pp, s_max_sell_pp, durs),
                key=lambda x: x[4], reverse=True,
            )
            short_rows    = [x[0] for x in _s_zipped]
            s_bonus_pcts  = [x[1] for x in _s_zipped]
            s_buy_pp      = [x[2] for x in _s_zipped]
            s_sell_pp     = [x[3] for x in _s_zipped]
            s_max_buy_pp  = [x[4] for x in _s_zipped]
            s_max_sell_pp = [x[5] for x in _s_zipped]
            durs          = [x[6] for x in _s_zipped]
            s_max_vals    = s_max_buy_pp
            bpp_strs2  = [_fmt_price(v) for v in s_buy_pp]
            spp_strs2  = [_fmt_price(v) for v in s_sell_pp]
            mbpp_strs2 = [_fmt_price(v) for v in s_max_buy_pp]
            mspp_strs2 = [_fmt_price(v) for v in s_max_sell_pp]
            wkp2  = max(max(len(s) for s in bpp_strs2),  4)
            wvp2  = max(max(len(s) for s in spp_strs2),  4)
            wmk2  = max(max(len(s) for s in mbpp_strs2), len("MK/PP▲"))
            wmv2  = max(max(len(s) for s in mspp_strs2), 5)
            best_s_idx = max(range(len(short_rows)), key=lambda i: float(short_rows[i][1].get("bonus") or 0))
            best_s_maxpp_idx = 0
            hdr_s = (
                f"  {'Item':<{_WI}}  {'Regio':<{_WC}}  {'Bonus':>{wb2}}  {'Verloopt':<{wdur}}"
                f"  {'K/PP':>{wkp2}}  {'V/PP':>{wvp2}}  {'MK/PP▲':>{wmk2}}  {'MV/PP':>{wmv2}}"
            )
            sep_s = "  " + "-" * (len(hdr_s) - 2)
            rows_s = [
                f"  {item[:_WI]:<{_WI}}  {(d.get('region_name') or d.get('region_id') or '?')[:_WC]:<{_WC}}"
                f"  {self._pct(d.get('bonus')):>{wb2}}  {dur:<{wdur}}"
                f"  {bpp_strs2[i]:>{wkp2}}  {spp_strs2[i]:>{wvp2}}  {mbpp_strs2[i]:>{wmk2}}  {mspp_strs2[i]:>{wmv2}}"
                for i, ((item, d), dur) in enumerate(zip(short_rows, durs))
            ]
            table_s = "\n".join([hdr_s, sep_s] + rows_s)
        else:
            table_s = "(geen)"

        MSG_LIMIT = 1900

        async def _send_table(title: str, table_text: str) -> None:
            lines = table_text.splitlines()
            header_lines = lines[:2]
            data_lines = lines[2:]
            chunks: list[list[str]] = []
            chunk: list[str] = []
            for line in data_lines:
                body = "\n".join(header_lines + chunk + [line])
                if len(f"**{title}**\n```\n{body}\n```") > MSG_LIMIT and chunk:
                    chunks.append(chunk)
                    chunk = [line]
                else:
                    chunk.append(line)
            if chunk:
                chunks.append(chunk)
            for idx, ch in enumerate(chunks):
                chunk_title = title if idx == 0 else f"{title} (vervolg)"
                block = (
                    f"**{chunk_title}**\n```\n" + "\n".join(header_lines + ch) + "\n```"
                )
                await ctx.send(block)

        await _send_table("📈 Langetermijnleiders", table_l)
        await _send_table("⚡ Kortetermijnleiders", table_s)

        best_embed = discord.Embed(colour=colour)
        if best_l_idx is not None:
            bl_item, bl = long_rows[best_l_idx]
            best_embed.add_field(
                name="🏆 Hoogste langetermijn",
                value=f"**{bl_item}** — {bl.get('country_name')} **{bl.get('production_bonus')}%**",
                inline=False,
            )
        if best_s_idx is not None:
            bs_item, bs = short_rows[best_s_idx]
            rl = bs.get("region_name") or bs.get("region_id") or "?"
            dur = self._format_duration(bs.get("deposit_end_at") or "")
            best_embed.add_field(
                name="⚡ Hoogste kortetermijn",
                value=(
                    f"**{bs_item}** — {rl} **{bs.get('bonus')}%**"
                    + (f"  ⏳ {dur}" if dur else "")
                ),
                inline=False,
            )
        # Best Max/PP across both tables
        l_best_maxpp = l_max_vals[best_l_maxpp_idx] if best_l_maxpp_idx is not None and l_max_vals else -1.0
        s_best_maxpp = s_max_vals[best_s_maxpp_idx] if best_s_maxpp_idx is not None and s_max_vals else -1.0
        if l_best_maxpp > 0 or s_best_maxpp > 0:
            if l_best_maxpp >= s_best_maxpp and best_l_maxpp_idx is not None:
                bm_item, bm = long_rows[best_l_maxpp_idx]
                bm_val = _fmt_price(l_best_maxpp)
                bm_value = f"**{bm_item}** — {bm.get('country_name')} **{bm.get('production_bonus')}%**  |  Max/PP: **{bm_val}**"
            elif best_s_maxpp_idx is not None:
                bm_item, bm = short_rows[best_s_maxpp_idx]
                rl2 = bm.get("region_name") or bm.get("region_id") or "?"
                bm_dur = self._format_duration(bm.get("deposit_end_at") or "")
                bm_val = _fmt_price(s_best_maxpp)
                bm_value = (
                    f"**{bm_item}** — {rl2} **{bm.get('bonus')}%**"
                    + (f"  ⏳ {bm_dur}" if bm_dur else "")
                    + f"  |  Max/PP: **{bm_val}**"
                )
            else:
                bm_value = None
            if bm_value:
                best_embed.add_field(
                    name="💰 Beste Max/PP",
                    value=bm_value,
                    inline=False,
                )
        if best_embed.fields:
            await ctx.send(embed=best_embed)

        legend_parts = [
            "**K/PP** = hoogste kooporder / PP",
            "**V/PP** = laagste verkooporder / PP",
            "**MK/PP** = K/PP × max bonus",
            "**MV/PP** = V/PP × max bonus",
        ]
        if not self._client:
            legend_parts.append(
                "⚠️ Marktprijzen niet beschikbaar (API offline)"
                " — K/PP, V/PP, MK/PP en MV/PP staan op ?"
            )
        all_db_timestamps = [
            r.get("updated_at") for r in tops + deposit_tops if r.get("updated_at")
        ]
        if all_db_timestamps:
            latest_ts = max(all_db_timestamps)
            try:
                _dt = datetime.fromisoformat(latest_ts)
                ts_str = _dt.strftime("%d-%m-%Y %H:%M")
            except Exception:
                ts_str = (latest_ts or "")[:16].replace("T", " ") or "onbekend"
            legend_parts.append(f"-# Productiedata bijgewerkt: {ts_str} UTC")
        await ctx.send("_" + "\n".join(legend_parts) + "_")

    # ------------------------------------------------------------------ #
    # /topbonus                                                            #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="topbonus", description="Toon de beste langetermijn- en kortetermijnbonus."
    )
    async def topbonus(self, ctx: Context):
        """Show the single best long-term and best short-term production bonus."""
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return
        if hasattr(ctx, "defer"):
            await ctx.defer()
        tops: list[dict] = []
        deposit_tops: list[dict] = []
        try:
            tops = await self._db.get_all_tops()
            deposit_tops = await self._db.get_all_deposit_tops()
        except Exception:
            logger.exception("Failed to fetch production data")
            await ctx.send("Ophalen van productiedata mislukt; zie logs.")
            return

        if not tops and not deposit_tops:
            await ctx.send("Nog geen productiedata opgeslagen.")
            return

        colour = self._embed_colour()
        embed = discord.Embed(title="Hoogste Productiebonussen", colour=colour)

        if tops:
            bl = max(tops, key=lambda t: float(t.get("production_bonus") or 0))
            bd = self._long_bd(bl)
            embed.add_field(
                name="🏆 Hoogste langetermijn",
                value=(
                    f"**{bl.get('item')}** — {bl.get('country_name')} **{bl.get('production_bonus')}%**"
                    + (f"\n*{bd}*" if bd else "")
                ),
                inline=False,
            )
        if deposit_tops:
            bs = max(deposit_tops, key=lambda d: float(d.get("bonus") or 0))
            rl = bs.get("region_name") or bs.get("region_id") or "?"
            dur = self._format_duration(bs.get("deposit_end_at") or "")
            bd = self._short_bd(bs)
            embed.add_field(
                name="⚡ Hoogste kortetermijn",
                value=(
                    f"**{bs.get('item')}** — {rl} **{bs.get('bonus')}%**"
                    + (f"  ⏳ {dur}" if dur else "")
                    + (f"\n*{bd}*" if bd else "")
                ),
                inline=False,
            )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # /verhuiskosten                                                        #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="verhuiskosten",
        description="Toon het break-evenpunt om verhuiskosten van een bedrijf terug te verdienen.",
    )
    @app_commands.describe(
        bonuses='Optioneel: huidige bonus, of "huidig nieuw" (bijv. "30" of "30 55"). Leeg laten voor volledige tabel.',
    )
    async def verhuiskosten(self, ctx: Context, bonuses: str = ""):
        """Break-even table: hours of Automated Engine production to recover the 5-concrete move cost."""
        parts = bonuses.split()
        bonus: float = 0.0
        new_bonus: float | None = None
        try:
            if len(parts) >= 1:
                bonus = float(parts[0])
            if len(parts) >= 2:
                new_bonus = float(parts[1])
        except ValueError:
            await ctx.send(
                "Ongeldige invoer. Gebruik `/verhuiskosten`, `/verhuiskosten 30`, of `/verhuiskosten 30 55`."
            )
            return
        if not self._client:
            await self._send_api_offline(ctx)
            return
        if hasattr(ctx, "defer"):
            await ctx.defer()

        try:
            prices_resp = await self._client.get("/itemTrading.getPrices")
        except Exception:
            await self._send_api_offline(ctx)
            return

        prices = self._unwrap_prices(prices_resp)
        if not prices:
            await ctx.send("Kon marktprijzen niet verwerken vanuit API-antwoord.")
            return

        concrete_price = float(prices.get("concrete") or prices.get("Concrete") or 0)
        if concrete_price <= 0:
            await ctx.send("Betonprijs niet gevonden of nul in marktdata.")
            return
        move_cost = 5.0 * concrete_price

        pp_items = ["grain", "lead", "iron", "limestone"]
        pp_prices = [
            float(prices[k]) for k in pp_items if prices.get(k) and float(prices[k]) > 0
        ]
        if not pp_prices:
            await ctx.send(
                "Kon niet genoeg artikelprijzen ophalen voor PP-waardeberekening."
            )
            return
        avg_pp_value = sum(pp_prices) / len(pp_prices)

        colour = self._embed_colour()

        def _fmt_h(h: float) -> str:
            total_h = round(h)
            if total_h < 24:
                return f"{total_h}h"
            d, rem = divmod(total_h, 24)
            if d >= 10:
                return f"{d}d"
            return f"{d}d{rem}h"

        G = "\u001b[32m"
        Y = "\u001b[33m"
        R = "\u001b[31m"
        RESET = "\u001b[0m"

        def _col(h: float) -> str:
            return G if h <= 72 else (Y if h <= 120 else R)

        levels = list(range(1, 8))
        CELL = 5

        if new_bonus is not None:
            bonus_gain = new_bonus - bonus
            assumption = f"Verhuizing van **{bonus:g}%** → **{new_bonus:g}%** (winst: **+{bonus_gain:g}%**)"
            if bonus_gain <= 0:
                embed = discord.Embed(
                    title="Break-evenpunt — bedrijfsverhuizing",
                    description=(
                        f"{assumption}\n\n"
                        "De nieuwe bonus is niet hoger dan je huidige bonus — verhuizing levert geen winst op."
                    ),
                    colour=colour,
                )
            else:
                level_lines = []
                for lv in levels:
                    extra_per_hour = lv * (bonus_gain / 100) * avg_pp_value
                    h = move_cost / extra_per_hour
                    level_lines.append(f"Niveau {lv}: **{_fmt_h(h)}**")
                embed = discord.Embed(
                    title="Break-evenpunt — bedrijfsverhuizing",
                    description=(
                        "Automated Engine productietijd om de verhuiskosten terug te verdienen.\n"
                        f"{assumption}\n\n"
                        + "\n".join(level_lines)
                        + f"\n\n**Verhuiskosten:** 5 × {concrete_price:.2f} = **{move_cost:.2f} coins**\n"
                        f"**Gemiddelde PP-waarde:** {avg_pp_value:.4f} coins/pp"
                    ),
                    colour=colour,
                )
            await ctx.send(embed=embed)
            return

        # Full table
        all_bonuses = list(range(5, 85, 5))

        level_cols_width = 6 * len(levels)
        eng_label = "Automated Engine Level"
        pad_left = max(0, (level_cols_width - len(eng_label)) // 2)
        eng_header = " " * 7 + " " * pad_left + eng_label

        hdr = f"{'Bonus':>5} │" + "".join(f" {'Lv' + str(lv):<{CELL}}" for lv in levels)
        sep = "──────┼" + "─" * (6 * len(levels))

        rows = []
        for b in all_bonuses:
            bonus_gain = b - bonus
            cells = []
            for lv in levels:
                if bonus_gain <= 0:
                    cells.append(f"{R}{'∞':>{CELL}}{RESET}")
                else:
                    extra_per_hour = lv * (bonus_gain / 100) * avg_pp_value
                    h = move_cost / extra_per_hour
                    cells.append(f"{_col(h)}{_fmt_h(h):>{CELL}}{RESET}")
            rows.append(f" {b:>3}% │" + "".join(f" {c}" for c in cells))

        table = (
            "```ansi\n"
            + eng_header
            + "\n"
            + hdr
            + "\n"
            + sep
            + "\n"
            + "\n".join(rows)
            + "\n```"
        )
        await ctx.send(table)

        if bonus > 0:
            assumption = (
                f"Je huidige productiebonus is **{bonus:g}%**.\n"
                f"Voeg een tweede getal toe voor een specifiek doel, bijv. `/verhuiskosten {bonus:g} 55`."
            )
        else:
            assumption = (
                "Je bedrijf heeft momenteel **geen productiebonus**.\n"
                "Je kunt je huidige bonus als eerste getal opgeven (bijv. `/verhuiskosten 30`), "
                "en optioneel een doelbonus als tweede getal (bijv. `/verhuiskosten 30 55`)."
            )
        embed = discord.Embed(
            title="Break-evenpunt — bedrijfsverhuizing",
            description=(
                "Automated Engine productietijd om de verhuiskosten terug te verdienen.\n"
                f"{assumption}\n\n"
                f"**Verhuiskosten:** 5 × {concrete_price:.2f} = **{move_cost:.2f} coins**\n"
                f"**Gemiddelde PP-waarde:** {avg_pp_value:.4f} coins/pp"
            ),
            colour=colour,
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _pct(v) -> str:
        try:
            return f"{float(v):.2f}%"
        except (TypeError, ValueError):
            return "0%"

    @staticmethod
    def _long_bd(t: dict) -> str:
        parts: list[str] = []
        if t.get("strategic_bonus"):
            parts.append(f"{t['strategic_bonus']}% strat")
        if t.get("ethic_bonus"):
            parts.append(f"{t['ethic_bonus']}% eth")
        if t.get("ethic_deposit_bonus"):
            parts.append(f"{t['ethic_deposit_bonus']}% eth.dep")
        return " + ".join(parts)

    @staticmethod
    def _short_bd(d: dict) -> str:
        parts: list[str] = []
        if d.get("permanent_bonus"):
            parts.append(f"{d['permanent_bonus']}% perm")
        if d.get("deposit_bonus"):
            parts.append(f"{d['deposit_bonus']}% dep")
        if d.get("ethic_deposit_bonus"):
            parts.append(f"{d['ethic_deposit_bonus']}% eth.dep")
        return " + ".join(parts)

    @staticmethod
    def _format_duration(iso_str: str) -> str | None:
        try:
            end = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            delta = end - datetime.now(timezone.utc)
            if delta.total_seconds() <= 0:
                return "verlopen"
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes = remainder // 60
            if hours >= 24:
                days, hrs = divmod(hours, 24)
                return f"{days}d {hrs}h" if hrs else f"{days}d"
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        except Exception:
            return None

    @staticmethod
    def _unwrap_prices(resp) -> dict[str, float]:
        def _from_dict(d: dict) -> dict[str, float]:
            out: dict[str, float] = {}
            for k, v in d.items():
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    pass
            return out

        if isinstance(resp, dict):
            data = (
                (resp.get("result") or {}).get("data")
                if isinstance(resp.get("result"), dict)
                else None
            )
            if isinstance(data, dict):
                result = _from_dict(data)
                if result:
                    return result
            candidate = _from_dict(resp)
            if candidate:
                return candidate
            for v in resp.values():
                if isinstance(v, list):
                    out: dict[str, float] = {}
                    for entry in v:
                        if isinstance(entry, dict):
                            code = (
                                entry.get("itemCode")
                                or entry.get("item")
                                or entry.get("code")
                            )
                            price = entry.get("price") or entry.get("value")
                            if code and price is not None:
                                try:
                                    out[code] = float(price)
                                except (TypeError, ValueError):
                                    pass
                    if out:
                        return out
        if isinstance(resp, list):
            out = {}
            for entry in resp:
                if isinstance(entry, dict):
                    code = (
                        entry.get("itemCode") or entry.get("item") or entry.get("code")
                    )
                    price = entry.get("price") or entry.get("value")
                    if code and price is not None:
                        try:
                            out[code] = float(price)
                        except (TypeError, ValueError):
                            pass
            return out
        return {}

    @staticmethod
    def _parse_order_prices(resp) -> tuple[float, float]:
        """Extract (highest_buy_price, lowest_sell_price) from getTopOrders response."""
        if isinstance(resp, dict):
            inner = (resp.get("result") or {}).get("data")
            if isinstance(inner, dict):
                resp = inner
        if not isinstance(resp, dict):
            return 0.0, 0.0

        buy_price = 0.0
        sell_price = 0.0

        for buy_key in ("buyOrders", "buy_orders", "bids", "buy"):
            orders = resp.get(buy_key)
            if isinstance(orders, list) and orders:
                raw = (
                    orders[0].get("price")
                    or orders[0].get("unitPrice")
                    or orders[0].get("value")
                )
                if raw is not None:
                    try:
                        buy_price = float(raw)
                    except (TypeError, ValueError):
                        pass
                break

        for sell_key in ("sellOrders", "sell_orders", "asks", "sell"):
            orders = resp.get(sell_key)
            if isinstance(orders, list) and orders:
                raw = (
                    orders[0].get("price")
                    or orders[0].get("unitPrice")
                    or orders[0].get("value")
                )
                if raw is not None:
                    try:
                        sell_price = float(raw)
                    except (TypeError, ValueError):
                        pass
                break

        return buy_price, sell_price


async def setup(bot) -> None:
    await bot.add_cog(BonusCog(bot))
