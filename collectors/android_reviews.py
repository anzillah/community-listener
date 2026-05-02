"""
Collector 5 — Google Play Store reviews.

Primary path  : Google Play Developer API v3 (androidpublisher)
                Requires GOOGLE_PLAY_SERVICE_ACCOUNT_JSON to be set.
                Returns every review — identical coverage to Play Console.

Fallback path : google-play-scraper (public Play Store scraping)
                Used automatically when the service-account credential is absent.
                Coverage is limited — only a subset of reviews is reachable.

Polls every POLL_INTERVAL_MINUTES for every app in ANDROID_APP_IDS.
If APPBOT_ANDROID_APP_IDS is configured the review link opens directly in AppBot.
"""
import json
import logging
from datetime import datetime, timezone

import config
from db import is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)
SOURCE = "android_review"

# Only pull reviews newer than this many days on first run to avoid a
# flood of historical tickets.
_INITIAL_LOOKBACK_DAYS = 7


# ── public entry point ────────────────────────────────────────────────────────

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
        if config.GOOGLE_PLAY_SERVICE_ACCOUNT_JSON:
            total += _collect_via_api(client, app_id, appbot_id)
        else:
            logger.warning(
                "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON not set — "
                "falling back to scraper for %s (limited coverage)", app_id
            )
            total += _collect_via_scraper(client, app_id, appbot_id)
    logger.info("Android reviews: %d new ticket(s) created total", total)


# ── Google Play Developer API path ────────────────────────────────────────────

def _build_service():
    """Build an authenticated androidpublisher v3 service."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(config.GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)


def _collect_via_api(client: HelpScoutClient, app_id: str, appbot_id: str) -> int:
    try:
        service = _build_service()
    except Exception as exc:
        logger.error("Failed to build Play Developer API service: %s", exc)
        return 0

    short_name = app_id.split(".")[-1].title()
    play_store_link = f"https://play.google.com/store/apps/details?id={app_id}"
    cutoff = _cutoff_timestamp()

    new_count = 0
    page_token = None

    while True:
        try:
            kwargs = {"packageName": app_id, "maxResults": 1000}
            if page_token:
                kwargs["token"] = page_token
            response = service.reviews().list(**kwargs).execute()
        except Exception as exc:
            logger.error("Play Developer API error (app %s): %s", app_id, exc)
            break

        reviews = response.get("reviews", [])
        for review in reviews:
            review_id: str = review.get("reviewId", "")
            if not review_id or is_processed(review_id, SOURCE):
                continue

            # Pull the user comment (first comment is always the user's)
            comments = review.get("comments", [])
            if not comments:
                continue
            user_comment = comments[0].get("userComment", {})

            last_modified_secs = int(
                user_comment.get("lastModified", {}).get("seconds", 0)
            )
            if last_modified_secs and last_modified_secs < cutoff:
                # Reviews come back newest-first per page; once we're past the
                # cutoff on a fresh run we can stop paging.
                logger.debug(
                    "Android API (%s): reached cutoff at %s, stopping",
                    app_id, datetime.fromtimestamp(last_modified_secs, tz=timezone.utc)
                )
                return new_count

            rating: int = user_comment.get("starRating", 0)
            body_text: str = user_comment.get("text") or ""
            author: str = review.get("authorName") or "unknown"
            version: str = user_comment.get("appVersionName") or ""
            title = "(no title)"  # Play API has no separate title field

            if appbot_id:
                review_link = (
                    f"https://app.appbot.co/apps/{appbot_id}"
                    f"/reviews/{review_id}/external_reply"
                )
            else:
                review_link = play_store_link

            tags = ["android-review"] + get_signal_tags(body_text)
            short_summary, detailed_summary = summarize(body_text)
            subject = make_subject(
                "android_review", body_text[:70], rating=rating,
                summary=short_summary or None,
            )
            body = render_body(
                f"Google Play — {short_name}",
                author,
                review_link,
                body_text,
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
                mark_processed(review_id, SOURCE)
                new_count += 1
            else:
                logger.warning(
                    "Android create_ticket failed (API path):\n"
                    "  review_id : %s\n"
                    "  author    : %r\n"
                    "  subject   : %r",
                    review_id, author, subject[:120],
                )

        next_token = response.get("tokenPagination", {}).get("nextPageToken")
        if not next_token:
            break
        page_token = next_token

    logger.info("Android reviews API (%s): %d new ticket(s)", short_name, new_count)
    return new_count


def _cutoff_timestamp() -> int:
    """Unix timestamp before which we skip reviews on a fresh run."""
    from datetime import timedelta
    cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=_INITIAL_LOOKBACK_DAYS)
    return int(cutoff_dt.timestamp())


# ── google-play-scraper fallback path ─────────────────────────────────────────

def _collect_via_scraper(client: HelpScoutClient, app_id: str, appbot_id: str) -> int:
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
            "google-play-scraper not installed. Run: pip install google-play-scraper"
        )
        return 0
    except Exception as exc:
        logger.error("google-play-scraper fetch error (app %s): %s", app_id, exc)
        return 0

    short_name = app_id.split(".")[-1].title()
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
            f"Google Play — {short_name}",
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
                "Android create_ticket failed (scraper path):\n"
                "  review_id : %s\n"
                "  author    : %r\n"
                "  email     : %s\n"
                "  subject   : %r",
                item_id, author, f"play-{_slug(author)}@play.io", subject[:120],
            )

    logger.info("Android reviews scraper (%s): %d new ticket(s)", short_name, new_count)
    return new_count


# ── helpers ───────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    slug = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in name.lower())[:40]
    return slug.strip("-") or "user"
