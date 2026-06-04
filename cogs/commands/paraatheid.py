"""
This module defines the ParaatheadCog, which provides the /paraatheid command to display the war readiness of players, MUs, or countries in the WarEraNL bot.
- /paraatheid land:NL      — overzicht per niveaugroep: %oorlogsmodus + cooldown voor eco-spelers.
- /paraatheid speler:naam  — of speler al in oorlogsmodus is, of wanneer die kan resetten.
- /paraatheid mu:naam      — %oorlog + reset-cooldown voor eco-spelers in de MU.
- /paraatheid nl_mus:Ja    — overzicht van alle NL MUs gegroepeerd op type.
"""

from __future__ import annotations

import json
import logging
import re
import time as _t
from datetime import datetime as _dt
from datetime import timezone as _tz

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import (
    CommandCogBase,
    citizen_autocomplete,
    country_autocomplete,
)
from services.country_utils import country_id as cid_of
from services.country_utils import find_country

logger = logging.getLogger("discord_bot")


def _fmt_hm(secs: float) -> str:
    """Format seconds as 'Xh Ym'."""
    total_m = int(secs) // 60
    h, m = divmod(total_m, 60)
    return f"{h}u {m:02d}m"


def _pill_field_value(stats: dict) -> str:
    """Format pill stats dict as a field value string."""
    buff: int = stats["buff"]
    debuff: int = stats["debuff"]
    none_: int = stats["none"]
    total: int = stats["total"]
    parts: list[str] = []
    if buff:
        avg_str = _fmt_hm(stats["avg_buff_secs"])
        parts.append(f"� In buff: **{buff}** (gem. {avg_str} over)")
    else:
        parts.append("💊 In buff: **0**")
    if debuff:
        avg_str = _fmt_hm(stats["avg_debuff_secs"])
        parts.append(f"🤢 In debuff: **{debuff}** (gem. {avg_str} over)")
    else:
        parts.append("🤢 In debuff: **0**")
    parts.append(f"⬜ Geen actieve pil: **{none_}**")
    parts.append(f"*(Totaal: {total} spelers)*")
    return "\n".join(parts)


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
        """Autocomplete MU names from the known_mus registry (all game MUs)."""
        if not self._db:
            return []
        try:
            names = await self._db.get_known_mu_names(current)
        except Exception:
            try:
                names = await self._db.get_distinct_mu_names()
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
        mu="MU-naam — % oorlogsmodus + cooldown voor eco-spelers in de MU (alle MUs).",
        nl_mus="Toon paraatheid voor alle NL MUs in één tabel (geen verdere invoer nodig).",
        divisie="Toon paraatheid voor alle divisies tegelijk (één tabel per divisie).",
    )
    @app_commands.autocomplete(
        land=country_autocomplete,
        mu=_paraatheid_mu_autocomplete,
        speler=citizen_autocomplete,
    )
    @app_commands.choices(
        nl_mus=[app_commands.Choice(name="Ja", value="ja")],
        divisie=[app_commands.Choice(name="Ja", value="ja")],
    )
    async def paraatheid(  # noqa: C901
        self,
        ctx: Context,
        land: str | None = None,
        speler: str | None = None,
        mu: str | None = None,
        nl_mus: str | None = None,
        divisie: str | None = None,
    ):
        """/paraatheid — oorlogsparaatheid in vijf modi:

        /paraatheid land:NL      — tabel per niveaugroep: %oorlog + cooldown voor eco-spelers
        /paraatheid speler:naam  — of speler al in oorlogsmodus is, of wanneer die kan resetten
        /paraatheid mu:naam      — %oorlog + reset-cooldown voor eco-spelers in de MU (alle MUs)
        /paraatheid nl_mus:Ja    — overzicht van alle NL MUs gegroepeerd op type
        /paraatheid divisie:Ja    — overzicht paraatheid per divisie (D1–D5)
        """
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return

        if speler is None and land is None and mu is None and nl_mus is None and divisie is None:
            speler = ctx.author.display_name

        provided = sum(x is not None for x in (land, speler, mu, divisie)) + int(
            nl_mus is not None
        )
        if provided > 1:
            await ctx.send(
                "Geef precies één optie op: **land**, **speler**, **mu**, **divisie** of **nl_mus**."
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
            # Pill buff/debuff status via live API (only if exactly one player found)
            if len(results) == 1 and self._client:
                try:
                    user_data = await self._client.get(
                        "/user.getUserLite",
                        params={"userId": results[0]["user_id"]},
                    )
                    buffs = (user_data or {}).get("buffs") or {}
                    buff_codes: list = buffs.get("buffCodes") or []
                    buff_end_at: str | None = buffs.get("buffEndAt")
                    now_ts = _t.time()
                    if "cocain" in buff_codes and buff_end_at:
                        try:
                            from datetime import datetime as _dt
                            from datetime import timezone as _tz
                            end_ts = _dt.fromisoformat(buff_end_at.replace("Z", "+00:00")).timestamp()
                            secs_left = max(0.0, end_ts - now_ts)
                            pill_val = f"� In buff — nog {_fmt_hm(secs_left)}"
                        except Exception:
                            pill_val = "💊 In buff"
                    else:
                        debuff_duration = 16 * 3600
                        if buff_end_at:
                            try:
                                from datetime import datetime as _dt
                                from datetime import timezone as _tz
                                end_ts = _dt.fromisoformat(buff_end_at.replace("Z", "+00:00")).timestamp()
                                debuff_left = max(0.0, end_ts + debuff_duration - now_ts)
                                if debuff_left > 0:
                                    pill_val = f"🤢 In debuff — nog {_fmt_hm(debuff_left)}"
                                else:
                                    pill_val = "⬜ Geen actieve pil"
                            except Exception:
                                pill_val = "⬜ Geen actieve pil"
                        else:
                            pill_val = "⬜ Geen actieve pil"
                    embed.add_field(name="💊 Pill status", value=pill_val, inline=False)
                except Exception:
                    pass
            await ctx.send(embed=embed)
            return

        # ══ Mode 2: MU ═══════════════════════════════════════════════════
        if mu is not None:
            # Prefer live API (always has full current membership); fall back to DB cache.
            mu_name: str | None = None
            players: list[dict] = []
            citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
            if citizen_cache:
                try:
                    mu_name, players = await citizen_cache.fetch_mu_players_live(mu)
                    if players:
                        logger.info(
                            "paraatheid mu: live fetch returned %d players for %r",
                            len(players), mu_name,
                        )
                    else:
                        logger.warning(
                            "paraatheid mu: live fetch returned 0 players for %r, trying DB", mu
                        )
                except Exception:
                    logger.exception("paraatheid mu: live fetch failed, falling back to DB")

            if mu_name is None or not players:
                logger.info("paraatheid mu: using DB fallback for %r", mu)
                try:
                    mu_name, players = await self._db.get_mu_readiness_players(
                        mu, country_id=None
                    )
                except Exception as exc:
                    await ctx.send(f"Databasefout: {exc}")
                    return

            if mu_name is None or not players:
                await ctx.send(f"Geen MU gevonden die overeenkomt met `{mu}`.")
                return

            total_mu = len(players)
            war_count = sum(1 for p in players if p["skill_mode"] == "war")
            can_reset_count = sum(
                1 for p in players if p["skill_mode"] != "war" and p["can_reset"]
            )
            war_pct = war_count / total_mu * 100 if total_mu else 0.0

            # Average remaining cooldown for eco players still waiting
            eco_waiting = [
                max(0.0, 7 - p["days_ago"])
                for p in players
                if p["skill_mode"] == "eco" and not p["can_reset"] and p["days_ago"] is not None
            ]
            avg_eco_cd_str = (
                f"{sum(eco_waiting) / len(eco_waiting):.1f}d"
                if eco_waiting else None
            )

            summary_line = (
                f"⚔️ **{war_count}** paraat ({war_pct:.0f}%)  •  "
                f"✅ **{can_reset_count}** kunnen resetten"
            )

            # Detect whether live data includes health/hunger (live fetch path)
            has_live_stats = any(p.get("health_cur") is not None for p in players)

            embed_limit_MU = 3900
            name_w = 10 if has_live_stats else 13
            lvl_w = 2
            num_w = len(str(total_mu))

            is_first_chunk = True

            _GRN = "\033[32m"
            _RED = "\033[31m"
            _RST = "\033[0m"

            async def _flush_mu(lines: list[str]) -> None:
                nonlocal is_first_chunk
                block = "```ansi\n" + "\n".join(lines) + "\n```"
                if is_first_chunk:
                    embed = discord.Embed(
                        title=f"Paraatheid — {mu_name} — overzicht militaire eenheid",
                        description=summary_line + "\n" + block,
                        colour=colour,
                    )
                    is_first_chunk = False
                else:
                    embed = discord.Embed(description=block, colour=colour)
                await ctx.send(embed=embed)

            if has_live_stats:
                # 3-space prefix matches: emoji (2 visual cols) + 1 space for all mode icons
                header = f"   {'#':>{num_w}} {'naam':<{name_w}} {'lv':>{lvl_w}} {'hp':>3} {'hu':>2} 💊 {'tijd':>7}"
                sep = "─" * (3 + num_w + 1 + name_w + 1 + lvl_w + 1 + 3 + 1 + 2 + 1 + 1 + 1 + 7)
            else:
                header = f"   {'#':>{num_w}}  {'naam':<{name_w}}  {'lv':>{lvl_w}}  cooldown"
                sep = "─" * (3 + num_w + 2 + name_w + 2 + lvl_w + 2 + 16)
            pending: list[str] = [header, sep]
            for i, p in enumerate(players, start=1):
                mode = p["skill_mode"]
                # ⚔️ and 🌾 are both 2 display columns wide → consistent alignment
                mode_char = "🌾" if mode == "eco" else ("⚔️" if mode == "war" else "❓")
                num = str(i).rjust(num_w)
                name = str(p["citizen_name"] or "?")[:name_w].ljust(name_w)
                lvl = str(p["level"] or "?").rjust(lvl_w)
                if has_live_stats:
                    hp_str = str(int(p["health_cur"])) if p.get("health_cur") is not None else "  ?"
                    hu_str = str(int(p["hunger_cur"])) if p.get("hunger_cur") is not None else " ?"
                    pill_icon = p.get("pill_icon", "⚪")
                    if pill_icon == "🟢":
                        pill_str = f"{_GRN}+{_RST}"
                    elif pill_icon == "🔴":
                        pill_str = f"{_RED}-{_RST}"
                    else:
                        pill_str = "."
                    exp_ts = p.get("pill_expires_ts")
                    if exp_ts is not None:
                        secs_left = max(0.0, exp_ts - _t.time())
                        raw_tijd = _fmt_hm(secs_left)
                        if pill_icon == "🟢":
                            tijd_str = f"{_GRN}{raw_tijd:>7}{_RST}"
                        else:
                            tijd_str = f"{_RED}{raw_tijd:>7}{_RST}"
                    else:
                        tijd_str = f"{'—':>7}"
                    line = f"{mode_char} {num} {name} {lvl} {hp_str:>3} {hu_str:>2} {pill_str} {tijd_str}"
                else:
                    if mode == "war":
                        cd = "par"
                    elif p["can_reset"]:
                        cd = "reset"
                    elif p["days_ago"] is not None:
                        cd = f"{max(0.0, 7 - p['days_ago']):.1f}d"
                    else:
                        cd = "reset"
                    line = f"{mode_char} {num}  {name}  {lvl}  {cd}"
                candidate = "\n".join(pending + [line])
                if len(candidate) > embed_limit_MU and len(pending) > 2:
                    await _flush_mu(pending)
                    pending = [header, sep, line]
                else:
                    pending.append(line)
            if len(pending) > 2:
                await _flush_mu(pending)

            # Stats embed: pill from live data (any MU, incl. non-Dutch), health/hunger/eco-cd from live data
            stats_parts: list[str] = []
            if has_live_stats:
                # Compute pill stats directly from live player data — works for any MU
                buff_players = [p for p in players if p.get("pill_icon") == "🟢"]
                debuff_players = [p for p in players if p.get("pill_icon") == "🔴"]
                buff_n = len(buff_players)
                debuff_n = len(debuff_players)
                none_n = len(players) - buff_n - debuff_n
                now_live = _t.time()
                buff_secs_l = [p["pill_expires_ts"] - now_live for p in buff_players if p.get("pill_expires_ts")]
                debuff_secs_l = [p["pill_expires_ts"] - now_live for p in debuff_players if p.get("pill_expires_ts")]
                buff_line = f"� Pil actief: **{buff_n}**"
                if buff_secs_l:
                    buff_line += f" (gem. {_fmt_hm(sum(buff_secs_l) / len(buff_secs_l))} over)"
                stats_parts.append(buff_line)
                debuff_line = f"🤢 In debuff: **{debuff_n}**"
                if debuff_secs_l:
                    debuff_line += f" (gem. {_fmt_hm(sum(debuff_secs_l) / len(debuff_secs_l))} over)"
                stats_parts.append(debuff_line)
                stats_parts.append(f"⬜ Geen pil: **{none_n}**")
            elif self._db:
                try:
                    pill_stats = await self._db.get_pill_stats_from_tracking(mu_names=[mu_name])
                    buff_n = pill_stats["buff"]
                    debuff_n = pill_stats["debuff"]
                    none_n = pill_stats["none"]
                    buff_line = f"� Pil actief: **{buff_n}**"
                    if buff_n and pill_stats.get("avg_buff_secs"):
                        buff_line += f" (gem. {_fmt_hm(pill_stats['avg_buff_secs'])} over)"
                    stats_parts.append(buff_line)
                    debuff_line = f"🤢 In debuff: **{debuff_n}**"
                    if debuff_n and pill_stats.get("avg_debuff_secs"):
                        debuff_line += f" (gem. {_fmt_hm(pill_stats['avg_debuff_secs'])} over)"
                    stats_parts.append(debuff_line)
                    stats_parts.append(f"⬜ Geen pil: **{none_n}**")
                except Exception:
                    pass
            if has_live_stats:
                health_vals = [p["health_cur"] for p in players if p.get("health_cur") is not None]
                hunger_vals = [p["hunger_cur"] for p in players if p.get("hunger_cur") is not None]
                if health_vals:
                    stats_parts.append(f"❤️ Gem. health: **{sum(health_vals) / len(health_vals):.0f}**")
                if hunger_vals:
                    stats_parts.append(f"🍞 Gem. hunger: **{sum(hunger_vals) / len(hunger_vals):.1f}**")
            if avg_eco_cd_str:
                stats_parts.append(
                    f"⏱️ Gem. eco-cd: **{avg_eco_cd_str}** ({len(eco_waiting)} eco-spelers wachtend)"
                )
            if stats_parts:
                pill_emb = discord.Embed(
                    title=f"📊 Stats — {mu_name}",
                    description="\n".join(stats_parts),
                    colour=colour,
                )
                await ctx.send(embed=pill_emb)
            return

        # ══ Mode 3: NL MUs ════════════════════════════════════════════════
        if nl_mus:
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

            # New schema: {id, type, role_id, name, thumbnail}
            _entries = [e for e in _mus_data.get("embeds", []) if isinstance(e, dict)]
            _new_schema = any(e.get("id") and e.get("type") for e in _entries)
            if _new_schema:
                for _entry in _entries:
                    _name = str(_entry.get("name") or f"MU {str(_entry.get('id'))[:8]}")
                    _type_raw = str(_entry.get("type", "")).strip().lower()
                    if _type_raw == "elite":
                        _mu_types[_name] = "Elite MU"
                    elif _type_raw == "eco":
                        _mu_types[_name] = "Eco MU"
                    else:
                        _mu_types[_name] = "Standaard MU"

            # Backward compatibility for old schema
            if not _mu_types:
                for _emb in _entries:
                    _title = _emb.get("title", "")
                    _m = re.search(r"\[\*\*(.+?)\*\*\]", _emb.get("description", ""))
                    _mu_types[_title] = _m.group(1) if _m else "Standaard MU"
            if not _mu_types:
                await ctx.send("Geen MUs gevonden in het configuratiebestand.")
                return
            try:
                mu_stats = await self._db.get_all_mu_readiness()
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return

            name_w_all = 12
            hdr = f"{'naam':<{name_w_all}}  {'par':>5}  {'kan':>3}  {'≥15':>3}  {'≥20':>3}  {'avg':>4}"
            sep = "─" * len(hdr)

            _cat_cfg = [
                ("Elite MU", "🟠", "🟠 Elite MU"),
                ("Eco MU", "🟢", "🟢 Eco MU"),
                ("Standaard MU", "🔵", "🔵 Standaard MU"),
            ]

            emb = discord.Embed(
                title="Paraatheid — overzicht alle NL militaire eenheden",
                description="par = paraat / totaal  •  kan = kan nu resetten  •  ≥15/≥20 = paraat op lvl ≥15/≥20  •  avg = gem. wachttijd eco-spelers",
                colour=discord.Color.gold(),
            )

            # Fetch last_updated for NL citizen data
            _nl_last_updated: str | None = None
            if nl_country_id and self._db:
                try:
                    _, _, _nl_last_updated = await self._db.get_level_distribution(nl_country_id)
                except Exception:
                    pass
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
                        row = f"{mu_name[:name_w_all]:<{name_w_all}}  {'?':>5}  {'?':>3}  {'?':>3}  {'?':>3}  {'?':>4}"
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
                        row = f"{mu_name[:name_w_all]:<{name_w_all}}  {par_str:>5}  {kan_str:>3}  {w15_str:>3}  {w20_str:>3}  {avg_str:>4}"
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
                        f"{'totaal':<{name_w_all}}  {tot_par_str:>5}  {tot_kan_str:>3}  {tot_w15_str:>3}  {tot_w20_str:>3}  {tot_avg_str:>4}"
                    )

                # Split rows into chunks that each fit within Discord's 1024-char field limit
                FIELD_MAX = 1024
                prefix = "```\n" + hdr + "\n" + sep + "\n"
                suffix = "\n```"
                overhead = len(prefix) + len(suffix)
                chunks: list[list[str]] = []
                current: list[str] = []
                current_len = 0
                for row in rows:
                    row_len = len(row) + 1  # +1 for newline
                    if current and overhead + current_len + row_len > FIELD_MAX:
                        chunks.append(current)
                        current = []
                        current_len = 0
                    current.append(row)
                    current_len += row_len
                if current:
                    chunks.append(current)

                for i, chunk in enumerate(chunks):
                    block_text = prefix + "\n".join(chunk) + suffix
                    chunk_label = field_label if i == 0 else f"{field_label} (vervolg)"
                    emb.add_field(name=chunk_label, value=block_text, inline=False)
                has_data = True

            if not has_data:
                await ctx.send(
                    "Geen gecachete MU-data gevonden. Voer eerst `/peil burgers` uit."
                )
                return
            _footer_parts = ["Automatisch elk uur vernieuwd  •  Handmatig: /peil burgers nl"]
            if _nl_last_updated:
                _footer_parts.insert(0, f"Bijgewerkt: {_nl_last_updated[:16].replace('T', ' ')} UTC")
            emb.set_footer(text="  •  ".join(_footer_parts))
            await ctx.send(embed=emb)
            # Pill buff/debuff section (from hourly tracking DB, MU members only)
            if self._db:
                try:
                    _nl_mu_names = list(_mu_types.keys())
                    pill_stats = await self._db.get_pill_stats_from_tracking(mu_names=_nl_mu_names)
                    _sp: list[str] = []
                    _bl = f"� Pil actief: **{pill_stats['buff']}**"
                    if pill_stats['buff'] and pill_stats.get('avg_buff_secs'):
                        _bl += f" (gem. {_fmt_hm(pill_stats['avg_buff_secs'])} over)"
                    _sp.append(_bl)
                    _dl = f"🤢 In debuff: **{pill_stats['debuff']}**"
                    if pill_stats['debuff'] and pill_stats.get('avg_debuff_secs'):
                        _dl += f" (gem. {_fmt_hm(pill_stats['avg_debuff_secs'])} over)"
                    _sp.append(_dl)
                    _sp.append(f"⬜ Geen pil: **{pill_stats['none']}**")
                    pill_emb = discord.Embed(
                        title="📊 Stats — NL MUs",
                        description="\n".join(_sp),
                        colour=discord.Color.gold(),
                    )
                    await ctx.send(embed=pill_emb)
                except Exception:
                    pass
            return

        # ══ Mode 3.5: divisie ═══════════════════════════════════════════
        if divisie is not None:
            from cogs.tasks.war_guild_divisions import DIVISION_MUS  # noqa: PLC0415
            try:
                mu_stats = await self._db.get_all_mu_readiness()
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return

            name_w_div = 12
            hdr = f"{'naam':<{name_w_div}}  {'par':>5}  {'kan':>3}  {'≥15':>3}  {'≥20':>3}  {'avg':>4}"
            sep = "─" * len(hdr)
            FIELD_MAX = 1024
            prefix = "```\n" + hdr + "\n" + sep + "\n"
            suffix = "\n```"
            overhead = len(prefix) + len(suffix)

            emb = discord.Embed(
                title="Paraatheid — overzicht per divisie",
                description=(
                    "par = paraat / totaal  •  kan = kan nu resetten  •  "
                    "≥15/≥20 = paraat op lvl ≥15/≥20  •  avg = gem. wachttijd eco-spelers"
                ),
                colour=colour,
            )
            all_div_mu_names: list[str] = []

            for div_num in sorted(DIVISION_MUS):
                mu_names_in_div = DIVISION_MUS[div_num]
                all_div_mu_names.extend(mu_names_in_div)
                rows: list[str] = []
                total_par = total_total = total_kan = total_w15 = total_w20 = 0
                all_waiting_div: list[float] = []

                for mu_name in mu_names_in_div:
                    stats = mu_stats.get(mu_name)
                    if stats is None:
                        row = f"{mu_name[:name_w_div]:<{name_w_div}}  {'?':>5}  {'?':>3}  {'?':>3}  {'?':>3}  {'?':>4}"
                    else:
                        par_str = f"{stats['war']}/{stats['total']}"
                        kan_str = str(stats["can_reset"])
                        w15_str = str(stats.get("war_15", 0))
                        w20_str = str(stats.get("war_20", 0))
                        if stats["waiting_days"]:
                            avg_rem = max(
                                0.0,
                                7 - sum(stats["waiting_days"]) / len(stats["waiting_days"]),
                            )
                            avg_str = f"{avg_rem:.1f}d"
                        else:
                            avg_str = "—"
                        row = f"{mu_name[:name_w_div]:<{name_w_div}}  {par_str:>5}  {kan_str:>3}  {w15_str:>3}  {w20_str:>3}  {avg_str:>4}"
                        total_par += stats["war"]
                        total_total += stats["total"]
                        total_kan += stats["can_reset"]
                        total_w15 += stats.get("war_15", 0)
                        total_w20 += stats.get("war_20", 0)
                        all_waiting_div.extend(stats["waiting_days"])
                    rows.append(row)

                if total_total:
                    tot_par_str = f"{total_par}/{total_total}"
                    tot_kan_str = str(total_kan)
                    tot_w15_str = str(total_w15)
                    tot_w20_str = str(total_w20)
                    if all_waiting_div:
                        tot_avg_rem = max(0.0, 7 - sum(all_waiting_div) / len(all_waiting_div))
                        tot_avg_str = f"{tot_avg_rem:.1f}d"
                    else:
                        tot_avg_str = "—"
                    rows.append("─" * len(hdr))
                    rows.append(
                        f"{'totaal':<{name_w_div}}  {tot_par_str:>5}  {tot_kan_str:>3}  {tot_w15_str:>3}  {tot_w20_str:>3}  {tot_avg_str:>4}"
                    )

                # Split into chunks fitting Discord's 1024-char field limit
                chunks: list[list[str]] = []
                current_chunk: list[str] = []
                current_len = 0
                for row in rows:
                    row_len = len(row) + 1
                    if current_chunk and overhead + current_len + row_len > FIELD_MAX:
                        chunks.append(current_chunk)
                        current_chunk = []
                        current_len = 0
                    current_chunk.append(row)
                    current_len += row_len
                if current_chunk:
                    chunks.append(current_chunk)

                for i, chunk in enumerate(chunks):
                    block_text = prefix + "\n".join(chunk) + suffix
                    field_label = f"Divisie {div_num}" if i == 0 else f"Divisie {div_num} (vervolg)"
                    emb.add_field(name=field_label, value=block_text, inline=False)

            await ctx.send(embed=emb)

            if self._db:
                try:
                    pill_stats = await self._db.get_pill_stats_from_tracking(mu_names=all_div_mu_names)
                    _sp_div: list[str] = []
                    _bl_div = f"💊 Pil actief: **{pill_stats['buff']}**"
                    if pill_stats["buff"] and pill_stats.get("avg_buff_secs"):
                        _bl_div += f" (gem. {_fmt_hm(pill_stats['avg_buff_secs'])} over)"
                    _sp_div.append(_bl_div)
                    _dl_div = f"🤢 In debuff: **{pill_stats['debuff']}**"
                    if pill_stats["debuff"] and pill_stats.get("avg_debuff_secs"):
                        _dl_div += f" (gem. {_fmt_hm(pill_stats['avg_debuff_secs'])} over)"
                    _sp_div.append(_dl_div)
                    _sp_div.append(f"⬜ Geen pil: **{pill_stats['none']}**")
                    pill_emb = discord.Embed(
                        title="📊 Pill stats — alle divisies",
                        description="\n".join(_sp_div),
                        colour=colour,
                    )
                    await ctx.send(embed=pill_emb)
                except Exception:
                    pass
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

        # Pill buff/debuff section — only for NL (tracking data is NL-only)
        nl_country_id_cfg = self.config.get("nl_country_id")
        if self._db and nl_country_id_cfg and cid == nl_country_id_cfg:
            try:
                pill_stats = await self._db.get_pill_stats_from_tracking(country_id=cid)
                _sp2: list[str] = []
                _bl2 = f"� Pil actief: **{pill_stats['buff']}**"
                if pill_stats['buff'] and pill_stats.get('avg_buff_secs'):
                    _bl2 += f" (gem. {_fmt_hm(pill_stats['avg_buff_secs'])} over)"
                _sp2.append(_bl2)
                _dl2 = f"🤢 In debuff: **{pill_stats['debuff']}**"
                if pill_stats['debuff'] and pill_stats.get('avg_debuff_secs'):
                    _dl2 += f" (gem. {_fmt_hm(pill_stats['avg_debuff_secs'])} over)"
                _sp2.append(_dl2)
                _sp2.append(f"⬜ Geen pil: **{pill_stats['none']}**")
                pill_emb = discord.Embed(
                    title=f"📊 Stats — {country_name}",
                    description="\n".join(_sp2),
                    colour=colour,
                )
                await ctx.send(embed=pill_emb)
            except Exception:
                pass


async def setup(bot) -> None:
    """Add the ParaatheidCog to the bot."""
    await bot.add_cog(ParaatheadCog(bot))
