"""OpenAI-compatible semantic QA, diagnosis, planning, and text-fit provider."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from math import isfinite
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import (
    AgentState,
    AuditEvent,
    IssueSeverity,
    QaDiagnosis,
    QaIssue,
    QaRepairSpec,
    QaRootCause,
    RepairAction,
    RepairActionType,
    RepairPlan,
    SemanticQaResult,
    TextFitResult,
    TranslationBlock,
    TranslationResult,
)
from .protection import (
    ProtectionValidationError,
    protect_text,
    restore_text,
    validate_protected_tokens,
)
from .providers import (
    ProcessingProvider,
    ProviderError,
    ProviderProtocolError,
    RetryableProviderError,
)


class QaRepairProcessingProvider(ProcessingProvider, ABC):
    """Extended processing contract used by the bounded QA/repair agent."""

    @abstractmethod
    async def diagnose(
        self,
        *,
        spec: QaRepairSpec,
        request_id: str,
    ) -> QaDiagnosis:
        """Diagnose QA issues without modifying document content."""

    @abstractmethod
    async def plan(
        self,
        *,
        spec: QaRepairSpec,
        diagnosis: QaDiagnosis,
        request_id: str,
    ) -> RepairPlan:
        """Create a bounded repair plan using only actions allowed by the spec."""


class OpenAIProcessingProvider(QaRepairProcessingProvider):
    """QA/repair language provider using OpenAI-compatible Chat Completions.

    Credentials are accepted only as constructor arguments.  This class never
    reads dotenv files or environment variables, and transport errors never
    include request headers or response bodies.
    """

    manages_semantic_qa_retries = True

    _SYSTEM_PROMPT = """\
You are the language QA component of a format-preserving document pipeline.
Follow the requested task and return JSON only, exactly matching the supplied schema.
Never change, omit, duplicate, or translate __FT_PROTECTED_NNNN__ placeholders.
For Word documents, reason about paragraphs, tables, and sections only.
For PowerPoint documents, reason about slides, shapes, and text boxes only.
Preserve Word section dimensions and PowerPoint slide dimensions, object positions,
media, themes, and all content that is not explicitly targeted by a repair action.
For PowerPoint text overflow, prefer native autofit first, then safe text-box margin
adjustment, then a readable font-size adjustment, and only then expand the shape
within slide bounds. Never reduce text below the supplied minimum readable size.
When coordinates drifted, restore the exact source geometry. Never move or resize
a shape without source-coordinate or overflow evidence. Every PowerPoint repair
action must reference its slide and shape_id.
Use only the explicitly allowed repair actions. Do not include Markdown or commentary.
For semantic_qa, passed must be true exactly when there are no error-severity
issues. Warning- and info-severity issues alone do not make passed false.
For retranslate, terminology_fix, placeholder_restore, and replace_translation,
return the complete replacement in proposed_text. For fit_text, proposed_text may be
null so the dedicated text-fit step can produce it. All other actions must return
proposed_text as null.
"""

    _DIRECT_TEXT_REPAIR_ACTIONS = frozenset(
        {
            RepairActionType.RETRANSLATE,
            RepairActionType.TERMINOLOGY_FIX,
            RepairActionType.PLACEHOLDER_RESTORE,
            RepairActionType.REPLACE_TRANSLATION,
        }
    )

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        headers: Mapping[str, str] | None = None,
        name: str = "openai-compatible-processing",
        temperature: float | None = 0.0,
        max_request_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
        retry_jitter_seconds: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if (
            isinstance(max_request_retries, bool)
            or not isinstance(max_request_retries, int)
            or not 0 <= max_request_retries <= 5
        ):
            raise ValueError("max_request_retries must be between 0 and 5")
        if not isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if not isfinite(retry_jitter_seconds) or not 0 <= retry_jitter_seconds <= 1:
            raise ValueError("retry_jitter_seconds must be between 0 and 1")

        self.name = name
        self._model = model
        self._url = self._normalize_url(base_url)
        # timeout_seconds is the absolute budget for one logical provider
        # request, including every HTTP attempt and every retry delay.
        self._request_deadline_seconds = timeout_seconds
        self._temperature = temperature
        self._max_request_retries = max_request_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_jitter_seconds = retry_jitter_seconds
        self._owns_client = client is None
        self._headers = dict(headers or {})
        if api_key is not None:
            authorization_key = next(
                (key for key in self._headers if key.casefold() == "authorization"),
                "Authorization",
            )
            self._headers[authorization_key] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient()

    async def __aenter__(self) -> OpenAIProcessingProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _normalize_url(base_url: str) -> str:
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

    async def _request_json(
        self,
        *,
        task: str,
        request_id: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, int, dict[str, int]]:
        request_document = {
            "task": task,
            "request_id": request_id,
            "input": input_data,
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
                    "name": task,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature

        started = perf_counter()
        deadline = started + self._request_deadline_seconds
        response: httpx.Response | None = None
        http_attempts = 0
        for retry_index in range(self._max_request_retries + 1):
            remaining = deadline - perf_counter()
            if remaining <= 0:
                raise RetryableProviderError(
                    "provider request deadline exhausted after "
                    f"{http_attempts} HTTP attempts",
                    http_attempts=http_attempts,
                )
            http_attempts += 1
            try:
                response = await asyncio.wait_for(
                    self._client.post(
                        self._url,
                        json=payload,
                        headers=self._headers,
                        timeout=httpx.Timeout(remaining),
                    ),
                    timeout=remaining,
                )
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if retry_index < self._max_request_retries:
                    await self._wait_before_retry(
                        retry_index=retry_index,
                        request_id=request_id,
                        deadline=deadline,
                        http_attempts=http_attempts,
                        failure="provider transport failed",
                    )
                    continue
                raise RetryableProviderError(
                    f"provider transport failed after {http_attempts} HTTP attempts",
                    http_attempts=http_attempts,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(
                    str(exc), http_attempts=http_attempts
                ) from exc

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if retry_index < self._max_request_retries:
                    await self._wait_before_retry(
                        retry_index=retry_index,
                        request_id=request_id,
                        deadline=deadline,
                        http_attempts=http_attempts,
                        failure=f"provider returned HTTP {response.status_code}",
                        response=response,
                    )
                    continue
                raise RetryableProviderError(
                    f"provider returned HTTP {response.status_code} after "
                    f"{http_attempts} HTTP attempts",
                    http_attempts=http_attempts,
                )
            break

        if response is None:  # pragma: no cover - defensive loop invariant
            raise RetryableProviderError(
                "provider request produced no response",
                http_attempts=http_attempts,
            )
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"provider returned HTTP {response.status_code}",
                http_attempts=http_attempts,
            )

        try:
            envelope = response.json()
        except ValueError as exc:
            raise ProviderProtocolError(
                "Chat Completions response is not valid JSON",
                http_attempts=http_attempts,
            ) from exc
        if not isinstance(envelope, dict):
            raise ProviderProtocolError(
                "Chat Completions response must be an object",
                http_attempts=http_attempts,
            )
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderProtocolError(
                "Chat Completions response must contain at least one choice",
                http_attempts=http_attempts,
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderProtocolError(
                "Chat Completions choice must be an object",
                http_attempts=http_attempts,
            )
        finish_reason = choice.get("finish_reason")
        if finish_reason not in {None, "stop"}:
            raise ProviderProtocolError(
                f"Chat Completions finished with reason {finish_reason!r}",
                http_attempts=http_attempts,
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderProtocolError(
                "Chat Completions choice has no message",
                http_attempts=http_attempts,
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderProtocolError(
                "Chat Completions message content must be a non-empty string",
                http_attempts=http_attempts,
            )
        try:
            output = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderProtocolError(
                "Chat Completions content is not strict JSON",
                http_attempts=http_attempts,
            ) from exc
        if not isinstance(output, dict):
            raise ProviderProtocolError(
                "agent output must be a JSON object",
                http_attempts=http_attempts,
            )

        response_model = envelope.get("model")
        if response_model is not None and not isinstance(response_model, str):
            raise ProviderProtocolError(
                "Chat Completions model must be a string",
                http_attempts=http_attempts,
            )
        usage = envelope.get("usage", {})
        if not isinstance(usage, dict):
            raise ProviderProtocolError(
                "Chat Completions usage must be an object",
                http_attempts=http_attempts,
            )
        normalized_usage: dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderProtocolError(
                    f"Chat Completions usage.{key} must be a non-negative integer",
                    http_attempts=http_attempts,
                )
            normalized_usage[key] = value
        normalized_usage["http_attempts"] = http_attempts
        elapsed_ms = round((perf_counter() - started) * 1000)
        return output, response_model, elapsed_ms, normalized_usage

    def _retry_delay_seconds(
        self,
        retry_index: int,
        *,
        request_id: str,
        response: httpx.Response | None = None,
        now: datetime | None = None,
    ) -> float:
        """Return deterministic backoff plus a standards-compliant Retry-After."""

        delay = self._retry_backoff_seconds * (2**retry_index)
        if self._retry_jitter_seconds:
            digest = sha256(f"{request_id}:{retry_index}".encode()).digest()
            fraction = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
            delay += self._retry_jitter_seconds * fraction
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                parsed_retry_after = self._parse_retry_after(
                    retry_after,
                    now=now,
                )
                if parsed_retry_after is not None:
                    delay = max(delay, parsed_retry_after)
        return max(delay, 0.0)

    @staticmethod
    def _parse_retry_after(
        value: str,
        *,
        now: datetime | None = None,
    ) -> float | None:
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return max((retry_at - current).total_seconds(), 0.0)
        if not isfinite(seconds) or seconds < 0:
            return None
        return seconds

    @staticmethod
    def _audit_usage(usage: dict[str, int]) -> tuple[dict[str, int], int]:
        token_usage = dict(usage)
        http_attempts = token_usage.pop("http_attempts", 1)
        return token_usage, http_attempts

    async def _wait_before_retry(
        self,
        *,
        retry_index: int,
        request_id: str,
        deadline: float,
        http_attempts: int,
        failure: str,
        response: httpx.Response | None = None,
    ) -> None:
        delay = self._retry_delay_seconds(
            retry_index,
            request_id=request_id,
            response=response,
        )
        remaining = deadline - perf_counter()
        if remaining <= 0 or delay >= remaining:
            raise RetryableProviderError(
                f"{failure}; retry budget exhausted after "
                f"{http_attempts} HTTP attempts",
                http_attempts=http_attempts,
            )
        await asyncio.sleep(delay)

    @staticmethod
    def _object_schema(
        properties: dict[str, Any],
        *,
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required or list(properties),
        }

    @classmethod
    def _semantic_qa_schema(cls) -> dict[str, Any]:
        issue_schema = cls._object_schema(
            {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "severity": {
                    "type": "string",
                    "enum": [severity.value for severity in IssueSeverity],
                },
                "recoverable": {"type": "boolean"},
            }
        )
        return cls._object_schema(
            {
                "passed": {"type": "boolean"},
                "issues": {"type": "array", "items": issue_schema},
            }
        )

    async def semantic_qa(
        self,
        *,
        source: TranslationBlock,
        translation: TranslationResult,
        request_id: str,
    ) -> SemanticQaResult:
        if source.block_id != translation.block_id:
            raise ProviderProtocolError(
                "source and translation block IDs differ",
                http_attempts=0,
            )
        output, model, elapsed_ms, usage = await self._request_json(
            task="semantic_qa",
            request_id=request_id,
            input_data={
                "block_id": source.block_id,
                "source_language": source.source_language,
                "target_language": source.target_language,
                "source_text": source.source_text,
                "translated_text": translation.translated_text,
                "context": source.context,
                "glossary": source.glossary,
                "checks": [
                    "meaning",
                    "omissions",
                    "negation",
                    "numbers",
                    "terminology",
                    "tone",
                ],
            },
            schema=self._semantic_qa_schema(),
        )
        semantic_http_attempts = usage.get("http_attempts")
        if set(output) != {"passed", "issues"}:
            raise ProviderProtocolError(
                "semantic QA output must contain only passed and issues",
                http_attempts=semantic_http_attempts,
            )
        if not isinstance(output["passed"], bool) or not isinstance(
            output["issues"], list
        ):
            raise ProviderProtocolError(
                "semantic QA output has invalid field types",
                http_attempts=semantic_http_attempts,
            )

        issues: list[QaIssue] = []
        for index, raw_issue in enumerate(output["issues"], start=1):
            if not isinstance(raw_issue, dict) or set(raw_issue) != {
                "code",
                "message",
                "severity",
                "recoverable",
            }:
                raise ProviderProtocolError(
                    "semantic QA issue has invalid fields",
                    http_attempts=semantic_http_attempts,
                )
            try:
                issue = QaIssue(
                    issue_id=f"{request_id}-issue-{index}",
                    code=raw_issue["code"],
                    message=raw_issue["message"],
                    severity=raw_issue["severity"],
                    recoverable=raw_issue["recoverable"],
                    stage="semantic_qa",
                    block_id=source.block_id,
                )
            except (TypeError, ValueError) as exc:
                raise ProviderProtocolError(
                    f"semantic QA issue {index} is invalid",
                    http_attempts=semantic_http_attempts,
                ) from exc
            issues.append(issue)

        has_error = any(issue.severity == IssueSeverity.ERROR for issue in issues)
        consistency_recovery: str | None = None
        if output["passed"] and has_error:
            # Never let an optimistic summary flag override concrete error
            # evidence returned by the same response.
            consistency_recovery = "derived_failure_from_error_issues"
        elif not output["passed"] and not has_error:
            # A provider-level failure assertion is quality evidence even when
            # the model mislabeled every detail as warning/info.  Preserve the
            # original issues and add a blocking, block-scoped review marker so
            # this recovery can never turn an unavailable/inconsistent check
            # into a pass.
            issues.append(
                QaIssue(
                    issue_id=f"{request_id}-provider-review-required",
                    code="SEMANTIC_QA_REVIEW_REQUIRED",
                    message=(
                        "Semantic QA marked this block as failed but returned no "
                        "error-severity issue; provider issues were retained and "
                        "the block requires review"
                    ),
                    severity=IssueSeverity.ERROR,
                    recoverable=True,
                    stage="semantic_qa",
                    block_id=source.block_id,
                    details={
                        "status": "provider_output_inconsistent",
                        "provider_passed": False,
                        "provider_issue_count": len(output["issues"]),
                    },
                )
            )
            consistency_recovery = "preserved_provider_failure_as_review"
        passed = not any(
            issue.severity == IssueSeverity.ERROR for issue in issues
        )
        token_usage, http_attempts = self._audit_usage(usage)
        audit = self._audit_event(
            request_id=request_id,
            event_type="semantic_qa_completed",
            state=AgentState.VERIFYING,
            model=model,
            details={
                "issue_count": len(issues),
                "usage": token_usage,
                "http_attempts": http_attempts,
                "consistency_recovery": consistency_recovery,
            },
        )
        return SemanticQaResult(
            block_id=source.block_id,
            passed=passed,
            issues=issues,
            provider=self.name,
            model=model or self._model,
            latency_ms=elapsed_ms,
            metadata={"audit_events": [audit.model_dump(mode="json")]},
        )

    @classmethod
    def _fit_text_schema(
        cls,
        *,
        max_protected_characters: int | None = None,
    ) -> dict[str, Any]:
        fitted_text_schema: dict[str, Any] = {"type": "string"}
        if max_protected_characters is not None:
            fitted_text_schema["maxLength"] = max_protected_characters
        return cls._object_schema(
            {
                "fitted_text": fitted_text_schema,
                "meaning_preserved": {"type": "boolean"},
                "warnings": {"type": "array", "items": {"type": "string"}},
            }
        )

    async def fit_text(
        self,
        *,
        source: TranslationBlock,
        translation: TranslationResult,
        max_length: int,
        request_id: str,
    ) -> TextFitResult:
        if source.block_id != translation.block_id:
            raise ProviderProtocolError("source and translation block IDs differ")
        if max_length < 1:
            raise ValueError("max_length must be positive")

        protected = protect_text(translation.translated_text)
        restoration_character_delta = sum(
            len(token.value) - len(token.placeholder) for token in protected.tokens
        )
        max_protected_characters = max_length - restoration_character_delta
        minimum_protected_characters = sum(
            len(token.placeholder) for token in protected.tokens
        )
        if max_protected_characters < minimum_protected_characters:
            raise ProviderProtocolError(
                "fit-text maximum cannot preserve all protected tokens"
            )
        output, model, elapsed_ms, usage = await self._request_json(
            task="fit_text",
            request_id=request_id,
            input_data={
                "block_id": source.block_id,
                "source_text": source.source_text,
                "current_translation": protected.protected_text,
                "protected_placeholders": [
                    token.placeholder for token in protected.tokens
                ],
                "protected_token_lengths": [
                    {
                        "placeholder": token.placeholder,
                        "placeholder_characters": len(token.placeholder),
                        "restored_characters": len(token.value),
                    }
                    for token in protected.tokens
                ],
                "max_protected_characters": max_protected_characters,
                "max_restored_characters": max_length,
                "instruction": (
                    "Shorten the current translation without changing meaning. "
                    "Preserve every placeholder exactly once. The fitted_text "
                    "field must contain no more than max_protected_characters "
                    "characters before placeholder restoration and no more than "
                    "max_restored_characters characters after restoration. Both "
                    "limits are hard requirements. Never truncate a word, clause, "
                    "sentence, name, title, or source fact to hit the limit. Set "
                    "meaning_preserved to true only when the fitted text is "
                    "grammatically complete and retains every material source fact; "
                    "otherwise set it to false so the caller can safely reject it."
                ),
            },
            schema=self._fit_text_schema(
                max_protected_characters=max_protected_characters
            ),
        )
        if set(output) != {"fitted_text", "meaning_preserved", "warnings"}:
            raise ProviderProtocolError(
                "fit-text output must contain only fitted_text, meaning_preserved, "
                "and warnings"
            )
        if (
            not isinstance(output["fitted_text"], str)
            or not isinstance(output["meaning_preserved"], bool)
            or not isinstance(output["warnings"], list)
            or any(not isinstance(item, str) for item in output["warnings"])
        ):
            raise ProviderProtocolError("fit-text output has invalid field types")
        if output["meaning_preserved"] is not True:
            raise ProviderProtocolError(
                "fit-text provider could not preserve complete meaning within "
                "the requested limit"
            )
        placeholder_errors = validate_protected_tokens(
            output["fitted_text"], protected.tokens
        )
        if placeholder_errors:
            raise ProviderProtocolError(
                "fit-text protected token mismatch: " + "; ".join(placeholder_errors)
            )
        try:
            fitted_text = restore_text(output["fitted_text"], protected.tokens)
        except ProtectionValidationError as exc:
            raise ProviderProtocolError(str(exc)) from exc
        if len(fitted_text) > max_length:
            raise ProviderProtocolError(
                f"fitted text has {len(fitted_text)} characters; "
                f"maximum is {max_length}"
            )
        token_usage, http_attempts = self._audit_usage(usage)
        audit = self._audit_event(
            request_id=request_id,
            event_type="fit_text_completed",
            state=AgentState.REPAIRING,
            model=model,
            details={
                "input_characters": len(translation.translated_text),
                "output_characters": len(fitted_text),
                "usage": token_usage,
                "http_attempts": http_attempts,
            },
        )
        return TextFitResult(
            block_id=source.block_id,
            fitted_text=fitted_text,
            provider=self.name,
            model=model or self._model,
            latency_ms=elapsed_ms,
            warnings=output["warnings"],
            metadata={"audit_events": [audit.model_dump(mode="json")]},
        )

    @classmethod
    def _diagnosis_schema(cls) -> dict[str, Any]:
        action_values = [action.value for action in RepairActionType]
        root_cause = cls._object_schema(
            {
                "code": {"type": "string"},
                "description": {"type": "string"},
                "issue_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "repairable": {"type": "boolean"},
                "recommended_action": {
                    "type": "string",
                    "enum": action_values,
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
            }
        )
        return cls._object_schema(
            {
                "summary": {"type": "string"},
                "root_causes": {"type": "array", "items": root_cause},
                "repairable_issue_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "needs_human_review": {"type": "boolean"},
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
            }
        )

    @staticmethod
    def _issue_ref(issue: QaIssue, index: int) -> str:
        return issue.issue_id or f"issue-{index + 1}"

    @classmethod
    def _spec_input(cls, spec: QaRepairSpec) -> dict[str, Any]:
        translation_by_id = {result.block_id: result for result in spec.translations}
        issue_refs = [
            (cls._issue_ref(issue, index), issue)
            for index, issue in enumerate(spec.issues)
        ]
        issue_ids_by_block: dict[str, list[str]] = {}
        for issue_id, issue in issue_refs:
            if issue.block_id is not None:
                issue_ids_by_block.setdefault(issue.block_id, []).append(issue_id)
        return {
            "job_id": spec.job_id,
            "document_format": spec.document_format,
            "source_language": spec.source_language,
            "target_language": spec.target_language,
            "strict_layout": spec.strict_layout,
            "preserve_section_or_slide_count": spec.strict_page_count,
            "allowed_actions": [action.value for action in spec.allowed_actions],
            "issues": [
                {
                    "issue_id": issue_id,
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity.value,
                    "block_id": issue.block_id,
                    "slide": issue.slide,
                    "office_location": cls._office_location(
                        issue.details, spec.document_format
                    ),
                    "recoverable": issue.recoverable,
                }
                for issue_id, issue in issue_refs
            ],
            "blocks": [
                {
                    "block_id": block.block_id,
                    "source_text": block.source_text,
                    "translated_text": (
                        translation_by_id[block.block_id].translated_text
                        if block.block_id in translation_by_id
                        else None
                    ),
                    "protocol": block.protocol,
                    "max_length_hint": block.max_length_hint,
                    "issue_ids": issue_ids_by_block.get(block.block_id, []),
                    "office_location": cls._office_location(
                        block.metadata, spec.document_format
                    ),
                }
                for block in spec.blocks
                if (
                    block.block_id in translation_by_id
                    or block.block_id in issue_ids_by_block
                )
            ],
        }

    @staticmethod
    def _office_location(
        metadata: Mapping[str, Any],
        document_format: str,
    ) -> dict[str, Any]:
        if document_format == "docx":
            allowed = {
                "part_name",
                "xpath",
                "paragraph_index",
                "table_index",
                "row_index",
                "cell_index",
                "section_index",
            }
        else:
            allowed = {
                "slide",
                "slide_index",
                "shape_id",
                "shape_name",
                "textbox_id",
                "placeholder_type",
            }
        location = {
            key: value
            for key, value in metadata.items()
            if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
        }
        if document_format == "pptx":
            for geometry_key in (
                "geometry",
                "source_geometry",
                "current_geometry",
            ):
                geometry = metadata.get(geometry_key)
                if not isinstance(geometry, Mapping):
                    continue
                filtered_geometry = {
                    key: value
                    for key, value in geometry.items()
                    if key in {"x", "y", "width", "height", "unit"}
                    and isinstance(value, (str, int, float))
                }
                if filtered_geometry:
                    location[geometry_key] = filtered_geometry
        return location

    async def diagnose(
        self,
        *,
        spec: QaRepairSpec,
        request_id: str,
    ) -> QaDiagnosis:
        output, model, elapsed_ms, usage = await self._request_json(
            task="qa_diagnosis",
            request_id=request_id,
            input_data=self._spec_input(spec),
            schema=self._diagnosis_schema(),
        )
        expected_fields = {
            "summary",
            "root_causes",
            "repairable_issue_ids",
            "needs_human_review",
            "confidence",
        }
        if set(output) != expected_fields:
            raise ProviderProtocolError("diagnosis output has invalid fields")
        if not isinstance(output["root_causes"], list) or not isinstance(
            output["repairable_issue_ids"], list
        ):
            raise ProviderProtocolError("diagnosis arrays have invalid types")

        known_issue_ids = {
            self._issue_ref(issue, index) for index, issue in enumerate(spec.issues)
        }
        allowed_actions = set(spec.allowed_actions)
        root_causes: list[QaRootCause] = []
        for index, raw_cause in enumerate(output["root_causes"], start=1):
            try:
                cause = QaRootCause.model_validate(raw_cause)
            except (TypeError, ValueError) as exc:
                raise ProviderProtocolError(
                    f"diagnosis root cause {index} is invalid"
                ) from exc
            unknown = set(cause.issue_ids) - known_issue_ids
            if unknown:
                raise ProviderProtocolError(
                    f"diagnosis references unknown issues {sorted(unknown)}"
                )
            if cause.recommended_action not in allowed_actions:
                raise ProviderProtocolError(
                    "diagnosis recommends an action disallowed by the spec"
                )
            root_causes.append(cause)

        repairable_ids = output["repairable_issue_ids"]
        if any(not isinstance(issue_id, str) for issue_id in repairable_ids):
            raise ProviderProtocolError(
                "repairable_issue_ids must contain only strings"
            )
        unknown_repairable = set(repairable_ids) - known_issue_ids
        if unknown_repairable:
            raise ProviderProtocolError(
                "diagnosis references unknown repairable issues "
                f"{sorted(unknown_repairable)}"
            )
        token_usage, http_attempts = self._audit_usage(usage)
        audit = self._audit_event(
            request_id=request_id,
            event_type="diagnosis_completed",
            state=AgentState.DIAGNOSING,
            model=model,
            details={
                "root_cause_count": len(root_causes),
                "usage": token_usage,
                "http_attempts": http_attempts,
            },
        )
        try:
            return QaDiagnosis(
                request_id=request_id,
                state=AgentState.PLANNING,
                summary=output["summary"],
                root_causes=root_causes,
                issue_ids=sorted(known_issue_ids),
                repairable_issue_ids=repairable_ids,
                needs_human_review=output["needs_human_review"],
                confidence=output["confidence"],
                provider=self.name,
                model=model or self._model,
                latency_ms=elapsed_ms,
                audit_events=[audit],
            )
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError("diagnosis output is invalid") from exc

    @classmethod
    def _plan_schema(cls) -> dict[str, Any]:
        nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        nullable_integer = {
            "anyOf": [
                {"type": "integer", "minimum": 1},
                {"type": "null"},
            ]
        }
        nullable_font_size = {
            "anyOf": [
                {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 400,
                },
                {"type": "null"},
            ]
        }
        nullable_scale = {
            "anyOf": [
                {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 4,
                },
                {"type": "null"},
            ]
        }
        nullable_geometry = {
            "anyOf": [
                cls._object_schema(
                    {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "width": {"type": "number", "exclusiveMinimum": 0},
                        "height": {"type": "number", "exclusiveMinimum": 0},
                        "unit": {
                            "type": "string",
                            "enum": ["points", "emu"],
                        },
                    }
                ),
                {"type": "null"},
            ]
        }
        nullable_margins = {
            "anyOf": [
                cls._object_schema(
                    {
                        "left": {"type": "number", "minimum": 0},
                        "right": {"type": "number", "minimum": 0},
                        "top": {"type": "number", "minimum": 0},
                        "bottom": {"type": "number", "minimum": 0},
                    }
                ),
                {"type": "null"},
            ]
        }
        action = cls._object_schema(
            {
                "block_id": nullable_string,
                "action_type": {
                    "type": "string",
                    "enum": [action_type.value for action_type in RepairActionType],
                },
                "issue_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "instruction": {"type": "string"},
                "rationale": {"type": "string"},
                "max_length": nullable_integer,
                "proposed_text": nullable_string,
                "slide": nullable_integer,
                "shape_id": nullable_string,
                "geometry": nullable_geometry,
                "textbox_margins": nullable_margins,
                "autofit_mode": {
                    "anyOf": [
                        {
                            "type": "string",
                            "enum": ["shrink_text", "resize_shape"],
                        },
                        {"type": "null"},
                    ]
                },
                "font_size_pt": nullable_font_size,
                "min_font_size_pt": nullable_font_size,
                "scale": nullable_scale,
                "clamp_to_slide": {"type": "boolean"},
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
            }
        )
        return cls._object_schema(
            {
                "actions": {"type": "array", "items": action},
                "rationale": {"type": "string"},
                "needs_human_review": {"type": "boolean"},
            }
        )

    async def plan(
        self,
        *,
        spec: QaRepairSpec,
        diagnosis: QaDiagnosis,
        request_id: str,
    ) -> RepairPlan:
        plan_input = self._spec_input(spec)
        protected_by_block = {}
        for block_input in plan_input["blocks"]:
            translated_text = block_input["translated_text"]
            if translated_text is None:
                block_input["protected_placeholders"] = []
                continue
            protected = protect_text(translated_text)
            protected_by_block[block_input["block_id"]] = protected
            block_input["translated_text"] = protected.protected_text
            block_input["protected_placeholders"] = [
                token.placeholder for token in protected.tokens
            ]
        plan_input["diagnosis"] = diagnosis.model_dump(mode="json")
        plan_input["max_rounds"] = spec.max_rounds
        output, model, elapsed_ms, usage = await self._request_json(
            task="qa_repair_plan",
            request_id=request_id,
            input_data=plan_input,
            schema=self._plan_schema(),
        )
        if set(output) != {"actions", "rationale", "needs_human_review"}:
            raise ProviderProtocolError("repair plan output has invalid fields")
        if not isinstance(output["actions"], list):
            raise ProviderProtocolError("repair plan actions must be an array")

        known_blocks = {block.block_id for block in spec.blocks}
        known_shape_refs = self._known_shape_refs(spec)
        known_issue_ids = {
            self._issue_ref(issue, index) for index, issue in enumerate(spec.issues)
        }
        allowed_actions = set(spec.allowed_actions)
        actions: list[RepairAction] = []
        for index, raw_action in enumerate(output["actions"], start=1):
            if not isinstance(raw_action, dict) or set(raw_action) != {
                "block_id",
                "action_type",
                "issue_ids",
                "instruction",
                "rationale",
                "max_length",
                "proposed_text",
                "slide",
                "shape_id",
                "geometry",
                "textbox_margins",
                "autofit_mode",
                "font_size_pt",
                "min_font_size_pt",
                "scale",
                "clamp_to_slide",
                "confidence",
            }:
                raise ProviderProtocolError(
                    f"repair plan action {index} has invalid fields"
                )
            try:
                action_type = RepairActionType(raw_action["action_type"])
            except (TypeError, ValueError) as exc:
                raise ProviderProtocolError(
                    f"repair plan action {index} has invalid action_type"
                ) from exc
            if action_type not in allowed_actions:
                raise ProviderProtocolError(
                    f"repair plan action {index} is disallowed by the spec"
                )
            powerpoint_actions = {
                RepairActionType.ENABLE_AUTOFIT,
                RepairActionType.SET_TEXTBOX_MARGINS,
                RepairActionType.ADJUST_FONT_SIZE,
                RepairActionType.RESOLVE_TEXT_OVERFLOW,
                RepairActionType.RESTORE_SHAPE_COORDINATES,
                RepairActionType.SET_SHAPE_BOUNDS,
                RepairActionType.SET_FONT_SIZE,
            }
            if action_type in powerpoint_actions and spec.document_format != "pptx":
                raise ProviderProtocolError(
                    f"repair plan action {index} is only valid for PPTX"
                )
            word_layout_actions = {
                RepairActionType.INSERT_PAGE_BREAK_BEFORE,
                RepairActionType.REMOVE_PAGE_BREAK_BEFORE,
                RepairActionType.SET_KEEP_NEXT,
                RepairActionType.SET_KEEP_LINES,
                RepairActionType.SET_SPACING,
            }
            if action_type in word_layout_actions and spec.document_format != "docx":
                raise ProviderProtocolError(
                    f"repair plan action {index} is only valid for DOCX"
                )
            block_id = raw_action["block_id"]
            if block_id is not None and block_id not in known_blocks:
                raise ProviderProtocolError(
                    f"repair plan action {index} references unknown block {block_id!r}"
                )
            slide = raw_action["slide"]
            shape_id = raw_action["shape_id"]
            if action_type in powerpoint_actions:
                if (
                    isinstance(slide, bool)
                    or not isinstance(slide, int)
                    or slide < 1
                    or not isinstance(shape_id, str)
                    or not shape_id
                ):
                    raise ProviderProtocolError(
                        f"repair plan action {index} must reference slide and shape_id"
                    )
                if known_shape_refs and (slide, shape_id) not in known_shape_refs:
                    raise ProviderProtocolError(
                        f"repair plan action {index} references an unknown shape"
                    )
            elif slide is not None or shape_id is not None:
                raise ProviderProtocolError(
                    f"repair plan action {index} has unexpected shape coordinates"
                )
            issue_ids = raw_action["issue_ids"]
            if not isinstance(issue_ids, list) or any(
                not isinstance(issue_id, str) for issue_id in issue_ids
            ):
                raise ProviderProtocolError(
                    f"repair plan action {index} issue_ids are invalid"
                )
            unknown_issues = set(issue_ids) - known_issue_ids
            if unknown_issues:
                raise ProviderProtocolError(
                    f"repair plan action {index} references unknown issues "
                    f"{sorted(unknown_issues)}"
                )
            proposed_text = raw_action["proposed_text"]
            if proposed_text is not None and not isinstance(proposed_text, str):
                raise ProviderProtocolError(
                    f"repair plan action {index} proposed_text is invalid"
                )
            if action_type in self._DIRECT_TEXT_REPAIR_ACTIONS:
                protected = protected_by_block.get(block_id)
                if protected is None or proposed_text is None:
                    raise ProviderProtocolError(
                        f"repair plan action {index} cannot replace missing text"
                    )
                placeholder_errors = validate_protected_tokens(
                    proposed_text, protected.tokens
                )
                if placeholder_errors:
                    raise ProviderProtocolError(
                        f"repair plan action {index} protected token mismatch: "
                        + "; ".join(placeholder_errors)
                    )
                try:
                    proposed_text = restore_text(proposed_text, protected.tokens)
                except ProtectionValidationError as exc:
                    raise ProviderProtocolError(str(exc)) from exc
            elif action_type == RepairActionType.FIT_TEXT and proposed_text is not None:
                protected = protected_by_block.get(block_id)
                if protected is None:
                    raise ProviderProtocolError(
                        f"repair plan action {index} cannot fit missing text"
                    )
                placeholder_errors = validate_protected_tokens(
                    proposed_text, protected.tokens
                )
                if placeholder_errors:
                    raise ProviderProtocolError(
                        f"repair plan action {index} protected token mismatch: "
                        + "; ".join(placeholder_errors)
                    )
                try:
                    proposed_text = restore_text(proposed_text, protected.tokens)
                except ProtectionValidationError as exc:
                    raise ProviderProtocolError(str(exc)) from exc
            elif proposed_text is not None:
                raise ProviderProtocolError(
                    f"repair plan action {index} returned unexpected proposed_text"
                )
            try:
                actions.append(
                    RepairAction(
                        action_id=f"{request_id}-action-{index}",
                        action_type=action_type,
                        block_id=block_id,
                        issue_ids=issue_ids,
                        instruction=raw_action["instruction"],
                        rationale=raw_action["rationale"],
                        max_length=raw_action["max_length"],
                        proposed_text=proposed_text,
                        slide=slide,
                        shape_id=shape_id,
                        geometry=raw_action["geometry"],
                        textbox_margins=raw_action["textbox_margins"],
                        autofit_mode=raw_action["autofit_mode"],
                        font_size_pt=raw_action["font_size_pt"],
                        min_font_size_pt=raw_action["min_font_size_pt"],
                        scale=raw_action["scale"],
                        clamp_to_slide=raw_action["clamp_to_slide"],
                        confidence=raw_action["confidence"],
                        provider=self.name,
                        model=model or self._model,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ProviderProtocolError(
                    f"repair plan action {index} is invalid"
                ) from exc

        token_usage, http_attempts = self._audit_usage(usage)
        audit = self._audit_event(
            request_id=request_id,
            event_type="repair_plan_completed",
            state=AgentState.PLANNING,
            model=model,
            details={
                "action_count": len(actions),
                "usage": token_usage,
                "http_attempts": http_attempts,
            },
        )
        try:
            return RepairPlan(
                plan_id=f"{request_id}-plan",
                state=(
                    AgentState.NEEDS_REVIEW
                    if output["needs_human_review"] and not actions
                    else AgentState.REPAIRING
                ),
                actions=actions,
                rationale=output["rationale"],
                needs_human_review=output["needs_human_review"],
                provider=self.name,
                model=model or self._model,
                latency_ms=elapsed_ms,
                audit_events=[audit],
            )
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError("repair plan output is invalid") from exc

    @classmethod
    def _known_shape_refs(cls, spec: QaRepairSpec) -> set[tuple[int, str]]:
        refs: set[tuple[int, str]] = set()
        for block in spec.blocks:
            location = cls._office_location(block.metadata, "pptx")
            cls._add_shape_ref(refs, location)
        for issue in spec.issues:
            location = cls._office_location(issue.details, "pptx")
            if issue.slide is not None:
                location["slide"] = issue.slide
            cls._add_shape_ref(refs, location)
        return refs

    @staticmethod
    def _add_shape_ref(
        refs: set[tuple[int, str]],
        location: Mapping[str, Any],
    ) -> None:
        slide = location.get("slide")
        if slide is None:
            slide = location.get("slide_index")
        shape_id = location.get("shape_id")
        if (
            isinstance(slide, int)
            and not isinstance(slide, bool)
            and slide >= 1
            and shape_id is not None
        ):
            refs.add((slide, str(shape_id)))

    def _audit_event(
        self,
        *,
        request_id: str,
        event_type: str,
        state: AgentState,
        model: str | None,
        details: dict[str, Any],
    ) -> AuditEvent:
        return AuditEvent(
            event_id=f"{request_id}-{event_type}",
            event_type=event_type,
            state=state,
            request_id=request_id,
            provider=self.name,
            model=model or self._model,
            details=details,
        )


# More explicit product-facing name while preserving a concise implementation
# name for callers that only need ProcessingProvider behavior.
OpenAIQaRepairProvider = OpenAIProcessingProvider
