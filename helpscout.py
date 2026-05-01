"""
Help Scout v2 API client with automatic OAuth2 token refresh.

Endpoint: POST https://api.helpscout.net/v2/conversations
Auth:     Bearer token (client-credentials grant)
"""
import logging
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.helpscout.net/v2/oauth2/token"
_CONVERSATIONS_URL = "https://api.helpscout.net/v2/conversations"

# Help Scout rate limit is 200 req/min. On 429 we wait this many seconds
# before retrying (the Retry-After header is used when present).
_RETRY_WAIT_DEFAULT = 10
_MAX_RETRIES = 3


def _clean(text: str) -> str:
    """
    Strip control characters that cause silent 400 rejections from Help Scout.
    Keeps normal whitespace: tab (9), newline (10), carriage return (13).
    Removes null bytes and other C0 / C1 / DEL control chars.
    """
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text or "")


class HelpScoutClient:
    def __init__(self, client_id: str, client_secret: str, mailbox_id: int) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self.mailbox_id = mailbox_id
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a valid bearer token, refreshing when within 60 s of expiry."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 7200)
        logger.debug("Help Scout token refreshed")
        return self._token

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, url: str, payload: dict) -> requests.Response:
        """
        POST *payload* as JSON, retrying up to _MAX_RETRIES times on 429.
        Raises requests.HTTPError on non-2xx after retries are exhausted.
        """
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        for attempt in range(_MAX_RETRIES):
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", _RETRY_WAIT_DEFAULT))
                logger.warning(
                    "Help Scout rate-limited — waiting %ds (attempt %d/%d)",
                    wait, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(wait)
                # Refresh auth header in case token changed during sleep
                headers = {"Authorization": f"Bearer {self._get_token()}"}
                continue
            resp.raise_for_status()
            return resp
        # Last attempt — let raise_for_status() surface the error
        resp.raise_for_status()
        return resp  # unreachable, but satisfies type checkers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_ticket(
        self,
        subject: str,
        body: str,
        tags: list[str],
        customer_name: str = "Community Member",
        customer_email: str = "community@import.io",
    ) -> Optional[str]:
        """
        Create a Help Scout conversation.

        Returns the conversation ID (from the Location header) on success,
        or None if the request failed.
        """
        clean_body = _clean(body)
        # Strip parenthetical suffixes common in Google Play names like
        # "Megananda Agmie (Megananda)" — Help Scout rejects '(' in firstName.
        clean_name = re.sub(r'\s*\([^)]*\)', '', _clean(customer_name)).strip() or "User"
        payload = {
            "subject": _clean(subject)[:255],
            "mailboxId": self.mailbox_id,
            "type": "email",
            "status": "active",
            "customer": {
                "firstName": clean_name[:64],
                "email": customer_email,
            },
            "threads": [
                {
                    "type": "customer",
                    "text": clean_body,
                    "customer": {
                        "firstName": clean_name[:64],
                        "email": customer_email,
                    },
                }
            ],
            "tags": tags[:10],
        }

        try:
            resp = self._post(_CONVERSATIONS_URL, payload)
            # Help Scout returns 201 with a Location header like
            # https://api.helpscout.net/v2/conversations/{id}
            location = resp.headers.get("Location", "")
            conv_id = location.rstrip("/").split("/")[-1]
            logger.info("Created HS ticket %s — %s", conv_id, subject[:70])
            return conv_id
        except requests.HTTPError as exc:
            body_snippet = exc.response.text[:300] if exc.response else ""
            logger.error("Help Scout HTTP error: %s — %s", exc, body_snippet or "(empty body)")
            if exc.response is not None and exc.response.status_code == 400:
                logger.warning(
                    "400 detail — subject=%r  email=%s  body_len=%d  firstName=%r",
                    payload["subject"][:120],
                    customer_email,
                    len(clean_body),
                    clean_name[:64],
                )
            return None
        except requests.RequestException as exc:
            logger.error("Help Scout request error: %s", exc)
            return None

    def add_reply(
        self,
        conversation_id: str,
        body: str,
        customer_name: str = "Community Member",
        customer_email: str = "community@import.io",
    ) -> Optional[bool]:
        """
        Append a customer reply to an existing Help Scout conversation.

        Uses POST /v2/conversations/{id}/customer — the dedicated inbound-reply
        endpoint — so that Help Scout treats it as a customer message and fires
        notifications to the assigned team member.

        Returns:
          True   — reply added successfully
          None   — conversation not found (404); caller should clear stale ID
          False  — any other failure
        """
        clean_name = re.sub(r'\s*\([^)]*\)', '', _clean(customer_name)).strip() or "User"
        payload = {
            "customer": {
                "firstName": clean_name[:64],
                "email": customer_email,
            },
            "text": _clean(body),
        }
        try:
            self._post(
                f"{_CONVERSATIONS_URL}/{conversation_id}/customer",
                payload,
            )
            logger.debug("Added customer reply to conversation %s", conversation_id)
            return True
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning(
                    "Conversation %s not found (404) — may have been deleted in Help Scout",
                    conversation_id,
                )
                return None
            snippet = exc.response.text[:300] if exc.response else ""
            logger.error("Help Scout add_reply error: %s — %s", exc, snippet)
            return False
        except requests.RequestException as exc:
            logger.error("Help Scout request error: %s", exc)
            return False

    def find_conversation_by_tag(self, tag: str) -> Optional[str]:
        """
        Search Help Scout for a conversation with *tag*.
        Returns the conversation ID string, or None if not found.
        Used as a fallback when the local thread_map DB is missing or stale.
        """
        try:
            resp = requests.get(
                _CONVERSATIONS_URL,
                params={"tag": tag, "status": "all"},
                headers={"Authorization": f"Bearer {self._get_token()}"},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("_embedded", {}).get("conversations", [])
            return str(items[0]["id"]) if items else None
        except Exception as exc:
            logger.error("Help Scout find_conversation_by_tag error: %s", exc)
            return None
