"""
Keyword-based signal tagging.
Returns at most ONE tag — the single highest-priority match.
Priority order: bug > feature > question > praise

Keeping tags minimal means the Help Scout sidebar stays calm and
uncluttered; one clear signal is more useful than four noisy ones.
"""

_BUG = {
    "bug", "error", "crash", "broken", "freeze", "freezes", "froze",
    "stuck", "fail", "fails", "failed", "issue", "problem", "not working",
    "doesn't work", "doesn't sync", "not syncing", "sync problem",
}

_FEATURE = {
    "feature", "request", "suggestion", "suggest", "could you",
    "would be nice", "would love", "please add", "allow", "support",
    "wish", "hope", "improvement", "feature request",
}

_QUESTION = {
    "how", "why", "when", "what", "where", "who", "?", "help me",
    "can i", "is it possible", "does it", "will it", "should i",
}

_PRAISE = {
    "love", "great", "awesome", "amazing", "excellent", "best",
    "perfect", "thank", "thanks", "fantastic", "wonderful", "brilliant",
    "incredible", "outstanding", "favorite", "favourite",
}


def get_signal_tags(text: str) -> list[str]:
    """Return a list of 0 or 1 signal tags — the single highest-priority match."""
    lower = text.lower()
    for keywords, tag in (
        (_BUG,      "bug"),
        (_FEATURE,  "feature"),
        (_QUESTION, "question"),
        (_PRAISE,   "praise"),
    ):
        if any(kw in lower for kw in keywords):
            return [tag]
    return []
