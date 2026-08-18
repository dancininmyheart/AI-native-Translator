"""Controlled, immutable tools exposed to the Pi document agent.

This module is the security boundary between an agent's decisions and Office
file mutation.  It intentionally exposes no arbitrary read/write primitive:
all paths are resolved below ``<job_root>/<job_id>`` and candidates are
registered by immutable identifiers and SHA-256 digests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar, Iterator, Mapping, Sequence, TypeVar
from uuid import uuid4

from pydantic import ValidationError

from .agent_contracts import (
    AgentJobState,
    AgentToolResponse,
    ApplyRepairRequest,
    ArtifactSummary,
    CandidateRecord,
    FinalizeResultRequest,
    FinalResultRecord,
    FixtureProviderConfig,
    IssueSummary,
    OpenAICompatibleProviderConfig,
    PreflightDocumentRequest,
    QaRecord,
    REQUEST_MODELS,
    RunDocumentQaRequest,
    SourceRecord,
    ToolError,
    ToolStatus,
    TranslateOnceRequest,
    VerifyCandidateRequest,
)
from .docx_engine import DocxEngine
from .legacy import convert_doc_to_docx, convert_ppt_to_pptx
from .models import (
    ArtifactManifest,
    ArtifactType,
    JobSpec,
    QaIssue,
    RepairActionType,
    TranslationBlock,
    TranslationResult,
)
from .pipeline import _semantic_qa, inspect_document, translate_document
from .pptx_engine import PptxEngine
from .pptx_rendered_layout_qa import run_pptx_qa_with_rendered_layout
from .processing_provider import OpenAIProcessingProvider
from .providers import FixtureProvider, OpenAIChatCompletionsProvider
from .qa_agent import QaAgentState, run_qa_repair_agent_async
from .utils import atomic_write_json, sha256_file
from .word_qa import run_docx_qa


STATE_FILENAME = "agent-tool-state.json"
SUPPORTED_INPUT_SUFFIXES = {".doc", ".docx", ".ppt", ".pptx"}
SUPPORTED_CANDIDATE_SUFFIXES = {".docx", ".pptx"}
MEDIA_TYPES = {
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".json": "application/json",
}


class AgentToolFailure(RuntimeError):
    """Base exception carrying a stable machine-readable failure code."""

    code = "AGENT_TOOL_FAILURE"


class AgentPathError(AgentToolFailure):
    code = "UNSAFE_PATH"


class AgentStateError(AgentToolFailure):
    code = "INVALID_JOB_STATE"


class AgentConfigurationError(AgentToolFailure):
    code = "PROVIDER_CONFIGURATION_ERROR"


@dataclass(frozen=True, slots=True)
class _ToolContext:
    jobs_root: Path
    workspace: Path
    state_path: Path
    job_id: str


@dataclass(frozen=True, slots=True, repr=False)
class _ProviderSettings:
    base_url: str
    model: str
    api_key: str


@dataclass(frozen=True)
class _RepairProviderSpec:
    """Model-like adapter consumed by both QA orchestration and LLM processing."""

    job_id: str
    source_document: Path
    candidate_document: Path
    document_format: str
    work_dir: Path
    source_language: str
    target_language: str
    blocks: list[TranslationBlock]
    translations: list[TranslationResult]
    issues: list[QaIssue]
    max_rounds: int
    allowed_actions: list[RepairActionType]
    strict_layout: bool
    strict_page_count: bool
    rendered_layout_required: bool
    report_path: Path
    metadata: dict[str, Any]

    model_fields: ClassVar[dict[str, object]] = {
        name: object()
        for name in (
            "job_id",
            "source_document",
            "candidate_document",
            "document_format",
            "work_dir",
            "source_language",
            "target_language",
            "blocks",
            "translations",
            "issues",
            "max_rounds",
            "allowed_actions",
            "strict_layout",
            "strict_page_count",
            "rendered_layout_required",
            "report_path",
            "metadata",
        )
    }

    def model_copy(self, *, update: Mapping[str, Any]) -> _RepairProviderSpec:
        return dataclasses.replace(self, **dict(update))


RequestT = TypeVar(
    "RequestT",
    PreflightDocumentRequest,
    TranslateOnceRequest,
    RunDocumentQaRequest,
    ApplyRepairRequest,
    VerifyCandidateRequest,
    FinalizeResultRequest,
)


def _serialized_tool(model: type[RequestT]):
    """Serialize every stateful tool call for one job across threads/processes."""

    def decorate(function):
        @wraps(function)
        def guarded(request):
            resolved = _request(model, request)
            context = _context(resolved.job_root, resolved.job_id)
            with _exclusive_job_lock(context):
                return function(resolved)

        return guarded

    return decorate


@_serialized_tool(PreflightDocumentRequest)
def preflight_document(
    request: PreflightDocumentRequest | Mapping[str, Any],
) -> AgentToolResponse:
    resolved = _request(PreflightDocumentRequest, request)
    context = _context(resolved.job_root, resolved.job_id)
    source = _relative_file(context, resolved.input_relpath)
    if source.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
        raise AgentPathError("input must be DOC, DOCX, PPT, or PPTX")

    digest = sha256_file(source)
    state = _load_state(context, required=False)
    if state.source is not None:
        previous = state.source
        if (
            previous.input_relpath != _relpath(context, source)
            or previous.input_sha256 != digest
        ):
            raise AgentStateError(
                "job source is already registered with a different path or digest"
            )

    normalized = _persist_preflight_normalized_source(
        context,
        state,
        source,
        digest,
    )
    inspection = inspect_document(normalized)
    candidate: Path | None = None
    if resolved.existing_translation_relpath is not None:
        assert resolved.candidate_id is not None
        assert resolved.source_language is not None
        assert resolved.target_language is not None
        candidate = _register_pretranslated_candidate(
            context,
            state,
            existing_translation_relpath=resolved.existing_translation_relpath,
            candidate_id=resolved.candidate_id,
            source_language=resolved.source_language,
            target_language=resolved.target_language,
        )
    if sha256_file(source) != digest:
        raise AgentStateError("preflight modified the registered source")
    _save_state(context, state)
    artifacts = [
        _artifact(context, source, artifact_id="input", kind="input_document")
    ]
    if normalized != source:
        artifacts.append(
            _artifact(
                context,
                normalized,
                artifact_id="normalized_source",
                kind="normalized_source_document",
            )
        )
    if candidate is not None:
        assert resolved.candidate_id is not None
        artifacts.append(
            _artifact(
                context,
                candidate,
                artifact_id=f"candidate:{resolved.candidate_id}",
                kind="candidate_document",
            )
        )
    return AgentToolResponse(
        tool="preflight_document",
        job_id=context.job_id,
        status=ToolStatus.COMPLETED,
        summary={
            "source_format": source.suffix.lower().lstrip("."),
            "normalized_format": inspection.normalized_format,
            "normalized_relpath": _relpath(context, normalized),
            "page_or_slide_count": inspection.page_count,
            "block_count": inspection.block_count,
            "size_bytes": source.stat().st_size,
            "pretranslated_candidate_id": resolved.candidate_id,
        },
        artifacts=artifacts,
    )


@_serialized_tool(TranslateOnceRequest)
def translate_once(
    request: TranslateOnceRequest | Mapping[str, Any],
) -> AgentToolResponse:
    resolved = _request(TranslateOnceRequest, request)
    context = _context(resolved.job_root, resolved.job_id)
    state = _load_state(context)
    input_source = _registered_input(context, state, resolved.input_relpath)
    source = _registered_normalized_source(context, state)
    request_fingerprint = _translation_request_fingerprint(
        context,
        state,
        resolved,
    )
    existing = state.candidates.get(resolved.candidate_id)
    if existing is not None:
        return _resume_translation(
            context,
            state,
            resolved,
            request_fingerprint=request_fingerprint,
        )
    _assert_new_candidate(context, state, resolved.candidate_id)

    provider = _translation_provider(context, resolved.provider)
    internal_job_id = f"translate-{resolved.candidate_id}"
    output_dir = _contained_output(
        context, context.workspace / "work" / "translation"
    )
    run_root = _contained_output(context, output_dir / internal_job_id)
    if run_root.exists():
        raise AgentStateError(
            "translation run already exists for this immutable candidate identifier"
        )

    input_digest_before = sha256_file(input_source)
    source_digest_before = sha256_file(source)
    try:
        manifest = translate_document(
            JobSpec(
                job_id=internal_job_id,
                input_path=source,
                output_dir=output_dir,
                source_language=resolved.source_language,
                target_language=resolved.target_language,
                layout_mode=resolved.layout_mode,
                glossary=resolved.glossary,
                semantic_qa=False,
                layout_qa=True,
                max_translation_concurrency=resolved.max_translation_concurrency,
                max_provider_retries=resolved.max_provider_retries,
                max_text_fit_rounds=0,
                auto_qa_repair=False,
                max_qa_repair_rounds=0,
                metadata={"agent_controlled_repair": True},
            ),
            provider,
            processing_provider=None,
        )
    finally:
        _close_translation_provider(provider)
    if (
        sha256_file(input_source) != input_digest_before
        or sha256_file(source) != source_digest_before
    ):
        raise AgentStateError(
            "translation pipeline modified the registered or normalized source"
        )

    response_artifacts = _pipeline_report_artifacts(context, manifest)
    pipeline_issues = list(manifest.issues)
    issue_counts = _issue_counts(pipeline_issues)
    office_artifact = _select_office_artifact(manifest.artifacts)
    status = _tool_status(getattr(manifest.status, "value", manifest.status))
    if office_artifact is None:
        return AgentToolResponse(
            tool="translate_once",
            job_id=context.job_id,
            status=status,
            summary={
                "pipeline_status": str(
                    getattr(manifest.status, "value", manifest.status)
                ),
                "candidate_created": False,
                "issue_counts": issue_counts,
                "auto_qa_repair": False,
            },
            artifacts=response_artifacts,
            issues=_issue_summaries(pipeline_issues),
            error=ToolError(
                code="NO_CANDIDATE_PRODUCED",
                message="translation pipeline did not produce an Office candidate",
                retryable=status != ToolStatus.COMPLETED,
            ),
        )

    produced = _contained_existing_file(context, Path(office_artifact.path))
    suffix = produced.suffix.lower()
    destination = _candidate_destination(context, resolved.candidate_id, suffix)
    _copy_exclusive(produced, destination)
    registered = False
    try:
        normalized_raw = manifest.metadata.get("normalized_path")
        if not isinstance(normalized_raw, str) or not normalized_raw:
            raise AgentStateError(
                "pipeline manifest omitted the normalized source path"
            )
        normalized = _contained_existing_file(context, Path(normalized_raw))
        normalized_digest = sha256_file(normalized)
        actual_source_language = str(
            manifest.metadata.get("source_language") or resolved.source_language
        )
        if actual_source_language == "auto":
            actual_source_language = "unknown"
        requested_target_language = str(
            manifest.requested_target_language or resolved.target_language
        )
        resolved_target_language = str(
            manifest.resolved_target_language
            or manifest.metadata.get("resolved_target_language")
            or manifest.target_language
            or ""
        )
        if not resolved_target_language or resolved_target_language == "auto":
            raise AgentStateError(
                "pipeline manifest omitted the resolved target language"
            )

        manifest_path = _contained_existing_file(context, run_root / "manifest.json")
        record = CandidateRecord(
            candidate_id=resolved.candidate_id,
            relpath=_relpath(context, destination),
            sha256=sha256_file(destination),
            size_bytes=destination.stat().st_size,
            document_format=suffix.lstrip("."),
            source_relpath=_relpath(context, normalized),
            source_sha256=normalized_digest,
            source_language=actual_source_language,
            target_language=resolved_target_language,
            requested_target_language=requested_target_language,
            resolved_target_language=resolved_target_language,
            created_by="translate_once",
            pipeline_manifest_relpath=_relpath(context, manifest_path),
            pipeline_status=str(getattr(manifest.status, "value", manifest.status)),
            request_fingerprint=request_fingerprint,
        )
        state.candidates[resolved.candidate_id] = record
        if state.source is not None and state.source.normalized_relpath is None:
            state.source = state.source.model_copy(
                update={
                    "normalized_format": suffix.lstrip("."),
                    "normalized_relpath": _relpath(context, normalized),
                    "normalized_sha256": normalized_digest,
                }
            )
        _save_state(context, state)
        registered = True
    except Exception:
        if not registered:
            state.candidates.pop(resolved.candidate_id, None)
            destination.unlink(missing_ok=True)
            if run_root.exists():
                shutil.rmtree(run_root)
        raise
    candidate_artifact = _artifact(
        context,
        destination,
        artifact_id=f"candidate:{resolved.candidate_id}",
        kind="candidate_document",
    )
    return AgentToolResponse(
        tool="translate_once",
        job_id=context.job_id,
        status=status,
        summary={
            "pipeline_status": record.pipeline_status,
            "candidate_id": resolved.candidate_id,
            "candidate_created": True,
            "document_format": record.document_format,
            "source_language": record.source_language,
            "target_language": record.target_language,
            "requested_target_language": record.requested_target_language,
            "resolved_target_language": record.resolved_target_language,
            "provider_kind": resolved.provider.kind,
            "issue_counts": issue_counts,
            "auto_qa_repair": False,
            "resumed": False,
            "provider_invoked": bool(
                manifest.metadata.get("provider_invoked", True)
            ),
        },
        artifacts=[candidate_artifact, *response_artifacts],
        issues=_issue_summaries(pipeline_issues),
    )


@_serialized_tool(RunDocumentQaRequest)
def run_document_qa(
    request: RunDocumentQaRequest | Mapping[str, Any],
) -> AgentToolResponse:
    resolved = _request(RunDocumentQaRequest, request)
    context = _context(resolved.job_root, resolved.job_id)
    state = _load_state(context)
    record, source, candidate = _registered_candidate(
        context, state, resolved.candidate_id
    )
    issues, qa_metadata = _run_qa_core(
        context,
        record,
        source,
        candidate,
        rendered_layout_required=resolved.rendered_layout_required,
        semantic_qa=resolved.semantic_qa,
        max_processing_concurrency=resolved.max_processing_concurrency,
        provider_timeout_seconds=resolved.provider_timeout_seconds,
    )
    status = _qa_status(issues)
    report = _new_report_path(context, "qa", resolved.candidate_id)
    counts = _issue_counts(issues)
    atomic_write_json(
        report,
        {
            "job_id": context.job_id,
            "candidate_id": resolved.candidate_id,
            "status": status.value,
            "candidate_sha256": record.sha256,
            "source_sha256": record.source_sha256,
            "issue_counts": counts,
            "issues": [_issue_payload(issue) for issue in issues],
            "semantic_qa": resolved.semantic_qa,
            "metadata": qa_metadata,
        },
    )
    qa_record = QaRecord(
        status=status,
        candidate_sha256=record.sha256,
        source_sha256=record.source_sha256,
        report_relpath=_relpath(context, report),
        issue_counts=counts,
    )
    state.candidates[resolved.candidate_id] = record.model_copy(
        update={"latest_qa": qa_record}
    )
    _save_state(context, state)
    return AgentToolResponse(
        tool="run_document_qa",
        job_id=context.job_id,
        status=status,
        summary={
            "candidate_id": resolved.candidate_id,
            "issue_counts": counts,
            "quality_score": _quality_score(counts),
            "semantic_qa": resolved.semantic_qa,
            **qa_metadata,
        },
        artifacts=[
            _artifact(
                context,
                report,
                artifact_id=f"qa:{resolved.candidate_id}",
                kind="qa_report",
            )
        ],
        issues=_issue_summaries(issues),
    )


@_serialized_tool(ApplyRepairRequest)
def apply_repair(
    request: ApplyRepairRequest | Mapping[str, Any],
) -> AgentToolResponse:
    resolved = _request(ApplyRepairRequest, request)
    context = _context(resolved.job_root, resolved.job_id)
    state = _load_state(context)
    record, source, candidate = _registered_candidate(
        context, state, resolved.candidate_id
    )
    request_fingerprint = _repair_request_fingerprint(resolved, record)
    existing_output = state.candidates.get(resolved.output_candidate_id)
    if existing_output is not None:
        return _resume_repair(
            context,
            state,
            resolved,
            parent=record,
            request_fingerprint=request_fingerprint,
        )
    _assert_new_candidate(context, state, resolved.output_candidate_id)
    blocks, translations = _recover_translation_context(
        source,
        candidate,
        source_language=record.source_language,
        target_language=record.target_language,
    )
    baseline_issues = _load_persisted_document_qa_issues(context, record)
    work_dir = _contained_output(
        context,
        context.workspace / "r" / resolved.output_candidate_id,
    )
    report_path = _contained_output(
        context,
        context.workspace
        / "reports"
        / "repair"
        / f"{resolved.output_candidate_id}.json",
    )
    repair_spec = _RepairProviderSpec(
        job_id=f"{context.job_id}-{resolved.output_candidate_id}",
        source_document=source,
        candidate_document=candidate,
        document_format=record.document_format,
        work_dir=work_dir,
        source_language=record.source_language,
        target_language=record.target_language,
        blocks=blocks,
        translations=translations,
        issues=list(baseline_issues),
        max_rounds=resolved.max_rounds,
        allowed_actions=list(RepairActionType),
        strict_layout=True,
        strict_page_count=True,
        rendered_layout_required=resolved.rendered_layout_required,
        report_path=report_path,
        metadata={"outer_agent": "pi", "parent_candidate_id": record.candidate_id},
    )
    processing_provider = (
        _processing_provider(resolved.provider_timeout_seconds)
        if resolved.use_processing_provider
        else None
    )
    source_before = sha256_file(source)
    candidate_before = sha256_file(candidate)

    def verification_qa_runner(**kwargs: Any) -> dict[str, Any]:
        verification_source = Path(kwargs["source_document"])
        verification_candidate = Path(kwargs["candidate_document"])
        issues, metadata = _run_qa_core(
            context,
            record,
            verification_source,
            verification_candidate,
            rendered_layout_required=resolved.rendered_layout_required,
            semantic_qa=resolved.use_processing_provider,
            max_processing_concurrency=1,
            provider_timeout_seconds=resolved.provider_timeout_seconds,
            authorized_coordinate_changes=kwargs.get(
                "authorized_coordinate_changes"
            ),
        )
        return {"issues": issues, "metadata": metadata}

    repair_report = asyncio.run(
        _run_repair_and_close(
            repair_spec,
            processing_provider,
            qa_runner=verification_qa_runner,
            baseline_issues=baseline_issues,
        )
    )
    if sha256_file(source) != source_before or sha256_file(candidate) != candidate_before:
        raise AgentStateError("repair agent modified a protected source or candidate")

    if repair_report.status == QaAgentState.FAILED:
        return AgentToolResponse(
            tool="apply_repair",
            job_id=context.job_id,
            status=ToolStatus.FAILED,
            summary={
                "candidate_id": resolved.candidate_id,
                "output_candidate_id": resolved.output_candidate_id,
                "round_count": len(repair_report.rounds),
                "output_created": False,
            },
            artifacts=[
                _artifact(
                    context,
                    repair_report.report_path,
                    artifact_id=f"repair:{resolved.output_candidate_id}",
                    kind="repair_report",
                )
            ],
            issues=_issue_summaries(repair_report.accepted_issues),
            error=ToolError(
                code="REPAIR_AGENT_FAILED",
                message="bounded repair agent failed before producing an accepted candidate",
                retryable=True,
            ),
        )

    accepted = _contained_existing_file(context, repair_report.accepted_document)
    if sha256_file(accepted) == candidate_before:
        return AgentToolResponse(
            tool="apply_repair",
            job_id=context.job_id,
            status=ToolStatus.NEEDS_REVIEW,
            summary={
                "candidate_id": resolved.candidate_id,
                "output_candidate_id": resolved.output_candidate_id,
                "output_created": False,
                "created_now": False,
                "round_count": len(repair_report.rounds),
                "accepted_score": repair_report.accepted_score,
                "rollback_performed": repair_report.rollback,
                "issue_counts": _issue_counts(repair_report.accepted_issues),
                "processing_provider_used": resolved.use_processing_provider,
                "resumed": False,
                "repair_invoked": True,
                "reason": "repair_did_not_change_candidate",
            },
            artifacts=[
                _artifact(
                    context,
                    repair_report.report_path,
                    artifact_id=f"repair:{resolved.output_candidate_id}",
                    kind="repair_report",
                )
            ],
            issues=_issue_summaries(repair_report.accepted_issues),
            error=ToolError(
                code="REPAIR_NOT_IMPROVED",
                message=(
                    "bounded repair produced no byte-level change; "
                    "the parent candidate remains selected"
                ),
                retryable=False,
            ),
        )
    destination = _candidate_destination(
        context, resolved.output_candidate_id, accepted.suffix.lower()
    )
    _copy_exclusive(accepted, destination)
    registered = False
    try:
        accepted_issues = list(repair_report.accepted_issues)
        counts = _issue_counts(accepted_issues)
        status = (
            ToolStatus.COMPLETED
            if repair_report.status == QaAgentState.COMPLETED
            else ToolStatus.NEEDS_REVIEW
        )
        qa_record = QaRecord(
            status=status,
            candidate_sha256=sha256_file(destination),
            source_sha256=record.source_sha256,
            report_relpath=_relpath(context, repair_report.report_path),
            issue_counts=counts,
        )
        output_record = CandidateRecord(
            candidate_id=resolved.output_candidate_id,
            relpath=_relpath(context, destination),
            sha256=sha256_file(destination),
            size_bytes=destination.stat().st_size,
            document_format=record.document_format,
            source_relpath=record.source_relpath,
            source_sha256=record.source_sha256,
            source_language=record.source_language,
            target_language=record.target_language,
            requested_target_language=record.requested_target_language,
            resolved_target_language=record.resolved_target_language,
            created_by="apply_repair",
            parent_candidate_id=record.candidate_id,
            pipeline_manifest_relpath=record.pipeline_manifest_relpath,
            pipeline_status=record.pipeline_status,
            request_fingerprint=request_fingerprint,
            latest_qa=qa_record,
        )
        state.candidates[resolved.output_candidate_id] = output_record
        _save_state(context, state)
        registered = True
    except Exception:
        if not registered:
            state.candidates.pop(resolved.output_candidate_id, None)
            destination.unlink(missing_ok=True)
            if work_dir.exists():
                shutil.rmtree(work_dir)
            report_path.unlink(missing_ok=True)
        raise
    return AgentToolResponse(
        tool="apply_repair",
        job_id=context.job_id,
        status=status,
        summary={
            "candidate_id": resolved.candidate_id,
            "output_candidate_id": resolved.output_candidate_id,
            "output_created": True,
            "created_now": True,
            "round_count": len(repair_report.rounds),
            "accepted_score": repair_report.accepted_score,
            "rollback_performed": repair_report.rollback,
            "issue_counts": counts,
            "processing_provider_used": resolved.use_processing_provider,
            "resumed": False,
            "repair_invoked": True,
        },
        artifacts=[
            _artifact(
                context,
                destination,
                artifact_id=f"candidate:{resolved.output_candidate_id}",
                kind="candidate_document",
            ),
            _artifact(
                context,
                repair_report.report_path,
                artifact_id=f"repair:{resolved.output_candidate_id}",
                kind="repair_report",
            ),
        ],
        issues=_issue_summaries(accepted_issues),
    )


@_serialized_tool(VerifyCandidateRequest)
def verify_candidate(
    request: VerifyCandidateRequest | Mapping[str, Any],
) -> AgentToolResponse:
    resolved = _request(VerifyCandidateRequest, request)
    context = _context(resolved.job_root, resolved.job_id)
    state = _load_state(context)
    record, source, candidate = _registered_candidate(
        context, state, resolved.candidate_id
    )
    if not _has_current_full_qa(record):
        return AgentToolResponse(
            tool="verify_candidate",
            job_id=context.job_id,
            status=ToolStatus.NEEDS_REVIEW,
            summary={
                "candidate_id": resolved.candidate_id,
                "verified": False,
                "full_qa_required": True,
            },
            error=ToolError(
                code="FULL_QA_REQUIRED",
                message=(
                    "candidate must pass the current persisted run_document_qa "
                    "gate before package verification"
                ),
                retryable=False,
            ),
        )
    package = _validate_ooxml(candidate, record.document_format)
    issues, qa_metadata = _run_qa_core(
        context,
        record,
        source,
        candidate,
        rendered_layout_required=resolved.rendered_layout_required,
        semantic_qa=False,
        max_processing_concurrency=4,
        provider_timeout_seconds=60,
    )
    status = _qa_status(issues)
    counts = _issue_counts(issues)
    report = _new_report_path(context, "verification", resolved.candidate_id)
    atomic_write_json(
        report,
        {
            "job_id": context.job_id,
            "candidate_id": resolved.candidate_id,
            "status": status.value,
            "candidate_sha256": record.sha256,
            "source_sha256": record.source_sha256,
            "package": package,
            "issue_counts": counts,
            "issues": [_issue_payload(issue) for issue in issues],
            "metadata": qa_metadata,
        },
    )
    verification = QaRecord(
        status=status,
        candidate_sha256=record.sha256,
        source_sha256=record.source_sha256,
        report_relpath=_relpath(context, report),
        issue_counts=counts,
    )
    state.candidates[resolved.candidate_id] = record.model_copy(
        update={"verification": verification}
    )
    _save_state(context, state)
    return AgentToolResponse(
        tool="verify_candidate",
        job_id=context.job_id,
        status=status,
        summary={
            "candidate_id": resolved.candidate_id,
            "package_valid": True,
            "package_part_count": package["part_count"],
            "issue_counts": counts,
            "quality_score": _quality_score(counts),
            **qa_metadata,
        },
        artifacts=[
            _artifact(
                context,
                report,
                artifact_id=f"verification:{resolved.candidate_id}",
                kind="verification_report",
            )
        ],
        issues=_issue_summaries(issues),
    )


@_serialized_tool(FinalizeResultRequest)
def finalize_result(
    request: FinalizeResultRequest | Mapping[str, Any],
) -> AgentToolResponse:
    resolved = _request(FinalizeResultRequest, request)
    context = _context(resolved.job_root, resolved.job_id)
    state = _load_state(context)
    record, _source, candidate = _registered_candidate(
        context, state, resolved.candidate_id
    )
    if not _has_current_full_qa(record):
        return AgentToolResponse(
            tool="finalize_result",
            job_id=context.job_id,
            status=ToolStatus.NEEDS_REVIEW,
            summary={
                "candidate_id": resolved.candidate_id,
                "finalized": False,
                "full_qa_required": True,
            },
            error=ToolError(
                code="FULL_QA_REQUIRED",
                message=(
                    "candidate must pass the current persisted run_document_qa "
                    "gate before finalization"
                ),
                retryable=False,
            ),
        )
    verification = record.verification
    if (
        verification is None
        or verification.status != ToolStatus.COMPLETED
        or verification.candidate_sha256 != record.sha256
        or verification.source_sha256 != record.source_sha256
    ):
        return AgentToolResponse(
            tool="finalize_result",
            job_id=context.job_id,
            status=ToolStatus.NEEDS_REVIEW,
            summary={
                "candidate_id": resolved.candidate_id,
                "finalized": False,
                "verification_required": True,
            },
            error=ToolError(
                code="VERIFICATION_REQUIRED",
                message="candidate must pass current verification before finalization",
                retryable=False,
            ),
        )

    suffix = candidate.suffix.lower()
    basename = resolved.result_basename or (
        f"{context.job_id}-{record.target_language}"
    )
    supplied_suffix = Path(basename).suffix.lower()
    if supplied_suffix in SUPPORTED_CANDIDATE_SUFFIXES:
        if supplied_suffix != suffix:
            raise AgentStateError("result extension must match the candidate format")
        filename = basename
    else:
        filename = f"{basename}{suffix}"
    destination = context.workspace / "results" / filename
    destination = _contained_output(context, destination)
    result_key = filename
    existing = state.results.get(result_key)
    if existing is not None:
        existing_path = _relative_file(context, existing.relpath)
        if (
            existing.relpath != _relpath(context, destination)
            or
            existing.candidate_id != record.candidate_id
            or existing.sha256 != record.sha256
            or sha256_file(existing_path) != record.sha256
        ):
            raise AgentStateError("final result name is already bound to different data")
    elif destination.exists():
        raise AgentStateError("refusing to overwrite an unregistered final result")
    else:
        _copy_exclusive(candidate, destination)
        registered = False
        try:
            state.results[result_key] = FinalResultRecord(
                candidate_id=record.candidate_id,
                relpath=_relpath(context, destination),
                sha256=record.sha256,
                size_bytes=destination.stat().st_size,
            )
            _save_state(context, state)
            registered = True
        except Exception:
            if not registered:
                state.results.pop(result_key, None)
                destination.unlink(missing_ok=True)
            raise
    return AgentToolResponse(
        tool="finalize_result",
        job_id=context.job_id,
        status=ToolStatus.COMPLETED,
        summary={
            "candidate_id": resolved.candidate_id,
            "finalized": True,
            "document_format": record.document_format,
            "target_language": record.target_language,
        },
        artifacts=[
            _artifact(
                context,
                destination,
                artifact_id="final_result",
                kind="translated_document",
            )
        ],
    )


TOOL_FUNCTIONS = {
    "preflight_document": preflight_document,
    "translate_once": translate_once,
    "run_document_qa": run_document_qa,
    "apply_repair": apply_repair,
    "verify_candidate": verify_candidate,
    "finalize_result": finalize_result,
}


def invoke_tool(tool: str, payload: Mapping[str, Any]) -> AgentToolResponse:
    """Validate and invoke one named tool from an in-process integration."""

    model = REQUEST_MODELS.get(tool)
    function = TOOL_FUNCTIONS.get(tool)
    if model is None or function is None:
        raise ValueError(f"unknown agent tool: {tool}")
    request = model.model_validate(dict(payload))
    return function(request)


def _request(model: type[RequestT], value: RequestT | Mapping[str, Any]) -> RequestT:
    if isinstance(value, model):
        return value
    return model.model_validate(value)


def _context(job_root: Path, job_id: str) -> _ToolContext:
    root = job_root.expanduser().resolve()
    allowed_root = os.environ.get("FORMAT_TRANSLATOR_AGENT_JOBS_ROOT")
    if allowed_root and root != Path(allowed_root).expanduser().resolve():
        raise AgentPathError("job_root is outside the configured jobs boundary")
    if not root.is_dir():
        raise AgentPathError("job_root must be an existing directory")
    workspace = (root / job_id).resolve()
    try:
        relative = workspace.relative_to(root)
    except ValueError as error:
        raise AgentPathError("job workspace escapes job_root") from error
    if len(relative.parts) != 1 or relative.name != job_id:
        raise AgentPathError("job workspace must be a direct child named by job_id")
    if not workspace.is_dir():
        raise AgentPathError("job workspace must already exist")
    return _ToolContext(
        jobs_root=root,
        workspace=workspace,
        state_path=workspace / STATE_FILENAME,
        job_id=job_id,
    )


@contextmanager
def _exclusive_job_lock(context: _ToolContext) -> Iterator[None]:
    """Hold a crash-released OS file lock for the full stateful tool call."""

    raw_timeout = os.environ.get("FORMAT_TRANSLATOR_AGENT_LOCK_TIMEOUT_SECONDS", "30")
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as error:
        raise AgentConfigurationError(
            "FORMAT_TRANSLATOR_AGENT_LOCK_TIMEOUT_SECONDS must be numeric"
        ) from error
    if not 0 < timeout_seconds <= 600:
        raise AgentConfigurationError(
            "FORMAT_TRANSLATOR_AGENT_LOCK_TIMEOUT_SECONDS must be in (0, 600]"
        )

    lock_path = _contained_output(context, context.workspace / ".agent-tool.lock")
    if lock_path.is_symlink():
        raise AgentPathError("job lock must not be a symbolic link")
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise AgentPathError("cannot open the contained job lock") from error

    handle = os.fdopen(descriptor, "r+b", buffering=0)
    acquired = False
    try:
        if os.fstat(descriptor).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while not acquired:
            acquired = _try_lock_file(handle)
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise AgentStateError("another tool call is active for this job")
            time.sleep(0.05)
        yield
    finally:
        if acquired:
            _unlock_file(handle)
        handle.close()


def _try_lock_file(handle: Any) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {13, 36} or getattr(error, "winerror", None) in {
                33,
                36,
            }:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _contained_output(context: _ToolContext, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(context.workspace)
    except ValueError as error:
        raise AgentPathError("resolved path escapes the job workspace") from error
    return resolved


def _contained_existing_file(context: _ToolContext, path: Path) -> Path:
    resolved = _contained_output(context, path)
    if not resolved.is_file():
        raise FileNotFoundError("registered job file does not exist")
    return resolved


def _relative_file(context: _ToolContext, relpath: str) -> Path:
    raw = Path(relpath)
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise AgentPathError("document reference must be a safe relative path")
    return _contained_existing_file(context, context.workspace / raw)


def _relpath(context: _ToolContext, path: Path) -> str:
    resolved = _contained_output(context, path)
    return resolved.relative_to(context.workspace).as_posix()


def _load_state(
    context: _ToolContext,
    *,
    required: bool = True,
) -> AgentJobState:
    if not context.state_path.exists():
        if required:
            raise AgentStateError("preflight_document must register the job first")
        return AgentJobState(job_id=context.job_id)
    try:
        state_path = _contained_existing_file(context, context.state_path)
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        state = AgentJobState.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise AgentStateError("agent job state is missing, corrupt, or incompatible") from error
    if state.job_id != context.job_id:
        raise AgentStateError("agent job state belongs to a different job")
    return state


def _save_state(context: _ToolContext, state: AgentJobState) -> None:
    state.updated_at = datetime.now(timezone.utc)
    state_path = _contained_output(context, context.state_path)
    atomic_write_json(state_path, state.model_dump(mode="json"))


def _persist_preflight_normalized_source(
    context: _ToolContext,
    state: AgentJobState,
    source: Path,
    source_digest: str,
) -> Path:
    """Create or verify the immutable OOXML source used by later tools."""

    expected_suffix = (
        ".docx" if source.suffix.lower() in {".doc", ".docx"} else ".pptx"
    )
    previous = state.source
    if previous is not None and previous.normalized_relpath is not None:
        normalized = _relative_file(context, previous.normalized_relpath)
        if (
            previous.normalized_format != expected_suffix.lstrip(".")
            or normalized.suffix.lower() != expected_suffix
            or sha256_file(normalized) != previous.normalized_sha256
        ):
            raise AgentStateError("immutable normalized source digest or format changed")
        return normalized

    if source.suffix.lower() in SUPPORTED_CANDIDATE_SUFFIXES:
        normalized = source
    else:
        normalized_dir = _contained_output(
            context,
            context.workspace / "normalized",
        )
        expected = _contained_output(
            context,
            normalized_dir / f"{source.stem}{expected_suffix}",
        )
        if expected.exists():
            raise AgentStateError(
                "unregistered normalized output already exists; refusing overwrite"
            )
        if source.suffix.lower() == ".doc":
            normalized = convert_doc_to_docx(source, normalized_dir)
        else:
            normalized = convert_ppt_to_pptx(source, normalized_dir)
        normalized = _contained_existing_file(context, normalized)
        if normalized != expected:
            raise AgentStateError("legacy conversion returned an unexpected output path")
    if sha256_file(source) != source_digest:
        raise AgentStateError("legacy normalization modified the original source")
    normalized_digest = sha256_file(normalized)
    state.source = SourceRecord(
        input_relpath=_relpath(context, source),
        input_sha256=source_digest,
        size_bytes=source.stat().st_size,
        source_format=source.suffix.lower().lstrip("."),
        normalized_format=expected_suffix.lstrip("."),
        normalized_relpath=_relpath(context, normalized),
        normalized_sha256=normalized_digest,
    )
    return normalized


def _register_pretranslated_candidate(
    context: _ToolContext,
    state: AgentJobState,
    *,
    existing_translation_relpath: str,
    candidate_id: str,
    source_language: str,
    target_language: str,
) -> Path:
    """Register a supplied OOXML translation without copying or translating it."""

    if state.source is None or state.source.normalized_relpath is None:
        raise AgentStateError("normalized source must exist before candidate registration")
    source = _relative_file(context, state.source.input_relpath)
    normalized = _relative_file(context, state.source.normalized_relpath)
    candidate = _relative_file(context, existing_translation_relpath)
    expected_suffix = (
        ".docx"
        if state.source.source_format in {"doc", "docx"}
        else ".pptx"
    )
    if candidate.suffix.lower() not in SUPPORTED_CANDIDATE_SUFFIXES:
        raise AgentStateError("pretranslated candidate must be DOCX or PPTX")
    if candidate.suffix.lower() != expected_suffix:
        raise AgentStateError(
            "pretranslated candidate must match the source document format family"
        )
    _validate_ooxml(candidate, expected_suffix.lstrip("."))
    if candidate in {source, normalized}:
        raise AgentStateError("pretranslated candidate must differ from the source")
    digest = sha256_file(candidate)
    fingerprint = _fingerprint(
        {
            "tool": "preflight_document",
            "mode": "pretranslated_qa",
            "input_relpath": state.source.input_relpath,
            "input_sha256": state.source.input_sha256,
            "normalized_relpath": state.source.normalized_relpath,
            "normalized_sha256": state.source.normalized_sha256,
            "existing_translation_relpath": _relpath(context, candidate),
            "existing_translation_sha256": digest,
            "candidate_id": candidate_id,
            "source_language": source_language,
            "target_language": target_language,
        }
    )
    existing = state.candidates.get(candidate_id)
    if existing is not None:
        if (
            existing.created_by != "pretranslated_qa"
            or existing.relpath != _relpath(context, candidate)
            or existing.sha256 != digest
            or existing.document_format != expected_suffix.lstrip(".")
            or existing.source_relpath != _relpath(context, normalized)
            or existing.source_sha256 != state.source.normalized_sha256
            or existing.parent_candidate_id is not None
            or existing.source_language
            != ("unknown" if source_language.casefold() == "auto" else source_language)
            or existing.target_language != target_language
            or existing.request_fingerprint != fingerprint
        ):
            raise AgentStateError(
                "pretranslated candidate identifier is already bound differently"
            )
        _registered_candidate(context, state, candidate_id)
        return candidate
    if any(
        record.relpath == _relpath(context, candidate)
        for record in state.candidates.values()
    ):
        raise AgentStateError(
            "pretranslated document is already registered under another candidate"
        )
    _assert_new_candidate(context, state, candidate_id)
    normalized_sha256 = state.source.normalized_sha256
    assert normalized_sha256 is not None
    state.candidates[candidate_id] = CandidateRecord(
        candidate_id=candidate_id,
        relpath=_relpath(context, candidate),
        sha256=digest,
        size_bytes=candidate.stat().st_size,
        document_format=expected_suffix.lstrip("."),
        source_relpath=_relpath(context, normalized),
        source_sha256=normalized_sha256,
        source_language=(
            "unknown" if source_language.casefold() == "auto" else source_language
        ),
        target_language=target_language,
        requested_target_language=target_language,
        resolved_target_language=target_language,
        created_by="pretranslated_qa",
        pipeline_status="PRETRANSLATED",
        request_fingerprint=fingerprint,
    )
    return candidate


def _registered_input(
    context: _ToolContext,
    state: AgentJobState,
    input_relpath: str,
) -> Path:
    if state.source is None:
        raise AgentStateError("preflight source registration is missing")
    if state.source.input_relpath != input_relpath.replace("\\", "/"):
        raise AgentStateError("translation input differs from the preflight source")
    source = _relative_file(context, state.source.input_relpath)
    if sha256_file(source) != state.source.input_sha256:
        raise AgentStateError("registered source digest changed after preflight")
    return source


def _registered_normalized_source(
    context: _ToolContext,
    state: AgentJobState,
) -> Path:
    if (
        state.source is None
        or state.source.normalized_relpath is None
        or state.source.normalized_sha256 is None
    ):
        raise AgentStateError("preflight normalized source registration is missing")
    normalized = _relative_file(context, state.source.normalized_relpath)
    if sha256_file(normalized) != state.source.normalized_sha256:
        raise AgentStateError("registered normalized source digest changed")
    return normalized


def _registered_candidate(
    context: _ToolContext,
    state: AgentJobState,
    candidate_id: str,
) -> tuple[CandidateRecord, Path, Path]:
    if state.source is None:
        raise AgentStateError("preflight source registration is missing")
    input_path = _relative_file(context, state.source.input_relpath)
    if sha256_file(input_path) != state.source.input_sha256:
        raise AgentStateError("original source digest changed")
    record = state.candidates.get(candidate_id)
    if record is None:
        raise AgentStateError("candidate identifier is not registered")
    candidate = _relative_file(context, record.relpath)
    source = _relative_file(context, record.source_relpath)
    if sha256_file(candidate) != record.sha256:
        raise AgentStateError("immutable candidate digest changed")
    if sha256_file(source) != record.source_sha256:
        raise AgentStateError("normalized source digest changed")
    if candidate.suffix.lower().lstrip(".") != record.document_format:
        raise AgentStateError("candidate extension no longer matches its record")
    return record, source, candidate


def _assert_new_candidate(
    context: _ToolContext,
    state: AgentJobState,
    candidate_id: str,
) -> None:
    if candidate_id in state.candidates:
        raise AgentStateError("candidate identifiers are immutable and cannot be reused")
    candidate_dir = _contained_output(context, context.workspace / "candidates")
    for suffix in SUPPORTED_CANDIDATE_SUFFIXES:
        if (candidate_dir / f"{candidate_id}{suffix}").exists():
            raise AgentStateError("candidate output already exists and cannot be overwritten")


def _candidate_destination(
    context: _ToolContext,
    candidate_id: str,
    suffix: str,
) -> Path:
    if suffix not in SUPPORTED_CANDIDATE_SUFFIXES:
        raise AgentStateError("candidate must be DOCX or PPTX")
    return _contained_output(
        context,
        context.workspace / "candidates" / f"{candidate_id}{suffix}",
    )


def _copy_exclusive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as input_handle, destination.open("xb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except FileExistsError as error:
        raise AgentStateError("refusing to overwrite an immutable artifact") from error
    except Exception:
        if destination.exists():
            # This path was opened with O_EXCL by this call; removal cannot target
            # a pre-existing user artifact.
            destination.unlink()
        raise


def _translation_request_fingerprint(
    context: _ToolContext,
    state: AgentJobState,
    request: TranslateOnceRequest,
) -> str:
    if state.source is None or state.source.normalized_sha256 is None:
        raise AgentStateError("normalized source registration is missing")
    provider_payload = request.provider.model_dump(mode="json")
    if isinstance(request.provider, FixtureProviderConfig):
        fixture = _relative_file(context, request.provider.fixture_relpath)
        provider_payload["fixture_sha256"] = sha256_file(fixture)
    else:
        settings = _load_provider_settings()
        provider_payload["provider_base_url"] = settings.base_url
        provider_payload["provider_model"] = settings.model
    return _fingerprint(
        {
            "tool": "translate_once",
            "input_relpath": state.source.input_relpath,
            "input_sha256": state.source.input_sha256,
            "normalized_relpath": state.source.normalized_relpath,
            "normalized_sha256": state.source.normalized_sha256,
            "candidate_id": request.candidate_id,
            "source_language": request.source_language,
            "target_language": request.target_language,
            "provider": provider_payload,
            "glossary": request.glossary,
            "layout_mode": request.layout_mode,
            "max_translation_concurrency": request.max_translation_concurrency,
            "max_provider_retries": request.max_provider_retries,
        }
    )


def _repair_request_fingerprint(
    request: ApplyRepairRequest,
    parent: CandidateRecord,
) -> str:
    provider_identity: dict[str, str] | None = None
    if request.use_processing_provider:
        settings = _load_provider_settings()
        provider_identity = {
            "base_url": settings.base_url,
            "model": settings.model,
        }
    return _fingerprint(
        {
            "tool": "apply_repair",
            "candidate_id": request.candidate_id,
            "output_candidate_id": request.output_candidate_id,
            "parent_sha256": parent.sha256,
            "source_relpath": parent.source_relpath,
            "source_sha256": parent.source_sha256,
            "source_language": parent.source_language,
            "target_language": parent.target_language,
            "document_format": parent.document_format,
            "max_rounds": request.max_rounds,
            "rendered_layout_required": request.rendered_layout_required,
            "use_processing_provider": request.use_processing_provider,
            "provider_timeout_seconds": request.provider_timeout_seconds,
            "processing_provider": provider_identity,
        }
    )


def _fingerprint(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _resume_translation(
    context: _ToolContext,
    state: AgentJobState,
    request: TranslateOnceRequest,
    *,
    request_fingerprint: str,
) -> AgentToolResponse:
    record, _source, candidate = _registered_candidate(
        context,
        state,
        request.candidate_id,
    )
    normalized_sha256 = state.source.normalized_sha256 if state.source else None
    if (
        record.created_by != "translate_once"
        or record.parent_candidate_id is not None
        or record.request_fingerprint != request_fingerprint
        or record.source_sha256 != normalized_sha256
        or (record.requested_target_language or record.target_language)
        != request.target_language
        or (
            request.source_language != "auto"
            and record.source_language != request.source_language
        )
        or record.pipeline_manifest_relpath is None
    ):
        raise AgentStateError(
            "candidate identifier is already bound to a different translation request"
        )
    manifest_path = _relative_file(context, record.pipeline_manifest_relpath)
    try:
        manifest = ArtifactManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        raise AgentStateError("translation resume manifest is invalid") from error
    pipeline_status = str(record.pipeline_status or manifest.status.value)
    issues = list(manifest.issues)
    return AgentToolResponse(
        tool="translate_once",
        job_id=context.job_id,
        status=_tool_status(pipeline_status),
        summary={
            "pipeline_status": pipeline_status,
            "candidate_id": record.candidate_id,
            "candidate_created": False,
            "document_format": record.document_format,
            "source_language": record.source_language,
            "target_language": record.target_language,
            "requested_target_language": (
                record.requested_target_language or record.target_language
            ),
            "resolved_target_language": (
                record.resolved_target_language or record.target_language
            ),
            "provider_kind": request.provider.kind,
            "issue_counts": _issue_counts(issues),
            "auto_qa_repair": False,
            "resumed": True,
            "provider_invoked": False,
        },
        artifacts=[
            _artifact(
                context,
                candidate,
                artifact_id=f"candidate:{record.candidate_id}",
                kind="candidate_document",
            ),
            *_pipeline_report_artifacts(context, manifest),
        ],
        issues=_issue_summaries(issues),
    )


def _resume_repair(
    context: _ToolContext,
    state: AgentJobState,
    request: ApplyRepairRequest,
    *,
    parent: CandidateRecord,
    request_fingerprint: str,
) -> AgentToolResponse:
    output, _source, candidate = _registered_candidate(
        context,
        state,
        request.output_candidate_id,
    )
    qa = output.latest_qa
    if (
        output.created_by != "apply_repair"
        or output.parent_candidate_id != parent.candidate_id
        or output.request_fingerprint != request_fingerprint
        or output.source_relpath != parent.source_relpath
        or output.source_sha256 != parent.source_sha256
        or output.source_language != parent.source_language
        or output.target_language != parent.target_language
        or output.document_format != parent.document_format
        or qa is None
    ):
        raise AgentStateError(
            "output candidate identifier is already bound to a different repair request"
        )
    report = _relative_file(context, qa.report_relpath)
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AgentStateError("repair resume report is invalid") from error
    raw_issues = payload.get("accepted_issues", [])
    issues = list(raw_issues) if isinstance(raw_issues, list) else []
    return AgentToolResponse(
        tool="apply_repair",
        job_id=context.job_id,
        status=qa.status,
        summary={
            "candidate_id": parent.candidate_id,
            "output_candidate_id": output.candidate_id,
            "output_created": True,
            "created_now": False,
            "round_count": len(payload.get("rounds", [])),
            "accepted_score": payload.get("accepted_score"),
            "rollback_performed": bool(
                any(
                    bool(round_.get("rolled_back"))
                    for round_ in payload.get("rounds", [])
                    if isinstance(round_, Mapping)
                )
            ),
            "issue_counts": qa.issue_counts,
            "processing_provider_used": request.use_processing_provider,
            "resumed": True,
            "repair_invoked": False,
        },
        artifacts=[
            _artifact(
                context,
                candidate,
                artifact_id=f"candidate:{output.candidate_id}",
                kind="candidate_document",
            ),
            _artifact(
                context,
                report,
                artifact_id=f"repair:{output.candidate_id}",
                kind="repair_report",
            ),
        ],
        issues=_issue_summaries(issues),
    )


def _load_persisted_document_qa_issues(
    context: _ToolContext,
    record: CandidateRecord,
) -> list[QaIssue]:
    """Load the exact latest ``run_document_qa`` result for a candidate.

    Repair decisions must be tied to the immutable candidate digest and to the
    persisted QA artifact that the outer agent observed.  Re-running a cheaper
    deterministic baseline here would discard semantic findings and can turn a
    model's nondeterminism into a false improvement.
    """

    qa = record.latest_qa
    if qa is None:
        raise AgentStateError(
            "apply_repair requires a persisted run_document_qa report"
        )
    report_parts = Path(qa.report_relpath).parts
    if len(report_parts) < 3 or tuple(report_parts[:2]) != ("reports", "qa"):
        raise AgentStateError(
            "apply_repair requires the latest run_document_qa report"
        )
    if (
        qa.candidate_sha256 != record.sha256
        or qa.source_sha256 != record.source_sha256
    ):
        raise AgentStateError("persisted QA hashes do not match the candidate")

    report = _relative_file(context, qa.report_relpath)
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
        raw_issues = payload["issues"]
        if not isinstance(raw_issues, list):
            raise TypeError("issues must be a list")
        issues = [QaIssue.model_validate(issue) for issue in raw_issues]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValidationError) as error:
        raise AgentStateError("persisted run_document_qa report is invalid") from error

    payload_counts = payload.get("issue_counts")
    counts = _issue_counts(issues)
    if (
        payload.get("job_id") != context.job_id
        or payload.get("candidate_id") != record.candidate_id
        or payload.get("candidate_sha256") != record.sha256
        or payload.get("source_sha256") != record.source_sha256
        or payload_counts != counts
        or qa.issue_counts != counts
        or payload.get("status") != qa.status.value
    ):
        raise AgentStateError(
            "persisted run_document_qa report does not match agent state"
        )
    return issues


def _has_current_full_qa(record: CandidateRecord) -> bool:
    """Return whether the candidate passed a digest-bound full QA gate.

    Package verification is a separate check and must never replace or weaken
    the persisted ``run_document_qa`` decision.
    """

    qa = record.latest_qa
    if (
        qa is None
        or qa.status != ToolStatus.COMPLETED
        or qa.candidate_sha256 != record.sha256
        or qa.source_sha256 != record.source_sha256
    ):
        return False
    report_parts = Path(qa.report_relpath).parts
    return len(report_parts) >= 3 and tuple(report_parts[:2]) == ("reports", "qa")


def _translation_provider(
    context: _ToolContext,
    config: FixtureProviderConfig | OpenAICompatibleProviderConfig,
) -> FixtureProvider | OpenAIChatCompletionsProvider:
    if isinstance(config, FixtureProviderConfig):
        fixture = _relative_file(context, config.fixture_relpath)
        return FixtureProvider(fixture, missing=config.missing)
    settings = _load_provider_settings()
    return OpenAIChatCompletionsProvider(
        base_url=settings.base_url,
        model=settings.model,
        api_key=settings.api_key,
        timeout_seconds=config.timeout_seconds,
        temperature=config.temperature,
    )


def _processing_provider(timeout_seconds: float) -> OpenAIProcessingProvider:
    settings = _load_provider_settings()
    return OpenAIProcessingProvider(
        base_url=settings.base_url,
        model=settings.model,
        api_key=settings.api_key,
        timeout_seconds=timeout_seconds,
        temperature=0.0,
    )


def _load_provider_settings() -> _ProviderSettings:
    """Load provider secrets with standard names taking precedence."""

    names = {
        "base_url": ("OPENAI_BASE_URL", "MODEL_BASE_URL", "model_baseurl"),
        "api_key": ("OPENAI_API_KEY", "API_KEY", "api_key", "apil_key"),
        "model": ("OPENAI_MODEL", "MODEL_NAME", "model_name"),
    }
    values: dict[str, str] = {}
    for field, aliases in names.items():
        values[field] = next(
            (
                os.environ[name].strip()
                for name in aliases
                if os.environ.get(name, "").strip()
            ),
            "",
        )
    if not all(values.values()):
        configured = os.environ.get("FORMAT_TRANSLATOR_ENV_FILE")
        dotenv = (
            Path(configured).expanduser().resolve()
            if configured
            else (Path.cwd() / ".env").resolve()
        )
        file_values = _read_dotenv(dotenv) if dotenv.is_file() else {}
        for field, aliases in names.items():
            if not values[field]:
                values[field] = next(
                    (file_values[name] for name in aliases if file_values.get(name)),
                    "",
                ).strip()
    missing = [field for field, value in values.items() if not value]
    if missing:
        raise AgentConfigurationError(
            "missing OpenAI-compatible provider settings: " + ", ".join(missing)
        )
    return _ProviderSettings(**values)


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _close_translation_provider(provider: object) -> None:
    close = getattr(provider, "aclose", None)
    if not callable(close):
        return
    try:
        asyncio.run(close())
    except Exception:
        # The translation result is already persisted.  A transport cleanup
        # failure must not mutate it or disclose credentials in the response.
        return


def _select_office_artifact(artifacts: Sequence[Any]) -> Any | None:
    preferred: Any | None = None
    for artifact in artifacts:
        path = Path(artifact.path)
        if path.suffix.lower() not in SUPPORTED_CANDIDATE_SUFFIXES:
            continue
        value = getattr(artifact.artifact_type, "value", artifact.artifact_type)
        if value == ArtifactType.TRANSLATED_DOCUMENT.value:
            return artifact
        if value == ArtifactType.DIAGNOSTIC.value:
            preferred = artifact
    return preferred


def _pipeline_report_artifacts(
    context: _ToolContext,
    manifest: Any,
) -> list[ArtifactSummary]:
    result: list[ArtifactSummary] = []
    for index, item in enumerate(manifest.artifacts):
        path = Path(item.path)
        if path.suffix.lower() in SUPPORTED_CANDIDATE_SUFFIXES:
            continue
        value = str(getattr(item.artifact_type, "value", item.artifact_type))
        result.append(
            _artifact(
                context,
                path,
                artifact_id=f"pipeline:{value}:{index}",
                kind=value,
            )
        )
    manifest_path = Path(str(manifest.metadata.get("manifest_path", "")))
    if manifest_path.is_file():
        result.append(
            _artifact(
                context,
                manifest_path,
                artifact_id="pipeline:manifest",
                kind="manifest",
            )
        )
    return result


def _artifact(
    context: _ToolContext,
    path: Path,
    *,
    artifact_id: str,
    kind: str,
) -> ArtifactSummary:
    resolved = _contained_existing_file(context, path)
    return ArtifactSummary(
        artifact_id=artifact_id,
        kind=kind,
        relpath=_relpath(context, resolved),
        filename=resolved.name,
        size_bytes=resolved.stat().st_size,
        sha256=sha256_file(resolved),
        media_type=MEDIA_TYPES.get(resolved.suffix.lower()),
    )


def _recover_translation_context(
    source: Path,
    candidate: Path,
    *,
    source_language: str,
    target_language: str,
) -> tuple[list[TranslationBlock], list[TranslationResult]]:
    engine: DocxEngine | PptxEngine = (
        DocxEngine() if source.suffix.lower() == ".docx" else PptxEngine()
    )
    blocks = [
        block.model_copy(
            update={
                "source_language": source_language,
                "target_language": target_language,
            }
        )
        for block in engine.extract(source)
    ]
    candidate_blocks = {
        block.block_id: block for block in engine.extract(candidate)
    }
    translations: list[TranslationResult] = []
    for block in blocks:
        translated = candidate_blocks.get(block.block_id)
        if translated is None:
            continue
        translations.append(
            TranslationResult(
                block_id=block.block_id,
                translated_text=translated.source_text,
                segments=list(translated.segments),
                provider="agent-recovered-candidate",
                metadata={"content_not_persisted_in_agent_state": True},
            )
        )
    return blocks, translations


def _run_qa_core(
    context: _ToolContext,
    record: CandidateRecord,
    source: Path,
    candidate: Path,
    *,
    rendered_layout_required: bool,
    semantic_qa: bool,
    max_processing_concurrency: int,
    provider_timeout_seconds: float,
    authorized_coordinate_changes: Mapping[str, Sequence[int]] | None = None,
) -> tuple[list[QaIssue], dict[str, Any]]:
    source_before = sha256_file(source)
    candidate_before = sha256_file(candidate)
    blocks, translations = _recover_translation_context(
        source,
        candidate,
        source_language=record.source_language,
        target_language=record.target_language,
    )
    metadata: dict[str, Any] = {
        "block_count": len(blocks),
        "translation_result_count": len(translations),
    }
    if record.document_format == "docx":
        issues = list(
            run_docx_qa(
                source,
                candidate,
                blocks=blocks,
                translations=translations,
            )
        )
        metadata["rendered_layout"] = {
            "required": False,
            "backend": "native-ooxml",
        }
    else:
        combined = run_pptx_qa_with_rendered_layout(
            source,
            candidate,
            blocks=blocks,
            translations=translations,
            rendered_layout_required=rendered_layout_required,
            authorized_coordinate_changes=authorized_coordinate_changes,
        )
        issues = list(combined["issues"])
        metadata["rendered_layout"] = _rendered_summary(
            combined.get("rendered_layout_qa")
        )
    if semantic_qa:
        provider = _processing_provider(provider_timeout_seconds)
        semantic_issues = asyncio.run(
            _semantic_qa_and_close(
                blocks,
                translations,
                provider,
                context.job_id,
                max_processing_concurrency,
            )
        )
        issues.extend(semantic_issues)
        metadata["semantic_qa_issue_count"] = len(semantic_issues)
    else:
        metadata["semantic_qa_issue_count"] = 0
    if sha256_file(source) != source_before or sha256_file(candidate) != candidate_before:
        raise AgentStateError("QA modified a protected Office document")
    return issues, metadata


async def _semantic_qa_and_close(
    blocks: Sequence[TranslationBlock],
    translations: Sequence[TranslationResult],
    provider: OpenAIProcessingProvider,
    job_id: str,
    concurrency: int,
) -> list[QaIssue]:
    try:
        return await _semantic_qa(
            blocks,
            translations,
            provider,
            job_id,
            concurrency,
        )
    finally:
        await provider.aclose()


async def _run_repair_and_close(
    spec: _RepairProviderSpec,
    provider: OpenAIProcessingProvider | None,
    *,
    qa_runner: Any,
    baseline_issues: Sequence[QaIssue],
) -> Any:
    try:
        return await run_qa_repair_agent_async(
            spec,
            processing_provider=provider,
            qa_runner=qa_runner,
            baseline_issues=baseline_issues,
        )
    finally:
        if provider is not None:
            await provider.aclose()


def _rendered_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"available": False, "backend": "unknown"}
    capabilities = value.get("capabilities")
    if isinstance(capabilities, Sequence) and not isinstance(
        capabilities, (str, bytes)
    ):
        safe_capabilities = [str(item)[:80] for item in capabilities]
    else:
        safe_capabilities = []
    return {
        "available": bool(value.get("available", False)),
        "backend": str(value.get("backend") or value.get("provider") or "unknown")[
            :80
        ],
        "precision": str(value.get("precision") or "unknown")[:80],
        "capabilities": safe_capabilities,
        "required": bool(value.get("required", False)),
    }


def _validate_ooxml(path: Path, document_format: str) -> dict[str, Any]:
    required = (
        "word/document.xml"
        if document_format == "docx"
        else "ppt/presentation.xml"
    )
    try:
        with zipfile.ZipFile(path) as package:
            corrupt = package.testzip()
            if corrupt is not None:
                raise AgentStateError("candidate contains a corrupt OOXML part")
            names = set(package.namelist())
    except zipfile.BadZipFile as error:
        raise AgentStateError("candidate is not a valid OOXML package") from error
    if required not in names:
        raise AgentStateError("candidate is missing a required OOXML part")
    return {
        "valid": True,
        "required_part": required,
        "part_count": len(names),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _new_report_path(
    context: _ToolContext,
    kind: str,
    candidate_id: str,
) -> Path:
    path = (
        context.workspace
        / "reports"
        / kind
        / f"{candidate_id}-{uuid4().hex[:12]}.json"
    )
    return _contained_output(context, path)


def _issue_payload(issue: Any) -> dict[str, Any]:
    if isinstance(issue, Mapping):
        value = dict(issue)
    else:
        dumper = getattr(issue, "model_dump", None)
        if callable(dumper):
            value = dict(dumper(mode="json"))
        else:
            value = {
                "code": getattr(issue, "code", type(issue).__name__),
                "message": str(getattr(issue, "message", issue)),
                "severity": getattr(issue, "severity", "error"),
            }
    return _jsonable(value)


def _issue_summaries(issues: Sequence[Any]) -> list[IssueSummary]:
    summaries: list[IssueSummary] = []
    for issue in issues[:200]:
        value = _issue_payload(issue)
        severity = value.get("severity", "error")
        if isinstance(severity, Mapping):
            severity = severity.get("value", "error")
        severity_text = str(getattr(severity, "value", severity)).lower()
        if severity_text not in {"error", "warning", "info"}:
            severity_text = "error"
        summaries.append(
            IssueSummary(
                code=_short(value.get("code") or "UNKNOWN_QA_ISSUE", 160),
                message=_short(value.get("message") or "QA issue detected", 500),
                severity=severity_text,
                stage=_optional_short(value.get("stage"), 120),
                block_id=_optional_short(value.get("block_id"), 240),
                page=_positive_int(value.get("page")),
                slide=_positive_int(value.get("slide")),
                recoverable=bool(value.get("recoverable", True)),
            )
        )
    return summaries


def _issue_counts(issues: Sequence[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for issue in issues:
        value = _issue_payload(issue).get("severity", "error")
        value = str(getattr(value, "value", value)).lower()
        counts[value if value in {"error", "warning", "info"} else "error"] += 1
    return {name: counts[name] for name in ("error", "warning", "info")}


def _quality_score(counts: Mapping[str, int]) -> float:
    penalty = min(70, counts["error"] * 10)
    penalty += min(25, counts["warning"] * 2)
    penalty += min(5, counts["info"] * 0.5)
    return round(max(0.0, 100.0 - penalty), 1)


def _qa_status(issues: Sequence[Any]) -> ToolStatus:
    return (
        ToolStatus.NEEDS_REVIEW
        if _issue_counts(issues)["error"]
        else ToolStatus.COMPLETED
    )


def _tool_status(status: Any) -> ToolStatus:
    value = str(status).upper()
    if value == "COMPLETED":
        return ToolStatus.COMPLETED
    if value == "NEEDS_REVIEW":
        return ToolStatus.NEEDS_REVIEW
    return ToolStatus.FAILED


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return (text or "unknown")[:limit]


def _optional_short(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return _short(value, limit)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 1 else None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return _short(value, 500)


__all__ = [
    "AgentConfigurationError",
    "AgentPathError",
    "AgentStateError",
    "AgentToolFailure",
    "TOOL_FUNCTIONS",
    "apply_repair",
    "finalize_result",
    "invoke_tool",
    "preflight_document",
    "run_document_qa",
    "translate_once",
    "verify_candidate",
]
