"""
This module defines the RedditTracker cog, which polls the WarEraNL subreddit for new posts and forwards them
to a configured Discord channel. Includes !reddit_poll (owner-only) to manually
trigger a poll cycle.
"""

import asyncio
import datetime
import html
import json
import logging
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
import feedparser
import httpx
import requests
import yarl
from curl_cffi.requests import AsyncSession
from discord.ext import commands, tasks

DEFAULT_HEADERS = {
    "User-Agent": "WareraNLBot/1.0 (by /u/Creepino +https://github.com/colgre/wareraNL-bot)"
}


logger = logging.getLogger("discord_bot")


class RedditTracker(commands.Cog, name="reddit"):
    """Cog that polls a subreddit and posts new submissions to a channel.

    Behavior:
    - Polls `/r/<subreddit>/new.json` every `reddit_poll_interval_seconds` (config)
    - Stores last-seen post id in `data/reddit_state.json`
    - Posts new submissions (oldest first) to channel configured in
      `bot.config['channels']['reddit']` or falls back to `channels.production`.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = bot.logger if hasattr(bot, "logger") else logger
        self.subreddit = "WarEraNL"
        self.interval = 600  # 10 minutes
        self.state_path = Path("data") / "reddit_state.json"
        self.session = aiohttp.ClientSession(
            trust_env=True, headers=DEFAULT_HEADERS, version=aiohttp.HttpVersion11
        )
        self._last_seen: str | None = None
        self.poll_task = tasks.loop(seconds=self.interval)(self._poll)

    async def cog_load(self) -> None:
        # Ensure data directory exists and load last seen id
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_seen = data.get("last_post_id")
                    self.logger.debug(
                        f"Loaded reddit state, last_post_id={self._last_seen}"
                    )
            except Exception:
                self.logger.exception("Failed to read reddit state file")
        else:
            self._last_seen = None

        self.session = aiohttp.ClientSession(
            trust_env=True, headers=DEFAULT_HEADERS, version=aiohttp.HttpVersion11
        )
        # Start the polling loop immediately but ensure the first iteration
        # waits until the bot is fully ready (non-blocking for startup).
        self.poll_task.before_loop(self._poll_before_loop)
        self.poll_task.start()

    async def cog_unload(self) -> None:
        # Cancel the polling task and close session
        try:
            if self.poll_task.is_running():
                self.poll_task.cancel()
        except Exception:
            pass
        if self.session:
            await self.session.close()

    async def _save_state(self) -> None:
        try:
            with self.state_path.open("w", encoding="utf-8") as f:
                json.dump({"last_post_id": self._last_seen}, f)
        except Exception:
            self.logger.exception("Failed to write reddit state file")

    async def _fetch_new(self) -> list[dict]:
        url = "https://rss.app/feeds/v1.1/QpzeuVUPxN7QYtaA.json"
        try:
            url = "https://reddit.com/r/wareranl/new/.rss"
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.google.com/",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
            }
            feed = None
            async with AsyncSession() as s:
                # 'impersonate' makes your script look like a real Chrome browser
                response = await s.get(
                    url, impersonate="chrome", headers=headers, timeout=15
                )

                if response.status_code == 200:
                    feed = feedparser.parse(response.text)
                else:
                    print(f"Error {response.status_code}: Could not access subreddit")

            if feed:
                posts = []
                for entry in feed.entries[:1]:
                    p: dict = {}
                    # id or link as unique name
                    p["name"] = entry.get("id")
                    p["title"] = entry.get("title") or ""
                    # link field in this feed is full URL
                    p["permalink"] = entry.get("link")
                    p["url"] = p.get("permalink")
                    p["selftext"] = entry.get("content") or ""
                    p["created"] = entry.get("published_parsed")
                    p["author"] = entry.get("author")
                    p["image"] = entry.get("media_thumbnail")
                    posts.append(p)
                self.logger.debug(f"Fetched {len(posts)} posts from reddit feed")
                return posts
            return []
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.exception("Failed to fetch reddit feed: %s", e)
            return []

    async def _poll(self) -> None:
        posts = await self._fetch_new()
        if not posts:
            self.logger.warning("No posts found")
            return

        # Collect posts newer than last_seen; Reddit returns newest first
        self.logger.info(
            f"Processing {len(posts)} posts from reddit feed, last_seen={self._last_seen}"
        )
        # Determine which posts are newer than the last seen post.
        # Feed order may be newest-first or oldest-first; try to detect
        # using available timestamps and slice accordingly.
        if self._last_seen is None:
            new_posts = posts.copy()
        else:
            idx = next(
                (i for i, p in enumerate(posts) if p.get("name") == self._last_seen),
                None,
            )
            if idx is None:
                self.logger.info(
                    "Last seen post not present in feed; treating all fetched posts as new"
                )
                new_posts = posts.copy()
            else:
                # Try to detect ordering via timestamps if available
                def _ts(obj):
                    v = obj.get("created_utc")
                    return v if isinstance(v, (int, float)) else None

                first_ts = _ts(posts[0])
                last_ts = _ts(posts[-1])
                if first_ts is not None and last_ts is not None:
                    newest_first = first_ts >= last_ts
                else:
                    # Default to newest-first when unsure
                    newest_first = True
                if newest_first:
                    # posts[0] is newest — items before idx are newer
                    new_posts = posts[:idx]
                else:
                    # posts[-1] is newest — items after idx are newer
                    new_posts = posts[idx + 1 :]

        if not new_posts:
            self.logger.info("No new posts since last poll")
            return

        # Determine channel id
        self.logger.debug("Looking for reddit channel in bot config")
        channels_cfg = self.bot.config.get("channels", {})
        channel_id = channels_cfg.get("reddit")
        if not channel_id:
            self.logger.warning(
                "No `channels.reddit` or configured; skipping reddit posts"
            )
            return

        self.logger.debug(f"Finding channel in guilds {list(self.bot.guilds)}")
        for guild in self.bot.guilds:
            self.logger.debug(f"Looking for reddit channel in guild {guild.id}")
            channel = guild.get_channel(channel_id)

            if channel is None:
                self.logger.warning(f"Configured reddit channel {channel_id} not found")
                return

            for post in new_posts:
                self.logger.info(
                    f"Posting reddit submission {post.get('name')} to channel {channel.id}"
                )
                try:
                    self.logger.debug(f"Parsing post data: {post}")
                    title = post.get("title")
                    author = post.get("author")
                    self.logger.debug(
                        f"Parsed post data: title={title}, author={author}"
                    )
                    permalink = post.get("permalink")
                    url = post.get("url")
                    post_name = post.get("name")

                    self.logger.debug(
                        f"Creating embed for post: title={title}, author={author}, permalink={permalink}, url={url}"
                    )
                    # Sanitize title (Discord limits) and permalink (may be full URL or path)
                    title = title or "(no title)"
                    if len(title) > 256:
                        title = title[:253] + "…"

                    embed_url = None
                    if permalink:
                        try:
                            if isinstance(permalink, str) and (
                                permalink.startswith("http://")
                                or permalink.startswith("https://")
                            ):
                                embed_url = permalink
                            elif isinstance(permalink, str) and permalink.startswith(
                                "/"
                            ):
                                embed_url = "https://reddit.com" + permalink
                        except Exception:
                            embed_url = None

                    if embed_url:
                        embed = discord.Embed(
                            title=title, url=embed_url, color=0xFF4500
                        )
                    else:
                        embed = discord.Embed(title=title, color=0xFF4500)

                    if author:
                        try:
                            embed.set_author(name=f"{author}")
                        except Exception as e:
                            self.logger.warning(
                                f"Failed to set author for post {post_name}: {e}"
                            )
                    created_time = post.get("created")
                    if created_time:
                        # Always construct a timezone-aware datetime in UTC, then convert
                        # to the desired local timezone (Europe/Amsterdam) for display.
                        try:
                            if isinstance(created_time, (int, float)):
                                dt = datetime.datetime.fromtimestamp(
                                    created_time, tz=datetime.timezone.utc
                                )
                            elif isinstance(created_time, time.struct_time):
                                # struct_time is typically in UTC from feedparser
                                dt = datetime.datetime(
                                    *created_time[:6], tzinfo=datetime.timezone.utc
                                )
                            else:
                                # Fallback: try to parse as string timestamp
                                dt = datetime.datetime.fromisoformat(str(created_time))
                            # Convert to Europe/Amsterdam for NL local time
                            local_tz = ZoneInfo("Europe/Amsterdam")
                            dt = dt.astimezone(local_tz)
                        except Exception:
                            # As a fallback, assign without timezone
                            dt = None
                        if dt:
                            embed.timestamp = dt

                    selftext = post.get("selftext", "")
                    if selftext:
                        # feedparser may return a list of content dicts like
                        # [{'type': 'text/html', 'value': '<p>html...</p>'}]
                        # Normalize to a plain text string for Discord embeds.
                        if isinstance(selftext, list):
                            parts = []
                            for item in selftext:
                                if isinstance(item, dict):
                                    v = item.get("value") or item.get("text") or ""
                                else:
                                    v = str(item)
                                parts.append(v)
                            desc_text = "\n\n".join(parts)
                        elif isinstance(selftext, dict):
                            desc_text = (
                                selftext.get("value") or selftext.get("text") or ""
                            )
                        else:
                            desc_text = str(selftext)

                        # Unescape HTML entities
                        desc_text = html.unescape(desc_text)
                        # Replace common block-level tags and <br> with newlines to preserve paragraphs
                        desc_text = re.sub(
                            r"(?i)<\s*(br\s*/?|/p|p|/div|div|/li|li|/tr|tr|/td|td|h[1-6]|/h[1-6]|blockquote|/blockquote)[^>]*>",
                            "\n",
                            desc_text,
                        )
                        # Strip any remaining tags
                        desc_text = re.sub(r"<[^>]+>", "", desc_text)
                        # Remove reddit inline placeholders like [link] and [comments]
                        desc_text = re.sub(
                            r"\s*\[(?:link|comments)\]\s*",
                            " ",
                            desc_text,
                            flags=re.IGNORECASE,
                        )
                        # Remove 'submitted by /u/username' or similar variants
                        desc_text = re.sub(
                            r"\bsubmitted by\s*(?:/)?u/[A-Za-z0-9_-]+",
                            "",
                            desc_text,
                            flags=re.IGNORECASE,
                        )
                        desc_text = re.sub(
                            r"\bsubmitted by\s*[A-Za-z0-9_-]+",
                            "",
                            desc_text,
                            flags=re.IGNORECASE,
                        )
                        # Normalize spaces within lines (keep newlines)
                        desc_text = re.sub(r"[ \t]+", " ", desc_text)
                        # Normalize newlines: convert CRLF to LF and collapse multiple blank lines to two
                        desc_text = desc_text.replace("\r\n", "\n").replace("\r", "\n")
                        desc_text = re.sub(r"\n\s*\n+", "\n\n", desc_text).strip()

                        if desc_text:
                            description = (
                                (desc_text[:1900] + "…")
                                if len(desc_text) > 1900
                                else desc_text
                            )
                            embed.description = description

                    # For link/image posts show the URL (if valid)
                    if url:
                        try:
                            embed.add_field(name="Link", value=url, inline=False)
                        except Exception:
                            pass
                    # thumb = post.get("thumbnail")
                    # if isinstance(thumb, str) and thumb.startswith("http"):
                    # 	try:
                    # 		embed.set_thumbnail(url=thumb)
                    # 	except Exception as e:
                    # 		self.logger.warning(f"Failed to set thumbnail for post {post_name}: {e}")

                    # Try to find a larger image to show in the embed. Prefer explicit
                    # `image` or `thumbnail` fields (which may be a list/dict from feedparser),
                    # then attachments, then fall back to the first <img src="..."> in the
                    # original HTML content from the feed.
                    image_url = None
                    # Check explicit image/thumbnail fields
                    for key in (
                        "image",
                        "thumbnail",
                        "media_thumbnail",
                        "media_content",
                    ):
                        v = post.get(key)
                        if isinstance(v, str) and v.startswith("http"):
                            image_url = v
                            self.logger.debug(
                                f"Found image URL in post {post_name} field '{key}': {image_url}"
                            )
                            break
                        if isinstance(v, dict):
                            # common shape: {'url': 'https://...'}
                            for k in ("url", "src", "value"):
                                if (
                                    v.get(k)
                                    and isinstance(v.get(k), str)
                                    and v.get(k).startswith("http")
                                ):
                                    image_url = v.get(k)
                                    self.logger.debug(
                                        f"Found image URL in post {post_name} field '{key}' dict: {image_url}"
                                    )
                                    break
                            if image_url:
                                break
                        if isinstance(v, list):
                            for item in v:
                                if isinstance(item, str) and item.startswith("http"):
                                    image_url = item
                                    break
                                if isinstance(item, dict):
                                    for k in ("url", "src", "value"):
                                        if (
                                            item.get(k)
                                            and isinstance(item.get(k), str)
                                            and item.get(k).startswith("http")
                                        ):
                                            image_url = item.get(k)
                                            break
                                if image_url:
                                    self.logger.debug(
                                        f"Found image URL in post {post_name} field '{key}' list: {image_url}"
                                    )
                                    break

                    # Check attachments list
                    if not image_url:
                        attachments = post.get("attachments", [])
                        for a in attachments:
                            if (
                                isinstance(a, str)
                                and a.startswith("http")
                                and re.search(
                                    r"\.(jpg|jpeg|png|gif)$", a, re.IGNORECASE
                                )
                            ):
                                image_url = a
                                self.logger.debug(
                                    f"Found image URL in post {post_name} attachments: {image_url}"
                                )
                                break
                            if isinstance(a, dict):
                                for k in ("url", "src", "value"):
                                    if (
                                        a.get(k)
                                        and isinstance(a.get(k), str)
                                        and a.get(k).startswith("http")
                                        and re.search(
                                            r"\.(jpg|jpeg|png|gif)$",
                                            a.get(k),
                                            re.IGNORECASE,
                                        )
                                    ):
                                        image_url = a.get(k)
                                        self.logger.debug(
                                            f"Found image URL in post {post_name} attachments dict: {image_url}"
                                        )
                                        break

                    # If still not found, try to extract the first <img src="..."> from
                    # the original HTML content if we kept it (desc_text was stripped),
                    # we constructed `desc_text` from `selftext` earlier; try to recover HTML
                    # content from the original `selftext` when available.
                    if not image_url:
                        raw_content = None
                        raw = post.get("selftext")
                        if isinstance(raw, list):
                            # items may be dicts with 'value' containing HTML
                            for item in raw:
                                if isinstance(item, dict):
                                    raw_content = item.get("value") or item.get("text")
                                    if raw_content:
                                        break
                                else:
                                    raw_content = str(item)
                                    if raw_content:
                                        break
                            if not raw_content:
                                raw_content = ""
                        elif isinstance(raw, dict):
                            raw_content = raw.get("value") or raw.get("text") or ""
                        elif isinstance(raw, str):
                            raw_content = raw
                        else:
                            raw_content = ""

                        m = re.search(
                            r'<img[^>]+src=["\']([^"\']+)["\']',
                            raw_content or "",
                            re.IGNORECASE,
                        )
                        if m:
                            image_url = m.group(1)
                            self.logger.debug(
                                f"Extracted image URL from HTML selftext for post {post_name}: {image_url}"
                            )

                    if image_url:
                        try:
                            embed.set_image(url=image_url)
                        except Exception as e:
                            self.logger.warning(
                                f"Failed to set image URL for post {post_name}: {e}"
                            )
                    self.logger.debug(
                        f"Created embed for post {post_name}: {embed.to_dict()}"
                    )
                    await channel.send(embed=embed)
                    self._last_seen = post_name
                    await self._save_state()
                    self.logger.info(
                        f"Posted new reddit submission {post_name} -> {channel.id}"
                    )
                except Exception:
                    self.logger.exception("Failed posting reddit submission")

    async def _poll_before_loop(self) -> None:
        """Await bot readiness before allowing the first poll iteration.

        This runs inside the loop machinery and therefore does not block
        the cog_load / startup path.
        """
        try:
            await self.bot.wait_until_ready()
        except Exception:
            self.logger.exception("_poll_before_loop: wait_until_ready failed")
        # small additional delay to give startup a moment to settle
        # try:
        # 	await asyncio.sleep(5)
        # except Exception:
        # 	pass

    @commands.command(name="reddit_poll", help="Force a reddit poll (owner only)")
    @commands.is_owner()
    async def reddit_poll(self, ctx: commands.Context) -> None:
        await ctx.send("Running reddit poll...")
        await self._poll()
        await ctx.send("Done.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RedditTracker(bot))


async def main():

    headers = {
        "User-Agent": "WarEraNL-bot/1.0 (by /u/Creepino +https://github.com/colgre/wareraNL-bot)"
    }

    url = "https://www.reddit.com/r/WarEraNL.json"

    # Test with requests
    resp = requests.get(url, headers=headers)
    print("Requests Response:", resp.json())

    url = yarl.URL(url, encoded=True)
    import ssl

    ssl_context = ssl.create_default_context()
    ssl_context.set_alpn_protocols(["http/1.1"])
    ssl_context.check_hostname = False
    # Test with aiohttp
    async with aiohttp.ClientSession(
        trust_env=True,
        headers=headers,
        version=aiohttp.HttpVersion11,
        connector=aiohttp.TCPConnector(ssl=ssl_context),
    ) as session:
        async with session.get(url, timeout=15, allow_redirects=True) as resp:
            print("aiohttp Response Status:", resp.status)
            if resp.status == 200:
                print("aiohttp Response:", await resp.json())

    url = "https://www.reddit.com/r/WarEraNL.json"
    headers = {"User-Agent": "python:WarEraNL-bot:1.0 (by /u/Creepino)"}

    async with httpx.AsyncClient(http1=True) as client:
        response = await client.get(url, headers=headers)
        print("HTTPX Response Status:", response.status_code)
        if response.status_code == 200:
            print("HTTPX Response:", response.json())
        # else:
        # print("HTTPX Error Response:", response.text)

    async def fetch_reddit_rss(subreddit="WarEraNL"):
        url = "https://old.reddit.com/r/wareranl/new/.rss"
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.google.com/",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        async with AsyncSession() as s:
            # 'impersonate' makes your script look like a real Chrome browser
            response = await s.get(
                url, impersonate="chrome", headers=headers, timeout=15
            )

            if response.status_code == 200:
                feed = feedparser.parse(response.text)
                if not feed.entries:
                    print(f"No posts found or feed empty for r/{subreddit}")
                    return

                for entry in feed.entries[:3]:  # Just the 3 newest
                    print(f"Post: {entry.title}")
                    print(f"Link: {entry.link}\n")
                    print(entry.keys())
            else:
                print(f"Error {response.status_code}: Could not access r/{subreddit}")

    await fetch_reddit_rss("wareranl")


if __name__ == "__main__":
    asyncio.run(main())
