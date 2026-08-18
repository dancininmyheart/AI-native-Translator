"""Typed contracts shared by the document pipeline and translation providers.

The models in this module deliberately contain no provider- or filesystem-specific
defaults.  They are safe to serialize into a job manifest and to reconstruct after
a worker restart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .languages import normalize_source_language, normalize_target_language


class StrictModel(BaseModel):
    """Base class for wire and manifest models.

    Rejecting unknown fields makes provider protocol drift visible instead of
    silently discarding information.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        allow_inf_nan=False,
    )


class JobStatus(StrEnum):
    RECEIVED = "RECEIVED"
    CONVERTING = "CONVERTING"
    EXTRACTING = "EXTRACTING"
    TRANSLATING = "TRANSLATING"
    SEMANTIC_QA = "SEMANTIC_QA"
    PATCHING = "PATCHING"
    LAYOUT_QA = "LAYOUT_QA"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AgentState(StrEnum):
    PENDING = "PENDING"
    DIAGNOSING = "DIAGNOSING"
    PLANNING = "PLANNING"
    REPAIRING = "REPAIRING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RepairActionType(StrEnum):
    RETRANSLATE = "retranslate"
    FIT_TEXT = "fit_text"
    TERMINOLOGY_FIX = "terminology_fix"
    PLACEHOLDER_RESTORE = "placeholder_restore"
    INSERT_PAGE_BREAK_BEFORE = "insert_page_break_before"
    REMOVE_PAGE_BREAK_BEFORE = "remove_page_break_before"
    SET_KEEP_NEXT = "set_keep_next"
    SET_KEEP_LINES = "set_keep_lines"
    SET_SPACING = "set_spacing"
    REPLACE_TRANSLATION = "replace_translation"
    ENABLE_AUTOFIT = "enable_autofit"
    SET_TEXTBOX_MARGINS = "set_textbox_margins"
    ADJUST_FONT_SIZE = "adjust_font_size"
    RESOLVE_TEXT_OVERFLOW = "resolve_text_overflow"
    RESTORE_SHAPE_COORDINATES = "restore_shape_coordinates"
    SET_SHAPE_BOUNDS = "set_shape_bounds"
    SET_FONT_SIZE = "set_font_size"
    FLAG_FOR_REVIEW = "flag_for_review"


class RepairActionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    APPLIED = "APPLIED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class ArtifactType(StrEnum):
    TRANSLATED_DOCUMENT = "translated_document"
    QA_REPORT = "qa_report"
    TIMING_REPORT = "timing_report"
    MANIFEST = "manifest"
    DIAGNOSTIC = "diagnostic"


class ProtectedToken(StrictModel):
    """A source substring replaced with a stable placeholder before translation."""

    placeholder: str
    value: str
    kind: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> ProtectedToken:
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class ProtectedText(StrictModel):
    """Protected text plus the information necessary for lossless restoration."""

    original_text: str
    protected_text: str
    tokens: list[ProtectedToken] = Field(default_factory=list)


class TextSegment(StrictModel):
    """Text associated with one original formatting run."""

    segment_id: str
    text: str
    style_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TranslationBlock(StrictModel):
    """Smallest independently translatable unit in a document."""

    block_id: str = Field(min_length=1)
    source_text: str
    protocol: Literal["plain", "tagged"] = "plain"
    source_language: str | None = None
    target_language: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    segments: list[TextSegment] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    protected_tokens: list[ProtectedToken] = Field(default_factory=list)
    max_length_hint: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderUsage(StrictModel):
    input_characters: int = Field(default=0, ge=0)
    output_characters: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    currency: str | None = None


class TranslationResult(StrictModel):
    """Normalized output returned by every translation provider."""

    block_id: str = Field(min_length=1)
    translated_text: str
    segments: list[TextSegment] = Field(default_factory=list)
    provider: str
    model: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QaIssue(StrictModel):
    issue_id: str | None = None
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: IssueSeverity = IssueSeverity.ERROR
    stage: str | None = None
    block_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    recoverable: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class StageTiming(StrictModel):
    stage: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    attempts: int = Field(default=1, ge=1)
    provider: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_elapsed_seconds(self) -> StageTiming:
        if self.finished_at is not None:
            measured = (self.finished_at - self.started_at).total_seconds()
            if measured < 0:
                raise ValueError("finished_at cannot precede started_at")
            if self.elapsed_seconds is None:
                self.elapsed_seconds = measured
        return self


class Artifact(StrictModel):
    artifact_type: ArtifactType
    path: Path
    filename: str | None = None
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is not None:
            normalized = value.lower()
            if len(normalized) != 64 or any(
                c not in "0123456789abcdef" for c in normalized
            ):
                raise ValueError(
                    "sha256 must contain exactly 64 hexadecimal characters"
                )
            return normalized
        return value


class JobSpec(StrictModel):
    """A single-document, single-target translation job."""

    job_id: str = Field(min_length=1)
    input_path: Path
    output_dir: Path
    source_language: str = "auto"
    target_language: str = Field(min_length=2)
    pages: list[int] | None = None
    layout_mode: Literal["strict", "balanced"] = "strict"
    glossary: dict[str, str] = Field(default_factory=dict)
    semantic_qa: bool = True
    layout_qa: bool = True
    max_translation_concurrency: int = Field(default=4, ge=1, le=8)
    max_provider_retries: int = Field(default=3, ge=0)
    max_translation_retries: int = Field(default=2, ge=0)
    max_text_fit_rounds: int = Field(default=2, ge=0)
    auto_qa_repair: bool = True
    max_qa_repair_rounds: int = Field(default=2, ge=0, le=5)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_language")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return normalize_source_language(value, allow_auto=True)

    @field_validator("target_language")
    @classmethod
    def normalize_target(cls, value: str) -> str:
        return normalize_target_language(value, allow_auto=True)

    @field_validator("pages")
    @classmethod
    def validate_pages(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if any(page < 1 for page in value):
            raise ValueError("page numbers are one-based and must be positive")
        if len(set(value)) != len(value):
            raise ValueError("page numbers must not contain duplicates")
        return sorted(value)

    @model_validator(mode="after")
    def validate_languages(self) -> JobSpec:
        if (
            self.source_language != "auto"
            and self.target_language != "auto"
            and self.source_language.casefold() == self.target_language.casefold()
        ):
            raise ValueError("source_language and target_language must differ")
        return self


class SampleSpec(StrictModel):
    job_id: str = Field(min_length=1)
    input_path: Path
    output_dir: Path
    target_languages: list[str] = Field(min_length=1)
    page: int = Field(default=1, ge=1)
    source_language: str = "auto"
    layout_mode: Literal["strict", "balanced"] = "strict"
    glossary: dict[str, str] = Field(default_factory=dict)
    max_translation_concurrency: int = Field(default=4, ge=1, le=8)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_languages")
    @classmethod
    def validate_targets(cls, value: list[str]) -> list[str]:
        normalized = [language.strip() for language in value]
        if any(len(language) < 2 for language in normalized):
            raise ValueError(
                "target language identifiers must be at least two characters"
            )
        if len({language.casefold() for language in normalized}) != len(normalized):
            raise ValueError("target_languages must not contain duplicates")
        return normalized


class DocumentManifest(StrictModel):
    input_path: Path
    normalized_path: Path
    source_format: str
    normalized_format: str
    page_count: int | None = Field(default=None, ge=0)
    block_count: int = Field(default=0, ge=0)
    media_hashes: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactManifest(StrictModel):
    job_id: str
    status: JobStatus
    source_path: Path
    target_language: str | None = None
    requested_target_language: str | None = None
    resolved_target_language: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    issues: list[QaIssue] = Field(default_factory=list)
    timings: list[StageTiming] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class CleanupReport(StrictModel):
    removed_jobs: list[str] = Field(default_factory=list)
    removed_bytes: int = Field(default=0, ge=0)
    failures: dict[str, str] = Field(default_factory=dict)


class SemanticQaResult(StrictModel):
    block_id: str
    passed: bool
    issues: list[QaIssue] = Field(default_factory=list)
    provider: str
    model: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TextFitResult(StrictModel):
    block_id: str
    fitted_text: str
    provider: str
    model: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(StrictModel):
    """Serializable, secret-free evidence for one agent state transition."""

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    state: AgentState
    actor: str = "qa-repair-agent"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_id: str | None = None
    round_number: int | None = Field(default=None, ge=1)
    action_id: str | None = None
    provider: str | None = None
    model: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ShapeGeometry(StrictModel):
    """PowerPoint shape bounds in an explicit coordinate space."""

    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    unit: Literal["points", "emu"] = "points"


class TextboxMargins(StrictModel):
    """PowerPoint text-box internal margins, expressed in points."""

    left: float = Field(ge=0)
    right: float = Field(ge=0)
    top: float = Field(ge=0)
    bottom: float = Field(ge=0)


class RepairAction(StrictModel):
    action_id: str = Field(min_length=1)
    action_type: RepairActionType
    status: RepairActionStatus = RepairActionStatus.PENDING
    block_id: str | None = None
    issue_ids: list[str] = Field(default_factory=list)
    instruction: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    max_length: int | None = Field(default=None, ge=1)
    proposed_text: str | None = None
    slide: int | None = Field(default=None, ge=1)
    shape_id: str | None = None
    geometry: ShapeGeometry | None = None
    textbox_margins: TextboxMargins | None = None
    autofit_mode: Literal["shrink_text", "resize_shape"] | None = None
    font_size_pt: float | None = Field(default=None, gt=0, le=400)
    min_font_size_pt: float | None = Field(default=None, gt=0, le=400)
    scale: float | None = Field(default=None, ge=0.1, le=4)
    clamp_to_slide: bool = True
    confidence: float = Field(default=1.0, ge=0, le=1)
    attempts: int = Field(default=0, ge=0)
    provider: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    audit_events: list[AuditEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action_target(self) -> RepairAction:
        block_actions = {
            RepairActionType.RETRANSLATE,
            RepairActionType.FIT_TEXT,
            RepairActionType.TERMINOLOGY_FIX,
            RepairActionType.PLACEHOLDER_RESTORE,
            RepairActionType.INSERT_PAGE_BREAK_BEFORE,
            RepairActionType.REMOVE_PAGE_BREAK_BEFORE,
            RepairActionType.SET_KEEP_NEXT,
            RepairActionType.SET_KEEP_LINES,
            RepairActionType.SET_SPACING,
            RepairActionType.REPLACE_TRANSLATION,
        }
        powerpoint_actions = {
            RepairActionType.ENABLE_AUTOFIT,
            RepairActionType.SET_TEXTBOX_MARGINS,
            RepairActionType.ADJUST_FONT_SIZE,
            RepairActionType.RESOLVE_TEXT_OVERFLOW,
            RepairActionType.RESTORE_SHAPE_COORDINATES,
            RepairActionType.SET_SHAPE_BOUNDS,
            RepairActionType.SET_FONT_SIZE,
        }
        if self.action_type in block_actions and not self.block_id:
            raise ValueError(f"{self.action_type.value} requires block_id")
        if self.action_type in powerpoint_actions:
            if self.slide is None or not self.shape_id:
                raise ValueError(
                    f"{self.action_type.value} requires slide and shape_id"
                )
        elif any(
            value is not None
            for value in (
                self.slide,
                self.shape_id,
                self.geometry,
                self.textbox_margins,
                self.autofit_mode,
                self.font_size_pt,
                self.min_font_size_pt,
                self.scale,
            )
        ):
            raise ValueError(
                f"{self.action_type.value} cannot carry PowerPoint-only fields"
            )
        if self.action_type in {
            RepairActionType.RESTORE_SHAPE_COORDINATES,
            RepairActionType.SET_SHAPE_BOUNDS,
        } and self.geometry is None:
            raise ValueError(f"{self.action_type.value} requires geometry")
        if (
            self.geometry is not None
            and self.action_type
            not in {
                RepairActionType.RESOLVE_TEXT_OVERFLOW,
                RepairActionType.RESTORE_SHAPE_COORDINATES,
                RepairActionType.SET_SHAPE_BOUNDS,
            }
        ):
            raise ValueError(
                f"{self.action_type.value} cannot carry geometry"
            )
        if (
            self.action_type == RepairActionType.SET_TEXTBOX_MARGINS
            and self.textbox_margins is None
        ):
            raise ValueError("set_textbox_margins requires textbox_margins")
        if (
            self.textbox_margins is not None
            and self.action_type
            not in {
                RepairActionType.RESOLVE_TEXT_OVERFLOW,
                RepairActionType.SET_TEXTBOX_MARGINS,
            }
        ):
            raise ValueError(
                f"{self.action_type.value} cannot carry textbox_margins"
            )
        if (
            self.action_type == RepairActionType.SET_FONT_SIZE
            and self.font_size_pt is None
        ):
            raise ValueError("set_font_size requires font_size_pt")
        if (
            self.font_size_pt is not None
            and self.action_type
            not in {
                RepairActionType.RESOLVE_TEXT_OVERFLOW,
                RepairActionType.ADJUST_FONT_SIZE,
                RepairActionType.SET_FONT_SIZE,
            }
        ):
            raise ValueError(
                f"{self.action_type.value} cannot carry font_size_pt"
            )
        if (
            self.autofit_mode is not None
            and self.action_type
            not in {
                RepairActionType.RESOLVE_TEXT_OVERFLOW,
                RepairActionType.ENABLE_AUTOFIT,
            }
        ):
            raise ValueError(
                f"{self.action_type.value} cannot carry autofit_mode"
            )
        if (
            self.font_size_pt is not None
            and self.min_font_size_pt is not None
            and self.font_size_pt < self.min_font_size_pt
        ):
            raise ValueError("font_size_pt cannot be below min_font_size_pt")
        if (
            self.min_font_size_pt is not None
            and self.action_type
            not in {
                RepairActionType.RESOLVE_TEXT_OVERFLOW,
                RepairActionType.ADJUST_FONT_SIZE,
                RepairActionType.SET_FONT_SIZE,
            }
        ):
            raise ValueError(
                f"{self.action_type.value} cannot carry min_font_size_pt"
            )
        if (
            self.scale is not None
            and self.action_type
            not in {
                RepairActionType.RESOLVE_TEXT_OVERFLOW,
                RepairActionType.ADJUST_FONT_SIZE,
                RepairActionType.SET_SHAPE_BOUNDS,
            }
        ):
            raise ValueError(
                f"{self.action_type.value} cannot carry scale"
            )
        if self.action_type == RepairActionType.FIT_TEXT and self.max_length is None:
            raise ValueError("fit_text requires max_length")
        if (
            self.action_type == RepairActionType.REPLACE_TRANSLATION
            and self.proposed_text is None
        ):
            raise ValueError("replace_translation requires proposed_text")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at cannot precede started_at")
        return self


class RepairRound(StrictModel):
    round_number: int = Field(ge=1)
    state: AgentState = AgentState.PENDING
    actions: list[RepairAction] = Field(default_factory=list)
    issues_before: list[QaIssue] = Field(default_factory=list)
    issues_after: list[QaIssue] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_round_elapsed(self) -> RepairRound:
        if self.finished_at is not None:
            measured = (self.finished_at - self.started_at).total_seconds()
            if measured < 0:
                raise ValueError("finished_at cannot precede started_at")
            if self.elapsed_seconds is None:
                self.elapsed_seconds = measured
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("actions must have unique action_id values")
        return self


class QaRepairSpec(StrictModel):
    """Complete, restart-safe input for a bounded QA/repair agent run."""

    job_id: str = Field(min_length=1)
    source_document: Path
    candidate_document: Path
    document_format: Literal["docx", "pptx"]
    work_dir: Path
    source_language: str
    target_language: str = Field(min_length=2)
    blocks: list[TranslationBlock]
    translations: list[TranslationResult]
    issues: list[QaIssue]
    max_rounds: int = Field(default=2, ge=1, le=5)
    allowed_actions: list[RepairActionType] = Field(
        default_factory=lambda: list(RepairActionType)
    )
    strict_layout: bool = True
    strict_page_count: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_repair_inputs(self) -> QaRepairSpec:
        expected_suffix = f".{self.document_format}"
        if self.source_document.suffix.casefold() != expected_suffix:
            raise ValueError(
                "source_document extension must match document_format"
            )
        if self.candidate_document.suffix.casefold() != expected_suffix:
            raise ValueError(
                "candidate_document extension must match document_format"
            )
        block_ids = [block.block_id for block in self.blocks]
        translation_ids = [translation.block_id for translation in self.translations]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("blocks must have unique block_id values")
        if len(translation_ids) != len(set(translation_ids)):
            raise ValueError("translations must have unique block_id values")
        if set(translation_ids) - set(block_ids):
            raise ValueError("translations contain unknown block_id values")
        issue_ids = [
            issue.issue_id for issue in self.issues if issue.issue_id is not None
        ]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("issues must have unique issue_id values")
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("allowed_actions must not contain duplicates")
        if (
            self.source_language != "auto"
            and self.source_language.casefold() == self.target_language.casefold()
        ):
            raise ValueError("source_language and target_language must differ")
        return self


class QaRootCause(StrictModel):
    code: str = Field(min_length=1)
    description: str = Field(min_length=1)
    issue_ids: list[str] = Field(default_factory=list)
    repairable: bool
    recommended_action: RepairActionType
    confidence: float = Field(ge=0, le=1)


class QaDiagnosis(StrictModel):
    request_id: str = Field(min_length=1)
    state: AgentState = AgentState.PLANNING
    summary: str = Field(min_length=1)
    root_causes: list[QaRootCause] = Field(default_factory=list)
    issue_ids: list[str] = Field(default_factory=list)
    repairable_issue_ids: list[str] = Field(default_factory=list)
    needs_human_review: bool
    confidence: float = Field(ge=0, le=1)
    provider: str
    model: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepairPlan(StrictModel):
    plan_id: str = Field(min_length=1)
    state: AgentState = AgentState.REPAIRING
    actions: list[RepairAction] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    needs_human_review: bool
    provider: str
    model: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_plan_actions(self) -> RepairPlan:
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("actions must have unique action_id values")
        return self


class QaRepairReport(StrictModel):
    job_id: str = Field(min_length=1)
    state: AgentState
    rounds: list[RepairRound] = Field(default_factory=list)
    initial_issues: list[QaIssue] = Field(default_factory=list)
    final_issues: list[QaIssue] = Field(default_factory=list)
    candidate_document: Path | None = None
    document_format: Literal["docx", "pptx"]
    accepted: bool = False
    rollback: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    provider: str | None = None
    model: str | None = None
    audit_events: list[AuditEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_report(self) -> QaRepairReport:
        if (
            self.candidate_document is not None
            and self.candidate_document.suffix.casefold()
            != f".{self.document_format}"
        ):
            raise ValueError(
                "candidate_document extension must match document_format"
            )
        if self.accepted and self.rollback:
            raise ValueError("a repair report cannot be accepted and rolled back")
        round_numbers = [round_.round_number for round_ in self.rounds]
        if len(round_numbers) != len(set(round_numbers)):
            raise ValueError("rounds must have unique round_number values")
        if self.finished_at is not None:
            measured = (self.finished_at - self.started_at).total_seconds()
            if measured < 0:
                raise ValueError("finished_at cannot precede started_at")
            if self.elapsed_seconds is None:
                self.elapsed_seconds = measured
        return self
