"""
Discord service entry point — runs the Discord bot as a persistent process.

All polled collectors (Reddit, iOS/Android reviews, Bluesky, GitHub) are
scheduled via discord.ext.tasks inside the bot's own asyncio event loop.
This eliminates the threading complications that prevented APScheduler's
BackgroundScheduler from firing reliably in this container environment.

Usage:
  python discord_service.py
"""
import logging
import sys
import time

import config
import db
from helpscout import HelpScoutClient
from collectors import (
    android_reviews,
    bluesky,
    discord_bot,
    github_comments,
    ios_reviews,
    reddit,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_POLLED = [
    (reddit,          "reddit"),
    (ios_reviews,     "ios_reviews"),
    (android_reviews, "android_reviews"),
    (bluesky,         "bluesky"),
    (github_comments, "github_comments"),
]


def main() -> None:
    db.init_db()

    client = HelpScoutClient(
        client_id=config.HELPSCOUT_CLIENT_ID,
        client_secret=config.HELPSCOUT_CLIENT_SECRET,
        mailbox_id=config.HELPSCOUT_MAILBOX_ID,
    )

    backoff = 30
    while True:
        try:
            logger.info(
                "Starting Discord bot (%d poller(s), every %d min)…",
                len(_POLLED), config.POLL_INTERVAL_MINUTES,
            )
            discord_bot.run_bot(client, pollers=_POLLED)
        except KeyboardInterrupt:
            logger.info("Interrupted — exiting.")
            break
        except Exception as exc:
            logger.error("Discord bot crashed (%s) — restarting in %ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            break


if __name__ == "__main__":
    main()
