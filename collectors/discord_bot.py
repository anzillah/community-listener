"""
Collector 3 — Discord bot.
Monitors one or more channels and forwards messages that contain trigger
keywords into Help Scout. Runs continuously in its own thread.

Channels are set via DISCORD_CHANNEL_IDS (comma-separated) in .env.

Requires the MESSAGE_CONTENT privileged intent to be enabled in the
Discord Developer Portal → Bot → Privileged Gateway Intents.

When `pollers` are supplied to run_bot(), they are scheduled as a plain
asyncio.create_task background loop within the bot's asyncio event loop,
avoiding the threading issues of APScheduler BackgroundScheduler.
"""
import asyncio
import logging

import discord

import config
from db import is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)
SOURCE = "discord"

# Only forward messages that contain at least one of these keywords.
# Checked as substrings (case-insensitive), so "fail" also catches "failing" / "failed",
# "problem" catches "problems", etc.
_TRIGGER_KEYWORDS = {
    # Explicit bug language
    "bug", "error", "crash", "broken", "issue",
    # Synonyms commonly used without saying "bug"
    "problem", "fail", "glitch", "stuck",
    # Multi-word phrases (also matched as substrings)
    "not working", "doesn't work", "won't work", "stopped working",
    # Sync-related (kept from original set)
    "sync",
}


def _should_forward(content: str) -> bool:
    lower = content.lower()
    return any(kw in lower for kw in _TRIGGER_KEYWORDS)


def run_bot(client: HelpScoutClient, pollers=None) -> None:
    """Blocking call — run the Discord bot.

    pollers: optional list of (module, name) pairs whose .collect(client)
             will be called on a fixed interval via discord.ext.tasks,
             running inside the bot's asyncio event loop.
    """
    intents = discord.Intents.default()
    intents.message_content = True  # privileged intent

    bot = discord.Client(intents=intents)
    _pollers = list(pollers or [])

    # ── Background polling loop ───────────────────────────────────────────────
    async def _poll_loop() -> None:
        """Async task: poll all collectors every POLL_INTERVAL_MINUTES minutes.

        Waits for the bot to be ready, then immediately runs an initial pass,
        then sleeps for the interval and repeats forever.
        """
        await bot.wait_until_ready()
        loop = asyncio.get_running_loop()
        interval = config.POLL_INTERVAL_MINUTES * 60  # seconds
        logger.info(
            "Poller loop started — running %d collector(s) every %d min",
            len(_pollers), config.POLL_INTERVAL_MINUTES,
        )
        while True:
            logger.info("Poller tick: running %d collector(s)…", len(_pollers))
            for module, name in _pollers:
                try:
                    await loop.run_in_executor(None, module.collect, client)
                except Exception as exc:
                    logger.error("Poller failed for %s: %s", name, exc)
            logger.info("Poller tick complete. Next run in %d min.", config.POLL_INTERVAL_MINUTES)
            await asyncio.sleep(interval)

    # ── Discord event handlers ────────────────────────────────────────────────

    async def _forward_message(message: discord.Message) -> None:
        """Process a single Discord message → Help Scout ticket (shared by on_ready and on_message)."""
        if message.author.bot:
            return
        if message.channel.id not in config.DISCORD_CHANNEL_IDS:
            return
        if not _should_forward(message.content):
            return

        item_id = str(message.id)
        if is_processed(item_id, SOURCE):
            return

        author = str(message.author)
        content = message.content
        guild_id = message.guild.id if message.guild else "@me"
        link = (
            f"https://discord.com/channels/{guild_id}"
            f"/{message.channel.id}/{message.id}"
        )
        channel_name = getattr(message.channel, "name", str(message.channel.id))
        tags = ["discord"] + get_signal_tags(content)

        loop = asyncio.get_event_loop()

        def _create_ticket() -> str | None:
            short_summary, detailed_summary = summarize(content)
            subject = make_subject("discord", content[:70], summary=short_summary or None)
            body = render_body(
                f"Discord — #{channel_name}", author, link, content,
                summary=detailed_summary or None,
            )
            return client.create_ticket(
                subject, body, tags,
                customer_name=author,
                customer_email=f"discord-{message.author.id}@discord.io",
            )

        conv_id = await loop.run_in_executor(None, _create_ticket)
        if conv_id:
            await loop.run_in_executor(None, lambda: mark_processed(item_id, SOURCE))
            logger.info("Discord → HS ticket %s from %s", conv_id, author)

    @bot.event
    async def on_ready() -> None:
        logger.info("Discord bot connected as %s (id=%s)", bot.user, bot.user.id)

        # Start the background polling loop as an asyncio task.
        if _pollers:
            asyncio.create_task(_poll_loop(), name="community-poller")

        # Backfill: sweep each monitored channel for recent messages the bot
        # missed while it was offline. Looks back up to 200 messages per channel.
        # Already-processed messages are skipped via is_processed(), so this is
        # safe to run on every reconnect without creating duplicates.
        loop = asyncio.get_event_loop()
        for channel_id in config.DISCORD_CHANNEL_IDS:
            channel = bot.get_channel(channel_id)
            if channel is None:
                logger.warning("Discord: channel %s not found during backfill", channel_id)
                continue
            backfilled = 0
            async for msg in channel.history(limit=200):
                await _forward_message(msg)
                backfilled += 1
            logger.info(
                "Discord: backfill complete for #%s (%d messages scanned)",
                getattr(channel, "name", channel_id), backfilled,
            )

    @bot.event
    async def on_message(message: discord.Message) -> None:
        await _forward_message(message)

    bot.run(config.DISCORD_TOKEN, log_handler=None)
