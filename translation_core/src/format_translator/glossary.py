from __future__ import annotations

import csv
import json
from pathlib import Path


def load_glossary(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items()}
        if isinstance(payload, list):
            return {
                str(row["source"]): str(row["target"])
                for row in payload
                if "source" in row and "target" in row
            }
        raise ValueError("Glossary JSON must be an object or a list")
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not {"source", "target"}.issubset(reader.fieldnames or []):
                raise ValueError("Glossary CSV requires source,target columns")
            return {
                row["source"].strip(): row["target"].strip()
                for row in reader
                if row["source"].strip()
            }
    raise ValueError("Glossary must be .json or .csv")


def glossary_issues(
    source_text: str,
    translated_text: str,
    glossary: dict[str, str],
) -> list[str]:
    issues = []
    source_folded = source_text.casefold()
    translated_folded = translated_text.casefold()
    for source, target in glossary.items():
        if (
            source.casefold() in source_folded
            and target.casefold() not in translated_folded
        ):
            issues.append(
                f"Missing required glossary translation: {source} -> {target}"
            )
    return issues
