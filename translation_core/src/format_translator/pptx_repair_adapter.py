"""Safe normalization and execution adapter for PPTX repair proposals."""

from __future__ import annotations

import dataclasses
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pptx_engine import (
    REPAIR_ACTIONS,
    execute_pptx_repairs,
    extract_translation_blocks,
    run_pptx_qa,
    structure_snapshot,
    validate_pptx,
)
from .pptx_rendered_layout_qa import (
    LayoutRunner,
    compare_pptx_rendered_layout,
    pptx_text_shape_targets,
)
from .utils import atomic_write_json, sha256_file


ENGINE_ACTIONS = frozenset(REPAIR_ACTIONS)
NORMALIZED_ACTIONS = ENGINE_ACTIONS
INPUT_ACTIONS = NORMALIZED_ACTIONS | frozenset(
    {
        "resolve_text_overflow",
        "adjust_shape_geometry",
        "set_font_size",
        "enable_autofit",
        "set_textbox_margins",
    }
)
ACTION_ALIASES = {
    "enable_autofit": "set_autofit",
    "set_textbox_margins": "adjust_margins",
}
MIN_FONT_SIZE_PT = 8.0
MAX_FONT_SIZE_PT = 72.0
MAX_FONT_SCALE = 1.0
MIN_MARGIN_SCALE = 0.5
MAX_MARGIN_SCALE = 1.0
MAX_RENDERED_EVIDENCE_ITEMS = 8
EMU_PER_POINT = 12_700
RENDERED_LAYOUT_ACTIONS = frozenset(
    {
        "resolve_text_overflow",
        "restore_table_row_heights",
        "set_font_size",
        "adjust_font_size",
        "set_autofit",
        "adjust_margins",
        "replace_translation",
        "set_shape_bounds",
        "adjust_shape_geometry",
        "restore_shape_coordinates",
    }
)
SYNTHETIC_ONLY_ACTIONS = frozenset({"restore_table_row_heights"})
STRICT_LAYOUT_ACTIONS = frozenset(
    {
        "replace_translation",
        "restore_table_row_heights",
    }
)
RENDERED_AUTHORITY_FATAL_CODES = frozenset(
    {
        "PPTX_RENDERED_LAYOUT_UNAVAILABLE",
        "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT",
        "PPTX_RENDERED_LAYOUT_CAPABILITY_MISMATCH",
        "PPTX_RENDERED_LAYOUT_PROVIDER_MISMATCH",
        "PPTX_RENDERED_LAYOUT_MEASUREMENT_FAILED",
        "PPTX_RENDERED_LAYOUT_TARGET_NOT_MEASURED",
        "PPTX_RENDERED_SLIDE_OBJECT_MEASUREMENT_FAILED",
        "PPTX_RENDERED_TABLE_GEOMETRY_CAPABILITY_INSUFFICIENT",
    }
)


class PptxRepairAdapterError(RuntimeError):
    """Raised when no safe, executable PPTX repair can be produced."""

    def __init__(
        self,
        message: str,
        *,
        rendered_layout_qa: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.rendered_layout_qa = (
            dict(rendered_layout_qa) if rendered_layout_qa is not None else None
        )


@dataclass(slots=True)
class PptxRepairAdaptation:
    normalized_actions: list[dict[str, Any]] = field(default_factory=list)
    rejected_actions: list[dict[str, Any]] = field(default_factory=list)
    max_actions: int = 100
    strict_layout: bool = False
    source_document: Path | None = None
    candidate_document: Path | None = None
    created_at: str = field(default_factory=lambda: _now())

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(dataclasses.asdict(self))


@dataclass(frozen=True, slots=True)
class _ShapeContext:
    slide: int
    shape_id: str
    source_shape: Mapping[str, Any] | None
    candidate_shape: Mapping[str, Any]
    slide_width: int
    slide_height: int
    source_document: Path


def adapt_pptx_repairs(
    source_document: str | os.PathLike[str],
    candidate_document: str | os.PathLike[str],
    *,
    issues: Iterable[Any] = (),
    actions: Iterable[Any] = (),
    max_actions: int = 100,
    validate_block_references: bool = True,
    strict_layout: bool = False,
) -> PptxRepairAdaptation:
    """Normalize plans and QA findings to an auditable, bounded whitelist."""

    source = _pptx_path(source_document, "source_document")
    candidate = _pptx_path(candidate_document, "candidate_document")
    if max_actions < 1 or max_actions > 100:
        raise ValueError("max_actions must be in the range 1..100")
    if type(strict_layout) is not bool:
        raise ValueError("strict_layout must be a boolean")

    source_snapshot = structure_snapshot(source)
    candidate_snapshot = structure_snapshot(candidate)
    context = _snapshot_context(source_snapshot, candidate_snapshot)
    known_blocks = {
        str(getattr(block, "block_id", ""))
        for block in extract_translation_blocks(candidate)
    }
    adaptation = PptxRepairAdaptation(
        max_actions=max_actions,
        strict_layout=strict_layout,
        source_document=source,
        candidate_document=candidate,
    )
    conflicts: dict[tuple[Any, ...], int] = {}

    explicit = list(actions)
    for index, raw in enumerate(explicit):
        _normalize_one(
            raw,
            source=f"plan[{index}]",
            context=context,
            known_blocks=known_blocks,
            adaptation=adaptation,
            conflicts=conflicts,
            synthetic=False,
            validate_block_references=validate_block_references,
            strict_layout=strict_layout,
        )

    indexed_issues = list(enumerate(issues))
    if strict_layout:
        indexed_issues = _prioritize_strict_issues(indexed_issues)
    for index, issue in indexed_issues:
        proposal = _default_issue_action(issue, context)
        if proposal is None:
            continue
        if isinstance(proposal, Mapping) and proposal.get("_reject_reason"):
            adaptation.rejected_actions.append(
                _rejection(
                    source=f"issue[{index}]",
                    action=proposal.get("action"),
                    code="MANUAL_REVIEW_REQUIRED",
                    reason=str(proposal["_reject_reason"]),
                    payload=_issue_dict(issue),
                )
            )
            continue
        _normalize_one(
            proposal,
            source=f"issue[{index}]",
            context=context,
            known_blocks=known_blocks,
            adaptation=adaptation,
            conflicts=conflicts,
            synthetic=True,
            validate_block_references=validate_block_references,
            strict_layout=strict_layout,
        )
    return adaptation


def execute_pptx_repair_adapter(
    source_document: str | os.PathLike[str],
    candidate_document: str | os.PathLike[str],
    output_document: str | os.PathLike[str],
    *,
    issues: Iterable[Any] = (),
    actions: Iterable[Any] = (),
    audit_path: str | os.PathLike[str] | None = None,
    max_actions: int = 100,
    layout_runner: LayoutRunner | None = None,
    rendered_layout_required: bool = False,
    strict_layout: bool = False,
) -> dict[str, Any]:
    """Normalize and execute safe actions without overwriting either input."""

    source = _pptx_path(source_document, "source_document")
    candidate = _pptx_path(candidate_document, "candidate_document")
    output = Path(output_document).expanduser().resolve()
    if output.suffix.lower() != ".pptx":
        raise ValueError("output_document must end with .pptx")
    if output in {source, candidate}:
        raise ValueError("output_document must differ from both inputs")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite PPTX candidate: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    audit_destination: Path | None = None
    if audit_path is not None:
        audit_destination = Path(audit_path).expanduser().resolve()
        if audit_destination in {source, candidate, output}:
            raise ValueError("audit_path must differ from all PPTX paths")
        if audit_destination.exists():
            raise FileExistsError(
                f"Refusing to overwrite repair audit: {audit_destination}"
            )

    issue_list = list(issues)
    action_list = list(actions)
    baseline_rendered_layout_qa: dict[str, Any] | None = None
    rendered_targets: list[dict[str, object]] | None = None
    if strict_layout and any(
        str(_issue_dict(issue).get("code", "")).upper().startswith(
            "PPTX_RENDERED_"
        )
        for issue in issue_list
    ):
        rendered_targets = pptx_text_shape_targets(candidate)
        baseline_rendered_layout_qa = compare_pptx_rendered_layout(
            source,
            candidate,
            rendered_targets,
            runner=layout_runner,
            required=True,
        )
        issue_list = _merge_live_rendered_issues(
            issue_list,
            baseline_rendered_layout_qa["issues"],
        )
    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=issue_list,
        actions=action_list,
        max_actions=max_actions,
        strict_layout=strict_layout,
    )
    if not adaptation.normalized_actions:
        raise PptxRepairAdapterError("No safe PPTX actions remain after normalization")

    baseline_source_qa_issues = (
        [_issue_dict(issue) for issue in run_pptx_qa(source, candidate)]
        if strict_layout
        else []
    )
    executed_actions = list(adaptation.normalized_actions)
    rendered_regression_recovery: dict[str, Any] | None = None

    try:
        (
            engine_result,
            source_qa_issues,
            blocking_source_errors,
            validation,
        ) = _execute_validated_repair_attempt(
            source=source,
            candidate=candidate,
            output=output,
            actions=executed_actions,
            baseline_source_qa_issues=baseline_source_qa_issues,
            strict_layout=strict_layout,
        )
        if rendered_targets is None:
            rendered_targets = _rendered_layout_targets(
                executed_actions,
                output,
            )
        if baseline_rendered_layout_qa is None:
            baseline_rendered_layout_qa = compare_pptx_rendered_layout(
                source,
                candidate,
                rendered_targets,
                runner=layout_runner,
                required=(
                    rendered_layout_required
                    or _requires_table_runtime_geometry(
                        executed_actions
                    )
                    or any(
                        action.get("rendered_load_ratio") is not None
                        for action in executed_actions
                    )
                ),
            )
        rendered_layout_qa, blocking_rendered_errors = _run_rendered_layout_gate(
            source=source,
            output=output,
            targets=rendered_targets,
            actions=executed_actions,
            baseline=baseline_rendered_layout_qa,
            layout_runner=layout_runner,
            rendered_layout_required=rendered_layout_required,
            strict_layout=strict_layout,
        )
        recovery_actions, excluded_targets = (
            _rendered_regression_recovery_actions(
                executed_actions,
                blocking_rendered_errors,
                candidate,
            )
            if strict_layout
            else ([], [])
        )
        if blocking_rendered_errors and recovery_actions:
            initial_blocking_rendered_errors = list(blocking_rendered_errors)
            output.unlink(missing_ok=True)
            executed_actions = recovery_actions
            (
                engine_result,
                source_qa_issues,
                blocking_source_errors,
                validation,
            ) = _execute_validated_repair_attempt(
                source=source,
                candidate=candidate,
                output=output,
                actions=executed_actions,
                baseline_source_qa_issues=baseline_source_qa_issues,
                strict_layout=strict_layout,
            )
            rendered_layout_qa, blocking_rendered_errors = (
                _run_rendered_layout_gate(
                    source=source,
                    output=output,
                    targets=rendered_targets,
                    actions=executed_actions,
                    baseline=baseline_rendered_layout_qa,
                    layout_runner=layout_runner,
                    rendered_layout_required=rendered_layout_required,
                    strict_layout=strict_layout,
                )
            )
            rendered_regression_recovery = {
                "attempted": True,
                "initial_blocking_issue_count": len(
                    initial_blocking_rendered_errors
                ),
                "excluded_targets": excluded_targets,
                "remaining_blocking_issue_count": len(blocking_rendered_errors),
            }
        if blocking_rendered_errors:
            codes = sorted(
                {
                    str(issue.get("code", "UNKNOWN_RENDERED_LAYOUT_ERROR"))
                    for issue in blocking_rendered_errors
                }
            )
            if audit_destination is not None:
                atomic_write_json(
                    audit_destination,
                    {
                        "operation": "pptx_repair_adapter",
                        "status": "rendered_layout_qa_failed",
                        "source_document": str(source),
                        "candidate_document": str(candidate),
                        "output_document": str(output),
                        "source_sha256": sha256_file(source),
                        "candidate_sha256": sha256_file(candidate),
                        "output_sha256": sha256_file(output),
                        "strict_layout": strict_layout,
                        "normalized_actions": executed_actions,
                        "rejected_actions": adaptation.rejected_actions,
                        "engine_result": _jsonable(engine_result),
                        "baseline_source_qa_issues": baseline_source_qa_issues,
                        "source_qa_issues": source_qa_issues,
                        "blocking_source_qa_issues": blocking_source_errors,
                        "baseline_rendered_layout_qa": baseline_rendered_layout_qa,
                        "rendered_layout_qa": rendered_layout_qa,
                        "blocking_rendered_layout_issues": blocking_rendered_errors,
                        "rendered_regression_recovery": (
                            rendered_regression_recovery
                        ),
                        "validation": _jsonable(validation),
                        "created_at": _now(),
                    },
                )
            raise PptxRepairAdapterError(
                "Rendered PPTX layout QA failed: " + ", ".join(codes),
                rendered_layout_qa=rendered_layout_qa,
            )
    except Exception:
        output.unlink(missing_ok=True)
        raise

    audit = {
        "operation": "pptx_repair_adapter",
        "source_document": str(source),
        "candidate_document": str(candidate),
        "output_document": str(output),
        "source_sha256": sha256_file(source),
        "candidate_sha256": sha256_file(candidate),
        "output_sha256": sha256_file(output),
        "strict_layout": strict_layout,
        "normalized_actions": executed_actions,
        "rejected_actions": adaptation.rejected_actions,
        "engine_result": _jsonable(engine_result),
        "baseline_source_qa_issues": baseline_source_qa_issues,
        "source_qa_issues": source_qa_issues,
        "blocking_source_qa_issues": blocking_source_errors,
        "baseline_rendered_layout_qa": baseline_rendered_layout_qa,
        "rendered_layout_qa": rendered_layout_qa,
        "blocking_rendered_layout_issues": blocking_rendered_errors,
        "rendered_regression_recovery": rendered_regression_recovery,
        "validation": _jsonable(validation),
        "created_at": _now(),
    }
    if audit_destination is not None:
        atomic_write_json(audit_destination, audit)
        audit["audit_path"] = str(audit_destination)
    return {
        "output_path": str(output),
        "normalized_actions": executed_actions,
        "rejected_actions": adaptation.rejected_actions,
        "audit": audit,
        "qa_issues": source_qa_issues,
        "rendered_layout_qa": rendered_layout_qa,
        "validation": validation,
    }


def _execute_validated_repair_attempt(
    *,
    source: Path,
    candidate: Path,
    output: Path,
    actions: Sequence[Mapping[str, Any]],
    baseline_source_qa_issues: Sequence[Mapping[str, Any]],
    strict_layout: bool,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    Mapping[str, Any],
]:
    engine_result = execute_pptx_repairs(
        candidate,
        output,
        actions,
        strict=False,
    )
    if not engine_result.get("applied"):
        raise PptxRepairAdapterError(
            "No safe PPTX action could be applied; all location-scoped "
            "repairs require manual review"
        )
    engine_qa_issues = [
        _issue_dict(issue) for issue in engine_result.get("qa_issues", [])
    ]
    source_qa_issues = [
        _issue_dict(issue)
        for issue in run_pptx_qa(
            source,
            output,
            authorized_coordinate_changes=(
                engine_result.get("audit", {}).get("authorized_coordinate_changes")
            ),
        )
    ]
    engine_errors = [
        issue
        for issue in engine_qa_issues
        if _severity_value(issue.get("severity", "error")) == "error"
    ]
    source_errors = [
        issue
        for issue in source_qa_issues
        if _severity_value(issue.get("severity", "error")) == "error"
    ]
    blocking_source_errors = (
        _new_error_issues(baseline_source_qa_issues, source_qa_issues)
        if strict_layout
        else source_errors
    )
    blocking_errors = [*engine_errors, *blocking_source_errors]
    if blocking_errors:
        codes = sorted(
            {
                str(issue.get("code", "UNKNOWN_PPTX_QA_ERROR"))
                for issue in blocking_errors
            }
        )
        raise PptxRepairAdapterError(
            "Low-level PPTX repair failed its QA gate: " + ", ".join(codes)
        )
    validation = engine_result.get("validation") or validate_pptx(output)
    if not validation.get("valid"):
        raise PptxRepairAdapterError(
            "Repaired PPTX failed validation: "
            + "; ".join(validation.get("errors", []))
        )
    return (
        dict(engine_result),
        source_qa_issues,
        blocking_source_errors,
        validation,
    )


def _run_rendered_layout_gate(
    *,
    source: Path,
    output: Path,
    targets: Sequence[Mapping[str, object]],
    actions: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Any],
    layout_runner: LayoutRunner | None,
    rendered_layout_required: bool,
    strict_layout: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rendered_layout_qa = dict(
        compare_pptx_rendered_layout(
            source,
            output,
            list(targets),
            runner=layout_runner,
            required=(
                rendered_layout_required
                or _requires_table_runtime_geometry(actions)
                or any(
                    action.get("rendered_load_ratio") is not None
                    for action in actions
                )
            ),
        )
    )
    rendered_layout_qa["issues"] = list(rendered_layout_qa.get("issues", []))
    if _requires_table_runtime_geometry(
        actions
    ) and "table_runtime_geometry" not in rendered_layout_qa.get(
        "capabilities", []
    ):
        rendered_layout_qa["issues"].append(
            {
                "code": "PPTX_RENDERED_TABLE_GEOMETRY_CAPABILITY_INSUFFICIENT",
                "severity": "error",
                "message": (
                    "Table row-height repair requires a rendered-layout "
                    "provider with table runtime geometry measurements."
                ),
                "details": {
                    "provider": rendered_layout_qa.get("provider"),
                    "capabilities": rendered_layout_qa.get("capabilities", []),
                    "required_capability": "table_runtime_geometry",
                },
            }
        )
    rendered_errors = [
        issue
        for issue in rendered_layout_qa["issues"]
        if _severity_value(issue.get("severity", "error")) == "error"
    ]
    blocking = (
        _new_or_worsened_rendered_errors(
            list(baseline.get("issues", [])),
            rendered_errors,
        )
        if strict_layout
        else rendered_errors
    )
    return rendered_layout_qa, blocking


def _rendered_regression_recovery_actions(
    actions: Sequence[Mapping[str, Any]],
    blocking_issues: Sequence[Mapping[str, Any]],
    document: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop only actions targeting rendered regressions, leaving safe work intact."""

    if not blocking_issues or any(
        str(issue.get("code", "")).upper() in RENDERED_AUTHORITY_FATAL_CODES
        for issue in blocking_issues
    ):
        return [], []
    targets: set[tuple[int, str]] = set()
    for issue in blocking_issues:
        details = issue.get("details")
        details = details if isinstance(details, Mapping) else {}
        slide = issue.get("slide", details.get("slide"))
        shape_id = details.get("shape_id", issue.get("shape_id"))
        try:
            normalized_slide = int(slide)
        except (TypeError, ValueError):
            continue
        if isinstance(slide, bool) or shape_id is None:
            continue
        normalized_shape_id = str(shape_id).strip()
        if normalized_slide < 1 or not normalized_shape_id:
            continue
        targets.add((normalized_slide, normalized_shape_id))
    if not targets:
        return [], []

    block_targets: dict[str, tuple[int, str]] = {}
    if any(
        str(action.get("action", "")) == "replace_translation"
        for action in actions
    ):
        for block in extract_translation_blocks(document):
            context = getattr(block, "context", {})
            if not isinstance(context, Mapping):
                continue
            slide = context.get("slide_number")
            shape_id = context.get("shape_id")
            try:
                normalized_slide = int(slide)
            except (TypeError, ValueError):
                continue
            if isinstance(slide, bool) or shape_id is None:
                continue
            normalized_shape_id = str(shape_id).strip()
            if normalized_slide < 1 or not normalized_shape_id:
                continue
            block_targets[str(getattr(block, "block_id", ""))] = (
                normalized_slide,
                normalized_shape_id,
            )

    kept: list[dict[str, Any]] = []
    removed = False
    for action in actions:
        key = block_targets.get(str(action.get("block_id", "")))
        if key is None:
            try:
                key = (
                    int(action.get("slide")),
                    str(action.get("shape_id", "")).strip(),
                )
            except (TypeError, ValueError):
                key = (-1, "")
        if key in targets:
            removed = True
            continue
        kept.append(dict(action))
    if not removed or not kept:
        return [], []
    excluded_targets = [
        {"slide": slide, "shape_id": shape_id}
        for slide, shape_id in sorted(targets)
    ]
    return kept, excluded_targets


def _rendered_layout_targets(
    actions: Sequence[Mapping[str, Any]],
    document: Path,
) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    blocks = {
        str(block.block_id): block for block in extract_translation_blocks(document)
    }
    snapshot = structure_snapshot(document)
    rendered_shapes = {
        (int(slide_data["number"]), str(shape["shape_id"])): shape
        for slide_data in snapshot["slides"]
        for shape in slide_data["shapes"]
    }
    for action in actions:
        action_name = str(action.get("action", ""))
        if action_name not in RENDERED_LAYOUT_ACTIONS:
            continue
        slide = action.get("slide")
        shape_id = action.get("shape_id")
        if (slide is None or shape_id is None) and action.get(
            "action"
        ) == "replace_translation":
            block = blocks.get(str(action.get("block_id", "")))
            context = getattr(block, "context", {}) if block is not None else {}
            if isinstance(context, Mapping):
                slide = context.get("slide_number")
                shape_id = context.get("shape_id")
        if slide is None or shape_id is None:
            continue
        slide = int(slide)
        shape_id = str(shape_id)
        key = (slide, shape_id)
        shape = rendered_shapes.get(key)
        if shape is None:
            continue
        if action_name == "restore_table_row_heights":
            if shape.get("shape_type") != "graphic_frame":
                continue
        elif not (
            shape.get("shape_type") == "shape" and str(shape.get("text", "")).strip()
        ):
            continue
        if key in seen:
            continue
        seen.add(key)
        targets.append({"slide": slide, "shape_id": shape_id})
    return targets


def _requires_table_runtime_geometry(
    actions: Sequence[Mapping[str, Any]],
) -> bool:
    return any(
        str(action.get("action", "")) == "restore_table_row_heights"
        for action in actions
    )


def _normalize_one(
    raw: Any,
    *,
    source: str,
    context: Mapping[tuple[int, str], _ShapeContext],
    known_blocks: set[str],
    adaptation: PptxRepairAdaptation,
    conflicts: dict[tuple[Any, ...], int],
    synthetic: bool,
    validate_block_references: bool,
    strict_layout: bool,
) -> None:
    payload = _mapping(raw)
    name = _action_name(payload)
    requested_name = name
    name = ACTION_ALIASES.get(str(name), name)
    if name not in INPUT_ACTIONS:
        adaptation.rejected_actions.append(
            _rejection(
                source=source,
                action=name,
                code="UNKNOWN_ACTION",
                reason=f"Action is outside the PPTX repair whitelist: {name!r}",
                payload=payload,
            )
        )
        return
    trusted_rendered_repair = (
        synthetic
        and name == "resolve_text_overflow"
        and payload.get("_trusted_rendered_layout") is True
    )
    if (
        strict_layout
        and name not in STRICT_LAYOUT_ACTIONS
        and not trusted_rendered_repair
    ):
        adaptation.rejected_actions.append(
            _rejection(
                source=source,
                action=name,
                code="STRICT_LAYOUT_ACTION_FORBIDDEN",
                reason=(
                    "Strict layout permits text replacement, trusted table "
                    "row-height restoration, and issue-scoped rendered text "
                    "repairs only"
                ),
                payload=payload,
            )
        )
        return
    if name in SYNTHETIC_ONLY_ACTIONS and not synthetic:
        adaptation.rejected_actions.append(
            _rejection(
                source=source,
                action=name,
                code="SYNTHETIC_ONLY_ACTION",
                reason=(
                    f"Action {name!r} may only be synthesized from a trusted "
                    "rendered-layout QA issue"
                ),
                payload=payload,
            )
        )
        return
    try:
        normalized, conflict_key = _normalize_payload(
            payload,
            name=name,
            context=context,
            known_blocks=known_blocks,
            validate_block_references=validate_block_references,
        )
    except (TypeError, ValueError) as error:
        adaptation.rejected_actions.append(
            _rejection(
                source=source,
                action=name,
                code="INVALID_ACTION",
                reason=str(error),
                payload=payload,
            )
        )
        return

    conflicting = _conflicting_key(conflict_key, conflicts)
    if conflicting is not None:
        existing_action = adaptation.normalized_actions[conflicts[conflicting]]
        if (
            name == "resolve_text_overflow"
            and existing_action.get("action") == "resolve_text_overflow"
        ):
            _merge_overflow_actions(existing_action, normalized, source=source)
            return
        if synthetic:
            return
        adaptation.rejected_actions.append(
            _rejection(
                source=source,
                action=name,
                code="SHAPE_ACTION_CONFLICT",
                reason=(
                    "Another action in this round already targets the same "
                    "shape and repair property"
                ),
                payload=payload,
            )
        )
        return
    if len(adaptation.normalized_actions) >= adaptation.max_actions:
        adaptation.rejected_actions.append(
            _rejection(
                source=source,
                action=name,
                code="ACTION_LIMIT_EXCEEDED",
                reason=f"At most {adaptation.max_actions} actions are allowed",
                payload=payload,
            )
        )
        return
    conflicts[conflict_key] = len(adaptation.normalized_actions)
    normalized["adapter_source"] = source
    if requested_name != name:
        normalized["requested_action_type"] = requested_name
    adaptation.normalized_actions.append(normalized)


def _conflicting_key(
    key: tuple[Any, ...],
    existing: Mapping[tuple[Any, ...], int],
) -> tuple[Any, ...] | None:
    return key if key in existing else None


def _merge_overflow_actions(
    existing: dict[str, Any],
    incoming: Mapping[str, Any],
    *,
    source: str,
) -> None:
    """Merge compatible evidence for one shape into one auditable cascade."""

    existing["target_load_ratio"] = min(
        float(existing.get("target_load_ratio", 0.92)),
        float(incoming.get("target_load_ratio", 0.92)),
    )
    existing["margin_scale"] = min(
        float(existing.get("margin_scale", 0.75)),
        float(incoming.get("margin_scale", 0.75)),
    )
    existing["allow_expand"] = bool(existing.get("allow_expand", True)) and bool(
        incoming.get("allow_expand", True)
    )
    existing["min_font_size_pt"] = max(
        float(existing.get("min_font_size_pt", MIN_FONT_SIZE_PT)),
        float(incoming.get("min_font_size_pt", MIN_FONT_SIZE_PT)),
    )
    rendered_loads = [
        float(value)
        for value in (
            existing.get("rendered_load_ratio"),
            incoming.get("rendered_load_ratio"),
        )
        if value is not None
    ]
    if rendered_loads:
        existing["rendered_load_ratio"] = max(rendered_loads)
    rendered_targets = [
        float(value)
        for value in (
            existing.get("rendered_target_load_ratio"),
            incoming.get("rendered_target_load_ratio"),
        )
        if value is not None
    ]
    if rendered_targets:
        existing["rendered_target_load_ratio"] = min(rendered_targets)
    autofit_scales = [
        int(value)
        for value in (
            existing.get("autofit_font_scale"),
            incoming.get("autofit_font_scale"),
        )
        if value is not None
    ]
    if autofit_scales:
        existing["autofit_font_scale"] = max(autofit_scales)
    preserve_modes = [
        bool(value)
        for value in (
            existing.get("preserve_autofit_mode"),
            incoming.get("preserve_autofit_mode"),
        )
        if value is not None
    ]
    if preserve_modes:
        existing["preserve_autofit_mode"] = all(preserve_modes)

    sources = list(existing.get("adapter_sources") or [existing["adapter_source"]])
    if source not in sources:
        sources.append(source)
    existing["adapter_sources"] = sources
    if incoming.get("trusted_rendered_evidence") is True:
        existing["trusted_rendered_evidence"] = True
    issue_codes: list[str] = []
    evidence_items: list[dict[str, Any]] = []
    truncated_evidence_count = 0
    for action in (existing, incoming):
        value = action.get("rendered_evidence")
        if isinstance(value, Mapping):
            issue_codes.extend(str(code) for code in value.get("issue_codes", []))
            items = value.get("evidence_items")
            if isinstance(items, Sequence) and not isinstance(
                items,
                (str, bytes, bytearray),
            ):
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    normalized_item = _jsonable(item)
                    if normalized_item in evidence_items:
                        continue
                    if len(evidence_items) >= MAX_RENDERED_EVIDENCE_ITEMS:
                        truncated_evidence_count += 1
                        continue
                    evidence_items.append(normalized_item)
            try:
                truncated_evidence_count += max(
                    0,
                    int(value.get("truncated_evidence_count", 0)),
                )
            except (TypeError, ValueError):
                pass
    if issue_codes:
        existing["rendered_evidence"] = {
            "issue_codes": sorted(set(issue_codes)),
            "evidence_items": evidence_items,
        }
        if truncated_evidence_count:
            existing["rendered_evidence"]["truncated_evidence_count"] = (
                truncated_evidence_count
            )


def _normalize_payload(
    payload: Mapping[str, Any],
    *,
    name: str,
    context: Mapping[tuple[int, str], _ShapeContext],
    known_blocks: set[str],
    validate_block_references: bool,
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    if name in {"replace_translation"}:
        block_id = str(payload.get("block_id", "") or "")
        text = payload.get("text", payload.get("proposed_text"))
        if not block_id or (validate_block_references and block_id not in known_blocks):
            raise ValueError(f"replace_translation has unknown block_id {block_id!r}")
        if not isinstance(text, str) or not text:
            raise ValueError("replace_translation requires non-empty text")
        action = {
            "action": "replace_translation",
            "block_id": block_id,
            "text": text,
        }
        if payload.get("segments") is not None:
            action["segments"] = _jsonable(payload["segments"])
        return action, ("block", block_id, "text")

    shape = _shape_context(payload, context)
    base = {"slide": shape.slide, "shape_id": shape.shape_id}
    if name == "restore_table_row_heights":
        _require_table_graphic_frame(shape)
        rows = _normalize_table_rows(payload.get("rows"))
        return {
            "action": "restore_table_row_heights",
            **base,
            "rows": rows,
        }, (
            "shape",
            shape.slide,
            shape.shape_id,
            "table_row_heights",
        )
    if name == "resolve_text_overflow":
        _require_text_shape(shape)
        target_load = _number(
            payload.get("target_load_ratio", 0.92),
            "target_load_ratio",
        )
        if not 0.5 <= target_load <= 1.0:
            raise ValueError("target_load_ratio must be between 0.5 and 1.0")
        margin_scale = _number(
            payload.get("margin_scale", 0.75),
            "margin_scale",
        )
        if not MIN_MARGIN_SCALE <= margin_scale <= MAX_MARGIN_SCALE:
            raise ValueError("margin_scale must be between 0.5 and 1.0")
        minimum_font = _number(
            _first_present(
                payload,
                ("min_font_size_pt", "minimum_font_size_pt"),
            )
            or MIN_FONT_SIZE_PT,
            "min_font_size_pt",
        )
        if not MIN_FONT_SIZE_PT <= minimum_font <= MAX_FONT_SIZE_PT:
            raise ValueError("min_font_size_pt must be between 8 and 72 points")
        normalized = {
            "action": "resolve_text_overflow",
            **base,
            "target_load_ratio": target_load,
            "margin_scale": margin_scale,
            "allow_expand": _boolean(payload.get("allow_expand", True), "allow_expand"),
            "min_font_size_pt": minimum_font,
            **(
                {
                    "rendered_load_ratio": _number(
                        payload["rendered_load_ratio"],
                        "rendered_load_ratio",
                    )
                }
                if payload.get("rendered_load_ratio") is not None
                else {}
            ),
        }
        if payload.get("rendered_target_load_ratio") is not None:
            rendered_target = _number(
                payload["rendered_target_load_ratio"],
                "rendered_target_load_ratio",
            )
            rendered_load = normalized.get("rendered_load_ratio")
            if not 0.5 <= rendered_target <= 10.0 or (
                rendered_load is not None
                and rendered_target > float(rendered_load) + 0.001
            ):
                raise ValueError(
                    "rendered_target_load_ratio must be between 0.5 and 10 "
                    "and must not exceed rendered_load_ratio"
                )
            normalized["rendered_target_load_ratio"] = rendered_target
        if payload.get("autofit_font_scale") is not None:
            autofit_font_scale = _integer(
                payload["autofit_font_scale"],
                "autofit_font_scale",
            )
            if not 50_000 <= autofit_font_scale <= 100_000:
                raise ValueError(
                    "autofit_font_scale must be between 50000 and 100000"
                )
            normalized["autofit_font_scale"] = autofit_font_scale
        if payload.get("preserve_autofit_mode") is not None:
            normalized["preserve_autofit_mode"] = _boolean(
                payload["preserve_autofit_mode"],
                "preserve_autofit_mode",
            )
        if payload.get("_trusted_rendered_layout") is True:
            normalized["trusted_rendered_evidence"] = True
            evidence = payload.get("_rendered_evidence")
            if isinstance(evidence, Mapping):
                normalized["rendered_evidence"] = _jsonable(evidence)
        return normalized, ("shape", shape.slide, shape.shape_id, "overflow")
    if name == "set_autofit":
        _require_text_shape(shape)
        return {"action": "set_autofit", **base}, (
            "shape",
            shape.slide,
            shape.shape_id,
            "autofit",
        )
    if name == "adjust_margins":
        _require_text_shape(shape)
        scale = _number(payload.get("scale", 0.75), "margin scale")
        if not MIN_MARGIN_SCALE <= scale <= MAX_MARGIN_SCALE:
            raise ValueError("margin scale must be between 0.5 and 1.0")
        return {"action": "adjust_margins", **base, "scale": scale}, (
            "shape",
            shape.slide,
            shape.shape_id,
            "margins",
        )
    if name in {"set_font_size", "adjust_font_size"}:
        _require_text_shape(shape)
        action = _font_action(payload, shape, name)
        return action, ("shape", shape.slide, shape.shape_id, "font_size")
    if name == "restore_shape_coordinates":
        return {
            "action": "restore_shape_coordinates",
            **base,
            "source_pptx": str(shape.source_document),
        }, ("shape", shape.slide, shape.shape_id, "geometry")
    if name in {"set_shape_bounds", "adjust_shape_geometry"}:
        bounds = _geometry_bounds(payload, shape)
        return {
            "action": "set_shape_bounds",
            **base,
            "bounds_emu": bounds,
            "clamp_to_slide": _boolean(
                payload.get("clamp_to_slide", False),
                "clamp_to_slide",
            ),
        }, (
            "shape",
            shape.slide,
            shape.shape_id,
            "geometry",
        )
    raise ValueError(f"Unsupported normalized action {name!r}")


def _prioritize_strict_issues(
    indexed_issues: Sequence[tuple[int, Any]],
) -> list[tuple[int, Any]]:
    """Rank repair targets while preserving evidence order within each shape."""

    groups: dict[tuple[Any, ...], list[tuple[int, Any]]] = {}
    for index, issue in indexed_issues:
        data = _issue_dict(issue)
        details = data.get("details")
        details = details if isinstance(details, Mapping) else {}
        slide = data.get("slide", details.get("slide"))
        shape_id = details.get("shape_id", data.get("shape_id"))
        try:
            normalized_slide = int(slide)
        except (TypeError, ValueError):
            normalized_slide = 0
        normalized_shape_id = str(shape_id).strip() if shape_id is not None else ""
        key = (
            ("shape", normalized_slide, normalized_shape_id)
            if normalized_slide > 0 and normalized_shape_id
            else ("issue", index)
        )
        groups.setdefault(key, []).append((index, issue))

    ranked_groups = sorted(
        groups.values(),
        key=lambda group: min(
            _strict_issue_repair_priority(issue, index)
            for index, issue in group
        ),
    )
    return [entry for group in ranked_groups for entry in group]


def _strict_issue_repair_priority(issue: Any, original_index: int) -> tuple[Any, ...]:
    """Prefer bounded rendered fixes that need the smallest visual adjustment."""

    data = _issue_dict(issue)
    code = str(data.get("code", "")).upper()
    details = data.get("details")
    details = details if isinstance(details, Mapping) else {}
    hint = details.get("repair_hint")
    hint = hint if isinstance(hint, Mapping) else {}
    if code == "PPTX_RENDERED_TABLE_GEOMETRY_CHANGED":
        return (0, 0.0, original_index)
    if code == "PPTX_RENDERED_TEXT_OVERFLOW":
        if (
            _action_name(hint) != "resolve_text_overflow"
            or hint.get("automatic") is not True
        ):
            return (3, float("inf"), original_index)
        try:
            load = float(details.get("rendered_load_ratio"))
        except (TypeError, ValueError):
            return (3, float("inf"), original_index)
        if not math.isfinite(load):
            return (3, float("inf"), original_index)
        return (1, load, original_index)
    if code == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP":
        try:
            reference = max(
                0.0,
                float(details.get("reference_overlap_area_pt2", 0.0)),
            )
            candidate = max(
                0.0,
                float(details.get("candidate_overlap_area_pt2", 0.0)),
            )
            tolerance = max(
                0.0,
                float(details.get("worsening_tolerance_pt2", 0.0)),
            )
        except (TypeError, ValueError):
            return (3, float("inf"), original_index)
        allowed = reference + tolerance
        if allowed <= 0.0 or candidate <= allowed:
            return (3, float("inf"), original_index)
        load = math.sqrt(candidate / allowed)
        if not math.isfinite(load) or load > 10.0:
            return (3, float("inf"), original_index)
        return (1, load, original_index)
    return (2, 0.0, original_index)


def _default_issue_action(
    issue: Any,
    context: Mapping[tuple[int, str], _ShapeContext],
) -> Mapping[str, Any] | None:
    data = _issue_dict(issue)
    code = str(data.get("code", "")).upper()
    details = data.get("details")
    details = details if isinstance(details, Mapping) else {}
    hint = details.get("repair_hint")
    hint = hint if isinstance(hint, Mapping) else {}
    slide = data.get("slide", hint.get("slide"))
    shape_id = details.get("shape_id", hint.get("shape_id"))
    severity = _severity_value(data.get("severity", "error"))
    base = {"slide": slide, "shape_id": shape_id}

    if code == "PPTX_RENDERED_TABLE_GEOMETRY_CHANGED":
        return _rendered_table_restore_issue_action(
            details=details,
            hint=hint,
            slide=slide,
            shape_id=shape_id,
        )

    if code == "PPTX_SHAPE_COORDINATE_CHANGED":
        return {"action": "restore_shape_coordinates", **base}
    if code == "PPTX_SHAPE_OUT_OF_BOUNDS":
        shape = context.get(_shape_key(slide, shape_id))
        if shape is None:
            return {
                "action": "set_shape_bounds",
                **base,
                "_reject_reason": "Out-of-bounds issue does not identify a valid shape",
            }
        if shape.source_shape and shape.source_shape.get("bbox_emu") is not None:
            return {"action": "restore_shape_coordinates", **base}
        candidate_bbox = shape.candidate_shape.get("bbox_emu")
        if candidate_bbox is None:
            return {
                "action": "set_shape_bounds",
                **base,
                "_reject_reason": "Shape has no clampable bounds",
            }
        return {
            "action": "set_shape_bounds",
            **base,
            "bbox_emu": _clamp_bbox(
                _bbox(candidate_bbox, "candidate shape bounds"),
                shape.slide_width,
                shape.slide_height,
            ),
        }
    if code == "PPTX_RENDERED_TEXT_OVERFLOW":
        return _trusted_rendered_overflow_action(
            details=details,
            hint=hint,
            slide=slide,
            shape_id=shape_id,
            severity=severity,
        )
    if code == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP":
        return _trusted_rendered_overlap_action(
            details=details,
            slide=slide,
            shape_id=shape_id,
            severity=severity,
        )
    if code in {
        "PPTX_TEXT_CAPACITY_RISK",
        "PPTX_TRANSLATION_EXPANSION_RISK",
    }:
        return {"action": "resolve_text_overflow", **base}
    if code == "PPTX_SUSPECTED_SHAPE_OVERLAP":
        bounds = _first_present(
            details,
            ("safe_bounds", "target_bbox", "target_bbox_emu"),
        )
        if bounds is None:
            return {
                "action": "set_shape_bounds",
                **base,
                "_reject_reason": (
                    "Overlap requires explicit safe_bounds or target_bbox; "
                    "automatic geometry guessing is forbidden"
                ),
            }
        return {"action": "set_shape_bounds", **base, "bbox_emu": bounds}
    return None


def _trusted_rendered_overflow_action(
    *,
    details: Mapping[str, Any],
    hint: Mapping[str, Any],
    slide: Any,
    shape_id: Any,
    severity: str,
) -> Mapping[str, Any]:
    base = {
        "action": "resolve_text_overflow",
        "slide": slide,
        "shape_id": shape_id,
    }

    def review(reason: str) -> Mapping[str, Any]:
        return {**base, "_reject_reason": reason}

    if _action_name(hint) != "resolve_text_overflow" or hint.get(
        "automatic"
    ) is not True:
        return review(
            "Rendered overflow issue has no trusted automatic repair hint"
        )
    if str(hint.get("shape_id")) != str(shape_id):
        return review("Rendered overflow hint targets a different shape")
    try:
        if int(hint.get("slide")) != int(slide):
            return review("Rendered overflow hint targets a different slide")
    except (TypeError, ValueError):
        return review("Rendered overflow hint has an invalid slide")
    try:
        detail_load = _number(
            details.get("rendered_load_ratio"),
            "rendered_load_ratio",
        )
        hint_load = _number(
            hint.get("rendered_load_ratio"),
            "repair_hint.rendered_load_ratio",
        )
    except ValueError as error:
        return review(str(error))
    if not 1.0 < detail_load <= 10.0 or abs(detail_load - hint_load) > 1e-6:
        return review(
            "Rendered overflow load ratio is invalid or inconsistent with its hint"
        )
    rendered_target = _source_relative_rendered_target(details, detail_load)
    return {
        **base,
        "target_load_ratio": 0.92,
        "rendered_target_load_ratio": rendered_target,
        "rendered_load_ratio": detail_load,
        "autofit_font_scale": 98_000,
        "preserve_autofit_mode": True,
        "margin_scale": 0.75,
        "allow_expand": True,
        "_trusted_rendered_layout": True,
        "_rendered_evidence": {
            "issue_codes": ["PPTX_RENDERED_TEXT_OVERFLOW"],
            "provider": details.get("provider"),
            "rendered_load_ratio": detail_load,
            "rendered_target_load_ratio": rendered_target,
            "evidence_items": [
                _rendered_evidence_item(
                    code="PPTX_RENDERED_TEXT_OVERFLOW",
                    severity=severity,
                    details=details,
                    measurements={
                        "rendered_load_ratio": detail_load,
                        "rendered_target_load_ratio": rendered_target,
                    },
                )
            ],
        },
    }


def _source_relative_rendered_target(
    details: Mapping[str, Any],
    rendered_load: float,
) -> float:
    """Restore the source envelope instead of forcing legacy overflow under 1.0."""

    try:
        bound_width = _number(details.get("bound_width_pt"), "bound_width_pt")
        bound_height = _number(details.get("bound_height_pt"), "bound_height_pt")
        available_width = _number(
            details.get("available_width_pt"),
            "available_width_pt",
        )
        available_height = _number(
            details.get("available_height_pt"),
            "available_height_pt",
        )
        reference_width = _number(
            details.get("reference_overflow_width_pt"),
            "reference_overflow_width_pt",
        )
        reference_height = _number(
            details.get("reference_overflow_height_pt"),
            "reference_overflow_height_pt",
        )
        width_tolerance = _number(
            details.get("width_tolerance_pt"),
            "width_tolerance_pt",
        )
        height_tolerance = _number(
            details.get("height_tolerance_pt"),
            "height_tolerance_pt",
        )
    except ValueError:
        return 0.92
    if min(available_width, available_height, bound_width, bound_height) <= 0:
        return 0.92
    candidate_width_ratio = bound_width / available_width
    candidate_height_ratio = bound_height / available_height
    allowed_width_ratio = (
        available_width + max(0.0, reference_width) + max(0.0, width_tolerance)
    ) / available_width
    allowed_height_ratio = (
        available_height
        + max(0.0, reference_height)
        + max(0.0, height_tolerance)
    ) / available_height
    required_scale = min(
        1.0,
        allowed_width_ratio / candidate_width_ratio,
        allowed_height_ratio / candidate_height_ratio,
    )
    target = rendered_load * required_scale
    return max(0.5, min(rendered_load, target))


def _trusted_rendered_overlap_action(
    *,
    details: Mapping[str, Any],
    slide: Any,
    shape_id: Any,
    severity: str,
) -> Mapping[str, Any]:
    base = {
        "action": "resolve_text_overflow",
        "slide": slide,
        "shape_id": shape_id,
    }

    def review(reason: str) -> Mapping[str, Any]:
        return {**base, "_reject_reason": reason}

    if details.get("kind") != "shape" or details.get("region_id") != "shape":
        return review(
            "Rendered overlap is not an ordinary text shape with a safe text-only repair"
        )
    try:
        reference_area = _number(
            details.get("reference_overlap_area_pt2"),
            "reference_overlap_area_pt2",
        )
        candidate_area = _number(
            details.get("candidate_overlap_area_pt2"),
            "candidate_overlap_area_pt2",
        )
        tolerance = _number(
            details.get("worsening_tolerance_pt2"),
            "worsening_tolerance_pt2",
        )
    except ValueError as error:
        return review(str(error))
    allowed_area = reference_area + tolerance
    if reference_area < 0 or tolerance <= 0 or allowed_area <= 0:
        return review("Rendered overlap has no positive measured safe boundary")
    if candidate_area <= allowed_area:
        return review("Rendered overlap does not exceed its measured safe boundary")
    rendered_load = math.sqrt(candidate_area / allowed_area)
    if not 1.0 < rendered_load <= 10.0:
        return review("Rendered overlap requires an unsafe font reduction")
    return {
        **base,
        "target_load_ratio": 1.0,
        "rendered_target_load_ratio": 1.0,
        "rendered_load_ratio": rendered_load,
        "margin_scale": 1.0,
        "preserve_autofit_mode": False,
        "allow_expand": False,
        "_trusted_rendered_layout": True,
        "_rendered_evidence": {
            "issue_codes": ["PPTX_RENDERED_TEXT_OBJECT_OVERLAP"],
            "provider": details.get("provider"),
            "reference_overlap_area_pt2": reference_area,
            "candidate_overlap_area_pt2": candidate_area,
            "allowed_overlap_area_pt2": allowed_area,
            "evidence_items": [
                _rendered_evidence_item(
                    code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
                    severity=severity,
                    details=details,
                    measurements={
                        "reference_overlap_area_pt2": reference_area,
                        "candidate_overlap_area_pt2": candidate_area,
                        "allowed_overlap_area_pt2": allowed_area,
                    },
                )
            ],
        },
    }


def _rendered_evidence_item(
    *,
    code: str,
    severity: str,
    details: Mapping[str, Any],
    measurements: Mapping[str, Any],
) -> dict[str, Any]:
    """Retain bounded, issue-specific rendered measurements for repair audit."""

    item: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "provider": details.get("provider"),
        "measurements": _jsonable(measurements),
    }
    bounds: dict[str, Any] = {}
    for key in (
        "bound_left_pt",
        "bound_top_pt",
        "bound_width_pt",
        "bound_height_pt",
        "available_width_pt",
        "available_height_pt",
        "reference_overflow_width_pt",
        "reference_overflow_height_pt",
        "width_tolerance_pt",
        "height_tolerance_pt",
    ):
        if details.get(key) is not None:
            bounds[key] = _jsonable(details[key])
    object_bounds = details.get("object")
    if isinstance(object_bounds, Mapping):
        bounds["object"] = {
            str(key): _jsonable(value)
            for key, value in list(object_bounds.items())[:16]
        }
    overlaps = details.get("overlaps")
    if isinstance(overlaps, Sequence) and not isinstance(
        overlaps,
        (str, bytes, bytearray),
    ):
        bounds["overlaps"] = [_jsonable(value) for value in overlaps[:8]]
    if bounds:
        item["bounds"] = bounds
    return item


def _rendered_table_restore_issue_action(
    *,
    details: Mapping[str, Any],
    hint: Mapping[str, Any],
    slide: Any,
    shape_id: Any,
) -> Mapping[str, Any]:
    base = {
        "action": "restore_table_row_heights",
        "slide": slide,
        "shape_id": shape_id,
    }

    def review(reason: str) -> Mapping[str, Any]:
        return {**base, "_reject_reason": reason}

    if _action_name(hint) != "restore_table_row_heights":
        return review(
            "Rendered table geometry issue has no trusted row-height repair hint"
        )
    if hint.get("automatic") is not True:
        return review("Rendered table row-height repair was not marked automatic")
    if details.get("row_count_changed") is not False:
        return review("Table row count changed; row heights cannot be restored safely")
    if details.get("table_coordinates_unchanged") is not True:
        return review("Table coordinates were not proven unchanged by rendered QA")
    if details.get("automatic_row_height_restore_safe") is not True:
        return review("Rendered QA did not mark table row-height restoration safe")

    try:
        reference_rows = _numeric_list(
            details.get("reference_row_heights_pt"),
            "reference_row_heights_pt",
        )
        candidate_rows = _numeric_list(
            details.get("candidate_row_heights_pt"),
            "candidate_row_heights_pt",
        )
        row_deltas = _numeric_list(
            details.get("row_height_deltas_pt"),
            "row_height_deltas_pt",
        )
        row_tolerances = _numeric_list(
            details.get("row_height_tolerances_pt"),
            "row_height_tolerances_pt",
        )
        reference_count = _positive_integer(
            details.get("reference_row_count"),
            "reference_row_count",
        )
        candidate_count = _positive_integer(
            details.get("candidate_row_count"),
            "candidate_row_count",
        )
    except ValueError as error:
        return review(str(error))

    lengths = {
        len(reference_rows),
        len(candidate_rows),
        len(row_deltas),
        len(row_tolerances),
        reference_count,
        candidate_count,
    }
    if len(lengths) != 1:
        return review("Rendered table row measurements have inconsistent row counts")
    if any(tolerance < 0 for tolerance in row_tolerances):
        return review("Table row-height tolerances cannot be negative")
    if any(delta > 1e-6 for delta in row_deltas):
        return review(
            "Table contains a positive or mixed row-height delta and requires "
            "manual review"
        )

    significant_rows = [
        index
        for index, (delta, tolerance) in enumerate(
            zip(row_deltas, row_tolerances),
            start=1,
        )
        if abs(delta) > tolerance
    ]
    if not significant_rows:
        return review("Table has no significant negative row-height delta")
    if any(row_deltas[index - 1] >= 0 for index in significant_rows):
        return review("All significant table row-height deltas must be negative")

    changed = details.get("changed_row_indices")
    try:
        changed_rows = _positive_integer_list(
            changed,
            "changed_row_indices",
        )
    except ValueError as error:
        return review(str(error))
    if changed_rows != significant_rows:
        return review("Changed table row indices do not match the rendered deltas")

    for index, (reference, candidate, delta, tolerance) in enumerate(
        zip(reference_rows, candidate_rows, row_deltas, row_tolerances),
        start=1,
    ):
        observed = candidate - reference
        if abs(observed - delta) > max(0.05, tolerance * 0.1):
            return review(
                f"Table row {index} delta is inconsistent with its rendered heights"
            )

    expected_rows = [
        {
            "row_index": index,
            "height_pt": reference_rows[index - 1],
        }
        for index in significant_rows
    ]
    try:
        hinted_rows = _normalize_table_rows(hint.get("rows"))
    except ValueError as error:
        return review(str(error))
    if [row["row_index"] for row in hinted_rows] != significant_rows or any(
        abs(float(hinted["height_pt"]) - float(expected["height_pt"])) > 0.01
        for hinted, expected in zip(hinted_rows, expected_rows)
    ):
        return review("Table repair hint rows do not match trusted reference heights")
    return {**base, "rows": expected_rows}


def _snapshot_context(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[tuple[int, str], _ShapeContext]:
    source_slides = {int(slide["number"]): slide for slide in source.get("slides", [])}
    width = int(candidate["slide_size_emu"]["width"])
    height = int(candidate["slide_size_emu"]["height"])
    result: dict[tuple[int, str], _ShapeContext] = {}
    for slide in candidate.get("slides", []):
        number = int(slide["number"])
        source_shapes = {
            str(shape["shape_id"]): shape
            for shape in source_slides.get(number, {}).get("shapes", [])
        }
        for shape in slide.get("shapes", []):
            shape_id = str(shape["shape_id"])
            result[(number, shape_id)] = _ShapeContext(
                slide=number,
                shape_id=shape_id,
                source_shape=source_shapes.get(shape_id),
                candidate_shape=shape,
                slide_width=width,
                slide_height=height,
                source_document=Path(str(source["path"])).expanduser().resolve(),
            )
    return result


def _shape_context(
    payload: Mapping[str, Any],
    context: Mapping[tuple[int, str], _ShapeContext],
) -> _ShapeContext:
    slide = payload.get("slide", payload.get("slide_number"))
    shape_id = payload.get("shape_id")
    key = _shape_key(slide, shape_id)
    if key not in context:
        raise ValueError(
            f"Shape {shape_id!r} does not exist on one-based slide {slide!r}"
        )
    return context[key]


def _shape_key(slide: Any, shape_id: Any) -> tuple[int, str]:
    if isinstance(slide, bool):
        raise ValueError("slide must be a one-based integer")
    try:
        number = int(slide)
    except (TypeError, ValueError) as error:
        raise ValueError("slide must be a one-based integer") from error
    if number < 1:
        raise ValueError("slide must be a one-based integer")
    identifier = str(shape_id or "")
    if not identifier:
        raise ValueError("shape_id is required")
    return number, identifier


def _require_text_shape(shape: _ShapeContext) -> None:
    if shape.candidate_shape.get("body_properties") is None:
        raise ValueError(
            f"Shape {shape.shape_id} on slide {shape.slide} has no text body"
        )


def _require_table_graphic_frame(shape: _ShapeContext) -> None:
    if shape.candidate_shape.get("shape_type") != "graphic_frame":
        raise ValueError(
            f"Shape {shape.shape_id} on slide {shape.slide} is not a "
            "table graphic frame"
        )


def _normalize_table_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ValueError("restore_table_row_heights requires a rows list")
    if not value:
        raise ValueError("restore_table_row_heights requires a non-empty rows list")
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"rows[{position}] must be a mapping")
        row_index = _positive_integer(
            raw.get("row_index"),
            f"rows[{position}].row_index",
        )
        if row_index in seen:
            raise ValueError(f"row_index {row_index} must be unique")
        seen.add(row_index)
        height = _number(
            raw.get("height_pt"),
            f"rows[{position}].height_pt",
        )
        if not 1.0 <= height <= 1000.0:
            raise ValueError("table row height_pt must be between 1 and 1000")
        rows.append({"row_index": row_index, "height_pt": height})
    return sorted(rows, key=lambda row: int(row["row_index"]))


def _numeric_list(value: Any, label: str) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"{label} must be a numeric list")
    if not value:
        raise ValueError(f"{label} cannot be empty")
    return [_number(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_integer_list(value: Any, label: str) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"{label} must be a list of positive integers")
    result = [
        _positive_integer(item, f"{label}[{index}]") for index, item in enumerate(value)
    ]
    if len(set(result)) != len(result):
        raise ValueError(f"{label} values must be unique")
    return result


def _font_action(
    payload: Mapping[str, Any],
    shape: _ShapeContext,
    name: str,
) -> dict[str, Any]:
    base = {"slide": shape.slide, "shape_id": shape.shape_id}
    absolute = _first_present(
        payload,
        ("font_size_pt", "target_font_size_pt", "value"),
    )
    if name == "set_font_size" or absolute is not None:
        target = _number(
            absolute,
            "font size",
        )
        if not MIN_FONT_SIZE_PT <= target <= MAX_FONT_SIZE_PT:
            raise ValueError("font size must be between 8 and 72 points")
        action: dict[str, Any] = {
            "action": "set_font_size",
            **base,
            "font_size_pt": target,
        }
    else:
        scale = _number(payload.get("scale", 0.90), "font scale")
        if not 0.5 <= scale <= MAX_FONT_SCALE:
            raise ValueError("font scale must be between 0.5 and 1.0")
        action = {
            "action": "adjust_font_size",
            **base,
            "scale": round(scale, 6),
        }
    minimum = _first_present(
        payload,
        ("min_font_size_pt", "minimum_font_size_pt"),
    )
    if minimum is not None:
        minimum_value = _number(minimum, "min_font_size_pt")
        if not MIN_FONT_SIZE_PT <= minimum_value <= MAX_FONT_SIZE_PT:
            raise ValueError("min_font_size_pt must be between 8 and 72 points")
        action["min_font_size_pt"] = minimum_value
    return action


def _geometry_bounds(
    payload: Mapping[str, Any],
    shape: _ShapeContext,
) -> list[int]:
    geometry = payload.get("geometry")
    if isinstance(geometry, Mapping):
        missing = [key for key in ("x", "y", "width", "height") if key not in geometry]
        if missing:
            raise ValueError(
                "geometry requires x, y, width, height; missing " + ", ".join(missing)
            )
        unit = str(geometry.get("unit", "emu")).lower()
        if unit not in {"emu", "points", "point", "pt"}:
            raise ValueError("geometry unit must be emu or points")
        values = [
            _number(geometry[key], f"geometry.{key}")
            for key in ("x", "y", "width", "height")
        ]
        factor = EMU_PER_POINT if unit in {"points", "point", "pt"} else 1
        value: Any = [round(item * factor) for item in values]
    else:
        value = _first_present(
            payload,
            (
                "bounds_emu",
                "bbox_emu",
                "target_bbox",
                "target_bbox_emu",
                "safe_bounds",
            ),
        )
    bbox = _bbox(value, "shape bounds")
    clamp = _boolean(payload.get("clamp_to_slide", False), "clamp_to_slide")
    x, y, width, height = bbox
    if not clamp:
        if x < 0 or y < 0:
            raise ValueError("shape bounds x and y cannot be negative")
        if x + width > shape.slide_width or y + height > shape.slide_height:
            raise ValueError("shape bounds must stay inside the slide canvas")
    return bbox


def _bbox(value: Any, label: str) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must contain [x, y, width, height]")
    if len(value) != 4:
        raise ValueError(f"{label} must contain exactly four values")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{label} values must be integers")
        try:
            parsed = int(item)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} values must be integers") from error
        result.append(parsed)
    if result[2] <= 0 or result[3] <= 0:
        raise ValueError(f"{label} width and height must be positive")
    return result


def _clamp_bbox(
    bbox: Sequence[int],
    slide_width: int,
    slide_height: int,
) -> list[int]:
    x, y, width, height = bbox
    width = min(width, slide_width)
    height = min(height, slide_height)
    x = min(max(0, x), slide_width - width)
    y = min(max(0, y), slide_height - height)
    return [x, y, width, height]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(_jsonable(value))
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dict(_jsonable(dumper(mode="json")))
    if dataclasses.is_dataclass(value):
        return dict(_jsonable(dataclasses.asdict(value)))
    raise TypeError(
        f"Repair action must be mapping-like, received {type(value).__name__}"
    )


def _issue_dict(issue: Any) -> dict[str, Any]:
    if isinstance(issue, Mapping):
        return dict(_jsonable(issue))
    dumper = getattr(issue, "model_dump", None)
    if callable(dumper):
        return dict(_jsonable(dumper(mode="json")))
    return {
        "code": getattr(issue, "code", "UNKNOWN"),
        "message": getattr(issue, "message", ""),
        "severity": _severity_value(getattr(issue, "severity", "error")),
        "slide": getattr(issue, "slide", None),
        "block_id": getattr(issue, "block_id", None),
        "details": _jsonable(getattr(issue, "details", {})),
    }


def _severity_value(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value).lower()


def _new_error_issues(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return error occurrences that were not already present before repair."""

    remaining: dict[tuple[Any, ...], int] = {}
    for issue in before:
        if _severity_value(issue.get("severity", "error")) != "error":
            continue
        key = _issue_identity(issue)
        remaining[key] = remaining.get(key, 0) + 1

    new: list[dict[str, Any]] = []
    for issue in after:
        if _severity_value(issue.get("severity", "error")) != "error":
            continue
        key = _issue_identity(issue)
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
            continue
        new.append(dict(issue))
    return new


def _merge_live_rendered_issues(
    supplied: Sequence[Any],
    live: Sequence[Mapping[str, Any]],
) -> list[Any]:
    """Augment supplied QA with live rendered findings, preferring live details."""

    result = list(supplied)
    rendered_positions: dict[tuple[Any, ...], int] = {}
    for index, issue in enumerate(result):
        data = _issue_dict(issue)
        if str(data.get("code", "")).upper().startswith("PPTX_RENDERED_"):
            rendered_positions[_issue_identity(data)] = index
    for issue in live:
        normalized = dict(issue)
        key = _issue_identity(normalized)
        position = rendered_positions.get(key)
        if position is None:
            rendered_positions[key] = len(result)
            result.append(normalized)
        else:
            result[position] = normalized
    return result


def _new_or_worsened_rendered_errors(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Allow unresolved baseline layout errors but reject new regressions."""

    baseline: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for issue in before:
        if _severity_value(issue.get("severity", "error")) != "error":
            continue
        if not str(issue.get("code", "")).upper().startswith("PPTX_RENDERED_"):
            continue
        baseline.setdefault(_issue_identity(issue), []).append(issue)

    after_errors = [
        issue
        for issue in after
        if _severity_value(issue.get("severity", "error")) == "error"
    ]
    blocking: list[dict[str, Any]] = []
    for issue in after_errors:
        code = str(issue.get("code", "")).upper()
        if code in RENDERED_AUTHORITY_FATAL_CODES:
            blocking.append(dict(issue))
            continue
        key = _issue_identity(issue)
        matches = baseline.get(key)
        if not matches:
            blocking.append(dict(issue))
            continue
        reference = matches.pop(0)
        if _rendered_issue_worsened(reference, issue):
            blocking.append(dict(issue))
    return blocking


def _rendered_issue_worsened(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    code = str(after.get("code", "")).upper()
    before_details = before.get("details")
    after_details = after.get("details")
    before_details = before_details if isinstance(before_details, Mapping) else {}
    after_details = after_details if isinstance(after_details, Mapping) else {}
    if code == "PPTX_RENDERED_TABLE_GEOMETRY_CHANGED":
        return _rendered_table_geometry_worsened(
            before_details,
            after_details,
        )
    metric = None
    tolerance = 0.01
    if code == "PPTX_RENDERED_TEXT_OVERFLOW":
        metric = "rendered_load_ratio"
    elif code == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP":
        metric = "candidate_overlap_area_pt2"
        tolerance = max(
            1.0,
            float(before_details.get(metric, 0.0)) * 0.01,
        )
    if metric is None:
        return False
    try:
        before_value = float(before_details[metric])
        after_value = float(after_details[metric])
    except (KeyError, TypeError, ValueError):
        return False
    return after_value > before_value + tolerance


def _rendered_table_geometry_worsened(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Reject any material new table displacement or row-height damage."""

    if (
        before.get("row_count_changed") is False
        and after.get("row_count_changed") is True
    ):
        return True
    if (
        before.get("table_coordinates_unchanged") is True
        and after.get("table_coordinates_unchanged") is False
    ):
        return True

    before_changed_rows = _rendered_integer_set(
        before.get("changed_row_indices")
    )
    after_changed_rows = _rendered_integer_set(after.get("changed_row_indices"))
    if before_changed_rows is not None and after_changed_rows is not None:
        if after_changed_rows - before_changed_rows:
            return True

    before_row_count_delta = _rendered_count_delta(before)
    after_row_count_delta = _rendered_count_delta(after)
    if (
        before_row_count_delta is not None
        and after_row_count_delta is not None
        and after_row_count_delta > before_row_count_delta
    ):
        return True

    if _rendered_excess_worsened(
        before.get("row_height_deltas_pt"),
        before.get("row_height_tolerances_pt"),
        after.get("row_height_deltas_pt"),
        after.get("row_height_tolerances_pt"),
    ):
        return True

    for delta_field, tolerance_field in (
        ("frame_height_delta_pt", "frame_height_tolerance_pt"),
        ("frame_left_delta_pt", "frame_left_tolerance_pt"),
        ("frame_top_delta_pt", "frame_top_tolerance_pt"),
        ("frame_width_delta_pt", "frame_width_tolerance_pt"),
    ):
        before_delta = _rendered_float(before.get(delta_field))
        before_tolerance = _rendered_float(before.get(tolerance_field))
        after_delta = _rendered_float(after.get(delta_field))
        after_tolerance = _rendered_float(after.get(tolerance_field))
        if None in {
            before_delta,
            before_tolerance,
            after_delta,
            after_tolerance,
        }:
            continue
        before_excess = max(0.0, abs(before_delta) - abs(before_tolerance))
        after_excess = max(0.0, abs(after_delta) - abs(after_tolerance))
        if after_excess > before_excess + 0.01:
            return True
    return False


def _rendered_excess_worsened(
    before_deltas: Any,
    before_tolerances: Any,
    after_deltas: Any,
    after_tolerances: Any,
) -> bool:
    before_delta_values = _rendered_float_sequence(before_deltas)
    before_tolerance_values = _rendered_float_sequence(before_tolerances)
    after_delta_values = _rendered_float_sequence(after_deltas)
    after_tolerance_values = _rendered_float_sequence(after_tolerances)
    if any(
        value is None
        for value in (
            before_delta_values,
            before_tolerance_values,
            after_delta_values,
            after_tolerance_values,
        )
    ):
        return False
    assert before_delta_values is not None
    assert before_tolerance_values is not None
    assert after_delta_values is not None
    assert after_tolerance_values is not None
    before_excess = [
        max(0.0, abs(delta) - abs(tolerance))
        for delta, tolerance in zip(
            before_delta_values,
            before_tolerance_values,
        )
    ]
    after_excess = [
        max(0.0, abs(delta) - abs(tolerance))
        for delta, tolerance in zip(
            after_delta_values,
            after_tolerance_values,
        )
    ]
    if len(after_excess) > len(before_excess):
        return True
    return any(
        after_value > before_value + 0.01
        for before_value, after_value in zip(before_excess, after_excess)
    )


def _rendered_float_sequence(value: Any) -> list[float] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    result: list[float] = []
    for item in value:
        number = _rendered_float(item)
        if number is None:
            return None
        result.append(number)
    return result


def _rendered_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rendered_integer_set(value: Any) -> set[int] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    result: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            number = int(item)
        except (TypeError, ValueError):
            return None
        if number < 1:
            return None
        result.add(number)
    return result


def _rendered_count_delta(details: Mapping[str, Any]) -> int | None:
    reference = _rendered_float(details.get("reference_row_count"))
    candidate = _rendered_float(details.get("candidate_row_count"))
    if reference is None or candidate is None:
        return None
    if not reference.is_integer() or not candidate.is_integer():
        return None
    return abs(int(candidate) - int(reference))


def _issue_identity(issue: Mapping[str, Any]) -> tuple[Any, ...]:
    details = issue.get("details")
    details = details if isinstance(details, Mapping) else {}

    def field(name: str) -> Any:
        value = details.get(name)
        return issue.get(name) if value is None else value

    return (
        str(issue.get("code", "UNKNOWN")).strip().upper(),
        issue.get("page"),
        issue.get("slide"),
        issue.get("block_id"),
        field("shape_id"),
        field("other_shape_id"),
        field("region_id"),
        field("cell_row"),
        field("cell_column"),
        field("inherited_scope"),
        field("inherited_shape_id"),
    )


def _action_name(action: Mapping[str, Any]) -> str | None:
    value = action.get("action", action.get("action_type"))
    if hasattr(value, "value"):
        value = value.value
    return str(value) if value is not None else None


def _first_present(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if mapping.get(name) is not None:
            return mapping[name]
    return None


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not result == result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{label} must be finite")
    return result


def _integer(value: Any, label: str) -> int:
    number = _number(value, label)
    if not number.is_integer():
        raise ValueError(f"{label} must be an integer")
    return int(number)


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _rejection(
    *,
    source: str,
    action: Any,
    code: str,
    reason: str,
    payload: Any,
) -> dict[str, Any]:
    return {
        "source": source,
        "action": action,
        "code": code,
        "reason": reason,
        "payload": _jsonable(payload),
    }


def _pptx_path(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    if path.suffix.lower() != ".pptx":
        raise ValueError(f"{label} must be a PPTX file")
    return path


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if hasattr(value, "value"):
        return _jsonable(value.value)
    return repr(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
