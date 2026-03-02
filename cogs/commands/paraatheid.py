"""
This module defines the ParaatheadCog, which provides the /paraatheid command to display the war readiness of players, MUs, or countries in the WarEraNL bot.
- /paraatheid land:NL      — overzicht per niveaugroep: %oorlogsmodus + cooldown voor eco-spelers.
- /paraatheid speler:naam  — of speler al in oorlogsmodus is, of wanneer die kan resetten.
- /paraatheid mu:naam      — %oorlog + reset-cooldown voor eco-spelers in de MU.
- /paraatheid alle_mus:Ja  — overzicht van alle NL MUs gegroepeerd op type.
"""

from __future__ import annotations

import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import country_id as cid_of
from services.country_utils import find_country

logger = logging.getLogger("discord_bot")


class ParaatheadCog(CommandCogBase, name="paraatheid"):
    """Oorlogsparaatheid commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Autocomplete helpers                                                 #
    # ------------------------------------------------------------------ #

    async def _paraatheid_mu_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete MU names from the citizen_levels cache."""
        if not self._db:
            return []
        try:
            nl_country_id = self.config.get("nl_country_id")
            names = await self._db.get_distinct_mu_names(nl_country_id)
        except Exception:
            return []
        return [
            app_commands.Choice(name=n, value=n)
            for n in names
            if current.lower() in n.lower()
        ][:25]

    # ------------------------------------------------------------------ #
    # /paraatheid                                                          #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="paraatheid",
        description="Toon oorlogsparaatheid: wie is al in oorlogsmodus, en wie kan snel wisselen?",
    )
    @app_commands.describe(
        land="Land — overzicht per niveaugroep: %oorlogsmodus + cooldown voor eco-spelers.",
        speler="Zoek een speler op naam of ID.",
        mu="MU-naam — % oorlogsmodus + cooldown voor eco-spelers in de MU.",
        alle_mus="Toon paraatheid voor alle NL MUs in één tabel (geen verdere invoer nodig).",
    )
    @app_commands.autocomplete(
        land=country_autocomplete, mu=_paraatheid_mu_autocomplete
    )
    @app_commands.choices(alle_mus=[app_commands.Choice(name="Ja", value="ja")])
    async def paraatheid(  # noqa: C901
        self,
        ctx: Context,
        land: str | None = None,
        speler: str | None = None,
        mu: str | None = None,
        alle_mus: str | None = None,
    ):
        """/paraatheid — oorlogsparaatheid in vier modi:

        /paraatheid land:NL      — tabel per niveaugroep: %oorlog + cooldown voor eco-spelers
        /paraatheid speler:naam  — of speler al in oorlogsmodus is, of wanneer die kan resetten
        /paraatheid mu:naam      — %oorlog + reset-cooldown voor eco-spelers in de MU
        /paraatheid alle_mus:Ja  — overzicht van alle NL MUs gegroepeerd op type
        """
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return

        if speler is None and land is None and mu is None and alle_mus is None:
            speler = ctx.author.display_name

        provided = sum(x is not None for x in (land, speler, mu)) + int(
            alle_mus is not None
        )
        if provided > 1:
            await ctx.send(
                "Geef precies één optie op: **land**, **speler**, **mu** of **alle_mus**."
            )
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        colour = self._embed_colour()

        # ══ Mode 1: player ═══════════════════════════════════════════════
        if speler is not None:
            try:
                results = await self._db.find_citizen_readiness(speler)
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return
            if not results:
                await ctx.send(f"Geen speler gevonden voor `{speler}`.")
                return
            country_list_p = await self._fetch_country_list(ctx)
            cid_to_name: dict[str, str] = {}
            if country_list_p:
                for _c in country_list_p:
                    _cid = cid_of(_c)
                    _name = _c.get("name") or _c.get("code") or _cid
                    if _cid:
                        cid_to_name[_cid] = _name
            lines_p: list[str] = []
            for r in results:
                mode = r["skill_mode"]
                lvl = r["level"] or "?"
                raw_cid = r["country_id"] or ""
                land = cid_to_name.get(raw_cid) or raw_cid or "?"
                if mode == "war":
                    lines_p.append(
                        f"**{r['citizen_name']}** (lvl {lvl}, land {land})\n"
                        "⚔️ Paraat — kan nu vechten"
                    )
                else:
                    mode_str = "🌾 eco" if mode == "eco" else "❓ onbekend"
                    if r["can_reset"]:
                        cd_str = "✅ kan nu resetten naar oorlogsmodus"
                    elif r["days_ago"] is not None:
                        remaining = max(0.0, 7 - r["days_ago"])
                        cd_str = f"⏳ nog {remaining:.1f}d wachten voor reset"
                    else:
                        cd_str = "✅ kan nu resetten naar oorlogsmodus"
                    lines_p.append(
                        f"**{r['citizen_name']}** (lvl {lvl}, land {land})\n"
                        f"Skill-mode: {mode_str}\n"
                        f"Cooldown: {cd_str}"
                    )
            embed = discord.Embed(
                title=f"Paraatheid — {speler}",
                description="\n\n".join(lines_p),
                colour=colour,
            )
            await ctx.send(embed=embed)
            return

        # ══ Mode 2: MU ═══════════════════════════════════════════════════
        if mu is not None:
            nl_country_id = self.config.get("nl_country_id")
            try:
                mu_name, players = await self._db.get_mu_readiness_players(
                    mu, nl_country_id
                )
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return
            if mu_name is None or not players:
                await ctx.send(f"Geen MU gevonden die overeenkomt met `{mu}`.")
                return

            total_mu = len(players)
            war_count = sum(1 for p in players if p["skill_mode"] == "war")
            eco_players = [p for p in players if p["skill_mode"] != "war"]
            can_reset_count = sum(1 for p in eco_players if p["can_reset"])
            waiting_players = [
                p
                for p in eco_players
                if not p["can_reset"] and p["days_ago"] is not None
            ]
            if waiting_players:
                avg_rem = max(
                    0.0,
                    7
                    - sum(p["days_ago"] for p in waiting_players)
                    / len(waiting_players),
                )
                cd_avg_str = f"gem. {avg_rem:.1f}d cooldown resterend"
            else:
                cd_avg_str = None
            war_pct = war_count / total_mu * 100 if total_mu else 0.0

            desc_lines = [f"⚔️ Paraat: **{war_count}** / {total_mu} ({war_pct:.0f}%)"]
            if can_reset_count:
                desc_lines.append(f"✅ Kan resetten: **{can_reset_count}**")
            if cd_avg_str:
                desc_lines.append(f"⏱️ {cd_avg_str}")

            summary_embed = discord.Embed(
                title=f"Paraatheid — {mu_name}",
                description="\n".join(desc_lines),
                colour=colour,
            )
            await ctx.send(embed=summary_embed)

            embed_limit_MU = 3900
            name_w = 16
            lvl_w = 2

            async def _flush_mu(lines: list[str]) -> None:
                block = "```\n" + "\n".join(lines) + "\n```"
                embed = discord.Embed(description=block, colour=colour)
                await ctx.send(embed=embed)

            header = f"  {'naam':<{name_w}}  {'lv':>{lvl_w}}  cooldown"
            sep = "─" * len(header)
            pending: list[str] = [header, sep]
            for p in players:
                mode = p["skill_mode"]
                mode_icon = "🌾" if mode == "eco" else ("⚔️" if mode == "war" else "❓")
                name = str(p["citizen_name"] or "?")[:name_w].ljust(name_w)
                lvl = str(p["level"] or "?").rjust(lvl_w)
                if mode == "war":
                    cd = "paraat"
                elif p["can_reset"]:
                    cd = "kan nu resetten"
                elif p["days_ago"] is not None:
                    cd = f"⏳ {max(0.0, 7 - p['days_ago']):.1f}d"
                else:
                    cd = "kan nu resetten"
                line = f"{mode_icon} {name}  {lvl}  {cd}"
                candidate = "\n".join(pending + [line])
                if len(candidate) > embed_limit_MU and len(pending) > 2:
                    await _flush_mu(pending)
                    pending = [header, sep, line]
                else:
                    pending.append(line)
            if len(pending) > 2:
                await _flush_mu(pending)
            return

        # ══ Mode 3: alle MUs ══════════════════════════════════════════════
        if alle_mus:
            nl_country_id = self.config.get("nl_country_id")
            testing = getattr(self.bot, "testing", False)
            mus_json = "templates/mus.testing.json" if testing else "templates/mus.json"
            try:
                with open(mus_json, encoding="utf-8") as _f:
                    _mus_data = json.load(_f)
            except Exception as exc:
                await ctx.send(f"Kon {mus_json} niet lezen: {exc}")
                return
            _mu_types: dict[str, str] = {}
            for _emb in _mus_data.get("embeds", []):
                _title = _emb.get("title", "")
                _m = re.search(r"\[\*\*(.+?)\*\*\]", _emb.get("description", ""))
                _mu_types[_title] = _m.group(1) if _m else "Standaard MU"
            if not _mu_types:
                await ctx.send("Geen MUs gevonden in het configuratiebestand.")
                return
            try:
                mu_stats = await self._db.get_all_mu_readiness(nl_country_id)
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return

            name_w_all = 16
            hdr = f"{'naam':<{name_w_all}}  {'par':>5}  {'kan':>3}  {'≥15':>3}  {'≥20':>3}  {'avg':>5}"
            sep = "─" * len(hdr)

            _cat_cfg = [
                ("Elite MU", "🟠", "🟠 Elite MU"),
                ("Eco MU", "🟢", "🟢 Eco MU"),
                ("Standaard MU", "🔵", "🔵 Standaard MU"),
            ]

            emb = discord.Embed(
                title="Paraatheid — Alle NL MUs",
                description="par = paraat / totaal  •  kan = kan nu resetten  •  ≥15/≥20 = paraat op lvl ≥15/≥20  •  avg = gem. wachttijd eco-spelers",
                colour=discord.Color.gold(),
            )
            has_data = False

            for mu_type, _emoji, field_label in _cat_cfg:
                mu_names_of_type = [n for n, t in _mu_types.items() if t == mu_type]
                if not mu_names_of_type:
                    continue

                rows: list[str] = []
                total_par = total_total = total_kan = total_w15 = total_w20 = 0
                all_waiting: list[float] = []
                for mu_name in mu_names_of_type:
                    stats = mu_stats.get(mu_name)
                    if stats is None:
                        row = f"{mu_name[:name_w_all]:<{name_w_all}}  {'?':>5}  {'?':>3}  {'?':>3}  {'?':>3}  {'?':>5}"
                    else:
                        par_str = f"{stats['war']}/{stats['total']}"
                        kan_str = str(stats["can_reset"])
                        w15_str = str(stats.get("war_15", 0))
                        w20_str = str(stats.get("war_20", 0))
                        if stats["waiting_days"]:
                            avg_rem = max(
                                0.0,
                                7
                                - sum(stats["waiting_days"])
                                / len(stats["waiting_days"]),
                            )
                            avg_str = f"{avg_rem:.1f}d"
                        else:
                            avg_str = "—"
                        row = f"{mu_name[:name_w_all]:<{name_w_all}}  {par_str:>5}  {kan_str:>3}  {w15_str:>3}  {w20_str:>3}  {avg_str:>5}"
                        total_par += stats["war"]
                        total_total += stats["total"]
                        total_kan += stats["can_reset"]
                        total_w15 += stats.get("war_15", 0)
                        total_w20 += stats.get("war_20", 0)
                        all_waiting.extend(stats["waiting_days"])
                    rows.append(row)

                if total_total:
                    tot_par_str = f"{total_par}/{total_total}"
                    tot_kan_str = str(total_kan)
                    tot_w15_str = str(total_w15)
                    tot_w20_str = str(total_w20)
                    if all_waiting:
                        tot_avg_rem = max(0.0, 7 - sum(all_waiting) / len(all_waiting))
                        tot_avg_str = f"{tot_avg_rem:.1f}d"
                    else:
                        tot_avg_str = "—"
                    rows.append("─" * len(hdr))
                    rows.append(
                        f"{'totaal':<{name_w_all}}  {tot_par_str:>5}  {tot_kan_str:>3}  {tot_w15_str:>3}  {tot_w20_str:>3}  {tot_avg_str:>5}"
                    )

                block_text = (
                    "```\n" + hdr + "\n" + sep + "\n" + "\n".join(rows) + "\n```"
                )
                emb.add_field(name=field_label, value=block_text, inline=False)
                has_data = True

            if not has_data:
                await ctx.send(
                    "Geen gecachete MU-data gevonden. Voer eerst `/peil burgers` uit."
                )
                return
            await ctx.send(embed=emb)
            return

        # ══ Mode 4: land ══════════════════════════════════════════════
        country_list = await self._fetch_country_list(ctx)
        if country_list is None:
            return
        target = find_country(land, country_list)
        if target is None:
            await ctx.send(f"Land `{land}` niet gevonden.")
            return
        cid = cid_of(target)
        country_name = target.get("name", land)

        try:
            (
                skill_buckets,
                last_updated,
            ) = await self._db.get_skill_mode_by_level_buckets(cid)
            cd_buckets, _ = await self._db.get_skill_reset_cooldown_by_level_buckets(
                cid
            )
        except Exception as exc:
            await ctx.send(f"Databasefout: {exc}")
            return

        if not skill_buckets:
            await ctx.send(
                f"Nog geen gecachte vaardigheidsdata voor **{country_name}**.\n"
                f"Run `/peil burgers` om de cache op te bouwen."
            )
            return

        all_bkts = sorted(set(skill_buckets) | set(cd_buckets))
        max_bucket = max(all_bkts)
        header = f"{'Levels':<9}  {'Spl':>4}  {'%Oor':>4}  {'Kan':>4}  {'CD':>6}"
        sep = "─" * (9 + 2 + 4 + 2 + 4 + 2 + 4 + 2 + 6)

        data_rows: list[str] = []
        for b in all_bkts:
            s = skill_buckets.get(b, {"eco": 0, "war": 0, "unknown": 0})
            c = cd_buckets.get(
                b, {"count": 0, "avg_days_ago": 0.0, "available": 0, "no_data": 0}
            )
            total_s = s["eco"] + s["war"] + s["unknown"]
            known_s = s["eco"] + s["war"]
            war_pct_b = s["war"] / known_s * 100 if known_s else 0.0
            total_c = c["count"] + c["no_data"]
            avail_pct_b = c["available"] / total_c * 100 if total_c else 0.0
            avg_rem_b = max(0.0, 7 - c["avg_days_ago"]) if c["count"] else None
            cd_str_b = f"{avg_rem_b:.1f}d" if avg_rem_b is not None else "n.v.t."
            b_end = min(b + 4, max_bucket + 4)
            data_rows.append(
                f" {b:>3}–{b_end:<3}  {total_s:>4}  {war_pct_b:>3.0f}%  {avail_pct_b:>3.0f}%  {cd_str_b:>6}"
            )

        embed_limit = 3900
        chunks: list[list[str]] = []
        cur_chunk: list[str] = []
        for row in data_rows:
            candidate = "\n".join(cur_chunk + [row])
            if (
                len(f"```\n{header}\n{sep}\n{candidate}\n```") > embed_limit
                and cur_chunk
            ):
                chunks.append(cur_chunk)
                cur_chunk = [row]
            else:
                cur_chunk.append(row)
        if cur_chunk:
            chunks.append(cur_chunk)

        total_eco = sum(v["eco"] for v in skill_buckets.values())
        total_war = sum(v["war"] for v in skill_buckets.values())
        total_known = total_eco + total_war
        total_citizens = sum(
            v["eco"] + v["war"] + v["unknown"] for v in skill_buckets.values()
        )
        total_avail = sum(v["available"] for v in cd_buckets.values())
        total_cd_data = sum(v["count"] for v in cd_buckets.values())
        war_pct_total = total_war / total_known * 100 if total_known else 0.0
        avail_pct_total = total_avail / total_eco * 100 if total_eco else 0.0

        footer_parts = [f"{total_citizens} burgers"]
        if last_updated:
            footer_parts.append(
                f"Bijgewerkt: {last_updated[:16].replace('T', ' ')} UTC"
            )
        footer_parts.append("Spl=spelers  %Oor=%Oorlog  Kan/CD=alleen eco-spelers")
        footer_text = "  •  ".join(footer_parts)

        page_embeds: list[discord.Embed] = []
        for page_idx, chunk in enumerate(chunks):
            block = f"```\n{header}\n{sep}\n" + "\n".join(chunk) + "\n```"
            emb = discord.Embed(
                title=f"Paraatheid — {country_name}",
                description=block,
                colour=colour,
            )
            emb.set_footer(
                text=footer_text
                if page_idx == 0
                else f"{total_citizens} burgers (vervolg)"
            )
            page_embeds.append(emb)

        last = page_embeds[-1]
        if total_known > 0:
            last.add_field(
                name="⚔️ Paraat (oorlogsmodus)",
                value=f"**{total_war}** / {total_citizens} ({war_pct_total:.1f}%)",
                inline=True,
            )
        last.add_field(
            name="✅ Eco — kan nu resetten",
            value=f"**{total_avail}** ({avail_pct_total:.0f}% van eco)",
            inline=True,
        )
        if total_cd_data > 0:
            overall_avg_d = (
                sum(v["avg_days_ago"] * v["count"] for v in cd_buckets.values())
                / total_cd_data
            )
            last.add_field(
                name="⏱️ Gem. wachttijd eco-spelers",
                value=f"**{max(0.0, 7 - overall_avg_d):.1f}** dagen",
                inline=True,
            )

        for emb in page_embeds:
            await ctx.send(embed=emb)


async def setup(bot) -> None:
    """Add the ParaatheidCog to the bot."""
    await bot.add_cog(ParaatheadCog(bot))
