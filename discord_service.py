"""
Discord service entry point — runs the Discord bot as a persistent process.

Deploy this as a long-running service (e.g. contextone infra), separate from
the periodic polled collectors in main.py.

Usage:
  python discord_service.py
"""
import logging
import sys
import time

import config
import db
from helpscout import HelpScoutClient
from collectors import discord_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


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
            logger.info("Starting Discord bot…")
            discord_bot.run_bot(client)
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
