"""Hidden admin command: DM verified citizens who own iron factories outside Nigeria.

Nigeria is being promoted as the country with the best Iron production bonus
in WarEra; this campaign nudges every verified citizen who still has iron
factories elsewhere to relocate them, so the tax revenue flows to Nigeria
(and therefore, indirectly, the Netherlands) instead of a foreign nation.

Discovery works exactly as instructed: for every citizen the guild has
verified (``identity_links``), call ``company.getCompanies`` filtered by
their WarEra user ID (perPage=100 — the "high limit" needed so one call
covers virtually every player), then batch ``company.getById`` on every
company found to read its item code, region (resolved to a country via
``region.getRegionsObject``) and disabled state. This mirrors the same
per-owner ``company.getCompanies`` query ``services/full_fetcher.py``'s
company-census reconciliation phase already confirmed reliable (unlike the
unfiltered listing, which is known to silently skip companies).

Two commands share that one discovery routine (``_find_iron_targets``):
  /nigeria-ijzer-campagne         — actually DMs every qualifying citizen.
  /nigeria-ijzer-campagne-test    — prints the same target list in the
                                     current channel and DMs only the bot
                                     owner (PrinceRealMarijn) as a preview,
                                     without touching anyone else's inbox.

Both are owner/admin-only (``utils.checks.is_owner_or_admin``) and
registered guild-only, matching the convention other sensitive owner
commands (``cogs/owner.py``) use.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from utils.checks import is_owner_or_admin

logger = logging.getLogger("discord_bot")

NIGERIA_COUNTRY_ID = "683ddd2c24b5a2e114af15fa"
IRON_ITEM_CODE = "iron"
_COMPANY_BATCH = 100

# The test command only ever DMs the bot owner, regardless of who is
# actually found to have foreign iron factories, so the content can be
# previewed without touching anyone else's inbox.
# PrinceRealMarijn — WarEra ID 695579c03f0cff6efb694b3a.
TEST_DM_DISCORD_ID = 565626197048819731


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


def _paginate(lines: list[str], max_len: int = 3900) -> list[str]:
    """Chunk *lines* into blocks that fit an embed description."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        add = len(line) + 1
        if current and current_len + add > max_len:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += add
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


class NigeriaIronCampaignCog(CommandCogBase, name="nigeria_iron_campaign"):
    """Owner-only campaign to nudge citizens to move iron factories to Nigeria."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── shared discovery ─────────────────────────────────────────────

    async def _find_iron_targets(self) -> tuple[list[dict], dict]:
        """Return (targets, stats).

        *targets* is a list of dicts (discord_id, warera_id, citizen_name,
        foreign_iron_count) — one entry per verified citizen who owns at
        least one non-disabled iron factory outside Nigeria.
        """
        guild_id = str(self.config.get("guild_id") or "")
        links = await self._db.get_identity_links_for_guild(guild_id)
        links = [link for link in links if link.get("in_game_user_id")]
        if not links:
            return [], {"verified": 0, "companies_checked": 0, "truncated_owners": 0}

        warera_ids = [link["in_game_user_id"] for link in links]
        names = await self._db.get_citizen_names_by_discord_ids(
            [link["discord_user_id"] for link in links]
        )

        # region → controlling country, needed to resolve a company's location
        raw_regions = await self._client.get("/region.getRegionsObject")
        regions = _unwrap_trpc(raw_regions)
        region_country: dict[str, str] = {}
        if isinstance(regions, dict):
            for rid, robj in regions.items():
                if not isinstance(robj, dict):
                    continue
                country = robj.get("country")
                if isinstance(country, dict):
                    country = country.get("_id") or country.get("id")
                if country:
                    region_country[str(rid)] = str(country)

        # company.getCompanies?userId=X for every verified citizen. A citizen
        # owning >100 companies would need a second page we don't chase —
        # rare enough for an individual player that logging it is enough.
        owner_company_ids: dict[str, list[str]] = {}
        truncated_owners = 0
        for start in range(0, len(warera_ids), _COMPANY_BATCH):
            chunk = warera_ids[start : start + _COMPANY_BATCH]
            results = await self._client.batch_get(
                "/company.getCompanies",
                [{"userId": uid, "perPage": 100} for uid in chunk],
                batch_size=_COMPANY_BATCH,
            )
            for uid, raw in zip(chunk, results):
                data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
                ids = data.get("items") if isinstance(data, dict) else None
                owner_company_ids[uid] = [str(cid) for cid in ids] if isinstance(ids, list) else []
                if isinstance(data, dict) and (data.get("nextCursor") or data.get("cursor")):
                    truncated_owners += 1

        all_company_ids = sorted(
            {cid for ids in owner_company_ids.values() for cid in ids}
        )

        # company.getById for every discovered company, to read itemCode,
        # region (→ country) and disabledAt.
        company_info: dict[str, dict] = {}
        for start in range(0, len(all_company_ids), _COMPANY_BATCH):
            chunk = all_company_ids[start : start + _COMPANY_BATCH]
            results = await self._client.batch_get(
                "/company.getById",
                [{"companyId": cid} for cid in chunk],
                batch_size=_COMPANY_BATCH,
            )
            for cid, raw in zip(chunk, results):
                company = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
                if not isinstance(company, dict):
                    continue
                company_info[cid] = {
                    "item_code": str(company.get("itemCode") or ""),
                    "country_id": region_country.get(str(company.get("region") or "")),
                    "disabled": bool(company.get("disabledAt")),
                }

        targets: list[dict] = []
        for link in links:
            uid = link["in_game_user_id"]
            discord_id = link["discord_user_id"]
            foreign_iron = 0
            for cid in owner_company_ids.get(uid, []):
                info = company_info.get(cid)
                if not info or info["disabled"]:
                    continue
                if info["item_code"] != IRON_ITEM_CODE:
                    continue
                if info["country_id"] == NIGERIA_COUNTRY_ID:
                    continue
                foreign_iron += 1
            if foreign_iron > 0:
                targets.append({
                    "discord_id": discord_id,
                    "warera_id": uid,
                    "citizen_name": names.get(discord_id) or uid,
                    "foreign_iron_count": foreign_iron,
                })

        stats = {
            "verified": len(links),
            "companies_checked": len(all_company_ids),
            "truncated_owners": truncated_owners,
        }
        return targets, stats

    # ── DM content ────────────────────────────────────────────────────

    def _build_campaign_embed(self, foreign_count: int, *, is_test: bool = False) -> discord.Embed:
        nl_plural = "en" if foreign_count != 1 else ""
        nl_verb = "staan" if foreign_count != 1 else "staat"
        en_plural = "ies" if foreign_count != 1 else "y"
        en_verb = "are" if foreign_count != 1 else "is"

        description = (
            "**🇳🇱 Nederlands**\n"
            "Nigeria — onze proxynatie — heeft momenteel de hoogste productiebonus op **IJzer** "
            "van heel WarEra. Door je ijzerfabriek te verplaatsen naar Nigeria profiteer je zelf "
            "van deze bonus, én blijft alle belasting die je betaalt binnen onze eigen kring: "
            "Nigeria (en daarmee indirect ook Nederland) ontvangt zo alle belastinginkomsten, "
            "in plaats van een vreemde natie.\n\n"
            f"Je hebt op dit moment **{foreign_count}** ijzerfabriek{nl_plural} die niet in "
            f"Nigeria {nl_verb}. We raden je sterk aan deze te verplaatsen naar de regio "
            "**Abuja** in Nigeria zodra dat mogelijk is.\n\n"
            "Vragen? Stel ze gerust in de server.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "**🇬🇧 English**\n"
            "Nigeria — our proxy nation — currently has the highest production bonus on **Iron** "
            "in all of WarEra. By moving your iron factory to Nigeria you benefit from this bonus "
            "yourself, and all the tax you pay stays within our own circle: Nigeria (and therefore "
            "indirectly the Netherlands too) receives all the tax revenue, instead of a foreign "
            "nation.\n\n"
            f"You currently have **{foreign_count}** iron factor{en_plural} that {en_verb} not "
            "located in Nigeria. We strongly recommend relocating them to the **Abuja** region "
            "in Nigeria as soon as possible.\n\n"
            "Questions? Feel free to ask in the server."
        )
        if is_test:
            description = "🧪 **TESTBERICHT / TEST MESSAGE — preview only**\n\n" + description

        embed = discord.Embed(
            title="🇳🇬 Verplaats je ijzerfabriek naar Nigeria! / Move your iron factory to Nigeria!",
            description=description,
            colour=discord.Colour.green(),
        )
        embed.set_footer(text="WareraNL — Nigeria IJzer Campagne")
        return embed

    # ── real command ──────────────────────────────────────────────────

    @app_commands.command(
        name="nigeria-ijzer-campagne",
        description="[Admin] DM alle geverifieerde leden met ijzerfabrieken buiten Nigeria.",
    )
    @is_owner_or_admin()
    async def nigeria_ijzer_campagne(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ Alleen bruikbaar in de server zelf.")
            return
        if not self._db or not self._client:
            await interaction.followup.send("❌ Database of API-client nog niet gereed.")
            return

        targets, stats = await self._find_iron_targets()
        if not targets:
            await interaction.followup.send(
                f"✅ Niemand gevonden met ijzerfabrieken buiten Nigeria "
                f"({stats['verified']} geverifieerde leden gecontroleerd, "
                f"{stats['companies_checked']} bedrijven gecheckt)."
            )
            return

        sent = 0
        failed = 0
        failed_mentions: list[str] = []
        for target in targets:
            member = guild.get_member(int(target["discord_id"]))
            try:
                user = member or await self.bot.fetch_user(int(target["discord_id"]))
            except (discord.NotFound, discord.HTTPException):
                failed += 1
                failed_mentions.append(f"<@{target['discord_id']}>")
                continue

            embed = self._build_campaign_embed(target["foreign_iron_count"])
            try:
                await user.send(embed=embed)
                sent += 1
                logger.info(
                    "nigeria_ijzer_campagne: DM sent to %s (%s)",
                    user, target["discord_id"],
                )
            except discord.Forbidden:
                logger.warning(
                    "nigeria_ijzer_campagne: cannot DM %s (%s) — DMs disabled",
                    user, target["discord_id"],
                )
                failed += 1
                failed_mentions.append(f"<@{target['discord_id']}>")
            except discord.HTTPException as exc:
                logger.error(
                    "nigeria_ijzer_campagne: DM failed for %s: %s",
                    target["discord_id"], exc,
                )
                failed += 1
                failed_mentions.append(f"<@{target['discord_id']}>")
            await asyncio.sleep(1)

        summary = (
            f"📨 **Campagne voltooid**\n"
            f"Geverifieerde leden gecontroleerd: **{stats['verified']}**\n"
            f"Bedrijven gecheckt: **{stats['companies_checked']}**\n"
            f"Doelwitten (ijzerfabriek buiten Nigeria): **{len(targets)}**\n"
            f"DM verzonden: **{sent}**\n"
            f"DM's dicht/mislukt: **{failed}**"
        )
        if failed_mentions:
            summary += "\n\nNiet bereikt: " + ", ".join(failed_mentions[:30])
            if len(failed_mentions) > 30:
                summary += f" (+{len(failed_mentions) - 30} meer)"
        await interaction.followup.send(summary)

    # ── test command ──────────────────────────────────────────────────

    @app_commands.command(
        name="nigeria-ijzer-campagne-test",
        description="[Admin] Toon de doelgroep van de ijzer-campagne (dry-run, DM alleen naar jou).",
    )
    @is_owner_or_admin()
    async def nigeria_ijzer_campagne_test(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        if not self._db or not self._client:
            await interaction.followup.send("❌ Database of API-client nog niet gereed.")
            return
        if interaction.channel is None:
            await interaction.followup.send("❌ Alleen bruikbaar in een kanaal.")
            return

        targets, stats = await self._find_iron_targets()

        header = (
            f"🧪 **Testmodus — er worden geen DM's verzonden, behalve één preview naar jou**\n"
            f"Geverifieerde leden gecontroleerd: **{stats['verified']}**\n"
            f"Bedrijven gecheckt: **{stats['companies_checked']}**\n"
            f"Doelwitten (ijzerfabriek buiten Nigeria): **{len(targets)}**"
        )
        await interaction.followup.send(header)

        if targets:
            lines = [
                f"• {t['citizen_name']} (<@{t['discord_id']}>) — "
                f"{t['foreign_iron_count']} ijzerfabriek(en) buiten Nigeria"
                for t in targets
            ]
            for chunk in _paginate(lines):
                embed = discord.Embed(description=chunk, colour=discord.Colour.orange())
                await interaction.channel.send(embed=embed)

        # Preview DM — always to the bot owner, never to the actual targets.
        preview_count = next(
            (
                t["foreign_iron_count"]
                for t in targets
                if str(t["discord_id"]) == str(TEST_DM_DISCORD_ID)
            ),
            1,
        )
        try:
            owner_user = await self.bot.fetch_user(TEST_DM_DISCORD_ID)
            await owner_user.send(embed=self._build_campaign_embed(preview_count, is_test=True))
            await interaction.followup.send("✅ Preview-DM verzonden.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(f"⚠️ Preview-DM kon niet worden verzonden: {exc}")


async def setup(bot) -> None:
    """Add the campaign cog to the bot, guild-only (owner/admin command)."""
    guild_id = int(bot.config.get("guild_id") or 0)
    guilds = [discord.Object(id=guild_id)] if guild_id else []
    await bot.add_cog(NigeriaIronCampaignCog(bot), guilds=guilds or None)
