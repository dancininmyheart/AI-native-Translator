#!/usr/bin/env python3
"""Maintain an atomic run ledger for Pi-orchestrated Office translation."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
LEDGER_NAME = "operator-run.json"

MODES = {"pi_agent_live", "pretranslated_qa"}
FILE_TYPES = {"doc", "docx", "ppt", "pptx"}
OUTPUT_TYPE_BY_INPUT = {
    "doc": "docx",
    "docx": "docx",
    "ppt": "pptx",
    "pptx": "pptx",
}
QUALITY_PROFILES = {"fast", "standard", "strict"}

DEFAULT_MAX_REPAIR_ROUNDS = 2
HARD_MAX_REPAIR_ROUNDS = 3
TARGET_BUDGET_SECONDS = 30 * 60
SOFT_DEADLINE_SECONDS = 27 * 60
CLEANUP_GRACE_SECONDS = 5 * 60
DEFAULT_SERVICE_CONCURRENCY_LIMIT = 20
HARD_SERVICE_CONCURRENCY_LIMIT = 20

STAGES = {
    "preflight_document",
    "convert_legacy",
    "translate_once",
    "run_document_qa",
    "apply_repair",
    "verify_candidate",
    "finalize_result",
}
TERMINAL_STATUSES = {
    "completed",
    "completed_with_warnings",
    "needs_review",
    "failed",
    "cancelled",
}
DELIVERED_STATUSES = {"completed", "completed_with_warnings"}
ARTIFACT_KINDS = {
    "candidate",
    "output",
    "report",
    "audit",
    "manifest",
    "evidence",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ledger_path(run_dir: Path) -> Path:
    return run_dir / LEDGER_NAME


def load_ledger(run_dir: Path) -> dict[str, Any]:
    path = ledger_path(run_dir.resolve())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"ledger not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ledger is not valid UTF-8 JSON: {path}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported ledger schema: {data.get('schema_version')!r}"
        )
    return data


def _fsync_directory(directory: Path) -> None:
    """Durably commit a rename on Linux without breaking Windows tests."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    finally:
        os.close(directory_fd)


def atomic_write(run_dir: Path, data: dict[str, Any]) -> None:
    run_dir = run_dir.resolve()
    path = ledger_path(run_dir)
    temp = run_dir / f".{LEDGER_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            file_descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_directory(run_dir)
    except BaseException:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def require_no_open_stage(data: dict[str, Any]) -> None:
    if data.get("current_stage") is not None:
        raise RuntimeError("a stage is already active")


def require_nonterminal(data: dict[str, Any]) -> None:
    if data.get("status") in TERMINAL_STATUSES:
        raise RuntimeError("run is already terminal")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _completed_stage_count(data: dict[str, Any], stage: str) -> int:
    return sum(
        1
        for record in data.get("stages", [])
        if record.get("stage") == stage and record.get("status") == "completed"
    )


def _stage_attempt_count(data: dict[str, Any], stage: str) -> int:
    return sum(
        1 for record in data.get("stages", []) if record.get("stage") == stage
    )


def _deadline_state(data: dict[str, Any], now_epoch: float) -> str:
    if now_epoch <= float(data["target_deadline_epoch"]):
        return "within_target"
    if now_epoch <= float(data["cleanup_deadline_epoch"]):
        return "cleanup_grace"
    return "expired"


def finish_current_stage(
    data: dict[str, Any],
    *,
    expected_stage: str | None,
    status: str,
    now_epoch: float,
    now_text: str,
) -> None:
    current = data.get("current_stage")
    if not isinstance(current, dict):
        raise RuntimeError("no stage is active")
    if expected_stage is not None and current.get("stage") != expected_stage:
        raise RuntimeError(
            f"active stage is {current.get('stage')!r}, not {expected_stage!r}"
        )
    started_epoch = float(current["started_epoch"])
    record = dict(current)
    record.update(
        {
            "finished_at": now_text,
            "finished_epoch": now_epoch,
            "elapsed_seconds": round(max(0.0, now_epoch - started_epoch), 6),
            "status": status,
        }
    )
    data.setdefault("stages", []).append(record)
    data["current_stage"] = None
    data["updated_at"] = now_text


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    source = args.source.resolve()
    if not source.is_file():
        raise RuntimeError(f"source is unavailable: {source}")
    detected_type = source.suffix.lower().lstrip(".")
    declared_type = args.file_type.lower()
    if declared_type not in FILE_TYPES:
        raise RuntimeError(f"unsupported file type: {declared_type}")
    if detected_type != declared_type:
        raise RuntimeError(
            f"source suffix {detected_type!r} does not match file type "
            f"{declared_type!r}"
        )
    if args.mode not in MODES:
        raise RuntimeError(f"unsupported mode: {args.mode}")
    if args.quality_profile not in QUALITY_PROFILES:
        raise RuntimeError(f"unsupported quality profile: {args.quality_profile}")
    if args.page_count < 0:
        raise RuntimeError("page/slide count cannot be negative")
    if not 0 <= args.max_repair_rounds <= HARD_MAX_REPAIR_ROUNDS:
        raise RuntimeError(
            f"max repair rounds must be within 0..{HARD_MAX_REPAIR_ROUNDS}"
        )
    if not 1 <= args.service_concurrency_limit <= HARD_SERVICE_CONCURRENCY_LIMIT:
        raise RuntimeError(
            "service concurrency limit must be within "
            f"1..{HARD_SERVICE_CONCURRENCY_LIMIT} document jobs"
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_path(run_dir)
    if path.exists():
        raise RuntimeError(f"refusing to overwrite ledger: {path}")

    now_epoch = time.time()
    now_text = utc_now()
    data = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "execution_mode": args.mode,
        "translation_provenance": (
            "configured_provider"
            if args.mode == "pi_agent_live"
            else "pretranslated"
        ),
        "external_services": {
            "translation_api": args.mode == "pi_agent_live",
            "semantic_evaluation": False,
            "image_translation": False,
        },
        "source_path": str(source),
        "source_sha256": sha256_file(source),
        "source_size_bytes": source.stat().st_size,
        "file_type": declared_type,
        "expected_output_type": OUTPUT_TYPE_BY_INPUT[declared_type],
        "legacy_conversion_required": declared_type in {"doc", "ppt"},
        "target_language": args.target_language,
        "page_count": args.page_count,
        "quality_profile": args.quality_profile,
        "max_repair_rounds": args.max_repair_rounds,
        "hard_max_repair_rounds": HARD_MAX_REPAIR_ROUNDS,
        "target_budget_seconds": TARGET_BUDGET_SECONDS,
        "soft_deadline_seconds": SOFT_DEADLINE_SECONDS,
        "cleanup_grace_seconds": CLEANUP_GRACE_SECONDS,
        "soft_deadline_epoch": now_epoch + SOFT_DEADLINE_SECONDS,
        "target_deadline_epoch": now_epoch + TARGET_BUDGET_SECONDS,
        "cleanup_deadline_epoch": (
            now_epoch + TARGET_BUDGET_SECONDS + CLEANUP_GRACE_SECONDS
        ),
        "service_concurrency_limit": args.service_concurrency_limit,
        "service_concurrency_semantics": "active_document_jobs",
        "created_at": now_text,
        "created_epoch": now_epoch,
        "updated_at": now_text,
        "status": "active",
        "current_stage": None,
        "stages": [],
        "interruptions": [],
        "open_interruption": None,
        "resume_count": 0,
        "artifacts": [],
    }
    atomic_write(run_dir, data)
    return data


def command_start(args: argparse.Namespace) -> dict[str, Any]:
    if args.stage not in STAGES:
        raise RuntimeError(f"unsupported stage: {args.stage}")
    run_dir = args.run_dir.resolve()
    data = load_ledger(run_dir)
    require_nonterminal(data)
    require_no_open_stage(data)
    if data.get("status") == "interrupted":
        raise RuntimeError("resume the run before starting a stage")

    now_epoch = time.time()
    deadline_state = _deadline_state(data, now_epoch)
    if deadline_state == "expired":
        raise RuntimeError("cleanup deadline has expired")
    if deadline_state == "cleanup_grace" and args.stage not in {
        "verify_candidate",
        "finalize_result",
    }:
        raise RuntimeError(
            "target budget is exhausted; only verification/finalization may run"
        )
    if args.stage == "translate_once" and data["execution_mode"] != "pi_agent_live":
        raise RuntimeError("translate_once is forbidden in pretranslated_qa")
    if args.stage == "convert_legacy" and not data["legacy_conversion_required"]:
        raise RuntimeError("convert_legacy is valid only for DOC/PPT input")
    if args.stage == "apply_repair":
        if now_epoch > float(data["soft_deadline_epoch"]):
            raise RuntimeError("soft deadline forbids another repair iteration")
        attempted = _stage_attempt_count(data, "apply_repair")
        if attempted >= int(data["max_repair_rounds"]):
            raise RuntimeError("configured repair iteration limit reached")

    attempt = 1 + sum(
        1 for record in data.get("stages", []) if record.get("stage") == args.stage
    )
    now_text = utc_now()
    data["current_stage"] = {
        "stage": args.stage,
        "attempt": attempt,
        "started_at": now_text,
        "started_epoch": now_epoch,
    }
    data["updated_at"] = now_text
    atomic_write(run_dir, data)
    return data


def command_finish(args: argparse.Namespace) -> dict[str, Any]:
    if args.stage not in STAGES:
        raise RuntimeError(f"unsupported stage: {args.stage}")
    if args.status not in {"completed", "failed"}:
        raise RuntimeError("invalid stage status")
    run_dir = args.run_dir.resolve()
    data = load_ledger(run_dir)
    require_nonterminal(data)
    finish_current_stage(
        data,
        expected_stage=args.stage,
        status=args.status,
        now_epoch=time.time(),
        now_text=utc_now(),
    )
    atomic_write(run_dir, data)
    return data


def command_interrupt(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    data = load_ledger(run_dir)
    require_nonterminal(data)
    if data.get("status") == "interrupted":
        raise RuntimeError("run is already interrupted")
    now_epoch = time.time()
    now_text = utc_now()
    if data.get("current_stage") is not None:
        finish_current_stage(
            data,
            expected_stage=None,
            status="interrupted",
            now_epoch=now_epoch,
            now_text=now_text,
        )
    data["status"] = "interrupted"
    data["open_interruption"] = {
        "reason": args.reason,
        "started_at": now_text,
        "started_epoch": now_epoch,
    }
    data["updated_at"] = now_text
    atomic_write(run_dir, data)
    return data


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    data = load_ledger(run_dir)
    require_nonterminal(data)
    if data.get("status") != "interrupted":
        raise RuntimeError("run is not interrupted")
    interruption = data.get("open_interruption")
    if not isinstance(interruption, dict):
        raise RuntimeError("open interruption evidence is unavailable")
    now_epoch = time.time()
    now_text = utc_now()
    record = dict(interruption)
    record.update(
        {
            "finished_at": now_text,
            "finished_epoch": now_epoch,
            "elapsed_seconds": round(
                max(0.0, now_epoch - float(interruption["started_epoch"])), 6
            ),
        }
    )
    data.setdefault("interruptions", []).append(record)
    data["open_interruption"] = None
    data["status"] = "active"
    data["resume_count"] = int(data.get("resume_count", 0)) + 1
    data["updated_at"] = now_text
    atomic_write(run_dir, data)
    return data


def command_artifact(args: argparse.Namespace) -> dict[str, Any]:
    if args.kind not in ARTIFACT_KINDS:
        raise RuntimeError(f"unsupported artifact kind: {args.kind}")
    run_dir = args.run_dir.resolve()
    data = load_ledger(run_dir)
    require_nonterminal(data)
    path = args.path.resolve()
    if not path.is_file():
        raise RuntimeError(f"artifact is unavailable: {path}")
    if not _is_within(path, run_dir):
        raise RuntimeError("artifact must remain inside the run directory")
    if args.kind in {"candidate", "output"}:
        actual_type = path.suffix.lower().lstrip(".")
        expected_type = data["expected_output_type"]
        if actual_type != expected_type:
            raise RuntimeError(
                f"{args.kind} must be {expected_type.upper()}, not "
                f"{actual_type.upper() or 'extensionless'}"
            )
    if any(
        Path(existing.get("path", "")).resolve() == path
        for existing in data.get("artifacts", [])
    ):
        raise RuntimeError(f"artifact path is already registered and immutable: {path}")
    record = {
        "kind": args.kind,
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "recorded_at": utc_now(),
    }
    data.setdefault("artifacts", []).append(record)
    data["updated_at"] = record["recorded_at"]
    atomic_write(run_dir, data)
    return record


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    data = load_ledger(args.run_dir.resolve())
    failures: list[str] = []
    source = Path(data["source_path"])
    if not source.is_file() or sha256_file(source) != data["source_sha256"]:
        failures.append("source_digest_mismatch")
    for index, artifact in enumerate(data.get("artifacts", [])):
        path = Path(artifact["path"])
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            failures.append(f"artifact_digest_mismatch:{index}:{artifact['kind']}")
    result = {"verified": not failures, "failures": failures}
    if failures:
        raise RuntimeError(json.dumps(result, sort_keys=True))
    return result


def command_close(args: argparse.Namespace) -> dict[str, Any]:
    if args.status not in TERMINAL_STATUSES:
        raise RuntimeError("invalid terminal status")
    run_dir = args.run_dir.resolve()
    data = load_ledger(run_dir)
    require_nonterminal(data)
    require_no_open_stage(data)
    if data.get("open_interruption") is not None:
        raise RuntimeError("resume before closing the run")

    if args.status in DELIVERED_STATUSES:
        if _deadline_state(data, time.time()) == "expired":
            raise RuntimeError("expired runs cannot be marked completed")
        if not any(item.get("kind") == "output" for item in data["artifacts"]):
            raise RuntimeError("completed runs require a registered output artifact")
        required_stages = {
            "preflight_document",
            "run_document_qa",
            "verify_candidate",
            "finalize_result",
        }
        if data["execution_mode"] == "pi_agent_live":
            required_stages.add("translate_once")
        missing = sorted(
            stage
            for stage in required_stages
            if _completed_stage_count(data, stage) == 0
        )
        if missing:
            raise RuntimeError(
                "completed run is missing required stages: " + ", ".join(missing)
            )

    command_verify(argparse.Namespace(run_dir=run_dir))
    now_epoch = time.time()
    now_text = utc_now()
    data["status"] = args.status
    data["closed_at"] = now_text
    data["closed_epoch"] = now_epoch
    data["updated_at"] = now_text
    atomic_write(run_dir, data)
    return data


def command_summary(args: argparse.Namespace) -> dict[str, Any]:
    data = load_ledger(args.run_dir.resolve())
    stages = data.get("stages", [])

    def stage_seconds(*names: str, completed_only: bool = False) -> float:
        return round(
            sum(
                float(record.get("elapsed_seconds", 0))
                for record in stages
                if record.get("stage") in names
                and (not completed_only or record.get("status") == "completed")
            ),
            6,
        )

    now_epoch = time.time()
    end_epoch = float(data.get("closed_epoch") or now_epoch)
    interruption_seconds = sum(
        float(item.get("elapsed_seconds", 0))
        for item in data.get("interruptions", [])
    )
    open_interruption = data.get("open_interruption")
    if isinstance(open_interruption, dict):
        interruption_seconds += max(
            0.0, now_epoch - float(open_interruption["started_epoch"])
        )
    active_seconds = sum(float(item.get("elapsed_seconds", 0)) for item in stages)
    mode = data["execution_mode"]
    true_translation_seconds: float | None = None
    unavailable_reason: str | None = None
    if mode == "pi_agent_live":
        translated = stage_seconds("translate_once", completed_only=True)
        if translated > 0:
            true_translation_seconds = translated
        else:
            unavailable_reason = "completed_translate_once_stage_unavailable"
    else:
        unavailable_reason = "pretranslated_qa_has_no_translation_stage"

    page_count = int(data.get("page_count") or 0)
    remaining_target = max(
        0.0,
        float(data["target_deadline_epoch"]) - now_epoch,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": data["run_id"],
        "execution_mode": mode,
        "translation_provenance": data["translation_provenance"],
        "status": data["status"],
        "quality_profile": data["quality_profile"],
        "file_type": data["file_type"],
        "expected_output_type": data["expected_output_type"],
        "page_count": page_count,
        "run_wall_seconds": round(
            max(0.0, end_epoch - float(data["created_epoch"])), 6
        ),
        "active_processing_seconds": round(active_seconds, 6),
        "true_translation_seconds": true_translation_seconds,
        "true_translation_seconds_unavailable_reason": unavailable_reason,
        "average_translation_seconds_per_page": (
            round(true_translation_seconds / page_count, 6)
            if true_translation_seconds is not None and page_count > 0
            else None
        ),
        "conversion_seconds": stage_seconds("convert_legacy"),
        "validation_seconds": stage_seconds(
            "run_document_qa", "verify_candidate"
        ),
        "repair_seconds": stage_seconds("apply_repair"),
        "finalization_seconds": stage_seconds("finalize_result"),
        "interruption_seconds": round(interruption_seconds, 6),
        "resume_count": int(data.get("resume_count", 0)),
        "repair_iterations_completed": _completed_stage_count(
            data, "apply_repair"
        ),
        "repair_iterations_attempted": _stage_attempt_count(
            data, "apply_repair"
        ),
        "max_repair_rounds": int(data["max_repair_rounds"]),
        "target_budget_seconds": int(data["target_budget_seconds"]),
        "remaining_target_budget_seconds": round(remaining_target, 6),
        "deadline_state": _deadline_state(data, now_epoch),
        "service_concurrency_limit": int(data["service_concurrency_limit"]),
        "service_concurrency_semantics": data["service_concurrency_semantics"],
        "current_stage": data.get("current_stage"),
        "source_sha256": data["source_sha256"],
        "artifacts": data.get("artifacts", []),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomic Pi Office-translation run ledger"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--run-dir", type=Path, required=True)
    init.add_argument("--mode", required=True, choices=sorted(MODES))
    init.add_argument("--source", type=Path, required=True)
    init.add_argument("--target-language", required=True)
    init.add_argument("--file-type", required=True, choices=sorted(FILE_TYPES))
    init.add_argument("--page-count", type=int, default=0)
    init.add_argument(
        "--quality-profile",
        choices=sorted(QUALITY_PROFILES),
        default="standard",
    )
    init.add_argument(
        "--max-repair-rounds",
        type=int,
        default=DEFAULT_MAX_REPAIR_ROUNDS,
    )
    init.add_argument(
        "--service-concurrency-limit",
        type=int,
        default=DEFAULT_SERVICE_CONCURRENCY_LIMIT,
    )

    start = subparsers.add_parser("start")
    start.add_argument("--run-dir", type=Path, required=True)
    start.add_argument("--stage", required=True, choices=sorted(STAGES))

    finish = subparsers.add_parser("finish")
    finish.add_argument("--run-dir", type=Path, required=True)
    finish.add_argument("--stage", required=True, choices=sorted(STAGES))
    finish.add_argument(
        "--status",
        choices=("completed", "failed"),
        default="completed",
    )

    interrupt = subparsers.add_parser("interrupt")
    interrupt.add_argument("--run-dir", type=Path, required=True)
    interrupt.add_argument("--reason", required=True)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--run-dir", type=Path, required=True)

    artifact = subparsers.add_parser("artifact")
    artifact.add_argument("--run-dir", type=Path, required=True)
    artifact.add_argument("--kind", required=True, choices=sorted(ARTIFACT_KINDS))
    artifact.add_argument("--path", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--run-dir", type=Path, required=True)

    close = subparsers.add_parser("close")
    close.add_argument("--run-dir", type=Path, required=True)
    close.add_argument("--status", required=True, choices=sorted(TERMINAL_STATUSES))

    summary = subparsers.add_parser("summary")
    summary.add_argument("--run-dir", type=Path, required=True)
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        handler = globals()[f"command_{args.command}"]
        result = handler(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
