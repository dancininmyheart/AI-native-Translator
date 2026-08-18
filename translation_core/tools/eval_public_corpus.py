#!/usr/bin/env python3
"""Generate deterministic public Office fixtures for the policy-replay eval."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from p1a_demo_fixtures import _write_docx, _write_pptx


def _safe_destination(root: Path, relpath: object, label: str) -> tuple[Path, str]:
    if not isinstance(relpath, str) or "\\" in relpath:
        raise ValueError(f"{label} is invalid")
    normalized = PurePosixPath(relpath)
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        raise ValueError(f"{label} is invalid")
    return root.joinpath(*normalized.parts), normalized.as_posix()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_public_eval_corpus(manifest_path: str | Path, output_dir: str | Path) -> Path:
    manifest_file = Path(manifest_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("model_calls") != 0
        or manifest.get("pi_session") is not False
        or manifest.get("native_office_qa") is not False
        or manifest.get("qa_annotations") != "frozen_synthetic"
        or manifest.get("license") != "CC0-1.0"
        or not isinstance(manifest.get("scenarios"), list)
    ):
        raise ValueError("public eval manifest header is invalid")

    destination.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, str]] = []
    for scenario in manifest["scenarios"]:
        scenario_id = scenario.get("scenario_id")
        document_format = scenario.get("document_format")
        if not isinstance(scenario_id, str) or document_format not in {"docx", "pptx"}:
            raise ValueError("public eval scenario is invalid")
        source_path, source_relpath = _safe_destination(
            destination,
            scenario.get("source_relpath"),
            f"{scenario_id} source_relpath",
        )
        source_text = f"Synthetic public source for policy replay: {scenario_id}"
        if document_format == "docx":
            _write_docx(source_path, source_text)
        else:
            _write_pptx(source_path, source_text)
        files.append({"relpath": source_relpath, "sha256": _sha256(source_path)})

        rounds = scenario.get("rounds")
        if not isinstance(rounds, list):
            raise ValueError(f"{scenario_id} rounds are invalid")
        for round_index, round_data in enumerate(rounds):
            if not isinstance(round_data, dict):
                raise ValueError(f"{scenario_id} round {round_index} is invalid")
            candidate_path, candidate_relpath = _safe_destination(
                destination,
                round_data.get("candidate_relpath"),
                f"{scenario_id} candidate_relpath",
            )
            candidate_text = (
                f"Synthetic candidate r{round_index} for {scenario_id}; "
                f"frozen blockers={round_data.get('blocking_count')}; "
                f"frozen regressions={round_data.get('new_regressions')}"
            )
            if document_format == "docx":
                _write_docx(candidate_path, candidate_text)
            else:
                _write_pptx(candidate_path, candidate_text)
            files.append({"relpath": candidate_relpath, "sha256": _sha256(candidate_path)})

    index: dict[str, Any] = {
        "schema_version": 1,
        "corpus_id": manifest["corpus_id"],
        "corpus_version": manifest["corpus_version"],
        "license": "CC0-1.0",
        "pi_session": False,
        "model_calls": 0,
        "native_office_qa": False,
        "qa_annotations": "frozen_synthetic",
        "office_artifacts": "runtime_generated_carriers",
        "generated_by": "translation_core/tools/eval_public_corpus.py",
        "manifest_sha256": hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
        "files": sorted(files, key=lambda item: item["relpath"]),
    }
    index_path = destination / "fixture-index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return index_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    print(generate_public_eval_corpus(args.manifest, args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
