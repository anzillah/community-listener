"""
Collector — Reddit (public JSON API), one Help Scout conversation per thread.

Uses Reddit's public unauthenticated JSON endpoints — no OAuth app, no
client_id, no client_secret required. Only REDDIT_SUBREDDIT needs to be set
(defaults to "readwise").

One conversation per Reddit thread:
  - Original post → new HS conversation (created immediately, even with 0 comments)
  - Every subsequent comment → reply to the existing conversation
  - Nested replies quote the parent comment as a blockquote for context

Monitors each poll cycle:
  1. The two stickied posts (auto-discovered — handles monthly rotation)
  2. Recent posts via /r/{sub}/new (paginated until caught up, cap=500)
  3. Any extra thread IDs listed in REDDIT_THREAD_IDS

Auto-tags:
  reddit                 → all conversations
  bug-report             → "Bug Reports" threads
  feature-request        → "Feature Requests" threads
  reddit-thread-{id}     → per-thread deduplication / fallback tag

Reddit's public JSON API rate limit is ~1 req/s without OAuth. The collector
sleeps briefly between requests to stay well within that limit.
"""
import html as html_lib
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

import config
from db import get_thread_conv_id, set_thread_conv_id, clear_thread_conv_id, is_processed, mark_processed
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)

SOURCE = "reddit_comment"
SOURCE_POST = "reddit_post"
_TAG_PREFIX = "reddit-thread-"
_BASE = "https://www.reddit.com"
_RATE_DELAY = 1.1  # seconds between requests — Reddit asks for ~1 req/s without OAuth

# Team Reddit accounts — comments from these users are never forwarded to Help Scout.
_TEAM_ACCOUNTS: frozenset[str] = frozenset({
    "angie-at-readwise",
    "caylaatreadwise",
    "max-at-readwise",
    "romikid",
})


# ── Lightweight data wrappers ─────────────────────────────────────────────────

@dataclass
class _Submission:
    id: str
    title: str
    selftext: str
    permalink: str
    author: Optional[str]   # None = deleted / unavailable


@dataclass
class _Comment:
    id: str
    body: str
    permalink: str
    parent_id: str          # e.g. "t1_abc123" or "t3_xyz"
    author: Optional[str]   # None = deleted / unavailable


# ── Reddit JSON API helpers ───────────────────────────────────────────────────

def _get(path: str, params: dict | None = None):
    """GET a Reddit JSON endpoint, respecting the public rate limit."""
    url = path if path.startswith("http") else f"{_BASE}{path}"
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": config.REDDIT_USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    time.sleep(_RATE_DELAY)
    return resp.json()


def _parse_sub(data: dict) -> _Submission:
    raw_author = data.get("author")
    return _Submission(
        id=data["id"],
        title=data.get("title", ""),
        selftext=data.get("selftext", "") or "",
        permalink=data.get("permalink", ""),
        author=raw_author if raw_author and raw_author != "[deleted]" else None,
    )


def _parse_comment(data: dict) -> _Comment:
    raw_author = data.get("author")
    return _Comment(
        id=data["id"],
        body=data.get("body", "") or "",
        permalink=data.get("permalink", ""),
        parent_id=data.get("parent_id", ""),
        author=raw_author if raw_author and raw_author != "[deleted]" else None,
    )


def _flatten_listing(listing: dict, out: list) -> None:
    """Recursively flatten a Reddit comment listing into *out*."""
    for child in listing.get("data", {}).get("children", []):
        kind = child.get("kind")
        if kind == "t1":
            out.append(_parse_comment(child["data"]))
            replies = child["data"].get("replies")
            if isinstance(replies, dict):
                _flatten_listing(replies, out)
        # kind == "more" → collapsed threads, skip


def _fetch_new_posts(subreddit: str, max_posts: int = 500) -> list[_Submission]:
    """
    Fetch recent posts from /r/{sub}/new, paginating with Reddit's ``after``
    token until one of these stop conditions is met (whichever comes first):

    * A post that's already been processed is encountered — we're caught up.
    * ``max_posts`` total posts have been collected (safety cap).
    * Reddit returns an empty page or no ``after`` token.

    On a steady 10-minute poll cycle only the first page is usually needed.
    On gap recovery (e.g. after an outage) the collector pages back as far as
    necessary without missing anything.
    """
    posts: list[_Submission] = []
    after: str | None = None

    while len(posts) < max_posts:
        params: dict = {"limit": 100}          # 100 is Reddit's per-page max
        if after:
            params["after"] = after

        data = _get(f"/r/{subreddit}/new.json", params)
        listing = data.get("data", {})
        children = listing.get("children", [])

        if not children:
            break

        caught_up = False
        for child in children:
            if child.get("kind") != "t3":
                continue
            sub = _parse_sub(child["data"])
            if is_processed(sub.id, SOURCE_POST):
                # Posts are newest-first; once we see a processed one the rest
                # are older and already handled.
                caught_up = True
                break
            posts.append(sub)
            if len(posts) >= max_posts:
                break

        if caught_up:
            break

        after = listing.get("after")
        if not after:
            break

    return posts


def _fetch_sticky(subreddit: str, num: int) -> Optional[_Submission]:
    """Fetch the nth stickied post. Returns None if fewer than n stickies exist."""
    try:
        # Sticky endpoint redirects to /r/{sub}/comments/{id}.json → [post, comments]
        data = _get(f"/r/{subreddit}/about/sticky.json", {"num": num})
        return _parse_sub(data[0]["data"]["children"][0]["data"])
    except Exception:
        return None


def _fetch_thread(subreddit: str, thread_id: str) -> Optional[_Submission]:
    """Fetch just the submission object for a known thread ID."""
    try:
        data = _get(f"/r/{subreddit}/comments/{thread_id}.json", {"limit": 1})
        return _parse_sub(data[0]["data"]["children"][0]["data"])
    except Exception as exc:
        logger.error("Error fetching thread %s: %s", thread_id, exc)
        return None


def _fetch_comments(subreddit: str, thread_id: str) -> list[_Comment]:
    try:
        data = _get(f"/r/{subreddit}/comments/{thread_id}.json", {"limit": 500})
        comments: list[_Comment] = []
        _flatten_listing(data[1], comments)  # data[1] is the comments listing
        return comments
    except Exception as exc:
        logger.error("Error fetching comments for thread %s: %s", thread_id, exc)
        return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reddit_tag(thread_id: str) -> str:
    return f"{_TAG_PREFIX}{thread_id}"


def _customer_email(username: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in username.lower())[:40]
    return f"reddit-{safe}@reddit.io"


def _extra_tags(title: str) -> list[str]:
    t = title.lower()
    if "bug report" in t:
        return ["bug-report"]
    if "feature request" in t:
        return ["feature-request"]
    return []


def _format_comment_body(comment: _Comment, parent: Optional[_Comment] = None) -> str:
    """
    Format a Reddit comment as HTML for a Help Scout reply thread.
    If *parent* is provided (nested reply), the parent text is block-quoted
    so short replies like "no" are readable without context.
    """
    author = html_lib.escape(comment.author or "[deleted]")
    link = html_lib.escape(f"{_BASE}{comment.permalink}")
    body_html = html_lib.escape(comment.body)
    parts = []

    if parent is not None:
        parent_author = html_lib.escape(parent.author or "[deleted]")
        parent_text = parent.body if len(parent.body) <= 300 else parent.body[:300] + "…"
        parts.append(
            f"<blockquote style='border-left:3px solid #ccc;margin:8px 0;"
            f"padding:6px 12px;color:#666'>"
            f"<p style='margin:0 0 4px 0'><em>Replying to u/{parent_author}:</em></p>"
            f"<p style='white-space:pre-wrap;margin:0'>{html_lib.escape(parent_text)}</p>"
            f"</blockquote>"
        )

    parts.append(
        f"<p><strong>u/{author}</strong></p>"
        f"<p style='white-space:pre-wrap'>{body_html}</p>"
        f"<p><a href='{link}'>View comment on Reddit</a></p>"
    )
    return "\n".join(parts)


# ── Conversation management ───────────────────────────────────────────────────

def _get_or_create_conversation(
    submission: _Submission,
    client: HelpScoutClient,
) -> Optional[str]:
    """
    Return the Help Scout conversation ID for *submission*, creating it if
    it doesn't exist yet.

    Resolution order:
      1. Local thread_map DB (fast path)
      2. Help Scout tag search (handles DB loss / replacement)
      3. Create a new conversation with the OP body
    """
    thread_id = submission.id
    tag = _reddit_tag(thread_id)

    # 1. Fast path: local DB
    conv_id = get_thread_conv_id(thread_id)
    if conv_id:
        return conv_id

    # 2. Fallback: search Help Scout by tag
    conv_id = client.find_conversation_by_tag(tag)
    if conv_id:
        logger.info("Found HS conversation %s via tag for thread %s", conv_id, thread_id)
        set_thread_conv_id(thread_id, conv_id)
        return conv_id

    # 3. Create a new conversation — OP body as the first message
    author = submission.author or "deleted"
    op_text = submission.selftext or "(no body)"
    permalink = f"{_BASE}{submission.permalink}"

    short_summary, detailed_summary = summarize(f"{submission.title}\n\n{op_text}")
    tags_list = (
        [tag, "reddit"]
        + _extra_tags(submission.title)
        + get_signal_tags(f"{submission.title} {op_text}")
    )[:10]

    subject = make_subject("reddit_post", submission.title, summary=short_summary or None)
    body = render_body(
        f"Reddit — r/{config.REDDIT_SUBREDDIT}",
        f"u/{author}",
        permalink,
        op_text,
        summary=detailed_summary or None,
    )

    conv_id = client.create_ticket(
        subject, body, tags_list,
        customer_name=f"u/{author}",
        customer_email=_customer_email(author),
    )
    if conv_id:
        set_thread_conv_id(thread_id, conv_id)
        logger.info("Created HS conversation %s for Reddit thread %s", conv_id, thread_id)
    return conv_id


# ── Per-comment processing ────────────────────────────────────────────────────

def _process_comment(
    comment: _Comment,
    submission: _Submission,
    client: HelpScoutClient,
    comment_map: dict,
) -> bool:
    """
    Add *comment* as a reply to the conversation for *submission*.
    Returns True if a new reply was posted, False if already seen or failed.
    """
    if is_processed(comment.id, SOURCE):
        return False
    if comment.author and comment.author.lower() in _TEAM_ACCOUNTS:
        return False

    conv_id = _get_or_create_conversation(submission, client)
    if not conv_id:
        return False

    author = comment.author or "deleted"

    # Include parent comment as blockquote context for nested replies (t1_ = comment)
    parent = None
    if comment.parent_id.startswith("t1_"):
        parent = comment_map.get(comment.parent_id[3:])  # strip "t1_" prefix

    body = _format_comment_body(comment, parent=parent)
    ok = client.add_reply(
        conv_id, body,
        customer_name=f"u/{author}",
        customer_email=_customer_email(author),
    )
    if ok is None:
        # Conversation was deleted in Help Scout — remove the stale DB mapping
        # so the next poll cycle re-creates the conversation from scratch.
        logger.warning(
            "Conversation %s for thread %s was deleted — clearing stale mapping",
            conv_id, submission.id,
        )
        clear_thread_conv_id(submission.id)
        return False
    if ok:
        mark_processed(comment.id, SOURCE)
        logger.debug("Added comment %s to conversation %s", comment.id, conv_id)
    return ok


# ── Per-submission processing ─────────────────────────────────────────────────

def _process_submission(
    submission: _Submission,
    client: HelpScoutClient,
) -> bool:
    """
    Ensure a Help Scout conversation exists for *submission*, filing the OP
    body as the opening message.

    This fires on every new post — including posts with zero comments — so the
    team sees fresh threads immediately instead of waiting for the first reply.

    Returns True if a brand-new conversation was created, False if the
    conversation already existed or creation failed.
    """
    if is_processed(submission.id, SOURCE_POST):
        return False

    # _get_or_create_conversation handles DB + tag-based dedup and creates
    # the conversation with the OP body if it doesn't exist yet.
    conv_id = _get_or_create_conversation(submission, client)
    if conv_id:
        mark_processed(submission.id, SOURCE_POST)
        return True
    return False


# ── Entry point ───────────────────────────────────────────────────────────────

def collect(client: HelpScoutClient) -> None:
    if not config.REDDIT_SUBREDDIT:
        logger.debug("REDDIT_SUBREDDIT not set — skipping Reddit collector")
        return

    sub = config.REDDIT_SUBREDDIT
    submissions: dict[str, _Submission] = {}

    # 1. Stickied posts — rotated monthly for bug/feature threads
    for num in (1, 2):
        s = _fetch_sticky(sub, num)
        if s:
            submissions[s.id] = s

    # 2. Recent new posts — paginate until caught up (see _fetch_new_posts)
    try:
        for post in _fetch_new_posts(sub):
            submissions.setdefault(post.id, post)
    except Exception as exc:
        logger.error("Error fetching new posts from r/%s: %s", sub, exc)

    # 3. Manually specified extra thread IDs
    for thread_id in config.REDDIT_THREAD_IDS:
        if thread_id not in submissions:
            s = _fetch_thread(sub, thread_id)
            if s:
                submissions[thread_id] = s

    logger.info("Reddit: processing %d thread(s)…", len(submissions))
    new_posts = 0
    new_replies = 0

    for submission in submissions.values():
        # Always ensure the OP itself has a conversation (catches 0-comment posts).
        try:
            if _process_submission(submission, client):
                new_posts += 1
        except Exception as exc:
            logger.error("Error processing submission %s: %s", submission.id, exc)

        comments = _fetch_comments(sub, submission.id)
        comment_map = {c.id: c for c in comments}

        for comment in comments:
            try:
                if _process_comment(comment, submission, client, comment_map):
                    new_replies += 1
            except Exception as exc:
                logger.error("Error processing comment %s: %s", comment.id, exc)

    logger.info(
        "Reddit: %d new post(s) filed, %d new comment(s) added as replies",
        new_posts, new_replies,
    )
