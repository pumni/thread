from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from threads_platform.application.ports.threads import ThreadsContractError
from threads_platform.domain.discovery import DiscoverySearchMode, DiscoverySearchType
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI

# Synthetic documentation-contract fixture; values are not live Meta responses.
DISCOVERY_PAGE_FIXTURE = {
    "data": [
        {
            "id": "thread-doc-1",
            "media_product_type": "THREADS",
            "media_type": "TEXT_POST",
            "permalink": "https://www.threads.net/@sample/post/sample1",
            "username": "sample",
            "text": "Synthetic documentation-contract result",
            "timestamp": "2026-01-01T00:00:00+0000",
            "is_quote_post": False,
            "has_replies": True,
        }
    ],
    "paging": {"cursors": {"after": "cursor-doc-1"}},
}


@pytest.mark.asyncio
async def test_keyword_search_supports_documented_modes_order_windows_and_cursor() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=DISCOVERY_PAGE_FIXTURE)

    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime(2026, 2, 1, tzinfo=UTC)
    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        api = HttpThreadsAPI(client)
        pages = [
            await api.search_threads(
                SecretStr("docs-placeholder"),
                "design systems",
                search_mode=mode,
                search_type=search_type,
                after="resume-cursor",
                since=since,
                until=until,
                limit=50,
            )
            for mode in DiscoverySearchMode
            for search_type in DiscoverySearchType
        ]

    assert len(requests) == 4
    assert [request.url.params["search_mode"] for request in requests] == [
        "KEYWORD",
        "KEYWORD",
        "TAG",
        "TAG",
    ]
    assert [request.url.params["search_type"] for request in requests] == [
        "TOP",
        "RECENT",
        "TOP",
        "RECENT",
    ]
    assert all(request.url.params["q"] == "design systems" for request in requests)
    assert all(request.url.params["after"] == "resume-cursor" for request in requests)
    assert all(request.url.params["limit"] == "50" for request in requests)
    assert all(request.url.params["since"] == since.isoformat() for request in requests)
    assert all(request.url.params["until"] == until.isoformat() for request in requests)
    assert all(
        request.headers["Authorization"] == "Bearer docs-placeholder" for request in requests
    )
    assert all("docs-placeholder" not in str(request.url) for request in requests)
    assert pages[0].has_more is True
    assert pages[0].next_cursor == "cursor-doc-1"
    assert pages[0].threads[0].remote_thread_id == "thread-doc-1"
    assert pages[0].threads[0].timestamp == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_profile_lookup_posts_and_mentions_map_documented_shapes() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/profile_lookup"):
            # Synthetic shape based on Meta's documented profile fields. Its public-lookup
            # response body is not shown in the current official collection.
            return httpx.Response(
                200,
                json={
                    "id": "author-doc-1",
                    "username": "sample",
                    "name": "Sample User",
                    "threads_biography": "Synthetic public bio",
                    "threads_profile_picture_url": "https://cdn.example/profile.jpg",
                },
            )
        return httpx.Response(200, json=DISCOVERY_PAGE_FIXTURE)

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        api = HttpThreadsAPI(client)
        profile = await api.get_public_profile(SecretStr("docs-placeholder"), "sample")
        posts = await api.get_profile_posts(
            SecretStr("docs-placeholder"), "sample", after="profile-cursor", limit=10
        )
        mentions = await api.get_mentions(
            SecretStr("docs-placeholder"),
            after="mentions-cursor",
            since=None,
            until=None,
            limit=25,
        )

    assert profile.remote_author_id == "author-doc-1"
    assert profile.username == "sample"
    assert profile.display_name == "Sample User"
    assert profile.biography == "Synthetic public bio"
    assert profile.profile_picture_url == "https://cdn.example/profile.jpg"
    assert posts.next_cursor == mentions.next_cursor == "cursor-doc-1"
    assert [request.url.path for request in requests] == [
        "/v1.0/profile_lookup",
        "/v1.0/profile_posts",
        "/v1.0/me/mentions",
    ]
    assert requests[0].url.params["username"] == "sample"
    assert requests[1].url.params["after"] == "profile-cursor"
    assert requests[1].url.params["limit"] == "10"
    assert requests[2].url.params["after"] == "mentions-cursor"
    assert requests[2].url.params["limit"] == "25"
    assert all(
        request.headers["Authorization"] == "Bearer docs-placeholder" for request in requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_body",
    [
        {"data": "not-a-list"},
        {"data": [{"text": "missing stable remote id"}]},
        {"data": [{"id": "thread-doc-1", "has_replies": "yes"}]},
    ],
)
async def test_malformed_discovery_page_fails_closed(response_body: object) -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(ThreadsContractError) as error:
            await HttpThreadsAPI(client).get_mentions(
                SecretStr("docs-placeholder"),
                after=None,
                since=None,
                until=None,
                limit=50,
            )
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.asyncio
async def test_public_profile_missing_minimum_identity_fails_closed() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "sample"})

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(ThreadsContractError):
            await HttpThreadsAPI(client).get_public_profile(SecretStr("docs-placeholder"), "sample")
