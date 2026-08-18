"""Strict JSON contracts for the controlled document-agent tool bridge.

The Pi sidecar is deliberately not given arbitrary filesystem access.  Every
request carries a jobs parent plus a safe job identifier; document references
inside a job are relative paths or immutable candidate identifiers.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from .languages import (
    normalize_source_language,
    normalize_target_language,
)


SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SAFE_CANDIDATE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
LANGUAGE_PATTERN = re.compile(
    r"^(?:auto|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)$",
    re.IGNORECASE,
)


class AgentContract(BaseModel):
    """Base model used for every agent-facing JSON object."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        allow_inf_nan=False,
        str_strip_whitespace=True,
    )


def _validate_relative_path(value: str) -> str:
    """Return a normalized portable relative path without resolving the disk."""

    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or raw.startswith("/"):
        raise ValueError("path must be a non-empty relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path cannot contain empty, current, or parent components")
    # PurePosixPath does not consider a Windows drive absolute on non-Windows
    # hosts, so reject drive-like first components explicitly.
    if ":" in path.parts[0]:
        raise ValueError("path cannot contain a drive or URI scheme")
    return path.as_posix()


def _validate_language(value: str) -> str:
    if not LANGUAGE_PATTERN.fullmatch(value):
        raise ValueError("language must be 'auto' or a BCP-47-like language tag")
    return value


def _validate_source_language(value: str) -> str:
    return normalize_source_language(_validate_language(value), allow_auto=True)


def _validate_optional_source_language(value: str | None) -> str | None:
    return _validate_source_language(value) if value is not None else None


def _validate_target_language(value: str) -> str:
    return normalize_target_language(_validate_language(value), allow_auto=False)


def _validate_auto_target_language(value: str) -> str:
    return normalize_target_language(_validate_language(value), allow_auto=True)


def _validate_optional_target_language(value: str | None) -> str | None:
    return _validate_target_language(value) if value is not None else None


class ToolStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class ToolContextRequest(AgentContract):
    job_root: Path
    job_id: str = Field(pattern=SAFE_ID_PATTERN)

    @field_validator("job_id")
    @classmethod
    def reject_dot_job_ids(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("job_id cannot be a dot path")
        return value


class PreflightDocumentRequest(ToolContextRequest):
    input_relpath: str
    existing_translation_relpath: str | None = None
    candidate_id: str | None = Field(
        default=None,
        pattern=SAFE_CANDIDATE_ID_PATTERN,
    )
    source_language: str | None = Field(default=None, min_length=2, max_length=32)
    target_language: str | None = Field(default=None, min_length=2, max_length=32)

    _input_path = field_validator("input_relpath")(_validate_relative_path)
    _source_language = field_validator("source_language")(
        _validate_optional_source_language
    )
    _target_language = field_validator("target_language")(
        _validate_optional_target_language
    )

    @field_validator("existing_translation_relpath")
    @classmethod
    def validate_existing_translation_path(
        cls,
        value: str | None,
    ) -> str | None:
        return _validate_relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_pretranslated_pair(self) -> PreflightDocumentRequest:
        candidate_fields = (
            self.existing_translation_relpath,
            self.candidate_id,
            self.source_language,
            self.target_language,
        )
        if any(value is not None for value in candidate_fields) and not all(
            value is not None for value in candidate_fields
        ):
            raise ValueError(
                "existing_translation_relpath, candidate_id, source_language, and "
                "target_language must appear together"
            )
        if (
            self.source_language is not None
            and self.target_language is not None
            and self.source_language.casefold() != "auto"
            and self.source_language.casefold() == self.target_language.casefold()
        ):
            raise ValueError("source_language and target_language must differ")
        return self


class FixtureProviderConfig(AgentContract):
    kind: Literal["fixture"] = "fixture"
    fixture_relpath: str
    missing: Literal["error", "passthrough"] = "error"

    _fixture_path = field_validator("fixture_relpath")(_validate_relative_path)


class OpenAICompatibleProviderConfig(AgentContract):
    kind: Literal["openai_compatible"] = "openai_compatible"
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    temperature: float | None = Field(default=0.0, ge=0, le=2)


TranslationProviderConfig = Annotated[
    FixtureProviderConfig | OpenAICompatibleProviderConfig,
    Field(discriminator="kind"),
]


class TranslateOnceRequest(ToolContextRequest):
    input_relpath: str
    candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    source_language: str = Field(default="auto", min_length=2, max_length=32)
    target_language: str = Field(min_length=2, max_length=32)
    provider: TranslationProviderConfig
    glossary: dict[str, str] = Field(default_factory=dict, max_length=5_000)
    layout_mode: Literal["strict", "balanced"] = "strict"
    max_translation_concurrency: int = Field(default=4, ge=1, le=8)
    max_provider_retries: int = Field(default=3, ge=0, le=8)

    _input_path = field_validator("input_relpath")(_validate_relative_path)
    _source_language = field_validator("source_language")(_validate_source_language)
    _target_language = field_validator("target_language")(
        _validate_auto_target_language
    )

    @model_validator(mode="after")
    def validate_language_pair(self) -> TranslateOnceRequest:
        if (
            self.source_language.casefold() != "auto"
            and self.target_language.casefold() != "auto"
            and self.source_language.casefold() == self.target_language.casefold()
        ):
            raise ValueError("source_language and target_language must differ")
        return self


class RunDocumentQaRequest(ToolContextRequest):
    candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    rendered_layout_required: bool = False
    semantic_qa: bool = False
    max_processing_concurrency: int = Field(default=4, ge=1, le=8)
    provider_timeout_seconds: float = Field(default=60.0, gt=0, le=600)


class ApplyRepairRequest(ToolContextRequest):
    candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    output_candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    max_rounds: int = Field(default=2, ge=1, le=3)
    rendered_layout_required: bool = False
    use_processing_provider: bool = False
    provider_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    @model_validator(mode="after")
    def validate_distinct_candidates(self) -> ApplyRepairRequest:
        if self.candidate_id == self.output_candidate_id:
            raise ValueError("output_candidate_id must differ from candidate_id")
        return self


class VerifyCandidateRequest(ToolContextRequest):
    candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    rendered_layout_required: bool = False


class FinalizeResultRequest(ToolContextRequest):
    candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    result_basename: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=SAFE_ID_PATTERN,
    )

    @field_validator("result_basename")
    @classmethod
    def reject_dot_result_names(cls, value: str | None) -> str | None:
        if value in {".", ".."}:
            raise ValueError("result_basename cannot be a dot path")
        return value


class ArtifactSummary(AgentContract):
    artifact_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    relpath: str
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str | None = Field(default=None, max_length=160)

    _relative_path = field_validator("relpath")(_validate_relative_path)


class IssueSummary(AgentContract):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=500)
    severity: Literal["error", "warning", "info"] = "error"
    stage: str | None = Field(default=None, max_length=120)
    block_id: str | None = Field(default=None, max_length=240)
    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    recoverable: bool = True


class ToolError(AgentContract):
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False


class AgentToolResponse(AgentContract):
    tool: Literal[
        "preflight_document",
        "translate_once",
        "run_document_qa",
        "apply_repair",
        "verify_candidate",
        "finalize_result",
    ]
    job_id: str = Field(min_length=1, max_length=128)
    status: ToolStatus
    summary: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: list[ArtifactSummary] = Field(default_factory=list, max_length=200)
    issues: list[IssueSummary] = Field(default_factory=list, max_length=200)
    error: ToolError | None = None


class SourceRecord(AgentContract):
    input_relpath: str
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    source_format: Literal["doc", "docx", "ppt", "pptx"]
    normalized_format: Literal["docx", "pptx"] | None = None
    normalized_relpath: str | None = None
    normalized_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    _input_path = field_validator("input_relpath")(_validate_relative_path)

    @field_validator("normalized_relpath")
    @classmethod
    def validate_normalized_path(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def validate_normalized_fields(self) -> SourceRecord:
        populated = (
            self.normalized_format is not None,
            self.normalized_relpath is not None,
            self.normalized_sha256 is not None,
        )
        if any(populated) and not all(populated):
            raise ValueError("normalized source fields must be set together")
        return self


class QaRecord(AgentContract):
    status: ToolStatus
    candidate_sha256: str = Field(pattern=SHA256_PATTERN)
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    report_relpath: str
    issue_counts: dict[Literal["error", "warning", "info"], int]
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _report_path = field_validator("report_relpath")(_validate_relative_path)

    @field_validator("issue_counts")
    @classmethod
    def validate_counts(cls, value: dict[str, int]) -> dict[str, int]:
        expected = {"error", "warning", "info"}
        if set(value) != expected or any(count < 0 for count in value.values()):
            raise ValueError("issue_counts must contain non-negative severity counts")
        return value


class CandidateRecord(AgentContract):
    candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    relpath: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    document_format: Literal["docx", "pptx"]
    source_relpath: str
    source_sha256: str = Field(pattern=SHA256_PATTERN)
    source_language: str = Field(min_length=2, max_length=32)
    target_language: str = Field(min_length=2, max_length=32)
    requested_target_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=32,
    )
    resolved_target_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=32,
    )
    created_by: Literal[
        "translate_once",
        "apply_repair",
        "pretranslated_qa",
    ]
    parent_candidate_id: str | None = Field(
        default=None,
        pattern=SAFE_CANDIDATE_ID_PATTERN,
    )
    pipeline_manifest_relpath: str | None = None
    pipeline_status: str | None = Field(default=None, max_length=80)
    request_fingerprint: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    latest_qa: QaRecord | None = None
    verification: QaRecord | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _candidate_path = field_validator("relpath", "source_relpath")(
        _validate_relative_path
    )

    @field_validator("pipeline_manifest_relpath")
    @classmethod
    def validate_manifest_path(cls, value: str | None) -> str | None:
        return _validate_relative_path(value) if value is not None else None


class FinalResultRecord(AgentContract):
    candidate_id: str = Field(pattern=SAFE_CANDIDATE_ID_PATTERN)
    relpath: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    finalized_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _result_path = field_validator("relpath")(_validate_relative_path)


class AgentJobState(AgentContract):
    schema_version: Literal[1] = 1
    job_id: str = Field(pattern=SAFE_ID_PATTERN)
    source: SourceRecord | None = None
    candidates: dict[str, CandidateRecord] = Field(default_factory=dict)
    results: dict[str, FinalResultRecord] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_record_keys(self) -> AgentJobState:
        for key, record in self.candidates.items():
            if key != record.candidate_id:
                raise ValueError("candidate map keys must match candidate_id")
        for key, record in self.results.items():
            if key != PurePosixPath(record.relpath).name:
                raise ValueError("result map keys must match result filenames")
        return self


REQUEST_MODELS: dict[str, type[ToolContextRequest]] = {
    "preflight_document": PreflightDocumentRequest,
    "translate_once": TranslateOnceRequest,
    "run_document_qa": RunDocumentQaRequest,
    "apply_repair": ApplyRepairRequest,
    "verify_candidate": VerifyCandidateRequest,
    "finalize_result": FinalizeResultRequest,
}


__all__ = [
    "AgentContract",
    "AgentJobState",
    "AgentToolResponse",
    "ApplyRepairRequest",
    "ArtifactSummary",
    "CandidateRecord",
    "FinalizeResultRequest",
    "FixtureProviderConfig",
    "IssueSummary",
    "OpenAICompatibleProviderConfig",
    "PreflightDocumentRequest",
    "QaRecord",
    "REQUEST_MODELS",
    "RunDocumentQaRequest",
    "SourceRecord",
    "ToolError",
    "ToolStatus",
    "TranslateOnceRequest",
    "TranslationProviderConfig",
    "VerifyCandidateRequest",
]
