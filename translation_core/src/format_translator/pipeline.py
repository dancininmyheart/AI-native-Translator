from __future__ import annotations

import asyncio
import math
import re
import shutil
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Sequence

from .docx_engine import DocxEngine
from .glossary import glossary_issues
from .languages import (
    detect_source_language,
    detect_source_language_details,
    normalize_source_language,
    resolve_target_language,
    validate_language_pair,
)
from .legacy import convert_doc_to_docx, convert_ppt_to_pptx
from .models import (
    Artifact,
    ArtifactManifest,
    ArtifactType,
    CleanupReport,
    DocumentManifest,
    IssueSeverity,
    JobSpec,
    JobStatus,
    ProtectedText,
    ProtectedToken,
    QaIssue,
    QaRepairSpec as ProviderQaRepairSpec,
    SampleSpec,
    StageTiming,
    TextSegment,
    TranslationBlock,
    TranslationResult,
)
from .pptx_engine import PptxEngine
from .pptx_rendered_layout_qa import run_pptx_qa_with_rendered_layout
from .protection import (
    ProtectionValidationError,
    protect_text,
    restore_text,
    validate_protected_tokens,
    validate_restored_values,
)
from .providers import (
    FixtureProvider,
    ProcessingProvider,
    ProviderError,
    ProviderProtocolError,
    RetryableProviderError,
    TranslationProvider,
)
from .qa_agent import run_qa_repair_agent_async
from .utils import atomic_copy, atomic_write_json, sha256_file
from .word_qa import run_docx_qa


MAX_INPUT_BYTES = 50 * 1024 * 1024
PLAIN_BATCH_SIZE = 20
TAGGED_BATCH_SIZE = 4
MAX_RETRYABLE_BATCH_SPLIT_DEPTH = 2
MAX_PROTOCOL_BATCH_SPLIT_DEPTH = math.ceil(math.log2(PLAIN_BATCH_SIZE))
DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS = 16
MIN_EXTRA_PROVIDER_RECOVERY_CALLS = DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS
MAX_EXTRA_PROVIDER_RECOVERY_CALLS = 32
PROVIDER_RECOVERY_CALL_SCALING_START_BATCHES = 36
PROVIDER_RECOVERY_INITIAL_BATCHES_PER_EXTRA_CALL = 4
DEFAULT_PROVIDER_RECOVERY_SECONDS = 180.0
MIN_PROVIDER_RECOVERY_SECONDS = DEFAULT_PROVIDER_RECOVERY_SECONDS
MAX_PROVIDER_RECOVERY_SECONDS = 600.0
PROVIDER_RECOVERY_WALL_TIME_SCALING_START_BATCHES = 36
PROVIDER_RECOVERY_SECONDS_PER_BATCH_ABOVE_DEFAULT = 7.5
MAX_STRICT_TAGGED_RETRY_BLOCKS = 8
MAX_STRICT_TAGGED_PROVIDER_ATTEMPTS = 2
MAX_SEMANTIC_QA_EXTRA_LOGICAL_CALLS = 3
MAX_SEMANTIC_QA_SECONDS = 180.0
SEMANTIC_QA_PROTOCOL_OUTAGE_THRESHOLD = 3
PPTX_TEXT_FIT_ISSUE_CODES = frozenset(
    {
        "PPTX_RENDERED_TEXT_OVERFLOW",
        "PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
    }
)
TEXT_FIT_LENGTH_ERROR_PATTERN = re.compile(
    r"fitted text has (?P<actual>\d+) characters; maximum is (?P<maximum>\d+)"
)
ADAPTIVE_SPLIT_RETRYABLE_PATTERN = re.compile(r"\bHTTP\s+(?:502|503)\b", re.IGNORECASE)
_DOCX_RUN_SEGMENT_METADATA_KEYS = frozenset(
    {"xpath", "run_index", "in_hyperlink", "in_field"}
)
_DOCX_COALESCIBLE_RISK_FLAGS = frozenset({"multiple_text_nodes", "multiple_styles"})
_DOCX_KNOWN_RISK_FLAGS = _DOCX_COALESCIBLE_RISK_FLAGS | frozenset(
    {
        "hyperlink",
        "field",
        "bookmark",
        "comment_anchor",
        "tracked_change",
        "content_control",
        "drawing_or_object",
        "manual_break",
        "tab",
        "note_reference",
        "multiple_hyperlinks",
    }
)


class TranslationPipelineError(RuntimeError):
    pass


class TranslationValidationError(TranslationPipelineError):
    pass


class _ProviderRecoveryBudgetExceeded(ProviderProtocolError):
    pass


class _ProviderRecoveryDeadlineExceeded(RetryableProviderError):
    pass


@dataclass(frozen=True)
class _DocxProviderSegmentGroup:
    provider_segment_id: str
    original_segments: tuple[TextSegment, ...]


@dataclass(frozen=True)
class _DocxProviderBlockProjection:
    block_id: str
    groups: tuple[_DocxProviderSegmentGroup, ...]


def _provider_recovery_calls_for_initial_batches(initial_batch_count: int) -> int:
    if initial_batch_count < 0:
        raise ValueError("initial_batch_count must be non-negative")
    batches_above_default = max(
        0,
        initial_batch_count - PROVIDER_RECOVERY_CALL_SCALING_START_BATCHES,
    )
    additional_calls = math.ceil(
        batches_above_default / PROVIDER_RECOVERY_INITIAL_BATCHES_PER_EXTRA_CALL
    )
    return min(
        MAX_EXTRA_PROVIDER_RECOVERY_CALLS,
        max(
            MIN_EXTRA_PROVIDER_RECOVERY_CALLS,
            DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS + additional_calls,
        ),
    )


def _provider_recovery_seconds_for_initial_batches(
    initial_batch_count: int,
) -> float:
    if initial_batch_count < 0:
        raise ValueError("initial_batch_count must be non-negative")
    batches_above_default = max(
        0,
        initial_batch_count - PROVIDER_RECOVERY_WALL_TIME_SCALING_START_BATCHES,
    )
    scaled_seconds = (
        DEFAULT_PROVIDER_RECOVERY_SECONDS
        + batches_above_default * PROVIDER_RECOVERY_SECONDS_PER_BATCH_ABOVE_DEFAULT
    )
    return min(
        MAX_PROVIDER_RECOVERY_SECONDS,
        max(MIN_PROVIDER_RECOVERY_SECONDS, scaled_seconds),
    )


class _ProviderRecoveryBudget:
    """Candidate-wide cap for failure-triggered translation provider calls."""

    def __init__(
        self,
        *,
        max_calls: int | None = None,
        max_seconds: float | None = None,
    ) -> None:
        self.max_calls = (
            DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS if max_calls is None else max_calls
        )
        self.max_seconds = (
            DEFAULT_PROVIDER_RECOVERY_SECONDS if max_seconds is None else max_seconds
        )
        self._call_count = 0
        self._deadline: float | None = None
        self._lock = asyncio.Lock()

    async def begin(self) -> None:
        """Start recovery time at the failure decision, without consuming a call."""

        await self._remaining(consume_call=False)

    async def sleep(self, delay: float) -> None:
        """Apply recovery backoff inside the shared wall-clock deadline."""

        remaining = await self._remaining(consume_call=False)
        try:
            async with asyncio.timeout(remaining):
                await asyncio.sleep(delay)
        except TimeoutError as exc:
            raise self._deadline_exceeded() from exc

    async def run(
        self,
        operation: Callable[[], Awaitable[list[TranslationResult]]],
    ) -> list[TranslationResult]:
        remaining = await self._remaining(consume_call=True)
        try:
            async with asyncio.timeout(remaining):
                return await operation()
        except TimeoutError as exc:
            raise self._deadline_exceeded() from exc

    async def _remaining(self, *, consume_call: bool) -> float:
        async with self._lock:
            if self._call_count >= self.max_calls:
                raise _ProviderRecoveryBudgetExceeded(
                    f"provider recovery stopped after {self.max_calls} extra calls"
                )
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._deadline is None:
                self._deadline = now + self.max_seconds
            remaining = self._deadline - now
            if remaining <= 0:
                raise _ProviderRecoveryDeadlineExceeded(
                    f"provider recovery exceeded {self.max_seconds:g} seconds"
                )
            if consume_call:
                self._call_count += 1
            return remaining

    def _deadline_exceeded(self) -> _ProviderRecoveryDeadlineExceeded:
        return _ProviderRecoveryDeadlineExceeded(
            f"provider recovery exceeded {self.max_seconds:g} seconds"
        )


@contextmanager
def _timed_stage(
    timings: list[StageTiming],
    stage: str,
    *,
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        yield
    finally:
        finished_at = datetime.now(timezone.utc)
        timings.append(
            StageTiming(
                stage=stage,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=time.perf_counter() - started,
                provider=provider,
                metadata=metadata or {},
            )
        )


def inspect_document(path: Path) -> DocumentManifest:
    source = _validate_input(path)
    with tempfile.TemporaryDirectory(prefix="format-translator-inspect-") as temp:
        temporary = Path(temp)
        normalized = _normalize_input(source, temporary)
        engine = _engine_for(normalized)
        snapshot = engine.inspect(normalized)
        blocks = engine.extract(normalized)
        return DocumentManifest(
            input_path=source,
            # The normalized DOCX for a legacy .doc lives only for this inspection
            # call, so never expose that soon-to-be-deleted temporary path.
            normalized_path=source,
            source_format=source.suffix.lower().lstrip("."),
            normalized_format=normalized.suffix.lower().lstrip("."),
            page_count=snapshot.get("slide_count") or snapshot.get("page_count"),
            block_count=len(blocks),
            media_hashes=snapshot.get("media_hashes", {}),
            metadata={
                "section_sizes": snapshot.get("section_sizes", []),
                "package_entry_count": snapshot.get("package_entry_count", 0),
                "normalized_is_temporary": source.suffix.lower() == ".doc",
            },
        )


def translate_document(
    spec: JobSpec,
    provider: TranslationProvider | None = None,
    processing_provider: ProcessingProvider | None = None,
) -> ArtifactManifest:
    return asyncio.run(
        _translate_document_async(
            spec,
            provider or _provider_from_metadata(spec.metadata),
            processing_provider,
        )
    )


async def _translate_document_async(
    spec: JobSpec,
    provider: TranslationProvider,
    processing_provider: ProcessingProvider | None,
) -> ArtifactManifest:
    source = _validate_input(spec.input_path)
    job_root = spec.output_dir.expanduser().resolve() / spec.job_id
    if job_root.exists():
        raise FileExistsError(f"Job directory already exists: {job_root}")
    work = job_root / "work"
    artifacts_dir = job_root / "artifacts"
    input_dir = work / "input"
    normalized_dir = work / "normalized"
    for directory in (input_dir, normalized_dir, artifacts_dir):
        directory.mkdir(parents=True, exist_ok=False)

    manifest = ArtifactManifest(
        job_id=spec.job_id,
        status=JobStatus.RECEIVED,
        source_path=source,
        target_language=spec.target_language,
        requested_target_language=spec.target_language,
        metadata={"job_root": str(job_root), "layout_mode": spec.layout_mode},
    )
    manifest_path = job_root / "manifest.json"
    _persist_manifest(manifest, manifest_path)

    try:
        with _timed_stage(manifest.timings, "input"):
            copied_source = input_dir / source.name
            atomic_copy(source, copied_source)
            normalized = _normalize_input(copied_source, normalized_dir)
        manifest.status = JobStatus.EXTRACTING
        _persist_manifest(manifest, manifest_path)

        engine = _engine_for(normalized)
        with _timed_stage(manifest.timings, "extract"):
            baseline = engine.inspect(normalized)
            source_blocks = engine.extract(normalized)
            no_translatable_content = not source_blocks
            source_text = "\n".join(block.source_text for block in source_blocks)
            if no_translatable_content and spec.source_language == "auto":
                source_language = "und"
                detection_metadata = {
                    "family": "none",
                    "confidence": 0.0,
                    "method": "no_translatable_content",
                    "ambiguous": True,
                }
            elif spec.source_language == "auto":
                detection = detect_source_language_details(source_text)
                source_language = detection.language
                detection_metadata = {
                    "family": detection.family,
                    "confidence": detection.confidence,
                    "method": detection.method,
                    "ambiguous": detection.ambiguous,
                }
            else:
                source_language = normalize_source_language(
                    spec.source_language,
                    allow_auto=False,
                )
                detection_metadata = {
                    "family": source_language.split("-", 1)[0],
                    "confidence": 1.0,
                    "method": "explicit",
                    "ambiguous": False,
                }
            resolved_target_language = resolve_target_language(
                source_language,
                spec.target_language,
            )
            validate_language_pair(source_language, resolved_target_language)
            manifest.target_language = resolved_target_language
            manifest.resolved_target_language = resolved_target_language
            prepared, protected_by_id = _prepare_blocks(
                source_blocks,
                source_language=source_language,
                target_language=resolved_target_language,
                glossary=spec.glossary,
            )
            manifest.metadata.update(
                {
                    "source_language": source_language,
                    "requested_target_language": spec.target_language,
                    "resolved_target_language": resolved_target_language,
                    "language_detection": detection_metadata,
                    "block_count": len(prepared),
                    "source_format": source.suffix.lower().lstrip("."),
                    "normalized_path": str(normalized),
                    "media_hashes": baseline.get("media_hashes", {}),
                    "no_translatable_content": no_translatable_content,
                    "provider_invoked": False,
                }
            )
            if no_translatable_content:
                manifest.issues.append(
                    QaIssue(
                        code="NO_TRANSLATABLE_CONTENT",
                        message=(
                            "The document contains no translatable user text; "
                            "the normalized Office package was preserved unchanged."
                        ),
                        severity=IssueSeverity.INFO,
                        stage="extract",
                        recoverable=False,
                        details={
                            "noop": True,
                            "provider_invoked": False,
                            "normalized_format": normalized.suffix.lower().lstrip("."),
                        },
                    )
                )

        results: list[TranslationResult]
        if no_translatable_content:
            results = []
            with _timed_stage(
                manifest.timings,
                "translation",
                metadata={
                    "skipped": True,
                    "reason": "no_translatable_content",
                },
            ):
                pass
        else:
            manifest.status = JobStatus.TRANSLATING
            manifest.metadata["provider_invoked"] = True
            _persist_manifest(manifest, manifest_path)
            recovery_budget = _provider_recovery_budget_for_blocks(prepared)
            with _timed_stage(
                manifest.timings,
                "translation",
                provider=getattr(provider, "name", provider.__class__.__name__),
            ):
                raw_results = await _translate_blocks(
                    prepared,
                    provider=provider,
                    source_language=source_language,
                    target_language=resolved_target_language,
                    job_id=spec.job_id,
                    concurrency=spec.max_translation_concurrency,
                    retries=spec.max_provider_retries,
                    recovery_budget=recovery_budget,
                )
                (
                    results,
                    validation_issues,
                ) = await _validate_and_restore_with_strict_retry(
                    source_blocks,
                    prepared,
                    raw_results,
                    protected_by_id,
                    spec.glossary,
                    provider=provider,
                    source_language=source_language,
                    target_language=resolved_target_language,
                    job_id=spec.job_id,
                    concurrency=spec.max_translation_concurrency,
                    allow_fixture_unprotected=isinstance(provider, FixtureProvider),
                    recovery_budget=recovery_budget,
                )
                manifest.issues.extend(validation_issues)
                if any(
                    issue.severity == IssueSeverity.ERROR for issue in validation_issues
                ):
                    raise TranslationValidationError(
                        "One or more translation blocks failed validation"
                    )

        if (
            not no_translatable_content
            and spec.semantic_qa
            and processing_provider is not None
        ):
            manifest.status = JobStatus.SEMANTIC_QA
            _persist_manifest(manifest, manifest_path)
            semantic_qa_audit: dict[str, Any] = {}
            with _timed_stage(
                manifest.timings,
                "semantic_qa",
                provider=getattr(
                    processing_provider,
                    "name",
                    processing_provider.__class__.__name__,
                ),
                metadata=semantic_qa_audit,
            ):
                qa_issues = await _semantic_qa(
                    source_blocks,
                    results,
                    processing_provider,
                    spec.job_id,
                    spec.max_translation_concurrency,
                    audit_metadata=semantic_qa_audit,
                )
                manifest.issues.extend(qa_issues)
                if any(issue.severity == IssueSeverity.ERROR for issue in qa_issues):
                    raise TranslationValidationError("Semantic QA did not pass")

        manifest.status = JobStatus.PATCHING
        _persist_manifest(manifest, manifest_path)
        document_suffix = normalized.suffix.lower()
        candidate_document = work / f"translated-r0{document_suffix}"
        with _timed_stage(manifest.timings, "patch"):
            if no_translatable_content:
                atomic_copy(normalized, candidate_document)
                patch_report = {
                    "validation": engine.validate(candidate_document, baseline)
                }
            else:
                patch_report = engine.patch(
                    normalized,
                    candidate_document,
                    results,
                    blocks=source_blocks,
                    strict=True,
                )
            if not patch_report["validation"]["valid"]:
                raise TranslationPipelineError(
                    f"Patched {document_suffix.upper()} failed package validation"
                )

        manifest.status = JobStatus.LAYOUT_QA
        _persist_manifest(manifest, manifest_path)
        with _timed_stage(manifest.timings, "layout_qa"):
            layout_issues = _run_office_qa(
                normalized,
                candidate_document,
                blocks=source_blocks,
                translations=results,
            )

        if (
            any(issue.severity == IssueSeverity.ERROR for issue in layout_issues)
            and processing_provider is not None
            and spec.max_text_fit_rounds > 0
        ):
            (
                candidate_document,
                results,
                layout_issues,
                fit_issues,
            ) = await _attempt_text_fit(
                source_path=normalized,
                source_blocks=source_blocks,
                results=results,
                protected_by_id=protected_by_id,
                processing_provider=processing_provider,
                engine=engine,
                work=work,
                job_id=spec.job_id,
                rounds=spec.max_text_fit_rounds,
                concurrency=spec.max_translation_concurrency,
                layout_issues=layout_issues,
            )
            manifest.issues.extend(fit_issues)

        if (
            any(issue.severity == IssueSeverity.ERROR for issue in layout_issues)
            and spec.auto_qa_repair
            and spec.max_qa_repair_rounds > 0
        ):
            with _timed_stage(manifest.timings, "qa_repair_agent"):
                repair_report = await run_qa_repair_agent_async(
                    ProviderQaRepairSpec(
                        job_id=f"{spec.job_id}-qa-repair",
                        source_document=normalized,
                        candidate_document=candidate_document,
                        document_format=document_suffix.lstrip("."),
                        work_dir=work / "qa-repair",
                        source_language=source_language,
                        target_language=resolved_target_language,
                        blocks=list(source_blocks),
                        translations=list(results),
                        issues=list(layout_issues),
                        max_rounds=spec.max_qa_repair_rounds,
                        strict_layout=spec.layout_mode == "strict",
                        strict_page_count=spec.layout_mode == "strict",
                    ),
                    processing_provider=processing_provider,
                )
            manifest.metadata["qa_repair_agent"] = {
                "status": repair_report.status.value,
                "report_path": str(repair_report.report_path),
                "round_count": len(repair_report.rounds),
                "accepted_score": repair_report.accepted_score,
            }
            accepted_document = getattr(
                repair_report,
                "accepted_document",
                getattr(repair_report, "candidate_document", None),
            )
            if accepted_document is not None and Path(accepted_document).is_file():
                candidate_document = Path(accepted_document)
                layout_issues = _run_office_qa(
                    normalized,
                    candidate_document,
                    blocks=source_blocks,
                    translations=results,
                )
            if repair_report.status.value == "COMPLETED":
                manifest.issues.append(
                    QaIssue(
                        code="QA_AUTO_REPAIR_APPLIED",
                        message=(
                            "The QA repair agent produced a candidate that "
                            "passed the layout gate."
                        ),
                        severity=IssueSeverity.INFO,
                        stage="qa_repair_agent",
                        details={
                            "round_count": len(repair_report.rounds),
                            "report_path": str(repair_report.report_path),
                        },
                    )
                )

        manifest.issues.extend(layout_issues)
        layout_failed = any(
            issue.severity == IssueSeverity.ERROR for issue in layout_issues
        )

        translated_document = artifacts_dir / (
            f"{source.stem}-{resolved_target_language}{document_suffix}"
        )
        if layout_failed:
            diagnostic_document = artifacts_dir / (
                f"{source.stem}-{resolved_target_language}-needs-review{document_suffix}"
            )
            atomic_copy(candidate_document, diagnostic_document)
            manifest.artifacts.append(
                _artifact(diagnostic_document, ArtifactType.DIAGNOSTIC)
            )
        else:
            atomic_copy(candidate_document, translated_document)
            manifest.artifacts.append(
                _artifact(
                    translated_document,
                    ArtifactType.TRANSLATED_DOCUMENT,
                    resolved_target_language,
                )
            )

        error_issues = [
            issue for issue in manifest.issues if issue.severity == IssueSeverity.ERROR
        ]
        manifest.status = (
            JobStatus.NEEDS_REVIEW if error_issues else JobStatus.COMPLETED
        )
        _write_reports(manifest, job_root, artifacts_dir)
        _persist_manifest(manifest, manifest_path)
        return manifest
    except Exception as exc:
        manifest.status = (
            JobStatus.NEEDS_REVIEW
            if isinstance(
                exc,
                (
                    TranslationValidationError,
                    ProtectionValidationError,
                    ProviderProtocolError,
                ),
            )
            else JobStatus.FAILED
        )
        qa_unavailable_recorded = isinstance(exc, TranslationValidationError) and any(
            issue.stage == "semantic_qa"
            and issue.details.get("status") == "qa_unavailable"
            and issue.details.get("category") == "infrastructure"
            for issue in manifest.issues
        )
        if not qa_unavailable_recorded:
            manifest.issues.append(
                QaIssue(
                    code=exc.__class__.__name__,
                    message=str(exc),
                    severity=IssueSeverity.ERROR,
                    stage=manifest.status.value,
                    recoverable=manifest.status == JobStatus.NEEDS_REVIEW,
                )
            )
        _write_reports(manifest, job_root, artifacts_dir)
        _persist_manifest(manifest, manifest_path)
        return manifest


def build_page_sample(
    spec: SampleSpec,
    provider: TranslationProvider | None = None,
) -> ArtifactManifest:
    return asyncio.run(
        _build_page_sample_async(
            spec,
            provider or _provider_from_metadata(spec.metadata),
        )
    )


async def _build_page_sample_async(
    spec: SampleSpec,
    provider: TranslationProvider,
) -> ArtifactManifest:
    source = _validate_input(spec.input_path)
    job_root = spec.output_dir.expanduser().resolve() / spec.job_id
    if job_root.exists():
        raise FileExistsError(f"Job directory already exists: {job_root}")
    work = job_root / "work"
    artifacts_dir = job_root / "artifacts"
    work.mkdir(parents=True)
    artifacts_dir.mkdir()
    manifest = ArtifactManifest(
        job_id=spec.job_id,
        status=JobStatus.RECEIVED,
        source_path=source,
        metadata={"mode": "sample", "page": spec.page},
    )
    manifest_path = job_root / "manifest.json"
    _persist_manifest(manifest, manifest_path)
    try:
        copied = work / source.name
        atomic_copy(source, copied)
        normalized = _normalize_input(copied, work / "normalized")
        engine = _engine_for(normalized)
        all_blocks = engine.extract(normalized)
        sample_blocks = _select_sample_blocks(all_blocks, normalized, spec.page)
        if not sample_blocks:
            raise TranslationPipelineError(
                f"No text blocks were found for page/slide {spec.page}"
            )
        source_text = "\n".join(block.source_text for block in sample_blocks)
        source_language = (
            detect_source_language(source_text)
            if spec.source_language == "auto"
            else spec.source_language
        )
        for target in spec.target_languages:
            validate_language_pair(source_language, target)
            prepared, protected = _prepare_blocks(
                sample_blocks,
                source_language=source_language,
                target_language=target,
                glossary=spec.glossary,
            )
            raw = await _translate_blocks(
                prepared,
                provider=provider,
                source_language=source_language,
                target_language=target,
                job_id=f"{spec.job_id}-{target}",
                concurrency=spec.max_translation_concurrency,
                retries=3,
            )
            restored, issues = _validate_and_restore_results(
                sample_blocks,
                raw,
                protected,
                spec.glossary,
                allow_fixture_unprotected=isinstance(provider, FixtureProvider),
            )
            manifest.issues.extend(issues)
            translated_by_id = {result.block_id: result for result in restored}
            complete_results = [
                translated_by_id.get(block.block_id) or _passthrough_result(block)
                for block in all_blocks
            ]
            output = artifacts_dir / (
                f"{source.stem}-page-{spec.page}-{target}-sample"
                f"{normalized.suffix.lower()}"
            )
            patch_report = engine.patch(
                normalized,
                output,
                complete_results,
                blocks=all_blocks,
                strict=True,
            )
            if not patch_report["validation"]["valid"]:
                raise TranslationPipelineError(
                    f"Sample {normalized.suffix.upper()} failed package validation"
                )
            manifest.issues.extend(
                _run_office_qa(
                    normalized,
                    output,
                    blocks=all_blocks,
                    translations=complete_results,
                )
            )
            manifest.artifacts.append(
                _artifact(output, ArtifactType.TRANSLATED_DOCUMENT, target)
            )
        manifest.status = (
            JobStatus.NEEDS_REVIEW
            if any(issue.severity == IssueSeverity.ERROR for issue in manifest.issues)
            else JobStatus.COMPLETED
        )
        _write_reports(manifest, job_root, artifacts_dir)
        _persist_manifest(manifest, manifest_path)
        return manifest
    except Exception as exc:
        manifest.status = JobStatus.FAILED
        manifest.issues.append(
            QaIssue(
                code=exc.__class__.__name__,
                message=str(exc),
                severity=IssueSeverity.ERROR,
                stage="sample",
            )
        )
        _write_reports(manifest, job_root, artifacts_dir)
        _persist_manifest(manifest, manifest_path)
        return manifest


def cleanup_jobs(root: Path, older_than_days: int = 7) -> CleanupReport:
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        return CleanupReport()
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    report = CleanupReport()
    for child in resolved_root.iterdir():
        try:
            resolved_child = child.resolve()
            if resolved_child.parent != resolved_root or not resolved_child.is_dir():
                continue
            manifest = resolved_child / "manifest.json"
            if not manifest.is_file():
                continue
            modified = datetime.fromtimestamp(
                manifest.stat().st_mtime,
                tz=timezone.utc,
            )
            if modified >= cutoff:
                continue
            size = sum(
                item.stat().st_size
                for item in resolved_child.rglob("*")
                if item.is_file()
            )
            shutil.rmtree(resolved_child)
            report.removed_jobs.append(resolved_child.name)
            report.removed_bytes += size
        except Exception as exc:
            report.failures[child.name] = str(exc)
    return report


def _validate_input(path: Path) -> Path:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in {".doc", ".docx", ".ppt", ".pptx"}:
        raise ValueError(
            "Only Word (.doc/.docx) and PowerPoint (.ppt/.pptx) are accepted"
        )
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Input exceeds the 50 MB core-module limit")
    return source


def _normalize_input(source: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".doc":
        return convert_doc_to_docx(source, output_dir)
    if source.suffix.lower() == ".ppt":
        return convert_ppt_to_pptx(source, output_dir)
    destination = output_dir / source.name
    if destination.resolve() != source.resolve():
        atomic_copy(source, destination)
    return destination


def _prepare_blocks(
    blocks: Sequence[TranslationBlock],
    *,
    source_language: str,
    target_language: str,
    glossary: dict[str, str],
) -> tuple[list[TranslationBlock], dict[str, ProtectedText]]:
    prepared: list[TranslationBlock] = []
    protected_by_id: dict[str, ProtectedText] = {}
    for block in blocks:
        protected = protect_text(block.source_text)
        protected_by_id[block.block_id] = protected
        metadata = dict(block.metadata)
        metadata["original_source_text"] = block.source_text
        prepared.append(
            block.model_copy(
                update={
                    "source_text": protected.protected_text,
                    "segments": _protect_segments(
                        block.segments,
                        protected.tokens,
                    ),
                    "source_language": source_language,
                    "target_language": target_language,
                    "glossary": glossary,
                    "protected_tokens": protected.tokens,
                    "metadata": metadata,
                }
            )
        )
    return prepared, protected_by_id


def _protect_segments(
    segments: Sequence[TextSegment],
    tokens: Sequence[ProtectedToken],
) -> list[TextSegment]:
    """Apply block-level protected placeholders to formatting-run segments."""

    if not tokens:
        return list(segments)
    protected_segments: list[TextSegment] = []
    global_offset = 0
    token_by_start = {token.start: token for token in tokens}
    for segment in segments:
        output: list[str] = []
        for local_index, character in enumerate(segment.text):
            position = global_offset + local_index
            token = token_by_start.get(position)
            if token is not None:
                output.append(token.placeholder)
            if any(item.start <= position < item.end for item in tokens):
                continue
            output.append(character)
        protected_segments.append(segment.model_copy(update={"text": "".join(output)}))
        global_offset += len(segment.text)
    return protected_segments


def _ordinary_docx_run_index(segment: TextSegment) -> int | None:
    """Return the run index only for unambiguous, ordinary Word text runs."""

    if not isinstance(segment.style_id, str) or not segment.style_id:
        return None
    metadata = segment.metadata
    if frozenset(metadata) != _DOCX_RUN_SEGMENT_METADATA_KEYS:
        return None
    run_index = metadata.get("run_index")
    if type(run_index) is not int or run_index < 0:
        return None
    if metadata.get("in_hyperlink") is not False:
        return None
    if metadata.get("in_field") is not False:
        return None
    xpath = metadata.get("xpath")
    if not isinstance(xpath, str) or not xpath:
        return None
    return run_index


def _docx_block_has_only_coalescible_risks(block: TranslationBlock) -> bool:
    risk_flags = block.metadata.get("risk_flags")
    if not isinstance(risk_flags, (list, tuple)):
        return False
    if any(
        not isinstance(risk_flag, str) or risk_flag not in _DOCX_KNOWN_RISK_FLAGS
        for risk_flag in risk_flags
    ):
        return False
    return set(risk_flags) <= _DOCX_COALESCIBLE_RISK_FLAGS


def _coalesce_docx_provider_blocks(
    blocks: Sequence[TranslationBlock],
) -> tuple[
    list[TranslationBlock],
    dict[str, _DocxProviderBlockProjection],
]:
    """Collapse only provably equivalent adjacent Word runs for the provider.

    The returned projections retain every original segment so provider output can
    be expanded back to the exact writeback topology.  Blocks without a proven
    DOCX run mapping are passed through byte-for-byte at the model level.
    """

    provider_blocks: list[TranslationBlock] = []
    projections: dict[str, _DocxProviderBlockProjection] = {}
    for block in blocks:
        part_name = block.metadata.get("part_name")
        if (
            block.protocol != "tagged"
            or not isinstance(part_name, str)
            or not part_name.startswith("word/")
            or not _docx_block_has_only_coalescible_risks(block)
            or len(block.segments) < 2
        ):
            provider_blocks.append(block)
            continue

        run_indexes = [_ordinary_docx_run_index(segment) for segment in block.segments]
        run_index_counts = {
            run_index: run_indexes.count(run_index)
            for run_index in run_indexes
            if run_index is not None
        }
        grouped_segments: list[list[TextSegment]] = [[block.segments[0]]]
        for index, segment in enumerate(block.segments[1:], start=1):
            previous = block.segments[index - 1]
            previous_run_index = run_indexes[index - 1]
            run_index = run_indexes[index]
            can_coalesce = (
                previous_run_index is not None
                and run_index is not None
                and run_index_counts[previous_run_index] == 1
                and run_index_counts[run_index] == 1
                and run_index == previous_run_index + 1
                and segment.style_id == previous.style_id
                and segment.metadata["xpath"] != previous.metadata["xpath"]
            )
            if can_coalesce:
                grouped_segments[-1].append(segment)
            else:
                grouped_segments.append([segment])

        if all(len(group) == 1 for group in grouped_segments):
            provider_blocks.append(block)
            continue

        original_ids = [segment.segment_id for segment in block.segments]
        if any(not segment_id for segment_id in original_ids) or len(
            set(original_ids)
        ) != len(original_ids):
            raise ProviderProtocolError(
                f"DOCX segment mapping for {block.block_id!r} has invalid or "
                "duplicate original segment IDs"
            )
        if "".join(segment.text for segment in block.segments) != block.source_text:
            raise ProviderProtocolError(
                f"DOCX segment mapping for {block.block_id!r} does not cover "
                "the complete block source text"
            )

        groups = tuple(
            _DocxProviderSegmentGroup(
                provider_segment_id=group[0].segment_id,
                original_segments=tuple(group),
            )
            for group in grouped_segments
        )
        provider_segments = [
            group.original_segments[0].model_copy(
                update={
                    "text": "".join(segment.text for segment in group.original_segments)
                }
            )
            for group in groups
        ]
        provider_segment_ids = [segment.segment_id for segment in provider_segments]
        if len(set(provider_segment_ids)) != len(provider_segment_ids):
            raise ProviderProtocolError(
                f"DOCX provider mapping for {block.block_id!r} has duplicate "
                "coalesced segment IDs"
            )

        provider_blocks.append(block.model_copy(update={"segments": provider_segments}))
        projections[block.block_id] = _DocxProviderBlockProjection(
            block_id=block.block_id,
            groups=groups,
        )
    return provider_blocks, projections


def _expand_docx_provider_results(
    results: Sequence[TranslationResult],
    projections: dict[str, _DocxProviderBlockProjection],
) -> list[TranslationResult]:
    """Restore coalesced provider segments to the original Word run topology."""

    expanded_results: list[TranslationResult] = []
    for result in results:
        projection = projections.get(result.block_id)
        if projection is None:
            expanded_results.append(result)
            continue
        if projection.block_id != result.block_id:
            raise ProviderProtocolError(
                f"DOCX provider mapping block mismatch for {result.block_id!r}"
            )
        # String fixture results intentionally have no segment protocol.  Keep
        # their established fixture-only fallback path unchanged.
        if not result.segments and result.provider == "fixture":
            expanded_results.append(result)
            continue

        expected_ids = [group.provider_segment_id for group in projection.groups]
        actual_ids = [segment.segment_id for segment in result.segments]
        if actual_ids != expected_ids:
            raise ProviderProtocolError(
                f"DOCX provider segment mismatch for {result.block_id!r}; "
                f"expected {expected_ids!r}, got {actual_ids!r}"
            )

        expanded_segments: list[TextSegment] = []
        for provider_segment, group in zip(
            result.segments,
            projection.groups,
            strict=True,
        ):
            if (
                not group.original_segments
                or group.original_segments[0].segment_id != group.provider_segment_id
            ):
                raise ProviderProtocolError(
                    f"DOCX provider mapping is inconsistent for {result.block_id!r}"
                )
            for index, original_segment in enumerate(group.original_segments):
                expanded_segments.append(
                    original_segment.model_copy(
                        update={"text": provider_segment.text if index == 0 else ""}
                    )
                )

        original_ids = [
            segment.segment_id
            for group in projection.groups
            for segment in group.original_segments
        ]
        expanded_ids = [segment.segment_id for segment in expanded_segments]
        if expanded_ids != original_ids or len(set(expanded_ids)) != len(expanded_ids):
            raise ProviderProtocolError(
                f"DOCX provider expansion is inconsistent for {result.block_id!r}"
            )
        if "".join(segment.text for segment in expanded_segments) != "".join(
            segment.text for segment in result.segments
        ):
            raise ProviderProtocolError(
                f"DOCX provider expansion changed translated segment text for "
                f"{result.block_id!r}"
            )
        expanded_results.append(
            result.model_copy(update={"segments": expanded_segments})
        )
    return expanded_results


async def _translate_blocks(
    blocks: Sequence[TranslationBlock],
    *,
    provider: TranslationProvider,
    source_language: str,
    target_language: str,
    job_id: str,
    concurrency: int,
    retries: int,
    recovery_budget: _ProviderRecoveryBudget | None = None,
) -> list[TranslationResult]:
    if not blocks:
        return []
    provider_blocks, docx_projections = _coalesce_docx_provider_blocks(blocks)
    probe_batches, remaining_batches = _make_translation_batch_phases(provider_blocks)
    candidate_recovery_budget = (
        recovery_budget
        if recovery_budget is not None
        else _provider_recovery_budget_for_blocks(provider_blocks)
    )
    probe_results = await _run_batches(
        probe_batches,
        provider=provider,
        source_language=source_language,
        target_language=target_language,
        job_id=f"{job_id}-probe",
        concurrency=min(2, concurrency),
        retries=retries,
        recovery_budget=candidate_recovery_budget,
    )
    remaining_results = await _run_batches(
        remaining_batches,
        provider=provider,
        source_language=source_language,
        target_language=target_language,
        job_id=f"{job_id}-main",
        concurrency=concurrency,
        retries=retries,
        recovery_budget=candidate_recovery_budget,
    )
    by_id = {
        result.block_id: result.model_copy(
            update={
                "metadata": {
                    **dict(result.metadata),
                    "source_language": source_language,
                    "target_language": target_language,
                }
            }
        )
        for result in [*probe_results, *remaining_results]
    }
    if set(by_id) != {block.block_id for block in provider_blocks}:
        raise ProviderProtocolError(
            "Combined provider batches did not cover every block"
        )
    ordered_results = [by_id[block.block_id] for block in provider_blocks]
    return _expand_docx_provider_results(ordered_results, docx_projections)


def _make_translation_batch_phases(
    blocks: Sequence[TranslationBlock],
) -> tuple[list[list[TranslationBlock]], list[list[TranslationBlock]]]:
    probe_count = max(1, math.ceil(len(blocks) * 0.10))
    return (
        _make_batches(blocks[:probe_count]),
        _make_batches(blocks[probe_count:]),
    )


def _provider_recovery_budget_for_blocks(
    blocks: Sequence[TranslationBlock],
) -> _ProviderRecoveryBudget:
    probe_batches, remaining_batches = _make_translation_batch_phases(blocks)
    initial_batch_count = len(probe_batches) + len(remaining_batches)
    return _ProviderRecoveryBudget(
        max_calls=_provider_recovery_calls_for_initial_batches(initial_batch_count),
        max_seconds=_provider_recovery_seconds_for_initial_batches(initial_batch_count),
    )


def _make_batches(
    blocks: Sequence[TranslationBlock],
) -> list[list[TranslationBlock]]:
    plain = [block for block in blocks if block.protocol == "plain"]
    tagged = [block for block in blocks if block.protocol == "tagged"]
    return [
        *(
            plain[index : index + PLAIN_BATCH_SIZE]
            for index in range(0, len(plain), PLAIN_BATCH_SIZE)
        ),
        *(
            tagged[index : index + TAGGED_BATCH_SIZE]
            for index in range(0, len(tagged), TAGGED_BATCH_SIZE)
        ),
    ]


async def _gather_fail_fast(
    awaitables: Sequence[Awaitable[Any]],
    *,
    failure_event: asyncio.Event | None = None,
) -> list[Any]:
    """Cancel and await sibling work before propagating the first failure."""

    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    if not tasks:
        return []

    async def cancel_and_wait() -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )
    except BaseException:
        if failure_event is not None:
            failure_event.set()
        await cancel_and_wait()
        raise

    failure: BaseException | None = None
    for task in tasks:
        if task not in done:
            continue
        if task.cancelled():
            failure = asyncio.CancelledError()
            break
        exception = task.exception()
        if exception is not None:
            failure = exception
            break
    if failure is not None:
        if failure_event is not None:
            failure_event.set()
        await cancel_and_wait()
        raise failure
    return [task.result() for task in tasks]


async def _run_batches(
    batches: Sequence[Sequence[TranslationBlock]],
    *,
    provider: TranslationProvider,
    source_language: str,
    target_language: str,
    job_id: str,
    concurrency: int,
    retries: int,
    recovery_budget: _ProviderRecoveryBudget | None = None,
) -> list[TranslationResult]:
    semaphore = asyncio.Semaphore(concurrency)
    candidate_recovery_budget = (
        recovery_budget if recovery_budget is not None else _ProviderRecoveryBudget()
    )
    terminal_failure = asyncio.Event()

    async def invoke(
        batch: Sequence[TranslationBlock],
        *,
        request_path: str,
        attempt: int,
        recovery_call: bool,
    ) -> list[TranslationResult]:
        async def call_provider() -> list[TranslationResult]:
            return await provider.translate(
                batch,
                source_language=source_language,
                target_language=target_language,
                request_id=f"{job_id}-{request_path}-a{attempt}",
            )

        async with semaphore:
            if recovery_call:
                results = await candidate_recovery_budget.run(call_provider)
            else:
                results = await call_provider()
        requested_ids = [block.block_id for block in batch]
        result_ids = [result.block_id for result in results]
        if len(result_ids) != len(set(result_ids)):
            raise ProviderProtocolError("provider returned duplicate block IDs")
        missing = set(requested_ids) - set(result_ids)
        extra = set(result_ids) - set(requested_ids)
        if missing or extra:
            raise ProviderProtocolError(
                f"provider block mismatch; missing={sorted(missing)}, "
                f"extra={sorted(extra)}"
            )
        by_id = {result.block_id: result for result in results}
        return [by_id[block_id] for block_id in requested_ids]

    async def run(
        batch: Sequence[TranslationBlock],
        *,
        request_path: str,
        attempt_limit: int,
        allow_protocol_singleton_retry: bool = True,
        split_depth: int = 0,
    ) -> list[TranslationResult]:
        last_retryable: RetryableProviderError | None = None
        protocol_error: ProviderProtocolError | None = None
        for attempt in range(attempt_limit):
            try:
                return await invoke(
                    batch,
                    request_path=request_path,
                    attempt=attempt,
                    recovery_call=split_depth > 0 or attempt > 0,
                )
            except _ProviderRecoveryDeadlineExceeded:
                raise
            except RetryableProviderError as exc:
                last_retryable = exc
                if attempt + 1 < attempt_limit:
                    await candidate_recovery_budget.sleep(2**attempt)
            except _ProviderRecoveryBudgetExceeded:
                raise
            except ProviderProtocolError as exc:
                protocol_error = exc
                break

        if len(batch) == 1:
            if protocol_error is not None and allow_protocol_singleton_retry:
                await candidate_recovery_budget.begin()
                return await invoke(
                    batch,
                    request_path=f"{request_path}-strict",
                    attempt=0,
                    recovery_call=True,
                )
            if protocol_error is not None:
                raise protocol_error
            assert last_retryable is not None
            raise last_retryable

        if last_retryable is not None and not ADAPTIVE_SPLIT_RETRYABLE_PATTERN.search(
            str(last_retryable)
        ):
            raise last_retryable

        if protocol_error is not None and split_depth >= MAX_PROTOCOL_BATCH_SPLIT_DEPTH:
            raise protocol_error
        if (
            last_retryable is not None
            and split_depth >= MAX_RETRYABLE_BATCH_SPLIT_DEPTH
        ):
            raise last_retryable

        await candidate_recovery_budget.begin()
        midpoint = len(batch) // 2
        split_groups = await _gather_fail_fast(
            [
                run(
                    batch[:midpoint],
                    request_path=f"{request_path}-s0",
                    attempt_limit=1,
                    split_depth=split_depth + 1,
                ),
                run(
                    batch[midpoint:],
                    request_path=f"{request_path}-s1",
                    attempt_limit=1,
                    split_depth=split_depth + 1,
                ),
            ],
            failure_event=terminal_failure,
        )
        return [result for group in split_groups for result in group]

    next_batch_index = 0
    groups_by_index: dict[int, list[TranslationResult]] = {}

    async def worker() -> None:
        nonlocal next_batch_index
        while not terminal_failure.is_set():
            index = next_batch_index
            if index >= len(batches):
                return
            next_batch_index += 1
            try:
                groups_by_index[index] = await run(
                    batches[index],
                    request_path=f"b{index}",
                    attempt_limit=retries + 1,
                )
            except BaseException:
                terminal_failure.set()
                raise

    await _gather_fail_fast(
        [worker() for _ in range(min(max(1, concurrency), len(batches)))],
        failure_event=terminal_failure,
    )
    return [
        result for index in range(len(batches)) for result in groups_by_index[index]
    ]


def _validate_and_restore_results(
    source_blocks: Sequence[TranslationBlock],
    results: Sequence[TranslationResult],
    protected_by_id: dict[str, ProtectedText],
    glossary: dict[str, str],
    *,
    allow_fixture_unprotected: bool,
) -> tuple[list[TranslationResult], list[QaIssue]]:
    original_by_id = {block.block_id: block for block in source_blocks}
    restored_results: list[TranslationResult] = []
    issues: list[QaIssue] = []
    for result in results:
        original = original_by_id[result.block_id]
        protected = protected_by_id[result.block_id]
        token_errors = validate_protected_tokens(
            result.translated_text,
            protected.tokens,
        )
        already_restored = (
            allow_fixture_unprotected
            and token_errors
            and not any(
                token.placeholder in result.translated_text
                for token in protected.tokens
            )
            and not validate_restored_values(protected.tokens, result.translated_text)
        )
        if token_errors and not already_restored:
            issues.append(
                QaIssue(
                    code="PROTECTED_TOKEN_MISMATCH",
                    message="; ".join(token_errors),
                    severity=IssueSeverity.ERROR,
                    block_id=result.block_id,
                    stage="translation_validation",
                )
            )
            continue
        translated_text = (
            result.translated_text
            if already_restored
            else restore_text(result.translated_text, protected.tokens)
        )
        restored_segments = []
        for segment in result.segments:
            segment_text = segment.text
            if not already_restored:
                segment_text = restore_text(
                    segment_text,
                    protected.tokens,
                    strict=False,
                )
            restored_segments.append(segment.model_copy(update={"text": segment_text}))
        warnings = list(result.warnings)
        if original.protocol == "tagged" and restored_segments:
            aligned_segments = _align_tagged_segment_whitespace(
                translated_text,
                restored_segments,
            )
            if aligned_segments is None:
                issues.append(
                    QaIssue(
                        code="TAGGED_SEGMENT_TEXT_MISMATCH",
                        message=(
                            "Tagged segment text differs from the complete "
                            "translation beyond whitespace"
                        ),
                        severity=IssueSeverity.ERROR,
                        block_id=result.block_id,
                        stage="translation_validation",
                    )
                )
                continue
            if aligned_segments != restored_segments:
                restored_segments = aligned_segments
                warnings.append(
                    "Tagged segment whitespace was aligned to translated_text"
                )
        result = result.model_copy(
            update={
                "translated_text": translated_text,
                "segments": restored_segments,
                "warnings": warnings,
            }
        )
        if isinstance(result.provider, str) and result.provider == "fixture":
            result = _fixture_tagged_fallback(original, result)
        if not translated_text.strip():
            issues.append(
                QaIssue(
                    code="EMPTY_TRANSLATION",
                    message="Provider returned an empty translation",
                    severity=IssueSeverity.ERROR,
                    block_id=result.block_id,
                    stage="translation_validation",
                )
            )
            continue
        for message in glossary_issues(original.source_text, translated_text, glossary):
            issues.append(
                QaIssue(
                    code="GLOSSARY_MISMATCH",
                    message=message,
                    severity=IssueSeverity.ERROR,
                    block_id=result.block_id,
                    stage="translation_validation",
                )
            )
        restored_results.append(result)
    if len(restored_results) != len(source_blocks):
        missing = sorted(
            set(original_by_id) - {item.block_id for item in restored_results}
        )
        issues.append(
            QaIssue(
                code="TRANSLATION_SET_INCOMPLETE",
                message=f"Validated translations missing for {len(missing)} blocks",
                severity=IssueSeverity.ERROR,
                stage="translation_validation",
                details={"missing": missing[:50]},
            )
        )
    return restored_results, issues


async def _validate_and_restore_with_strict_retry(
    source_blocks: Sequence[TranslationBlock],
    prepared_blocks: Sequence[TranslationBlock],
    results: Sequence[TranslationResult],
    protected_by_id: dict[str, ProtectedText],
    glossary: dict[str, str],
    *,
    provider: TranslationProvider,
    source_language: str,
    target_language: str,
    job_id: str,
    allow_fixture_unprotected: bool,
    concurrency: int = 4,
    recovery_budget: _ProviderRecoveryBudget | None = None,
) -> tuple[list[TranslationResult], list[QaIssue]]:
    """Validate translations and strictly retry tagged blocks with split text.

    The first validation pass remains authoritative for deciding which blocks are
    unsafe.  Only ``TAGGED_SEGMENT_TEXT_MISMATCH`` blocks are retried as singleton
    requests, with at most ``MAX_STRICT_TAGGED_PROVIDER_ATTEMPTS`` attempts per
    block.  All successful raw results are retained unchanged and the complete set
    is validated again before writeback.
    """

    restored, issues = _validate_and_restore_results(
        source_blocks,
        results,
        protected_by_id,
        glossary,
        allow_fixture_unprotected=allow_fixture_unprotected,
    )
    mismatch_ids = {
        issue.block_id
        for issue in issues
        if issue.code == "TAGGED_SEGMENT_TEXT_MISMATCH" and issue.block_id is not None
    }
    if not mismatch_ids:
        return restored, issues
    if len(mismatch_ids) > MAX_STRICT_TAGGED_RETRY_BLOCKS:
        return restored, issues

    candidate_recovery_budget = (
        recovery_budget if recovery_budget is not None else _ProviderRecoveryBudget()
    )
    await candidate_recovery_budget.begin()
    source_by_id = {block.block_id: block for block in source_blocks}
    prepared_by_id = {block.block_id: block for block in prepared_blocks}
    raw_by_id = {result.block_id: result for result in results}
    block_indexes = {
        block.block_id: index for index, block in enumerate(prepared_blocks)
    }
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def retry_one(block_id: str) -> TranslationResult:
        prepared = prepared_by_id[block_id]
        if prepared.protocol != "tagged":
            raise ProviderProtocolError(
                f"strict tagged retry requested for non-tagged block {block_id!r}"
            )
        retry_context = dict(prepared.context)
        retry_context["translation_retry"] = {
            "mode": "strict_single_block",
            "reason": "TAGGED_SEGMENT_TEXT_MISMATCH",
        }
        retry_block = prepared.model_copy(update={"context": retry_context})
        provider_retry_blocks, docx_projections = _coalesce_docx_provider_blocks(
            [retry_block]
        )
        if len(provider_retry_blocks) != 1:
            raise ProviderProtocolError(
                f"strict retry mapping did not retain block {block_id!r}"
            )
        provider_retry_block = provider_retry_blocks[0]
        expected_segment_ids = [
            segment.segment_id for segment in provider_retry_block.segments
        ]
        for attempt in range(MAX_STRICT_TAGGED_PROVIDER_ATTEMPTS):
            request_id = f"{job_id}-strict-b{block_indexes[block_id]}-a{attempt}"

            async def call_provider() -> list[TranslationResult]:
                return await provider.translate(
                    [provider_retry_block],
                    source_language=source_language,
                    target_language=target_language,
                    request_id=request_id,
                )

            try:
                async with semaphore:
                    retry_results = await candidate_recovery_budget.run(call_provider)
            except _ProviderRecoveryBudgetExceeded:
                raise
            except ProviderProtocolError as exc:
                protocol_error = exc
            else:
                actual_block_ids = [result.block_id for result in retry_results]
                if actual_block_ids != [block_id]:
                    protocol_error = ProviderProtocolError(
                        f"strict retry block mismatch for {block_id!r}; got "
                        f"{actual_block_ids!r}"
                    )
                else:
                    retry_result = retry_results[0]
                    actual_segment_ids = [
                        segment.segment_id for segment in retry_result.segments
                    ]
                    if actual_segment_ids == expected_segment_ids:
                        try:
                            expanded_retry_result = _expand_docx_provider_results(
                                [retry_result],
                                docx_projections,
                            )[0]
                        except ProviderProtocolError as exc:
                            protocol_error = exc
                        else:
                            _, attempt_issues = _validate_and_restore_results(
                                [source_by_id[block_id]],
                                [expanded_retry_result],
                                {block_id: protected_by_id[block_id]},
                                glossary,
                                allow_fixture_unprotected=allow_fixture_unprotected,
                            )
                            tagged_text_mismatch = next(
                                (
                                    issue
                                    for issue in attempt_issues
                                    if issue.code == "TAGGED_SEGMENT_TEXT_MISMATCH"
                                ),
                                None,
                            )
                            if tagged_text_mismatch is not None:
                                protocol_error = ProviderProtocolError(
                                    f"strict retry tagged text mismatch for "
                                    f"{block_id!r}: {tagged_text_mismatch.message}"
                                )
                            else:
                                return expanded_retry_result.model_copy(
                                    update={
                                        "metadata": {
                                            **dict(expanded_retry_result.metadata),
                                            "source_language": source_language,
                                            "target_language": target_language,
                                            "pipeline_recovery": {
                                                "mode": "strict_single_block",
                                                "reason": (
                                                    "TAGGED_SEGMENT_TEXT_MISMATCH"
                                                ),
                                                "request_id": request_id,
                                            },
                                        }
                                    }
                                )
                    else:
                        protocol_error = ProviderProtocolError(
                            f"strict retry segment mismatch for {block_id!r}; expected "
                            f"{expected_segment_ids!r}, got {actual_segment_ids!r}"
                        )
            if attempt + 1 == MAX_STRICT_TAGGED_PROVIDER_ATTEMPTS:
                raise protocol_error

        raise AssertionError("strict tagged provider attempt loop did not terminate")

    ordered_mismatch_ids = [
        block.block_id for block in prepared_blocks if block.block_id in mismatch_ids
    ]
    replacements = await _gather_fail_fast(
        [retry_one(block_id) for block_id in ordered_mismatch_ids]
    )
    raw_by_id.update(
        {replacement.block_id: replacement for replacement in replacements}
    )
    merged_results = [raw_by_id[block.block_id] for block in source_blocks]
    return _validate_and_restore_results(
        source_blocks,
        merged_results,
        protected_by_id,
        glossary,
        allow_fixture_unprotected=allow_fixture_unprotected,
    )


def _align_tagged_segment_whitespace(
    translated_text: str,
    segments: Sequence[TextSegment],
) -> list[TextSegment] | None:
    """Make tagged runs concatenate to the provider's complete translation.

    Models occasionally omit a boundary space from their per-run output while
    retaining it in ``translated_text``.  The complete translation is the
    canonical wording, while the segment boundaries carry formatting.  It is
    safe to reconcile the two only when their non-whitespace characters are
    identical; any semantic difference remains a blocking validation error.
    """

    segment_text = "".join(segment.text for segment in segments)
    if segment_text == translated_text:
        return list(segments)

    without_whitespace = lambda value: "".join(  # noqa: E731
        character for character in value if not character.isspace()
    )
    if without_whitespace(segment_text) != without_whitespace(translated_text):
        return None

    non_whitespace_counts = [
        sum(not character.isspace() for character in segment.text)
        for segment in segments
    ]
    boundaries: list[int] = []
    wanted_count = 0
    found_count = 0
    cursor = 0
    for count in non_whitespace_counts[:-1]:
        wanted_count += count
        while cursor < len(translated_text) and found_count < wanted_count:
            if not translated_text[cursor].isspace():
                found_count += 1
            cursor += 1
        boundaries.append(cursor)

    aligned: list[TextSegment] = []
    start = 0
    for segment, end in zip(
        segments,
        [*boundaries, len(translated_text)],
        strict=True,
    ):
        aligned.append(segment.model_copy(update={"text": translated_text[start:end]}))
        start = end
    return aligned


def _fixture_tagged_fallback(
    block: TranslationBlock,
    result: TranslationResult,
) -> TranslationResult:
    if block.protocol != "tagged" or result.segments:
        return result
    source_segments = block.segments
    if not source_segments:
        return result
    lengths = [max(1, len(segment.text)) for segment in source_segments]
    total = sum(lengths)
    cursor = 0
    translated = result.translated_text
    segments: list[TextSegment] = []
    cumulative = 0
    for index, (source_segment, length) in enumerate(zip(source_segments, lengths)):
        cumulative += length
        boundary = (
            len(translated)
            if index == len(source_segments) - 1
            else round(len(translated) * cumulative / total)
        )
        segments.append(
            TextSegment(
                segment_id=source_segment.segment_id,
                text=translated[cursor:boundary],
                style_id=source_segment.style_id,
                metadata=dict(source_segment.metadata),
            )
        )
        cursor = boundary
    return result.model_copy(
        update={
            "segments": segments,
            "warnings": [
                *result.warnings,
                "fixture tagged segments were synthesized proportionally",
            ],
        }
    )


class _SemanticQaLogicalCallBudgetExceeded(RuntimeError):
    pass


class _SemanticQaRecoveryCallBudgetExceeded(RuntimeError):
    pass


async def _semantic_qa(
    blocks: Sequence[TranslationBlock],
    results: Sequence[TranslationResult],
    provider: ProcessingProvider,
    job_id: str,
    concurrency: int,
    *,
    audit_metadata: dict[str, Any] | None = None,
) -> list[QaIssue]:
    semaphore = asyncio.Semaphore(concurrency)
    source_by_id = {block.block_id: block for block in blocks}
    provider_unavailable = asyncio.Event()
    provider_attempts = 0
    initial_provider_attempts = 0
    recovery_provider_attempts = 0
    http_attempts = 0
    http_attempts_reported_calls = 0
    protocol_retry_attempts = 0
    retryable_retry_attempts = 0
    protocol_failure_streak = 0
    checked_indices: set[int] = set()
    protocol_failure_indices: set[int] = set()
    failed_provider_types: dict[int, str] = {}
    issue_groups: dict[int, list[QaIssue]] = {}
    stop_reason: str | None = None
    secondary_stop_reasons: list[str] = []
    logical_call_limit = len(results) + MAX_SEMANTIC_QA_EXTRA_LOGICAL_CALLS
    budget_lock = asyncio.Lock()
    stop_lock = asyncio.Lock()
    provider_call_attempts: list[dict[str, object]] = []

    def _valid_attempt_count(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def result_http_attempts(qa_result: Any) -> int | None:
        metadata = qa_result.metadata
        direct = metadata.get("http_attempts")
        if _valid_attempt_count(direct):
            return direct
        events = metadata.get("audit_events")
        if not isinstance(events, list):
            return None
        reported = 0
        found = False
        for event in events:
            if not isinstance(event, dict):
                continue
            details = event.get("details")
            if not isinstance(details, dict):
                continue
            value = details.get("http_attempts")
            if _valid_attempt_count(value):
                reported += value
                found = True
        return reported if found else None

    def record_http_attempts(value: object) -> None:
        nonlocal http_attempts, http_attempts_reported_calls
        if _valid_attempt_count(value):
            http_attempts += value
            http_attempts_reported_calls += 1

    async def stop_candidate(reason: str) -> None:
        nonlocal stop_reason
        async with stop_lock:
            if stop_reason is None:
                stop_reason = reason
            elif reason != stop_reason and reason not in secondary_stop_reasons:
                secondary_stop_reasons.append(reason)
            provider_unavailable.set()

    async def invoke(
        result: TranslationResult,
        request_id: str,
        *,
        parent_request_id: str | None = None,
        recovery_kind: str | None = None,
    ) -> list[QaIssue]:
        nonlocal initial_provider_attempts, protocol_retry_attempts
        nonlocal provider_attempts, recovery_provider_attempts
        nonlocal retryable_retry_attempts
        async with budget_lock:
            if (
                recovery_kind is not None
                and recovery_provider_attempts >= MAX_SEMANTIC_QA_EXTRA_LOGICAL_CALLS
            ):
                raise _SemanticQaRecoveryCallBudgetExceeded
            if provider_attempts >= logical_call_limit:
                raise _SemanticQaLogicalCallBudgetExceeded
            if recovery_kind is None:
                initial_provider_attempts += 1
            else:
                recovery_provider_attempts += 1
                if recovery_kind == "protocol":
                    protocol_retry_attempts += 1
                else:
                    retryable_retry_attempts += 1
            provider_attempts += 1
            stable_parent_request_id = parent_request_id or request_id
            provider_call_attempts.append(
                {
                    "request_id": request_id,
                    "parent_request_id": stable_parent_request_id,
                    "attempt": 1 if recovery_kind is None else 2,
                    "attempt_kind": (
                        "initial" if recovery_kind is None else f"{recovery_kind}_retry"
                    ),
                }
            )
        try:
            qa_result = await provider.semantic_qa(
                source=source_by_id[result.block_id],
                translation=result,
                request_id=request_id,
            )
        except ProviderError as exc:
            record_http_attempts(getattr(exc, "http_attempts", None))
            raise
        record_http_attempts(result_http_attempts(qa_result))
        if qa_result.block_id != result.block_id:
            raise ProviderProtocolError(
                "semantic QA result block ID does not match the requested block"
            )
        issues: list[QaIssue] = []
        for issue in qa_result.issues:
            if issue.block_id is None:
                issues.append(issue.model_copy(update={"block_id": result.block_id}))
            elif issue.block_id != result.block_id:
                raise ProviderProtocolError(
                    "semantic QA issue block ID does not match the requested block"
                )
            else:
                issues.append(issue)
        if not qa_result.passed and not any(
            issue.severity == IssueSeverity.ERROR for issue in issues
        ):
            # Enforce the normalized semantic contract at the orchestration
            # boundary too: third-party/legacy providers must not turn an
            # explicit failed verdict into a successful candidate merely
            # because their issue list is empty or warning-only.
            issues.append(
                QaIssue(
                    issue_id=f"{request_id}-provider-review-required",
                    code="SEMANTIC_QA_REVIEW_REQUIRED",
                    message=(
                        "Semantic QA marked this block as failed but returned no "
                        "error-severity issue; the block requires review"
                    ),
                    severity=IssueSeverity.ERROR,
                    recoverable=True,
                    stage="semantic_qa",
                    block_id=result.block_id,
                    details={
                        "status": "provider_output_inconsistent",
                        "provider_passed": False,
                        "provider_issue_count": len(qa_result.issues),
                    },
                )
            )
        return issues

    async def check(index: int, result: TranslationResult) -> str:
        nonlocal protocol_retry_attempts, retryable_retry_attempts, stop_reason
        async with semaphore:
            if provider_unavailable.is_set():
                return "stopped"
            parent_request_id = f"{job_id}-qa-{result.block_id}"
            try:
                issues = await invoke(result, parent_request_id)
            except _SemanticQaLogicalCallBudgetExceeded:
                await stop_candidate("logical_call_budget_exhausted")
                return "stopped"
            except ProviderProtocolError:
                # A malformed or internally inconsistent model response is
                # block-scoped. Retry that block once, then continue checking
                # the remaining candidate instead of declaring the provider
                # globally unavailable.
                await asyncio.sleep(0.25)
                if provider_unavailable.is_set():
                    return "stopped"
                try:
                    issues = await invoke(
                        result,
                        f"{parent_request_id}-protocol-r1",
                        parent_request_id=parent_request_id,
                        recovery_kind="protocol",
                    )
                except _SemanticQaRecoveryCallBudgetExceeded:
                    await stop_candidate("recovery_call_budget_exhausted")
                    return "stopped"
                except _SemanticQaLogicalCallBudgetExceeded:
                    await stop_candidate("logical_call_budget_exhausted")
                    return "stopped"
                except ProviderProtocolError as retry_error:
                    failed_provider_types[index] = type(retry_error).__name__
                    protocol_failure_indices.add(index)
                    return "protocol_failure"
                except RetryableProviderError as retry_error:
                    failed_provider_types[index] = type(retry_error).__name__
                    await stop_candidate("provider_retryable_error_exhausted")
                    return "provider_failure"
                except ProviderError as retry_error:
                    failed_provider_types[index] = type(retry_error).__name__
                    await stop_candidate("provider_nonretryable_error")
                    return "provider_failure"
            except RetryableProviderError as exhausted_error:
                if getattr(provider, "manages_semantic_qa_retries", False):
                    # This provider explicitly owns its bounded retry/deadline
                    # policy, so another orchestration retry would multiply its
                    # HTTP attempts and latency.
                    failed_provider_types[index] = type(exhausted_error).__name__
                    await stop_candidate("provider_retryable_error_exhausted")
                    return "provider_failure"
                # Generic and legacy providers get one orchestration-owned
                # retry. The candidate budget added below bounds this recovery
                # across all blocks rather than granting one unbounded retry
                # loop per provider implementation.
                await asyncio.sleep(0.25)
                if provider_unavailable.is_set():
                    return "stopped"
                try:
                    issues = await invoke(
                        result,
                        f"{parent_request_id}-retryable-r1",
                        parent_request_id=parent_request_id,
                        recovery_kind="retryable",
                    )
                except _SemanticQaRecoveryCallBudgetExceeded:
                    await stop_candidate("recovery_call_budget_exhausted")
                    return "stopped"
                except _SemanticQaLogicalCallBudgetExceeded:
                    await stop_candidate("logical_call_budget_exhausted")
                    return "stopped"
                except ProviderProtocolError as retry_error:
                    failed_provider_types[index] = type(retry_error).__name__
                    protocol_failure_indices.add(index)
                    return "protocol_failure"
                except RetryableProviderError as retry_error:
                    failed_provider_types[index] = type(retry_error).__name__
                    await stop_candidate("provider_retryable_error_exhausted")
                    return "provider_failure"
                except ProviderError as retry_error:
                    failed_provider_types[index] = type(retry_error).__name__
                    await stop_candidate("provider_nonretryable_error")
                    return "provider_failure"
            except ProviderError as provider_error:
                # Authentication and other non-retryable provider failures are
                # candidate-wide until configuration or upstream state changes.
                failed_provider_types[index] = type(provider_error).__name__
                await stop_candidate("provider_nonretryable_error")
                return "provider_failure"
            checked_indices.add(index)
            issue_groups[index] = issues
            return "checked"

    async def run_candidate_checks() -> None:
        nonlocal protocol_failure_streak, stop_reason
        # Keep probing serially while the provider returns persistent malformed
        # responses. This distinguishes a block-scoped response defect from a
        # systematic protocol outage before the candidate is fanned out.
        next_index = 0
        while next_index < len(results) and not provider_unavailable.is_set():
            outcome = await check(next_index, results[next_index])
            next_index += 1
            if outcome == "protocol_failure":
                protocol_failure_streak += 1
                if protocol_failure_streak >= SEMANTIC_QA_PROTOCOL_OUTAGE_THRESHOLD:
                    await stop_candidate("systematic_protocol_error")
                    break
                continue
            if outcome == "checked":
                protocol_failure_streak = 0
            break
        remaining_indices = list(range(next_index, len(results)))
        if provider_unavailable.is_set() or not remaining_indices:
            return

        queue: asyncio.Queue[int] = asyncio.Queue()
        for index in remaining_indices:
            queue.put_nowait(index)
        outcome_lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal protocol_failure_streak, stop_reason
            while not provider_unavailable.is_set():
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                outcome = await check(index, results[index])
                async with outcome_lock:
                    if outcome == "protocol_failure":
                        protocol_failure_streak += 1
                        if (
                            protocol_failure_streak
                            >= SEMANTIC_QA_PROTOCOL_OUTAGE_THRESHOLD
                        ):
                            await stop_candidate("systematic_protocol_error")
                    elif outcome == "checked":
                        protocol_failure_streak = 0

        worker_count = min(concurrency, len(remaining_indices))
        await asyncio.gather(*(worker() for _ in range(worker_count)))

    candidate_timeout = asyncio.timeout(MAX_SEMANTIC_QA_SECONDS)
    try:
        async with candidate_timeout:
            await run_candidate_checks()
    except TimeoutError:
        if not candidate_timeout.expired():
            raise
        await stop_candidate("wall_clock_budget_exhausted")
    issues = [issue for index in sorted(issue_groups) for issue in issue_groups[index]]
    unchecked_indices = set(range(len(results))) - checked_indices
    if unchecked_indices and stop_reason is None and protocol_failure_indices:
        await stop_candidate("provider_protocol_error")
    http_attempts_reporting_complete = http_attempts_reported_calls == provider_attempts
    if unchecked_indices:
        unchecked_block_ids = [
            results[index].block_id for index in sorted(unchecked_indices)
        ]
        issues.append(
            QaIssue(
                issue_id=f"{job_id}-qa-provider-failure",
                code="SEMANTIC_QA_PROVIDER_FAILED",
                message=(
                    "Semantic QA was unavailable after a bounded provider retry; "
                    f"{len(unchecked_indices)} of {len(results)} blocks were not "
                    "verified"
                ),
                severity=IssueSeverity.ERROR,
                stage="semantic_qa",
                recoverable=True,
                details={
                    "status": "qa_unavailable",
                    "category": "infrastructure",
                    "attempts": provider_attempts,
                    "retry_attempts": (
                        protocol_retry_attempts + retryable_retry_attempts
                    ),
                    "retryable_retry_attempts": retryable_retry_attempts,
                    "provider_calls": provider_attempts,
                    "unique_request_ids": len(
                        {call["request_id"] for call in provider_call_attempts}
                    ),
                    "initial_provider_calls": initial_provider_attempts,
                    "recovery_provider_calls": recovery_provider_attempts,
                    "recovery_call_limit": MAX_SEMANTIC_QA_EXTRA_LOGICAL_CALLS,
                    "http_attempts": http_attempts,
                    "http_attempts_reporting_complete": (
                        http_attempts_reporting_complete
                    ),
                    "unreported_http_attempt_calls": (
                        provider_attempts - http_attempts_reported_calls
                    ),
                    "logical_call_limit": logical_call_limit,
                    "wall_clock_limit_seconds": MAX_SEMANTIC_QA_SECONDS,
                    "stop_reason": stop_reason,
                    "secondary_stop_reasons": list(secondary_stop_reasons),
                    "total_blocks": len(results),
                    "checked_blocks": len(checked_indices),
                    "unchecked_blocks": len(unchecked_indices),
                    "unchecked_block_ids": unchecked_block_ids,
                    "provider_error_types": sorted(set(failed_provider_types.values())),
                },
            )
        )
    if audit_metadata is not None:
        audit_metadata.update(
            {
                "logical_provider_calls": provider_attempts,
                "provider_call_attempts": [
                    dict(call) for call in provider_call_attempts
                ],
                "initial_provider_calls": initial_provider_attempts,
                "recovery_provider_calls": recovery_provider_attempts,
                "recovery_call_limit": MAX_SEMANTIC_QA_EXTRA_LOGICAL_CALLS,
                "logical_call_limit": logical_call_limit,
                "http_attempts": http_attempts,
                "http_attempts_reporting_complete": (http_attempts_reporting_complete),
                "unreported_http_attempt_calls": (
                    provider_attempts - http_attempts_reported_calls
                ),
                "protocol_retry_attempts": protocol_retry_attempts,
                "retryable_retry_attempts": retryable_retry_attempts,
                "checked_blocks": len(checked_indices),
                "unchecked_blocks": len(unchecked_indices),
                "stop_reason": stop_reason,
                "secondary_stop_reasons": list(secondary_stop_reasons),
                "wall_clock_limit_seconds": MAX_SEMANTIC_QA_SECONDS,
            }
        )
    return issues


def _pptx_text_fit_candidates(
    source_blocks: Sequence[TranslationBlock],
    result_by_id: dict[str, TranslationResult],
    issues: Sequence[QaIssue],
) -> list[tuple[TranslationBlock, TranslationResult, list[QaIssue]]]:
    """Select editable slide paragraphs implicated by rendered-layout issues."""

    targeted_issues: dict[tuple[int, str], list[QaIssue]] = {}
    for issue in issues:
        if issue.code not in PPTX_TEXT_FIT_ISSUE_CODES or issue.slide is None:
            continue
        shape_id = str(issue.details.get("shape_id") or "").strip()
        if not shape_id:
            continue
        targeted_issues.setdefault((issue.slide, shape_id), []).append(issue)

    blocks_by_target: dict[
        tuple[int, str],
        list[tuple[TranslationBlock, TranslationResult]],
    ] = {}
    for block in source_blocks:
        result = result_by_id.get(block.block_id)
        if result is None or not result.translated_text.strip():
            continue
        context = block.context
        if context.get("document_type") != "pptx":
            continue
        if context.get("content_scope") in {"chart", "table"}:
            continue
        if context.get("shape_type") in {"chart", "graphic_frame", "table"}:
            continue
        try:
            slide = int(context.get("slide_number"))
        except (TypeError, ValueError):
            continue
        shape_id = str(context.get("shape_id") or "").strip()
        target = (slide, shape_id)
        if target in targeted_issues:
            blocks_by_target.setdefault(target, []).append((block, result))

    issues_by_block: dict[str, list[QaIssue]] = {}
    block_and_result_by_id: dict[str, tuple[TranslationBlock, TranslationResult]] = {}
    for target, related_issues in targeted_issues.items():
        target_blocks = blocks_by_target.get(target, [])
        for issue in related_issues:
            selected = target_blocks
            if issue.code == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP":
                overlap_texts = _overlap_issue_texts(issue)
                matched = [
                    pair
                    for pair in target_blocks
                    if any(
                        _rendered_text_matches(
                            pair[1].translated_text,
                            overlap_text,
                        )
                        for overlap_text in overlap_texts
                    )
                ]
                # Rendered lines normally identify one paragraph.  Retain the
                # whole-shape fallback for providers that omit line text.
                if matched:
                    selected = matched
            for block, result in selected:
                block_and_result_by_id[block.block_id] = (block, result)
                issues_by_block.setdefault(block.block_id, []).append(issue)

    candidates = [
        (*block_and_result_by_id[block_id], related_issues)
        for block_id, related_issues in issues_by_block.items()
    ]

    issue_priority = {
        "PPTX_RENDERED_TEXT_OVERFLOW": 0,
        "PPTX_RENDERED_TEXT_OBJECT_OVERLAP": 1,
    }
    candidates.sort(
        key=lambda candidate: (
            min(issue_priority[issue.code] for issue in candidate[2]),
            int(candidate[0].context.get("slide_number", 0)),
            str(candidate[0].context.get("shape_id", "")),
            int(candidate[0].context.get("paragraph_index") or 0),
            candidate[0].block_id,
        )
    )
    return candidates


def _overlap_issue_texts(issue: QaIssue) -> list[str]:
    overlaps = issue.details.get("overlaps")
    if not isinstance(overlaps, list):
        return []
    return [
        str(overlap.get("text") or "")
        for overlap in overlaps
        if isinstance(overlap, dict) and str(overlap.get("text") or "").strip()
    ]


def _normalized_rendered_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and unicodedata.category(character) != "Cf"
    )


def _rendered_text_matches(translation: str, rendered_line: str) -> bool:
    normalized_translation = _normalized_rendered_text(translation)
    normalized_line = _normalized_rendered_text(rendered_line)
    if not normalized_translation or not normalized_line:
        return False
    if normalized_translation == normalized_line:
        return True
    return min(len(normalized_translation), len(normalized_line)) >= 4 and (
        normalized_translation in normalized_line
        or normalized_line in normalized_translation
    )


def _overlap_fit_length_ratio(issue: QaIssue) -> float:
    ratio = 0.85
    overlaps = issue.details.get("overlaps")
    if not isinstance(overlaps, list):
        return ratio
    for overlap in overlaps:
        if not isinstance(overlap, dict):
            continue
        line_bbox = overlap.get("line_bbox_pt")
        if not isinstance(line_bbox, (list, tuple)) or len(line_bbox) < 3:
            continue
        try:
            line_width = float(line_bbox[2])
            intersection_width = float(overlap.get("intersection_width_pt", 0.0))
        except (TypeError, ValueError):
            continue
        if line_width <= 0.0 or intersection_width <= 0.0:
            continue
        visible_ratio = 1.0 - min(1.0, intersection_width / line_width)
        ratio = min(ratio, max(0.50, visible_ratio - 0.03))
    return ratio


def _text_fit_max_length(
    source: TranslationBlock,
    translation: TranslationResult,
    related_issues: Sequence[QaIssue],
    protected: ProtectedText | None = None,
) -> int:
    if not related_issues:
        return max(10, math.ceil(len(source.source_text) * 1.05))

    current_length = len(translation.translated_text)
    if current_length <= 1:
        return 1
    length_ratio = 0.95
    for issue in related_issues:
        if issue.code == "PPTX_RENDERED_TEXT_OVERFLOW":
            try:
                rendered_load_ratio = float(
                    issue.details.get("rendered_load_ratio", 1.0)
                )
            except (TypeError, ValueError):
                rendered_load_ratio = 1.0
            # Leave a small safety margin so a 1.019 rendered overflow still
            # asks the provider for a meaningfully shorter Chinese wording.
            length_ratio = min(
                length_ratio,
                0.97 / max(1.0, rendered_load_ratio),
            )
        elif issue.code == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP":
            length_ratio = min(length_ratio, _overlap_fit_length_ratio(issue))
    target_length = max(
        1,
        min(current_length - 1, math.floor(current_length * length_ratio)),
    )
    if protected is not None:
        protected_length = sum(len(token.value) for token in protected.tokens)
        target_length = max(target_length, protected_length)
    return min(current_length, target_length)


def _text_fit_overrun_from_exception(
    error: Exception,
    requested_max_length: int,
) -> int | None:
    if not isinstance(error, ProviderProtocolError):
        return None
    match = TEXT_FIT_LENGTH_ERROR_PATTERN.fullmatch(str(error).strip())
    if match is None:
        return None
    actual_length = int(match.group("actual"))
    reported_maximum = int(match.group("maximum"))
    if reported_maximum != requested_max_length or actual_length <= reported_maximum:
        return None
    return actual_length


def _text_fit_retry_max_length(
    requested_max_length: int,
    actual_length: int,
    protected: ProtectedText,
) -> int:
    overrun = max(1, actual_length - requested_max_length)
    tighter_limit = min(
        requested_max_length - overrun,
        math.floor(requested_max_length * 0.95),
    )
    protected_length = sum(len(token.value) for token in protected.tokens)
    return max(1, protected_length, tighter_limit)


def _fitted_translation_result(
    source: TranslationBlock,
    translation: TranslationResult,
    fitted_text: str,
    protected: ProtectedText,
    *,
    warnings: Sequence[str] = (),
) -> TranslationResult:
    segments: list[TextSegment] = []
    if source.protocol == "tagged":
        templates = list(translation.segments or source.segments)
        expected_ids = [segment.segment_id for segment in source.segments]
        if (
            not templates
            or [segment.segment_id for segment in templates] != expected_ids
        ):
            raise ValueError("tagged text-fit could not preserve extracted run ids")
        weights = [max(1, len(segment.text)) for segment in templates]
        boundaries = _proportional_text_boundaries(fitted_text, weights)
        protected_values = [token.value for token in protected.tokens]
        boundaries = _avoid_protected_value_splits(
            fitted_text,
            boundaries,
            protected_values,
        )
        start = 0
        for template, end in zip(
            templates,
            [*boundaries, len(fitted_text)],
            strict=True,
        ):
            segments.append(
                template.model_copy(update={"text": fitted_text[start:end]})
            )
            start = end
    return translation.model_copy(
        update={
            "translated_text": fitted_text,
            "segments": segments,
            "warnings": [*translation.warnings, *warnings],
        }
    )


def _proportional_text_boundaries(text: str, weights: Sequence[int]) -> list[int]:
    total = sum(weights)
    cumulative = 0
    boundaries: list[int] = []
    for weight in weights[:-1]:
        cumulative += weight
        boundaries.append(round(len(text) * cumulative / total))
    return boundaries


def _avoid_protected_value_splits(
    text: str,
    boundaries: Sequence[int],
    protected_values: Sequence[str],
) -> list[int]:
    spans: list[tuple[int, int]] = []
    for value in protected_values:
        if not value:
            continue
        cursor = 0
        while True:
            start = text.find(value, cursor)
            if start < 0:
                break
            spans.append((start, start + len(value)))
            cursor = start + len(value)

    adjusted: list[int] = []
    previous = 0
    for wanted in boundaries:
        boundary = max(previous, min(len(text), wanted))
        for start, end in spans:
            if start < boundary < end:
                choices = [value for value in (start, end) if value >= previous]
                if choices:
                    boundary = min(choices, key=lambda value: abs(value - wanted))
                break
        adjusted.append(boundary)
        previous = boundary
    return adjusted


def _layout_issue_score(issues: Sequence[QaIssue]) -> float:
    """Weight severity first, then rendered overflow/overlap magnitude."""

    score = 0.0
    for issue in issues:
        if issue.severity == IssueSeverity.ERROR:
            score += 1_000_000_000.0
        elif issue.severity == IssueSeverity.WARNING:
            score += 1_000_000.0
        else:
            continue

        details = issue.details
        if issue.code == "PPTX_RENDERED_TEXT_OVERFLOW":
            load_excess = max(
                0.0,
                _float_detail(details, "rendered_load_ratio") - 1.0,
            )
            width_ratio = _normalized_excess(
                details,
                "overflow_width_pt",
                "available_width_pt",
            )
            height_ratio = _normalized_excess(
                details,
                "overflow_height_pt",
                "available_height_pt",
            )
            score += 1_000.0 + 10_000.0 * max(
                load_excess,
                width_ratio,
                height_ratio,
            )
        elif issue.code == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP":
            overlap_area = max(
                0.0,
                _float_detail(details, "candidate_overlap_area_pt2"),
            )
            reference_area = max(
                0.0,
                _float_detail(details, "reference_overlap_area_pt2"),
            )
            tolerance = max(
                1.0,
                _float_detail(details, "worsening_tolerance_pt2"),
            )
            excess_area = max(0.0, overlap_area - reference_area)
            score += 1_000.0 + 10_000.0 * math.log1p(excess_area / tolerance)
    return score


def _float_detail(details: dict[str, Any], key: str) -> float:
    try:
        return float(details.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalized_excess(
    details: dict[str, Any],
    excess_key: str,
    available_key: str,
) -> float:
    excess = max(0.0, _float_detail(details, excess_key))
    available = _float_detail(details, available_key)
    return excess / available if available > 0.0 else 0.0


def _layout_error_identities(issues: Sequence[QaIssue]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            issue.code,
            issue.slide or 0,
            str(issue.details.get("shape_id") or ""),
            issue.block_id or "",
        )
        for issue in issues
        if issue.severity == IssueSeverity.ERROR
    )


async def _attempt_text_fit(
    *,
    source_path: Path,
    source_blocks: Sequence[TranslationBlock],
    results: Sequence[TranslationResult],
    protected_by_id: dict[str, ProtectedText],
    processing_provider: ProcessingProvider,
    engine: Any,
    work: Path,
    job_id: str,
    rounds: int,
    concurrency: int,
    layout_issues: Sequence[QaIssue] | None = None,
) -> tuple[
    Path,
    list[TranslationResult],
    list[QaIssue],
    list[QaIssue],
]:
    source_by_id = {block.block_id: block for block in source_blocks}
    result_by_id = {result.block_id: result for result in results}
    suffix = source_path.suffix.lower()
    current_document = work / f"translated-r0{suffix}"
    current_issues = (
        list(layout_issues)
        if layout_issues is not None
        else _run_office_qa(
            source_path,
            current_document,
            blocks=source_blocks,
            translations=results,
        )
    )
    fit_issues: list[QaIssue] = []
    semaphore = asyncio.Semaphore(concurrency)

    for round_number in range(1, rounds + 1):
        if suffix == ".pptx":
            candidates = _pptx_text_fit_candidates(
                source_blocks,
                result_by_id,
                current_issues,
            )
        else:
            candidates = [
                (source_by_id[result.block_id], result, [])
                for result in result_by_id.values()
                if source_by_id[result.block_id].protocol == "plain"
                and len(result.translated_text) > 50
                and len(result.translated_text)
                > math.ceil(len(source_by_id[result.block_id].source_text) * 1.05)
            ]
            candidates.sort(
                key=lambda candidate: (
                    len(candidate[1].translated_text)
                    / max(1, len(candidate[0].source_text))
                ),
                reverse=True,
            )
            candidates = candidates[:20]
        if not candidates:
            break

        async def fit_one(
            source: TranslationBlock,
            translation: TranslationResult,
            related_issues: Sequence[QaIssue],
        ) -> tuple[str, TranslationResult | None, str | None]:
            async with semaphore:
                protected = protected_by_id[source.block_id]
                requested_max_length = _text_fit_max_length(
                    source,
                    translation,
                    related_issues,
                    protected,
                )
                for attempt in range(2):
                    length_error_message: str
                    try:
                        fitted = await processing_provider.fit_text(
                            source=source,
                            translation=translation,
                            max_length=requested_max_length,
                            request_id=(
                                f"{job_id}-fit-r{round_number}-"
                                f"{source.block_id}-a{attempt + 1}"
                            ),
                        )
                    except Exception as exc:
                        actual_length = _text_fit_overrun_from_exception(
                            exc,
                            requested_max_length,
                        )
                        if actual_length is None or attempt == 1:
                            return source.block_id, None, str(exc)
                        length_error_message = str(exc)
                    else:
                        actual_length = len(fitted.fitted_text)
                        if actual_length <= requested_max_length:
                            break
                        length_error_message = (
                            f"fitted text has {actual_length} characters; "
                            f"maximum is {requested_max_length}"
                        )
                        if attempt == 1:
                            return source.block_id, None, length_error_message

                    retry_max_length = _text_fit_retry_max_length(
                        requested_max_length,
                        actual_length,
                        protected,
                    )
                    if retry_max_length >= requested_max_length:
                        return source.block_id, None, length_error_message
                    requested_max_length = retry_max_length
                else:
                    raise AssertionError("text-fit retry loop did not terminate")
                protected_errors = validate_restored_values(
                    protected.tokens,
                    fitted.fitted_text,
                )
                if protected_errors:
                    return source.block_id, None, "; ".join(protected_errors)
                try:
                    fitted_result = _fitted_translation_result(
                        source,
                        translation,
                        fitted.fitted_text,
                        protected,
                        warnings=fitted.warnings,
                    )
                except ValueError as exc:
                    return source.block_id, None, str(exc)
                return source.block_id, fitted_result, None

        fitted_values = await asyncio.gather(
            *(
                fit_one(source, result, related_issues)
                for source, result, related_issues in candidates
            )
        )
        changed = False
        trial_results = dict(result_by_id)
        for block_id, fitted_result, error in fitted_values:
            if error:
                fit_issues.append(
                    QaIssue(
                        code="TEXT_FIT_REJECTED",
                        message=error,
                        severity=IssueSeverity.WARNING,
                        stage="text_fit",
                        block_id=block_id,
                    )
                )
                continue
            if (
                fitted_result is None
                or fitted_result.translated_text
                == result_by_id[block_id].translated_text
            ):
                continue
            trial_results[block_id] = fitted_result
            changed = True
        if not changed:
            break

        trial_document = work / f"translated-r{round_number}{suffix}"
        try:
            engine.patch(
                source_path,
                trial_document,
                list(trial_results.values()),
                blocks=source_blocks,
                strict=True,
            )
            trial_issues = _run_office_qa(
                source_path,
                trial_document,
                blocks=source_blocks,
                translations=list(trial_results.values()),
            )
        except Exception as exc:
            fit_issues.append(
                QaIssue(
                    code="TEXT_FIT_ROUND_FAILED",
                    message=str(exc),
                    severity=IssueSeverity.WARNING,
                    stage="text_fit",
                    details={"round": round_number},
                )
            )
            break

        current_error_count = sum(
            issue.severity == IssueSeverity.ERROR for issue in current_issues
        )
        trial_error_count = sum(
            issue.severity == IssueSeverity.ERROR for issue in trial_issues
        )
        current_score = _layout_issue_score(current_issues)
        trial_score = _layout_issue_score(trial_issues)
        score_tolerance = max(1e-6, abs(current_score) * 1e-9)
        same_error_locations = _layout_error_identities(
            trial_issues
        ) == _layout_error_identities(current_issues)
        improved = trial_error_count < current_error_count or (
            trial_error_count == current_error_count
            and same_error_locations
            and trial_score < current_score - score_tolerance
        )
        if not improved:
            fit_issues.append(
                QaIssue(
                    code="TEXT_FIT_NO_IMPROVEMENT",
                    message="Text-fit round did not improve the layout score",
                    severity=IssueSeverity.WARNING,
                    stage="text_fit",
                    details={
                        "round": round_number,
                        "before_errors": current_error_count,
                        "after_errors": trial_error_count,
                        "before_score": current_score,
                        "after_score": trial_score,
                    },
                )
            )
            break
        current_document = trial_document
        current_issues = trial_issues
        result_by_id = trial_results
        if trial_error_count == 0:
            break

    ordered_results = [result_by_id[block.block_id] for block in source_blocks]
    return (
        current_document,
        ordered_results,
        current_issues,
        fit_issues,
    )


def _engine_for(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return DocxEngine()
    if suffix == ".pptx":
        return PptxEngine()
    raise ValueError(f"Unsupported normalized Office format: {suffix}")


def _run_office_qa(
    source: Path,
    candidate: Path,
    *,
    blocks: Sequence[TranslationBlock] | None = None,
    translations: Sequence[TranslationResult] | None = None,
) -> list[QaIssue]:
    kwargs = {
        "blocks": blocks,
        "translations": translations,
    }
    if source.suffix.lower() == ".docx":
        return run_docx_qa(source, candidate, **kwargs)
    if source.suffix.lower() == ".pptx":
        result = run_pptx_qa_with_rendered_layout(
            source,
            candidate,
            **kwargs,
        )
        return list(result["issues"])
    raise ValueError(f"Unsupported Office QA format: {source.suffix}")


def _select_sample_blocks(
    blocks: Sequence[TranslationBlock],
    normalized: Path,
    page_or_slide: int,
) -> list[TranslationBlock]:
    if normalized.suffix.lower() == ".pptx":
        selected = [
            block
            for block in blocks
            if _block_location_number(block, "slide") == page_or_slide
        ]
        return selected

    selected = [
        block
        for block in blocks
        if _block_location_number(block, "page") == page_or_slide
    ]
    if selected:
        return selected
    if page_or_slide != 1:
        return []

    # Word does not persist reliable page boundaries in OOXML.  The
    # deterministic sample boundary is therefore the first explicit page-break
    # region, capped to avoid translating the full document.
    first_region: list[TranslationBlock] = []
    for block in blocks:
        first_region.append(block)
        risks = set(block.metadata.get("risk_flags", []))
        if "manual_break" in risks or len(first_region) >= 30:
            break
    return first_region


def _block_location_number(block: TranslationBlock, kind: str) -> int | None:
    candidates = (
        block.context.get(kind),
        block.context.get(f"{kind}_number"),
        block.context.get(f"{kind}_index"),
        block.metadata.get(kind),
        block.metadata.get(f"{kind}_number"),
        block.metadata.get(f"{kind}_index"),
    )
    for value in candidates:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if kind == "slide" and "slide_index" in block.metadata and number == 0:
            return 1
        return number
    return None


def _passthrough_result(block: TranslationBlock) -> TranslationResult:
    return TranslationResult(
        block_id=block.block_id,
        translated_text=block.source_text,
        segments=[segment.model_copy() for segment in block.segments],
        provider="sample-passthrough",
    )


def _provider_from_metadata(metadata: dict[str, Any]) -> TranslationProvider:
    fixture = metadata.get("fixture")
    if fixture:
        return FixtureProvider(
            Path(str(fixture)),
            missing=str(metadata.get("fixture_missing", "error")),
        )
    raise ValueError(
        "A TranslationProvider is required. Pass one explicitly or set metadata.fixture."
    )


def _artifact(
    path: Path,
    artifact_type: ArtifactType,
    language: str | None = None,
) -> Artifact:
    return Artifact(
        artifact_type=artifact_type,
        path=path,
        filename=path.name,
        media_type={
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".json": "application/json",
        }.get(path.suffix.lower()),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        language=language,
    )


def _write_reports(
    manifest: ArtifactManifest,
    job_root: Path,
    artifacts_dir: Path,
) -> None:
    qa_path = artifacts_dir / "qa-report.json"
    timing_path = artifacts_dir / "timing-report.json"
    atomic_write_json(
        qa_path,
        {
            "job_id": manifest.job_id,
            "status": manifest.status.value,
            "issues": [issue.model_dump(mode="json") for issue in manifest.issues],
        },
    )
    atomic_write_json(
        timing_path,
        {
            "job_id": manifest.job_id,
            "timings": [timing.model_dump(mode="json") for timing in manifest.timings],
            "elapsed_seconds": sum(
                timing.elapsed_seconds or 0 for timing in manifest.timings
            ),
        },
    )
    existing_types = {artifact.artifact_type for artifact in manifest.artifacts}
    if ArtifactType.QA_REPORT not in existing_types:
        manifest.artifacts.append(_artifact(qa_path, ArtifactType.QA_REPORT))
    if ArtifactType.TIMING_REPORT not in existing_types:
        manifest.artifacts.append(_artifact(timing_path, ArtifactType.TIMING_REPORT))
    manifest.metadata["manifest_path"] = str(job_root / "manifest.json")


def _persist_manifest(manifest: ArtifactManifest, path: Path) -> None:
    manifest.updated_at = datetime.now(timezone.utc)
    atomic_write_json(path, manifest.model_dump(mode="json"))
