#!/usr/bin/env python3
"""Aggregate terminal Office-corpus QA results into a P0/P1 report.

The runtime QA schema currently exposes ``error``, ``warning`` and ``info``
rather than P0/P1.  This script applies the documented derived classification
defined by :data:`CLASSIFICATION_RULE_VERSION` and keeps that version in every
output so results remain auditable when the policy evolves.

Only Python's standard library is used.  Re-running the command replaces the
four output files atomically and deterministically.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CLASSIFICATION_RULE_VERSION = "office-p0-p1-derived-v1.4"

OUTPUT_FILENAMES = (
    "issue-summary.json",
    "issue-summary.csv",
    "issues.jsonl",
    "P0_P1_REPORT.md",
)

FAILURE_STATUSES = {
    "ABORTED",
    "CANCELED",
    "CANCELLED",
    "ERROR",
    "EXPIRED",
    "FAILED",
    "HARD_TIMEOUT",
    "TIMED_OUT",
    "TIMEOUT",
}
CANCELLED_STATUSES = {"ABORTED", "CANCELED", "CANCELLED"}
REVIEW_STATUSES = {"BLOCKED", "NEEDS_REVIEW", "REVIEW_REQUIRED"}
SUCCESS_STATUSES = {
    "COMPLETED",
    "COMPLETED_WITH_WARNINGS",
    "DONE",
    "FINALIZED",
    "SUCCEEDED",
    "SUCCESS",
}
NONTERMINAL_STATUSES = {
    "CONVERTING",
    "CREATED",
    "PENDING",
    "PREFLIGHT",
    "PREPARING",
    "QA",
    "QUEUED",
    "REPAIRING",
    "RUNNING",
    "TRANSLATING",
    "VERIFYING",
}
TERMINAL_STATUSES = FAILURE_STATUSES | REVIEW_STATUSES | SUCCESS_STATUSES

# These patterns apply only to severity=error.  A warning that merely mentions
# a package or a macro remains a warning and is never promoted to P0.
P0_RULE_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "package_corruption",
        r"(?:CORRUPT|BAD[_ ]?ZIP|BROKEN[_ ]?(?:ZIP|PACKAGE)|"
        r"INVALID[_ ]?(?:OOXML|DOCX|PPTX|PACKAGE|ZIP)|"
        r"(?:OOXML|DOCX|PPTX|PACKAGE|ZIP)[_ ]?(?:CORRUPT|INVALID|BROKEN))",
        "包或 OOXML/ZIP 容器损坏",
    ),
    (
        "openability_failure",
        r"(?:UNOPENABLE|CANNOT[_ ]?OPEN|CAN'T[_ ]?OPEN|UNABLE[_ ]?TO[_ ]?OPEN|"
        r"OPENABILITY.{0,40}(?:FAIL|ERROR|INVALID)|"
        r"(?:OPEN|OPENING)[_ ]?(?:FAILED|FAILURE|ERROR))",
        "文档或包无法打开",
    ),
    (
        "macro_or_vba",
        r"(?:MACRO|VBA(?:PROJECT)?|VBAPROJECT)",
        "检测到宏/VBA 或宏安全门失败",
    ),
    (
        "encryption",
        r"(?:ENCRYPT|PASSWORD[_ -]?PROTECT)",
        "检测到加密或密码保护",
    ),
    (
        "structure_relationship_media_loss",
        r"(?:(?:STRUCTUR(?:E|AL)|RELATIONSHIPS?|MEDIA|IMAGE|EMBEDDED[_ ]?OBJECT|"
        r"OLE[_ ]?OBJECT|TEXT[_ ]?BLOCK).{0,60}"
        r"(?:LOSS|LOST|MISSING|DROPPED|REMOVED|DELETED|MISMATCH|CHANGED|INTEGRITY)|"
        r"(?:LOSS|LOST|MISSING|DROPPED|REMOVED|DELETED).{0,60}"
        r"(?:STRUCTUR(?:E|AL)|RELATIONSHIPS?|MEDIA|IMAGE|EMBEDDED[_ ]?OBJECT|"
        r"OLE[_ ]?OBJECT|TEXT[_ ]?BLOCK))",
        "结构、关系、媒体或文本块丢失/破坏",
    ),
    (
        "hash_integrity_failure",
        r"(?:(?:HASH|SHA[_ -]?256|DIGEST).{0,40}"
        r"(?:MISMATCH|CHANGED|INVALID|FAIL|MISSING)|"
        r"(?:MISMATCH|INVALID|FAIL).{0,40}(?:HASH|SHA[_ -]?256|DIGEST))",
        "哈希或摘要完整性失败",
    ),
    (
        "unsafe_writeback",
        r"(?:UNSAFE[_ ]?WRITE[_ ]?BACK|WRITE[_ ]?BACK.{0,40}"
        r"(?:FAIL|ERROR|UNSAFE|INVALID)|"
        r"(?:FAIL|ERROR).{0,40}WRITE[_ ]?BACK)",
        "写回失败或不安全写回",
    ),
    (
        "conversion_failure",
        r"(?:(?:CONVERT|CONVERSION|LEGACY[_ ]?CONVERSION).{0,40}"
        r"(?:FAIL|ERROR|INVALID|UNAVAILABLE)|"
        r"(?:FAIL|ERROR).{0,40}(?:CONVERT|CONVERSION))",
        "DOC/PPT 转换失败",
    ),
)
P0_RULES = tuple(
    (name, re.compile(pattern, re.IGNORECASE | re.DOTALL), description)
    for name, pattern, description in P0_RULE_SPECS
)

CONTENT_QUALITY_STAGES = frozenset(
    {
        "content_qa",
        "language_qa",
        "semantic_qa",
        "semantic_validation",
        "terminology_qa",
        "translation_validation",
    }
)

LOCATION_FIELDS = (
    "part",
    "part_name",
    "relpath",
    "xpath",
    "block_id",
    "page",
    "slide",
    "shape_id",
    "shape_name",
    "paragraph",
    "run",
    "table",
    "row",
    "column",
    "cell",
    "relationship_id",
    "media",
    "object_id",
    "object_identity",
)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "warning": 2, "info": 3, "other": 4}


class AggregationError(RuntimeError):
    """Raised for an invalid run-level input that prevents aggregation."""


def _json_text(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=False)


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise AggregationError(f"JSON file does not exist: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AggregationError(f"Cannot read JSON {path}: {exc}") from exc


def _atomic_write_text(path: Path, content: str) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        temp.replace(path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _normalise_status(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    return (
        re.sub(r"[^A-Z0-9]+", "_", str(value).strip().upper()).strip("_") or "UNKNOWN"
    )


def _is_terminal_status(value: Any) -> bool:
    """Return true only for a recognised durable terminal job status."""

    return _normalise_status(value) in TERMINAL_STATUSES


def _first(mapping: Mapping[str, Any] | None, names: Sequence[str]) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _nested_dicts(mapping: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = [mapping]
    for key in ("terminal", "final", "result", "summary", "job", "metadata"):
        value = mapping.get(key)
        if isinstance(value, Mapping):
            result.append(value)
    return result


def _value_from_sources(
    sources: Iterable[Mapping[str, Any]], names: Sequence[str]
) -> Any:
    for source in sources:
        value = _first(source, names)
        if value not in (None, ""):
            return value
    return None


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_path(root: Path, candidate: Path, description: str) -> Path:
    resolved = candidate.resolve()
    if not _inside(root, resolved):
        raise AggregationError(f"{description} escapes run directory: {candidate}")
    return resolved


def _job_entries(index: Any) -> list[dict[str, Any]]:
    if isinstance(index, Mapping):
        raw_jobs = index.get("jobs")
    else:
        raw_jobs = index

    entries: list[dict[str, Any]] = []
    if isinstance(raw_jobs, list):
        for ordinal, value in enumerate(raw_jobs):
            if isinstance(value, Mapping):
                entry = dict(value)
            elif isinstance(value, str):
                entry = {"job_id": value}
            else:
                raise AggregationError(f"jobs[{ordinal}] must be an object or string")
            entry.setdefault("ordinal", ordinal)
            entries.append(entry)
    elif isinstance(raw_jobs, Mapping):
        for ordinal, (job_id, value) in enumerate(raw_jobs.items()):
            if isinstance(value, Mapping):
                entry = dict(value)
            elif isinstance(value, str):
                entry = {"job_dir": value}
            elif value is None:
                entry = {}
            else:
                raise AggregationError(f"jobs[{job_id!r}] has unsupported value")
            entry.setdefault("job_id", str(job_id))
            entry.setdefault("ordinal", ordinal)
            entries.append(entry)
    else:
        raise AggregationError("batch-index.json must contain a jobs array or object")

    seen: set[str] = set()
    for ordinal, entry in enumerate(entries):
        job_id = str(_first(entry, ("job_id", "id", "name")) or "").strip()
        if not job_id:
            raise AggregationError(f"jobs[{ordinal}] has no job_id")
        if job_id in seen:
            raise AggregationError(f"duplicate job_id in batch-index.json: {job_id}")
        if job_id in {".", ".."} or "/" in job_id or "\\" in job_id:
            raise AggregationError(f"unsafe job_id in batch-index.json: {job_id!r}")
        entry["job_id"] = job_id
        seen.add(job_id)
    return sorted(
        entries, key=lambda item: (int(item.get("ordinal", 0)), item["job_id"])
    )


def _read_optional_json(path: Path, errors: list[str]) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = _load_json(path)
    except AggregationError as exc:
        errors.append(str(exc))
        return None
    if not isinstance(value, Mapping):
        errors.append(f"Expected JSON object: {path}")
        return None
    return value


def _candidate_qa_from_tool_state(state: Mapping[str, Any]) -> str | None:
    candidates = state.get("candidates")
    if not isinstance(candidates, Mapping):
        return None

    final_candidate_ids: list[str] = []
    results = state.get("results")
    if isinstance(results, Mapping):
        for value in results.values():
            if isinstance(value, Mapping):
                candidate_id = value.get("candidate_id")
                if candidate_id:
                    final_candidate_ids.append(str(candidate_id))

    candidate_ids = final_candidate_ids + sorted(
        (str(key) for key in candidates), reverse=True
    )
    visited: set[str] = set()
    for candidate_id in candidate_ids:
        if candidate_id in visited:
            continue
        visited.add(candidate_id)
        candidate = candidates.get(candidate_id)
        if not isinstance(candidate, Mapping):
            continue
        latest_qa = candidate.get("latest_qa")
        if isinstance(latest_qa, Mapping) and latest_qa.get("report_relpath"):
            return str(latest_qa["report_relpath"])
    return None


def _source_info(
    entry: Mapping[str, Any],
    tool_state: Mapping[str, Any] | None,
    index: Mapping[str, Any],
) -> dict[str, Any]:
    source_state = tool_state.get("source") if isinstance(tool_state, Mapping) else None
    if not isinstance(source_state, Mapping):
        source_state = {}

    relative_path = str(
        _first(entry, ("relative_path", "source_relpath", "input_relpath"))
        or _first(source_state, ("input_relpath", "source_relpath"))
        or _first(entry, ("source_path", "input_path", "filename"))
        or entry["job_id"]
    ).replace("\\", "/")
    source_path = str(
        _first(entry, ("source_path", "input_path"))
        or _first(source_state, ("input_relpath", "source_relpath"))
        or relative_path
    )
    extension = str(
        _first(entry, ("extension", "source_format", "format"))
        or _first(source_state, ("source_format", "normalized_format"))
        or Path(relative_path).suffix
        or "unknown"
    ).lower()
    extension = extension.lstrip(".") or "unknown"
    collection = str(
        _first(entry, ("collection", "source_collection", "corpus_collection"))
        or _first(index, ("collection", "source_collection"))
        or "unknown"
    )
    filename = relative_path.rsplit("/", 1)[-1] or entry["job_id"]
    source_language_manifest_value = entry.get("source_language_manifest")
    source_language_manifest = (
        str(source_language_manifest_value).strip()
        if source_language_manifest_value not in (None, "")
        else "unknown"
    )
    if not source_language_manifest:
        source_language_manifest = "unknown"
    return {
        "source_file": relative_path,
        "source_path": source_path,
        "source_filename": filename,
        "source_format": extension,
        "source_collection": collection,
        "source_language_manifest": source_language_manifest,
        "source_sha256": _first(entry, ("source_sha256", "input_sha256"))
        or _first(source_state, ("input_sha256", "source_sha256")),
        "normalized_source_sha256": _first(
            entry,
            (
                "normalized_source_sha256",
                "normalized_sha256",
                "converted_source_sha256",
            ),
        )
        or _first(
            source_state,
            (
                "normalized_source_sha256",
                "normalized_sha256",
                "converted_source_sha256",
            ),
        ),
        "source_language_request": _first(
            entry, ("source_language_request", "source_language")
        )
        or _first(source_state, ("source_language_request", "source_language")),
        "target_language_request": _first(
            entry, ("target_language_request", "target_language")
        )
        or _first(source_state, ("target_language_request", "target_language")),
        "resolved_source_language": _first(
            entry, ("resolved_source_language", "detected_source_language")
        )
        or _first(
            source_state,
            ("resolved_source_language", "detected_source_language"),
        ),
        "resolved_target_language": _first(
            entry, ("resolved_target_language", "effective_target_language")
        )
        or _first(
            source_state,
            ("resolved_target_language", "effective_target_language"),
        ),
    }


def _location(issue: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    nested = issue.get("location")
    if isinstance(nested, Mapping):
        for key in sorted(nested):
            value = nested[key]
            if value not in (None, "", [], {}):
                result[str(key)] = value
    elif nested not in (None, "", [], {}):
        result["location"] = nested
    for field in LOCATION_FIELDS:
        value = issue.get(field)
        if value not in (None, "", [], {}) and field not in result:
            result[field] = value
    # Some adapters keep the stable Office object identifier in ``details``
    # while exposing only the slide/page at the top level.  It is still part
    # of the location: two missing shapes on one slide are distinct issues.
    details = issue.get("details")
    if isinstance(details, Mapping):
        for field in LOCATION_FIELDS:
            value = details.get(field)
            if value not in (None, "", [], {}) and field not in result:
                result[field] = value
    return result or {"scope": "job"}


def _classification(issue: Mapping[str, Any]) -> tuple[str, str]:
    forced = issue.get("_forced_priority")
    if forced in {"P0", "P1", "warning", "info", "other"}:
        return str(forced), str(issue.get("_classification_reason") or "forced")

    severity = str(issue.get("severity") or "info").strip().lower()
    if severity == "warning":
        return "warning", "severity_warning_not_counted_as_p0_p1"
    if severity == "info":
        return "info", "severity_info_not_counted_as_p0_p1"
    if severity != "error":
        return "other", "unrecognised_severity_not_counted_as_p0_p1"

    stage = re.sub(
        r"[^a-z0-9]+",
        "_",
        str(issue.get("stage") or "").strip().casefold(),
    ).strip("_")
    if stage in CONTENT_QUALITY_STAGES:
        return "P1", "content_quality_error_blocks_qa"

    evidence = " ".join(
        str(issue.get(key) or "") for key in ("code", "message", "stage")
    )
    for name, pattern, _description in P0_RULES:
        if pattern.search(evidence):
            return "P0", name
    return "P1", "remaining_severity_error_blocks_qa"


def _synthetic_issue(
    *,
    code: str,
    message: str,
    severity: str,
    stage: str,
    priority: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "issue_id": code,
        "code": code,
        "message": message,
        "severity": severity,
        "stage": stage,
        "location": {"scope": "job"},
        "recoverable": False,
        "details": {},
        "_synthetic": True,
        "_forced_priority": priority,
        "_classification_reason": reason,
        "_report_kind": stage,
    }


def _terminal_issue(entry: Mapping[str, Any], status: str) -> dict[str, Any] | None:
    error_code = str(entry.get("error_code") or "").strip()
    error_summary = str(entry.get("error_summary") or "").strip()
    if status in FAILURE_STATUSES:
        cancelled = status in CANCELLED_STATUSES
        code = error_code or ("JOB_CANCELLED" if cancelled else "JOB_FAILED")
        message = error_summary or (f"任务终态为 {status}；按派生口径计为 P0。")
        return _synthetic_issue(
            code=code,
            message=message,
            severity="error",
            stage="terminal",
            priority="P0",
            reason="job_cancelled" if cancelled else "job_failed",
        )
    return None


def _report_issues(
    report: Mapping[str, Any],
    *,
    report_kind: str = "qa",
    report_relpath: str | None = None,
) -> list[dict[str, Any]]:
    # A repair report contains the same issue many times under round history.
    # ``accepted_issues`` is the bounded final snapshot for its accepted
    # candidate and is therefore the only repair list we consume.
    raw: Any = report.get("accepted_issues") if report_kind == "repair" else None
    if not isinstance(raw, list):
        raw = report.get("issues")
    if not isinstance(raw, list):
        for key in ("qa", "verification", "verify", "result", "final"):
            nested = report.get(key)
            if isinstance(nested, Mapping) and isinstance(nested.get("issues"), list):
                raw = nested["issues"]
                break
    if not isinstance(raw, list):
        return []

    issues: list[dict[str, Any]] = []
    for ordinal, value in enumerate(raw):
        if isinstance(value, Mapping):
            issue = dict(value)
        else:
            issue = {
                "code": "UNSTRUCTURED_QA_ISSUE",
                "message": str(value),
                "severity": "info",
                "stage": report_kind,
            }
        issue.setdefault("_report_ordinal", ordinal)
        issue.setdefault("stage", report_kind)
        issue["_report_kind"] = report_kind
        issue["_report_relpath"] = report_relpath
        issues.append(issue)
    return issues


def _qa_comparison_evidence(
    report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return stable evidence needed to compare two final QA evaluations."""

    if report is None:
        return {
            "qa_candidate_sha256": None,
            "qa_source_sha256": None,
            "qa_semantic_enabled": None,
            "qa_rendered_backend": None,
            "qa_rendered_capabilities": None,
        }
    metadata = report.get("metadata")
    rendered_layout = (
        metadata.get("rendered_layout") if isinstance(metadata, Mapping) else None
    )
    backend: Any = None
    capabilities: list[str] | None = None
    if isinstance(rendered_layout, Mapping):
        backend = _first(rendered_layout, ("backend", "provider"))
        raw_capabilities = rendered_layout.get("capabilities")
        if isinstance(raw_capabilities, Sequence) and not isinstance(
            raw_capabilities, (str, bytes, bytearray)
        ):
            capabilities = sorted(
                {str(value) for value in raw_capabilities if str(value).strip()}
            )
        elif raw_capabilities in (None, ""):
            # The rendered-layout envelope itself is evidence that this
            # backend declared no extra capabilities.
            capabilities = []
        else:
            capabilities = [str(raw_capabilities)]

    semantic_value = _first(report, ("semantic_qa", "semantic_qa_enabled"))
    semantic_enabled = semantic_value if isinstance(semantic_value, bool) else None
    return {
        "qa_candidate_sha256": _first(
            report, ("candidate_sha256", "document_sha256")
        ),
        "qa_source_sha256": _first(report, ("source_sha256",)),
        "qa_semantic_enabled": semantic_enabled,
        "qa_rendered_backend": backend,
        "qa_rendered_capabilities": capabilities,
    }


def _supplemental_report_paths(job_dir: Path) -> list[tuple[str, Path]]:
    """Return deterministic verification/repair report paths below a job."""

    reports_root = job_dir / "reports"
    discovered: dict[Path, str] = {}
    for directory, kind in (
        ("verification", "verification"),
        ("verifications", "verification"),
        ("verify", "verification"),
        ("repair", "repair"),
    ):
        root = reports_root / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*.json"):
            if path.is_file():
                discovered[path.resolve()] = kind
    return [
        (discovered[path], path)
        for path in sorted(discovered, key=lambda item: item.as_posix().casefold())
    ]


def _normalise_issue(
    issue: Mapping[str, Any],
    *,
    job_id: str,
    status: str,
    source: Mapping[str, Any],
    report_relpath: str | None,
) -> dict[str, Any]:
    severity = str(issue.get("severity") or "info").strip().lower()
    code = str(issue.get("code") or "UNSPECIFIED_ISSUE").strip() or "UNSPECIFIED_ISSUE"
    message = str(issue.get("message") or issue.get("summary") or "").strip()
    report_kind = str(issue.get("_report_kind") or "qa")
    stage = str(issue.get("stage") or report_kind).strip() or report_kind
    location = _location(issue)
    issue_id = str(issue.get("issue_id") or "").strip()
    priority, reason = _classification(issue)
    details = issue.get("details")
    if (
        not isinstance(details, (Mapping, list, str, int, float, bool))
        and details is not None
    ):
        details = str(details)

    return {
        "classification_rule_version": CLASSIFICATION_RULE_VERSION,
        "priority": priority,
        "counts_as_p0_p1": priority in {"P0", "P1"},
        "classification_reason": reason,
        "job_id": job_id,
        "job_status": status,
        **source,
        "issue_id": issue_id or None,
        "code": code,
        "message": message,
        "severity": severity,
        "stage": stage,
        "location": location,
        "location_key": _json_text(location),
        "recoverable": issue.get("recoverable"),
        "details": details if details is not None else {},
        "report_relpath": issue.get("_report_relpath") or report_relpath,
        "report_kind": report_kind,
        "synthetic": bool(issue.get("_synthetic", False)),
    }


def _deduplicate(issues: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    duplicate_count = 0
    for issue in issues:
        identity = str(issue.get("issue_id") or issue.get("code") or "").strip()
        if not identity:
            identity = hashlib.sha256(
                str(issue.get("message") or "").encode("utf-8")
            ).hexdigest()[:16]
        key = (
            str(issue.get("source_run_root") or issue.get("source_run_id") or ""),
            str(issue["job_id"]),
            identity,
            str(issue["stage"]),
            str(issue["location_key"]),
        )
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        unique.append(issue)
    unique.sort(
        key=lambda issue: (
            PRIORITY_ORDER.get(str(issue["priority"]), 99),
            str(issue["source_file"]).casefold(),
            str(issue.get("source_run_id") or ""),
            str(issue["job_id"]),
            str(issue["code"]),
            str(issue["location_key"]),
            str(issue.get("issue_id") or ""),
        )
    )
    return unique, duplicate_count


def _report_path(run_dir: Path, job_dir: Path, report_relpath: str) -> Path:
    value = Path(report_relpath)
    if value.is_absolute():
        candidate = value
    else:
        # The current batch schema defines report_relpath relative to the job
        # directory.  Accept a run-relative jobs/<id>/... path for old fixtures.
        parts = value.parts
        candidate = (
            run_dir / value if parts and parts[0].lower() == "jobs" else job_dir / value
        )
    return _safe_path(run_dir, candidate, "report_relpath")


def _job_dir(run_dir: Path, entry: Mapping[str, Any]) -> Path:
    raw = _first(entry, ("job_dir", "workspace_relpath", "job_relpath"))
    if raw:
        value = Path(str(raw))
        candidate = value if value.is_absolute() else run_dir / value
    else:
        candidate = run_dir / "jobs" / str(entry["job_id"])
    return _safe_path(run_dir, candidate, "job directory")


def _collect_job(
    run_dir: Path,
    index: Mapping[str, Any],
    entry: Mapping[str, Any],
    diagnostics: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    job_id = str(entry["job_id"])
    job_dir = _job_dir(run_dir, entry)

    state_sources: list[Mapping[str, Any]] = _nested_dicts(entry)
    for filename in (
        "terminal-state.json",
        "job-terminal.json",
        "job-result.json",
        "summary.json",
        "result.json",
        "status.json",
    ):
        state = _read_optional_json(job_dir / filename, diagnostics)
        if state is not None:
            state_sources.extend(_nested_dicts(state))

    tool_state = _read_optional_json(job_dir / "agent-tool-state.json", diagnostics)
    status = _normalise_status(
        _value_from_sources(
            state_sources,
            ("status", "terminal_status", "job_status", "state"),
        )
    )
    # A terminal/final envelope may carry the gate failure one level below the
    # batch entry.  Resolve these values from the same ordered state sources as
    # the status so a synthetic review issue retains the real gate identity.
    gate_error_code = str(
        _value_from_sources(state_sources, ("error_code",)) or ""
    ).strip()
    gate_error_summary = str(
        _value_from_sources(state_sources, ("error_summary",)) or ""
    ).strip()
    source = _source_info(entry, tool_state, index)

    report_field_names = (
        "report_relpath",
        "final_report_relpath",
        "qa_report_relpath",
    )
    # The batch entry is authoritative for the current runner.  In particular,
    # do not consume an interim candidate from agent-tool-state while the batch
    # entry still says PREPARING/RUNNING.  Older fixtures that only store the
    # terminal record under jobs/<id>/ remain supported once their status is
    # terminal.
    entry_sources = _nested_dicts(entry)
    report_value = _value_from_sources(entry_sources, report_field_names)
    qa_issue_counts_value = _value_from_sources(entry_sources, ("qa_issue_counts",))
    qa_issue_counts_empty = qa_issue_counts_value in (None, "", [], {})
    status_is_nonterminal = status in NONTERMINAL_STATUSES or status == "UNKNOWN"
    no_qa_candidate = (
        status in REVIEW_STATUSES and not report_value and qa_issue_counts_empty
    )
    if not report_value and not status_is_nonterminal and not no_qa_candidate:
        report_value = _value_from_sources(state_sources[1:], report_field_names)
    if (
        not report_value
        and not status_is_nonterminal
        and not no_qa_candidate
        and tool_state is not None
    ):
        report_value = _candidate_qa_from_tool_state(tool_state)
    report_relpath = str(report_value).replace("\\", "/") if report_value else None

    raw_issues: list[dict[str, Any]] = []
    terminal_issue = _terminal_issue(entry, status)
    if terminal_issue is not None:
        raw_issues.append(terminal_issue)
    if no_qa_candidate:
        raw_issues.append(
            _synthetic_issue(
                code=str(entry.get("error_code") or "NO_QA_CANDIDATE"),
                message=str(
                    entry.get("error_summary")
                    or (
                        "任务进入 NEEDS_REVIEW，但 report_relpath 和 "
                        "qa_issue_counts 均为空，未产出可进入 QA 的候选文件。"
                    )
                ),
                severity="error",
                stage="terminal",
                priority="P0",
                reason="needs_review_without_qa_candidate",
            )
        )

    report: Mapping[str, Any] | None = None
    report_error: str | None = None
    if report_relpath:
        try:
            path = _report_path(run_dir, job_dir, report_relpath)
            value = _load_json(path)
            if not isinstance(value, Mapping):
                raise AggregationError(f"QA report must be a JSON object: {path}")
            report = value
        except AggregationError as exc:
            report_error = str(exc)
            diagnostics.append(f"{job_id}: {report_error}")
    elif status not in FAILURE_STATUSES:
        report_error = "final report_relpath is missing"
        diagnostics.append(f"{job_id}: {report_error}")

    if report_error and status not in FAILURE_STATUSES and not no_qa_candidate:
        active = status in NONTERMINAL_STATUSES or status == "UNKNOWN"
        raw_issues.append(
            _synthetic_issue(
                code="AGGREGATOR_REPORT_MISSING_OR_INVALID",
                message=report_error,
                severity="warning" if active else "error",
                stage="aggregation",
                priority="warning" if active else "P1",
                reason=(
                    "nonterminal_job_has_no_final_report"
                    if active
                    else "terminal_job_has_no_readable_final_qa_report"
                ),
            )
        )

    report_status = "UNKNOWN"
    primary_report_error_count = 0
    if report is not None:
        report_issues = _report_issues(
            report,
            report_kind="qa",
            report_relpath=report_relpath,
        )
        raw_issues.extend(report_issues)
        report_status = _normalise_status(report.get("status"))
        primary_report_error_count = sum(
            1
            for issue in report_issues
            if str(issue.get("severity") or "").lower() == "error"
        )

    # Verification is a terminal gate and repair's accepted_issues is the
    # final accepted-candidate snapshot.  Both may contain blockers that are
    # absent from the final QA file referenced by batch-index.json.  Scan only
    # terminal jobs so in-flight candidate reports never pollute the summary.
    verification_p1_detail_found = False
    if (
        not status_is_nonterminal
        and status not in FAILURE_STATUSES
        and not no_qa_candidate
    ):
        for supplemental_kind, supplemental_path in _supplemental_report_paths(job_dir):
            supplemental_relpath = supplemental_path.relative_to(job_dir).as_posix()
            try:
                supplemental_value = _load_json(supplemental_path)
                if not isinstance(supplemental_value, Mapping):
                    raise AggregationError(
                        f"Supplemental report must be a JSON object: {supplemental_path}"
                    )
            except AggregationError as exc:
                diagnostics.append(f"{job_id}: {exc}")
                if supplemental_kind == "verification":
                    # For a review-terminal job with a durable gate error, the
                    # job envelope is the stable fallback when the report
                    # cannot be parsed.  Counting both would inflate P1 while
                    # hiding the actionable error_code/error_summary.
                    if not (
                        status in REVIEW_STATUSES
                        and (gate_error_code or gate_error_summary)
                    ):
                        issue = _synthetic_issue(
                            code="VERIFICATION_REPORT_INVALID",
                            message=str(exc),
                            severity="error",
                            stage="verification",
                            priority="P1",
                            reason="verification_report_unreadable",
                        )
                        issue["_report_relpath"] = supplemental_relpath
                        raw_issues.append(issue)
                continue

            supplemental_issues = _report_issues(
                supplemental_value,
                report_kind=supplemental_kind,
                report_relpath=supplemental_relpath,
            )
            raw_issues.extend(supplemental_issues)
            if supplemental_kind == "verification" and any(
                _classification(issue)[0] == "P1" for issue in supplemental_issues
            ):
                verification_p1_detail_found = True
            supplemental_status = _normalise_status(supplemental_value.get("status"))
            supplemental_error_count = sum(
                1
                for issue in supplemental_issues
                if str(issue.get("severity") or "").lower() == "error"
            )
            if (
                supplemental_kind == "verification"
                and supplemental_status in FAILURE_STATUSES | REVIEW_STATUSES
                and supplemental_error_count == 0
            ):
                supplemental_code = str(
                    _first(
                        supplemental_value,
                        ("error_code", "code"),
                    )
                    or "VERIFICATION_FAILED_WITHOUT_ERROR_DETAIL"
                )
                supplemental_message = str(
                    _first(
                        supplemental_value,
                        ("error_summary", "message", "error"),
                    )
                    or (
                        f"Verification 报告终态为 {supplemental_status}，"
                        "但未提供 severity=error 的问题明细。"
                    )
                )
                issue = _synthetic_issue(
                    code=supplemental_code,
                    message=supplemental_message,
                    severity="error",
                    stage="verification",
                    priority="P1",
                    reason="verification_blocks_without_error_detail",
                )
                issue["_report_relpath"] = supplemental_relpath
                raw_issues.append(issue)
                verification_p1_detail_found = True

    # A NEEDS_REVIEW final/verification gate is a review-level blocker, not a
    # FAILED job.  If final QA itself has no error and the merged detail still
    # contains no P1 (for example, only a catastrophic/P0 verification detail),
    # preserve the durable job gate error as one deterministic P1.
    if (
        status in REVIEW_STATUSES
        and primary_report_error_count == 0
        and (gate_error_code or gate_error_summary)
        and not verification_p1_detail_found
    ):
        gate_issue = _synthetic_issue(
            code=gate_error_code or "JOB_NEEDS_REVIEW",
            message=(
                gate_error_summary or f"任务终态为 {status}，最终交付门禁未通过。"
            ),
            severity="error",
            stage="terminal",
            priority="P1",
            reason="review_gate_error_without_p1_detail",
        )
        gate_issue["details"] = {
            "evidence_source": "job_terminal_state",
            "job_status": status,
            "error_code": gate_error_code or None,
            "error_summary": gate_error_summary or None,
        }
        raw_issues.append(gate_issue)

    detailed_error_count = sum(
        1 for issue in raw_issues if str(issue.get("severity") or "").lower() == "error"
    )
    if status in REVIEW_STATUSES and detailed_error_count == 0:
        review_code = gate_error_code or "JOB_NEEDS_REVIEW"
        review_message = str(
            gate_error_summary
            or f"任务终态为 {status}，但最终 QA/verification/repair 报告均无 error 明细。"
        )
        raw_issues.append(
            _synthetic_issue(
                code=review_code,
                message=review_message,
                severity="error",
                stage="terminal",
                priority="P1",
                reason="review_terminal_without_blocking_issue_detail",
            )
        )
    elif (
        status not in REVIEW_STATUSES | FAILURE_STATUSES
        and primary_report_error_count == 0
        and report_status in REVIEW_STATUSES | FAILURE_STATUSES
        and detailed_error_count == 0
    ):
        raw_issues.append(
            _synthetic_issue(
                code="QA_REPORT_BLOCKED_WITHOUT_ERROR_DETAIL",
                message=(
                    f"QA 报告终态为 {report_status}，但未提供 severity=error 的问题明细。"
                ),
                severity="error",
                stage="qa",
                priority="P1",
                reason="qa_report_blocks_without_error_detail",
            )
        )

    normalised = [
        _normalise_issue(
            issue,
            job_id=job_id,
            status=status,
            source=source,
            report_relpath=report_relpath,
        )
        for issue in raw_issues
    ]

    job_record: dict[str, Any] = {
        "job_id": job_id,
        "ordinal": entry.get("ordinal"),
        "status": status,
        **source,
        "report_relpath": report_relpath,
        "result_relpath": entry.get("result_relpath"),
        "elapsed_seconds": entry.get("elapsed_seconds"),
        "repair_round": entry.get("repair_round"),
        "qa_issue_counts": entry.get("qa_issue_counts"),
        "qa_candidate_present": bool(report_value or not qa_issue_counts_empty),
        # A path (or a candidate mentioned by tool state) is not QA coverage.
        # Coverage requires a report that was parsed and reached a durable QA
        # outcome; otherwise malformed/in-flight reports skew run comparisons.
        "qa_report_readable": report is not None,
        "qa_report_terminal": (
            report is not None
            and report_status in SUCCESS_STATUSES | REVIEW_STATUSES
        ),
        **_qa_comparison_evidence(report),
        "error_code": gate_error_code or None,
        "error_summary": gate_error_summary or None,
    }
    return job_record, normalised


def _new_group(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "jobs": set(),
        "affected_jobs": set(),
        "files": set(),
        "affected_files": set(),
        "counts": Counter(),
        "statuses": Counter(),
    }


def _group_rows(
    jobs: Sequence[Mapping[str, Any]], issues: Sequence[Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    dimensions: dict[str, defaultdict[str, dict[str, Any]]] = {
        name: defaultdict(lambda: _new_group(""))
        for name in (
            "by_file",
            "by_format",
            "by_source_collection",
            "by_source_language_manifest",
            "by_issue_code",
            "by_stage",
        )
    }

    def touch(dimension: str, key: str) -> dict[str, Any]:
        group = dimensions[dimension][key]
        group["key"] = key
        return group

    def job_key(value: Mapping[str, Any]) -> str:
        return (
            f"{value.get('source_run_root') or value.get('source_run_id') or ''}"
            f"\x00{value['job_id']}"
        )

    for job in jobs:
        for dimension, key in (
            ("by_file", str(job["source_file"])),
            ("by_format", str(job["source_format"])),
            ("by_source_collection", str(job["source_collection"])),
            (
                "by_source_language_manifest",
                str(job["source_language_manifest"]),
            ),
        ):
            group = touch(dimension, key)
            group["jobs"].add(job_key(job))
            group["files"].add(str(job["source_file"]))
            group["statuses"][str(job["status"])] += 1

    for issue in issues:
        job_id = job_key(issue)
        source_file = str(issue["source_file"])
        priority = str(issue["priority"])
        for dimension, key in (
            ("by_file", source_file),
            ("by_format", str(issue["source_format"])),
            ("by_source_collection", str(issue["source_collection"])),
            (
                "by_source_language_manifest",
                str(issue["source_language_manifest"]),
            ),
            ("by_issue_code", str(issue["code"])),
            ("by_stage", str(issue["stage"])),
        ):
            group = touch(dimension, key)
            group["jobs"].add(job_id)
            group["affected_jobs"].add(job_id)
            group["files"].add(source_file)
            group["affected_files"].add(source_file)
            group["counts"][priority] += 1

    output: dict[str, list[dict[str, Any]]] = {}
    for dimension, groups in dimensions.items():
        rows: list[dict[str, Any]] = []
        for key in sorted(groups, key=str.casefold):
            group = groups[key]
            counts = group["counts"]
            row = {
                "key": key,
                "jobs_total": len(group["jobs"]),
                "jobs_affected": len(group["affected_jobs"]),
                "files_total": len(group["files"]),
                "files_affected": len(group["affected_files"]),
                "issue_total": sum(counts.values()),
                "p0": counts["P0"],
                "p1": counts["P1"],
                "warning": counts["warning"],
                "info": counts["info"],
                "other": counts["other"],
            }
            if group["statuses"]:
                row["job_statuses"] = dict(sorted(group["statuses"].items()))
            rows.append(row)
        output[dimension] = rows
    return output


def _classification_metadata() -> dict[str, Any]:
    return {
        "rule_version": CLASSIFICATION_RULE_VERSION,
        "p0": (
            "任务失败/取消、NEEDS_REVIEW 且未产出可 QA 候选，或 severity=error "
            "且命中包损坏、无法打开、宏/VBA、"
            "加密、结构/关系/媒体丢失、哈希完整性、写回、转换灾难性规则。"
        ),
        "p1": (
            "其余 severity=error 的阻断 QA 问题；NEEDS_REVIEW 且最终 QA 无 error "
            "时，verification/final gate 失败至少保留一条可追溯 P1。"
        ),
        "warning": "记录 severity=warning，但不计入 P0/P1。",
        "info": "记录 severity=info，但不计入 P0/P1。",
        "deduplication_key": (
            "source_run_root + job_id + (issue_id if present else code) + "
            "stage + location (including stable object_identity when present)"
        ),
        "job_state_rules": {
            "failed_or_cancelled": "P0",
            "needs_review_without_report_and_qa_counts": "P0",
            "needs_review_with_qa_candidate_and_blocking_error": (
                "classify the error by catastrophic P0 patterns, otherwise P1"
            ),
            "needs_review_with_gate_error_but_no_final_qa_error": (
                "merge parseable verification detail first; otherwise synthesize "
                "one deterministic P1 from job error_code/error_summary"
            ),
        },
        "content_quality_stage_rule": {
            "stages": sorted(CONTENT_QUALITY_STAGES),
            "priority": "P1",
            "reason": (
                "semantic/content wording such as corrupt, missing, or invalid "
                "does not prove Office package corruption"
            ),
        },
        "p0_pattern_rules": [
            {"name": name, "pattern": pattern, "description": description}
            for name, pattern, description in P0_RULE_SPECS
        ],
    }


def _summary_csv(aggregations: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    fields = (
        "dimension",
        "value",
        "jobs_total",
        "jobs_affected",
        "files_total",
        "files_affected",
        "issue_total",
        "p0",
        "p1",
        "warning",
        "info",
        "other",
        "classification_rule_version",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for dimension in (
        "overall",
        "by_file",
        "by_format",
        "by_source_collection",
        "by_source_language_manifest",
        "by_issue_code",
        "by_stage",
    ):
        for row in aggregations.get(dimension, []):
            writer.writerow(
                {
                    "dimension": dimension,
                    "value": row["key"],
                    **{field: row.get(field, 0) for field in fields[2:-1]},
                    "classification_rule_version": CLASSIFICATION_RULE_VERSION,
                }
            )
    return stream.getvalue()


def _md(value: Any, limit: int = 180) -> str:
    text = str(value if value not in (None, "") else "-")
    text = re.sub(r"\s+", " ", text).replace("|", "\\|")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _markdown_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(_md(value) for value in row) + " |" for row in rows)
    return output


def _report_markdown(
    summary: Mapping[str, Any], issues: Sequence[Mapping[str, Any]]
) -> str:
    totals = summary["totals"]
    aggregations = summary["aggregations"]
    lines = [
        "# Word/PPT 翻译 Agent P0/P1 统计报告",
        "",
        f"- Run ID：`{_md(summary['run']['run_id'])}`",
        f"- 输出目录：`{_md(summary['merge']['output_dir'])}`",
        f"- 分类规则：`{CLASSIFICATION_RULE_VERSION}`",
        f"- 任务总数：{totals['jobs_total']}",
        f"- 去重后问题总数：{totals['issues_total']}",
        f"- 被去重的重复记录：{totals['duplicates_removed']}",
        f"- 合并覆盖：{summary['merge']['overrides_applied']}",
        f"- 因非终态而跳过的覆盖：{summary['merge']['nonterminal_overrides_skipped']}",
        "",
        "## 总览",
        "",
        *_markdown_table(
            ("P0", "P1", "Warning", "Info", "Other", "P0+P1"),
            (
                (
                    totals["p0"],
                    totals["p1"],
                    totals["warning"],
                    totals["info"],
                    totals["other"],
                    totals["p0"] + totals["p1"],
                ),
            ),
        ),
        "",
        "## 任务终态",
        "",
        *_markdown_table(
            ("状态", "任务数"),
            sorted(summary["jobs_by_status"].items()),
        ),
    ]

    lines.extend(
        [
            "",
            "## Source Runs",
            "",
            *_markdown_table(
                ("角色", "Run ID", "Run Root", "原任务", "最终采用"),
                (
                    (
                        source_run["role"],
                        source_run["run_id"],
                        source_run["run_root"],
                        source_run["jobs_available"],
                        source_run["jobs_selected"],
                    )
                    for source_run in summary["source_runs"]
                ),
            ),
        ]
    )

    dimension_titles = (
        ("by_format", "按格式"),
        ("by_source_collection", "按 Source Collection"),
        ("by_source_language_manifest", "按源语言（Manifest）"),
        ("by_issue_code", "按 Issue Code"),
        ("by_stage", "按阶段"),
        ("by_file", "按文件"),
    )
    for dimension, title in dimension_titles:
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                *_markdown_table(
                    ("值", "任务", "问题", "P0", "P1", "Warning", "Info", "Other"),
                    (
                        (
                            row["key"],
                            row["jobs_total"],
                            row["issue_total"],
                            row["p0"],
                            row["p1"],
                            row["warning"],
                            row["info"],
                            row["other"],
                        )
                        for row in aggregations[dimension]
                    ),
                ),
            ]
        )

    blocking = [issue for issue in issues if issue["priority"] in {"P0", "P1"}]
    lines.extend(
        [
            "",
            "## P0/P1 明细",
            "",
        ]
    )
    if blocking:
        display_limit = 500
        lines.extend(
            _markdown_table(
                ("级别", "文件", "Source Run", "Job", "Code", "阶段", "位置", "说明"),
                (
                    (
                        issue["priority"],
                        issue["source_file"],
                        issue["source_run_id"],
                        issue["job_id"],
                        issue["code"],
                        issue["stage"],
                        issue["location_key"],
                        issue["message"],
                    )
                    for issue in blocking[:display_limit]
                ),
            )
        )
        if len(blocking) > display_limit:
            lines.extend(
                [
                    "",
                    f"> Markdown 仅展示前 {display_limit} 条；全部 {len(blocking)} 条见 `issues.jsonl`。",
                ]
            )
    else:
        lines.append("未发现 P0/P1 问题。")

    lines.extend(
        [
            "",
            "## 分类口径",
            "",
            "- P0：失败/取消任务；`NEEDS_REVIEW` 且未产出可 QA 候选；或 `severity=error` 且属于包损坏、无法打开、宏/VBA、加密、结构/关系/媒体丢失、哈希完整性、写回或转换灾难性错误。",
            "- P1：其余 `severity=error` 阻断 QA 问题。",
            "- Warning/Info：完整记录，但不计入 P0/P1。",
            "- 去重键：`source_run_root + job_id + (issue_id 或 code) + stage + location`；"
            "`location` 包含适配器提供的稳定 `object_identity`。",
            "",
            "完整机器可读明细见 `issues.jsonl`，聚合见 `issue-summary.json` / `issue-summary.csv`。",
        ]
    )
    if summary["diagnostics"]:
        lines.extend(["", "## 聚合诊断", ""])
        lines.extend(f"- {_md(item, 500)}" for item in summary["diagnostics"])
    return "\n".join(lines) + "\n"


def _run_metadata(run_dir: Path, index: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_dir": str(run_dir),
        "run_id": str(index.get("run_id") or run_dir.name),
        "batch_schema_version": index.get("schema_version"),
        "corpus_root": index.get("corpus_root"),
        "corpus_manifest_sha256": index.get("corpus_manifest_sha256"),
        "execution_mode": index.get("execution_mode"),
        "source_language": index.get("source_language"),
        "target_language": index.get("target_language"),
        "language_routing_policy": index.get("language_routing_policy"),
        "quality_profile": index.get("quality_profile"),
        "max_repair_rounds": index.get("max_repair_rounds"),
        "provider_route": _first(
            index,
            (
                "provider_route",
                "translation_provider",
                "provider_model_route",
                "model_route",
            ),
        ),
        "concurrency": (
            index.get("concurrency")
            or index.get("batch_worker_concurrency")
            or index.get("service_concurrency_limit")
        ),
        "started_at": index.get("started_at"),
        "completed_at": index.get("completed_at"),
        "inputs_frozen": index.get("inputs_frozen"),
    }


def _load_source_run(
    run_dir: Path,
    *,
    role: str,
) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise AggregationError(f"run directory does not exist: {run_dir}")
    index_path = run_dir / "batch-index.json"
    index_value = _load_json(index_path)
    if not isinstance(index_value, Mapping):
        raise AggregationError(f"batch-index.json must be a JSON object: {index_path}")
    index = dict(index_value)
    entries = _job_entries(index)
    metadata = _run_metadata(run_dir, index)
    run_id = str(metadata["run_id"])

    local_diagnostics: list[str] = []
    bundles: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for entry in entries:
        job, job_issues = _collect_job(run_dir, index, entry, local_diagnostics)
        provenance = {
            "source_run_id": run_id,
            "source_run_root": str(run_dir),
            "source_run_contract": _run_contract_payload(metadata),
        }
        job.update(provenance)
        for issue in job_issues:
            issue.update(provenance)
        bundles.append((job, job_issues))

    return {
        "role": role,
        "run_root": str(run_dir),
        "run_id": run_id,
        "index": index,
        "metadata": metadata,
        "bundles": bundles,
        "diagnostics": [f"[{run_id}] {item}" for item in local_diagnostics],
    }


def _override_key(job: Mapping[str, Any]) -> str:
    # Corpus relative paths are the stable merge identity.  Normalise only path
    # separators and case so no host-specific absolute corpus path is needed.
    value = str(job.get("source_file") or "").replace("\\", "/").strip()
    value = re.sub(r"/{2,}", "/", value).removeprefix("./").strip("/")
    return value.casefold()


COMPARISON_TOTAL_METRICS = (
    "issues_before_deduplication",
    "duplicates_removed",
    "issues_total",
    "p0",
    "p1",
    "p0_p1",
    "warning",
    "info",
    "other",
)
COMPARISON_FILE_METRICS = (
    "issue_total",
    "p0",
    "p1",
    "p0_p1",
    "warning",
    "info",
    "other",
)
COMPARISON_CONTRACT_FIELDS = (
    "classification.rule_version",
    "run.batch_schema_version",
    "run.corpus_manifest_sha256",
    "run.execution_mode",
    "run.source_language",
    "run.target_language",
    "run.language_routing_policy",
    "run.quality_profile",
    "run.max_repair_rounds",
    "run.provider_route",
    "run.inputs_frozen",
)
COMPARISON_RUN_CONTRACT_KEYS = tuple(
    field.removeprefix("run.")
    for field in COMPARISON_CONTRACT_FIELDS
    if field.startswith("run.")
)
COMPARISON_COMMON_JOB_FIELDS = (
    "source_sha256",
    "normalized_source_sha256",
    "source_language_request",
    "target_language_request",
    "resolved_source_language",
    "resolved_target_language",
    "qa_semantic_enabled",
    "qa_rendered_backend",
    "qa_rendered_capabilities",
    "qa_source_sha256",
)
COMPARISON_COMMON_JOB_REQUIRED_FIELDS = (
    *COMPARISON_COMMON_JOB_FIELDS,
    "qa_candidate_sha256",
)
_MISSING = object()


def compare_issue_summaries(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Separate quality changes on common QA coverage from coverage churn."""

    baseline_covered = _qa_covered_file_keys(baseline)
    current_covered = _qa_covered_file_keys(current)
    common = baseline_covered & current_covered
    newly_covered = current_covered - baseline_covered
    lost_qa = baseline_covered - current_covered
    baseline_by_file = _summary_by_file(baseline)
    current_by_file = _summary_by_file(current)

    baseline_totals = _metric_values(
        baseline.get("totals"),
        COMPARISON_TOTAL_METRICS,
    )
    current_totals = _metric_values(
        current.get("totals"),
        COMPARISON_TOTAL_METRICS,
    )
    total_delta = {
        metric: current_totals[metric] - baseline_totals[metric]
        for metric in COMPARISON_TOTAL_METRICS
    }
    common_baseline = _sum_file_metrics(baseline_by_file, common)
    common_current = _sum_file_metrics(current_by_file, common)
    common_delta = {
        metric: common_current[metric] - common_baseline[metric]
        for metric in COMPARISON_FILE_METRICS
    }
    newly_contribution = _sum_file_metrics(current_by_file, newly_covered)
    lost_contribution = _sum_file_metrics(baseline_by_file, lost_qa)
    comparability = _comparison_comparability(baseline, current, common)
    comparable = bool(comparability["comparable"])
    reconciliation: dict[str, dict[str, int]] = {}
    for metric in COMPARISON_FILE_METRICS:
        total_metric = "issues_total" if metric == "issue_total" else metric
        reconciled = (
            common_delta[metric]
            + newly_contribution[metric]
            - lost_contribution[metric]
        )
        reported = total_delta.get(total_metric, 0)
        reconciliation[metric] = {
            "common_delta": common_delta[metric],
            "newly_covered_contribution": newly_contribution[metric],
            "lost_qa_baseline_contribution": lost_contribution[metric],
            "reconciled_total_delta": reconciled,
            "reported_total_delta": reported,
        }

    return {
        "schema_version": "1.1",
        "baseline_run_id": _summary_run_id(baseline),
        "current_run_id": _summary_run_id(current),
        "comparability": comparability,
        "coverage": {
            "baseline_qa_covered_files": len(baseline_covered),
            "current_qa_covered_files": len(current_covered),
            "common_covered_files": sorted(common),
            "newly_covered_files": sorted(newly_covered),
            "lost_qa_files": sorted(lost_qa),
        },
        "totals": {
            "baseline": baseline_totals,
            "current": current_totals,
            "delta": total_delta if comparable else None,
        },
        "common_covered_totals": {
            "baseline": common_baseline,
            "current": common_current,
            "delta": common_delta if comparable else None,
        },
        "newly_covered_contribution": newly_contribution,
        "lost_qa_baseline_contribution": lost_contribution,
        "coverage_reconciliation": reconciliation if comparable else None,
    }


def _comparison_comparability(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    common_files: set[str],
) -> dict[str, Any]:
    """Validate the immutable run contract and common-file QA capabilities."""

    missing_fields: set[str] = set()
    mismatched_fields: set[str] = set()
    invalid_fields: set[str] = set()
    for field in COMPARISON_CONTRACT_FIELDS:
        baseline_value = _nested_value(baseline, field)
        current_value = _nested_value(current, field)
        if _comparison_value_missing(baseline_value):
            missing_fields.add(f"baseline.{field}")
        if _comparison_value_missing(current_value):
            missing_fields.add(f"current.{field}")
        if (
            not _comparison_value_missing(baseline_value)
            and not _comparison_value_missing(current_value)
            and _canonical_comparison_value(baseline_value)
            != _canonical_comparison_value(current_value)
        ):
            mismatched_fields.add(field)
    if (
        _nested_value(baseline, "run.inputs_frozen") is not True
        or _nested_value(current, "run.inputs_frozen") is not True
    ):
        invalid_fields.add("run.inputs_frozen")

    baseline_jobs = _summary_jobs_by_file(baseline)
    current_jobs = _summary_jobs_by_file(current)
    for label, summary, jobs in (
        ("baseline", baseline, baseline_jobs),
        ("current", current, current_jobs),
    ):
        summary_contract = _run_contract_payload(
            summary.get("run") if isinstance(summary.get("run"), Mapping) else {}
        )
        for file_key, job in sorted(jobs.items()):
            job_contract = job.get("source_run_contract")
            if not isinstance(job_contract, Mapping):
                missing_fields.add(
                    f"{label}.jobs.{file_key}.source_run_contract"
                )
                continue
            for field in COMPARISON_RUN_CONTRACT_KEYS:
                summary_value = summary_contract.get(field, _MISSING)
                job_value = job_contract.get(field, _MISSING)
                path = f"{label}.jobs.{file_key}.source_run_contract.{field}"
                if _comparison_value_missing(job_value):
                    missing_fields.add(path)
                    continue
                if (
                    not _comparison_value_missing(summary_value)
                    and _canonical_comparison_value(job_value)
                    != _canonical_comparison_value(summary_value)
                ):
                    mismatched_fields.add(path)
                if field == "inputs_frozen" and job_value is not True:
                    invalid_fields.add(path)

    for file_key in sorted(common_files):
        baseline_job = baseline_jobs.get(file_key)
        current_job = current_jobs.get(file_key)
        if baseline_job is None:
            missing_fields.add(f"baseline.jobs.{file_key}")
            continue
        if current_job is None:
            missing_fields.add(f"current.jobs.{file_key}")
            continue
        for field in COMPARISON_COMMON_JOB_REQUIRED_FIELDS:
            baseline_value = baseline_job.get(field, _MISSING)
            current_value = current_job.get(field, _MISSING)
            if _comparison_value_missing(baseline_value):
                missing_fields.add(f"baseline.jobs.{file_key}.{field}")
            if _comparison_value_missing(current_value):
                missing_fields.add(f"current.jobs.{file_key}.{field}")
        for field in COMPARISON_COMMON_JOB_FIELDS:
            baseline_value = baseline_job.get(field, _MISSING)
            current_value = current_job.get(field, _MISSING)
            if (
                not _comparison_value_missing(baseline_value)
                and not _comparison_value_missing(current_value)
                and _canonical_comparison_value(baseline_value)
                != _canonical_comparison_value(current_value)
            ):
                mismatched_fields.add(f"jobs.{file_key}.{field}")
        for label, job in (
            ("baseline", baseline_job),
            ("current", current_job),
        ):
            normalized_source = job.get("normalized_source_sha256", _MISSING)
            qa_source = job.get("qa_source_sha256", _MISSING)
            if (
                not _comparison_value_missing(normalized_source)
                and not _comparison_value_missing(qa_source)
                and _canonical_comparison_value(normalized_source)
                != _canonical_comparison_value(qa_source)
            ):
                invalid_fields.add(
                    f"{label}.jobs.{file_key}."
                    "normalized_source_sha256_vs_qa_source_sha256"
                )

    return {
        "comparable": (
            not missing_fields and not mismatched_fields and not invalid_fields
        ),
        "mismatched_fields": sorted(mismatched_fields),
        "missing_fields": sorted(missing_fields),
        "invalid_fields": sorted(invalid_fields),
        "common_file_contracts_checked": len(common_files),
    }


def _run_contract_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only immutable evaluation fields into each selected job."""

    return {
        field: metadata.get(field)
        for field in COMPARISON_RUN_CONTRACT_KEYS
    }


def _nested_value(mapping: Mapping[str, Any], dotted_path: str) -> Any:
    value: Any = mapping
    for segment in dotted_path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return _MISSING
        value = value[segment]
    return value


def _comparison_value_missing(value: Any) -> bool:
    return value is _MISSING or value is None or value == ""


def _canonical_comparison_value(value: Any) -> str:
    if isinstance(value, set):
        value = sorted(value, key=str)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _summary_jobs_by_file(
    summary: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    jobs = summary.get("jobs")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes, bytearray)):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        key = _summary_file_key(job.get("source_file"))
        if key:
            result[key] = job
    return result


def _summary_run_id(summary: Mapping[str, Any]) -> str:
    run = summary.get("run")
    return str(run.get("run_id", "unknown")) if isinstance(run, Mapping) else "unknown"


def _summary_file_key(value: Any) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    normalized = re.sub(r"/{2,}", "/", normalized).removeprefix("./").strip("/")
    return normalized.casefold()


def _qa_covered_file_keys(summary: Mapping[str, Any]) -> set[str]:
    jobs = summary.get("jobs")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes, bytearray)):
        return set()
    covered: set[str] = set()
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        if (
            job.get("qa_report_readable") is not True
            or job.get("qa_report_terminal") is not True
        ):
            continue
        key = _summary_file_key(job.get("source_file"))
        if key:
            covered.add(key)
    return covered


def _summary_by_file(
    summary: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    aggregations = summary.get("aggregations")
    if not isinstance(aggregations, Mapping):
        return {}
    rows = aggregations.get("by_file")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = _summary_file_key(row.get("key"))
        if key:
            result[key] = row
    return result


def _metric_values(value: Any, metrics: Sequence[str]) -> dict[str, int]:
    mapping = value if isinstance(value, Mapping) else {}
    result: dict[str, int] = {}
    for metric in metrics:
        if metric == "p0_p1" and mapping.get(metric) is None:
            result[metric] = int(mapping.get("p0", 0)) + int(mapping.get("p1", 0))
        else:
            result[metric] = int(mapping.get(metric, 0))
    return result


def _sum_file_metrics(
    rows: Mapping[str, Mapping[str, Any]],
    keys: Iterable[str],
) -> dict[str, int]:
    result = {metric: 0 for metric in COMPARISON_FILE_METRICS}
    for key in keys:
        values = _metric_values(rows.get(key), COMPARISON_FILE_METRICS)
        for metric, value in values.items():
            result[metric] += value
    return result


def aggregate(
    run_dir: Path,
    *,
    fallback_run_dirs: Sequence[Path] = (),
    output_dir: Path | None = None,
) -> dict[str, Any]:
    main_root = run_dir.expanduser().resolve()

    # Later sources have higher priority.  Repeating a root is harmless; its
    # last role wins, which also makes --run-dir authoritative if it was
    # accidentally supplied as a fallback.
    source_specs: list[tuple[str, Path]] = []
    for ordinal, fallback in enumerate(fallback_run_dirs, start=1):
        resolved = fallback.expanduser().resolve()
        source_specs = [(role, root) for role, root in source_specs if root != resolved]
        source_specs.append((f"fallback-{ordinal}", resolved))
    source_specs = [(role, root) for role, root in source_specs if root != main_root]
    source_specs.append(("main", main_root))

    source_runs = [
        _load_source_run(source_root, role=role) for role, source_root in source_specs
    ]
    main_run = source_runs[-1]

    diagnostics: list[str] = []
    selected: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    selection_owner: dict[str, str] = {}
    overrides_applied = 0
    nonterminal_overrides_skipped = 0
    for source_run in source_runs:
        diagnostics.extend(source_run["diagnostics"])
        for job, job_issues in source_run["bundles"]:
            key = _override_key(job)
            if not key:
                key = f"__job__/{source_run['run_root']}/{job['job_id']}".casefold()
            previous = selected.get(key)
            if previous is not None:
                previous_job = previous[0]
                if _is_terminal_status(
                    previous_job.get("status")
                ) and not _is_terminal_status(job.get("status")):
                    nonterminal_overrides_skipped += 1
                    diagnostics.append(
                        "merge retained terminal relative_path "
                        f"{previous_job['source_file']!r} from {selection_owner[key]}; "
                        f"ignored nonterminal {job['status']} from {source_run['run_id']}"
                    )
                    continue
                overrides_applied += 1
                diagnostics.append(
                    "merge override for relative_path "
                    f"{job['source_file']!r}: {selection_owner[key]} -> "
                    f"{source_run['run_id']}"
                )
            selected[key] = (job, job_issues)
            selection_owner[key] = str(source_run["run_id"])

    selected_bundles = [selected[key] for key in sorted(selected)]
    jobs = [job for job, _job_issues in selected_bundles]
    raw_issues = [
        issue for _job, job_issues in selected_bundles for issue in job_issues
    ]

    if output_dir is None:
        resolved_output_dir = main_root
    else:
        resolved_output_dir = output_dir.expanduser().resolve()
        try:
            resolved_output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AggregationError(
                f"cannot create output directory {resolved_output_dir}: {exc}"
            ) from exc
        if not resolved_output_dir.is_dir():
            raise AggregationError(
                f"output path is not a directory: {resolved_output_dir}"
            )

    issues, duplicates_removed = _deduplicate(raw_issues)
    counts = Counter(str(issue["priority"]) for issue in issues)
    jobs_by_status = Counter(str(job["status"]) for job in jobs)
    group_rows = _group_rows(jobs, issues)
    affected_job_keys = {
        (
            str(issue.get("source_run_root") or issue.get("source_run_id") or ""),
            str(issue["job_id"]),
        )
        for issue in issues
    }
    overall = {
        "key": "all",
        "jobs_total": len(jobs),
        "jobs_affected": len(affected_job_keys),
        "files_total": len({str(job["source_file"]) for job in jobs}),
        "files_affected": len({str(issue["source_file"]) for issue in issues}),
        "issue_total": len(issues),
        "p0": counts["P0"],
        "p1": counts["P1"],
        "warning": counts["warning"],
        "info": counts["info"],
        "other": counts["other"],
    }
    aggregations = {"overall": [overall], **group_rows}

    source_run_summaries: list[dict[str, Any]] = []
    selected_counts = Counter(str(job["source_run_root"]) for job in jobs)
    for source_run in source_runs:
        source_run_summaries.append(
            {
                "role": source_run["role"],
                "run_id": source_run["run_id"],
                "run_root": source_run["run_root"],
                "jobs_available": len(source_run["bundles"]),
                "jobs_selected": selected_counts[source_run["run_root"]],
            }
        )

    summary: dict[str, Any] = {
        "schema_version": "1.0",
        "classification": _classification_metadata(),
        "run": main_run["metadata"],
        "merge": {
            "enabled": len(source_runs) > 1,
            "override_key": "relative_path (normalised separators and case)",
            "precedence": (
                "terminal beats nonterminal; otherwise later fallback overrides "
                "earlier fallback and main overrides all fallbacks"
            ),
            "terminal_precedence": (
                "a nonterminal job never overrides an already selected terminal job"
            ),
            "fallback_run_count": len(source_runs) - 1,
            "overrides_applied": overrides_applied,
            "nonterminal_overrides_skipped": nonterminal_overrides_skipped,
            "output_dir": str(resolved_output_dir),
        },
        "source_runs": source_run_summaries,
        "totals": {
            "jobs_total": len(jobs),
            "jobs_success": sum(jobs_by_status[status] for status in SUCCESS_STATUSES),
            "jobs_failed_or_cancelled": sum(
                jobs_by_status[status] for status in FAILURE_STATUSES
            ),
            "jobs_needs_review": sum(
                jobs_by_status[status] for status in REVIEW_STATUSES
            ),
            "jobs_nonterminal_or_unknown": sum(
                count
                for status, count in jobs_by_status.items()
                if status in NONTERMINAL_STATUSES or status == "UNKNOWN"
            ),
            "issues_before_deduplication": len(raw_issues),
            "duplicates_removed": duplicates_removed,
            "issues_total": len(issues),
            "p0": counts["P0"],
            "p1": counts["P1"],
            "p0_p1": counts["P0"] + counts["P1"],
            "warning": counts["warning"],
            "info": counts["info"],
            "other": counts["other"],
        },
        "jobs_by_status": dict(sorted(jobs_by_status.items())),
        "aggregations": aggregations,
        "jobs": sorted(
            jobs,
            key=lambda job: (
                str(job["source_file"]).casefold(),
                str(job["source_run_id"]),
                int(job.get("ordinal") or 0),
                str(job["job_id"]),
            ),
        ),
        "diagnostics": sorted(set(diagnostics)),
        "outputs": list(OUTPUT_FILENAMES),
    }

    _atomic_write_text(
        resolved_output_dir / "issue-summary.json", _json_text(summary, indent=2) + "\n"
    )
    _atomic_write_text(
        resolved_output_dir / "issue-summary.csv", _summary_csv(aggregations)
    )
    jsonl = "".join(_json_text(issue) + "\n" for issue in issues)
    _atomic_write_text(resolved_output_dir / "issues.jsonl", jsonl)
    _atomic_write_text(
        resolved_output_dir / "P0_P1_REPORT.md", _report_markdown(summary, issues)
    )
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate a batch translation run's final QA reports into versioned "
            "P0/P1, warning and info statistics."
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help=(
            "Primary/retry batch run containing batch-index.json and jobs/<job_id>/. "
            "Its relative_path entries override fallback runs."
        ),
    )
    parser.add_argument(
        "--fallback-run-dir",
        "--base-run-dir",
        dest="fallback_run_dirs",
        action="append",
        default=[],
        type=Path,
        help=(
            "Base batch run to retain for files absent from --run-dir. Repeatable; "
            "later fallbacks override earlier ones."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for the four aggregate reports (default: --run-dir).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = aggregate(
            args.run_dir,
            fallback_run_dirs=args.fallback_run_dirs,
            output_dir=args.output_dir,
        )
    except AggregationError as exc:
        print(f"aggregation failed: {exc}", file=sys.stderr)
        return 2
    totals = summary["totals"]
    print(
        _json_text(
            {
                "run_id": summary["run"]["run_id"],
                "jobs": totals["jobs_total"],
                "p0": totals["p0"],
                "p1": totals["p1"],
                "warning": totals["warning"],
                "info": totals["info"],
                "duplicates_removed": totals["duplicates_removed"],
                "classification_rule_version": CLASSIFICATION_RULE_VERSION,
                "source_runs": len(summary["source_runs"]),
                "output_dir": summary["merge"]["output_dir"],
                "outputs": summary["outputs"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
