from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from format_translator.models import TextSegment, TranslationBlock
from format_translator.protection import (
    ProtectionValidationError,
    protect_text,
    restore_text,
)
from format_translator.providers import (
    FixtureProvider,
    OpenAIChatCompletionsProvider,
    ProviderError,
    ProviderProtocolError,
    RetryableProviderError,
)


def test_protection_round_trip() -> None:
    source = "Email a@example.com on 2026-07-29; total USD 1,250.00."
    protected = protect_text(source)
    assert protected.tokens
    assert "a@example.com" not in protected.protected_text
    assert restore_text(protected.protected_text, protected.tokens) == source


def test_protection_rejects_missing_placeholder() -> None:
    protected = protect_text("Visit https://example.com and pay 42.")
    damaged = protected.protected_text.replace(
        protected.tokens[0].placeholder,
        "",
    )
    with pytest.raises(ProtectionValidationError):
        restore_text(damaged, protected.tokens)


def test_fixture_provider_orders_results_and_supports_source_lookup() -> None:
    blocks = [
        TranslationBlock(block_id="b1", source_text="Hello"),
        TranslationBlock(block_id="b2", source_text="World"),
    ]
    provider = FixtureProvider({"World": "世界", "Hello": "你好"})
    results = asyncio.run(
        provider.translate(
            blocks,
            source_language="en",
            target_language="zh",
            request_id="test",
        )
    )
    assert [item.block_id for item in results] == ["b1", "b2"]
    assert [item.translated_text for item in results] == ["你好", "世界"]


def test_fixture_provider_rejects_missing_block() -> None:
    provider = FixtureProvider({})
    with pytest.raises(ProviderProtocolError):
        asyncio.run(
            provider.translate(
                [TranslationBlock(block_id="missing", source_text="text")],
                source_language="en",
                target_language="zh",
                request_id="test",
            )
        )


def _chat_completion(
    content: str,
    *,
    prompt_tokens: int = 17,
    completion_tokens: int = 8,
) -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "translator-v1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _openai_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str | None = "secret",
) -> tuple[OpenAIChatCompletionsProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="https://api.test/v1/",
        transport=httpx.MockTransport(handler),
    )
    provider = OpenAIChatCompletionsProvider(
        base_url="https://api.test/v1",
        model="translator-v1",
        api_key=api_key,
        client=client,
    )
    return provider, client


def test_openai_provider_posts_strict_chat_completion_and_orders_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = json.dumps(
            {
                "results": [
                    {
                        "block_id": "b2",
                        "translated_text": "世界",
                        "segments": [],
                    },
                    {
                        "block_id": "b1",
                        "translated_text": "你好",
                        "segments": [],
                    },
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_chat_completion(content))

    provider, client = _openai_provider(handler)
    blocks = [
        TranslationBlock(block_id="b1", source_text="Hello"),
        TranslationBlock(block_id="b2", source_text="World"),
    ]
    try:
        results = asyncio.run(
            provider.translate(
                blocks,
                source_language="en",
                target_language="zh",
                request_id="req-1",
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert [result.block_id for result in results] == ["b1", "b2"]
    assert [result.translated_text for result in results] == ["你好", "世界"]
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret"
    body = json.loads(request.content)
    assert body["model"] == "translator-v1"
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    system_prompt = body["messages"][0]["content"]
    assert "proper names" in system_prompt
    assert "word-by-word" in system_prompt
    assert "same target-language term" in system_prompt
    assert "opaque atomic tokens" in system_prompt
    assert "never convert" in system_prompt
    result_schema = body["response_format"]["json_schema"]["schema"]
    segments_schema = result_schema["properties"]["results"]["items"]["properties"][
        "segments"
    ]
    assert segments_schema["maxItems"] == 0
    encoded_request = request.content.decode("utf-8")
    assert all(block.block_id in encoded_request for block in blocks)
    assert all(block.source_text in encoded_request for block in blocks)


def test_openai_provider_parses_batch_usage_without_double_counting_tokens() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {
                "results": [
                    {
                        "block_id": "b1",
                        "translated_text": "你好",
                        "segments": [],
                    },
                    {
                        "block_id": "b2",
                        "translated_text": "世界",
                        "segments": [],
                    },
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json=_chat_completion(
                content,
                prompt_tokens=21,
                completion_tokens=9,
            ),
        )

    provider, client = _openai_provider(handler)
    try:
        results = asyncio.run(
            provider.translate(
                [
                    TranslationBlock(block_id="b1", source_text="Hello"),
                    TranslationBlock(block_id="b2", source_text="World!"),
                ],
                source_language="en",
                target_language="zh",
                request_id="usage",
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert results[0].usage.input_characters == 5
    assert results[0].usage.output_characters == 2
    assert results[0].usage.input_tokens == 21
    assert results[0].usage.output_tokens == 9
    assert results[0].metadata["batch_usage"] is True
    assert results[1].usage.input_characters == 6
    assert results[1].usage.output_characters == 2
    assert results[1].usage.input_tokens is None
    assert results[1].usage.output_tokens is None


@pytest.mark.parametrize(
    "results",
    [
        [{"block_id": "b1", "translated_text": "你好", "segments": []}],
        [
            {"block_id": "b1", "translated_text": "你好", "segments": []},
            {"block_id": "b1", "translated_text": "您好", "segments": []},
        ],
    ],
    ids=["missing-block", "duplicate-block"],
)
def test_openai_provider_rejects_incomplete_or_duplicate_results(
    results: list[dict[str, str]],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        content = json.dumps({"results": results}, ensure_ascii=False)
        return httpx.Response(200, json=_chat_completion(content))

    provider, client = _openai_provider(handler)
    try:
        with pytest.raises(ProviderProtocolError):
            asyncio.run(
                provider.translate(
                    [
                        TranslationBlock(block_id="b1", source_text="Hello"),
                        TranslationBlock(block_id="b2", source_text="World"),
                    ],
                    source_language="en",
                    target_language="zh",
                    request_id="bad-block-set",
                )
            )
    finally:
        asyncio.run(client.aclose())


def test_openai_provider_restores_omitted_whitespace_tagged_segment() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {
                "results": [
                    {
                        "block_id": "b1",
                        "translated_text": "你好 \n世界",
                        "segments": [
                            {"segment_id": "s0", "text": "你好"},
                            {"segment_id": "s2", "text": "世界"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_chat_completion(content))

    provider, client = _openai_provider(handler)
    block = TranslationBlock(
        block_id="b1",
        source_text="Hello \nworld",
        protocol="tagged",
        segments=[
            TextSegment(segment_id="s0", text="Hello"),
            TextSegment(segment_id="s1", text=" \n"),
            TextSegment(segment_id="s2", text="world"),
        ],
    )
    try:
        [result] = asyncio.run(
            provider.translate(
                [block],
                source_language="en",
                target_language="zh",
                request_id="omitted-whitespace",
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert [segment.segment_id for segment in result.segments] == ["s0", "s1", "s2"]
    assert [segment.text for segment in result.segments] == ["你好", " \n", "世界"]
    assert (
        "".join(segment.text for segment in result.segments) == result.translated_text
    )


@pytest.mark.parametrize(
    "segments",
    [
        [{"segment_id": "s0", "text": "你好"}],
        [
            {"segment_id": "s0", "text": "你好"},
            {"segment_id": "extra", "text": "额外"},
            {"segment_id": "s2", "text": "世界"},
        ],
        [
            {"segment_id": "s2", "text": "世界"},
            {"segment_id": "s0", "text": "你好"},
        ],
        [
            {"segment_id": "s0", "text": "你好"},
            {"segment_id": "s0", "text": "您好"},
            {"segment_id": "s2", "text": "世界"},
        ],
    ],
    ids=["non-whitespace-missing", "extra", "out-of-order", "duplicate"],
)
def test_openai_provider_rejects_unsafe_tagged_segment_mismatch(
    segments: list[dict[str, str]],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {
                "results": [
                    {
                        "block_id": "b1",
                        "translated_text": "你好 世界",
                        "segments": segments,
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(200, json=_chat_completion(content))

    provider, client = _openai_provider(handler)
    block = TranslationBlock(
        block_id="b1",
        source_text="Hello world",
        protocol="tagged",
        segments=[
            TextSegment(segment_id="s0", text="Hello"),
            TextSegment(segment_id="s1", text=" "),
            TextSegment(segment_id="s2", text="world"),
        ],
    )
    try:
        with pytest.raises(ProviderProtocolError):
            asyncio.run(
                provider.translate(
                    [block],
                    source_language="en",
                    target_language="zh",
                    request_id="unsafe-segment-mismatch",
                )
            )
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    "content",
    [
        '{"results":[{"block_id":"b1","translated_text":"你好"}]} trailing',
        json.dumps(
            {
                "results": [
                    {
                        "block_id": "b1",
                        "translated_text": "你好",
                        "segments": [],
                        "unexpected": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
    ],
    ids=["trailing-json", "unknown-result-field"],
)
def test_openai_provider_strictly_parses_message_json(content: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_completion(content))

    provider, client = _openai_provider(handler)
    try:
        with pytest.raises(ProviderProtocolError):
            asyncio.run(
                provider.translate(
                    [TranslationBlock(block_id="b1", source_text="Hello")],
                    source_language="en",
                    target_language="zh",
                    request_id="strict-json",
                )
            )
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (400, ProviderError),
        (429, RetryableProviderError),
        (503, RetryableProviderError),
    ],
)
def test_openai_provider_maps_http_errors(
    status_code: int,
    expected_error: type[ProviderError],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "upstream"}})

    provider, client = _openai_provider(handler)
    try:
        with pytest.raises(expected_error) as error:
            asyncio.run(
                provider.translate(
                    [TranslationBlock(block_id="b1", source_text="Hello")],
                    source_language="en",
                    target_language="zh",
                    request_id="http-error",
                )
            )
        if status_code == 400:
            assert type(error.value) is ProviderError
    finally:
        asyncio.run(client.aclose())


def test_openai_provider_treats_remote_disconnect_as_retryable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    provider, client = _openai_provider(handler)
    try:
        with pytest.raises(
            RetryableProviderError,
            match="Server disconnected",
        ):
            asyncio.run(
                provider.translate(
                    [TranslationBlock(block_id="b1", source_text="Hello")],
                    source_language="en",
                    target_language="zh",
                    request_id="remote-disconnect",
                )
            )
    finally:
        asyncio.run(client.aclose())
