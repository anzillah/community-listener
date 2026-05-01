"""
Central configuration — all values come from environment variables.
Copy .env.example to .env and fill in your credentials before running.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


# Help Scout (OAuth2 client-credentials flow)
HELPSCOUT_CLIENT_ID = _require("HELPSCOUT_CLIENT_ID")
HELPSCOUT_CLIENT_SECRET = _require("HELPSCOUT_CLIENT_SECRET")
HELPSCOUT_MAILBOX_ID = int(_require("HELPSCOUT_MAILBOX_ID"))

# Discord
DISCORD_TOKEN = _require("DISCORD_TOKEN")
# Accepts a comma-separated list: DISCORD_CHANNEL_IDS=111,222,333
# Falls back to the old single-value DISCORD_CHANNEL_ID for compatibility.
_raw_channels = os.getenv("DISCORD_CHANNEL_IDS") or os.getenv("DISCORD_CHANNEL_ID", "")
DISCORD_CHANNEL_IDS: list[int] = [
    int(x.strip()) for x in _raw_channels.split(",") if x.strip()
]
if not DISCORD_CHANNEL_IDS:
    raise RuntimeError("Missing required env var: DISCORD_CHANNEL_IDS")

# App Store IDs — comma-separated to support multiple apps.
# Falls back to the old singular IOS_APP_ID / ANDROID_APP_ID for compatibility.
_raw_ios = os.getenv("IOS_APP_IDS") or os.getenv("IOS_APP_ID", "")
IOS_APP_IDS: list[str] = [x.strip() for x in _raw_ios.split(",") if x.strip()]
if not IOS_APP_IDS:
    raise RuntimeError("Missing required env var: IOS_APP_IDS")

_raw_android = os.getenv("ANDROID_APP_IDS") or os.getenv("ANDROID_APP_ID", "")
ANDROID_APP_IDS: list[str] = [x.strip() for x in _raw_android.split(",") if x.strip()]
if not ANDROID_APP_IDS:
    raise RuntimeError("Missing required env var: ANDROID_APP_IDS")

# Reddit (public JSON API — no credentials required)
# One conversation per thread; comments added as replies.
# Leave REDDIT_SUBREDDIT blank to disable the Reddit collector.
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "CommunityListener/1.0 (by Readwise)")
REDDIT_SUBREDDIT = os.getenv("REDDIT_SUBREDDIT", "readwise")
_raw_thread_ids = os.getenv("REDDIT_THREAD_IDS", "")
REDDIT_THREAD_IDS: list[str] = [x.strip() for x in _raw_thread_ids.split(",") if x.strip()]
REDDIT_NEW_POSTS_LIMIT = int(os.getenv("REDDIT_NEW_POSTS_LIMIT", "25"))

# AppBot review links (optional — falls back to App Store / Play Store URLs)
# Parallel to IOS_APP_IDS / ANDROID_APP_IDS: index 0 maps to index 0, etc.
# Find your AppBot app IDs in the AppBot URL: app.appbot.co/apps/{id}/reviews
_raw_appbot_ios = os.getenv("APPBOT_IOS_APP_IDS", "")
APPBOT_IOS_APP_IDS: list[str] = [x.strip() for x in _raw_appbot_ios.split(",") if x.strip()]
_raw_appbot_android = os.getenv("APPBOT_ANDROID_APP_IDS", "")
APPBOT_ANDROID_APP_IDS: list[str] = [x.strip() for x in _raw_appbot_android.split(",") if x.strip()]

# Bluesky (optional — collector skips if BSKY_HANDLE not set)
# Handle to search mentions for, e.g. "readwise.bsky.social"
BSKY_HANDLE = os.getenv("BSKY_HANDLE", "")
# App password for authentication (create at bsky.app → Settings → App Passwords)
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD", "")

# GitHub (optional — collector skips if both are empty)
# Personal access token — raises rate limit from 60 → 5000 req/hr.
# Required when monitoring many repos to avoid hitting the anonymous cap.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
# Option A: specific repos, comma-separated. Falls back to old GITHUB_REPO.
_raw_repos = os.getenv("GITHUB_REPOS") or os.getenv("GITHUB_REPO", "")
GITHUB_REPOS: list[str] = [x.strip() for x in _raw_repos.split(",") if x.strip()]
# Option B: auto-discover every repo in an org (can be combined with GITHUB_REPOS).
GITHUB_ORG = os.getenv("GITHUB_ORG", "")

# Anthropic (optional — enables LLM-generated summaries in ticket subjects/bodies)
# Get your key at: https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Runtime knobs
DB_PATH = os.getenv("DB_PATH", "community_listener.db")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))
