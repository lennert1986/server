"""Telmore Musik authentication manager."""

import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.util import (
    lock,
    try_parse_int,
)
from music_assistant.providers.telmore.api_client import JsonLike

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


class TelmoreAccessToken:
    """Telmore Musik access token wrapper."""

    def __init__(self, access_token: str) -> None:
        """Initialize TelmoreAccessToken."""
        self._access_token = access_token
        self._token_parts = self._parse_access_token(access_token)

    def is_expired(self) -> bool:
        """Return True if token is expired."""
        expires_at = try_parse_int(self._token_parts.get("ExpiresOn", 0))
        return not expires_at or expires_at <= time.time()

    def _parse_access_token(self, token: str) -> JsonLike:
        """Parse the access token into key/value pairs."""
        return dict(part.split("=", 1) for part in token.split("&") if "=" in part)

    def __str__(self) -> str:
        """Return string representation of the access token."""
        return self._access_token


class TelmoreAuthManager:
    """Telmore Musik authentication manager."""

    def __init__(self, provider: "TelmoreMusikProvider"):
        """Initialize TelmoreAuthManager."""
        self._access_token: TelmoreAccessToken | None = None
        self._refresh_token: str | None = None
        self.mass = provider.mass
        self.provider = provider
        self.logger = provider.logger

    def invalidate(self) -> None:
        """Invalidate current access token."""
        self._access_token = None

    @lock
    async def auth_token(self) -> TelmoreAccessToken | None:
        """Authenticate and return access token."""
        if self._access_token and not self._access_token.is_expired():
            return self._access_token

        # Try refresh token flow first
        if self._refresh_token:
            self.logger.debug("Trying refresh token flow")

            async with self.mass.http_session.post(
                "https://musik.telmore.dk/api/token",
                data={"refresh_token": self._refresh_token},
            ) as refresh_response:
                try:
                    refresh_result = await refresh_response.json()
                except Exception as err:
                    self.logger.debug("Refresh token response could not be parsed: %s", err)
                    refresh_result = {}

                if refresh_result.get("status", 4) == 0:
                    access_token = refresh_result["tokenResult"]["access_token"]

                    self.logger.debug("Refresh token flow success")
                    self._access_token = TelmoreAccessToken(access_token)
                    self._refresh_token = refresh_result["tokenResult"]["refresh_token"]
                    return self._access_token

                self.logger.debug("Refresh token flow failed: %s", refresh_result)

        self.logger.debug("Starting delegated login flow")

        async with self.mass.http_session.get(
            "https://musik.telmore.dk/api/delegatedlogin"
        ) as delegate_response:
            delegate_html = await delegate_response.text()

            self.logger.debug("Delegated login status: %s", delegate_response.status)
            self.logger.debug("Delegated login final URL: %s", delegate_response.url)

            post_action_re = re.search(r'action="([^"]+)"', delegate_html)
            if not post_action_re:
                post_action_re = re.search(r"action='([^']+)'", delegate_html)

            if not post_action_re:
                self.logger.debug(
                    "Could not find login form action in delegated login HTML: %s",
                    delegate_html[:1000],
                )
                return None

            action = post_action_re.group(1)
            if action.startswith("http://") or action.startswith("https://"):
                login_url = action
            else:
                login_url = urljoin("https://id.telmore.dk", action)

            self.logger.debug("Submitting Telmore login to: %s", login_url)

            cookies = delegate_response.cookies

        async with self.mass.http_session.post(
            login_url,
            data={
                "pf.username": self.provider.config.get_value(CONF_USERNAME),
                "pf.pass": self.provider.config.get_value(CONF_PASSWORD),
                "pf.ok": "clicked",
                "pf.adapterId": "MusicUsernamePasswordAdapter",
            },
            cookies=cookies,
            headers={"referer": "https://id.telmore.dk/"},
            allow_redirects=True,
        ) as login_response:
            login_html = await login_response.text()

            self.logger.debug("Login response status: %s", login_response.status)
            self.logger.debug("Login response final URL: %s", login_response.url)

            # Telmore format:
            # recipient.postMessage({
            #   type: "tokens",
            #   accessToken: "...",
            #   refreshToken: "..."
            # }, origin);
            access_token_re = re.search(r'accessToken:\s*"([^"]+)"', login_html)
            refresh_token_re = re.search(r'refreshToken:\s*"([^"]+)"', login_html)

            # Fallback to old YouSee-style localStorage tokens
            if not access_token_re or not refresh_token_re:
                access_token_re = re.search(
                    r'localStorage\.setItem\("accesstoken", "([^"]+)"',
                    login_html,
                )
                refresh_token_re = re.search(
                    r'localStorage\.setItem\("refreshtoken", "([^"]+)"',
                    login_html,
                )

            if not access_token_re or not refresh_token_re:
                self.logger.debug(
                    "Could not extract tokens from login response HTML: %s",
                    login_html[:1500],
                )
                return None

            access_token = access_token_re.group(1)
            self._refresh_token = refresh_token_re.group(1)

            self._access_token = TelmoreAccessToken(access_token)
            self.logger.debug("Got new auth token")

            return self._access_token
