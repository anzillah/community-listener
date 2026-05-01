"""
Collector 4 — iOS App Store customer reviews.
Polls the iTunes RSS JSON feed for every app in IOS_APP_IDS.

Feed URL: https://itunes.apple.com/rss/customerreviews/id={APP_ID}/json

The feed's first entry is app metadata (no im:rating) — we read the app name
from it so tickets show "iOS App Store — Readwise" rather than a numeric ID.

If APPBOT_IOS_APP_IDS is configured (parallel list to IOS_APP_IDS), the
review link in each ticket opens directly to that review in AppBot so the
team can reply without leaving AppBot.
"""
import logging
from typing import Any

import requests

import config
from db import is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)
SOURCE = "ios_review"

_FEED_URL = "https://itunes.apple.com/rss/customerreviews/id={app_id}/json"
_HEADERS = {"User-Agent": "CommunityListener/1.0"}


def collect(client: HelpScoutClient) -> None:
    logger.info("Polling iOS App Store reviews for %d app(s)…", len(config.IOS_APP_IDS))
    total = 0
    for i, app_id in enumerate(config.IOS_APP_IDS):
        appbot_id = (
            config.APPBOT_IOS_APP_IDS[i]
            if i < len(config.APPBOT_IOS_APP_IDS)
            else ""
        )
        total += _collect_one(client, app_id, appbot_id)
    logger.info("iOS reviews: %d new ticket(s) created total", total)


def _collect_one(client: HelpScoutClient, app_id: str, appbot_id: str = "") -> int:
    url = _FEED_URL.format(app_id=app_id)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
    except Exception as exc:
        logger.error("iOS reviews fetch error (app %s): %s", app_id, exc)
        return 0

    entries: list[dict] = data.get("feed", {}).get("entry", [])
    if not entries:
        logger.info("iOS reviews (app %s): feed empty", app_id)
        return 0

    # The first entry is app metadata — grab the app name from it, then skip it
    app_name = _label(entries[0].get("im:name", {}), "label") or f"App {app_id}"
    source_label = f"iOS App Store — {app_name}"
    app_store_link = f"https://apps.apple.com/app/id{app_id}"

    new_count = 0
    for entry in entries:
        if "im:rating" not in entry:
            continue

        item_id = _label(entry.get("id", {}))
        if not item_id or is_processed(item_id, SOURCE):
            continue

        rating = int(_label(entry.get("im:rating", {}), "label") or "0")
        title = _label(entry.get("title", {}), "label") or "(no title)"
        body_text = _label(entry.get("content", {}), "label") or ""
        author = _label(entry.get("author", {}).get("name", {}), "label") or "unknown"
        version = _label(entry.get("im:version", {}), "label") or ""

        # Link opens directly to this review in AppBot if configured,
        # otherwise falls back to the App Store page
        if appbot_id:
            review_link = (
                f"https://app.appbot.co/apps/{appbot_id}"
                f"/reviews/{item_id}/external_reply"
            )
        else:
            review_link = app_store_link

        tags = ["ios-review"] + get_signal_tags(f"{title} {body_text}")
        short_summary, detailed_summary = summarize(f"{title}\n\n{body_text}")
        subject = make_subject(
            "ios_review", title, rating=rating,
            summary=short_summary or None,
        )
        body = render_body(
            source_label,
            author,
            review_link,
            f"{title}\n\n{body_text}",
            extra=[
                ("Rating",      f"{rating}/5  {'★' * rating}{'☆' * (5 - rating)}"),
                ("App version", version),
            ],
            summary=detailed_summary or None,
        )

        conv_id = client.create_ticket(
            subject, body, tags,
            customer_name=author,
            customer_email=f"appstore-{_slug(author)}@appstore.io",
        )
        if conv_id:
            mark_processed(item_id, SOURCE)
            new_count += 1

    logger.info("iOS reviews (%s): %d new ticket(s)", app_name, new_count)
    return new_count


def _label(obj: Any, key: str = "label") -> str:
    """Safely extract a 'label' value from an iTunes feed dict."""
    if isinstance(obj, dict):
        return obj.get(key, "") or ""
    return ""


def _slug(name: str) -> str:
    # isascii() guard prevents non-ASCII Unicode letters (Korean, Chinese, etc.)
    # from ending up in the email local-part, which Help Scout rejects.
    slug = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in name.lower())[:40]
    return slug.strip("-") or "user"
