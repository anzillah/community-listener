"""
Shared ticket formatting — one template used by every collector.

  make_subject()  →  standardised subject line (with optional gist summary)
  render_body()   →  consistent sanitised HTML body (with auto-generated Summary row)
  sanitize()      →  strip dangerous HTML before it reaches Help Scout
"""
from __future__ import annotations
import html
import re

# ── Source metadata: source_key → (emoji, bracket label) ─────────────────────
_SOURCE_META: dict[str, tuple[str, str]] = {
    "reddit_post":           ("💬", "Reddit Post"),
    "reddit_comment":        ("💬", "Reddit Comment"),
    "discord":               ("🐞", "Discord"),
    "ios_review":            ("⭐", "iOS Review"),
    "android_review":        ("⭐", "Android Review"),
    "bluesky":               ("🦋", "Bluesky"),
    "github_issue":          ("🐙", "GitHub"),
    "github_issue_comment":  ("🐙", "GitHub"),
    "github_review_comment": ("🐙", "GitHub"),
}

# Tags that are safe to keep in user-supplied HTML bodies (RSS / GitHub markdown)
_SAFE_TAGS = {
    "b", "strong", "i", "em", "u", "s", "strike",
    "p", "br", "ul", "ol", "li", "blockquote", "code", "pre",
}
_TAG_RE     = re.compile(r"<(/?)(\w+)([^>]*)>", re.IGNORECASE)
_ALL_TAGS_RE = re.compile(r"<[^>]+>")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove all HTML tags and unescape entities, returning plain text."""
    return _ALL_TAGS_RE.sub("", html.unescape(text)).strip()


def _excerpt(text: str, max_chars: int = 160) -> str:
    """
    Return a plain-text summary excerpt from HTML or plain text.
    Trims at a word boundary and appends '…' when truncated.
    """
    plain = re.sub(r"\s+", " ", _strip_html(text)).strip()
    if len(plain) <= max_chars:
        return plain
    cut = plain[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


# ── Public API ────────────────────────────────────────────────────────────────

def sanitize(text: str) -> str:
    """
    Strip all HTML tags except a safe allowlist, then collapse runs of
    blank lines so the body stays readable without being noisy.

    Called automatically by render_body() — collectors don't need to call
    this directly.
    """
    def _replace(m: re.Match) -> str:
        slash, tag, _ = m.group(1), m.group(2).lower(), m.group(3)
        if tag in _SAFE_TAGS:
            # Keep the tag but drop all attributes
            return f"<{slash}{tag}>"
        return ""  # strip everything else

    cleaned = _TAG_RE.sub(_replace, text)
    # Collapse 3+ consecutive newlines / <br> runs into two
    cleaned = re.sub(r"(\s*<br>\s*){3,}", "<br><br>", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def make_subject(
    source_key: str,
    title: str,
    rating: int | None = None,
    summary: str | None = None,
) -> str:
    """
    Standard subject format:

        {emoji} [{Label}] — {gist}                      ← when summary= is passed (AI summary only)
        {emoji} [{Label}] {title}                       ← fallback when no summary
        {emoji} [{Label} ★{n}] — {gist}                 ← reviews with summary
        {emoji} [{Label} ★{n}] {title}                  ← reviews without summary

    Examples
    --------
    💬 [Reddit Post] — Kindle highlights not syncing after library update
    ⭐ [iOS Review ★3] — App crashes on import from Kindle
    🐙 [GitHub] — Request to add OPDS support for third-party readers
    """
    emoji, label = _SOURCE_META.get(source_key, ("📌", source_key))
    rating_str = f" ★{rating}" if rating is not None else ""
    if summary:
        gist = _strip_html(summary)[:120].strip()
        if gist:
            return f"{emoji} [{label}{rating_str}] — {gist}"[:255]
    short_title = html.unescape(title.strip())[:70].rstrip()
    return f"{emoji} [{label}{rating_str}] {short_title}"[:255]


def render_body(
    source_label: str,
    author: str,
    link: str,
    text: str,
    extra: list[tuple[str, str]] | None = None,
    summary: str | None = None,
) -> str:
    """
    Render the canonical Help Scout ticket body.

    Parameters
    ----------
    source_label : e.g. ``"Reddit — r/readwise"``
    author       : display name or handle
    link         : URL to the original post / comment
    text         : raw message body (HTML or plain text — sanitized here)
    extra        : optional ``[(label, value), …]`` rows inserted between
                   Author and Link, e.g. ``[("Rating", "4/5 ★★★★☆")]``
    summary      : pre-computed LLM summary; falls back to a plain-text
                   excerpt of ``text`` when not provided
    """
    rows: list[tuple[str, str]] = [
        ("Source", html.escape(source_label)),
        ("Author", html.escape(author)),
    ]

    # Use the provided LLM summary, or fall back to an auto-generated excerpt
    _summary = summary or _excerpt(text)
    if _summary:
        rows.append(("Summary", html.escape(_summary)))

    if extra:
        for label, value in extra:
            rows.append((html.escape(label), html.escape(value)))
    rows.append(("Link", f"<a href='{html.escape(link)}'>{html.escape(link)}</a>"))

    table_rows = "".join(
        f"<tr>"
        f"<td style='color:#999;padding:3px 18px 3px 0;"
        f"white-space:nowrap;vertical-align:top'><b>{lbl}</b></td>"
        f"<td style='padding:3px 0'>{val}</td>"
        f"</tr>"
        for lbl, val in rows
    )

    safe_text = sanitize(text)

    return (
        f"<table style='font-size:13px;border-collapse:collapse;"
        f"margin-bottom:16px;line-height:1.6'>"
        f"{table_rows}"
        f"</table>"
        f"<hr style='margin:14px 0;border:none;border-top:1px solid #e8e8e8'>"
        f"<p style='white-space:pre-wrap;margin:0;font-size:14px'>{safe_text}</p>"
    )
