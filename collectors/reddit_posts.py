"""
Collector 1 — Reddit new posts via RSS.
Polls r/readwise/new.rss every POLL_INTERVAL_MINUTES.
"""
import hashlib
import logging

import feedparser

import config
from db import is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)
SOURCE = "reddit_post"

_HEADERS = {"User-Agent": "CommunityListener/1.0 (community support monitoring)"}


def collect(client: HelpScoutClient) -> None:
    logger.info("Polling Reddit posts RSS…")
    feed = feedparser.parse(config.REDDIT_RSS_POSTS, request_headers=_HEADERS)

    if feed.bozo and not feed.entries:
        logger.warning("Reddit posts RSS parse error: %s", feed.bozo_exception)
        return

    new_count = 0
    for entry in feed.entries:
        # Use the entry id if present; fall back to an MD5 of the link
        item_id = entry.get("id") or hashlib.md5(
            entry.get("link", entry.get("title", "")).encode()
        ).hexdigest()

        if is_processed(item_id, SOURCE):
            continue

        title = entry.get("title", "(no title)")
        author = entry.get("author", "unknown")
        link = entry.get("link", "")
        summary = entry.get("summary", "")

        # Strip "u/username: " prefix if Reddit includes it in the title
        if title.startswith("u/") and ": " in title:
            title = title.split(": ", 1)[1].strip()

        ai_summary = summarize(f"{title}\n\n{summary}")
        tags = ["reddit"] + get_signal_tags(f"{title} {summary}")
        subject = make_subject("reddit_post", title, summary=ai_summary or None)
        body = render_body("Reddit — r/readwise", author, link, summary, summary=ai_summary or None)

        conv_id = client.create_ticket(
            subject, body, tags,
            customer_name=author,
            customer_email=f"reddit-{_slug(author)}@reddit.io",
        )
        if conv_id:
            mark_processed(item_id, SOURCE)
            new_count += 1

    logger.info("Reddit posts: %d new ticket(s) created", new_count)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower())[:40]
