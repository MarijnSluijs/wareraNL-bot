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

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.tasks._base import TaskCogBase
from cogs.tasks.war_guild_divisions import DIVISION_MUS

logger = logging.getLogger("discord_bot")

# poll_state keys
_KEY_DASH_PAR = "wg_dash_paraatheid_msg_id"


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
    def _dashboard_channel_id(self) -> int:
        return int(self._war_cfg["dashboard_channel_id"])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def cog_load(self) -> None:
        self._dashboard_task.start()

    def cog_unload(self) -> None:
        self._dashboard_task.cancel()

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
        await self._upsert_dashboard_message(channel, _KEY_DASH_PAR, paraatheid_embeds)

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
        """Build an embed showing NL MU readiness grouped by division."""
        try:
            mu_stats = await self._db.get_all_mu_readiness()
        except Exception as exc:
            logger.error("war_guild_status: get_all_mu_readiness failed: %s", exc)
            return []

        name_w = 16
        hdr = f"{'naam':<{name_w}}  {'par':>5}  {'kan':>3}  {'≥15':>3}  {'≥20':>3}  {'avg':>5}"
        sep = "─" * len(hdr)

        div_labels = {
            1: "🥇 Divisie 1",
            2: "🥈 Divisie 2",
            3: "🥉 Divisie 3",
            4: "4️⃣ Divisie 4",
            5: "5️⃣ Divisie 5",
        }

        now_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
        emb = discord.Embed(
            title="📊 Paraatheid — Alle NL Divisies",
            description=(
                "par = paraat/totaal  •  kan = kan resetten  •  "
                "≥15/≥20 = paraat op dat level  •  avg = gem. eco-wachttijd"
            ),
            colour=discord.Colour(0xFFB612),
        )

        has_data = False
        for div_num in sorted(DIVISION_MUS.keys()):
            mu_names = DIVISION_MUS[div_num]
            field_label = div_labels.get(div_num, f"Divisie {div_num}")

            rows: list[str] = []
            total_par = total_total = total_kan = total_w15 = total_w20 = 0
            all_waiting: list[float] = []

            for mu_name in mu_names:
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


    # ── Manual refresh command ────────────────────────────────────────────────

    @app_commands.command(name="rfd", description="Refresh het war-guild dashboard.")
    @app_commands.default_permissions(administrator=True)
    async def rfd_slash(self, interaction: discord.Interaction) -> None:
        """Force-refresh the war-guild dashboard (admin only, ephemeral)."""
        await interaction.response.defer(ephemeral=True)
        await self._update_dashboard()
        await interaction.followup.send("✅ Dashboard ververst.", ephemeral=True)

    @commands.command(name="refreshdashboard", aliases=["rfd"])
    @commands.is_owner()
    async def refresh_dashboard(self, ctx: commands.Context) -> None:
        """Force-refresh the war-guild dashboard right now (owner only)."""
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass
        async with ctx.typing():
            await self._update_dashboard()

# ── Extension entry point ─────────────────────────────────────────────────────

async def setup(bot) -> None:
    """Only load when war_guild config is present and has the required channel IDs."""
    war_cfg = bot.config.get("war_guild")
    if not war_cfg:
        logger.debug("war_guild_status: no war_guild config — cog not loaded")
        return
    if not war_cfg.get("dashboard_channel_id"):
        logger.debug("war_guild_status: missing dashboard_channel_id in war_guild config — cog not loaded")
        return
    await bot.add_cog(WarGuildStatusCog(bot))
    logger.info("war_guild_status: cog loaded")
