"""
Community Support Listener — polled collectors entry point.

Runs five source collectors on a recurring asyncio-based schedule:
  1. Reddit          (every POLL_INTERVAL_MINUTES)
  2. iOS App Store reviews
  3. Android Play reviews
  4. Bluesky mentions
  5. GitHub comments

No Discord bot — that component is disabled.

Usage:
  python main.py
"""
import asyncio
import logging
import sys

import config
import db
from helpscout import HelpScoutClient
from collectors import (
    android_reviews,
    bluesky,
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


async def poll_forever(client: HelpScoutClient) -> None:
    """Run all collectors in a loop, sleeping between passes."""
    loop = asyncio.get_running_loop()
    interval = config.POLL_INTERVAL_MINUTES * 60  # seconds

    while True:
        logger.info("Poller tick: running %d collector(s)…", len(_POLLED))
        for module, name in _POLLED:
            try:
                await loop.run_in_executor(None, module.collect, client)
            except Exception as exc:
                logger.error("Collector %s failed: %s", name, exc)
        logger.info(
            "Poller tick complete. Next run in %d min.", config.POLL_INTERVAL_MINUTES
        )
        await asyncio.sleep(interval)


async def _main() -> None:
    db.init_db()

    client = HelpScoutClient(
        client_id=config.HELPSCOUT_CLIENT_ID,
        client_secret=config.HELPSCOUT_CLIENT_SECRET,
        mailbox_id=config.HELPSCOUT_MAILBOX_ID,
    )

    logger.info(
        "Starting poller — %d collector(s) every %d min",
        len(_POLLED), config.POLL_INTERVAL_MINUTES,
    )
    await poll_forever(client)


if __name__ == "__main__":
    asyncio.run(_main())
