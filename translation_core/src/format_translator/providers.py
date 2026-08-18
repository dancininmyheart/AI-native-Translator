"""Provider abstractions and built-in fixture/HTTP implementations."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from .models import (
    ProviderUsage,
    QaIssue,
    SemanticQaResult,
    TextFitResult,
    TextSegment,
    TranslationBlock,
    TranslationResult,
)
from .protection import validate_protected_tokens


class ProviderError(RuntimeError):
    """Base error surfaced by normalized providers."""

    def __init__(
        self,
        message: str,
        *,
        http_attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_attempts = http_attempts


class RetryableProviderError(ProviderError):
    """Temporary network, throttling, or upstream server error."""


class ProviderProtocolError(ProviderError):
    """Provider response did not satisfy the normalized contract."""


class TranslationProvider(ABC):
    """A provider translates one caller-selected batch.

    Batching, concurrency, retry, caching, and fallback routing intentionally
    belong to the orchestration layer.
    """

    name: str

    @abstractmethod
    async def translate(
        self,
        blocks: Sequence[TranslationBlock],
        *,
        source_language: str,
        target_language: str,
        request_id: str,
    ) -> list[TranslationResult]:
        """Translate all blocks exactly once and preserve their IDs."""


class ProcessingProvider(ABC):
    """Language-aware QA and text fitting without document write access."""

    name: str
    # True means a raised RetryableProviderError already represents an
    # exhausted provider-owned retry/deadline budget.  Orchestrators use this
    # capability instead of inferring retry behavior from a concrete type.
    manages_semantic_qa_retries: bool = False

    @abstractmethod
    async def semantic_qa(
        self,
        *,
        source: TranslationBlock,
        translation: TranslationResult,
        request_id: str,
    ) -> SemanticQaResult:
        """Check one source/translation pair for semantic defects."""

    @abstractmethod
    async def fit_text(
        self,
        *,
        source: TranslationBlock,
        translation: TranslationResult,
        max_length: int,
        request_id: str,
    ) -> TextFitResult:
        """Shorten a translation without changing its protected meaning."""


class FixtureProvider(TranslationProvider):
    """Deterministic provider backed by a JSON mapping.

    Supported fixture shapes:

    * ``{"block-id": "translation", "source text": "translation"}``
    * ``{"by_id": {...}, "by_source": {...}}``

    Mapping values may be strings or TranslationResult-shaped objects.  Lookup
    prefers block ID and falls back to exact source text.
    """

    name = "fixture"

    def __init__(
        self,
        fixture: Path | str | Mapping[str, Any],
        *,
        missing: str = "error",
        model: str = "fixture-v1",
    ) -> None:
        if missing not in {"error", "passthrough"}:
            raise ValueError("missing must be 'error' or 'passthrough'")
        raw = self._load_fixture(fixture)
        if "by_id" in raw or "by_source" in raw:
            self._by_id = self._ensure_mapping(raw.get("by_id", {}), "by_id")
            self._by_source = self._ensure_mapping(
                raw.get("by_source", {}), "by_source"
            )
        else:
            self._by_id = dict(raw)
            self._by_source = dict(raw)
        self._missing = missing
        self._model = model

    @staticmethod
    def _load_fixture(fixture: Path | str | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(fixture, Mapping):
            return dict(fixture)
        path = Path(fixture)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderProtocolError(f"cannot load fixture {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ProviderProtocolError("fixture root must be a JSON object")
        return loaded

    @staticmethod
    def _ensure_mapping(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProviderProtocolError(f"fixture field {field!r} must be an object")
        return value

    def _make_result(self, block: TranslationBlock, value: Any) -> TranslationResult:
        if isinstance(value, str):
            return TranslationResult(
                block_id=block.block_id,
                translated_text=value,
                provider=self.name,
                model=self._model,
            )
        if not isinstance(value, Mapping):
            raise ProviderProtocolError(
                f"fixture value for {block.block_id!r} must be a string or object"
            )
        data = dict(value)
        supplied_id = data.get("block_id", block.block_id)
        if supplied_id != block.block_id:
            raise ProviderProtocolError(
                f"fixture returned block_id {supplied_id!r} for {block.block_id!r}"
            )
        data["block_id"] = block.block_id
        data.setdefault("provider", self.name)
        data.setdefault("model", self._model)
        try:
            return TranslationResult.model_validate(data)
        except ValidationError as exc:
            raise ProviderProtocolError(
                f"invalid fixture result for {block.block_id!r}: {exc}"
            ) from exc

    async def translate(
        self,
        blocks: Sequence[TranslationBlock],
        *,
        source_language: str,
        target_language: str,
        request_id: str,
    ) -> list[TranslationResult]:
        del source_language, target_language, request_id
        results: list[TranslationResult] = []
        for block in blocks:
            value = self._by_id.get(block.block_id)
            if value is None:
                value = self._by_source.get(block.source_text)
            if value is None:
                if self._missing == "passthrough":
                    value = block.source_text
                else:
                    raise ProviderProtocolError(
                        f"fixture has no translation for block {block.block_id!r}"
                    )
            results.append(self._make_result(block, value))
        return results


class HttpJsonTranslationProvider(TranslationProvider):
    """Normalized JSON-over-HTTP translation provider.

    The remote endpoint must return ``{"results": [...]}``; every requested
    block must occur exactly once and no unrequested block may be returned.
    """

    def __init__(
        self,
        *,
        base_url: str,
        endpoint: str = "/translate",
        api_key: str | None = None,
        api_key_header: str = "Authorization",
        api_key_prefix: str = "Bearer",
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        name: str = "http-json",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.name = name
        self._endpoint = endpoint
        self._owns_client = client is None
        merged_headers = dict(headers or {})
        if api_key is not None:
            credential = f"{api_key_prefix} {api_key}".strip()
            merged_headers[api_key_header] = credential
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=merged_headers,
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def __aenter__(self) -> HttpJsonTranslationProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _block_payload(block: TranslationBlock) -> dict[str, Any]:
        return {
            "block_id": block.block_id,
            "protocol": block.protocol,
            "text": block.source_text,
            "segments": [segment.model_dump(mode="json") for segment in block.segments],
            "context": block.context,
            "glossary": block.glossary,
            "protected_tokens": [
                token.model_dump(mode="json") for token in block.protected_tokens
            ],
            "max_length_hint": block.max_length_hint,
        }

    async def translate(
        self,
        blocks: Sequence[TranslationBlock],
        *,
        source_language: str,
        target_language: str,
        request_id: str,
    ) -> list[TranslationResult]:
        requested_ids = [block.block_id for block in blocks]
        if len(set(requested_ids)) != len(requested_ids):
            raise ProviderProtocolError("request contains duplicate block IDs")
        if not blocks:
            return []

        payload = {
            "request_id": request_id,
            "source_language": source_language,
            "target_language": target_language,
            "blocks": [self._block_payload(block) for block in blocks],
        }
        started = perf_counter()
        try:
            response = await self._client.post(self._endpoint, json=payload)
        except httpx.TransportError as exc:
            raise RetryableProviderError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc)) from exc

        if response.status_code == 429 or 500 <= response.status_code < 600:
            raise RetryableProviderError(
                f"provider returned HTTP {response.status_code}"
            )
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"provider returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderProtocolError("provider response is not valid JSON") from exc
        if not isinstance(body, dict) or set(body) - {
            "results",
            "provider",
            "model",
            "usage",
        }:
            raise ProviderProtocolError(
                "response must be an object containing results and only supported metadata"
            )
        if "results" not in body or not isinstance(body["results"], list):
            raise ProviderProtocolError("response.results must be an array")

        provider_name = body.get("provider", self.name)
        provider_model = body.get("model")
        elapsed_ms = round((perf_counter() - started) * 1000)
        raw_results: list[dict[str, Any]] = []
        for item in body["results"]:
            if not isinstance(item, dict):
                raise ProviderProtocolError("each result must be an object")
            normalized = dict(item)
            normalized.setdefault("provider", provider_name)
            normalized.setdefault("model", provider_model)
            normalized.setdefault("latency_ms", elapsed_ms)
            raw_results.append(normalized)

        try:
            results = TypeAdapter(list[TranslationResult]).validate_python(raw_results)
        except ValidationError as exc:
            raise ProviderProtocolError(f"invalid provider results: {exc}") from exc

        result_ids = [result.block_id for result in results]
        if len(set(result_ids)) != len(result_ids):
            raise ProviderProtocolError("provider returned duplicate block IDs")
        requested_set = set(requested_ids)
        result_set = set(result_ids)
        missing = requested_set - result_set
        extra = result_set - requested_set
        if missing or extra:
            raise ProviderProtocolError(
                f"provider block mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
            )
        by_id = {result.block_id: result for result in results}
        return [by_id[block_id] for block_id in requested_ids]


class OpenAIChatCompletionsProvider(TranslationProvider):
    """Batch translator for OpenAI-compatible Chat Completions endpoints.

    The provider uses strict JSON Schema output and validates the response again
    locally.  It never discovers credentials from files or the environment:
    callers must supply ``api_key`` (or an authorization header) explicitly.
    Batching and concurrent calls remain the orchestrator's responsibility.
    """

    _SYSTEM_PROMPT = """\
You are a professional document translator in a format-preserving pipeline.
Translate every input block from the declared source language to the target language.
Return JSON only, conforming exactly to the supplied schema.
Rules:
1. Return every block_id exactly once and never invent or modify an ID.
2. Preserve every __FT_PROTECTED_NNNN__ placeholder exactly once, byte for byte.
3. Preserve meaning, numbers represented by placeholders, terminology, and tone.
4. Do not add explanations, Markdown, comments, or text outside the JSON object.
5. For plain blocks, segments MUST be an empty array.
6. For tagged blocks, return every segment_id exactly once in its original order.
7. translated_text must contain the complete translated block. For tagged blocks,
   segments must contain the corresponding translated formatting-run text.
8. For tagged blocks, concatenating every segments[].text in order MUST equal
   translated_text exactly, including spaces and line breaks.
9. Treat protected placeholders as opaque atomic tokens: preserve them exactly,
   never convert their values, and never add translated currencies or units around
   them.
10. Preserve proper names of people, companies, organizations, brands, and products
    in their original script unless the glossary supplies a target-language form or
    a well-established official target-language name exists. Never translate an
    unfamiliar proper name word-by-word.
11. Use the same target-language term for the same source term across all blocks in
    this batch unless the context clearly requires a different sense.
"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        headers: Mapping[str, str] | None = None,
        name: str = "openai-compatible",
        temperature: float | None = 0.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")

        self.name = name
        self._model = model
        self._url = self._chat_completions_url(base_url)
        self._timeout = httpx.Timeout(timeout_seconds)
        self._temperature = temperature
        self._owns_client = client is None
        self._headers = dict(headers or {})
        if api_key is not None:
            authorization_key = next(
                (key for key in self._headers if key.casefold() == "authorization"),
                "Authorization",
            )
            self._headers[authorization_key] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient()

    async def __aenter__(self) -> OpenAIChatCompletionsProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _chat_completions_url(base_url: str) -> str:
        """Normalize a host, ``.../v1``, or full endpoint URL."""

        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")

        path = parsed.path.rstrip("/")
        if path.endswith("/chat/completions"):
            endpoint_path = path
        elif path.endswith("/v1"):
            endpoint_path = f"{path}/chat/completions"
        else:
            endpoint_path = f"{path}/v1/chat/completions"
        return urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", ""))

    @staticmethod
    def _response_schema(*, plain_only: bool = False) -> dict[str, Any]:
        segments_schema: dict[str, Any] = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "segment_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["segment_id", "text"],
            },
        }
        if plain_only:
            segments_schema["maxItems"] = 0
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "block_id": {"type": "string"},
                            "translated_text": {"type": "string"},
                            "segments": segments_schema,
                        },
                        "required": [
                            "block_id",
                            "translated_text",
                            "segments",
                        ],
                    },
                }
            },
            "required": ["results"],
        }

    @staticmethod
    def _input_block(block: TranslationBlock) -> dict[str, Any]:
        placeholder_errors = validate_protected_tokens(
            block.source_text, block.protected_tokens
        )
        if placeholder_errors:
            raise ProviderProtocolError(
                f"invalid protected source block {block.block_id!r}: "
                + "; ".join(placeholder_errors)
            )
        return {
            "block_id": block.block_id,
            "protocol": block.protocol,
            "text": block.source_text,
            "segments": [
                {"segment_id": segment.segment_id, "text": segment.text}
                for segment in block.segments
            ],
            "context": block.context,
            "glossary": block.glossary,
            "protected_placeholders": [
                token.placeholder for token in block.protected_tokens
            ],
            "max_length_hint": block.max_length_hint,
        }

    def _request_payload(
        self,
        blocks: Sequence[TranslationBlock],
        *,
        source_language: str,
        target_language: str,
        request_id: str,
    ) -> dict[str, Any]:
        request_document = {
            "request_id": request_id,
            "source_language": source_language,
            "target_language": target_language,
            "blocks": [self._input_block(block) for block in blocks],
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        request_document,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "translation_batch",
                    "strict": True,
                    "schema": self._response_schema(
                        plain_only=all(block.protocol == "plain" for block in blocks)
                    ),
                },
            },
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        return payload

    @staticmethod
    def _completion_content(body: Any) -> tuple[str, str | None, dict[str, Any]]:
        if not isinstance(body, dict):
            raise ProviderProtocolError("Chat Completions response must be an object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderProtocolError(
                "Chat Completions response must contain at least one choice"
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderProtocolError("Chat Completions choice must be an object")
        finish_reason = choice.get("finish_reason")
        if finish_reason not in {None, "stop"}:
            raise ProviderProtocolError(
                f"Chat Completions finished with reason {finish_reason!r}"
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderProtocolError("Chat Completions choice has no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderProtocolError(
                "Chat Completions message content must be a non-empty string"
            )
        model = body.get("model")
        if model is not None and not isinstance(model, str):
            raise ProviderProtocolError("Chat Completions model must be a string")
        usage = body.get("usage", {})
        if not isinstance(usage, dict):
            raise ProviderProtocolError("Chat Completions usage must be an object")
        return content, model, usage

    @staticmethod
    def _parse_json_results(content: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderProtocolError(
                "Chat Completions content is not strict JSON"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"results"}:
            raise ProviderProtocolError(
                "translation JSON must contain only the results field"
            )
        results = payload["results"]
        if not isinstance(results, list):
            raise ProviderProtocolError("translation JSON results must be an array")

        normalized: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict) or set(item) != {
                "block_id",
                "translated_text",
                "segments",
            }:
                raise ProviderProtocolError(
                    "each translation result must contain exactly "
                    "block_id, translated_text, and segments"
                )
            if not isinstance(item["block_id"], str):
                raise ProviderProtocolError("result block_id must be a string")
            if not isinstance(item["translated_text"], str):
                raise ProviderProtocolError("result translated_text must be a string")
            segments = item["segments"]
            if not isinstance(segments, list):
                raise ProviderProtocolError("result segments must be an array")
            for segment in segments:
                if (
                    not isinstance(segment, dict)
                    or set(segment) != {"segment_id", "text"}
                    or not isinstance(segment["segment_id"], str)
                    or not isinstance(segment["text"], str)
                ):
                    raise ProviderProtocolError(
                        "each segment must contain string segment_id and text fields"
                    )
            normalized.append(item)
        return normalized

    @staticmethod
    def _usage_value(usage: Mapping[str, Any], key: str) -> int | None:
        value = usage.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProviderProtocolError(
                f"Chat Completions usage.{key} must be a non-negative integer"
            )
        return value

    def _validate_and_normalize_results(
        self,
        *,
        raw_results: Sequence[dict[str, Any]],
        blocks: Sequence[TranslationBlock],
        response_model: str | None,
        usage: Mapping[str, Any],
        elapsed_ms: int,
    ) -> list[TranslationResult]:
        requested_ids = [block.block_id for block in blocks]
        result_ids = [result["block_id"] for result in raw_results]
        if len(result_ids) != len(set(result_ids)):
            raise ProviderProtocolError("provider returned duplicate block IDs")
        missing = set(requested_ids) - set(result_ids)
        extra = set(result_ids) - set(requested_ids)
        if missing or extra:
            raise ProviderProtocolError(
                f"provider block mismatch; missing={sorted(missing)}, "
                f"extra={sorted(extra)}"
            )

        prompt_tokens = self._usage_value(usage, "prompt_tokens")
        completion_tokens = self._usage_value(usage, "completion_tokens")
        by_raw_id = {result["block_id"]: result for result in raw_results}
        normalized: list[TranslationResult] = []
        for index, block in enumerate(blocks):
            raw = by_raw_id[block.block_id]
            translated_text = raw["translated_text"]
            if block.source_text.strip() and not translated_text.strip():
                raise ProviderProtocolError(
                    f"provider returned empty translation for {block.block_id!r}"
                )
            placeholder_errors = validate_protected_tokens(
                translated_text, block.protected_tokens
            )
            if placeholder_errors:
                raise ProviderProtocolError(
                    f"protected token mismatch for {block.block_id!r}: "
                    + "; ".join(placeholder_errors)
                )

            raw_segments = raw["segments"]
            source_segment_ids = [segment.segment_id for segment in block.segments]
            result_segment_ids = [segment["segment_id"] for segment in raw_segments]
            if block.protocol == "tagged":
                if len(source_segment_ids) != len(set(source_segment_ids)):
                    raise ProviderProtocolError(
                        f"source block {block.block_id!r} contains duplicate segment IDs"
                    )
                if len(result_segment_ids) != len(set(result_segment_ids)):
                    raise ProviderProtocolError(
                        f"provider returned duplicate segment IDs for {block.block_id!r}"
                    )
                if result_segment_ids != source_segment_ids:
                    source_by_id = {
                        segment.segment_id: segment for segment in block.segments
                    }
                    result_id_set = set(result_segment_ids)
                    missing_segment_ids = [
                        segment_id
                        for segment_id in source_segment_ids
                        if segment_id not in result_id_set
                    ]
                    expected_returned_ids = [
                        segment_id
                        for segment_id in source_segment_ids
                        if segment_id in result_id_set
                    ]
                    may_restore_whitespace = (
                        not (result_id_set - set(source_segment_ids))
                        and result_segment_ids == expected_returned_ids
                        and all(
                            source_by_id[segment_id].text.isspace()
                            for segment_id in missing_segment_ids
                        )
                    )
                    if not may_restore_whitespace:
                        raise ProviderProtocolError(
                            f"segment mismatch for {block.block_id!r}; expected "
                            f"{source_segment_ids!r}, got {result_segment_ids!r}"
                        )
                    returned_by_id = {
                        segment["segment_id"]: segment for segment in raw_segments
                    }
                    raw_segments = [
                        returned_by_id.get(
                            segment.segment_id,
                            {
                                "segment_id": segment.segment_id,
                                "text": segment.text,
                            },
                        )
                        for segment in block.segments
                    ]
                segment_placeholder_errors = validate_protected_tokens(
                    "".join(segment["text"] for segment in raw_segments),
                    block.protected_tokens,
                )
                if segment_placeholder_errors:
                    raise ProviderProtocolError(
                        f"protected segment token mismatch for {block.block_id!r}: "
                        + "; ".join(segment_placeholder_errors)
                    )
            elif result_segment_ids:
                raise ProviderProtocolError(
                    f"plain block {block.block_id!r} returned unexpected segments"
                )

            result_usage = ProviderUsage(
                input_characters=len(block.source_text),
                output_characters=len(translated_text),
                input_tokens=prompt_tokens if index == 0 else None,
                output_tokens=completion_tokens if index == 0 else None,
            )
            metadata: dict[str, Any] = {}
            if index == 0 and (
                prompt_tokens is not None or completion_tokens is not None
            ):
                metadata["batch_usage"] = True
            normalized.append(
                TranslationResult(
                    block_id=block.block_id,
                    translated_text=translated_text,
                    segments=[
                        TextSegment(
                            segment_id=segment["segment_id"],
                            text=segment["text"],
                        )
                        for segment in raw_segments
                    ],
                    provider=self.name,
                    model=response_model or self._model,
                    latency_ms=elapsed_ms,
                    usage=result_usage,
                    metadata=metadata,
                )
            )
        return normalized

    async def translate(
        self,
        blocks: Sequence[TranslationBlock],
        *,
        source_language: str,
        target_language: str,
        request_id: str,
    ) -> list[TranslationResult]:
        requested_ids = [block.block_id for block in blocks]
        if len(requested_ids) != len(set(requested_ids)):
            raise ProviderProtocolError("request contains duplicate block IDs")
        if not blocks:
            return []

        payload = self._request_payload(
            blocks,
            source_language=source_language,
            target_language=target_language,
            request_id=request_id,
        )
        started = perf_counter()
        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.TransportError as exc:
            raise RetryableProviderError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc)) from exc

        if response.status_code == 429 or 500 <= response.status_code < 600:
            raise RetryableProviderError(
                f"provider returned HTTP {response.status_code}"
            )
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"provider returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderProtocolError(
                "Chat Completions response is not valid JSON"
            ) from exc
        content, response_model, usage = self._completion_content(body)
        raw_results = self._parse_json_results(content)
        elapsed_ms = round((perf_counter() - started) * 1000)
        return self._validate_and_normalize_results(
            raw_results=raw_results,
            blocks=blocks,
            response_model=response_model,
            usage=usage,
            elapsed_ms=elapsed_ms,
        )


class FixtureProcessingProvider(ProcessingProvider):
    """Deterministic processing provider useful for pipeline tests."""

    name = "fixture-processing"

    async def semantic_qa(
        self,
        *,
        source: TranslationBlock,
        translation: TranslationResult,
        request_id: str,
    ) -> SemanticQaResult:
        del source, request_id
        passed = bool(translation.translated_text.strip())
        issues = (
            []
            if passed
            else [
                QaIssue(
                    code="empty_translation",
                    message="translation is empty",
                    block_id=translation.block_id,
                    stage="semantic_qa",
                )
            ]
        )
        return SemanticQaResult(
            block_id=translation.block_id,
            passed=passed,
            issues=issues,
            provider=self.name,
        )

    async def fit_text(
        self,
        *,
        source: TranslationBlock,
        translation: TranslationResult,
        max_length: int,
        request_id: str,
    ) -> TextFitResult:
        del source, request_id
        if max_length < 1:
            raise ValueError("max_length must be positive")
        fitted = translation.translated_text[:max_length]
        return TextFitResult(
            block_id=translation.block_id,
            fitted_text=fitted,
            provider=self.name,
            warnings=(
                ["fixture provider truncated text"]
                if fitted != translation.translated_text
                else []
            ),
        )
