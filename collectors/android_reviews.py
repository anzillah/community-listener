"""
Collector 5 — Google Play Store reviews.
Uses the google-play-scraper library; polls every POLL_INTERVAL_MINUTES
for every app in ANDROID_APP_IDS.

If APPBOT_ANDROID_APP_IDS is configured (parallel list to ANDROID_APP_IDS),
the review link in each ticket opens directly to that review in AppBot.
"""
import logging

import config
from db import is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)
SOURCE = "android_review"


def collect(client: HelpScoutClient) -> None:
    logger.info(
        "Polling Google Play reviews for %d app(s)…", len(config.ANDROID_APP_IDS)
    )
    total = 0
    for i, app_id in enumerate(config.ANDROID_APP_IDS):
        appbot_id = (
            config.APPBOT_ANDROID_APP_IDS[i]
            if i < len(config.APPBOT_ANDROID_APP_IDS)
            else ""
        )
        total += _collect_one(client, app_id, appbot_id)
    logger.info("Android reviews: %d new ticket(s) created total", total)


def _collect_one(client: HelpScoutClient, app_id: str, appbot_id: str = "") -> int:
    try:
        from google_play_scraper import Sort, reviews as gp_reviews

        result, _ = gp_reviews(
            app_id,
            lang="en",
            country="us",
            sort=Sort.NEWEST,
            count=50,
        )
    except ImportError:
        logger.error(
            "google-play-scraper not installed. Run: pip3 install google-play-scraper"
        )
        return 0
    except Exception as exc:
        logger.error("Google Play reviews fetch error (app %s): %s", app_id, exc)
        return 0

    # Use the last segment of the package name as a short readable label
    # e.g. "com.readwise.reader" → "Reader"
    short_name = app_id.split(".")[-1].title()
    source_label = f"Google Play — {short_name}"
    play_store_link = f"https://play.google.com/store/apps/details?id={app_id}"

    new_count = 0
    for review in result:
        item_id: str = review.get("reviewId", "")
        if not item_id or is_processed(item_id, SOURCE):
            continue

        rating: int = review.get("score", 0)
        title: str = review.get("title") or "(no title)"
        body_text: str = review.get("content") or ""
        author: str = review.get("userName") or "unknown"
        version: str = review.get("reviewCreatedVersion") or ""

        # Link opens directly to this review in AppBot if configured,
        # otherwise falls back to the Play Store page
        if appbot_id:
            review_link = (
                f"https://app.appbot.co/apps/{appbot_id}"
                f"/reviews/{item_id}/external_reply"
            )
        else:
            review_link = play_store_link

        tags = ["android-review"] + get_signal_tags(f"{title} {body_text}")
        short_summary, detailed_summary = summarize(f"{title}\n\n{body_text}")
        subject = make_subject(
            "android_review", title, rating=rating,
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

        email = f"play-{_slug(author)}@play.io"
        conv_id = client.create_ticket(
            subject, body, tags,
            customer_name=author,
            customer_email=email,
        )
        if conv_id:
            mark_processed(item_id, SOURCE)
            new_count += 1
        else:
            logger.warning(
                "Android create_ticket failed:\n"
                "  review_id : %s\n"
                "  author    : %r\n"
                "  email     : %s\n"
                "  subject   : %r",
                item_id, author, email, subject[:120],
            )

    logger.info("Android reviews (%s): %d new ticket(s)", short_name, new_count)
    return new_count


def _slug(name: str) -> str:
    # isascii() guard prevents non-ASCII Unicode letters (Korean, Chinese, etc.)
    # from ending up in the email local-part, which Help Scout rejects.
    slug = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in name.lower())[:40]
    return slug.strip("-") or "user"
