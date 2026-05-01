"""
Collector 6 — Bluesky mentions.
Polls the AT Protocol search API for posts mentioning BSKY_HANDLE.

Authentication is required for the search endpoint. Set BSKY_HANDLE and
BSKY_APP_PASSWORD in your .env file.

  BSKY_HANDLE      = yourhandle.bsky.social
  BSKY_APP_PASSWORD = xxxx-xxxx-xxxx-xxxx  (create at bsky.app → Settings → App Passwords)

Search endpoint:
  GET https://bsky.social/xrpc/app.bsky.feed.searchPosts
  ?q=%40{handle}&limit=25&sort=latest
  Authorization: Bearer {access_jwt}
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

import config
from db import is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)
SOURCE = "bluesky"

_AUTH_URL = "https://bsky.social/xrpc/com.atproto.server.createSession"
_SEARCH_URL = "https://bsky.social/xrpc/app.bsky.feed.searchPosts"

# Only surface posts created within the last poll window + a small buffer
_LOOKBACK_MINUTES = config.POLL_INTERVAL_MINUTES + 5

# Module-level token cache so we don't re-auth on every poll
_access_jwt: str = ""
_jwt_expiry: float = 0.0


def _get_token() -> str:
    """Return a valid Bluesky access JWT, refreshing when needed."""
    import time
    global _access_jwt, _jwt_expiry

    app_password = config.BSKY_APP_PASSWORD
    if not app_password:
        raise RuntimeError("BSKY_APP_PASSWORD not set — cannot authenticate with Bluesky")

    if _access_jwt and time.time() < _jwt_expiry - 60:
        return _access_jwt

    resp = requests.post(
        _AUTH_URL,
        json={"identifier": config.BSKY_HANDLE, "password": app_password},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _access_jwt = data["accessJwt"]
    # Bluesky JWTs are valid for ~2 hours; refresh after 90 minutes to be safe
    _jwt_expiry = time.time() + 90 * 60
    logger.debug("Bluesky token refreshed")
    return _access_jwt


def collect(client: HelpScoutClient) -> None:
    if not config.BSKY_HANDLE:
        logger.debug("BSKY_HANDLE not set — skipping Bluesky collector")
        return

    logger.info("Polling Bluesky mentions of @%s…", config.BSKY_HANDLE)

    try:
        token = _get_token()
        resp = requests.get(
            _SEARCH_URL,
            params={"q": f"@{config.BSKY_HANDLE}", "limit": 25, "sort": "latest"},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "CommunityListener/1.0",
            },
            timeout=15,
        )
        resp.raise_for_status()
        posts: list[dict] = resp.json().get("posts", [])
    except Exception as exc:
        logger.error("Bluesky search error: %s", exc)
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_LOOKBACK_MINUTES)
    new_count = 0

    for post in posts:
        uri: str = post.get("uri", "")  # at://did:plc:.../app.bsky.feed.post/{rkey}
        if not uri:
            continue

        # Dedup by AT URI (stable across reposts / likes)
        if is_processed(uri, SOURCE):
            continue

        # Skip posts older than our lookback window
        indexed_at_str: str = post.get("indexedAt", "")
        if indexed_at_str:
            try:
                indexed_at = datetime.fromisoformat(
                    indexed_at_str.replace("Z", "+00:00")
                )
                if indexed_at < cutoff:
                    continue
            except ValueError:
                pass

        record: dict = post.get("record", {})
        text: str = record.get("text", "")
        author: dict = post.get("author", {})
        handle: str = author.get("handle", "unknown")
        display_name: str = author.get("displayName") or handle

        # Build the bsky.app web URL from the AT URI
        rkey = uri.split("/")[-1]
        web_url = f"https://bsky.app/profile/{handle}/post/{rkey}"

        tags = ["bluesky"] + get_signal_tags(text)
        short_summary, detailed_summary = summarize(text)
        subject = make_subject("bluesky", text[:70], summary=short_summary or None)
        body = render_body(
            "Bluesky",
            f"@{handle}" + (f" ({display_name})" if display_name != handle else ""),
            web_url,
            text,
            summary=detailed_summary or None,
        )

        conv_id = client.create_ticket(
            subject, body, tags,
            customer_name=display_name,
            customer_email=f"bsky-{_slug(handle)}@bsky.io",
        )
        if conv_id:
            mark_processed(uri, SOURCE)
            new_count += 1

    logger.info("Bluesky: %d new ticket(s) created", new_count)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower())[:40]
