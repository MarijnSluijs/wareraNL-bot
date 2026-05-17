"""War-guild status cog.

Two responsibilities:
  1. Post (and keep alive) a persistent "Ready voor war / Eco nodig" button
     message in the war-status channel.  Choices are stored in the DB.
  2. Post one paraatheid overview + one war-status overview in the dashboard
     channel, and edit them hourly with fresh data.

Only loaded when ``config["war_guild"]`` is present (same guard as war_sync).
Required war_guild config keys:
    guild_id              — war guild Discord ID
    war_status_channel_id — channel ID for the war-status buttons
    dashboard_channel_id  — channel ID for the hourly dashboard
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# poll_state keys
_KEY_STATUS_MSG  = "wg_war_status_msg_id"
_KEY_DASH_PAR    = "wg_dash_paraatheid_msg_id"
_KEY_DASH_WS     = "wg_dash_warstatus_msg_id"


# ── Persistent button view ────────────────────────────────────────────────────

class WarStatusView(discord.ui.View):
    """Persistent war-readiness buttons (timeout=None survives bot restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Ready voor war",
        style=discord.ButtonStyle.success,
        custom_id="wg_status_ready_v1",
    )
    async def ready_button(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await _handle_status(interaction, "ready")

    @discord.ui.button(
        label="🌾 Eco nodig",
        style=discord.ButtonStyle.secondary,
        custom_id="wg_status_eco_v1",
    )
    async def eco_button(
        self, interaction: discord.Interaction, _btn: discord.ui.Button
    ) -> None:
        await _handle_status(interaction, "eco")


async def _handle_status(interaction: discord.Interaction, choice: str) -> None:
    db = getattr(interaction.client, "_ext_db", None)
    if db is None:
        await interaction.response.send_message(
            "❌ Database niet beschikbaar.", ephemeral=True
        )
        return
    await db.upsert_war_status(str(interaction.user.id), choice)
    label = "✅ Ready voor war" if choice == "ready" else "🌾 Eco nodig"
    await interaction.response.send_message(
        f"Jouw status is ingesteld op: **{label}**", ephemeral=True
    )


# ── Main cog ─────────────────────────────────────────────────────────────────

class WarGuildStatusCog(TaskCogBase, name="war_guild_status"):
    """Manages war-status buttons and the hourly dashboard in the war guild."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._war_cfg: dict = bot.config["war_guild"]

    # ── Config helpers ────────────────────────────────────────────────────────

    @property
    def _war_guild(self) -> Optional[discord.Guild]:
        return self.bot.get_guild(int(self._war_cfg["guild_id"]))

    @property
    def _war_status_channel_id(self) -> int:
        return int(self._war_cfg["war_status_channel_id"])

    @property
    def _dashboard_channel_id(self) -> int:
        return int(self._war_cfg["dashboard_channel_id"])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def cog_load(self) -> None:
        self.bot.add_view(WarStatusView())
        asyncio.create_task(self._deferred_startup())
        self._dashboard_task.start()

    def cog_unload(self) -> None:
        self._dashboard_task.cancel()

    async def _deferred_startup(self) -> None:
        await self._wait_for_services()
        await self._ensure_war_status_message()

    # ── War-status button message ─────────────────────────────────────────────

    async def _ensure_war_status_message(self) -> None:
        guild = self._war_guild
        if not guild:
            logger.warning(
                "war_guild_status: war guild %s not found", self._war_cfg.get("guild_id")
            )
            return
        channel = guild.get_channel(self._war_status_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "war_guild_status: war_status channel %d not found",
                self._war_status_channel_id,
            )
            return

        if self._db:
            stored = await self._db.get_poll_state(_KEY_STATUS_MSG)
            if stored:
                try:
                    await channel.fetch_message(int(stored))
                    logger.info(
                        "war_guild_status: war_status message %s still exists", stored
                    )
                    return
                except discord.NotFound:
                    pass

        embed = discord.Embed(
            title="⚔️ War Status",
            description=(
                "Geef aan of je klaar bent om te vechten, of nog eco nodig hebt.\n\n"
                "Klik op de knop die bij jouw situatie past. "
                "Je kunt je keuze altijd bijwerken door opnieuw te klikken."
            ),
            colour=discord.Colour(0xFF6600),
        )
        embed.set_footer(text="Jouw keuze wordt opgeslagen en getoond op het dashboard.")
        msg = await channel.send(embed=embed, view=WarStatusView())
        if self._db:
            await self._db.set_poll_state(_KEY_STATUS_MSG, str(msg.id))
        logger.info("war_guild_status: posted war_status message %d", msg.id)

    # ── Hourly dashboard task ─────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def _dashboard_task(self) -> None:
        try:
            await self._update_dashboard()
        except Exception:
            logger.exception("war_guild_status: dashboard update error")

    @_dashboard_task.before_loop
    async def _before_dashboard_task(self) -> None:
        await self._wait_for_services()

    async def _update_dashboard(self) -> None:
        guild = self._war_guild
        if not guild or not self._db:
            return
        channel = guild.get_channel(self._dashboard_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "war_guild_status: dashboard channel %d not found",
                self._dashboard_channel_id,
            )
            return

        paraatheid_embeds = await self._build_paraatheid_embeds()
        warstatus_embed = await self._build_warstatus_embed()

        await self._upsert_dashboard_message(channel, _KEY_DASH_PAR, paraatheid_embeds)
        await self._upsert_dashboard_message(channel, _KEY_DASH_WS, [warstatus_embed])

    async def _upsert_dashboard_message(
        self,
        channel: discord.TextChannel,
        poll_key: str,
        embeds: list[discord.Embed],
    ) -> None:
        """Edit the stored message in-place, or post a new one."""
        if not embeds:
            return
        # Clamp to Discord's 10-embed-per-message limit
        embeds = embeds[:10]
        if self._db:
            stored = await self._db.get_poll_state(poll_key)
            if stored:
                try:
                    msg = await channel.fetch_message(int(stored))
                    await msg.edit(embeds=embeds)
                    return
                except discord.NotFound:
                    pass
        msg = await channel.send(embeds=embeds)
        if self._db:
            await self._db.set_poll_state(poll_key, str(msg.id))
        logger.info(
            "war_guild_status: posted dashboard message %d (key=%s)", msg.id, poll_key
        )

    # ── Paraatheid embed builder ──────────────────────────────────────────────

    async def _build_paraatheid_embeds(self) -> list[discord.Embed]:
        """Build an embed showing NL MU readiness grouped by type (nl_mus mode)."""
        testing = getattr(self.bot, "testing", False)
        mus_json = "templates/mus.testing.json" if testing else "templates/mus.json"
        try:
            with open(mus_json, encoding="utf-8") as f:
                mus_data = json.load(f)
        except Exception as exc:
            logger.error("war_guild_status: cannot read %s: %s", mus_json, exc)
            return []

        mu_types: dict[str, str] = {}
        entries = [e for e in mus_data.get("embeds", []) if isinstance(e, dict)]
        for entry in entries:
            name = str(entry.get("name") or f"MU {str(entry.get('id', ''))[:8]}")
            type_raw = str(entry.get("type", "")).strip().lower()
            if type_raw == "elite":
                mu_types[name] = "Elite MU"
            elif type_raw == "eco":
                mu_types[name] = "Eco MU"
            else:
                mu_types[name] = "Standaard MU"

        # Backward compat: old schema uses title + description[**...**]
        if not mu_types:
            for emb in entries:
                title = emb.get("title", "")
                m = re.search(r"\[\*\*(.+?)\*\*\]", emb.get("description", ""))
                mu_types[title] = m.group(1) if m else "Standaard MU"
        if not mu_types:
            return []

        try:
            mu_stats = await self._db.get_all_mu_readiness()
        except Exception as exc:
            logger.error("war_guild_status: get_all_mu_readiness failed: %s", exc)
            return []

        name_w = 16
        hdr = f"{'naam':<{name_w}}  {'par':>5}  {'kan':>3}  {'≥15':>3}  {'≥20':>3}  {'avg':>5}"
        sep = "─" * len(hdr)

        cat_cfg = [
            ("Elite MU", "🟠 Elite MU"),
            ("Eco MU", "🟢 Eco MU"),
            ("Standaard MU", "🔵 Standaard MU"),
        ]

        now_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
        emb = discord.Embed(
            title="📊 Paraatheid — Alle NL MUs",
            description=(
                "par = paraat/totaal  •  kan = kan resetten  •  "
                "≥15/≥20 = paraat op dat level  •  avg = gem. eco-wachttijd"
            ),
            colour=discord.Colour(0xFFB612),
        )

        has_data = False
        for mu_type, field_label in cat_cfg:
            mu_names_of_type = [n for n, t in mu_types.items() if t == mu_type]
            if not mu_names_of_type:
                continue

            rows: list[str] = []
            total_par = total_total = total_kan = total_w15 = total_w20 = 0
            all_waiting: list[float] = []

            for mu_name in mu_names_of_type:
                stats = mu_stats.get(mu_name)
                if stats is None:
                    rows.append(
                        f"{mu_name[:name_w]:<{name_w}}  "
                        f"{'?':>5}  {'?':>3}  {'?':>3}  {'?':>3}  {'?':>5}"
                    )
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
                    rows.append(
                        f"{mu_name[:name_w]:<{name_w}}  "
                        f"{par_str:>5}  {kan_str:>3}  {w15_str:>3}  {w20_str:>3}  {avg_str:>5}"
                    )
                    total_par += stats["war"]
                    total_total += stats["total"]
                    total_kan += stats["can_reset"]
                    total_w15 += stats.get("war_15", 0)
                    total_w20 += stats.get("war_20", 0)
                    all_waiting.extend(stats["waiting_days"])

            if total_total:
                tot_avg_str = (
                    f"{max(0.0, 7 - sum(all_waiting) / len(all_waiting)):.1f}d"
                    if all_waiting
                    else "—"
                )
                rows.append("─" * len(hdr))
                rows.append(
                    f"{'totaal':<{name_w}}  "
                    f"{total_par}/{total_total:>3}  {total_kan:>3}  "
                    f"{total_w15:>3}  {total_w20:>3}  {tot_avg_str:>5}"
                )

            # Split into chunks that each fit within Discord's 1024-char field limit
            FIELD_MAX = 1024
            prefix = "```\n" + hdr + "\n" + sep + "\n"
            suffix = "\n```"
            overhead = len(prefix) + len(suffix)
            chunks: list[list[str]] = []
            current: list[str] = []
            current_len = 0
            for row in rows:
                row_len = len(row) + 1  # +1 for \n
                if current and overhead + current_len + row_len > FIELD_MAX:
                    chunks.append(current)
                    current = []
                    current_len = 0
                current.append(row)
                current_len += row_len
            if current:
                chunks.append(current)

            for i, chunk in enumerate(chunks):
                block = prefix + "\n".join(chunk) + suffix
                label = field_label if i == 0 else f"{field_label} (vervolg)"
                emb.add_field(name=label, value=block, inline=False)
            has_data = True

        if not has_data:
            return []

        emb.set_footer(
            text=f"Bijgewerkt: {now_str} UTC  •  Automatisch elk uur vernieuwd"
        )
        return [emb]

    # ── War-status embed builder ──────────────────────────────────────────────

    async def _build_warstatus_embed(self) -> discord.Embed:
        """Build an embed showing war-status choices grouped per MU."""
        now_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
        colour = discord.Colour(0xFFB612)

        try:
            rows = await self._db.get_war_status_by_mu()
        except Exception as exc:
            logger.error("war_guild_status: get_war_status_by_mu failed: %s", exc)
            return discord.Embed(
                title="⚔️ War Status — per MU",
                description="❌ Kon data niet ophalen.",
                colour=colour,
            )

        # Aggregate: {mu_name: {ready: int, eco: int}}
        by_mu: dict[str, dict[str, int]] = {}
        for row in rows:
            mu = row["mu_name"]
            choice = row["choice"]
            cnt = row["count"]
            if mu not in by_mu:
                by_mu[mu] = {"ready": 0, "eco": 0}
            if choice in by_mu[mu]:
                by_mu[mu][choice] = cnt

        emb = discord.Embed(title="⚔️ War Status — per MU", colour=colour)

        if not by_mu:
            emb.description = "_Nog geen statussen ingesteld._"
            emb.set_footer(text=f"Bijgewerkt: {now_str} UTC")
            return emb

        total_ready = total_eco = 0
        lines: list[str] = []
        for mu_name in sorted(by_mu.keys()):
            r = by_mu[mu_name].get("ready", 0)
            e = by_mu[mu_name].get("eco", 0)
            total = r + e
            lines.append(
                f"**{mu_name}**  —  ✅ {r} ready  •  🌾 {e} eco  *(totaal: {total})*"
            )
            total_ready += r
            total_eco += e

        grand_total = total_ready + total_eco
        summary = (
            f"✅ **{total_ready}** ready  •  🌾 **{total_eco}** eco  "
            f"*(van {grand_total} spelers)*"
        )
        emb.description = summary + "\n\n" + "\n".join(lines)
        emb.set_footer(
            text=f"Bijgewerkt: {now_str} UTC  •  Automatisch elk uur vernieuwd"
        )
        return emb


    # ── Manual refresh command ────────────────────────────────────────────────

    @commands.command(name="refreshdashboard", aliases=["rfd"])
    @commands.is_owner()
    async def refresh_dashboard(self, ctx: commands.Context) -> None:
        """Force-refresh the war-guild dashboard right now (owner only)."""
        async with ctx.typing():
            await self._update_dashboard()
        await ctx.message.add_reaction("✅")


# ── Extension entry point ─────────────────────────────────────────────────────

async def setup(bot) -> None:
    """Only load when war_guild config is present and has the required channel IDs."""
    war_cfg = bot.config.get("war_guild")
    if not war_cfg:
        logger.debug("war_guild_status: no war_guild config — cog not loaded")
        return
    if not war_cfg.get("war_status_channel_id") or not war_cfg.get("dashboard_channel_id"):
        logger.debug("war_guild_status: missing channel IDs in war_guild config — cog not loaded")
        return
    await bot.add_cog(WarGuildStatusCog(bot))
    logger.info("war_guild_status: cog loaded")
