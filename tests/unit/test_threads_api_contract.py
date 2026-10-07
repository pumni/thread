from datetime import timedelta
from typing import cast
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import SecretStr

from threads_platform.application.ports.threads import (
    THREAD_POST_INSIGHT_ORDER,
    MediaContainerRequest,
    ThreadPostInsightMetric,
    ThreadPostInsightName,
    ThreadPostInsights,
    ThreadsAPIError,
    ThreadsContractError,
    ThreadsTransportError,
)
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

# Synthetic fixture following Meta's current official post-insights example shape;
# this is documentation-contract evidence, not a live response.
DOCUMENTATION_POST_INSIGHTS_FIXTURE = {
    "data": [
        {
            "name": "likes",
            "period": "lifetime",
            "values": [{"value": 12}],
            "title": "Likes",
            "description": "Ignored adapter metadata",
            "id": "ignored-row-metadata",
        },
        {"name": "replies", "period": "lifetime", "values": [{"value": 3}]},
        {"name": "reposts", "period": "lifetime", "values": [{"value": 2}]},
        {"name": "quotes", "period": "lifetime", "values": [{"value": 1}]},
    ]
}


@pytest.mark.asyncio
async def test_documented_post_insights_requests_exact_metric_subset_and_maps_values() -> None:
    requests: list[httpx2.Request] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=DOCUMENTATION_POST_INSIGHTS_FIXTURE)

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
    ) as client:
        result = await HttpThreadsAPI(client).get_post_insights(
            SecretStr("test-placeholder"), "media-doc-example"
        )

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path.endswith("/media-doc-example/insights")
    assert parse_qs(requests[0].url.query.decode()) == {"metric": ["likes,replies,reposts,quotes"]}
    assert tuple(metric.name for metric in result.metrics) == THREAD_POST_INSIGHT_ORDER
    assert tuple(metric.value for metric in result.metrics) == (12, 3, 2, 1)
    assert result.period == "lifetime"
    assert "media-doc-example" not in repr(result)


@pytest.mark.asyncio
async def test_post_insights_missing_row_and_null_value_remain_unknown() -> None:
    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"data": [{"name": "likes", "period": "lifetime", "values": [{"value": None}]}]},
        )

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/", transport=httpx2.MockTransport(respond)
    ) as client:
        result = await HttpThreadsAPI(client).get_post_insights(
            SecretStr("test-placeholder"), "media-doc-example"
        )

    values = {metric.name: metric.value for metric in result.metrics}
    assert values == {
        ThreadPostInsightName.LIKES: None,
        ThreadPostInsightName.REPLIES: None,
        ThreadPostInsightName.REPOSTS: None,
        ThreadPostInsightName.QUOTES: None,
    }


def test_post_insights_dto_rejects_boolean_as_integer_and_noncanonical_order() -> None:
    with pytest.raises(ValueError):
        ThreadPostInsightMetric(ThreadPostInsightName.LIKES, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ThreadPostInsights(
            "media-doc-example",
            "lifetime",
            (ThreadPostInsightMetric(ThreadPostInsightName.REPLIES, 1),),
        )


@pytest.mark.parametrize(
    "rows",
    cast(
        list[object],
        [
            [
                {"name": "likes", "period": "lifetime", "values": [{"value": 1}]},
                {"name": "likes", "period": "lifetime", "values": [{"value": 2}]},
            ],
            [{"name": "views", "period": "lifetime", "values": [{"value": 1}]}],
            [{"name": "likes", "period": "day", "values": [{"value": 1}]}],
            [{"name": "likes", "period": "lifetime", "values": [{"value": True}]}],
            [{"name": "likes", "period": "lifetime", "values": [{"value": -1}]}],
            [{"name": "likes", "period": "lifetime", "values": [{"value": "1"}]}],
            [{"name": "likes", "period": "lifetime", "values": [{}]}],
            [{"name": "likes", "period": "lifetime", "values": []}],
            [{"name": "likes", "period": "lifetime", "values": [{"value": 1}, {"value": 2}]}],
        ],
    ),
)
@pytest.mark.asyncio
async def test_post_insights_malformed_rows_fail_closed(rows: object) -> None:
    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": rows})

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/", transport=httpx2.MockTransport(respond)
    ) as client:
        with pytest.raises(ThreadsContractError):
            await HttpThreadsAPI(client).get_post_insights(
                SecretStr("test-placeholder"), "media-doc-example"
            )


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(403, "THREADS_PERMISSION_DENIED"), (429, "THREADS_RATE_LIMITED")],
)
@pytest.mark.asyncio
async def test_post_insights_permission_and_rate_errors_are_sanitized(
    status: int,
    expected_code: str,
) -> None:
    sentinel = "INSIGHTS_RAW_ERROR_BODY_SENTINEL"

    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": {"message": sentinel}})

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/", transport=httpx2.MockTransport(respond)
    ) as client:
        with pytest.raises(ThreadsAPIError) as error:
            await HttpThreadsAPI(client).get_post_insights(
                SecretStr("INSIGHTS_TOKEN_SENTINEL"), "media-doc-example"
            )

    assert error.value.code == expected_code
    assert sentinel not in str(error.value)
    assert sentinel not in repr(error.value)
    assert "INSIGHTS_TOKEN_SENTINEL" not in repr(error.value)


@pytest.mark.asyncio
async def test_post_insights_transport_error_is_sanitized() -> None:
    async def respond(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("INSIGHTS_RAW_TRANSPORT_SENTINEL", request=request)

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/", transport=httpx2.MockTransport(respond)
    ) as client:
        with pytest.raises(ThreadsAPIError) as error:
            await HttpThreadsAPI(client).get_post_insights(
                SecretStr("INSIGHTS_TOKEN_SENTINEL"), "media-doc-example"
            )

    assert error.value.code == "THREADS_TRANSPORT_FAILURE"
    assert "INSIGHTS_RAW_TRANSPORT_SENTINEL" not in str(error.value)
    assert "INSIGHTS_TOKEN_SENTINEL" not in repr(error.value)


@pytest.mark.asyncio
async def test_documented_container_publish_media_and_quota_contract() -> None:
    requests: list[httpx2.Request] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path.endswith("/me/threads_publishing_limit"):
            return httpx2.Response(200, json=DOCUMENTATION_QUOTA_FIXTURE)
        if request.url.path.endswith("/me/threads_publish"):
            return httpx2.Response(200, json={"id": "media-doc-example"})
        if request.url.path.endswith("/me/threads"):
            return httpx2.Response(200, json={"id": "container-doc-example"})
        if request.url.path.endswith("/container-doc-example"):
            return httpx2.Response(
                200,
                json={"id": "container-doc-example", "status": "FINISHED"},
            )
        return httpx2.Response(
            200,
            json={
                "id": "media-doc-example",
                "text": "Documentation example post",
                "permalink": "https://www.threads.net/@example/post/example",
                "timestamp": "2026-01-01T00:00:00+0000",
            },
        )

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
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
    assert [request.method for request in requests] == ["GET", "POST", "GET", "POST", "GET"]
    assert all(request.content == b"" for request in requests)
    assert all(
        request.headers["Authorization"] == "Bearer test-placeholder" for request in requests
    )
    assert all("test-placeholder" not in str(request.url) for request in requests)


@pytest.mark.asyncio
async def test_documented_reply_page_maps_nested_ids_and_cursor() -> None:
    requests: list[httpx2.Request] = []

    async def respond(_: httpx2.Request) -> httpx2.Response:
        requests.append(_)
        return httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "id": "reply-doc-example",
                        "text": "Nested reply example",
                        "timestamp": "2026-01-01T00:00:00+0000",
                        "root_post": {"id": "root-doc-example"},
                        "replied_to": {"id": "parent-doc-example"},
                        "is_reply_owned_by_me": False,
                    }
                ],
                "paging": {
                    "cursors": {"before": "before-doc", "after": "after-doc"},
                    "next": "https://graph.threads.net/v1.0/example/conversation?after=after-doc",
                },
            },
        )

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
    ) as client:
        page = await HttpThreadsAPI(client).get_conversation(
            SecretStr("test-placeholder"), "root-doc-example", "resume-doc"
        )

    assert page.has_more is True
    assert page.next_cursor == "after-doc"
    reply = page.replies[0]
    assert reply.reply_id == "reply-doc-example"
    assert reply.text == "Nested reply example"
    assert reply.timestamp == "2026-01-01T00:00:00+0000"
    assert reply.root_post_id == "root-doc-example"
    assert reply.replied_to_id == "parent-doc-example"
    assert reply.is_reply_owned_by_me is False
    requested_fields = parse_qs(requests[0].url.query.decode())["fields"][0].split(",")
    assert requested_fields == [
        "id",
        "text",
        "timestamp",
        "root_post",
        "replied_to",
        "is_reply_owned_by_me",
    ]
    assert requested_fields.count("is_reply_owned_by_me") == 1


@pytest.mark.parametrize(
    ("field_present", "ownership_value", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (True, None, None),
        (False, None, None),
    ],
)
@pytest.mark.asyncio
async def test_reply_ownership_exact_boolean_and_unknown_mapping(
    field_present: bool,
    ownership_value: object,
    expected: bool | None,
) -> None:
    reply: dict[str, object] = {"id": "reply-doc-example"}
    if field_present:
        reply["is_reply_owned_by_me"] = ownership_value

    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [reply]})

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
    ) as client:
        page = await HttpThreadsAPI(client).get_replies(
            SecretStr("test-placeholder"), "root-doc-example", None
        )

    assert page.replies[0].is_reply_owned_by_me is expected


@pytest.mark.parametrize("ownership_value", [0, 1, "true", "false"])
@pytest.mark.asyncio
async def test_reply_ownership_rejects_non_json_boolean_values(ownership_value: object) -> None:
    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"data": [{"id": "reply-doc-example", "is_reply_owned_by_me": ownership_value}]},
        )

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
    ) as client:
        with pytest.raises(ThreadsContractError):
            await HttpThreadsAPI(client).get_conversation(
                SecretStr("test-placeholder"), "root-doc-example", None
            )


@pytest.mark.parametrize("parent_id", ["root-doc-example", "nested-reply-doc-example"])
@pytest.mark.asyncio
async def test_direct_and_nested_replied_to_ids_are_preserved(parent_id: str) -> None:
    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "data": [
                    {
                        "id": "reply-doc-example",
                        "root_post": {"id": "root-doc-example"},
                        "replied_to": {"id": parent_id},
                        "is_reply_owned_by_me": False,
                    }
                ]
            },
        )

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
    ) as client:
        page = await HttpThreadsAPI(client).get_conversation(
            SecretStr("test-placeholder"), "root-doc-example", None
        )

    assert page.replies[0].root_post_id == "root-doc-example"
    assert page.replies[0].replied_to_id == parent_id


@pytest.mark.asyncio
async def test_documented_image_video_and_carousel_request_fields() -> None:
    posted_forms: list[dict[str, list[str]]] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        posted_forms.append(parse_qs(request.url.query.decode()))
        return httpx2.Response(200, json={"id": f"container-doc-{len(posted_forms)}"})

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
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

    async def respond(request: httpx2.Request) -> httpx2.Response:
        paths_and_forms.append((request.url.path, parse_qs(request.url.query.decode())))
        return httpx2.Response(200, json={"success": True})

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
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
    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, headers={"Retry-After": "3"})

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
    ) as client:
        with pytest.raises(ThreadsAPIError) as captured:
            await HttpThreadsAPI(client).get_publishing_quota(SecretStr("test-placeholder"))

    assert captured.value.code == expected_code
    assert captured.value.retry_after == retry_after
    assert "test-placeholder" not in str(captured.value)


@pytest.mark.asyncio
async def test_publish_timeout_is_sanitized_without_retaining_request_secrets() -> None:
    token = "SYNTHETIC_BEARER_TOKEN"
    secret_url = (
        "https://"
        + "SYNTHETIC_USER:SYNTHETIC_PASSWORD"
        + "@graph.threads.invalid/SYNTHETIC_URL_PATH?access_token=SYNTHETIC_URL_TOKEN"
    )
    requests: list[httpx2.Request] = []

    async def timeout(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise httpx2.ReadTimeout(secret_url, request=request)

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(timeout),
    ) as client:
        with pytest.raises(ThreadsTransportError) as captured:
            await HttpThreadsAPI(client).publish_container(
                SecretStr(token), "container-doc-example"
            )

    assert captured.value.code == "THREADS_TRANSPORT_FAILURE"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/me/threads_publish")
    assert requests[0].url.params["creation_id"] == "container-doc-example"
    assert requests[0].headers["Authorization"] == f"Bearer {token}"
    assert requests[0].content == b""
    assert token not in str(requests[0].url)
    rendered_error = f"{captured.value!s} {captured.value!r}"
    for secret in (token, "SYNTHETIC_PASSWORD", "SYNTHETIC_URL_PATH", "SYNTHETIC_URL_TOKEN"):
        assert secret not in rendered_error


@pytest.mark.asyncio
async def test_invalid_json_remains_a_sanitized_contract_error() -> None:
    async def respond(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"not-json")

    async with httpx2.AsyncClient(
        base_url="https://graph.threads.net/v1.0/",
        transport=httpx2.MockTransport(respond),
    ) as client:
        with pytest.raises(ThreadsContractError) as captured:
            await HttpThreadsAPI(client).get_publishing_quota(SecretStr("SYNTHETIC_TOKEN"))

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SYNTHETIC_TOKEN" not in repr(captured.value)
