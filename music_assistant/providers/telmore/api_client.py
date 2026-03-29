"""API Client for Telmore Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import LoginFailed

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import json_dumps
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.providers.telmore.constants import MAX_PAGES_PAGINATED, PAGE_SIZE

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


JsonLike = dict[str, Any]


class TelmoreGraphQLError(Exception):
    """Telmore Musik GraphQL error."""

    def __init__(self, data: JsonLike) -> None:
        """Initialize TelmoreGraphQLError."""
        super().__init__(json_dumps(data))


class TelmoreAPIClient:
    """Client for interacting with Telmore API."""

    GRAPHQL_ENDPOINT = "https://graphql-1458.api.247e.com/graphql"

    # Telmore web client values observed from browser traffic
    APP_VERSION = "0.2.1.4892"
    CLIENT_ID = "46aef9c9-92f5-4c5f-84b4-820e9fc0ca4d"

    throttler = ThrottlerManager(rate_limit=4, period=1)

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize API client."""
        self.provider = provider
        self.auth = provider.auth
        self.logger = provider.logger
        self.mass = provider.mass

    @throttle_with_retries  # type: ignore[type-var]
    async def post_graphql(
        self, query: str, variables: JsonLike, _headers: JsonLike | None = None
    ) -> JsonLike:
        """Post GraphQL query to Telmore endpoint with authorization."""
        locale = self.mass.metadata.locale.split("_")[0]

        token = await self.auth.auth_token()
        if token is None:
            raise LoginFailed("Authentication with Telmore failed")

        headers: JsonLike = {
            "Authorization": f"Bearer {str(token)}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": locale,
            "x-app-version": self.APP_VERSION,
            "x-client-id": self.CLIENT_ID,
        }

        if _headers:
            headers |= _headers

        async with self.mass.http_session.post(
            self.GRAPHQL_ENDPOINT,
            json={"query": query, "variables": variables},
            headers=headers,
        ) as resp:
            if resp.status in {401, 403}:
                self.logger.debug("GraphQL auth failed with status %s", resp.status)
                self.auth.invalidate()
                raise LoginFailed("Authentication with Telmore failed")

            if resp.status == 415:
                self.logger.debug("GraphQL request rejected with 415 Unsupported Media Type")

            resp.raise_for_status()

            result = await resp.json()
            if len(result.get("errors", [])) > 0:
                self.logger.debug("GraphQL returned errors: %s", result.get("errors"))
                raise TelmoreGraphQLError(result)

            return dict(result)

    async def paginate_graphql(
        self,
        query: str,
        variables: JsonLike,
        page_path: list[str],
        variables_first_key: str = "first",
        variables_after_key: str = "after",
    ) -> AsyncGenerator[JsonLike, None]:
        """Paginate GraphQL results."""
        after = None
        has_more = True
        i = 0
        while has_more and (i < MAX_PAGES_PAGINATED):
            self.logger.log(VERBOSE_LOG_LEVEL, "Paginating GraphQL query, page %s", i + 1)
            vars_with_pagination = variables | {
                variables_first_key: PAGE_SIZE,
                variables_after_key: after,
            }
            result = await self.post_graphql(query, vars_with_pagination)

            page_data = result
            for key in page_path:
                page_data = page_data.get(key, {})

            for item in page_data.get("items", []):
                yield item

            page_info = page_data.get("pageInfo", {})
            has_more = page_info.get("hasNextPage", False)
            after = page_info.get("endCursor", None)
            i += 1
