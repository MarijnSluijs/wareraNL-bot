"""Proxy opportunity command based on country population and government vacancies."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list

logger = logging.getLogger("discord_bot")


class ProxyCog(CommandCogBase, name="proxy"):
    """Commands for finding proxy opportunities."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @staticmethod
    def _unwrap_data(payload: object) -> object:
        """Best-effort unwrapping of tRPC-style envelopes."""
        if isinstance(payload, dict):
            result = payload.get("result")
            if isinstance(result, dict) and "data" in result:
                return result.get("data")
            if "data" in payload:
                return payload.get("data")
        return payload

    @staticmethod
    def _country_citizen_count(country: dict) -> int | None:
        """Extract active citizen count from country payload.

        Primary source: rankings.countryActivePopulation.value
        """
        rankings = country.get("rankings")
        if isinstance(rankings, dict):
            active_population = rankings.get("countryActivePopulation")
            if isinstance(active_population, dict):
                try:
                    return int(active_population.get("value"))
                except (TypeError, ValueError):
                    return None

    @staticmethod
    def _has_person(value: object) -> bool:
        """Return True when a government role field contains a valid user id."""
        if not isinstance(value, str):
            return False
        text = value.strip().lower()
        return bool(text) and text not in {"none", "null"}

    async def _get_government_presence_batched(
        self, country_ids: list[str]
    ) -> dict[str, tuple[bool, bool]]:
        """Return country_id -> (has_president, has_vice_president)."""
        if not country_ids:
            return {}

        data_by_country: dict[str, tuple[bool, bool]] = {}
        inputs = [{"countryId": cid} for cid in country_ids]

        try:
            if hasattr(self._client, "batch_get"):
                responses = await self._client.batch_get(
                    "government.getByCountryId",
                    inputs,
                    batch_size=50,
                )
            else:
                responses = []
                for cid in country_ids:
                    response = await self._client.get(
                        "/government.getByCountryId",
                        params={"input": '{"countryId":"' + cid + '"}'},
                    )
                    responses.append(response)
        except Exception:
            logger.exception("proxy: batched government lookup failed")
            return data_by_country

        for cid, response in zip(country_ids, responses, strict=False):
            data = self._unwrap_data(response)
            if not isinstance(data, dict):
                continue

            has_president = self._has_person(data.get("president"))
            has_vice_president = self._has_person(data.get("vicePresident"))
            data_by_country[cid] = (has_president, has_vice_president)

        return data_by_country

    @app_commands.command(
        name="proxy",
        description="Toon landen met proxy-kansen op basis van bevolking en regering.",
    )
    @app_commands.describe(
        limit="Max aantal resultaten (1-50).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def proxy_opportunities(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 25,
    ) -> None:
        """Find countries matching proxy opportunity criteria."""
        await interaction.response.defer(thinking=True)

        if not self._client:
            await self._send_api_offline(interaction)
            return

        try:
            response = await self._client.get("/country.getAllCountries")
            countries = extract_country_list(response)
        except Exception:
            await self._send_api_offline(interaction)
            return

        if not countries:
            await interaction.followup.send("Geen landen ontvangen van de API.")
            return

        opportunities: list[dict] = []
        small_countries: list[dict] = []
        for country in countries:
            count = self._country_citizen_count(country)
            if count is None:
                continue

            if count < 5:
                small_countries.append(country)

        gov_check_candidates: list[tuple[str, str, int, str]] = []

        for country in small_countries:
            count = self._country_citizen_count(country)
            if count is None:
                continue

            cid = cid_of(country)
            name = str(country.get("name") or cid)
            link = f"https://app.warera.io/country/{cid}"

            if count <= 2:
                opportunities.append(
                    {
                        "name": name,
                        "count": count,
                        "reason": "<= 2 active citizens",
                        "link": link,
                    }
                )
                continue

            gov_check_candidates.append((cid, name, count, link))

        gov_presence = await self._get_government_presence_batched(
            [cid for cid, _name, _count, _link in gov_check_candidates]
        )

        for cid, name, count, link in gov_check_candidates:
            presence = gov_presence.get(cid)
            if presence is None:
                logger.warning(
                    "proxy: skipping %s (%s) because government data is unavailable",
                    name,
                    cid,
                )
                continue

            has_president, has_vice_president = presence
            if not has_president and not has_vice_president:
                opportunities.append(
                    {
                        "name": name,
                        "count": count,
                        "reason": "<= 4 active citizens and no president/vice president",
                        "link": link,
                    }
                )

        opportunities.sort(key=lambda item: (item["count"], item["name"].lower()))

        if not opportunities:
            await interaction.followup.send(
                "Geen proxy opportunities gevonden met de huidige criteria."
            )
            return

        shown = opportunities[: int(limit)]
        lines = [
            f"- [{item['name']}]({item['link']}) - {item['count']} citizens ({item['reason']})"
            for item in shown
        ]
        description = "\n".join(lines)

        embed = discord.Embed(
            title="🎯 Proxy Opportunities",
            description=description,
            color=discord.Color.orange(),
        )
        embed.set_footer(
            text=f"Toont {len(shown)} van {len(opportunities)} gevonden landen"
        )
        await interaction.followup.send(embed=embed)


async def setup(bot) -> None:
    """Add the ProxyCog to the bot."""
    await bot.add_cog(ProxyCog(bot))
