"""Background tasks and slash commands for per-channel MU subscriptions.

Two features:
  1. /weekschade mu:<name>  — post weekly damage for a MU every Monday at 01:00
     NL time (just before the 02:00 weekly-damage reset).  Run the same command
     again in the same channel to unsubscribe.

  2. /contract_gewonnen mu:<name>  — ping the channel whenever the chosen MU
     wins a mercenary-contract auction.  Polled every minute.  Run again to stop.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context

from cogs.tasks._base import TaskCogBase
from cogs.tasks.war_guild_divisions import DIVISION_MUS
from services.damage_calc import fmt_damage

logger = logging.getLogger("discord_bot")

_TZ_NL = ZoneInfo("Europe/Amsterdam")
_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"


# ── Shared helpers (inlined from mudmg to avoid cross-cog imports) ────────────

def _mu_weekly_total(mu_data: dict) -> float:
    obj = mu_data.get("mu") if isinstance(mu_data.get("mu"), dict) else mu_data
    rankings = obj.get("rankings") if isinstance(obj, dict) else None
    if not isinstance(rankings, dict):
        return 0.0
    weekly_obj = rankings.get("muWeeklyDamages") or {}
    return float(weekly_obj.get("value") or 0)


def _mu_members(mu_data: dict) -> list[str]:
    obj = mu_data.get("mu") if isinstance(mu_data.get("mu"), dict) else mu_data
    if not isinstance(obj, dict):
        return []
    members = obj.get("members") or []
    return [str(m) for m in members if m]


def _unwrap_auctions(resp: object) -> list:
    if not isinstance(resp, dict):
        return []
    inner = resp.get("result", resp)
    data = inner.get("data", inner) if isinstance(inner, dict) else resp
    if isinstance(data, dict):
        return (
            data.get("auctions")
            or data.get("items")
            or data.get("docs")
            or (data.get("data") if isinstance(data.get("data"), list) else None)
            or []
        )
    if isinstance(data, list):
        return data
    return []


# ── Autocomplete ──────────────────────────────────────────────────────────────

async def _mu_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest MU names from the known_mus DB table, falling back to DIVISION_MUS."""
    db = getattr(interaction.client, "_ext_db", None)
    if db:
        try:
            names = await db.get_known_mu_names(current)
            if names:
                return [app_commands.Choice(name=n, value=n) for n in names[:25]]
        except Exception:
            pass
    all_names = [n for names in DIVISION_MUS.values() for n in names]
    q = current.strip().lower()
    return [
        app_commands.Choice(name=n, value=n) for n in all_names if q in n.lower()
    ][:25]


# ── Cog ───────────────────────────────────────────────────────────────────────

class MuSubscriptionsCog(TaskCogBase, name="mu_subscriptions"):
    """Weekly MU damage reports and auction-win pings."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._country_names: dict[str, str] = {}
        self._country_names_refreshed_at: float = 0.0
        # In-memory dedup for the weekly posting window.
        # Keyed by (channel_id, mu_name); cleared when we exit the Monday 01:xx window.
        self._posted_this_week: set[tuple[str, str]] = set()

    async def _refresh_country_names(self) -> None:
        """Refresh country name cache at most once every 10 minutes."""
        if time.monotonic() - self._country_names_refreshed_at < 600:
            return
        try:
            resp = await self._client.get("/country.getAllCountries")
            inner = resp.get("result", resp) if isinstance(resp, dict) else {}
            data = inner.get("data", inner) if isinstance(inner, dict) else resp
            if isinstance(data, list):
                self._country_names = {
                    str(c["_id"]): c.get("name") or c.get("shortName") or str(c["_id"])
                    for c in data if isinstance(c, dict) and c.get("_id")
                }
                self._country_names_refreshed_at = time.monotonic()
        except Exception as exc:
            logger.warning("mu_subscriptions: country cache refresh failed: %s", exc)

    async def cog_load(self) -> None:
        self._weekly_check.start()
        self._auction_win_check.start()

    async def cog_unload(self) -> None:
        self._weekly_check.cancel()
        self._auction_win_check.cancel()

    # ── Helper: resolve MU name → (mu_id, canonical_name) ────────────────────

    async def _resolve_mu(self, mu_name: str) -> tuple[str | None, str | None]:
        """Look up a MU ID from the DB; return (mu_id, canonical_name) or (None, None)."""
        if not self._db:
            return None, None
        try:
            mu_id, canonical = await self._db.get_known_mu_by_name(mu_name)
            return mu_id, canonical
        except Exception as exc:
            logger.warning("mu_subscriptions: resolve_mu failed for %r: %s", mu_name, exc)
            return None, None

    # ── Weekly damage task ────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _weekly_check(self) -> None:
        if not self._db or not self._client:
            return
        nl_now = datetime.now(_TZ_NL)
        # Monday == 0, fire in the 01:00 hour
        if nl_now.weekday() != 0 or nl_now.hour != 1:
            # Outside the posting window — clear the set so it's fresh for next Monday.
            if self._posted_this_week:
                self._posted_this_week.clear()
            return
        try:
            subs = await self._db.get_all_weekly_report_subs()
        except Exception as exc:
            logger.warning("mu_subscriptions: failed to load weekly subs: %s", exc)
            return
        for sub in subs:
            key = (sub["channel_id"], sub["mu_name"])
            # In-memory check: if we already posted this run of the Monday window,
            # skip regardless of whether the DB write succeeded (prevents spam when
            # update_weekly_report_posted fails due to a locked DB).
            if key in self._posted_this_week:
                continue
            last = sub.get("last_posted_at")
            if last:
                try:
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - last_dt).total_seconds() < 6 * 24 * 3600:
                        self._posted_this_week.add(key)
                        continue  # already posted within the last 6 days
                except (ValueError, TypeError):
                    pass
            # Mark before posting so that even if the DB write fails on return,
            # the next minute's iteration won't re-post.
            self._posted_this_week.add(key)
            try:
                await self._post_weekly_damage(sub)
            except Exception as exc:
                logger.warning(
                    "mu_subscriptions: weekly post failed for %s in channel %s: %s",
                    sub["mu_name"], sub["channel_id"], exc,
                )

    @_weekly_check.before_loop
    async def _before_weekly_check(self) -> None:
        await self._wait_for_services()

    async def _post_weekly_damage(self, sub: dict) -> None:
        channel = self.bot.get_channel(int(sub["channel_id"]))
        if channel is None:
            return

        mu_id = sub["mu_id"]
        mu_name = sub["mu_name"]

        try:
            results = await self._client.batch_get("/mu.getById", [{"muId": mu_id}])
            mu_data = results[0] if results else None
        except Exception as exc:
            logger.warning("mu_subscriptions: batch_get failed for %s: %s", mu_name, exc)
            return

        if not isinstance(mu_data, dict):
            return

        weekly_total = _mu_weekly_total(mu_data)
        members = _mu_members(mu_data)

        weekly_map: dict[str, tuple[str, float]] = {}
        level_map: dict[str, int] = {}
        if self._db and members:
            try:
                weekly_map = await self._db.get_weekly_damages_for_users(members)
            except Exception as exc:
                logger.warning("mu_subscriptions: weekly_map DB lookup failed: %s", exc)
            try:
                level_map = await self._db.get_levels_for_users(members)
            except Exception as exc:
                logger.warning("mu_subscriptions: level DB lookup failed: %s", exc)

        rows: list[tuple[str, float, int]] = []
        for uid in members:
            if uid in weekly_map:
                name, weekly = weekly_map[uid]
                if weekly > 0:
                    level = level_map.get(uid, 0)
                    rows.append((name, weekly, level))
        rows.sort(key=lambda r: r[1], reverse=True)

        if rows:
            name_w = min(max(len(r[0]) for r in rows), 16)
            name_w = max(name_w, 4)
            W = 10
            L = 3
            E = 8
            header = (
                f"{'#':>3}  {'Naam':<{name_w}}  {'Wekelijks':>{W}}"
                f"  {'Lvl':>{L}}  {'W/Per lvl':>{E}}"
            )
            sep = "─" * len(header)
            lines = [header, sep]
            for i, (name, weekly, level) in enumerate(rows, 1):
                display = name[:name_w] if len(name) > name_w else name
                l_str = str(level) if level else "─"
                e_str = fmt_damage(weekly / level) if weekly and level else "─"
                lines.append(
                    f"{i:>3}  {display:<{name_w}}  {fmt_damage(weekly):>{W}}"
                    f"  {l_str:>{L}}  {e_str:>{E}}"
                )
            table = "```\n" + "\n".join(lines) + "\n```"
        else:
            table = "*Geen schadedata beschikbaar.*"

        footer_line = f"Totaal MU: {fmt_damage(weekly_total)} · Wekelijkse schade reset om 02:00 NL-tijd"
        content = f"**⚔️ Weekschade {mu_name}**\n{table}\n_{footer_line}_"

        try:
            await channel.send(content)
        except Exception as exc:
            logger.warning(
                "mu_subscriptions: failed to send weekly embed for %s: %s", mu_name, exc
            )
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.update_weekly_report_posted(sub["channel_id"], mu_name, now_iso)
        except Exception as exc:
            logger.warning(
                "mu_subscriptions: failed to persist last_posted_at for %s: %s", mu_name, exc
            )

    # ── Auction-win task ──────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _auction_win_check(self) -> None:
        if not self._db or not self._client:
            return
        await self._refresh_country_names()
        try:
            subs = await self._db.get_all_auction_win_subs()
        except Exception as exc:
            logger.warning("mu_subscriptions: failed to load auction subs: %s", exc)
            return
        for sub in subs:
            try:
                await self._check_auction_wins(sub)
            except Exception as exc:
                logger.warning(
                    "mu_subscriptions: auction check failed for %s: %s", sub["mu_name"], exc
                )

    @_auction_win_check.before_loop
    async def _before_auction_win_check(self) -> None:
        await self._wait_for_services()

    async def _check_auction_wins(self, sub: dict) -> None:
        mu_id = sub["mu_id"]
        mu_name = sub["mu_name"]
        # cutoff_at stores the MongoDB _id of the newest contract known at subscribe time.
        # Only contracts with _id strictly greater (i.e. created later) are posted.
        # ObjectID lexicographic order == chronological order (first 4 bytes = Unix timestamp).
        cutoff = sub["cutoff_at"]

        try:
            resp = await self._client.post(
                "/mercenaryContractAuction.getPaginatedAuctions",
                json={"status": "won", "limit": 20, "page": 1},
            )
            auctions = _unwrap_auctions(resp)
        except Exception as exc:
            logger.warning(
                "mu_subscriptions: auction API failed for %s: %s", mu_name, exc
            )
            return

        channel = self.bot.get_channel(int(sub["channel_id"]))
        if channel is None:
            return

        seen: set[str] = set(sub["seen_ids"])
        new_auctions = []
        updated_seen = list(sub["seen_ids"])
        for auction in auctions:
            auction_id = str(auction.get("_id") or "")
            if not auction_id or auction_id in seen:
                continue
            # Client-side filter: only contracts won by our subscribed MU
            if str(auction.get("currentWinner") or "") != mu_id:
                continue
            # Skip anything whose ObjectID is at or before the cutoff ID
            if cutoff and auction_id <= cutoff:
                continue
            new_auctions.append(auction)
            seen.add(auction_id)
            updated_seen.append(auction_id)

        if not new_auctions:
            return

        for auction in new_auctions:
            await self._post_auction_won(channel, mu_name, auction)

        await self._db.update_auction_seen_ids(sub["channel_id"], mu_name, updated_seen)
        # Advance the cutoff to the newest _id posted so the baseline moves forward.
        new_cutoff = max(str(a.get("_id") or "") for a in new_auctions)
        if new_cutoff > cutoff:
            await self._db.update_auction_cutoff(sub["channel_id"], mu_name, new_cutoff)

    async def _post_auction_won(
        self, channel: discord.abc.Messageable, mu_name: str, auction: dict
    ) -> None:
        battle_id = str(auction.get("battle") or "")
        payout = float(auction.get("currentPayout") or auction.get("budget") or 0)
        per_k = float(auction.get("currentPerK") or auction.get("initialPerK") or 0)
        min_dmg = int(auction.get("minimumDamage") or 0)
        country_id = str(auction.get("country") or "")
        won_at_str = str(auction.get("wonAt") or auction.get("updatedAt") or "")

        country_name = self._country_names.get(country_id, country_id[:8] if country_id else "")

        lines: list[str] = [f"**MU:** {mu_name}"]
        if country_name:
            lines.append(f"**Land:** {country_name}")
        if per_k > 0:
            lines.append(f"**Vergoeding:** {per_k:g} CC per 1k schade")
        if payout > 0:
            lines.append(f"**Uitbetaling:** {payout:,.0f} CC")
        if min_dmg > 0:
            lines.append(f"**Minimale schade:** {min_dmg:,}")
        if won_at_str:
            try:
                won_dt = datetime.fromisoformat(won_at_str.replace("Z", "+00:00"))
                lines.append(f"**Gewonnen:** <t:{int(won_dt.timestamp())}:f>")
            except (ValueError, TypeError):
                pass

        embed = discord.Embed(
            title=f"🏆 Contract gewonnen: {mu_name}",
            description="\n".join(lines),
            url=_BATTLE_URL.format(battle_id=battle_id) if battle_id else None,
            colour=discord.Colour(0x2ECC71),
        )

        try:
            await channel.send(embed=embed)
        except Exception as exc:
            logger.warning(
                "mu_subscriptions: failed to send auction embed for %s: %s", mu_name, exc
            )

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(
        name="weekschade",
        description="Abonneer dit kanaal op wekelijkse MU-schade (elke maandag 01:00 NL). Opnieuw uitvoeren stopt het.",
    )
    @app_commands.describe(mu="Naam van de MU")
    @app_commands.autocomplete(mu=_mu_autocomplete)
    async def weekschade(self, interaction: discord.Interaction, mu: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._db:
            await interaction.followup.send("❌ Database niet beschikbaar.", ephemeral=True)
            return

        mu_id, canonical = await self._resolve_mu(mu)
        if not mu_id:
            await interaction.followup.send(
                f"❌ MU **{mu}** niet gevonden in de database.\n"
                "Zorg dat de MU-scan is uitgevoerd (`/syncwar` of wacht op de dagelijkse scan).",
                ephemeral=True,
            )
            return

        mu_name = canonical or mu
        channel_id = str(interaction.channel_id)
        guild_id = str(interaction.guild_id or "")

        if await self._db.has_weekly_report_sub(channel_id, mu_name):
            await self._db.remove_weekly_report_sub(channel_id, mu_name)
            await interaction.followup.send(
                f"✅ Afgemeld voor wekelijkse schade-updates van **{mu_name}** in dit kanaal.",
                ephemeral=True,
            )
        else:
            await self._db.add_weekly_report_sub(channel_id, guild_id, mu_name, mu_id)
            await interaction.followup.send(
                f"✅ Aangemeld voor wekelijkse schade-updates van **{mu_name}**.\n"
                "Elke maandag om **01:00 NL-tijd** wordt er hier een overzicht gepost "
                "(net voor de reset om 02:00).",
                ephemeral=True,
            )

    @app_commands.command(
        name="contract_gewonnen",
        description="Ping dit kanaal wanneer een MU een auctiecontract wint. Opnieuw uitvoeren stopt het.",
    )
    @app_commands.describe(mu="Naam van de MU")
    @app_commands.autocomplete(mu=_mu_autocomplete)
    async def contract_gewonnen(self, interaction: discord.Interaction, mu: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._db:
            await interaction.followup.send("❌ Database niet beschikbaar.", ephemeral=True)
            return

        mu_id, canonical = await self._resolve_mu(mu)
        if not mu_id:
            await interaction.followup.send(
                f"❌ MU **{mu}** niet gevonden in de database.\n"
                "Zorg dat de MU-scan is uitgevoerd (`/syncwar` of wacht op de dagelijkse scan).",
                ephemeral=True,
            )
            return

        mu_name = canonical or mu
        channel_id = str(interaction.channel_id)
        guild_id = str(interaction.guild_id or "")

        if await self._db.has_auction_win_sub(channel_id, mu_name):
            await self._db.remove_auction_win_sub(channel_id, mu_name)
            await interaction.followup.send(
                f"✅ Afgemeld voor contractmeldingen van **{mu_name}** in dit kanaal.",
                ephemeral=True,
            )
        else:
            # Determine cutoff: the MongoDB _id of the newest already-won contract.
            # ObjectID lexicographic order == chronological order, so any contract
            # with _id > cutoff was created after this subscription and will be posted.
            # Falls back to "" (post everything) if no contracts exist yet.
            cutoff_at: str = ""
            if self._client:
                try:
                    resp = await self._client.post(
                        "/mercenaryContractAuction.getPaginatedAuctions",
                        json={"status": "won", "limit": 50, "page": 1},
                    )
                    ids = [
                        str(a.get("_id") or "")
                        for a in _unwrap_auctions(resp)
                        if a.get("_id") and str(a.get("currentWinner") or "") == mu_id
                    ]
                    if ids:
                        cutoff_at = max(ids)
                except Exception as exc:
                    logger.warning(
                        "mu_subscriptions: could not fetch cutoff for %s: %s", mu_name, exc
                    )

            await self._db.add_auction_win_sub(channel_id, guild_id, mu_name, mu_id, cutoff_at)
            await interaction.followup.send(
                f"✅ Aangemeld voor contractmeldingen van **{mu_name}**.\n"
                "Zodra deze MU een auctiecontract wint wordt dat hier gemeld.",
                ephemeral=True,
            )


    @app_commands.command(
        name="mu_abonnementen",
        description="Toon alle actieve weekschade- en contractmeldingen abonnementen.",
    )
    async def mu_abonnementen(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self._db:
            await interaction.followup.send("❌ Database niet beschikbaar.", ephemeral=True)
            return

        weekly_subs = await self._db.get_all_weekly_report_subs()
        auction_subs = await self._db.get_all_auction_win_subs()

        embed = discord.Embed(
            title="📋 Actieve MU-abonnementen",
            colour=self._embed_colour(),
        )

        if weekly_subs:
            lines = [
                f"<#{s['channel_id']}> — **{s['mu_name']}**"
                for s in sorted(weekly_subs, key=lambda s: s["mu_name"])
            ]
            embed.add_field(
                name=f"⚔️ Weekschade ({len(weekly_subs)})",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(name="⚔️ Weekschade", value="*Geen abonnementen.*", inline=False)

        if auction_subs:
            lines = [
                f"<#{s['channel_id']}> — **{s['mu_name']}**"
                for s in sorted(auction_subs, key=lambda s: s["mu_name"])
            ]
            embed.add_field(
                name=f"🏆 Contract gewonnen ({len(auction_subs)})",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="🏆 Contract gewonnen", value="*Geen abonnementen.*", inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


    @commands.command(name="weekschade_preview", hidden=True)
    @commands.check(lambda ctx: ctx.bot.is_owner(ctx.author) if not isinstance(ctx.author, discord.Member)
                    else ctx.bot.is_owner(ctx.author))
    async def weekschade_preview(self, ctx: Context, *, mu: str) -> None:
        """Preview the weekschade message for a MU in the current channel. Owner-only."""
        if not await ctx.bot.is_owner(ctx.author):
            await ctx.send("❌ Dit commando is alleen voor de bot-eigenaar.", delete_after=5)
            return
        if not self._db:
            await ctx.send("❌ Database niet beschikbaar.")
            return

        mu_id, canonical = await self._resolve_mu(mu)
        if not mu_id:
            await ctx.send(f"❌ MU **{mu}** niet gevonden in de database.")
            return

        mu_name = canonical or mu
        fake_sub = {
            "channel_id": str(ctx.channel.id),
            "mu_id": mu_id,
            "mu_name": mu_name,
        }
        await ctx.send(f"*Preview weekschade voor **{mu_name}**:*")
        await self._post_weekly_damage(fake_sub)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MuSubscriptionsCog(bot))
