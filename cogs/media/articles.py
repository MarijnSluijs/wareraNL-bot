"""
This module defines the ArticleScanner cog, which periodically polls the WarEra API for 
new Dutch-language articles and posts them to a Discord channel as embeds. 
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.api_client import APIClient
from services.db import Database

logger = logging.getLogger("discord_bot")

# WarEra in-game article URL template
_ARTICLE_URL = "https://app.warera.io/article/{article_id}"


async def _owner_check(interaction: discord.Interaction) -> bool:
    return await interaction.client.is_owner(interaction.user)


def _html_to_markdown(html: str, max_chars: int = 800) -> str:
    """Convert article HTML to Discord markdown, preserving bold/italic/newlines."""
    if not html:
        return ""
    text = html
    # Remove img tags (and any inline wrapper like <em><img/></em> that would leave orphan markers)
    text = re.sub(
        r"(<(?:em|i|b|strong|u)>\s*)?<img[^>]*>\s*(</(?:em|i|b|strong|u)>)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Block elements → newlines first
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</(h[1-6]|li|tr|div|blockquote)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<h[1-6][^>]*>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</h[1-6]>", "**\n", text, flags=re.IGNORECASE)
    # Inline formatting
    text = re.sub(r"<(b|strong)[^>]*>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</(b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"<(i|em)[^>]*>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"</(i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"<u[^>]*>", "__", text, flags=re.IGNORECASE)
    text = re.sub(r"</u>", "__", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'")
    # Remove empty markdown markers left behind by stripped tags (e.g. ** ** or * *)
    text = re.sub(r"\*{1,2}\s*\*{1,2}", "", text)
    text = re.sub(r"__\s*__", "", text)
    # Collapse excessive blank lines (max 2 consecutive newlines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\u2026"
    return text


def _extract_sentences(html: str, n: int = 4, max_chars: int = 800) -> str:
    """Convert HTML to markdown and return up to n sentences (hard-capped at max_chars)."""
    text = _html_to_markdown(html, max_chars=max_chars * 2)  # convert first, then trim
    if not text:
        return ""
    sentences: list[str] = []
    remaining = text
    for _ in range(n):
        m = re.search(r"[.!?](?:\s|$)", remaining)
        if m:
            sentences.append(remaining[: m.start() + 1].strip())
            remaining = remaining[m.end() :].strip()
        else:
            if remaining:
                sentences.append(remaining.strip())
            break
    result = "\n\n".join(s for s in sentences if s)
    if len(result) > max_chars:
        result = result[:max_chars].rstrip() + "\u2026"
    return result


def _extract_avatar(user_data: dict) -> str | None:
    """Try common field names for a player profile picture URL."""
    for key in (
        "avatarUrl",
        "profilePicture",
        "imageUrl",
        "image",
        "avatar",
        "photo",
        "picture",
        "thumbnailUrl",
    ):
        val = user_data.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None


def _unwrap(response) -> dict:
    """Unwrap a tRPC response envelope: {result: {data: ...}} → data."""
    if isinstance(response, dict):
        result = response.get("result")
        if isinstance(result, dict):
            return result.get("data") or {}
    return response if isinstance(response, dict) else {}


class ArticleScanner(commands.Cog, name="article_scanner"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.config = getattr(self.bot, "config", {}) or {}
        self._client: APIClient | None = None
        self._db: Database | None = None
        self._first_run: bool = True

    def cog_load(self) -> None:
        asyncio.create_task(self._ensure_services_and_start())

    def cog_unload(self) -> None:
        self.article_poll.cancel()
        if self._client:
            asyncio.create_task(self._client.close())

    async def _ensure_services_and_start(self) -> None:
        base_url = self.config.get("api_base_url", "https://api.example.local")
        db_path = self.config.get("articles_db_path", "database/articles.db")
        api_keys = None
        try:
            with open("_api_keys.json", "r") as kf:
                api_keys = json.load(kf).get("keys", [])
        except Exception:
            logger.debug("No _api_keys.json found or failed to parse")

        self._client = APIClient(base_url=base_url, api_keys=api_keys)
        await self._client.start()
        # Reuse the same external.db as the production poller
        self._db = Database(db_path)
        await self._db.setup()

        self.article_poll.start()

    # ------------------------------------------------------------------ #
    # Periodic poll                                                        #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=1)
    async def article_poll(self) -> None:
        """Fetch recent Dutch-language articles and post any we haven't seen yet."""
        if not self._client or not self._db:
            return
        try:
            await self._run_article_poll()
        except Exception:
            logger.exception("Article poll failed")

    @article_poll.before_loop
    async def before_article_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Core poll logic                                                      #
    # ------------------------------------------------------------------ #

    async def _run_article_poll(self) -> None:
        channel_id = self.config.get("channels", {}).get("articles")
        if not channel_id:
            logger.warning("No 'articles' channel configured — skipping article poll")
            return

        # Fetch the most recent Dutch-language articles
        try:
            resp = await self._client.get(
                "/article.getArticlesPaginated",
                params={
                    "input": json.dumps({"type": "last", "limit": 20})
                },  # no language filter — filter by author citizenship instead
            )
        except Exception as exc:
            logger.warning("Failed to fetch articles: %s", exc)
            return

        data = _unwrap(resp)
        items = data.get("items") or data.get("articles") or []
        if not items:
            logger.debug("Article poll: no items in response")
            return

        if self._first_run:
            self._first_run = False

            # If the articles channel is empty, post the newest article so the
            # channel is never left blank after a fresh deploy / restart.
            channel_empty = True
            for guild in self.bot.guilds:
                channel = guild.get_channel(channel_id)
                if channel:
                    try:
                        history = [
                            m
                            async for m in channel.history(limit=50)
                            if m.author == self.bot.user
                        ]
                        if history:
                            channel_empty = False
                            break
                    except Exception:
                        pass  # no permission to read history — assume not empty

            if channel_empty and items:
                # Post up to N Dutch articles on empty startup.
                # Scan all fetched items (newest-first) until we've successfully
                # posted startup_count articles — skipping non-Dutch authors.
                startup_count = self.config.get("articles_startup_count", 3)
                posted_count = 0
                for candidate in items:
                    if posted_count >= startup_count:
                        break
                    aid = str(candidate.get("id") or candidate.get("_id") or "")
                    if not aid:
                        continue
                    logger.debug("Article poll: startup — trying article %s", aid)
                    success = await self._post_article(candidate, aid, channel_id)
                    if success:
                        posted_count += 1
                    await asyncio.sleep(1)
                logger.info(
                    "Article poll: startup scan complete — posted %d article(s)",
                    posted_count,
                )

            # Mark all fetched articles as seen to avoid re-posting on next tick.
            for article in items:
                aid = str(article.get("id") or article.get("_id") or "")
                if aid:
                    await self._db.mark_article_seen(aid)
            logger.info(
                "Article poll: initial run — marked %d articles as seen", len(items)
            )
            return

        # Process newest-first; items are typically newest-first from the API
        for article in items:
            aid = str(article.get("id") or article.get("_id") or "")
            if not aid:
                continue
            if await self._db.has_seen_article(aid):
                continue

            # New article — fetch full details for content
            await self._post_article(article, aid, channel_id)
            await self._db.mark_article_seen(aid)
            # Small delay to avoid overwhelming Discord if multiple new articles arrive at once
            await asyncio.sleep(1)

    async def _post_article(self, lite: dict, article_id: str, channel_id: int) -> bool:
        """Fetch full article and post an embed to the articles channel.

        Returns True if the article was posted, False if it was skipped
        (e.g. author is not a Dutch citizen).
        """
        # Try to get full article content
        full: dict = {}
        try:
            resp = await self._client.get(
                "/article.getArticleById",
                params={"input": json.dumps({"articleId": article_id})},
            )
            full = _unwrap(resp)
        except Exception as exc:
            logger.warning("Could not fetch full article %s: %s", article_id, exc)
            full = lite  # fall back to lite data

        # ---- title ----
        title = full.get("title") or lite.get("title") or "Onbekende titel"

        # ---- author: the field is a user ID string ----
        raw_author = full.get("author") or lite.get("author") or ""
        author_id = (
            raw_author
            if isinstance(raw_author, str)
            else (raw_author.get("id") or raw_author.get("_id") or "")
        )

        player_name = "Onbekend"
        avatar_url: str | None = None
        if author_id:
            try:
                user_resp = await self._client.get(
                    "/user.getUserLite",
                    params={"input": json.dumps({"userId": author_id})},
                )
                user_data = _unwrap(user_resp)
                if isinstance(user_data, dict):
                    # Country check — only post articles by Dutch citizens
                    nl_country_id = self.config.get("nl_country_id")
                    if nl_country_id:
                        author_country = user_data.get("country", "")
                        if author_country != nl_country_id:
                            logger.debug(
                                "Skipping article %s — author %s is from country %s (not NL)",
                                article_id,
                                author_id,
                                author_country,
                            )
                            return False

                    for key in ("name", "username", "displayName", "nick"):
                        val = user_data.get(key)
                        if isinstance(val, str) and val:
                            player_name = val
                            break
                    avatar_url = _extract_avatar(user_data)
            except Exception as exc:
                logger.warning("Could not fetch user %s: %s", author_id, exc)

        # Fallback if getUserLite didn't return a name
        if player_name == "Onbekend" and isinstance(raw_author, dict):
            player_name = (
                raw_author.get("username")
                or raw_author.get("name")
                or raw_author.get("displayName")
                or "Onbekend"
            )

        # ---- content preview (up to 4 sentences) ----
        content_raw = (
            full.get("content")
            or full.get("body")
            or full.get("text")
            or lite.get("excerpt")
            or ""
        )
        preview = _html_to_markdown(content_raw)

        # ---- publish timestamp ----
        published_at: datetime | None = None
        for ts_key in ("publishedAt", "createdAt", "date", "updatedAt"):
            ts_val = full.get(ts_key) or lite.get(ts_key)
            if isinstance(ts_val, str):
                try:
                    published_at = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                    break
                except ValueError:
                    pass

        article_url = _ARTICLE_URL.format(article_id=article_id)

        # ---- build embed ----
        embed = discord.Embed(
            title=title,
            url=article_url,
            description=preview or "*Geen voorvertoning beschikbaar.*",
            colour=discord.Color.from_rgb(255, 182, 18),
            timestamp=published_at or datetime.now(timezone.utc),
        )
        embed.set_author(
            name=f"✍️ {player_name}",
            icon_url=avatar_url,
        )
        embed.set_footer(text="WarEra — Nieuw artikel")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        # ---- "Lees meer" button ----
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Lees meer",
                url=article_url,
                style=discord.ButtonStyle.link,
            )
        )

        # Post to all guilds that have this channel
        posted = False
        for guild in self.bot.guilds:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed, view=view)
                    posted = True
                    logger.info(
                        "Posted article %s ('%s' by %s) to guild %s",
                        article_id,
                        title,
                        player_name,
                        guild.name,
                    )
                except Exception:
                    logger.exception(
                        "Failed to post article %s to guild %s", article_id, guild.name
                    )

        if not posted:
            logger.warning(
                "Article %s not posted — channel %d not found in any guild",
                article_id,
                channel_id,
            )

        return posted

    # ------------------------------------------------------------------ #
    # Test command                                                         #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="nieuwste_artikel",
        description="[TEST] Post het nieuwste Nederlandse artikel naar het artikelkanaal.",
    )
    @app_commands.check(_owner_check)
    async def nieuwste_artikel(self, interaction: discord.Interaction) -> None:
        """Fetch and post the single newest Dutch article. Test server only."""
        if not self.bot.testing:
            await interaction.response.send_message(
                "Dit commando is alleen beschikbaar op de testserver.", ephemeral=True
            )
            return

        if not self._client or not self._db:
            await interaction.response.send_message(
                "Services zijn nog niet gereed, probeer het later opnieuw.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            resp = await self._client.get(
                "/article.getArticlesPaginated",
                params={
                    "input": json.dumps({"type": "last", "limit": 1})
                },  # no language filter
            )
        except Exception as exc:
            await interaction.followup.send(f"API-fout: {exc}", ephemeral=True)
            return

        data = _unwrap(resp)
        items = data.get("items") or data.get("articles") or []
        if not items:
            await interaction.followup.send("Geen artikelen gevonden.", ephemeral=True)
            return

        channel_id = self.config.get("channels", {}).get("articles")
        if not channel_id:
            await interaction.followup.send(
                "Geen artikelkanaal geconfigureerd.", ephemeral=True
            )
            return

        article = items[0]
        aid = str(article.get("id") or article.get("_id") or "")
        await self._post_article(article, aid, channel_id)
        await interaction.followup.send(
            f"Artikel `{aid}` gepost naar <#{channel_id}>.", ephemeral=True
        )


async def setup(bot) -> None:
    """Add the ArticleScanner cog to the bot."""
    await bot.add_cog(ArticleScanner(bot))
