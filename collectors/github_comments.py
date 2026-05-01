"""
Collector 7 — GitHub issues, issue comments, and PR review comments.
Polls three endpoints per repo every POLL_INTERVAL_MINUTES:
  - /repos/{owner}/{repo}/issues            (new issues — PRs filtered out)
  - /repos/{owner}/{repo}/issues/comments   (issue + PR discussion threads)
  - /repos/{owner}/{repo}/pulls/comments    (inline PR review comments)

Repos are resolved from two optional, combinable sources:
  GITHUB_REPOS = readwiseio/obsidian-readwise,readwiseio/readwise-cli
                 comma-separated list of specific repos
  GITHUB_ORG   = readwiseio
                 auto-discovers every public repo in the org each run

Uses ?since= so only new/updated items are returned each poll.
Deduplication by item ID prevents double-ticketing on any overlap.

GITHUB_TOKEN is strongly recommended when monitoring many repos
(5000 req/hr authenticated vs 60 req/hr anonymous).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import config
from db import is_processed, mark_processed, get_last_poll, set_last_poll
from helpscout import HelpScoutClient
from summarizer import summarize
from tagger import get_signal_tags
from template import make_subject, render_body

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_POLL_STATE_KEY = "github"
# How far back to look on the very first run (no saved timestamp yet)
_DEFAULT_LOOKBACK_DAYS = 7


# ── Shared helpers ────────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CommunityListener/1.0",
    }
    if config.GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return h


def _since_iso() -> str:
    """
    Return the ISO-8601 `since` timestamp to pass to GitHub.

    Uses the last successful poll time stored in the DB so comments are never
    missed during downtime or gaps between --once runs. Falls back to
    _DEFAULT_LOOKBACK_DAYS ago on the very first run.
    """
    saved = get_last_poll(_POLL_STATE_KEY)
    if saved:
        return saved
    ts = datetime.now(timezone.utc) - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_paged(url: str, params: dict) -> list[dict]:
    """Fetch all pages from any GitHub list endpoint."""
    results: list[dict] = []
    while url:
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=15)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("GitHub API error %s: %s", url, exc)
            break
        results.extend(resp.json())
        url = _next_link(resp.headers.get("Link", ""))
        params = {}
    return results


def _next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        segments = [s.strip() for s in part.split(";")]
        if len(segments) == 2 and segments[1] == 'rel="next"':
            return segments[0].lstrip("<").rstrip(">")
    return None


def _issue_number_from_url(issue_url: str) -> str:
    return "#" + issue_url.rstrip("/").split("/")[-1]


# ── Repo discovery ────────────────────────────────────────────────────────────

def _get_repos() -> list[str]:
    """
    Return the deduplicated list of owner/repo strings to monitor,
    combining any explicit GITHUB_REPOS with org-wide discovery.
    """
    repos: list[str] = list(config.GITHUB_REPOS)

    if config.GITHUB_ORG:
        org = config.GITHUB_ORG.strip("/")
        logger.info("Discovering repos for GitHub org: %s", org)
        url = f"{_API_BASE}/orgs/{org}/repos"
        try:
            for repo in _fetch_paged(url, {"per_page": 100, "type": "public"}):
                full_name: str = repo.get("full_name", "")
                if full_name and full_name not in repos:
                    repos.append(full_name)
        except Exception as exc:
            logger.error("Could not list repos for org %s: %s", org, exc)

    return repos


# ── Per-repo polling ──────────────────────────────────────────────────────────

def _ticket(
    client: HelpScoutClient,
    *,
    owner_repo: str,
    source: str,
    source_key: str,
    item_id: str,
    comment_type: str,
    body_text: str,
    author_login: str,
    html_url: str,
    context_label: str,
) -> bool:
    tags = ["github"] + get_signal_tags(body_text)
    short_summary, detailed_summary = summarize(body_text)
    subject = make_subject(source_key, body_text[:70], summary=short_summary or None)
    body = render_body(
        f"GitHub — {owner_repo}",
        f"@{author_login}",
        html_url,
        body_text,
        extra=[
            ("Type",   comment_type),
            ("Thread", context_label),
        ],
        summary=detailed_summary or None,
    )
    conv_id = client.create_ticket(
        subject, body, tags,
        customer_name=author_login,
        customer_email=f"github-{author_login}@github.io",
    )
    if conv_id:
        mark_processed(item_id, source)
        return True
    return False


def _collect_repo(client: HelpScoutClient, owner_repo: str, since: str) -> int:
    new_count = 0

    # 1. Issue / PR discussion comments
    source_issue = "github_issue_comment"
    for comment in _fetch_paged(
        f"{_API_BASE}/repos/{owner_repo}/issues/comments",
        {"since": since, "per_page": 100, "sort": "created", "direction": "desc"},
    ):
        item_id = f"ic-{comment['id']}"
        if is_processed(item_id, source_issue):
            continue

        is_pr = "/pull/" in comment.get("html_url", "")
        ok = _ticket(
            client,
            owner_repo=owner_repo,
            source=source_issue,
            source_key="github_issue_comment",
            item_id=item_id,
            comment_type="PR Comment" if is_pr else "Issue Comment",
            body_text=comment.get("body", ""),
            author_login=comment.get("user", {}).get("login", "unknown"),
            html_url=comment.get("html_url", ""),
            context_label=_issue_number_from_url(comment.get("issue_url", "")),
        )
        if ok:
            new_count += 1

    # 2. Inline PR review comments
    source_review = "github_review_comment"
    for comment in _fetch_paged(
        f"{_API_BASE}/repos/{owner_repo}/pulls/comments",
        {"since": since, "per_page": 100, "sort": "created", "direction": "desc"},
    ):
        item_id = f"rc-{comment['id']}"
        if is_processed(item_id, source_review):
            continue

        pr_number = "#" + comment.get("pull_request_url", "").rstrip("/").split("/")[-1]
        ok = _ticket(
            client,
            owner_repo=owner_repo,
            source=source_review,
            source_key="github_review_comment",
            item_id=item_id,
            comment_type="PR Review Comment",
            body_text=comment.get("body", ""),
            author_login=comment.get("user", {}).get("login", "unknown"),
            html_url=comment.get("html_url", ""),
            context_label=f"PR {pr_number}",
        )
        if ok:
            new_count += 1

    # 3. New issues (PRs are excluded — GitHub returns them from /issues too,
    #    but they have a "pull_request" key which we filter out)
    source_issue_open = "github_issue"
    for issue in _fetch_paged(
        f"{_API_BASE}/repos/{owner_repo}/issues",
        {"since": since, "state": "all", "per_page": 100, "sort": "created", "direction": "desc"},
    ):
        if "pull_request" in issue:
            continue  # skip PRs

        item_id = f"issue-{issue['id']}"
        if is_processed(item_id, source_issue_open):
            continue

        ok = _ticket(
            client,
            owner_repo=owner_repo,
            source=source_issue_open,
            source_key="github_issue",
            item_id=item_id,
            comment_type=f"Issue #{issue['number']} ({issue.get('state', 'open')})",
            body_text=issue.get("body") or "(no description)",
            author_login=issue.get("user", {}).get("login", "unknown"),
            html_url=issue.get("html_url", ""),
            context_label=issue.get("title", ""),
        )
        if ok:
            new_count += 1

    return new_count


# ── Entry point ───────────────────────────────────────────────────────────────

def collect(client: HelpScoutClient) -> None:
    repos = _get_repos()
    if not repos:
        logger.debug("No GitHub repos configured — skipping GitHub collector")
        return

    since = _since_iso()
    # Record the start time now so the next poll picks up from here,
    # even if this run finds 0 comments.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info("Polling GitHub comments for %d repo(s) (since %s)…", len(repos), since)
    total = 0
    for repo in repos:
        logger.debug("GitHub: scanning %s", repo)
        total += _collect_repo(client, repo.strip("/"), since)
    logger.info("GitHub: %d new ticket(s) created total", total)

    set_last_poll(_POLL_STATE_KEY, now_iso)
