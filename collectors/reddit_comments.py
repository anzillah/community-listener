"""
Collector 2 — Reddit megathread comments via RSS.
Polls r/readwise/comments.rss every POLL_INTERVAL_MINUTES.

Instead of creating one ticket per comment (which creates noise from short
or emoji-only replies), comments are grouped by their parent thread and a
single ticket is created per thread per poll cycle.

  • Trivial comments (< 15 chars) are silently marked processed and skipped.
  • Each ticket's body lists every meaningful comment with author + link.
  • Subject shows the thread title + comment count gist.
"""
import hashlib
import html as html_mod
import logging
import re
from collections import defaultdict

import feedparser

import config
from db import is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)
SOURCE = "reddit_comment"

_HEADERS = {"User-Agent": "CommunityListener/1.0 (community support monitoring)"}

# Comments shorter than this (stripped of whitespace) are silently skipped.
_MIN_COMMENT_LEN = 15


# ── Helpers ───────────────────────────────────────────────────────────────────

def _item_id(entry: dict) -> str:
    return entry.get("id") or hashlib.md5(
        entry.get("link", entry.get("title", "")).encode()
    ).hexdigest()


def _thread_url(comment_url: str) -> str:
    """
    Strip the comment-specific suffix and return the parent post URL.

    Reddit comment URLs look like:
      https://www.reddit.com/r/readwise/comments/abc123/title/xyz789/
    The parent thread URL is:
      https://www.reddit.com/r/readwise/comments/abc123/
    """
    match = re.match(r"(https?://[^/]+/r/[^/]+/comments/[^/?#]+)", comment_url)
    return (match.group(1) + "/") if match else ""


def _thread_title(entry: dict) -> str:
    """
    Reddit RSS comment titles are like "Comment by u/foo on: Thread Title".
    Extract just the thread title portion.
    """
    raw = entry.get("title", "")
    if " on: " in raw:
        return raw.split(" on: ", 1)[1].strip()
    return raw.strip()


# ── Main collector ────────────────────────────────────────────────────────────

def collect(client: HelpScoutClient) -> None:
    logger.info("Polling Reddit comments RSS…")
    feed = feedparser.parse(config.REDDIT_RSS_COMMENTS, request_headers=_HEADERS)

    if feed.bozo and not feed.entries:
        logger.warning("Reddit comments RSS parse error: %s", feed.bozo_exception)
        return

    # ── Phase 1: group unprocessed comments by parent thread ─────────────────
    threads: dict[str, list[dict]] = defaultdict(list)

    for entry in feed.entries:
        iid = _item_id(entry)
        if is_processed(iid, SOURCE):
            continue

        thread = _thread_url(entry.get("link", ""))
        if thread:
            threads[thread].append(entry)
        else:
            # Can't determine thread — mark processed so we don't revisit
            mark_processed(iid, SOURCE)

    # ── Phase 2: one ticket per thread ───────────────────────────────────────
    new_count = 0
    for thread_url, entries in threads.items():

        # Separate meaningful comments from trivial ones
        meaningful = [
            e for e in entries
            if len((e.get("summary") or "").strip()) >= _MIN_COMMENT_LEN
        ]

        # Always mark trivial-only comments as processed
        trivial = [e for e in entries if e not in meaningful]
        for e in trivial:
            mark_processed(_item_id(e), SOURCE)

        if not meaningful:
            continue  # nothing worth a ticket for this thread

        # ── Build metadata ────────────────────────────────────────────────────
        n = len(meaningful)
        title = _thread_title(meaningful[0])
        authors = list(dict.fromkeys(e.get("author", "unknown") for e in meaningful))
        author_str = (
            ", ".join(authors) if len(authors) <= 3
            else f"{authors[0]} +{len(authors) - 1} others"
        )

        # ── Build combined body ───────────────────────────────────────────────
        # Each comment block: bold author name, comment text, then the URL
        blocks = []
        for e in meaningful:
            a = html_mod.escape(e.get("author", "unknown"))
            link = e.get("link", thread_url)
            text = (e.get("summary") or "").strip()
            blocks.append(
                f"<strong>{a}</strong><br>"
                f"{text}<br>"
                f"<em>{html_mod.escape(link)}</em>"
            )
        combined = "<br><br>".join(blocks)

        # ── Tags: union of all signal tags across comments ────────────────────
        all_tags = ["reddit"]
        for e in meaningful:
            all_tags += get_signal_tags(e.get("summary", ""))
        tags = list(dict.fromkeys(all_tags))  # dedup, preserve order

        # ── LLM summary of all comments combined ─────────────────────────────
        combined_text = " ".join((e.get("summary") or "").strip() for e in meaningful)
        ai_summary = summarize(combined_text)

        # ── Subject: thread title + LLM summary (or comment count fallback) ──
        subject = make_subject(
            "reddit_comment", title,
            summary=ai_summary or f"{n} new comment{'s' if n > 1 else ''}",
        )

        body = render_body(
            "Reddit — r/readwise",
            author_str,
            thread_url,
            combined,
            summary=ai_summary or None,
        )

        conv_id = client.create_ticket(
            subject, body, tags,
            customer_name=author_str,
            customer_email=f"reddit-{_slug(authors[0])}@reddit.io",
        )
        if conv_id:
            for e in meaningful:
                mark_processed(_item_id(e), SOURCE)
            new_count += 1

    logger.info("Reddit comments: %d new thread ticket(s) created", new_count)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower())[:40]
