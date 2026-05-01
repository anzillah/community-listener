"""
LLM-powered content summarizer using Claude.

Returns two summaries per message in a single API call:
  short    — one phrase (≤12 words) for the ticket subject line
  detailed — 2-3 sentence interpretation for the ticket body Summary row

Requires ANTHROPIC_API_KEY to be set. Falls back to ("", "") gracefully if
the key is missing, the anthropic package is not installed, or the call fails.
"""
import logging
import os

logger = logging.getLogger(__name__)

_MIN_TEXT_LEN = 20  # skip summarising trivially short strings

_PROMPT = (
    "Given this community message, provide two summaries:\n\n"
    "SHORT (max 12 words): A direct phrase describing what the person wants or "
    "needs. Interpret their intent, not just their words.\n\n"
    "DETAILED (max 2 sentences): Describe their specific issue or request in depth — "
    "include relevant context, what they have already tried if mentioned, and what "
    "a good resolution would look like for them.\n\n"
    "Reply in this exact format, nothing else:\n"
    "SHORT: <summary>\n"
    "DETAILED: <summary>\n\n"
    "Text:\n{text}"
)


def summarize(text: str) -> tuple[str, str]:
    """
    Return (short_summary, detailed_summary) for the given text.

    short_summary    — one phrase (≤12 words) suitable for a subject-line gist
    detailed_summary — 2-3 sentences suitable for the ticket body Summary row

    Both strings are "" when:
      - text is too short to be worth summarising
      - ANTHROPIC_API_KEY is not set
      - the anthropic package is not installed
      - the API call fails for any reason
    """
    if not text or len(text.strip()) < _MIN_TEXT_LEN:
        return "", ""

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "", ""

    try:
        import anthropic  # lazy import so the package is optional

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[
                {
                    "role": "user",
                    "content": _PROMPT.format(text=text[:3000]),
                }
            ],
        )
        raw = msg.content[0].text.strip()

        short = ""
        detailed_parts: list[str] = []
        in_detailed = False
        for line in raw.splitlines():
            if line.startswith("SHORT:"):
                short = line[6:].strip().rstrip(".")
                in_detailed = False
            elif line.startswith("DETAILED:"):
                in_detailed = True
                part = line[9:].strip()
                if part:
                    detailed_parts.append(part)
            elif in_detailed and line.strip():
                detailed_parts.append(line.strip())

        detailed = " ".join(detailed_parts).rstrip(".")
        return short[:100], detailed[:300]

    except ImportError:
        logger.warning(
            "anthropic package not installed — LLM summaries disabled. "
            "Run: pip install anthropic"
        )
        return "", ""
    except Exception as exc:
        logger.warning("LLM summarize error: %s", exc)
        return "", ""
