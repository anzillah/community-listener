"""
Discord service entry point — runs the Discord bot as a persistent process
alongside APScheduler-based pollers (Reddit, iOS/Android reviews, Bluesky,
GitHub).  Everything runs in one process so logs are unified and there are no
subshell / background-process capture issues.

Usage:
  python discord_service.py
"""
import logging
import sys
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

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


def _start_pollers(client: HelpScoutClient) -> BackgroundScheduler:
    """Start APScheduler for all polled collectors and run an immediate pass."""
    scheduler = BackgroundScheduler(
        job_defaults={
            "max_instances": 1,
            "misfire_grace_time": 60,
            "coalesce": True,
        }
    )
    trigger = IntervalTrigger(minutes=config.POLL_INTERVAL_MINUTES)

    for module, job_id in _POLLED:
        scheduler.add_job(
            module.collect,
            trigger,
            args=[client],
            id=job_id,
            name=job_id.replace("_", " ").title(),
        )

    scheduler.start()
    logger.info(
        "Poller scheduler started — every %d min (%s)",
        config.POLL_INTERVAL_MINUTES,
        ", ".join(j for _, j in _POLLED),
    )

    # Run all collectors immediately so we don't wait one full interval
    logger.info("Running initial collection pass…")
    for module, name in _POLLED:
        try:
            module.collect(client)
        except Exception as exc:
            logger.error("Initial collection failed for %s: %s", name, exc)

    return scheduler


def main() -> None:
    db.init_db()

    client = HelpScoutClient(
        client_id=config.HELPSCOUT_CLIENT_ID,
        client_secret=config.HELPSCOUT_CLIENT_SECRET,
        mailbox_id=config.HELPSCOUT_MAILBOX_ID,
    )

    scheduler = _start_pollers(client)

    backoff = 30
    while True:
        try:
            logger.info("Starting Discord bot…")
            discord_bot.run_bot(client)
        except KeyboardInterrupt:
            logger.info("Interrupted — exiting.")
            scheduler.shutdown(wait=False)
            break
        except Exception as exc:
            logger.error("Discord bot crashed (%s) — restarting in %ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            break


if __name__ == "__main__":
    main()
