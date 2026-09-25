from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from threads_platform.application.ports.threads import MediaContainerRequest, ThreadsAPIError
from threads_platform.infrastructure.threads_api.client import HttpThreadsAPI

# documentation-contract fixture: shapes follow Meta's collection examples. Values are synthetic;
# this is not a live fixture or account response.
DOCUMENTATION_QUOTA_FIXTURE = {
    "data": [
        {
            "quota_usage": 1,
            "config": {"quota_total": 250, "quota_duration": 86400},
            "reply_quota_usage": 0,
            "reply_config": {"quota_total": 1000, "quota_duration": 86400},
        }
    ]
}


@pytest.mark.asyncio
async def test_documented_container_publish_media_and_quota_contract() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/me/threads_publishing_limit"):
            return httpx.Response(200, json=DOCUMENTATION_QUOTA_FIXTURE)
        if request.url.path.endswith("/me/threads_publish"):
            return httpx.Response(200, json={"id": "media-doc-example"})
        if request.url.path.endswith("/me/threads"):
            return httpx.Response(200, json={"id": "container-doc-example"})
        if request.url.path.endswith("/container-doc-example"):
            return httpx.Response(
                200,
                json={"id": "container-doc-example", "status": "FINISHED"},
            )
        return httpx.Response(
            200,
            json={
                "id": "media-doc-example",
                "text": "Documentation example post",
                "permalink": "https://www.threads.net/@example/post/example",
                "timestamp": "2026-01-01T00:00:00+0000",
            },
        )

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        api = HttpThreadsAPI(client)
        token = SecretStr("test-placeholder")
        quota = await api.get_publishing_quota(token)
        container = await api.create_container(
            token, MediaContainerRequest(media_type="TEXT", text="Documentation example post")
        )
        status = await api.get_container(token, container.container_id)
        media_id = await api.publish_container(token, container.container_id)
        media = await api.get_media(token, media_id)

    assert quota.usage == 1
    assert quota.total == 250
    assert quota.reply_total == 1000
    assert container.container_id == "container-doc-example"
    assert status.status == "FINISHED"
    assert media.media_id == "media-doc-example"
    assert len(requests) == 5
    assert all(
        request.headers["Authorization"] == "Bearer test-placeholder" for request in requests
    )
    assert all("test-placeholder" not in str(request.url) for request in requests)


@pytest.mark.asyncio
async def test_documented_reply_page_maps_nested_ids_and_cursor() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "reply-doc-example",
                        "text": "Nested reply example",
                        "timestamp": "2026-01-01T00:00:00+0000",
                        "root_post": {"id": "root-doc-example"},
                        "replied_to": {"id": "parent-doc-example"},
                    }
                ],
                "paging": {
                    "cursors": {"before": "before-doc", "after": "after-doc"},
                    "next": "https://graph.threads.net/v1.0/example/conversation?after=after-doc",
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        page = await HttpThreadsAPI(client).get_conversation(
            SecretStr("test-placeholder"), "root-doc-example", "resume-doc"
        )

    assert page.has_more is True
    assert page.next_cursor == "after-doc"
    assert page.replies[0].root_post_id == "root-doc-example"
    assert page.replies[0].replied_to_id == "parent-doc-example"


@pytest.mark.asyncio
async def test_documented_image_video_and_carousel_request_fields() -> None:
    posted_forms: list[dict[str, list[str]]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        posted_forms.append(parse_qs(request.url.query.decode()))
        return httpx.Response(200, json={"id": f"container-doc-{len(posted_forms)}"})

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        api = HttpThreadsAPI(client)
        token = SecretStr("test-placeholder")
        await api.create_container(
            token,
            MediaContainerRequest(
                media_type="IMAGE",
                image_url="https://media.example/image.jpg",
                alt_text="documentation alt text",
            ),
        )
        await api.create_container(
            token,
            MediaContainerRequest(
                media_type="VIDEO",
                video_url="https://media.example/video.mp4",
                is_carousel_item=True,
                alt_text="documentation video alt text",
            ),
        )
        await api.create_container(
            token,
            MediaContainerRequest(
                media_type="CAROUSEL",
                children=("child-doc-1", "child-doc-2"),
                text="Carousel example",
            ),
        )

    assert posted_forms[0]["media_type"] == ["IMAGE"]
    assert posted_forms[0]["image_url"] == ["https://media.example/image.jpg"]
    assert posted_forms[0]["alt_text"] == ["documentation alt text"]
    assert posted_forms[1]["is_carousel_item"] == ["true"]
    assert posted_forms[1]["video_url"] == ["https://media.example/video.mp4"]
    assert posted_forms[2]["media_type"] == ["CAROUSEL"]
    assert posted_forms[2]["children"] == ["child-doc-1,child-doc-2"]


@pytest.mark.asyncio
async def test_documented_moderation_mutation_responses() -> None:
    paths_and_forms: list[tuple[str, dict[str, list[str]]]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        paths_and_forms.append((request.url.path, parse_qs(request.url.query.decode())))
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        api = HttpThreadsAPI(client)
        token = SecretStr("test-placeholder")
        await api.manage_reply(token, "reply-doc-example", hide=True)
        await api.manage_pending_reply(token, "pending-reply-doc", approve=False)

    assert paths_and_forms == [
        ("/v1.0/reply-doc-example/manage_reply", {"hide": ["true"]}),
        ("/v1.0/pending-reply-doc/manage_pending_reply", {"approve": ["false"]}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_code", "retry_after"),
    [
        (400, "THREADS_INVALID_REQUEST", None),
        (401, "THREADS_AUTHENTICATION_FAILED", None),
        (403, "THREADS_PERMISSION_DENIED", None),
        (429, "THREADS_RATE_LIMITED", timedelta(seconds=3)),
        (503, "THREADS_SERVER_ERROR", timedelta(seconds=3)),
    ],
)
async def test_http_statuses_map_to_sanitized_typed_errors(
    status_code: int, expected_code: str, retry_after: timedelta | None
) -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers={"Retry-After": "3"})

    async with httpx.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(ThreadsAPIError) as captured:
            await HttpThreadsAPI(client).get_publishing_quota(SecretStr("test-placeholder"))

    assert captured.value.code == expected_code
    assert captured.value.retry_after == retry_after
    assert "test-placeholder" not in str(captured.value)
