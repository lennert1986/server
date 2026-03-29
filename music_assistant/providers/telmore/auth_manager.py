"""Telmore Musik authentication manager."""

import re
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.util import lock, try_parse_int
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
        """Parse token into key/value pairs."""
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

        if self._refresh_token:
            self.logger.debug("Trying refresh token flow")
            async with self.mass.http_session.post(
                "https://musik.telmore.dk/api/token",
                data={"refresh_token": self._refresh_token},
            ) as refresh_response:
                try:
                    refresh_result = await refresh_response.json()
                except Exception as err:
                    self.logger.debug("Refresh token JSON parse failed: %s", err)
                    refresh_result = {}

                if refresh_result.get("status", 4) == 0:
                    access_token = refresh_result["tokenResult"]["access_token"]
                    self._access_token = TelmoreAccessToken(access_token)
                    self._refresh_token = refresh_result["tokenResult"]["refresh_token"]
                    self.logger.debug("Refresh token flow success")
                    return self._access_token

                self.logger.debug("Refresh token flow failed: %s", refresh_result)

        self.logger.debug("Starting delegated login flow")

        session_value = await self._get_login_session()
        if not session_value:
            self.logger.debug("No session value found in delegated login redirect")
            return None

        callback_url = await self._perform_internal_login(session_value)
        if not callback_url:
            self.logger.debug("No callback URL returned from internal-login")
            return None

        html = await self._fetch_callback_html(callback_url)
        if not html:
            self.logger.debug("No HTML returned from delegated login response")
            return None

        if not self._extract_tokens_from_html(html):
            self.logger.debug("Could not extract tokens from callback HTML")
            self.logger.debug("Callback HTML snippet: %s", html[:1500])
            return None

        self.logger.debug("Got new auth token")
        return self._access_token

    async def _get_login_session(self) -> str | None:
        """Start delegated login and extract session token from redirect URL."""
        async with self.mass.http_session.get(
            "https://musik.telmore.dk/api/delegatedlogin",
            allow_redirects=True,
        ) as response:
            html = await response.text()
            final_url = str(response.url)

            self.logger.debug("Delegated login status: %s", response.status)
            self.logger.debug("Delegated login final URL: %s", final_url)

            parsed = urlparse(final_url)
            query = parse_qs(parsed.query)
            session_value = query.get("session", [None])[0]

            if not session_value:
                self.logger.debug("Delegated login HTML snippet: %s", html[:1000])

            return session_value

    async def _perform_internal_login(self, session_value: str) -> str | None:
        """Authenticate against id.telmore.dk/internal-login."""
        username = self.provider.config.get_value(CONF_USERNAME)
        password = self.provider.config.get_value(CONF_PASSWORD)

        installation_id = "46aef9c9-92f5-4c5f-84b4-820e9fc0ca4d"

        referer = (
            "https://id.telmore.dk/"
            f"?session={session_value}"
            "&theme=auto&style=nopadding"
            f"&installationid={installation_id}"
        )

        payload = {
            "username": username,
            "password": password,
            "session": session_value,
        }

        headers = {
            "origin": "https://id.telmore.dk",
            "referer": referer,
            "content-type": "application/json",
            "accept": "*/*",
        }

        self.logger.debug("Submitting internal-login for user: %s", username)

        async with self.mass.http_session.post(
            "https://id.telmore.dk/internal-login",
            json=payload,
            headers=headers,
            allow_redirects=True,
        ) as response:
            self.logger.debug("Internal-login status: %s", response.status)

            try:
                result = await response.json(content_type=None)
            except Exception as err:
                text = await response.text()
                self.logger.debug("Internal-login JSON parse failed: %s", err)
                self.logger.debug("Internal-login response snippet: %s", text[:1000])
                return None

            callback_url = result.get("url")
            self.logger.debug("Internal-login callback URL found: %s", bool(callback_url))
            if callback_url:
                self.logger.debug("Internal-login callback host: %s", urlparse(callback_url).netloc)

            return callback_url

    async def _fetch_callback_html(self, callback_url: str) -> str | None:
        """Fetch delegated login callback HTML that contains tokens."""
        async with self.mass.http_session.get(
            callback_url,
            allow_redirects=True,
            headers={"referer": "https://id.telmore.dk/"},
        ) as response:
            html = await response.text()
            self.logger.debug("Callback fetch status: %s", response.status)
            self.logger.debug("Callback final URL: %s", response.url)
            return html if response.status == 200 else None

    def _extract_tokens_from_html(self, html: str) -> bool:
        """Extract tokens from Telmore callback HTML."""
        access_token_re = re.search(r'accessToken:\s*"([^"]+)"', html)
        refresh_token_re = re.search(r'refreshToken:\s*"([^"]+)"', html)

        if not access_token_re or not refresh_token_re:
            access_token_re = re.search(
                r'localStorage\.setItem\("accesstoken", "([^"]+)"',
                html,
            )
            refresh_token_re = re.search(
                r'localStorage\.setItem\("refreshtoken", "([^"]+)"',
                html,
            )

        if not access_token_re or not refresh_token_re:
            return False

        access_token = access_token_re.group(1)
        self._refresh_token = refresh_token_re.group(1)
        self._access_token = TelmoreAccessToken(access_token)
        return True
