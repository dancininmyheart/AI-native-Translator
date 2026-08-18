"""Native OOXML quality checks for translated DOCX files.

The checks intentionally cover only facts that can be established from the
Word package itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from lxml import etree

from .docx_engine import (
    NS,
    W,
    extract_translation_blocks,
    structure_snapshot,
    validate_output_package,
)
from .models import IssueSeverity, QaIssue

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
FORMAT_PART_PATTERNS = (
    re.compile(r"^word/styles(?:WithEffects)?\.xml$"),
    re.compile(r"^word/numbering\.xml$"),
    re.compile(r"^word/settings\.xml$"),
    re.compile(r"^word/fontTable\.xml$"),
    re.compile(r"^word/theme/.*\.xml$"),
)
TRANSLATABLE_PART_PATTERNS = (
    re.compile(r"^word/document\.xml$"),
    re.compile(r"^word/header\d+\.xml$"),
    re.compile(r"^word/footer\d+\.xml$"),
    re.compile(r"^word/footnotes\.xml$"),
    re.compile(r"^word/endnotes\.xml$"),
    re.compile(r"^word/comments\.xml$"),
)
MOJIBAKE_PATTERNS = (
    ("\ufffd", "Unicode replacement character"),
    ("锟斤拷", "common GBK replacement sequence"),
    ("ï¿½", "mis-decoded UTF-8 replacement sequence"),
    ("â€™", "mis-decoded apostrophe"),
    ("â€œ", "mis-decoded opening quote"),
    ("â€", "mis-decoded punctuation"),
    ("Ã©", "mis-decoded Latin text"),
)


class DocxQaResult(list[QaIssue]):
    """A list-compatible result carrying a deterministic quality score."""

    def __init__(
        self,
        issues: Iterable[QaIssue] = (),
        *,
        metrics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(issues)
        self.quality_score = calculate_quality_score(self)
        self.metrics = dict(metrics or {})

    @property
    def issues(self) -> list[QaIssue]:
        return list(self)

    @property
    def summary(self) -> dict[str, Any]:
        counts = Counter(issue.severity.value for issue in self)
        return {
            "quality_score": self.quality_score,
            "issue_count": len(self),
            "errors": counts[IssueSeverity.ERROR.value],
            "warnings": counts[IssueSeverity.WARNING.value],
            "info": counts[IssueSeverity.INFO.value],
            **self.metrics,
        }


def run_docx_qa(
    source_docx: str | os.PathLike[str],
    candidate_docx: str | os.PathLike[str],
    *,
    blocks: Iterable[Any] | None = None,
    translations: Iterable[Any] | Mapping[str, Any] | None = None,
) -> DocxQaResult:
    """Compare source and candidate DOCX packages directly."""

    source = Path(source_docx).expanduser().resolve()
    candidate = Path(candidate_docx).expanduser().resolve()
    issues: list[QaIssue] = []

    source_validation = validate_output_package(source)
    candidate_validation = validate_output_package(candidate)
    _validation_issues(issues, source_validation, side="source", path=source)
    _validation_issues(issues, candidate_validation, side="candidate", path=candidate)
    if not source_validation["valid"] or not candidate_validation["valid"]:
        return DocxQaResult(
            _deduplicate(issues),
            metrics={"deep_comparison_completed": False},
        )

    source_snapshot = structure_snapshot(source)
    candidate_snapshot = structure_snapshot(candidate)
    _compare_package_structure(
        issues,
        source_snapshot,
        candidate_snapshot,
    )

    supplied_blocks = (
        list(blocks) if blocks is not None else extract_translation_blocks(source)
    )
    translation_map = _translation_map(translations)

    with (
        zipfile.ZipFile(source) as source_package,
        zipfile.ZipFile(candidate) as candidate_package,
    ):
        common_parts = sorted(
            set(_translatable_parts(source_package.namelist()))
            & set(_translatable_parts(candidate_package.namelist()))
        )
        source_trees: dict[str, etree._ElementTree] = {}
        candidate_trees: dict[str, etree._ElementTree] = {}
        for part_name in common_parts:
            source_tree = _parse_xml(source_package.read(part_name), part_name)
            candidate_tree = _parse_xml(candidate_package.read(part_name), part_name)
            source_trees[part_name] = source_tree
            candidate_trees[part_name] = candidate_tree
            _compare_part_structure(issues, part_name, source_tree, candidate_tree)
            _compare_paragraph_formatting(
                issues, part_name, source_tree, candidate_tree
            )
            _compare_pagination_properties(
                issues, part_name, source_tree, candidate_tree
            )
            _compare_table_geometry(issues, part_name, source_tree, candidate_tree)

        _compare_fixed_format_parts(issues, source_package, candidate_package)
        _check_block_text(
            issues,
            supplied_blocks,
            translation_map,
            source_trees,
            candidate_trees,
        )

    deduplicated = _deduplicate(issues)
    return DocxQaResult(
        deduplicated,
        metrics={
            "deep_comparison_completed": True,
            "source_blocks": len(supplied_blocks),
            "translation_results": len(translation_map),
        },
    )


def calculate_quality_score(issues: Sequence[QaIssue]) -> float:
    """Score structural translation quality on a stable 0–100 scale."""

    counts = Counter(issue.severity for issue in issues)
    error_penalty = min(70.0, counts[IssueSeverity.ERROR] * 10.0)
    warning_penalty = min(25.0, counts[IssueSeverity.WARNING] * 2.0)
    info_penalty = min(5.0, counts[IssueSeverity.INFO] * 0.5)
    return round(max(0.0, 100.0 - error_penalty - warning_penalty - info_penalty), 1)


def _validation_issues(
    issues: list[QaIssue],
    validation: Mapping[str, Any],
    *,
    side: str,
    path: Path,
) -> None:
    for error in validation.get("errors", []):
        code = (
            "docx_relationship_invalid"
            if "relationship" in error
            else "docx_package_invalid"
        )
        _add_issue(
            issues,
            code=code,
            message=f"{side.capitalize()} DOCX failed package validation: {error}",
            severity=IssueSeverity.ERROR,
            location=f"{side}:{path.name}",
            details={
                "side": side,
                "path": str(path),
                "validation_error": error,
                "repair_hint": (
                    "Regenerate the DOCX from the last valid stage; do not "
                    "attempt text repair on a corrupt package."
                ),
            },
        )


def _compare_package_structure(
    issues: list[QaIssue],
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    source_entries = set(source["package_entries"])
    candidate_entries = set(candidate["package_entries"])
    for name in sorted(source_entries - candidate_entries):
        _add_issue(
            issues,
            code="docx_part_missing",
            message=f"Candidate is missing package part {name}",
            severity=IssueSeverity.ERROR,
            location=name,
            details={
                "part_name": name,
                "repair_hint": "Restore the missing part from the source package.",
            },
        )
    for name in sorted(candidate_entries - source_entries):
        _add_issue(
            issues,
            code="docx_part_added",
            message=f"Candidate contains an unexpected package part {name}",
            severity=IssueSeverity.WARNING,
            location=name,
            details={
                "part_name": name,
                "repair_hint": (
                    "Confirm the added part is intentional and has valid relationships."
                ),
            },
        )

    source_media = source["media_hashes"]
    candidate_media = candidate["media_hashes"]
    for name in sorted(set(source_media) | set(candidate_media)):
        if name not in candidate_media:
            code, message = (
                "docx_media_missing",
                f"Embedded media was removed: {name}",
            )
        elif name not in source_media:
            code, message = (
                "docx_media_added",
                f"Embedded media was added: {name}",
            )
        elif source_media[name] != candidate_media[name]:
            code, message = (
                "docx_media_changed",
                f"Embedded media bytes changed: {name}",
            )
        else:
            continue
        _add_issue(
            issues,
            code=code,
            message=message,
            severity=IssueSeverity.ERROR,
            location=name,
            details={
                "part_name": name,
                "source_sha256": source_media.get(name),
                "candidate_sha256": candidate_media.get(name),
                "repair_hint": (
                    "Restore the original media bytes; translation must not alter media."
                ),
            },
        )


def _compare_part_structure(
    issues: list[QaIssue],
    part_name: str,
    source: etree._ElementTree,
    candidate: etree._ElementTree,
) -> None:
    for label, xpath, code in (
        ("paragraph", "//w:p", "docx_paragraph_count_changed"),
        ("table", "//w:tbl", "docx_table_count_changed"),
        ("text node", "//w:t", "docx_text_node_count_changed"),
    ):
        source_count = len(source.xpath(xpath, namespaces=NS))
        candidate_count = len(candidate.xpath(xpath, namespaces=NS))
        if source_count == candidate_count:
            continue
        _add_issue(
            issues,
            code=code,
            message=(
                f"{label.capitalize()} count changed in {part_name}: "
                f"{source_count} → {candidate_count}"
            ),
            severity=IssueSeverity.ERROR,
            location=part_name,
            details={
                "part_name": part_name,
                "source_count": source_count,
                "candidate_count": candidate_count,
                "repair_hint": (
                    "Reapply translations to the original OOXML structure "
                    "without creating or deleting structural nodes."
                ),
            },
        )
    if part_name == "word/document.xml":
        _compare_section_geometry(issues, source, candidate)


def _compare_paragraph_formatting(
    issues: list[QaIssue],
    part_name: str,
    source: etree._ElementTree,
    candidate: etree._ElementTree,
) -> None:
    source_paragraphs = source.xpath("//w:p", namespaces=NS)
    candidate_paragraphs = candidate.xpath("//w:p", namespaces=NS)
    changed: list[dict[str, Any]] = []
    for index, (source_p, candidate_p) in enumerate(
        zip(source_paragraphs, candidate_paragraphs), start=1
    ):
        source_signature = _non_text_signature(source_p)
        candidate_signature = _non_text_signature(candidate_p)
        if source_signature != candidate_signature:
            changed.append(
                {
                    "paragraph_index": index,
                    "source_xpath": source.getpath(source_p),
                    "candidate_xpath": candidate.getpath(candidate_p),
                    "source_signature": source_signature,
                    "candidate_signature": candidate_signature,
                }
            )
    if changed:
        _add_issue(
            issues,
            code="docx_non_text_format_changed",
            message=(
                f"Non-text paragraph markup changed in {len(changed)} "
                f"paragraph(s) of {part_name}"
            ),
            severity=IssueSeverity.WARNING,
            location=part_name,
            details={
                "part_name": part_name,
                "changed_count": len(changed),
                "examples": changed[:20],
                "repair_hint": (
                    "Restore paragraph/run formatting and modify only existing w:t values."
                ),
            },
        )


def _compare_section_geometry(
    issues: list[QaIssue],
    source: etree._ElementTree,
    candidate: etree._ElementTree,
) -> None:
    before = [
        _section_geometry(section)
        for section in source.xpath("//w:sectPr", namespaces=NS)
    ]
    after = [
        _section_geometry(section)
        for section in candidate.xpath("//w:sectPr", namespaces=NS)
    ]
    if before == after:
        return
    _add_issue(
        issues,
        code="docx_section_geometry_changed",
        message="Section/page geometry differs from the source",
        severity=IssueSeverity.ERROR,
        location="word/document.xml:sectPr",
        details={
            "source": before,
            "candidate": after,
            "repair_hint": (
                "Restore source section size, margins, columns, grid, "
                "header/footer distance, and page-border geometry."
            ),
        },
    )


def _compare_pagination_properties(
    issues: list[QaIssue],
    part_name: str,
    source: etree._ElementTree,
    candidate: etree._ElementTree,
) -> None:
    source_paragraphs = source.xpath("//w:p", namespaces=NS)
    candidate_paragraphs = candidate.xpath("//w:p", namespaces=NS)
    changes: list[dict[str, Any]] = []
    for index, (source_p, candidate_p) in enumerate(
        zip(source_paragraphs, candidate_paragraphs), start=1
    ):
        before = _pagination_snapshot(source_p)
        after = _pagination_snapshot(candidate_p)
        if before != after:
            changes.append(
                {
                    "paragraph_index": index,
                    "xpath": source.getpath(source_p),
                    "before": before,
                    "after": after,
                }
            )
    if changes:
        _add_issue(
            issues,
            code="docx_pagination_property_changed",
            message=(
                f"Explicit pagination/keep properties changed in "
                f"{len(changes)} paragraph(s) of {part_name}"
            ),
            severity=IssueSeverity.WARNING,
            location=part_name,
            details={
                "part_name": part_name,
                "changed_count": len(changes),
                "examples": changes[:20],
                "repair_hint": (
                    "Review explicit page breaks, pageBreakBefore, keepNext, "
                    "and keepLines; restore source values unless the repair is audited."
                ),
            },
        )


def _compare_table_geometry(
    issues: list[QaIssue],
    part_name: str,
    source: etree._ElementTree,
    candidate: etree._ElementTree,
) -> None:
    source_tables = source.xpath("//w:tbl", namespaces=NS)
    candidate_tables = candidate.xpath("//w:tbl", namespaces=NS)
    changed: list[dict[str, Any]] = []
    for index, (source_table, candidate_table) in enumerate(
        zip(source_tables, candidate_tables), start=1
    ):
        before = _table_geometry(source_table)
        after = _table_geometry(candidate_table)
        if before != after:
            changed.append(
                {
                    "table_index": index,
                    "xpath": source.getpath(source_table),
                    "before": before,
                    "after": after,
                }
            )
    if changed:
        _add_issue(
            issues,
            code="docx_table_geometry_changed",
            message=(
                f"Table geometry changed in {len(changed)} table(s) of {part_name}"
            ),
            severity=IssueSeverity.ERROR,
            location=part_name,
            details={
                "part_name": part_name,
                "changed_count": len(changed),
                "examples": changed[:10],
                "repair_hint": (
                    "Restore tblGrid, row/cell counts, widths, gridSpan, "
                    "vertical merges, and row heights from the source."
                ),
            },
        )


def _compare_fixed_format_parts(
    issues: list[QaIssue],
    source: zipfile.ZipFile,
    candidate: zipfile.ZipFile,
) -> None:
    source_names = set(source.namelist())
    candidate_names = set(candidate.namelist())
    for part_name in sorted(source_names & candidate_names):
        if not any(pattern.match(part_name) for pattern in FORMAT_PART_PATTERNS):
            continue
        source_hash = _canonical_xml_hash(source.read(part_name), part_name)
        candidate_hash = _canonical_xml_hash(candidate.read(part_name), part_name)
        if source_hash == candidate_hash:
            continue
        _add_issue(
            issues,
            code="docx_global_format_part_changed",
            message=f"Global formatting part changed: {part_name}",
            severity=IssueSeverity.ERROR,
            location=part_name,
            details={
                "part_name": part_name,
                "source_sha256": source_hash,
                "candidate_sha256": candidate_hash,
                "repair_hint": (
                    "Restore the original styles/theme/numbering/settings part."
                ),
            },
        )


def _check_block_text(
    issues: list[QaIssue],
    blocks: Sequence[Any],
    translations: Mapping[str, Any],
    source_trees: Mapping[str, etree._ElementTree],
    candidate_trees: Mapping[str, etree._ElementTree],
) -> None:
    for block in blocks:
        block_id = _field(block, "block_id")
        part_name = _block_field(block, "part_name")
        xpath = _block_field(block, "xpath")
        source_text = str(_field(block, "source_text", ""))
        candidate_tree = candidate_trees.get(part_name)
        source_tree = source_trees.get(part_name)
        if candidate_tree is None or source_tree is None:
            continue
        source_matches = source_tree.xpath(xpath, namespaces=NS)
        matches = candidate_tree.xpath(xpath, namespaces=NS)
        if len(matches) != 1:
            _add_issue(
                issues,
                code="docx_block_location_missing",
                message=f"Candidate location is missing for block {block_id}",
                severity=IssueSeverity.ERROR,
                location=f"{part_name}:{xpath}",
                block_id=block_id,
                details={
                    "part_name": part_name,
                    "xpath": xpath,
                    "repair_hint": (
                        "Restore the source paragraph structure and reapply this block."
                    ),
                },
            )
            continue
        candidate_text = _owned_paragraph_text(matches[0])
        if source_text.strip() and not candidate_text.strip():
            _add_issue(
                issues,
                code="docx_translated_text_empty",
                message=f"Translated text is empty for block {block_id}",
                severity=IssueSeverity.ERROR,
                location=f"{part_name}:{xpath}",
                block_id=block_id,
                details={
                    "part_name": part_name,
                    "xpath": xpath,
                    "source_length": len(source_text),
                    "repair_hint": "Restore or retranslate the missing block text.",
                },
            )
            continue

        _check_mojibake(issues, candidate_text, block_id, part_name, xpath)
        _check_expansion_risk(
            issues,
            block,
            source_text,
            candidate_text,
            part_name,
            xpath,
            source_paragraph=(source_matches[0] if len(source_matches) == 1 else None),
        )

        expected = translations.get(block_id)
        if expected is not None:
            expected_text = _translation_text(expected)
            if expected_text is not None and candidate_text != expected_text:
                _add_issue(
                    issues,
                    code="docx_translation_writeback_mismatch",
                    message=(
                        f"Written text does not match TranslationResult for {block_id}"
                    ),
                    severity=IssueSeverity.ERROR,
                    location=f"{part_name}:{xpath}",
                    block_id=block_id,
                    details={
                        "part_name": part_name,
                        "xpath": xpath,
                        "expected_length": len(expected_text),
                        "actual_length": len(candidate_text),
                        "expected_sha256": _text_hash(expected_text),
                        "actual_sha256": _text_hash(candidate_text),
                        "repair_hint": (
                            "Reapply the audited TranslationResult to its exact block ID."
                        ),
                    },
                )


def _check_mojibake(
    issues: list[QaIssue],
    text: str,
    block_id: str,
    part_name: str,
    xpath: str,
) -> None:
    detected = [
        {"sequence": sequence, "description": description}
        for sequence, description in MOJIBAKE_PATTERNS
        if sequence in text
    ]
    tofu_count = text.count("□")
    if tofu_count >= 2:
        detected.append(
            {"sequence": "□", "description": f"{tofu_count} missing-glyph boxes"}
        )
    if not detected:
        return
    _add_issue(
        issues,
        code="docx_text_encoding_suspect",
        message=f"Possible mojibake or missing glyphs in block {block_id}",
        severity=IssueSeverity.ERROR,
        location=f"{part_name}:{xpath}",
        block_id=block_id,
        details={
            "part_name": part_name,
            "xpath": xpath,
            "detected": detected,
            "repair_hint": (
                "Retranslate from Unicode source text and use a target-language font."
            ),
        },
    )


def _check_expansion_risk(
    issues: list[QaIssue],
    block: Any,
    source_text: str,
    candidate_text: str,
    part_name: str,
    xpath: str,
    *,
    source_paragraph: etree._Element | None,
) -> None:
    source_length = len(source_text.strip())
    candidate_length = len(candidate_text.strip())
    if source_length == 0:
        return
    length_ratio = candidate_length / source_length
    source_capacity = _estimated_glyph_advance(source_text.strip())
    candidate_capacity = _estimated_glyph_advance(candidate_text.strip())
    capacity_ratio = candidate_capacity / max(source_capacity, 0.01)
    max_length_hint = _positive_length_hint(_block_field(block, "max_length_hint"))
    exceeds_hint = max_length_hint is not None and candidate_length > max_length_hint
    layout_context = _layout_capacity_context(source_paragraph, part_name)
    if exceeds_hint:
        layout_context = {
            **layout_context,
            "kind": "explicit_length_budget",
            "constrained": True,
            "evidence": [
                *layout_context["evidence"],
                {
                    "max_length_hint": max_length_hint,
                    "candidate_length": candidate_length,
                },
            ],
        }
    inflated = (
        bool(layout_context["constrained"])
        and source_capacity >= 4.0
        and capacity_ratio > 1.8
    )
    if not exceeds_hint and not inflated:
        return
    severity = (
        IssueSeverity.ERROR
        if exceeds_hint or capacity_ratio > 2.5
        else IssueSeverity.WARNING
    )
    block_id = block_id_or_unknown(block)
    candidate_average_advance = candidate_capacity / max(candidate_length, 1)
    fitted_max_length = max(
        1,
        int((source_capacity * 1.25) / max(candidate_average_advance, 0.01)),
    )
    if max_length_hint is not None:
        fitted_max_length = min(fitted_max_length, max_length_hint)
    _add_issue(
        issues,
        code="docx_translation_expansion_risk",
        message=(f"Translation may exceed a constrained layout at block {block_id}"),
        severity=severity,
        location=f"{part_name}:{xpath}",
        block_id=_field(block, "block_id"),
        details={
            "part_name": part_name,
            "xpath": xpath,
            "source_length": source_length,
            "candidate_length": candidate_length,
            "length_ratio": round(length_ratio, 3),
            "capacity_metric": "estimated_glyph_advance_em",
            "source_capacity_units": round(source_capacity, 3),
            "candidate_capacity_units": round(candidate_capacity, 3),
            "capacity_ratio": round(capacity_ratio, 3),
            "source_script": _dominant_script(source_text),
            "candidate_script": _dominant_script(candidate_text),
            "layout_context": layout_context,
            "max_length_hint": max_length_hint,
            "repair_hint": {
                "action": "fit_text",
                "action_type": "fit_text",
                "automatic": True,
                "block_id": block_id,
                "max_length": fitted_max_length,
                "scope": {
                    "part_name": part_name,
                    "xpath": xpath,
                    "block_id": block_id,
                },
                "constraints": {
                    "preserve_meaning": True,
                    "preserve_formatting": True,
                    "forbid_text_deletion": True,
                    "forbid_page_geometry_change": True,
                    "forbid_global_font_change": True,
                },
                "verification": "repeat_docx_native_and_rendered_layout_qa",
            },
        },
    )


def _estimated_glyph_advance(text: str) -> float:
    """Estimate horizontal text demand in font-relative units.

    Raw character counts systematically overstate CJK-to-Latin expansion:
    ideographs are normally full-em glyphs while most Latin glyphs occupy
    roughly half an em.  This deterministic estimate is deliberately
    conservative and is only used when OOXML proves that the text container
    cannot freely grow.
    """

    units = 0.0
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cf", "Mn", "Me"}:
            continue
        if character.isspace():
            units += 0.28
        elif unicodedata.east_asian_width(character) in {"F", "W"}:
            units += 1.0
        elif category.startswith("P"):
            units += 0.35
        elif character.isupper():
            units += 0.62
        elif character.islower() or character.isdigit():
            units += 0.52
        else:
            units += 0.68
    return units


def _dominant_script(text: str) -> str:
    counts: Counter[str] = Counter()
    for character in text:
        if character.isspace() or unicodedata.category(character).startswith(
            ("P", "S")
        ):
            continue
        codepoint = ord(character)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            counts["han"] += 1
        elif 0x3040 <= codepoint <= 0x30FF:
            counts["japanese_kana"] += 1
        elif 0xAC00 <= codepoint <= 0xD7AF:
            counts["hangul"] += 1
        elif 0x0400 <= codepoint <= 0x052F:
            counts["cyrillic"] += 1
        elif 0x0600 <= codepoint <= 0x06FF:
            counts["arabic"] += 1
        elif "LATIN" in unicodedata.name(character, ""):
            counts["latin"] += 1
        elif character.isdigit():
            counts["numeric"] += 1
        else:
            counts["other"] += 1
    return counts.most_common(1)[0][0] if counts else "unknown"


def _layout_capacity_context(
    paragraph: etree._Element | None,
    part_name: str,
) -> dict[str, Any]:
    if paragraph is None:
        return {
            "kind": "unknown",
            "constrained": False,
            "evidence": [],
        }

    table_row = next(
        (
            ancestor
            for ancestor in paragraph.iterancestors()
            if ancestor.tag == f"{W}tr"
        ),
        None,
    )
    if table_row is not None:
        exact_heights = [
            element
            for element in table_row.xpath("./w:trPr/w:trHeight", namespaces=NS)
            if element.get(f"{W}hRule") == "exact"
        ]
        if exact_heights:
            return {
                "kind": "fixed_table_row",
                "constrained": True,
                "evidence": [
                    {
                        "row_height_twips": exact_heights[-1].get(f"{W}val"),
                        "height_rule": "exact",
                    }
                ],
            }

    table_cell = next(
        (
            ancestor
            for ancestor in paragraph.iterancestors()
            if ancestor.tag == f"{W}tc"
        ),
        None,
    )
    if table_cell is not None:
        cell_properties = table_cell.find(f"{W}tcPr")
        no_wrap = (
            cell_properties.find(f"{W}noWrap") if cell_properties is not None else None
        )
        cell_width = (
            cell_properties.find(f"{W}tcW") if cell_properties is not None else None
        )
        table = next(
            (
                ancestor
                for ancestor in table_cell.iterancestors()
                if ancestor.tag == f"{W}tbl"
            ),
            None,
        )
        table_layout = (
            table.find(f"{W}tblPr/{W}tblLayout") if table is not None else None
        )
        grid_columns = (
            table.findall(f"{W}tblGrid/{W}gridCol") if table is not None else []
        )
        fixed_cell_width = (
            cell_width is not None
            and cell_width.get(f"{W}w") not in {None, "0"}
            and cell_width.get(f"{W}type") not in {"auto", "nil"}
        )
        fixed_grid_widths = [
            column.get(f"{W}w")
            for column in grid_columns
            if column.get(f"{W}w") not in {None, "0"}
        ]
        if (
            _on_off(no_wrap) is True
            and table_layout is not None
            and table_layout.get(f"{W}type") == "fixed"
            and (fixed_cell_width or fixed_grid_widths)
        ):
            return {
                "kind": "fixed_width_no_wrap_table_cell",
                "constrained": True,
                "evidence": [
                    {
                        "cell_no_wrap": True,
                        "cell_width": (
                            {
                                "value": cell_width.get(f"{W}w"),
                                "type": cell_width.get(f"{W}type") or "dxa",
                            }
                            if fixed_cell_width
                            else None
                        ),
                        "table_layout": "fixed",
                        "grid_column_widths": fixed_grid_widths,
                    }
                ],
            }

    textbox_content = next(
        (
            ancestor
            for ancestor in paragraph.iterancestors()
            if ancestor.tag == f"{W}txbxContent"
        ),
        None,
    )
    if textbox_content is not None:
        shape = next(
            (
                ancestor
                for ancestor in textbox_content.iterancestors()
                if etree.QName(ancestor).localname in {"wsp", "shape"}
            ),
            textbox_content,
        )
        grows_to_fit = bool(
            shape.xpath(".//a:spAutoFit", namespaces=NS)
            or shape.xpath(
                ".//v:textbox[contains(translate(@style, "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'mso-fit-shape-to-text:t')]",
                namespaces=NS,
            )
        )
        return {
            "kind": ("autogrowing_textbox" if grows_to_fit else "fixed_textbox"),
            "constrained": not grows_to_fit,
            "evidence": [{"shape_grows_to_fit_text": grows_to_fit}],
        }

    frame = paragraph.find(f"{W}pPr/{W}framePr")
    if (
        frame is not None
        and frame.get(f"{W}hRule") == "exact"
        and frame.get(f"{W}h") is not None
    ):
        return {
            "kind": "fixed_paragraph_frame",
            "constrained": True,
            "evidence": [
                {
                    "frame_height_twips": frame.get(f"{W}h"),
                    "height_rule": "exact",
                }
            ],
        }

    return {
        "kind": (
            "reflowable_body"
            if part_name == "word/document.xml"
            else "reflowable_auxiliary_part"
        ),
        "constrained": False,
        "evidence": [],
    }


def _non_text_signature(paragraph: etree._Element) -> str:
    clone = etree.fromstring(etree.tostring(paragraph))
    for text_node in clone.xpath(".//w:t | .//w:delText", namespaces=NS):
        text_node.text = ""
        text_node.attrib.pop(XML_SPACE, None)
    canonical = etree.tostring(clone, method="c14n", exclusive=True)
    return _sha256(canonical)


def _canonical_xml_hash(data: bytes, part_name: str) -> str:
    tree = _parse_xml(data, part_name)
    canonical = etree.tostring(tree, method="c14n", exclusive=True)
    return _sha256(canonical)


def _pagination_snapshot(paragraph: etree._Element) -> dict[str, Any]:
    ppr = paragraph.find(f"{W}pPr")
    return {
        "explicit_page_breaks": len(
            paragraph.xpath(".//w:br[@w:type='page']", namespaces=NS)
        ),
        "page_break_before": _on_off(
            ppr.find(f"{W}pageBreakBefore") if ppr is not None else None
        ),
        "keep_next": _on_off(ppr.find(f"{W}keepNext") if ppr is not None else None),
        "keep_lines": _on_off(ppr.find(f"{W}keepLines") if ppr is not None else None),
    }


def _on_off(element: etree._Element | None) -> bool | None:
    if element is None:
        return None
    return element.get(f"{W}val") not in {"0", "false", "off"}


def _table_geometry(table: etree._Element) -> dict[str, Any]:
    grid = table.find(f"{W}tblGrid")
    rows = table.findall(f"{W}tr")
    return {
        "grid_widths": (
            [column.get(f"{W}w") for column in grid.findall(f"{W}gridCol")]
            if grid is not None
            else []
        ),
        "table_properties": _selected_attributes(
            table.find(f"{W}tblPr"),
            {
                "tblW",
                "tblLayout",
                "tblInd",
                "tblCellSpacing",
                "jc",
            },
        ),
        "rows": [
            {
                "height": _selected_attributes(
                    row.find(f"{W}trPr"), {"trHeight", "cantSplit"}
                ),
                "cells": [
                    _selected_attributes(
                        cell.find(f"{W}tcPr"),
                        {"tcW", "gridSpan", "vMerge", "textDirection"},
                    )
                    for cell in row.findall(f"{W}tc")
                ],
            }
            for row in rows
        ],
    }


def _section_geometry(section: etree._Element) -> dict[str, Any]:
    selected_names = {
        "pgSz",
        "pgMar",
        "paperSrc",
        "pgBorders",
        "lnNumType",
        "pgNumType",
        "cols",
        "formProt",
        "textDirection",
        "docGrid",
    }
    geometry: list[dict[str, Any]] = []
    for child in section:
        local_name = etree.QName(child).localname
        if local_name not in selected_names:
            continue
        record: dict[str, Any] = {
            "name": local_name,
            "attributes": {
                etree.QName(key).localname: value
                for key, value in sorted(child.attrib.items())
            },
        }
        if local_name in {"cols", "pgBorders"}:
            record["children"] = [
                {
                    "name": etree.QName(grandchild).localname,
                    "attributes": {
                        etree.QName(key).localname: value
                        for key, value in sorted(grandchild.attrib.items())
                    },
                }
                for grandchild in child
            ]
        geometry.append(record)
    return {"geometry": geometry}


def _selected_attributes(
    parent: etree._Element | None, child_names: set[str]
) -> list[dict[str, Any]]:
    if parent is None:
        return []
    selected: list[dict[str, Any]] = []
    for child in parent:
        local_name = etree.QName(child).localname
        if local_name not in child_names:
            continue
        selected.append(
            {
                "name": local_name,
                "attributes": {
                    etree.QName(key).localname: value
                    for key, value in sorted(child.attrib.items())
                },
            }
        )
    return selected


def _translation_map(
    translations: Iterable[Any] | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if translations is None:
        return {}
    if isinstance(translations, Mapping):
        return dict(translations)
    result: dict[str, Any] = {}
    for translation in translations:
        block_id = _field(translation, "block_id")
        if block_id:
            result[str(block_id)] = translation
    return result


def _translation_text(translation: Any) -> str | None:
    if isinstance(translation, str):
        return translation
    value = _field(translation, "translated_text", None)
    return str(value) if value is not None else None


def _parse_xml(data: bytes, part_name: str) -> etree._ElementTree:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        remove_blank_text=False,
        huge_tree=False,
    )
    try:
        return etree.ElementTree(etree.fromstring(data, parser=parser))
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ValueError(f"Invalid XML in {part_name}: {exc}") from exc


def _translatable_parts(names: Iterable[str]) -> list[str]:
    return sorted(
        name
        for name in names
        if any(pattern.match(name) for pattern in TRANSLATABLE_PART_PATTERNS)
    )


def _owned_paragraph_text(paragraph: etree._Element) -> str:
    nodes = [
        node
        for node in paragraph.xpath(".//w:t", namespaces=NS)
        if _nearest_paragraph(node) == paragraph
    ]
    return "".join(node.text or "" for node in nodes)


def _nearest_paragraph(node: etree._Element) -> etree._Element | None:
    return next(
        (ancestor for ancestor in node.iterancestors() if ancestor.tag == f"{W}p"),
        None,
    )


def _block_field(block: Any, name: str) -> Any:
    direct = _field(block, name, None)
    if direct is not None:
        return direct
    metadata = _field(block, "metadata", {}) or {}
    return _field(metadata, name)


def _positive_length_hint(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized.isascii() or not normalized.isdecimal():
        return None
    try:
        parsed = int(normalized)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def block_id_or_unknown(block: Any) -> str:
    return str(_field(block, "block_id", "<unknown>"))


def _add_issue(
    issues: list[QaIssue],
    *,
    code: str,
    message: str,
    severity: IssueSeverity,
    location: str,
    details: dict[str, Any],
    block_id: str | None = None,
) -> None:
    stable_payload = {
        "code": code,
        "block_id": block_id,
        "location": location,
        "details": details,
    }
    issue_id = (
        "wqa_"
        + hashlib.sha256(
            json.dumps(
                stable_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:20]
    )
    details = {"location": location, **details}
    issues.append(
        QaIssue(
            issue_id=issue_id,
            code=code,
            message=message,
            severity=severity,
            stage="DOCX_QA",
            block_id=block_id,
            recoverable=True,
            details=details,
        )
    )


def _deduplicate(issues: Iterable[QaIssue]) -> list[QaIssue]:
    unique: dict[str, QaIssue] = {}
    for issue in issues:
        unique[issue.issue_id or ""] = issue
    return sorted(
        unique.values(),
        key=lambda issue: (
            {
                IssueSeverity.ERROR: 0,
                IssueSeverity.WARNING: 1,
                IssueSeverity.INFO: 2,
            }[issue.severity],
            issue.code,
            issue.issue_id or "",
        ),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_hash(text: str) -> str:
    return _sha256(text.encode("utf-8"))
