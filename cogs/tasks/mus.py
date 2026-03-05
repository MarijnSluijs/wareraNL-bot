"""Background task and helper methods for synchronizing MU metadata."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")


def mus_path(testing: bool = False) -> str:
    return "templates/mus.testing.json" if testing else "templates/mus.json"


def _normalize_mu_type(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"elite", "elite mu"}:
        return "Elite"
    if raw in {"eco", "eco mu"}:
        return "Eco"
    if raw in {"standaard", "standard", "standaard mu", "standard mu"}:
        return "Standaard"
    return "Standaard"


class MUTasks(TaskCogBase, name="mu_tasks"):
    _DEFAULT_CACHE_SECONDS = 300

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.mu_refresh.start()

    def cog_unload(self) -> None:
        self.mu_refresh.cancel()

    @tasks.loop(hours=1)
    async def mu_refresh(self) -> None:
        try:
            await self.refresh_mu_info()
        except Exception:
            logger.exception("mu_refresh: unexpected error")

    @mu_refresh.before_loop
    async def before_mu_refresh(self) -> None:
        await self._wait_for_services()

    @staticmethod
    def _parse_updated_at(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _normalize_entries(self, raw_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in raw_entries:
            if not isinstance(item, dict):
                continue

            mu_id = str(item.get("id") or "").strip()
            if not mu_id:
                description = str(item.get("description", ""))
                match = re.search(r"/mu/([A-Za-z0-9]+)", description)
                if match:
                    mu_id = match.group(1)
            if not mu_id or mu_id in seen:
                continue

            mu_type = _normalize_mu_type(item.get("type"))
            if "type" not in item:
                description = str(item.get("description", "")).lower()
                if "elite" in description:
                    mu_type = "Elite"
                elif "eco" in description:
                    mu_type = "Eco"

            role_raw = item.get("role_id", 0)
            try:
                role_id = int(role_raw) if role_raw else 0
            except (TypeError, ValueError):
                role_id = 0

            normalized_item: dict[str, Any] = {
                "id": mu_id,
                "type": mu_type,
                "role_id": role_id,
            }

            if item.get("name"):
                normalized_item["name"] = str(item.get("name"))
            elif item.get("title"):
                normalized_item["name"] = str(item.get("title"))

            if item.get("thumbnail"):
                normalized_item["thumbnail"] = str(item.get("thumbnail"))

            normalized.append(normalized_item)
            seen.add(mu_id)

        return normalized

    async def refresh_mu_info(
        self,
        *,
        force: bool = False,
        min_age_seconds: int = _DEFAULT_CACHE_SECONDS,
    ) -> dict[str, Any]:
        """Refresh MU names/thumbnails from API and persist to mus.json.

        Uses a cache guard: when data is newer than *min_age_seconds*, refresh is skipped
        unless *force* is True.
        """
        path = mus_path(getattr(self.bot, "testing", False))

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {"embeds": [], "posted_message_ids": []}
        except Exception as exc:
            logger.warning("refresh_mu_info: failed to read %s: %s", path, exc)
            data = {"embeds": [], "posted_message_ids": []}

        existing_entries = self._normalize_entries(data.get("embeds", []))
        has_missing_metadata = any(
            not str(entry.get("name") or "").strip() or not str(entry.get("thumbnail") or "").strip()
            for entry in existing_entries
        )

        now = datetime.now(timezone.utc)
        last_updated = self._parse_updated_at(data.get("mu_info_updated_at"))
        if not force and last_updated is not None:
            age_seconds = max(0.0, (now - last_updated).total_seconds())
            if age_seconds < max(0, min_age_seconds) and not has_missing_metadata:
                return {
                    "path": path,
                    "updated": 0,
                    "entries": len(data.get("embeds", [])),
                    "skipped": True,
                    "age_seconds": age_seconds,
                }

        entries = self._normalize_entries(data.get("embeds", []))
        if not entries:
            data["embeds"] = []
            data.setdefault("posted_message_ids", [])
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            return {"path": path, "updated": 0, "entries": 0}

        if not self._client:
            logger.warning("refresh_mu_info: API client not ready")
            data["embeds"] = entries
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            return {"path": path, "updated": 0, "entries": len(entries)}

        mu_ids = [entry["id"] for entry in entries]
        inputs = [{"muId": mu_id} for mu_id in mu_ids]

        try:
            results = await self._client.batch_get(
                "/mu.getById",
                inputs,
                batch_size=100,
                chunk_sleep=0.0,
            )
        except Exception as exc:
            logger.warning("refresh_mu_info: batch_get failed: %s", exc)
            results = [None] * len(mu_ids)

        updated = 0
        for entry, payload in zip(entries, results):
            if not isinstance(payload, dict):
                continue

            live_name = payload.get("name") or payload.get("title")
            live_thumbnail = payload.get("avatarUrl") or payload.get("thumbnail")

            if isinstance(payload.get("mu"), dict):
                mu_obj = payload["mu"]
                live_name = live_name or mu_obj.get("name")
                live_thumbnail = live_thumbnail or mu_obj.get("avatarUrl")

            if live_name and entry.get("name") != str(live_name):
                entry["name"] = str(live_name)
                updated += 1
            if live_thumbnail and entry.get("thumbnail") != str(live_thumbnail):
                entry["thumbnail"] = str(live_thumbnail)
                updated += 1

        data["embeds"] = entries
        data.setdefault("posted_message_ids", [])
        data["mu_info_updated_at"] = now.isoformat()

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        logger.info("refresh_mu_info: %d entries, %d fields updated", len(entries), updated)
        return {
            "path": path,
            "updated": updated,
            "entries": len(entries),
            "skipped": False,
            "age_seconds": 0.0,
        }

    @app_commands.command(
        name="refreshmuinfo",
        description="Forceer direct een refresh van MU namen en thumbnails.",
    )
    @has_privileged_role()
    async def refreshmuinfo(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.refresh_mu_info(force=True)
            await interaction.followup.send(
                (
                    "✅ MU-info geforceerd ververst "
                    f"({result.get('entries', 0)} entries, {result.get('updated', 0)} velden aangepast)."
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ MU refresh mislukt: {exc}",
                ephemeral=True,
            )


async def setup(bot) -> None:
    """Add the MUTasks cog to the bot."""
    await bot.add_cog(MUTasks(bot))
