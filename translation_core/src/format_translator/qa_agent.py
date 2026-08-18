"""Bounded, persistent QA and repair orchestration for Office documents.

The coordinator owns paths, state transitions, scoring, acceptance, rollback,
and audit persistence.  Language providers and repair executors are duck typed
and may be synchronous or asynchronous, but cannot accept their own output.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import os
import re
import shutil
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias

from pydantic import ValidationError

from .pptx_repair_adapter import (
    PptxRepairAdaptation,
    adapt_pptx_repairs,
    execute_pptx_repair_adapter,
)
from .pptx_rendered_layout_qa import (
    LayoutRunner,
    run_pptx_qa_with_rendered_layout,
)
from .protection import protect_text, validate_restored_values
from .providers import ProviderError
from .utils import atomic_write_json, sha256_file


PathLike: TypeAlias = str | os.PathLike[str]
QaRunner: TypeAlias = Callable[..., Any]
SUPPORTED_OFFICE_SUFFIXES = frozenset({".docx", ".pptx"})
SAFE_DOCX_ACTIONS = frozenset(
    {
        "retranslate",
        "fit_text",
        "terminology_fix",
        "placeholder_restore",
        "insert_page_break_before",
        "remove_page_break_before",
        "set_keep_next",
        "set_keep_lines",
        "set_spacing",
        "replace_translation",
        "replace_text",
    }
)
SAFE_PPTX_ACTIONS = frozenset(
    {
        "retranslate",
        "fit_text",
        "terminology_fix",
        "placeholder_restore",
        "resolve_text_overflow",
        "restore_shape_coordinates",
        "restore_table_row_heights",
        "set_shape_bounds",
        "adjust_shape_geometry",
        "set_font_size",
        "enable_autofit",
        "set_autofit",
        "set_textbox_margins",
        "adjust_margins",
        "adjust_font_size",
        "replace_translation",
        "replace_text",
    }
)
STRICT_LAYOUT_PPTX_ACTIONS = frozenset(
    {
        "replace_translation",
        "resolve_text_overflow",
    }
)
TRUSTED_RENDERED_OVERFLOW_ISSUE_CODES = frozenset(
    {
        "PPTX_RENDERED_TEXT_OVERFLOW",
        "PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
    }
)
NON_EXECUTABLE_METADATA_ACTIONS = frozenset({"flag_for_review"})


class QaAgentState(StrEnum):
    RECEIVED = "RECEIVED"
    BASELINE_QA = "BASELINE_QA"
    DIAGNOSING = "DIAGNOSING"
    PLANNING = "PLANNING"
    REPAIRING = "REPAIRING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class QaRepairSpec:
    """Minimal Office-only input accepted by the repair coordinator."""

    job_id: str
    source_document: Path
    candidate_document: Path
    work_dir: Path
    max_rounds: int = 2
    strict_layout: bool = False
    strict_page_count: bool = True
    rendered_layout_required: bool = False
    report_path: Path | None = None

    @classmethod
    def from_value(
        cls,
        value: QaRepairSpec | Mapping[str, Any] | object,
    ) -> QaRepairSpec:
        if isinstance(value, cls):
            return value

        def get(
            name: str,
            default: Any = dataclasses.MISSING,
            aliases: Sequence[str] = (),
        ) -> Any:
            names = (name, *aliases)
            if isinstance(value, Mapping):
                for candidate in names:
                    if candidate in value:
                        return value[candidate]
            else:
                for candidate in names:
                    if hasattr(value, candidate):
                        return getattr(value, candidate)
            if default is not dataclasses.MISSING:
                return default
            raise ValueError(f"QA repair spec is missing required field {name!r}")

        report = get("report_path", None)
        return cls(
            job_id=str(get("job_id")),
            source_document=Path(
                get("source_document", aliases=("source_docx", "source_pptx"))
            ),
            candidate_document=Path(
                get(
                    "candidate_document",
                    aliases=("candidate_docx", "candidate_pptx"),
                )
            ),
            work_dir=Path(get("work_dir")),
            max_rounds=int(get("max_rounds", 2)),
            strict_layout=get("strict_layout", False),
            strict_page_count=bool(get("strict_page_count", True)),
            rendered_layout_required=bool(get("rendered_layout_required", False)),
            report_path=Path(report) if report is not None else None,
        )

    def validated(self) -> QaRepairSpec:
        if not self.job_id.strip():
            raise ValueError("job_id cannot be empty")
        if self.max_rounds < 0:
            raise ValueError("max_rounds cannot be negative")
        if type(self.strict_layout) is not bool:
            raise ValueError("strict_layout must be a boolean")
        source = self.source_document.expanduser().resolve()
        candidate = self.candidate_document.expanduser().resolve()
        work_dir = self.work_dir.expanduser().resolve()
        for path, label in (
            (source, "source_document"),
            (candidate, "candidate_document"),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"{label} does not exist: {path}")
            if path.suffix.lower() not in SUPPORTED_OFFICE_SUFFIXES:
                raise ValueError(f"{label} must be DOCX or PPTX: {path}")
        if source.suffix.lower() != candidate.suffix.lower():
            raise ValueError("source_document and candidate_document types must match")
        report = (
            self.report_path.expanduser().resolve()
            if self.report_path is not None
            else work_dir / "qa-repair-report.json"
        )
        return dataclasses.replace(
            self,
            job_id=self.job_id.strip(),
            source_document=source,
            candidate_document=candidate,
            work_dir=work_dir,
            report_path=report,
        )

    @property
    def source_docx(self) -> Path | None:
        return (
            self.source_document
            if self.source_document.suffix.lower() == ".docx"
            else None
        )

    @property
    def candidate_docx(self) -> Path | None:
        return (
            self.candidate_document
            if self.candidate_document.suffix.lower() == ".docx"
            else None
        )

    @property
    def source_pptx(self) -> Path | None:
        return (
            self.source_document
            if self.source_document.suffix.lower() == ".pptx"
            else None
        )

    @property
    def candidate_pptx(self) -> Path | None:
        return (
            self.candidate_document
            if self.candidate_document.suffix.lower() == ".pptx"
            else None
        )


@dataclass(slots=True)
class QaRepairReport:
    job_id: str
    run_id: str
    status: QaAgentState
    document_type: str
    report_path: Path
    source_document: Path
    initial_candidate_document: Path
    accepted_document: Path
    accepted_score: int | None = None
    accepted_issues: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    state_history: list[dict[str, Any]] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    started_at: str = field(default_factory=lambda: _now())
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(dataclasses.asdict(self))

    @property
    def state(self) -> QaAgentState:
        return self.status

    @property
    def document_format(self) -> str:
        return self.document_type

    @property
    def candidate_document(self) -> Path:
        return self.accepted_document

    @property
    def accepted(self) -> bool:
        return self.status == QaAgentState.COMPLETED

    @property
    def rollback(self) -> bool:
        return any(bool(round_.get("rolled_back")) for round_ in self.rounds)

    @property
    def final_issues(self) -> list[dict[str, Any]]:
        return list(self.accepted_issues)

    @property
    def initial_issues(self) -> list[dict[str, Any]]:
        if self.rounds:
            return list(self.rounds[0].get("before_issues", []))
        return list(self.accepted_issues)

    @property
    def candidate_docx(self) -> Path | None:
        return self.initial_candidate_document if self.document_type == "docx" else None

    @property
    def accepted_docx(self) -> Path | None:
        return self.accepted_document if self.document_type == "docx" else None

    @property
    def candidate_pptx(self) -> Path | None:
        return self.initial_candidate_document if self.document_type == "pptx" else None

    @property
    def accepted_pptx(self) -> Path | None:
        return self.accepted_document if self.document_type == "pptx" else None


def run_qa_repair_agent(
    spec: QaRepairSpec | Mapping[str, Any] | object,
    *,
    repair_engine: object | None = None,
    processing_provider: object | None = None,
    qa_runner: QaRunner | Mapping[str, QaRunner] | object | None = None,
    pptx_layout_runner: LayoutRunner | None = None,
    baseline_issues: Sequence[Any] | None = None,
) -> QaRepairReport:
    """Run the Office QA agent from synchronous code."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            run_qa_repair_agent_async(
                spec,
                repair_engine=repair_engine,
                processing_provider=processing_provider,
                qa_runner=qa_runner,
                pptx_layout_runner=pptx_layout_runner,
                baseline_issues=baseline_issues,
            )
        )
    raise RuntimeError(
        "run_qa_repair_agent cannot run inside an active event loop; "
        "await run_qa_repair_agent_async instead"
    )


async def run_qa_repair_agent_async(
    spec: QaRepairSpec | Mapping[str, Any] | object,
    *,
    repair_engine: object | None = None,
    processing_provider: object | None = None,
    qa_runner: QaRunner | Mapping[str, QaRunner] | object | None = None,
    pptx_layout_runner: LayoutRunner | None = None,
    baseline_issues: Sequence[Any] | None = None,
) -> QaRepairReport:
    """Run bounded Office repair rounds and persist every decision.

    ``baseline_issues`` is reserved for a caller that already performed and
    durably recorded QA for the exact immutable candidate.  Supplying it avoids
    a second, potentially nondeterministic baseline evaluation while every new
    repair candidate is still evaluated by ``qa_runner``.
    """

    raw_spec = spec
    resolved = QaRepairSpec.from_value(spec).validated()
    resolved.work_dir.mkdir(parents=True, exist_ok=True)
    assert resolved.report_path is not None
    document_type = resolved.source_document.suffix.lower().lstrip(".")
    run_token = uuid.uuid4().hex[:12]
    run_id = f"{_safe_name(resolved.job_id)}-{run_token}"
    run_root = resolved.work_dir / "qa" / run_token
    run_root.mkdir(parents=True, exist_ok=False)
    report = QaRepairReport(
        job_id=resolved.job_id,
        run_id=run_id,
        status=QaAgentState.RECEIVED,
        document_type=document_type,
        report_path=resolved.report_path,
        source_document=resolved.source_document,
        initial_candidate_document=resolved.candidate_document,
        accepted_document=resolved.candidate_document,
    )
    _record_state(report, QaAgentState.RECEIVED)

    current = resolved.candidate_document
    current_issues: list[Any] = []
    current_score: int | None = None
    verified_candidate_seen = False
    system_failures = 0

    try:
        runner = _select_qa_runner(
            document_type,
            qa_runner,
            pptx_layout_runner=pptx_layout_runner,
            rendered_layout_required=resolved.rendered_layout_required,
        )
        _record_state(report, QaAgentState.BASELINE_QA)
        if baseline_issues is None:
            current_issues = await _run_qa(
                runner,
                resolved.source_document,
                current,
                strict_page_count=resolved.strict_page_count,
                provider_spec=_updated_provider_spec(
                    raw_spec,
                    candidate_document=current,
                    issues=[],
                ),
            )
        else:
            current_issues = list(baseline_issues)
        current_score = _quality_score(current_issues)
        report.accepted_score = current_score
        report.accepted_issues = [_issue_dict(issue) for issue in current_issues]
        baseline_dir = run_root / "b"
        baseline_dir.mkdir()
        atomic_write_json(
            baseline_dir / "qa.json",
            {
                "score": current_score,
                "issues": report.accepted_issues,
                "document": str(current),
            },
        )
        _persist(report)
        if _passes_gate(current_issues):
            _finish(report, QaAgentState.COMPLETED)
            return report

        for round_number in range(1, resolved.max_rounds + 1):
            round_dir = run_root / f"r{round_number}"
            round_dir.mkdir()
            candidate = round_dir / f"c.{document_type}"
            round_record: dict[str, Any] = {
                "round": round_number,
                "started_at": _now(),
                "input_document": str(current),
                "candidate_document": str(candidate),
                "before_score": current_score,
                "before_issues": [_issue_dict(issue) for issue in current_issues],
                "accepted": False,
                "rolled_back": False,
            }
            report.rounds.append(round_record)

            _record_state(report, QaAgentState.DIAGNOSING, round_number)
            provider_spec = _updated_provider_spec(
                raw_spec,
                candidate_document=current,
                issues=current_issues,
            )
            diagnosis_provider_failed = False
            try:
                diagnosis = await _diagnose(
                    source_document=resolved.source_document,
                    candidate_document=current,
                    issues=current_issues,
                    provider_spec=provider_spec,
                    repair_engine=repair_engine,
                    processing_provider=processing_provider,
                    round_number=round_number,
                    job_id=resolved.job_id,
                )
            except (ProviderError, ValidationError) as error:
                diagnosis_provider_failed = True
                diagnosis = _deterministic_diagnosis(
                    resolved.source_document,
                    current,
                    current_issues,
                )
                round_record["provider_error"] = _provider_error_dict(
                    stage="diagnose",
                    error=error,
                    fallback="deterministic_diagnosis_and_default_plan",
                )
            round_record["diagnosis"] = _jsonable(diagnosis)
            _persist(report)

            _record_state(report, QaAgentState.PLANNING, round_number)
            default_plan = _default_plan(
                current_issues,
                document_type=document_type,
                has_processing_provider=processing_provider is not None,
            )
            if diagnosis_provider_failed:
                plan = default_plan
            else:
                try:
                    plan = await _plan(
                        diagnosis=diagnosis,
                        issues=current_issues,
                        provider_spec=provider_spec,
                        repair_engine=repair_engine,
                        processing_provider=processing_provider,
                        document_type=document_type,
                        strict_page_count=resolved.strict_page_count,
                        previous_rounds=report.rounds[:-1],
                        round_number=round_number,
                        job_id=resolved.job_id,
                    )
                except (ProviderError, ValidationError) as error:
                    plan = default_plan
                    round_record["provider_error"] = _provider_error_dict(
                        stage="plan",
                        error=error,
                        fallback="default_plan",
                    )
                    round_record["plan"] = _jsonable(plan)
                    _persist(report)
            prepared_actions = await _prepare_actions(
                _plan_actions(plan),
                provider_spec=provider_spec,
                processing_provider=processing_provider,
                round_number=round_number,
                job_id=resolved.job_id,
            )
            if document_type == "docx" and repair_engine is None:
                prepared_actions = _prepare_docx_replacement_actions(
                    prepared_actions,
                    provider_spec,
                )
            strict_layout = document_type == "pptx" and _strict_layout_enabled(
                provider_spec
            )
            strict_policy_rejections: list[dict[str, Any]] = []
            if strict_layout:
                (
                    prepared_actions,
                    strict_policy_rejections,
                ) = _strict_pptx_plan_actions(prepared_actions)
            pptx_adaptation: PptxRepairAdaptation | None = None
            if document_type == "pptx" and repair_engine is None:
                pptx_adaptation = adapt_pptx_repairs(
                    resolved.source_document,
                    current,
                    issues=current_issues,
                    actions=prepared_actions,
                    strict_layout=strict_layout,
                )
                prepared_actions = pptx_adaptation.normalized_actions
            round_record["plan"] = _jsonable(plan)
            round_record["prepared_actions"] = _jsonable(prepared_actions)
            if strict_layout:
                round_record["strict_layout_policy"] = {
                    "enabled": True,
                    "allowed_actions": sorted(STRICT_LAYOUT_PPTX_ACTIONS),
                    "rejected_plan_actions": strict_policy_rejections,
                }
            if pptx_adaptation is not None:
                round_record["pptx_adapter"] = pptx_adaptation.to_dict()
            executable_actions, metadata_actions = _partition_execution_actions(
                prepared_actions
            )
            if metadata_actions:
                round_record["non_executable_actions"] = _jsonable(metadata_actions)
            _persist(report)

            if not _actions_are_repairable(
                executable_actions,
                document_type=document_type,
                repair_engine=repair_engine,
                allowed_actions=_allowed_action_names(provider_spec),
                strict_layout=strict_layout,
                issues=current_issues,
            ):
                round_record.update(
                    {
                        "finished_at": _now(),
                        "outcome": "needs_review",
                        "reason": "No executable Office repair action is available.",
                    }
                )
                _persist(report)
                break

            _record_state(report, QaAgentState.REPAIRING, round_number)
            try:
                audit = await _run_repair(
                    document_type=document_type,
                    repair_engine=repair_engine,
                    source_document=resolved.source_document,
                    current_document=current,
                    output_document=candidate,
                    diagnosis=diagnosis,
                    plan=plan,
                    actions=executable_actions,
                    issues=current_issues,
                    processing_provider=processing_provider,
                    provider_spec=provider_spec,
                    round_number=round_number,
                    job_id=resolved.job_id,
                    strict_page_count=resolved.strict_page_count,
                    rollback_dir=round_dir / "rb",
                    audit_path=round_dir / "repair-audit.json",
                    pptx_adaptation=pptx_adaptation,
                    pptx_layout_runner=pptx_layout_runner,
                    rendered_layout_required=resolved.rendered_layout_required,
                )
                round_record["repair_audit"] = _jsonable(audit)
            except Exception as error:
                system_failures += 1
                round_record.update(
                    {
                        "finished_at": _now(),
                        "outcome": "repair_failed",
                        "error": _error_dict(error),
                    }
                )
                _persist(report)
                continue

            try:
                package_audit = _validate_office_candidate(
                    candidate,
                    expected_suffix=resolved.source_document.suffix.lower(),
                )
                round_record["package_audit"] = package_audit
            except Exception as error:
                system_failures += 1
                round_record.update(
                    {
                        "finished_at": _now(),
                        "outcome": "package_validation_failed",
                        "error": _error_dict(error),
                    }
                )
                _persist(report)
                continue

            _record_state(report, QaAgentState.VERIFYING, round_number)
            try:
                authorized_coordinate_changes = (
                    _nested_mapping_value(
                        audit,
                        "authorized_coordinate_changes",
                    )
                    if document_type == "pptx"
                    else None
                )
                candidate_issues = await _run_qa(
                    runner,
                    resolved.source_document,
                    candidate,
                    strict_page_count=resolved.strict_page_count,
                    provider_spec=_updated_provider_spec(
                        raw_spec,
                        candidate_document=candidate,
                        issues=current_issues,
                    ),
                    authorized_coordinate_changes=authorized_coordinate_changes,
                )
            except Exception as error:
                system_failures += 1
                round_record.update(
                    {
                        "finished_at": _now(),
                        "outcome": "verification_failed",
                        "error": _error_dict(error),
                    }
                )
                _persist(report)
                continue

            verified_candidate_seen = True
            candidate_score = _quality_score(candidate_issues)
            regression = _has_regression(current_issues, candidate_issues)
            identical_to_input = sha256_file(candidate) == sha256_file(current)
            improved = not identical_to_input and (
                current_score is None
                or (candidate_score > current_score and not regression)
            )
            candidate_issue_dicts = [_issue_dict(issue) for issue in candidate_issues]
            atomic_write_json(
                round_dir / "qa.json",
                {
                    "score": candidate_score,
                    "issues": candidate_issue_dicts,
                    "document": str(candidate),
                    "regression": regression,
                },
            )
            round_record.update(
                {
                    "finished_at": _now(),
                    "after_score": candidate_score,
                    "after_issues": candidate_issue_dicts,
                    "score_delta": (
                        None
                        if current_score is None
                        else candidate_score - current_score
                    ),
                    "regression": regression,
                    "identical_to_input": identical_to_input,
                    "accepted": improved,
                    "rolled_back": not improved,
                    "outcome": (
                        "accepted"
                        if improved
                        else (
                            "rejected_no_change"
                            if identical_to_input
                            else (
                                "rejected_regression"
                                if regression
                                else "rejected_no_improvement"
                            )
                        )
                    ),
                }
            )
            if improved:
                current = candidate
                current_issues = candidate_issues
                current_score = candidate_score
                report.accepted_document = current
                report.accepted_score = current_score
                report.accepted_issues = candidate_issue_dicts
            _persist(report)
            if improved and _passes_gate(current_issues):
                _finish(report, QaAgentState.COMPLETED)
                return report

        if system_failures and not verified_candidate_seen:
            _finish(
                report,
                QaAgentState.FAILED,
                failure={
                    "code": "ALL_REPAIR_ATTEMPTS_FAILED",
                    "message": "No Office candidate reached successful verification.",
                    "failed_rounds": system_failures,
                },
            )
        else:
            _finish(report, QaAgentState.NEEDS_REVIEW)
        return report
    except Exception as error:
        _finish(report, QaAgentState.FAILED, failure=_error_dict(error))
        return report


async def _run_qa(
    runner: QaRunner,
    source: Path,
    candidate: Path,
    *,
    strict_page_count: bool,
    provider_spec: Any,
    authorized_coordinate_changes: Mapping[str, Sequence[int]] | None = None,
) -> list[Any]:
    suffix = source.suffix.lower()
    result = await _invoke(
        runner,
        {
            "source_document": source,
            "candidate_document": candidate,
            "source_path": source,
            "candidate_path": candidate,
            "source_docx": source if suffix == ".docx" else None,
            "candidate_docx": candidate if suffix == ".docx" else None,
            "source_pptx": source if suffix == ".pptx" else None,
            "candidate_pptx": candidate if suffix == ".pptx" else None,
            "strict_page_count": strict_page_count,
            "blocks": _optional_spec_collection(provider_spec, "blocks"),
            "translations": _spec_collection(provider_spec, "translations"),
            "authorized_coordinate_changes": authorized_coordinate_changes,
        },
    )
    if result is None:
        return []
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return list(result)
    if isinstance(result, Mapping) and "issues" in result:
        return list(result["issues"])
    if hasattr(result, "issues"):
        return list(result.issues)
    raise TypeError(
        "Office QA runner must return issues or an object containing issues"
    )


def _select_qa_runner(
    document_type: str,
    supplied: QaRunner | Mapping[str, QaRunner] | object | None,
    *,
    pptx_layout_runner: LayoutRunner | None = None,
    rendered_layout_required: bool = False,
) -> QaRunner:
    function_name = f"run_{document_type}_qa"
    if supplied is not None:
        if isinstance(supplied, Mapping):
            for key in (document_type, f".{document_type}", function_name):
                candidate = supplied.get(key)
                if callable(candidate):
                    return candidate
        method = getattr(supplied, function_name, None)
        if callable(method):
            return method
        if callable(supplied):
            return supplied
        raise TypeError(f"qa_runner does not provide {function_name}")

    if document_type == "pptx":

        def combined_pptx_runner(**kwargs: Any) -> dict[str, Any]:
            return run_pptx_qa_with_rendered_layout(
                layout_runner=pptx_layout_runner,
                rendered_layout_required=rendered_layout_required,
                **kwargs,
            )

        return combined_pptx_runner

    module_name = ".word_qa"
    module = importlib.import_module(module_name, package=__package__)
    runner = getattr(module, function_name, None)
    if not callable(runner):
        raise RuntimeError(f"{module.__name__} does not provide {function_name}")
    return runner


async def _diagnose(
    *,
    source_document: Path,
    candidate_document: Path,
    issues: Sequence[Any],
    provider_spec: Any,
    repair_engine: object | None,
    processing_provider: object | None,
    round_number: int,
    job_id: str,
) -> Any:
    deterministic = _deterministic_diagnosis(
        source_document,
        candidate_document,
        issues,
    )
    target = _find_method(repair_engine, ("diagnose", "diagnose_office"))
    if target is None:
        target = _find_method(
            processing_provider,
            ("diagnose", "diagnose_office", "diagnose_layout"),
        )
    if target is None:
        return deterministic
    enriched = await _invoke(
        target,
        {
            "spec": provider_spec,
            "request_id": f"{job_id}-diagnose-r{round_number}",
            "job_id": job_id,
            "round_number": round_number,
            "source_document": source_document,
            "candidate_document": candidate_document,
            "issues": [_issue_dict(issue) for issue in issues],
            "deterministic_diagnosis": deterministic,
        },
    )
    # Keep the provider's typed diagnosis for the matching planner.  The round
    # audit is converted through _jsonable at persistence time; converting it
    # here would silently turn QaDiagnosis into a dict and break providers whose
    # plan contract deliberately requires that validated model.
    return {"deterministic": deterministic, "agent": enriched}


async def _plan(
    *,
    diagnosis: Any,
    issues: Sequence[Any],
    provider_spec: Any,
    repair_engine: object | None,
    processing_provider: object | None,
    document_type: str,
    strict_page_count: bool,
    previous_rounds: Sequence[Mapping[str, Any]],
    round_number: int,
    job_id: str,
) -> Any:
    default = _default_plan(
        issues,
        document_type=document_type,
        has_processing_provider=processing_provider is not None,
    )
    planner = _find_method(repair_engine, ("plan", "plan_repair"))
    planner_uses_processing_provider = False
    if planner is None:
        planner = _find_method(
            processing_provider,
            ("plan", "plan_repair", "plan_office_repair"),
        )
        planner_uses_processing_provider = planner is not None
    if planner is None:
        return default
    planner_diagnosis = diagnosis
    if planner_uses_processing_provider and isinstance(diagnosis, Mapping):
        planner_diagnosis = diagnosis.get("agent", diagnosis)
    planned = await _invoke(
        planner,
        {
            "spec": provider_spec,
            "diagnosis": planner_diagnosis,
            "request_id": f"{job_id}-plan-r{round_number}",
            "job_id": job_id,
            "round_number": round_number,
            "issues": [_issue_dict(issue) for issue in issues],
            "default_plan": default,
            "previous_rounds": _jsonable(previous_rounds),
            "strict_page_count": strict_page_count,
        },
    )
    return planned if planned is not None else default


async def _prepare_actions(
    actions: Sequence[Mapping[str, Any]],
    *,
    provider_spec: Any,
    processing_provider: object | None,
    round_number: int,
    job_id: str,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    direct_text_actions = {
        "retranslate",
        "fit_text",
        "terminology_fix",
        "placeholder_restore",
    }
    for index, raw in enumerate(actions, start=1):
        action = dict(raw)
        name = _action_name(action)
        if name in direct_text_actions and isinstance(action.get("proposed_text"), str):
            action["requested_action_type"] = name
            action["action_type"] = "replace_translation"
            prepared.append(action)
            continue
        if name != "fit_text":
            prepared.append(action)
            continue
        fitter = _find_method(processing_provider, ("fit_text",))
        block_id = action.get("block_id")
        source = _collection_item(provider_spec, "blocks", block_id)
        translation = _collection_item(provider_spec, "translations", block_id)
        if fitter is None or source is None or translation is None:
            action["blocked"] = True
            action["blocked_reason"] = "text-fit provider context is unavailable"
            prepared.append(action)
            continue
        fitted = await _invoke(
            fitter,
            {
                "source": source,
                "translation": translation,
                "max_length": action.get("max_length")
                or max(1, len(str(getattr(source, "source_text", "")))),
                "request_id": f"{job_id}-fit-r{round_number}-{index}",
            },
        )
        text = getattr(fitted, "fitted_text", None)
        if text is None and isinstance(fitted, Mapping):
            text = fitted.get("fitted_text")
        if not isinstance(text, str) or not text:
            action["blocked"] = True
            action["blocked_reason"] = "text-fit provider returned no usable text"
        else:
            action["requested_action_type"] = name
            action["action_type"] = "replace_translation"
            action["proposed_text"] = text
        prepared.append(action)
    return prepared


def _prepare_docx_replacement_actions(
    actions: Sequence[Mapping[str, Any]],
    provider_spec: Any,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for raw in actions:
        action = dict(raw)
        if action.get("blocked") or _action_name(action) not in {
            "replace_translation",
            "replace_text",
            "fit_text",
        }:
            prepared.append(action)
            continue
        block_id = action.get("block_id")
        block = _collection_item(provider_spec, "blocks", block_id)
        translation = _collection_item(provider_spec, "translations", block_id)
        proposed = action.get("proposed_text")
        if _object_value(block, "protocol", "plain") != "tagged":
            prepared.append(action)
            continue
        try:
            if translation is None or not isinstance(proposed, str):
                raise ValueError("tagged replacement context is unavailable")
            _copy_translation_with_text(
                translation,
                proposed,
                block=block,
            )
        except (TypeError, ValueError) as error:
            action["blocked"] = True
            action["blocked_reason"] = str(error)
        prepared.append(action)
    return prepared


async def _run_repair(
    *,
    document_type: str,
    repair_engine: object | None,
    source_document: Path,
    current_document: Path,
    output_document: Path,
    diagnosis: Any,
    plan: Any,
    actions: Sequence[Mapping[str, Any]],
    issues: Sequence[Any],
    processing_provider: object | None,
    provider_spec: Any,
    round_number: int,
    job_id: str,
    strict_page_count: bool,
    rollback_dir: Path,
    audit_path: Path,
    pptx_adaptation: PptxRepairAdaptation | None,
    pptx_layout_runner: LayoutRunner | None,
    rendered_layout_required: bool,
) -> Any:
    if output_document.exists():
        raise FileExistsError(f"Candidate must not be overwritten: {output_document}")
    executor = _select_repair_executor(document_type, repair_engine)
    executor_actions = (
        _docx_executor_actions(actions, provider_spec)
        if document_type == "docx" and repair_engine is None
        else (list(actions) if document_type == "pptx" else list(actions))
    )

    protected = list(dict.fromkeys((source_document, current_document)))
    rollback_dir.mkdir()
    backups: dict[Path, tuple[Path, str]] = {}
    for index, path in enumerate(protected):
        backup = rollback_dir / f"protected-{index:02d}.bak"
        shutil.copy2(path, backup)
        backups[path] = (backup, sha256_file(path))

    call_error: Exception | None = None
    result: Any = None
    try:
        suffix = f".{document_type}"
        result = await _invoke(
            executor,
            {
                "spec": provider_spec,
                "job_id": job_id,
                "round_number": round_number,
                "source_document": source_document,
                "current_document": current_document,
                "candidate_document": current_document,
                "source": current_document,
                "source_path": current_document,
                "output_document": output_document,
                "output": output_document,
                "output_path": output_document,
                "source_docx": (current_document if suffix == ".docx" else None),
                "candidate_docx": (current_document if suffix == ".docx" else None),
                "output_docx": output_document if suffix == ".docx" else None,
                "source_pptx": (current_document if suffix == ".pptx" else None),
                "candidate_pptx": (current_document if suffix == ".pptx" else None),
                "output_pptx": output_document if suffix == ".pptx" else None,
                "issues": [_issue_dict(issue) for issue in issues],
                "diagnosis": diagnosis,
                "plan": plan,
                "actions": executor_actions,
                "processing_provider": processing_provider,
                "strict_page_count": strict_page_count,
                "strict_layout": (
                    _strict_layout_enabled(provider_spec)
                    if document_type == "pptx"
                    else False
                ),
                "audit_path": audit_path,
                "layout_runner": pptx_layout_runner,
                "rendered_layout_required": rendered_layout_required,
            },
        )
    except Exception as error:
        call_error = error

    altered: list[str] = []
    for original, (backup, digest) in backups.items():
        if not original.is_file() or sha256_file(original) != digest:
            altered.append(str(original))
            _atomic_restore(backup, original)
    if altered:
        raise RuntimeError(
            "Repair executor modified protected inputs; restored: " + ", ".join(altered)
        )
    if call_error is not None:
        raise call_error

    produced = _result_path(result)
    if not output_document.exists() and produced is not None:
        produced = produced.expanduser().resolve()
        if not produced.is_file():
            raise FileNotFoundError(f"Repair result does not exist: {produced}")
        if produced in protected:
            raise RuntimeError("Repair executor returned a protected input")
        shutil.copy2(produced, output_document)
    if not output_document.is_file() or output_document.stat().st_size == 0:
        raise RuntimeError("Repair executor did not create a usable candidate")
    if pptx_adaptation is not None:
        return {
            "normalized_actions": pptx_adaptation.normalized_actions,
            "rejected_actions": pptx_adaptation.rejected_actions,
            "executor_result": _jsonable(result),
        }
    return result


def _select_repair_executor(
    document_type: str,
    supplied: object | None,
) -> Callable[..., Any]:
    names = (
        (
            "repair_docx",
            "execute_docx_repairs",
            "repair_document",
            "repair",
            "apply",
        )
        if document_type == "docx"
        else (
            "repair_pptx",
            "execute_pptx_repairs",
            "repair_document",
            "repair",
            "apply",
        )
    )
    executor = _find_method(supplied, names)
    if executor is None and callable(supplied):
        executor = supplied
    if executor is not None:
        return executor
    if supplied is not None:
        raise TypeError(f"repair_engine has no {document_type.upper()} executor")

    if document_type == "pptx":
        return execute_pptx_repair_adapter
    module_name = ".repair_engine"
    function_name = (
        "execute_repairs" if document_type == "docx" else "execute_pptx_repairs"
    )
    module = importlib.import_module(module_name, package=__package__)
    executor = getattr(module, function_name, None)
    if not callable(executor) and document_type == "pptx":
        executor = getattr(module, "apply_whitelisted_repairs", None)
    if not callable(executor):
        raise RuntimeError(f"{module.__name__} does not provide {function_name}")
    return executor


def _docx_executor_actions(
    actions: Sequence[Mapping[str, Any]],
    provider_spec: Any,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for action in actions:
        if action.get("blocked"):
            continue
        name = _action_name(action)
        action_id = str(action.get("action_id") or uuid.uuid4().hex)
        block_id = action.get("block_id")
        if name in {"insert_page_break_before", "remove_page_break_before"}:
            if not isinstance(block_id, str) or not block_id:
                continue
            converted.append(
                {
                    "type": "explicit_page_break",
                    "action_id": action_id,
                    "block_id": block_id,
                    "operation": (
                        "insert" if name == "insert_page_break_before" else "remove"
                    ),
                }
            )
        elif name in {"set_keep_next", "set_keep_lines", "set_spacing"}:
            if not isinstance(block_id, str) or not block_id:
                continue
            converted.append(
                {
                    "type": "paragraph_format",
                    "action_id": action_id,
                    "block_id": block_id,
                    "keep_next": (
                        bool(action.get("value")) if name == "set_keep_next" else None
                    ),
                    "keep_lines": (
                        bool(action.get("value")) if name == "set_keep_lines" else None
                    ),
                    "spacing_before_twips": action.get("spacing_before_twips"),
                    "spacing_after_twips": action.get("spacing_after_twips"),
                }
            )
        elif name in {"replace_translation", "replace_text", "fit_text"}:
            proposed = action.get("proposed_text")
            translation = _collection_item(provider_spec, "translations", block_id)
            block = _collection_item(provider_spec, "blocks", block_id)
            if not isinstance(proposed, str) or translation is None:
                continue
            result = _copy_translation_with_text(
                translation,
                proposed,
                block=block,
            )
            converted.append(
                {
                    "type": "replace_text",
                    "action_id": action_id,
                    "result": result,
                }
            )
    return converted


def _pptx_executor_actions(
    actions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    name_map = {
        "enable_autofit": "set_autofit",
        "set_autofit": "set_autofit",
        "set_textbox_margins": "adjust_margins",
        "adjust_margins": "adjust_margins",
        "adjust_font_size": "adjust_font_size",
        "restore_table_row_heights": "restore_table_row_heights",
        "replace_translation": "replace_translation",
        "replace_text": "replace_translation",
    }
    converted: list[dict[str, Any]] = []
    for raw in actions:
        if raw.get("blocked"):
            continue
        action = dict(raw)
        normalized = name_map.get(str(_action_name(action)))
        if normalized is None:
            continue
        action["action"] = normalized
        action.pop("action_type", None)
        converted.append(action)
    return converted


def _default_plan(
    issues: Sequence[Any],
    *,
    document_type: str,
    has_processing_provider: bool,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for index, issue in enumerate(issues, start=1):
        data = _issue_dict(issue)
        details = data.get("details")
        details = details if isinstance(details, Mapping) else {}
        hint = details.get("repair_hint")
        hint = hint if isinstance(hint, Mapping) else {}
        name = _action_name(hint)
        block_id = data.get("block_id") or hint.get("block_id")
        if name and hint.get("automatic") is not False:
            action = {
                **dict(hint),
                "action_id": hint.get("action_id") or f"default-{index}",
                "action_type": name,
                "block_id": block_id,
                "issue_ids": [data.get("issue_id") or f"issue-{index}"],
            }
            if name == "fit_text" and not has_processing_provider:
                action["blocked"] = True
                action["blocked_reason"] = "text-fit provider is unavailable"
                blocked.append(action)
            else:
                actions.append(action)
            continue

        code = str(data.get("code", "")).upper()
        overflow = any(token in code for token in ("OVERFLOW", "OUT_OF_BOUNDS"))
        if overflow and isinstance(block_id, str):
            action = {
                "action_id": f"default-{index}",
                "action_type": (
                    "fit_text" if document_type == "docx" else "enable_autofit"
                ),
                "block_id": block_id,
                "issue_ids": [data.get("issue_id") or f"issue-{index}"],
                "instruction": "Fit text within the existing Office container.",
                "rationale": code,
            }
            if document_type == "docx" and not has_processing_provider:
                action["blocked"] = True
                action["blocked_reason"] = "text-fit provider is unavailable"
                blocked.append(action)
            else:
                actions.append(action)
        else:
            blocked.append(
                {
                    "action_type": "flag_for_review",
                    "issue_ids": [data.get("issue_id") or f"issue-{index}"],
                    "reason": "no_safe_automatic_office_repair",
                }
            )
    return {
        "planner": "default",
        "actions": actions,
        "blocked_actions": blocked,
        "repairable": bool(actions),
    }


def _actions_are_repairable(
    actions: Sequence[Mapping[str, Any]],
    *,
    document_type: str,
    repair_engine: object | None,
    allowed_actions: set[str] | None,
    strict_layout: bool = False,
    issues: Sequence[Any] | None = None,
) -> bool:
    usable = [action for action in actions if not action.get("blocked")]
    if not usable:
        return False
    if strict_layout and (
        document_type != "pptx"
        or any(
            not _strict_pptx_action_is_allowed(action, issues=issues)
            for action in usable
        )
    ):
        return False
    safe_actions = SAFE_DOCX_ACTIONS if document_type == "docx" else SAFE_PPTX_ACTIONS
    if any(_action_name(action) not in safe_actions for action in usable):
        return False
    if allowed_actions is not None and any(
        not _action_is_allowed(action, allowed_actions) for action in usable
    ):
        return False
    if repair_engine is not None:
        return True
    allowed = (
        {
            "insert_page_break_before",
            "remove_page_break_before",
            "set_keep_next",
            "set_keep_lines",
            "set_spacing",
            "replace_translation",
            "replace_text",
            "fit_text",
        }
        if document_type == "docx"
        else {
            "resolve_text_overflow",
            "restore_shape_coordinates",
            "restore_table_row_heights",
            "set_shape_bounds",
            "adjust_shape_geometry",
            "set_font_size",
            "enable_autofit",
            "set_autofit",
            "set_textbox_margins",
            "adjust_margins",
            "adjust_font_size",
            "replace_translation",
            "replace_text",
        }
    )
    return all(_action_name(action) in allowed for action in usable)


def _strict_pptx_action_is_allowed(
    action: Mapping[str, Any],
    *,
    issues: Sequence[Any] | None = None,
) -> bool:
    name = _action_name(action)
    if name == "replace_translation":
        return True
    if name != "resolve_text_overflow":
        return False
    return _matches_trusted_rendered_overflow_issue(action, issues)


def _matches_trusted_rendered_overflow_issue(
    action: Mapping[str, Any],
    issues: Sequence[Any] | None,
) -> bool:
    """Verify strict overflow provenance against the selected QA issue.

    ``adapter_source`` and ``trusted_rendered_evidence`` are audit metadata,
    not capabilities.  Treat them as trustworthy only when the exact adapter
    issue index resolves to a rendered finding with the same code and target.
    """

    if action.get("trusted_rendered_evidence") is not True or issues is None:
        return False
    source_match = re.fullmatch(
        r"issue\[(0|[1-9][0-9]*)\]",
        str(action.get("adapter_source", "")),
    )
    if source_match is None:
        return False
    issue_index = int(source_match.group(1))
    if issue_index >= len(issues):
        return False

    issue = _issue_dict(issues[issue_index])
    issue_code = str(issue.get("code", "")).strip().upper()
    if issue_code not in TRUSTED_RENDERED_OVERFLOW_ISSUE_CODES:
        return False
    action_slide = action.get("slide")
    issue_slide = issue.get("slide")
    if (
        type(action_slide) is not int
        or action_slide <= 0
        or type(issue_slide) is not int
        or action_slide != issue_slide
    ):
        return False

    details = issue.get("details")
    details = details if isinstance(details, Mapping) else {}
    hint = details.get("repair_hint")
    hint = hint if isinstance(hint, Mapping) else {}
    issue_shape_id = details.get("shape_id")
    if issue_shape_id is None:
        issue_shape_id = hint.get("shape_id")
    action_shape_id = str(action.get("shape_id", "")).strip()
    if not action_shape_id or action_shape_id != str(issue_shape_id).strip():
        return False

    evidence = action.get("rendered_evidence")
    if not isinstance(evidence, Mapping):
        return False
    evidence_codes = evidence.get("issue_codes")
    if not isinstance(evidence_codes, Sequence) or isinstance(
        evidence_codes,
        (str, bytes, bytearray),
    ):
        return False
    return issue_code in {str(code).strip().upper() for code in evidence_codes}


def _partition_execution_actions(
    actions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    executable: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    for raw in actions:
        action = dict(raw)
        if action.get("blocked") or (
            _action_name(action) in NON_EXECUTABLE_METADATA_ACTIONS
        ):
            metadata.append(action)
        else:
            executable.append(action)
    return executable, metadata


def _strict_pptx_plan_actions(
    actions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only language replacements from an untrusted strict-mode plan.

    Table row-height restoration is deliberately absent here: it is only
    admitted later when the PPTX adapter synthesizes it from a trusted
    rendered-layout QA issue.
    """

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw in enumerate(actions):
        action = dict(raw)
        name = _action_name(action)
        if not action.get("blocked") and name == "replace_translation":
            accepted.append(action)
            continue
        rejected.append(
            {
                "source": f"plan[{index}]",
                "action": name,
                "code": (
                    "BLOCKED_ACTION"
                    if action.get("blocked")
                    else "STRICT_LAYOUT_ACTION_FORBIDDEN"
                ),
                "reason": (
                    str(
                        action.get(
                            "blocked_reason",
                            "The repair action is blocked",
                        )
                    )
                    if action.get("blocked")
                    else (
                        "Strict layout permits planned text replacement only; "
                        "format mutations require manual review"
                    )
                ),
                "payload": _jsonable(action),
            }
        )
    return accepted, rejected


def _action_is_allowed(
    action: Mapping[str, Any],
    allowed_actions: set[str],
) -> bool:
    name = str(action.get("requested_action_type") or _action_name(action))
    if name in allowed_actions:
        return True
    equivalents = {
        "set_autofit": "enable_autofit",
        "adjust_margins": "set_textbox_margins",
    }
    return equivalents.get(name) in allowed_actions


def _deterministic_diagnosis(
    source: Path,
    candidate: Path,
    issues: Sequence[Any],
) -> dict[str, Any]:
    source_snapshot = _office_snapshot(source)
    candidate_snapshot = _office_snapshot(candidate)
    codes: dict[str, int] = {}
    pages: set[int] = set()
    slides: set[int] = set()
    blocks: set[str] = set()
    for issue in issues:
        data = _issue_dict(issue)
        code = str(data.get("code", "UNKNOWN"))
        codes[code] = codes.get(code, 0) + 1
        if isinstance(data.get("page"), int):
            pages.add(data["page"])
        if isinstance(data.get("slide"), int):
            slides.add(data["slide"])
        if isinstance(data.get("block_id"), str):
            blocks.add(data["block_id"])
    return {
        "document_type": source.suffix.lower().lstrip("."),
        "source": source_snapshot,
        "candidate": candidate_snapshot,
        "issue_codes": codes,
        "affected_pages": sorted(pages),
        "affected_slides": sorted(slides),
        "affected_blocks": sorted(blocks),
    }


def _office_snapshot(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as package:
        names = package.namelist()
        media = [name for name in names if "/media/" in name]
        slides = [
            name
            for name in names
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        ]
        return {
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "part_count": len(names),
            "media_count": len(media),
            "slide_count": len(slides) if path.suffix.lower() == ".pptx" else None,
        }


def _validate_office_candidate(
    path: Path,
    *,
    expected_suffix: str,
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Candidate is missing or empty: {path}")
    if path.suffix.lower() != expected_suffix:
        raise ValueError("Repair executor changed the Office document type")
    try:
        with zipfile.ZipFile(path) as package:
            corrupt = package.testzip()
            if corrupt is not None:
                raise RuntimeError(f"Candidate contains corrupt OOXML part {corrupt}")
            names = set(package.namelist())
    except zipfile.BadZipFile as error:
        raise RuntimeError(f"Candidate is not a valid OOXML package: {path}") from error
    required = (
        "word/document.xml" if expected_suffix == ".docx" else "ppt/presentation.xml"
    )
    if required not in names:
        raise RuntimeError(f"Candidate is missing required part {required}")
    return {
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "part_count": len(names),
        "required_part": required,
    }


def _has_regression(before: Sequence[Any], after: Sequence[Any]) -> bool:
    before_errors = {
        _issue_fingerprint(issue) for issue in before if _severity(issue) == "error"
    }
    after_errors = {
        _issue_fingerprint(issue) for issue in after if _severity(issue) == "error"
    }
    if after_errors - before_errors:
        return True
    before_counts = _severity_counts(before)
    after_counts = _severity_counts(after)
    if after_counts["error"] > before_counts["error"]:
        return True
    if (
        after_counts["error"] == before_counts["error"]
        and after_counts["warning"] > before_counts["warning"]
    ):
        return True
    return False


def _quality_score(issues: Sequence[Any]) -> int:
    weights = {"error": 100, "warning": 10, "info": 1}
    return 1000 - sum(weights.get(_severity(issue), 100) for issue in issues)


def _passes_gate(issues: Sequence[Any]) -> bool:
    return all(_severity(issue) != "error" for issue in issues)


def _severity(issue: Any) -> str:
    value = _issue_dict(issue).get("severity", "error")
    if hasattr(value, "value"):
        value = value.value
    return str(value).lower()


def _severity_counts(issues: Sequence[Any]) -> dict[str, int]:
    result = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        severity = _severity(issue)
        result[severity if severity in result else "error"] += 1
    return result


def _issue_fingerprint(issue: Any) -> tuple[Any, ...]:
    data = _issue_dict(issue)
    code = "".join(
        character
        for character in str(data.get("code") or "unknown").casefold()
        if character.isalnum()
    )
    if code == "pptxrenderedtextobjectoverlap":
        return _pptx_rendered_overlap_fingerprint(data)
    raw_details = data.get("details")
    details = raw_details if isinstance(raw_details, Mapping) else {}
    raw_office_location = details.get("office_location")
    office_location = (
        raw_office_location if isinstance(raw_office_location, Mapping) else {}
    )
    block_id = _identity_component(data.get("block_id"))
    page = _first_identity_component(data.get("page"), office_location.get("page"))
    slide = _first_identity_component(data.get("slide"), office_location.get("slide"))
    shape_id = _first_identity_component(
        data.get("shape_id"),
        details.get("shape_id"),
        office_location.get("shape_id"),
        office_location.get("shape"),
    )
    cell_row = _first_identity_component(
        details.get("cell_row"), office_location.get("cell_row")
    )
    cell_column = _first_identity_component(
        details.get("cell_column"), office_location.get("cell_column")
    )
    region_id = _identity_component(details.get("region_id"))
    location = (
        block_id,
        page,
        slide,
        shape_id,
        cell_row,
        cell_column,
        region_id,
    )
    if any(value is not None for value in location):
        stage = _identity_component(data.get("stage"))
        stage_key = "".join(
            character for character in str(stage or "") if character.isalnum()
        )
        if stage_key in {"semantic", "semanticqa"}:
            return ("semantic_location", stage_key, *location)
        return (
            "location",
            code or "unknown",
            stage,
            *location,
        )
    return ("code", code or "unknown")


def _pptx_rendered_overlap_fingerprint(
    issue: Mapping[str, Any],
) -> tuple[Any, ...]:
    raw_details = issue.get("details")
    details = raw_details if isinstance(raw_details, Mapping) else {}
    raw_object = details.get("object")
    overlap_object = raw_object if isinstance(raw_object, Mapping) else {}
    slide = issue.get("slide")
    text_shape_id = _identity_component(details.get("shape_id", issue.get("shape_id")))
    region_id = _identity_component(details.get("region_id"))
    object_scope = _identity_component(
        details.get(
            "object_scope",
            details.get("inherited_scope", overlap_object.get("scope")),
        )
    )
    object_identity = _identity_component(
        details.get("object_identity", overlap_object.get("object_identity"))
    )
    if object_identity is None:
        object_shape_id = _identity_component(
            details.get(
                "object_shape_id",
                details.get(
                    "inherited_shape_id",
                    overlap_object.get("shape_id"),
                ),
            )
        )
        if object_shape_id is not None:
            if object_scope == "slide" and slide is not None:
                object_identity = f"slide:{slide}:shape:{object_shape_id}"
            elif object_scope is not None:
                object_identity = f"{object_scope}:shape:{object_shape_id}"
            else:
                object_identity = f"shape:{object_shape_id}"
    return (
        "pptx_rendered_text_object_overlap",
        slide,
        text_shape_id,
        region_id,
        object_identity,
    )


def _identity_component(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    return normalized or None


def _first_identity_component(*values: Any) -> str | None:
    for value in values:
        normalized = _identity_component(value)
        if normalized is not None:
            return normalized
    return None


def _plan_actions(plan: Any) -> list[dict[str, Any]]:
    if plan is None:
        return []
    raw = (
        plan.get("actions", [])
        if isinstance(plan, Mapping)
        else getattr(plan, "actions", [])
    )
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for action in raw:
        converted = _jsonable(action)
        if isinstance(converted, Mapping):
            result.append(dict(converted))
    return result


def _action_name(action: Mapping[str, Any]) -> str | None:
    value = action.get("action_type", action.get("action"))
    if hasattr(value, "value"):
        value = value.value
    return str(value) if value is not None else None


def _updated_provider_spec(
    raw_spec: Any,
    *,
    candidate_document: Path,
    issues: Sequence[Any],
) -> Any:
    updates = {
        "candidate_document": candidate_document,
        "issues": list(issues),
    }
    fields = getattr(type(raw_spec), "model_fields", {})
    if "candidate_docx" in fields and candidate_document.suffix.lower() == ".docx":
        updates["candidate_docx"] = candidate_document
    if "candidate_pptx" in fields and candidate_document.suffix.lower() == ".pptx":
        updates["candidate_pptx"] = candidate_document
    copier = getattr(raw_spec, "model_copy", None)
    if callable(copier):
        allowed = {key: value for key, value in updates.items() if key in fields}
        return copier(update=allowed)
    if isinstance(raw_spec, Mapping):
        result = dict(raw_spec)
        result.update(updates)
        return result
    return raw_spec


def _collection_item(spec: Any, name: str, block_id: Any) -> Any:
    values = _spec_collection(spec, name)
    for value in values or []:
        identifier = (
            value.get("block_id")
            if isinstance(value, Mapping)
            else getattr(value, "block_id", None)
        )
        if identifier == block_id:
            return value
    return None


def _spec_collection(spec: Any, name: str) -> list[Any]:
    values = (
        spec.get(name, []) if isinstance(spec, Mapping) else getattr(spec, name, [])
    )
    if values is None:
        return []
    if isinstance(values, Mapping):
        return list(values.values())
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        return list(values)
    return list(values)


def _optional_spec_collection(spec: Any, name: str) -> list[Any] | None:
    if isinstance(spec, Mapping):
        if name not in spec or spec[name] is None:
            return None
    elif not hasattr(spec, name) or getattr(spec, name) is None:
        return None
    return _spec_collection(spec, name)


def _allowed_action_names(spec: Any) -> set[str] | None:
    if isinstance(spec, Mapping):
        if "allowed_actions" not in spec:
            return None
        values = spec.get("allowed_actions")
    else:
        if not hasattr(spec, "allowed_actions"):
            return None
        values = getattr(spec, "allowed_actions")
    if values is None:
        return None
    result: set[str] = set()
    for value in values:
        if hasattr(value, "value"):
            value = value.value
        result.add(str(value))
    return result


def _strict_layout_enabled(spec: Any) -> bool:
    if isinstance(spec, Mapping):
        if "strict_layout" not in spec:
            return False
        value = spec.get("strict_layout")
    else:
        if not hasattr(spec, "strict_layout"):
            return False
        value = getattr(spec, "strict_layout")
    if type(value) is not bool:
        raise ValueError("strict_layout must be a boolean")
    return value


_TEXTUAL_DATE_PATTERN = re.compile(
    r"(?i)\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}(?:,\s*\d{4})?\b"
)
_BLANK_PLACEHOLDER_PATTERN = re.compile(r"_{2,}")


def _copy_translation_with_text(
    translation: Any,
    text: str,
    *,
    block: Any = None,
) -> Any:
    segments: list[Any] = []
    if _object_value(block, "protocol", "plain") == "tagged":
        segments = _remap_tagged_translation_segments(translation, text, block)
    copier = getattr(translation, "model_copy", None)
    if callable(copier):
        return copier(update={"translated_text": text, "segments": segments})
    if isinstance(translation, Mapping):
        result = dict(translation)
        result.update({"translated_text": text, "segments": segments})
        return result
    payload = _jsonable(translation)
    if isinstance(payload, Mapping):
        return {**payload, "translated_text": text, "segments": segments}
    raise TypeError("Translation cannot be copied for text replacement")


def _remap_tagged_translation_segments(
    translation: Any,
    replacement: str,
    block: Any,
) -> list[Any]:
    original = str(_object_value(translation, "translated_text", ""))
    segments = list(_object_value(translation, "segments", []) or [])
    expected = list(_object_value(block, "segments", []) or [])
    tags = [_object_value(segment, "segment_id") for segment in segments]
    expected_tags = [_object_value(segment, "segment_id") for segment in expected]
    if not segments or tags != expected_tags or any(tag is None for tag in tags):
        raise ValueError("tagged replacement requires the original run-tag mapping")
    if (
        "".join(str(_object_value(segment, "text", "")) for segment in segments)
        != original
    ):
        raise ValueError("tagged replacement segments do not match translated_text")

    protected = protect_text(
        original,
        extra_patterns={
            "textual_date": _TEXTUAL_DATE_PATTERN,
            "blank_placeholder": _BLANK_PLACEHOLDER_PATTERN,
        },
    )
    errors = validate_restored_values(protected.tokens, replacement)
    block_tokens = list(_object_value(block, "protected_tokens", []) or [])
    errors.extend(validate_restored_values(block_tokens, replacement))
    if errors:
        raise ValueError("unsafe tagged replacement: " + "; ".join(errors))

    source_texts = [str(_object_value(segment, "text", "")) for segment in segments]
    boundaries: list[int] = []
    offset = 0
    for segment_text in source_texts[:-1]:
        offset += len(segment_text)
        boundaries.append(_map_replacement_boundary(original, replacement, offset))
    boundaries = [max(0, min(len(replacement), value)) for value in boundaries]
    boundaries = list(_nondecreasing(boundaries))
    pieces: list[str] = []
    cursor = 0
    for boundary in [*boundaries, len(replacement)]:
        pieces.append(replacement[cursor:boundary])
        cursor = boundary
    source_boundaries = [0]
    for segment_text in source_texts:
        source_boundaries.append(source_boundaries[-1] + len(segment_text))
    for token in protected.tokens:
        first = next(
            (
                index
                for index in range(len(source_texts))
                if source_boundaries[index]
                <= token.start
                < source_boundaries[index + 1]
            ),
            None,
        )
        last = next(
            (
                index
                for index in range(len(source_texts))
                if source_boundaries[index] < token.end <= source_boundaries[index + 1]
            ),
            None,
        )
        if (
            first is not None
            and last is not None
            and token.value not in "".join(pieces[first : last + 1])
        ):
            raise ValueError(
                "unsafe tagged replacement: protected literal changed run-tag range "
                f"for {token.value!r}"
            )
    remapped = [
        _copy_segment_with_text(segment, piece)
        for segment, piece in zip(segments, pieces)
    ]
    if "".join(pieces) != replacement:
        raise ValueError("tagged replacement could not be mapped losslessly")
    return remapped


def _map_replacement_boundary(original: str, replacement: str, position: int) -> int:
    opcodes = SequenceMatcher(None, original, replacement, autojunk=False).get_opcodes()
    for _tag, old_start, old_end, new_start, new_end in opcodes:
        if old_start <= position <= old_end:
            if old_end == old_start:
                return new_end
            ratio = (position - old_start) / (old_end - old_start)
            return round(new_start + ratio * (new_end - new_start))
    return len(replacement)


def _nondecreasing(values: Sequence[int]) -> list[int]:
    result: list[int] = []
    floor = 0
    for value in values:
        floor = max(floor, value)
        result.append(floor)
    return result


def _copy_segment_with_text(segment: Any, text: str) -> Any:
    copier = getattr(segment, "model_copy", None)
    if callable(copier):
        return copier(update={"text": text})
    if isinstance(segment, Mapping):
        return {**segment, "text": text}
    payload = _jsonable(segment)
    if isinstance(payload, Mapping):
        return {**payload, "text": text}
    raise TypeError("Tagged translation segment cannot be copied")


def _object_value(value: Any, name: str, default: Any = None) -> Any:
    return (
        value.get(name, default)
        if isinstance(value, Mapping)
        else getattr(value, name, default)
    )


async def _invoke(target: Callable[..., Any], kwargs: Mapping[str, Any]) -> Any:
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        selected = dict(kwargs)
    else:
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        selected = (
            dict(kwargs)
            if accepts_kwargs
            else {
                key: value
                for key, value in kwargs.items()
                if key in signature.parameters
            }
        )
    if inspect.iscoroutinefunction(target):
        result = target(**selected)
    else:
        result = await asyncio.to_thread(target, **selected)
    if inspect.isawaitable(result):
        return await result
    return result


def _find_method(
    target: object | None,
    names: Sequence[str],
) -> Callable[..., Any] | None:
    if target is None:
        return None
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            return method
    return None


def _result_path(result: Any) -> Path | None:
    if result is None:
        return None
    if isinstance(result, (str, os.PathLike)):
        return Path(result)
    if isinstance(result, Mapping):
        for key in ("output_document", "output_path", "path"):
            if result.get(key) is not None:
                return Path(result[key])
    for name in (
        "output_document",
        "output_path",
        "path",
        "output_docx",
        "output_pptx",
    ):
        value = getattr(result, name, None)
        if value is not None:
            return Path(value)
    return None


def _nested_mapping_value(
    value: Any,
    key: str,
) -> Mapping[str, Sequence[int]] | None:
    if isinstance(value, Mapping):
        direct = value.get(key)
        if isinstance(direct, Mapping):
            return direct
        for child in value.values():
            found = _nested_mapping_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for child in value:
            found = _nested_mapping_value(child, key)
            if found is not None:
                return found
    return None


def _issue_dict(issue: Any) -> dict[str, Any]:
    if isinstance(issue, Mapping):
        return dict(_jsonable(issue))
    dumper = getattr(issue, "model_dump", None)
    if callable(dumper):
        return dict(_jsonable(dumper(mode="json")))
    if dataclasses.is_dataclass(issue):
        return dict(_jsonable(dataclasses.asdict(issue)))
    return {
        "code": str(getattr(issue, "code", "UNKNOWN_QA_ISSUE")),
        "message": str(getattr(issue, "message", repr(issue))),
        "severity": _jsonable(getattr(issue, "severity", "error")),
        "block_id": getattr(issue, "block_id", None),
        "page": getattr(issue, "page", None),
        "slide": getattr(issue, "slide", None),
    }


def _record_state(
    report: QaRepairReport,
    state: QaAgentState,
    round_number: int | None = None,
) -> None:
    report.status = state
    event: dict[str, Any] = {"state": state.value, "entered_at": _now()}
    if round_number is not None:
        event["round"] = round_number
    report.state_history.append(event)
    _persist(report)


def _finish(
    report: QaRepairReport,
    status: QaAgentState,
    *,
    failure: Mapping[str, Any] | None = None,
) -> None:
    report.failure = dict(failure) if failure is not None else None
    report.finished_at = _now()
    _record_state(report, status)


def _persist(report: QaRepairReport) -> None:
    atomic_write_json(report.report_path, report.to_dict())


def _atomic_restore(backup: Path, destination: Path) -> None:
    temporary = destination.parent / f".qa-restore-{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(backup, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _error_dict(error: BaseException) -> dict[str, Any]:
    return {"type": type(error).__name__, "message": str(error)}


def _provider_error_dict(
    *,
    stage: str,
    error: BaseException,
    fallback: str,
) -> dict[str, Any]:
    return {
        "stage": stage,
        **_error_dict(error),
        "fallback": fallback,
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return _jsonable(dumper(mode="json"))
    return repr(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_name(value: str) -> str:
    safe = "".join(character if character.isalnum() else "-" for character in value)
    return safe.strip("-")[:48] or "qa-job"
