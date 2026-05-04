"""
Community Support Listener — polled collectors entry point.

Runs five source collectors on a recurring schedule:
  1. Reddit          (APScheduler, every POLL_INTERVAL_MINUTES)
                     PRAW-based: one conversation per thread, comments as replies
  2. iOS App Store reviews (APScheduler, every POLL_INTERVAL_MINUTES)
  3. Android Play reviews  (APScheduler, every POLL_INTERVAL_MINUTES)
  4. Bluesky mentions      (APScheduler, every POLL_INTERVAL_MINUTES)
  5. GitHub comments       (APScheduler, every POLL_INTERVAL_MINUTES)

Reddit, Bluesky, and GitHub are opt-in: they skip silently when their
credentials are not set in the environment.

The Discord bot runs as a separate persistent service (discord_service.py).

All tickets land in the Help Scout "Community" mailbox.

Usage:
  python main.py           # run scheduler continuously
  python main.py --once    # run all collectors once, then exit
                           # (useful for testing / cron-based scheduling)
"""
import argparse
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

# Ordered list of (module, name) for all polled collectors.
# Reddit, Bluesky, and GitHub skip gracefully when credentials are absent.
_POLLED = [
    (reddit,          "reddit"),
    (ios_reviews,     "ios_reviews"),
    (android_reviews, "android_reviews"),
    (bluesky,         "bluesky"),
    (github_comments, "github_comments"),
]


def run_once(client: HelpScoutClient) -> None:
    """Run all polled collectors once and exit (good for cron / smoke testing)."""
    logger.info("Running all collectors once…")
    for module, name in _POLLED:
        try:
            module.collect(client)
        except Exception as exc:
            logger.error("Collector %s failed: %s", name, exc)
    logger.info("Done.")


def run_service(client: HelpScoutClient) -> None:
    """Start APScheduler for polled collectors and keep the process alive."""

    # --- APScheduler for Reddit / review / social collectors ---
    scheduler = BackgroundScheduler(
        job_defaults={
            "max_instances": 1,       # don't overlap if a job runs long
            "misfire_grace_time": 60, # tolerate up to 60 s late start
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
        "Scheduler started — polling every %d min (jobs: %s)",
        config.POLL_INTERVAL_MINUTES,
        ", ".join(j for _, j in _POLLED),
    )

    # Run all collectors immediately on startup so we don't wait 10 min
    logger.info("Running initial collection pass…")
    for module, name in _POLLED:
        try:
            module.collect(client)
        except Exception as exc:
            logger.error("Initial collection failed for %s: %s", name, exc)

    # Keep main thread alive; everything else runs in background threads
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down…")
        scheduler.shutdown(wait=False)


def main() -> None:
    # Write a timestamped entry to run_log.txt so we can verify scheduled
    # cloud runs are actually executing this file (not failing before import).
    import os as _os
    _log_path = _os.path.join(_os.path.dirname(__file__) or ".", "run_log.txt")
    try:
        from datetime import datetime as _dt, timezone as _tz
        with open(_log_path, "a") as _f:
            _f.write(f"{_dt.now(_tz.utc).isoformat()} start\n")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Community Support Listener")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run all polled collectors once and exit (no Discord bot)",
    )
    args = parser.parse_args()

    db.init_db()

    client = HelpScoutClient(
        client_id=config.HELPSCOUT_CLIENT_ID,
        client_secret=config.HELPSCOUT_CLIENT_SECRET,
        mailbox_id=config.HELPSCOUT_MAILBOX_ID,
    )

    if args.once:
        run_once(client)
    else:
        run_service(client)


if __name__ == "__main__":
    main()
