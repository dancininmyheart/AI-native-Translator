"""Native PPTX extraction, patching, repair, and OOXML quality checks.

Translation writes are limited to existing slide, chart-cache, and linked
workbook text nodes, while repair writes are limited to the explicit whitelist
in :data:`REPAIR_ACTIONS`.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import posixpath
import re
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from lxml import etree


P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
X_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
A14_NS = "http://schemas.microsoft.com/office/drawing/2010/main"
NS = {
    "p": P_NS,
    "a": A_NS,
    "c": C_NS,
    "x": X_NS,
    "r": R_NS,
    "mc": MC_NS,
    "a14": A14_NS,
}
P = f"{{{P_NS}}}"
A = f"{{{A_NS}}}"
C = f"{{{C_NS}}}"
X = f"{{{X_NS}}}"
R = f"{{{R_NS}}}"
MC = f"{{{MC_NS}}}"
XML_SPACE = f"{{{XML_NS}}}space"
_XPATH_QNAME_PREFIX = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z_][A-Za-z0-9_.-]*):(?=[A-Za-z_*])"
)

_SHAPE_TAGS = frozenset(
    {
        f"{P}sp",
        f"{P}pic",
        f"{P}graphicFrame",
        f"{P}cxnSp",
        f"{P}grpSp",
    }
)
_SUPPORTED_MC_CHOICE_NAMESPACES = frozenset(
    {
        P_NS,
        A_NS,
        C_NS,
        R_NS,
        A14_NS,
    }
)

REQUIRED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
    "ppt/_rels/presentation.xml.rels",
}
SLIDE_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
LAYOUT_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "slideLayout"
)
MASTER_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "slideMaster"
)
CHART_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
)
PACKAGE_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"
)
EMUS_PER_POINT = 12_700
DEFAULT_MARGIN_EMU = {
    "lIns": 91_440,
    "rIns": 91_440,
    "tIns": 45_720,
    "bIns": 45_720,
}
DEFAULT_MIN_SHAPE_EMU = 91_440
HARD_MIN_FONT_SIZE_PT = 8.0
TARGET_LANGUAGE_TAGS = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "ja": "ja-JP",
    "ja-jp": "ja-JP",
    "jp": "ja-JP",
    "ko": "ko-KR",
    "ko-kr": "ko-KR",
    "kr": "ko-KR",
    "fr": "fr-FR",
    "fr-fr": "fr-FR",
    "de": "de-DE",
    "de-de": "de-DE",
    "ru": "ru-RU",
    "ru-ru": "ru-RU",
    "en": "en-US",
    "en-us": "en-US",
}
PROMPT_LEAKAGE_RULES = (
    (
        "preserve_named_tokens_instruction",
        ("人名", "url", "邮箱", "保持原样"),
    ),
    (
        "translation_fidelity_instruction",
        ("精准传达原文含义", "避免意译", "省略"),
    ),
    (
        "special_character_passthrough_instruction",
        ("特殊字符", "无意义字符", "返回原文"),
    ),
    (
        "hyperlink_placeholder_instruction",
        ("hyperlink_", "超链接占位符", "不要翻译"),
    ),
    (
        "translation_output_only_instruction",
        ("只输出译文", "不要解释"),
    ),
    (
        "english_translation_output_only_instruction",
        ("translate the following", "return only the translation"),
    ),
)
REPAIR_ACTIONS = frozenset(
    {
        "set_autofit",
        "adjust_margins",
        "adjust_font_size",
        "set_font_size",
        "set_shape_bounds",
        "adjust_shape_geometry",
        "restore_shape_coordinates",
        "restore_table_row_heights",
        "resolve_text_overflow",
        "replace_translation",
    }
)


class PptxEngineError(RuntimeError):
    """Base error for PPTX parsing, patching, validation, and repair."""


class InvalidPptxError(PptxEngineError):
    """The input is not a safe, structurally valid PPTX package."""


class UnsafePptxPatchError(PptxEngineError):
    """A requested write exceeds the safe PPTX mutation contract."""


class UnsupportedPptxRepairError(PptxEngineError):
    """A repair action is outside the explicit whitelist or safety bounds."""


def validate_pptx(
    path: str | os.PathLike[str],
    baseline_snapshot: Mapping[str, Any] | None = None,
    *,
    authorized_coordinate_changes: Mapping[
        str, Sequence[int]
    ] | None = None,
) -> dict[str, Any]:
    """Validate PPTX ZIP, XML, relationships, and optional invariants."""

    candidate = Path(path).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    if not candidate.is_file():
        return {
            "valid": False,
            "errors": [f"PPTX does not exist: {candidate}"],
            "warnings": [],
            "snapshot": None,
        }
    if candidate.suffix.lower() != ".pptx":
        errors.append(f"expected a .pptx package, got: {candidate.name}")

    try:
        with zipfile.ZipFile(candidate) as package:
            infos = package.infolist()
            names = [info.filename for info in infos]
            name_set = set(names)
            if len(names) != len(name_set):
                errors.append("ZIP package contains duplicate member names")
            missing = sorted(REQUIRED_PARTS - name_set)
            if missing:
                errors.append("missing package entries: " + ", ".join(missing))
            unsafe_names = sorted(name for name in names if not _safe_part_name(name))
            if unsafe_names:
                errors.append(
                    "unsafe ZIP member paths: " + ", ".join(unsafe_names[:10])
                )
            encrypted = sorted(
                info.filename for info in infos if info.flag_bits & 0x1
            )
            if encrypted:
                errors.append(
                    "encrypted ZIP members are unsupported: "
                    + ", ".join(encrypted[:10])
                )
            total_uncompressed = sum(info.file_size for info in infos)
            if total_uncompressed > 2 * 1024 * 1024 * 1024:
                errors.append("uncompressed PPTX package exceeds 2 GiB safety limit")
            extreme_ratio = [
                info.filename
                for info in infos
                if info.compress_size > 0
                and info.file_size > 10 * 1024 * 1024
                and info.file_size / info.compress_size > 1_000
            ]
            if extreme_ratio:
                errors.append(
                    "suspicious ZIP compression ratio: "
                    + ", ".join(extreme_ratio[:10])
                )

            corrupt = package.testzip()
            if corrupt:
                errors.append(f"corrupt ZIP member: {corrupt}")
            for name in sorted(name_set):
                if name.endswith(".xml") or name.endswith(".rels"):
                    try:
                        _parse_xml(package.read(name), name)
                    except InvalidPptxError as exc:
                        errors.append(str(exc))
            if REQUIRED_PARTS <= name_set:
                errors.extend(_relationship_errors(package))
                errors.extend(_slide_order_errors(package))
            if "ppt/vbaProject.bin" in name_set:
                errors.append(
                    "macro project found in .pptx package; use a macro-free source"
                )
            if any(name.startswith("ppt/embeddings/") for name in name_set):
                warnings.append(
                    "embedded package objects are preserved but never opened"
                )
            external_count = _external_relationship_count(package)
            if external_count:
                warnings.append(
                    f"{external_count} external relationships are preserved "
                    "but never fetched"
                )
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"not a readable PPTX ZIP package: {exc}")

    snapshot: dict[str, Any] | None = None
    if not errors:
        try:
            snapshot = structure_snapshot(candidate, _skip_validation=True)
        except PptxEngineError as exc:
            errors.append(str(exc))

    if baseline_snapshot is not None and snapshot is not None:
        errors.extend(
            _baseline_errors(
                baseline_snapshot,
                snapshot,
                authorized_coordinate_changes=authorized_coordinate_changes,
            )
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "snapshot": snapshot,
    }


def extract_translation_blocks(
    path: str | os.PathLike[str],
) -> list[Any]:
    """Extract slide text paragraphs as stable ``TranslationBlock`` objects."""

    source = _validated_path(path)
    block_type, segment_type = _translation_types()
    blocks: list[Any] = []
    with zipfile.ZipFile(source) as package:
        slide_parts = _ordered_slide_parts(package)
        tree_cache: dict[str, etree._ElementTree] = {}
        seen_chart_parts: set[str] = set()
        chart_blocks_by_source: dict[str, Any] = {}
        for slide_number, part_name in enumerate(slide_parts, start=1):
            tree = _part_tree(package, part_name, tree_cache)
            for shape in _shape_elements(tree):
                shape_id, shape_name = _shape_identity(shape, tree)
                shape_type = _shape_type(shape)
                bbox = _shape_bbox(
                    package,
                    part_name,
                    shape,
                    tree_cache,
                )
                paragraphs = shape.xpath(".//a:p", namespaces=NS)
                for paragraph_index, paragraph in enumerate(paragraphs, start=1):
                    if _nearest_shape(paragraph) is not shape:
                        continue
                    text_nodes = _translatable_text_nodes(paragraph)
                    source_text = "".join(node.text or "" for node in text_nodes)
                    if not source_text.strip():
                        continue
                    xpath = tree.getpath(paragraph)
                    segments = []
                    style_ids: list[str] = []
                    for node_index, node in enumerate(text_nodes):
                        style_id = _run_style_id(node)
                        style_ids.append(style_id)
                        segments.append(
                            segment_type(
                                segment_id=f"t{node_index}",
                                text=node.text or "",
                                style_id=style_id,
                                metadata={
                                    "node_xpath": tree.getpath(node),
                                    "run_index": node_index,
                                },
                            )
                        )
                    protocol = "tagged" if len(text_nodes) > 1 else "plain"
                    capacity = _shape_capacity(shape, bbox)
                    block_id = _stable_block_id(part_name, shape_id, xpath)
                    blocks.append(
                        block_type(
                            block_id=block_id,
                            source_text=source_text,
                            protocol=protocol,
                            segments=segments,
                            max_length_hint=(
                                capacity["estimated_capacity_chars"]
                                if capacity is not None
                                and capacity["estimated_capacity_chars"] > 0
                                else None
                            ),
                            context={
                                "document_type": "pptx",
                                "slide_number": slide_number,
                                "shape_id": shape_id,
                                "shape_name": shape_name,
                                "shape_type": shape_type,
                                "paragraph_index": paragraph_index,
                                "bbox_emu": list(bbox) if bbox else None,
                            },
                            metadata={
                                "part_name": part_name,
                                "paragraph_xpath": xpath,
                                "shape_id": shape_id,
                                "text_node_xpaths": [
                                    tree.getpath(node) for node in text_nodes
                                ],
                                "style_ids": style_ids,
                            },
                        )
                    )
            blocks.extend(
                _extract_slide_chart_blocks(
                    package=package,
                    slide_tree=tree,
                    slide_part=part_name,
                    slide_number=slide_number,
                    tree_cache=tree_cache,
                    seen_chart_parts=seen_chart_parts,
                    chart_blocks_by_source=chart_blocks_by_source,
                    block_type=block_type,
                    segment_type=segment_type,
                )
            )
    return blocks


def _extract_slide_chart_blocks(
    *,
    package: zipfile.ZipFile,
    slide_tree: etree._ElementTree,
    slide_part: str,
    slide_number: int,
    tree_cache: dict[str, etree._ElementTree],
    seen_chart_parts: set[str],
    chart_blocks_by_source: dict[str, Any],
    block_type: type[Any],
    segment_type: type[Any],
) -> list[Any]:
    """Extract visible native-chart strings and link their workbook caches."""

    new_blocks: list[Any] = []
    relationships = {
        relationship["id"]: relationship
        for relationship in _relationships(package, slide_part)
    }
    for shape, chart_reference in _logical_chart_references(slide_tree):
        relationship_id = str(chart_reference.get(f"{R}id", "") or "")
        relationship = relationships.get(relationship_id)
        if (
            relationship is None
            or relationship["type"] != CHART_RELATIONSHIP_TYPE
            or relationship["target_mode"] == "External"
        ):
            continue
        chart_part = _resolve_target(slide_part, relationship["target"])
        if chart_part not in package.namelist() or chart_part in seen_chart_parts:
            continue
        seen_chart_parts.add(chart_part)
        shape_id, shape_name = _shape_identity(shape, slide_tree)
        bbox = _shape_bbox(
            package,
            slide_part,
            shape,
            tree_cache,
        )
        chart_tree = _part_tree(package, chart_part, tree_cache)

        for paragraph_index, paragraph in enumerate(
            chart_tree.xpath("//a:p", namespaces=NS),
            start=1,
        ):
            text_nodes = _translatable_text_nodes(paragraph)
            source_text = "".join(node.text or "" for node in text_nodes)
            if not _contains_translatable_text(source_text):
                continue
            block = _register_chart_text_location(
                chart_blocks_by_source=chart_blocks_by_source,
                block_type=block_type,
                segment_type=segment_type,
                source_text=source_text,
                chart_part=chart_part,
                chart_tree=chart_tree,
                container=paragraph,
                text_nodes=text_nodes,
                slide_number=slide_number,
                shape_id=shape_id,
                shape_name=shape_name,
                bbox=bbox,
                chart_role=_chart_text_role(paragraph),
                paragraph_index=paragraph_index,
            )
            if block is not None:
                new_blocks.append(block)

        cache_nodes = chart_tree.xpath(
            "//c:strCache/c:pt/c:v | //c:strLit/c:pt/c:v",
            namespaces=NS,
        )
        for node in cache_nodes:
            source_text = str(node.text or "")
            if not _contains_translatable_text(source_text):
                continue
            container = node.getparent()
            if container is None:
                continue
            block = _register_chart_text_location(
                chart_blocks_by_source=chart_blocks_by_source,
                block_type=block_type,
                segment_type=segment_type,
                source_text=source_text,
                chart_part=chart_part,
                chart_tree=chart_tree,
                container=container,
                text_nodes=[node],
                slide_number=slide_number,
                shape_id=shape_id,
                shape_name=shape_name,
                bbox=bbox,
                chart_role=_chart_text_role(node),
                paragraph_index=None,
            )
            if block is not None:
                new_blocks.append(block)

        for workbook_part in _chart_workbook_parts(package, chart_part):
            for location in _embedded_workbook_text_locations(
                package,
                workbook_part,
            ):
                block = chart_blocks_by_source.get(location["source_text"])
                if block is not None:
                    _append_linked_location(block, location)
    return new_blocks


def _register_chart_text_location(
    *,
    chart_blocks_by_source: dict[str, Any],
    block_type: type[Any],
    segment_type: type[Any],
    source_text: str,
    chart_part: str,
    chart_tree: etree._ElementTree,
    container: etree._Element,
    text_nodes: Sequence[etree._Element],
    slide_number: int,
    shape_id: str,
    shape_name: str,
    bbox: tuple[int, int, int, int] | None,
    chart_role: str,
    paragraph_index: int | None,
) -> Any | None:
    location = {
        "storage_kind": "xml",
        "part_name": chart_part,
        "container_xpath": chart_tree.getpath(container),
        "text_node_xpaths": [
            chart_tree.getpath(node) for node in text_nodes
        ],
        "source_text": source_text,
    }
    existing = chart_blocks_by_source.get(source_text)
    if existing is not None:
        _append_linked_location(existing, location)
        return None

    segments = []
    style_ids: list[str | None] = []
    for node_index, node in enumerate(text_nodes):
        style_id = _run_style_id(node) if node.tag == f"{A}t" else None
        style_ids.append(style_id)
        segments.append(
            segment_type(
                segment_id=f"t{node_index}",
                text=node.text or "",
                style_id=style_id,
                metadata={
                    "node_xpath": chart_tree.getpath(node),
                    "run_index": node_index,
                },
            )
        )
    container_xpath = chart_tree.getpath(container)
    block = block_type(
        block_id=_stable_block_id(chart_part, shape_id, container_xpath),
        source_text=source_text,
        protocol="tagged" if len(text_nodes) > 1 else "plain",
        segments=segments,
        context={
            "document_type": "pptx",
            "content_scope": "chart",
            "chart_role": chart_role,
            "slide_number": slide_number,
            "shape_id": shape_id,
            "shape_name": shape_name,
            "shape_type": "chart",
            "paragraph_index": paragraph_index,
            "bbox_emu": list(bbox) if bbox else None,
        },
        metadata={
            "storage_kind": "xml",
            "part_name": chart_part,
            "container_xpath": container_xpath,
            "paragraph_xpath": (
                container_xpath if container.tag == f"{A}p" else ""
            ),
            "block_kind": (
                "paragraph" if container.tag == f"{A}p" else "text_container"
            ),
            "shape_id": shape_id,
            "text_node_xpaths": location["text_node_xpaths"],
            "style_ids": style_ids,
            "linked_locations": [],
        },
    )
    chart_blocks_by_source[source_text] = block
    return block


def _append_linked_location(block: Any, location: Mapping[str, Any]) -> None:
    metadata = _field(block, "metadata", {})
    linked = metadata.setdefault("linked_locations", [])
    identity = (
        location.get("storage_kind"),
        location.get("part_name"),
        location.get("inner_part_name"),
        location.get("container_xpath"),
        tuple(location.get("text_node_xpaths", [])),
    )
    if any(
        (
            item.get("storage_kind"),
            item.get("part_name"),
            item.get("inner_part_name"),
            item.get("container_xpath"),
            tuple(item.get("text_node_xpaths", [])),
        )
        == identity
        for item in linked
    ):
        return
    linked.append(dict(location))


def _chart_text_role(node: etree._Element) -> str:
    ancestor_names = {
        etree.QName(ancestor).localname for ancestor in node.iterancestors()
    }
    if "title" in ancestor_names:
        return "title"
    if "tx" in ancestor_names:
        return "series_name"
    if "cat" in ancestor_names:
        return "category"
    if "dLbl" in ancestor_names or "dLbls" in ancestor_names:
        return "data_label"
    return "chart_text"


def _contains_translatable_text(value: str) -> bool:
    return bool(value.strip()) and any(
        unicodedata.category(character).startswith("L") for character in value
    )


def _chart_workbook_parts(
    package: zipfile.ZipFile,
    chart_part: str,
) -> list[str]:
    parts: list[str] = []
    for relationship in _relationships(package, chart_part):
        if (
            relationship["type"] != PACKAGE_RELATIONSHIP_TYPE
            or relationship["target_mode"] == "External"
        ):
            continue
        part_name = _resolve_target(chart_part, relationship["target"])
        if part_name in package.namelist():
            parts.append(part_name)
    return parts


def _embedded_workbook_text_locations(
    package: zipfile.ZipFile,
    workbook_part: str,
) -> list[dict[str, Any]]:
    data = package.read(workbook_part)
    try:
        workbook_stream = io.BytesIO(data)
        if not zipfile.is_zipfile(workbook_stream):
            return []
        workbook_stream.seek(0)
        with zipfile.ZipFile(workbook_stream) as workbook:
            names = set(workbook.namelist())
            candidate_parts = []
            if "xl/sharedStrings.xml" in names:
                candidate_parts.append("xl/sharedStrings.xml")
            candidate_parts.extend(
                sorted(
                    name
                    for name in names
                    if name.startswith("xl/worksheets/")
                    and name.endswith(".xml")
                )
            )
            locations: list[dict[str, Any]] = []
            for inner_part in candidate_parts:
                tree = _parse_xml(workbook.read(inner_part), inner_part)
                if inner_part == "xl/sharedStrings.xml":
                    containers = tree.xpath("//x:si", namespaces=NS)
                else:
                    containers = tree.xpath(
                        "//x:c[@t='inlineStr']/x:is | //x:c[@t='str']",
                        namespaces=NS,
                    )
                for container in containers:
                    if container.tag == f"{X}c":
                        text_nodes = container.xpath("./x:v", namespaces=NS)
                    else:
                        text_nodes = container.xpath(".//x:t", namespaces=NS)
                    source_text = "".join(
                        str(node.text or "") for node in text_nodes
                    )
                    if not _contains_translatable_text(source_text):
                        continue
                    locations.append(
                        {
                            "storage_kind": "embedded_xlsx",
                            "part_name": workbook_part,
                            "inner_part_name": inner_part,
                            "container_xpath": tree.getpath(container),
                            "text_node_xpaths": [
                                tree.getpath(node) for node in text_nodes
                            ],
                            "source_text": source_text,
                        }
                    )
            return locations
    except (OSError, zipfile.BadZipFile):
        return []


def apply_translations(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    results: Iterable[Any],
    *,
    blocks: Iterable[Any] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Write translations into existing PPTX text nodes in a new package."""

    source = _validated_path(source_path)
    destination = _new_output_path(source, output_path)
    baseline = structure_snapshot(source)
    extracted = (
        list(blocks) if blocks is not None else extract_translation_blocks(source)
    )
    block_map = _unique_by_block_id(extracted, "translation blocks")
    result_map = _unique_by_block_id(results, "translation results")

    unknown = sorted(set(result_map) - set(block_map))
    if unknown:
        raise UnsafePptxPatchError(
            "translation results reference unknown blocks: "
            + ", ".join(unknown[:10])
        )

    modified_parts: dict[str, etree._ElementTree] = {}
    modified_binary_parts: dict[str, bytes] = {}
    embedded_tree_cache: dict[tuple[str, str], etree._ElementTree] = {}
    embedded_mutations: dict[str, set[str]] = {}
    applied: list[str] = []
    skipped: list[dict[str, Any]] = []
    with zipfile.ZipFile(source) as package:
        for block_id, result in result_map.items():
            block = block_map[block_id]
            part_name = str(_block_metadata(block, "part_name"))
            if part_name not in modified_parts:
                modified_parts[part_name] = _parse_xml(
                    package.read(part_name),
                    part_name,
                )
            tree = modified_parts[part_name]
            linked_targets, linked_reason = _linked_translation_targets(
                package=package,
                modified_parts=modified_parts,
                embedded_tree_cache=embedded_tree_cache,
                block=block,
            )
            if linked_reason:
                skipped.append(
                    {
                        "block_id": block_id,
                        "reason": linked_reason["message"],
                        "details": linked_reason["details"],
                    }
                )
                continue
            reason = _patch_translation_block(tree, block, result, strict=strict)
            if reason:
                skipped.append({"block_id": block_id, "reason": reason})
            else:
                translated_text = _result_translation_text(block, result)
                if translated_text is None:
                    skipped.append(
                        {
                            "block_id": block_id,
                            "reason": "translation result has no text",
                        }
                    )
                    continue
                target_language = _result_target_language(result)
                for target in linked_targets:
                    _write_linked_translation(
                        target,
                        translated_text,
                        target_language=target_language,
                    )
                    if target["storage_kind"] == "embedded_xlsx":
                        embedded_mutations.setdefault(
                            target["part_name"],
                            set(),
                        ).add(target["inner_part_name"])
                applied.append(block_id)

        if strict and skipped:
            raise UnsafePptxPatchError(_skipped_message("PPTX patch", skipped))
        for outer_part, inner_parts in embedded_mutations.items():
            modified_binary_parts[outer_part] = _rewrite_embedded_package(
                package.read(outer_part),
                {
                    inner_part: embedded_tree_cache[(outer_part, inner_part)]
                    for inner_part in inner_parts
                },
                outer_part,
            )
        _write_package(
            package,
            destination,
            modified_parts,
            modified_binary_parts=modified_binary_parts,
            baseline_snapshot=baseline,
        )

    return {
        "output_path": str(destination),
        "applied": applied,
        "skipped": skipped,
        "audit": {
            "operation": "apply_translations",
            "source_sha256": _file_sha256(source),
            "output_sha256": _file_sha256(destination),
            "modified_parts": sorted(
                set(modified_parts) | set(modified_binary_parts)
            ),
            "source_overwritten": False,
        },
        "validation": validate_pptx(destination, baseline),
    }


def structure_snapshot(
    path: str | os.PathLike[str],
    *,
    _skip_validation: bool = False,
) -> dict[str, Any]:
    """Return slide, shape, relationship, and media structural evidence."""

    source = (
        Path(path).expanduser().resolve()
        if _skip_validation
        else _validated_path(path)
    )
    with zipfile.ZipFile(source) as package:
        names = sorted(package.namelist())
        presentation = _parse_xml(
            package.read("ppt/presentation.xml"),
            "ppt/presentation.xml",
        )
        size_nodes = presentation.xpath("/p:presentation/p:sldSz", namespaces=NS)
        if len(size_nodes) != 1:
            raise InvalidPptxError(
                "ppt/presentation.xml must contain exactly one p:sldSz"
            )
        width = _required_int(size_nodes[0], "cx", "slide width")
        height = _required_int(size_nodes[0], "cy", "slide height")
        tree_cache: dict[str, etree._ElementTree] = {
            "ppt/presentation.xml": presentation
        }
        slides: list[dict[str, Any]] = []
        for slide_number, part_name in enumerate(
            _ordered_slide_parts(package),
            start=1,
        ):
            tree = _part_tree(package, part_name, tree_cache)
            shapes: list[dict[str, Any]] = []
            for shape in _shape_elements(tree):
                shape_id, shape_name = _shape_identity(shape, tree)
                bbox = _shape_bbox(package, part_name, shape, tree_cache)
                paragraphs = [
                    paragraph
                    for paragraph in shape.xpath(".//a:p", namespaces=NS)
                    if _nearest_shape(paragraph) is shape
                ]
                text = "\n".join(
                    "".join(
                        node.text or ""
                        for node in _translatable_text_nodes(paragraph)
                    )
                    for paragraph in paragraphs
                )
                text = "\n".join(line for line in text.splitlines() if line)
                body_properties = _body_properties(shape)
                font_sizes = _font_sizes_pt(shape)
                capacity = _shape_capacity(shape, bbox, text=text)
                shapes.append(
                    {
                        "shape_id": shape_id,
                        "name": shape_name,
                        "shape_type": _shape_type(shape),
                        "bbox_emu": list(bbox) if bbox else None,
                        "text": text,
                        "text_block_count": sum(
                            1
                            for paragraph in paragraphs
                            if _translatable_text_nodes(paragraph)
                        ),
                        "body_properties": body_properties,
                        "font_sizes_pt": font_sizes,
                        "capacity": capacity,
                        "xml_path": tree.getpath(shape),
                    }
                )
            shape_ids = [str(shape["shape_id"]) for shape in shapes]
            if len(shape_ids) != len(set(shape_ids)):
                raise InvalidPptxError(
                    f"duplicate shape ids on slide {slide_number}"
                )
            slides.append(
                {
                    "number": slide_number,
                    "part_name": part_name,
                    "shape_count": len(shapes),
                    "shapes": shapes,
                }
            )
        return {
            "path": str(source),
            "slide_size_emu": {"width": width, "height": height},
            "slide_count": len(slides),
            "slides": slides,
            "media_hashes": {
                name: _sha256(package.read(name))
                for name in names
                if name.startswith("ppt/media/") and not name.endswith("/")
            },
            "relationships": _relationship_snapshot(package),
            "package_entries": names,
            "package_entry_count": len(names),
        }


def run_pptx_qa(
    source_path: str | os.PathLike[str],
    candidate_path: str | os.PathLike[str],
    *,
    coordinate_tolerance_emu: int = 12_700,
    overlap_threshold: float = 0.20,
    expansion_ratio_threshold: float = 1.50,
    capacity_warning_ratio: float = 0.85,
    blocks: Iterable[Any] | None = None,
    translations: Iterable[Any] | None = None,
    authorized_coordinate_changes: Mapping[
        str, Sequence[int]
    ] | None = None,
) -> list[Any]:
    """Compare two PPTX packages using native OOXML structure and geometry.

    Layout checks use native slide coordinates and text properties.  Capacity
    is necessarily an estimate, so overlap and capacity findings are marked as
    warnings unless the text box is clearly beyond its calculated capacity.
    """

    # Accepted for orchestration interface compatibility.  The native geometry
    # checks do not require translation payloads, but callers may supply them.
    _ = blocks, translations
    if coordinate_tolerance_emu < 0:
        raise ValueError("coordinate_tolerance_emu cannot be negative")
    if not 0 < overlap_threshold <= 1:
        raise ValueError("overlap_threshold must be in the range (0, 1]")
    if expansion_ratio_threshold <= 1:
        raise ValueError("expansion_ratio_threshold must be greater than 1")
    if not 0 < capacity_warning_ratio <= 1:
        raise ValueError("capacity_warning_ratio must be in the range (0, 1]")

    source = structure_snapshot(source_path)
    candidate = structure_snapshot(candidate_path)
    issues: list[Any] = []
    source_size = source["slide_size_emu"]
    candidate_size = candidate["slide_size_emu"]
    if source_size != candidate_size:
        issues.append(
            _qa_issue(
                code="PPTX_SLIDE_SIZE_MISMATCH",
                message="Candidate slide canvas size differs from the source.",
                severity="error",
                details={
                    "source_slide_size_emu": source_size,
                    "candidate_slide_size_emu": candidate_size,
                },
            )
        )
    if source["slide_count"] != candidate["slide_count"]:
        issues.append(
            _qa_issue(
                code="PPTX_SLIDE_COUNT_MISMATCH",
                message=(
                    f"Candidate contains {candidate['slide_count']} slides; "
                    f"source contains {source['slide_count']}."
                ),
                severity="error",
                details={
                    "source_slide_count": source["slide_count"],
                    "candidate_slide_count": candidate["slide_count"],
                },
            )
        )
    if source["media_hashes"] != candidate["media_hashes"]:
        issues.append(
            _qa_issue(
                code="PPTX_MEDIA_CHANGED",
                message="Embedded media entries or content changed.",
                severity="error",
                details=_mapping_difference(
                    source["media_hashes"],
                    candidate["media_hashes"],
                ),
            )
        )
    if source["relationships"] != candidate["relationships"]:
        issues.append(
            _qa_issue(
                code="PPTX_RELATIONSHIPS_CHANGED",
                message="OOXML package relationships changed.",
                severity="error",
                details=_mapping_difference(
                    source["relationships"],
                    candidate["relationships"],
                ),
            )
        )

    source_slides = {
        int(slide["number"]): slide for slide in source["slides"]
    }
    candidate_slides = {
        int(slide["number"]): slide for slide in candidate["slides"]
    }
    slide_width = int(candidate_size["width"])
    slide_height = int(candidate_size["height"])
    for slide_number in sorted(set(source_slides) | set(candidate_slides)):
        source_slide = source_slides.get(slide_number)
        candidate_slide = candidate_slides.get(slide_number)
        if source_slide is None or candidate_slide is None:
            continue
        source_shapes = _shape_snapshot_map(source_slide)
        candidate_shapes = _shape_snapshot_map(candidate_slide)
        if source_slide["shape_count"] != candidate_slide["shape_count"]:
            issues.append(
                _qa_issue(
                    code="PPTX_SHAPE_COUNT_MISMATCH",
                    message=(
                        f"Slide {slide_number} shape count changed from "
                        f"{source_slide['shape_count']} to "
                        f"{candidate_slide['shape_count']}."
                    ),
                    severity="error",
                    slide=slide_number,
                    details={
                        "source_shape_count": source_slide["shape_count"],
                        "candidate_shape_count": candidate_slide["shape_count"],
                        "removed_shape_ids": sorted(
                            set(source_shapes) - set(candidate_shapes)
                        ),
                        "added_shape_ids": sorted(
                            set(candidate_shapes) - set(source_shapes)
                        ),
                    },
                )
            )

        for shape_id in sorted(set(source_shapes) & set(candidate_shapes)):
            source_shape = source_shapes[shape_id]
            candidate_shape = candidate_shapes[shape_id]
            source_bbox = source_shape.get("bbox_emu")
            candidate_bbox = candidate_shape.get("bbox_emu")
            coordinate_change_key = _coordinate_change_key(
                slide_number,
                shape_id,
            )
            authorized_bbox = (
                authorized_coordinate_changes.get(coordinate_change_key)
                if authorized_coordinate_changes
                else None
            )
            coordinate_change_authorized = (
                authorized_bbox is not None
                and _bbox_equal(
                    authorized_bbox,
                    candidate_bbox,
                    coordinate_tolerance_emu,
                )
            )
            if not coordinate_change_authorized and not _bbox_equal(
                source_bbox,
                candidate_bbox,
                coordinate_tolerance_emu,
            ):
                issues.append(
                    _qa_issue(
                        code="PPTX_SHAPE_COORDINATE_CHANGED",
                        message=(
                            f"Slide {slide_number} shape {shape_id} coordinates "
                            "changed."
                        ),
                        severity="error",
                        slide=slide_number,
                        details={
                            "shape_id": shape_id,
                            "shape_name": candidate_shape.get("name"),
                            "source_bbox_emu": source_bbox,
                            "candidate_bbox_emu": candidate_bbox,
                            "tolerance_emu": coordinate_tolerance_emu,
                        },
                    )
                )

            if candidate_bbox is not None and candidate_shape.get("text"):
                candidate_out = _bbox_outside(
                    candidate_bbox,
                    slide_width,
                    slide_height,
                    coordinate_tolerance_emu,
                )
                source_out = (
                    _bbox_outside(
                        source_bbox,
                        int(source_size["width"]),
                        int(source_size["height"]),
                        coordinate_tolerance_emu,
                    )
                    if source_bbox is not None
                    else False
                )
                if candidate_out and not source_out:
                    issues.append(
                        _qa_issue(
                            code="PPTX_SHAPE_OUT_OF_BOUNDS",
                            message=(
                                f"Slide {slide_number} text shape {shape_id} "
                                "extends outside the slide canvas."
                            ),
                            severity="error",
                            slide=slide_number,
                            details={
                                "shape_id": shape_id,
                                "shape_name": candidate_shape.get("name"),
                                "bbox_emu": candidate_bbox,
                                "slide_size_emu": candidate_size,
                            },
                        )
                    )

            source_text = str(source_shape.get("text") or "")
            candidate_text = str(candidate_shape.get("text") or "")
            source_prompt_rules = set(_prompt_leakage_rules(source_text))
            candidate_prompt_rules = [
                rule
                for rule in _prompt_leakage_rules(candidate_text)
                if rule not in source_prompt_rules
            ]
            if candidate_prompt_rules:
                issues.append(
                    _qa_issue(
                        code="PPTX_TRANSLATION_PROMPT_LEAKAGE",
                        message=(
                            f"Slide {slide_number} shape {shape_id} contains "
                            "translation-instruction text that is absent from "
                            "the source."
                        ),
                        severity="error",
                        slide=slide_number,
                        details={
                            "shape_id": shape_id,
                            "shape_name": candidate_shape.get("name"),
                            "matched_rules": candidate_prompt_rules,
                            "candidate_character_count": (
                                _visible_character_count(candidate_text)
                            ),
                            "repair_hint": _pptx_repair_hint(
                                "PPTX_TRANSLATION_PROMPT_LEAKAGE",
                                slide_number,
                                shape_id,
                            ),
                        },
                    )
                )
            source_chars = _visible_character_count(source_text)
            candidate_chars = _visible_character_count(candidate_text)
            expansion_ratio = (
                candidate_chars / source_chars if source_chars else None
            )
            if (
                expansion_ratio is not None
                and source_chars >= 20
                and expansion_ratio >= expansion_ratio_threshold
            ):
                issues.append(
                    _qa_issue(
                        code="PPTX_TRANSLATION_EXPANSION_RISK",
                        message=(
                            f"Slide {slide_number} shape {shape_id} translated "
                            f"text expanded by {expansion_ratio:.2f}x."
                        ),
                        severity="warning",
                        slide=slide_number,
                        details={
                            "shape_id": shape_id,
                            "shape_name": candidate_shape.get("name"),
                            "source_character_count": source_chars,
                            "candidate_character_count": candidate_chars,
                            "expansion_ratio": round(expansion_ratio, 6),
                            "threshold": expansion_ratio_threshold,
                            "repair_hint": _pptx_repair_hint(
                                "PPTX_TRANSLATION_EXPANSION_RISK",
                                slide_number,
                                shape_id,
                            ),
                        },
                    )
                )

            capacity = candidate_shape.get("capacity")
            source_capacity = source_shape.get("capacity")
            if capacity and candidate_shape.get("shape_type") == "shape":
                load_ratio = float(capacity.get("load_ratio") or 0.0)
                source_load_ratio = (
                    float(source_capacity.get("load_ratio") or 0.0)
                    if source_capacity
                    else 0.0
                )
                if (
                    load_ratio >= capacity_warning_ratio
                    and load_ratio > source_load_ratio + 0.05
                ):
                    severity = "error" if load_ratio > 1.0 else "warning"
                    issues.append(
                        _qa_issue(
                            code="PPTX_TEXT_CAPACITY_RISK",
                            message=(
                                f"Slide {slide_number} shape {shape_id} uses an "
                                f"estimated {load_ratio:.0%} of text capacity."
                            ),
                            severity=severity,
                            slide=slide_number,
                            details={
                                "shape_id": shape_id,
                                "shape_name": candidate_shape.get("name"),
                                "capacity": capacity,
                                "source_load_ratio": source_load_ratio,
                                "threshold": capacity_warning_ratio,
                                "repair_hint": _pptx_repair_hint(
                                    "PPTX_TEXT_CAPACITY_RISK",
                                    slide_number,
                                    shape_id,
                                ),
                            },
                        )
                    )

            new_glyphs = _new_suspicious_characters(source_text, candidate_text)
            if new_glyphs:
                issues.append(
                    _qa_issue(
                        code="PPTX_NEW_GARBLED_TEXT",
                        message=(
                            f"Slide {slide_number} shape {shape_id} introduces "
                            "suspicious text characters."
                        ),
                        severity="error",
                        slide=slide_number,
                        details={
                            "shape_id": shape_id,
                            "shape_name": candidate_shape.get("name"),
                            "characters": new_glyphs,
                            "repair_hint": _pptx_repair_hint(
                                "PPTX_NEW_GARBLED_TEXT",
                                slide_number,
                                shape_id,
                            ),
                        },
                    )
                )

        source_overlaps = _shape_overlaps(source_slide, overlap_threshold)
        candidate_overlaps = _shape_overlaps(
            candidate_slide,
            overlap_threshold,
        )
        for pair_key in sorted(candidate_overlaps):
            overlap = candidate_overlaps[pair_key]
            source_overlap = source_overlaps.get(pair_key)
            if (
                source_overlap is not None
                and float(overlap["intersection_ratio"])
                <= float(source_overlap["intersection_ratio"]) + 0.10
            ):
                continue
            issues.append(
                _qa_issue(
                    code="PPTX_SUSPECTED_SHAPE_OVERLAP",
                    message=(
                        f"Slide {slide_number} introduces a possible text "
                        f"overlap between shapes {pair_key[0]} and {pair_key[1]}."
                    ),
                    severity="warning",
                    slide=slide_number,
                    details={
                        **overlap,
                        "source_intersection_ratio": (
                            source_overlap["intersection_ratio"]
                            if source_overlap is not None
                            else None
                        ),
                        "repair_hint": _pptx_repair_hint(
                            "PPTX_SUSPECTED_SHAPE_OVERLAP",
                            slide_number,
                            pair_key[1],
                        ),
                    },
                )
            )
    return _deduplicate_qa_issues(issues)


def apply_whitelisted_repairs(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    actions: Iterable[Mapping[str, Any]],
    *,
    blocks: Iterable[Any] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Apply bounded native-PPTX repairs to a new file with an audit trail."""

    source = _validated_path(source_path)
    destination = _new_output_path(source, output_path)
    baseline = structure_snapshot(source)
    normalized_actions = [
        _normalize_repair_action_input(action) for action in actions
    ]
    unsupported = sorted(
        {
            str(action.get("action", ""))
            for action in normalized_actions
            if action.get("action") not in REPAIR_ACTIONS
        }
    )
    if unsupported:
        raise UnsupportedPptxRepairError(
            "unsupported PPTX repair actions: " + ", ".join(unsupported)
        )
    priorities = {
        "replace_translation": 0,
        "set_autofit": 1,
        "resolve_text_overflow": 1,
        "adjust_margins": 2,
        "adjust_font_size": 3,
        "set_font_size": 3,
        "set_shape_bounds": 4,
        "adjust_shape_geometry": 4,
        "restore_shape_coordinates": 4,
        "restore_table_row_heights": 4,
    }
    ordered_actions = sorted(
        enumerate(normalized_actions),
        key=lambda item: (priorities[str(item[1]["action"])], item[0]),
    )
    extracted = (
        list(blocks) if blocks is not None else extract_translation_blocks(source)
    )
    block_map = _unique_by_block_id(extracted, "translation blocks")
    modified_parts: dict[str, etree._ElementTree] = {}
    audit_actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    authorized_coordinate_changes: dict[str, list[int]] = {}

    with zipfile.ZipFile(source) as package:
        slide_parts = _ordered_slide_parts(package)
        for original_index, action in ordered_actions:
            action_name = str(action["action"])
            part_backups = {
                name: etree.tostring(
                    tree,
                    xml_declaration=True,
                    encoding="UTF-8",
                    standalone=True,
                )
                for name, tree in modified_parts.items()
            }
            try:
                if action_name == "replace_translation":
                    audit = _repair_replace_translation(
                        package,
                        modified_parts,
                        block_map,
                        action,
                        strict=strict,
                    )
                else:
                    audit = _repair_shape_formatting(
                        package,
                        modified_parts,
                        slide_parts,
                        action,
                        baseline_snapshot=baseline,
                    )
                authorized_bbox = audit.get("authorized_bbox_emu")
                audit_slide = audit.get("slide")
                audit_shape_id = audit.get("shape_id")
                if (
                    authorized_bbox is not None
                    and audit_slide is not None
                    and audit_shape_id is not None
                ):
                    authorized_coordinate_changes[
                        _coordinate_change_key(
                            int(audit_slide),
                            str(audit_shape_id),
                        )
                    ] = [int(value) for value in authorized_bbox]
                audit_actions.append(
                    {
                        "request_index": original_index,
                        "action": action_name,
                        "status": "APPLIED",
                        **audit,
                    }
                )
            except (UnsafePptxPatchError, UnsupportedPptxRepairError) as exc:
                for name in set(modified_parts) - set(part_backups):
                    modified_parts.pop(name, None)
                for name, data in part_backups.items():
                    modified_parts[name] = _parse_xml(data, name)
                if strict:
                    raise
                skipped.append(
                    {
                        "request_index": original_index,
                        "action": action_name,
                        "status": "SKIPPED",
                        "reason": str(exc),
                    }
                )
        _write_package(
            package,
            destination,
            modified_parts,
            baseline_snapshot=baseline,
            authorized_coordinate_changes=authorized_coordinate_changes,
        )

    validation = validate_pptx(
        destination,
        baseline,
        authorized_coordinate_changes=authorized_coordinate_changes,
    )
    return {
        "output_path": str(destination),
        "applied": audit_actions,
        "skipped": skipped,
        "audit": {
            "operation": "apply_whitelisted_repairs",
            "repair_whitelist": sorted(REPAIR_ACTIONS),
            "priority_order": [
                "replace_translation",
                "set_autofit",
                "resolve_text_overflow",
                "adjust_margins",
                "adjust_font_size",
                "set_font_size",
                "set_shape_bounds",
                "adjust_shape_geometry",
                "restore_shape_coordinates",
                "restore_table_row_heights",
            ],
            "source_path": str(source),
            "output_path": str(destination),
            "source_sha256": _file_sha256(source),
            "output_sha256": _file_sha256(destination),
            "source_overwritten": False,
            "modified_parts": sorted(modified_parts),
            "actions": audit_actions,
            "skipped": skipped,
            "authorized_coordinate_changes": authorized_coordinate_changes,
        },
        "validation": validation,
        "qa_issues": run_pptx_qa(
            source,
            destination,
            authorized_coordinate_changes=authorized_coordinate_changes,
        ),
    }


def _normalize_repair_action_input(action: Any) -> dict[str, Any]:
    if isinstance(action, Mapping):
        payload = dict(action)
    else:
        dumper = getattr(action, "model_dump", None)
        if not callable(dumper):
            raise TypeError(
                "PPTX repair actions must be mappings or model_dump-capable "
                f"objects, received {type(action).__name__}"
            )
        payload = dict(dumper(mode="python"))
    action_value = payload.get("action", payload.get("action_type"))
    if hasattr(action_value, "value"):
        action_value = action_value.value
    action_name = str(action_value or "")
    aliases = {
        "enable_autofit": "set_autofit",
        "set_textbox_margins": "adjust_margins",
    }
    payload["action"] = aliases.get(action_name, action_name)

    geometry = payload.get("geometry")
    if geometry is not None:
        geometry_payload = _model_or_mapping(geometry, "geometry")
        unit = str(geometry_payload.get("unit", "points"))
        multiplier = EMUS_PER_POINT if unit == "points" else 1
        if unit not in {"points", "emu"}:
            raise UnsupportedPptxRepairError(
                "geometry unit must be points or emu"
            )
        try:
            payload["bounds_emu"] = [
                round(float(geometry_payload[name]) * multiplier)
                for name in ("x", "y", "width", "height")
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise UnsupportedPptxRepairError(
                "geometry requires numeric x, y, width, height"
            ) from exc

    margins = payload.get("textbox_margins")
    if margins is not None:
        margin_payload = _model_or_mapping(margins, "textbox_margins")
        try:
            payload["margins_emu"] = {
                "lIns": round(float(margin_payload["left"]) * EMUS_PER_POINT),
                "rIns": round(float(margin_payload["right"]) * EMUS_PER_POINT),
                "tIns": round(float(margin_payload["top"]) * EMUS_PER_POINT),
                "bIns": round(float(margin_payload["bottom"]) * EMUS_PER_POINT),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise UnsupportedPptxRepairError(
                "textbox_margins requires numeric left, right, top, bottom"
            ) from exc
    if (
        payload.get("minimum_font_size_pt") is None
        and payload.get("min_font_size_pt") is not None
    ):
        payload["minimum_font_size_pt"] = payload["min_font_size_pt"]
    if (
        payload.get("font_size_pt") is None
        and payload.get("target_font_size_pt") is not None
    ):
        payload["font_size_pt"] = payload["target_font_size_pt"]
    if payload.get("text") is None and payload.get("proposed_text") is not None:
        payload["text"] = payload["proposed_text"]
    return payload


def _model_or_mapping(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dict(dumper(mode="python"))
    raise UnsupportedPptxRepairError(
        f"{label} must be a mapping-like value"
    )


def execute_pptx_repairs(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    actions: Iterable[Mapping[str, Any]],
    audit_path: str | os.PathLike[str] | None = None,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Stable QA-agent wrapper that optionally persists the repair audit."""

    audit: Path | None = None
    if audit_path is not None:
        audit = Path(audit_path).expanduser().resolve()
        source_path = Path(source).expanduser().resolve()
        output_path = Path(output).expanduser().resolve()
        if audit in {source_path, output_path}:
            raise UnsafePptxPatchError(
                "Audit output must differ from source and repaired PPTX"
            )
        if audit.exists():
            raise UnsafePptxPatchError(
                f"Refusing to overwrite existing audit: {audit}"
            )
    report = apply_whitelisted_repairs(source, output, actions, strict=strict)
    if audit is not None:
        audit.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            suffix=".json",
            prefix=f".{audit.stem}-",
            dir=audit.parent,
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(
                json.dumps(
                    report["audit"],
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(audit)
        finally:
            temporary.unlink(missing_ok=True)
        report["audit_path"] = str(audit)
    return report


class PptxEngine:
    """Object facade for dependency injection into orchestration code."""

    def inspect(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        return structure_snapshot(path)

    def extract(self, path: str | os.PathLike[str]) -> list[Any]:
        return extract_translation_blocks(path)

    def patch(
        self,
        source_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        results: Iterable[Any],
        *,
        blocks: Iterable[Any] | None = None,
        strict: bool = True,
    ) -> dict[str, Any]:
        return apply_translations(
            source_path,
            output_path,
            results,
            blocks=blocks,
            strict=strict,
        )

    def validate(
        self,
        path: str | os.PathLike[str],
        baseline_snapshot: Mapping[str, Any] | None = None,
        *,
        authorized_coordinate_changes: Mapping[
            str, Sequence[int]
        ] | None = None,
    ) -> dict[str, Any]:
        return validate_pptx(
            path,
            baseline_snapshot,
            authorized_coordinate_changes=authorized_coordinate_changes,
        )

    def qa(
        self,
        source_path: str | os.PathLike[str],
        candidate_path: str | os.PathLike[str],
        **options: Any,
    ) -> list[Any]:
        return run_pptx_qa(source_path, candidate_path, **options)

    def repair(
        self,
        source_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        actions: Iterable[Mapping[str, Any]],
        *,
        blocks: Iterable[Any] | None = None,
        strict: bool = True,
    ) -> dict[str, Any]:
        return apply_whitelisted_repairs(
            source_path,
            output_path,
            actions,
            blocks=blocks,
            strict=strict,
        )


def _validated_path(path: str | os.PathLike[str]) -> Path:
    source = Path(path).expanduser().resolve()
    validation = validate_pptx(source)
    if not validation["valid"]:
        raise InvalidPptxError(
            "Invalid PPTX package: " + "; ".join(validation["errors"])
        )
    return source


def _safe_part_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return (
        bool(normalized)
        and not normalized.startswith("/")
        and not re.match(r"^[A-Za-z]:", normalized)
        and ".." not in normalized.split("/")
        and "\x00" not in normalized
    )


def _parse_xml(data: bytes, part_name: str) -> etree._ElementTree:
    if b"<!DOCTYPE" in data[:10_000].upper():
        raise InvalidPptxError(f"DOCTYPE is forbidden in OOXML part {part_name}")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_blank_text=False,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise InvalidPptxError(f"invalid XML in {part_name}: {exc}") from exc
    return etree.ElementTree(root)


def _relationship_part_name(part_name: str) -> str:
    directory = posixpath.dirname(part_name)
    filename = posixpath.basename(part_name)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _resolve_target(part_name: str, target: str) -> str:
    target = target.replace("\\", "/")
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(part_name), target))


def _relationships(
    package: zipfile.ZipFile,
    part_name: str,
) -> list[dict[str, str]]:
    rels_name = _relationship_part_name(part_name)
    if rels_name not in package.namelist():
        return []
    tree = _parse_xml(package.read(rels_name), rels_name)
    output: list[dict[str, str]] = []
    for relationship in tree.getroot():
        if etree.QName(relationship).localname != "Relationship":
            continue
        output.append(
            {
                "id": relationship.get("Id", ""),
                "type": relationship.get("Type", ""),
                "target": relationship.get("Target", ""),
                "target_mode": relationship.get("TargetMode", "Internal"),
            }
        )
    return output


def _ordered_slide_parts(package: zipfile.ZipFile) -> list[str]:
    presentation = _parse_xml(
        package.read("ppt/presentation.xml"),
        "ppt/presentation.xml",
    )
    relationship_map = {
        item["id"]: item
        for item in _relationships(package, "ppt/presentation.xml")
        if item["type"] == SLIDE_RELATIONSHIP_TYPE
    }
    slide_parts: list[str] = []
    for slide_id in presentation.xpath(
        "/p:presentation/p:sldIdLst/p:sldId",
        namespaces=NS,
    ):
        relationship_id = slide_id.get(f"{R}id")
        relationship = relationship_map.get(str(relationship_id))
        if relationship is None:
            raise InvalidPptxError(
                f"slide relationship is missing: {relationship_id}"
            )
        if relationship["target_mode"] == "External":
            raise InvalidPptxError(
                f"slide relationship cannot be external: {relationship_id}"
            )
        part_name = _resolve_target(
            "ppt/presentation.xml",
            relationship["target"],
        )
        if part_name not in package.namelist():
            raise InvalidPptxError(f"slide part does not exist: {part_name}")
        slide_parts.append(part_name)
    if len(slide_parts) != len(set(slide_parts)):
        raise InvalidPptxError("presentation references a slide part more than once")
    return slide_parts


def _relationship_errors(package: zipfile.ZipFile) -> list[str]:
    errors: list[str] = []
    names = set(package.namelist())
    for rels_name in sorted(name for name in names if name.endswith(".rels")):
        tree = _parse_xml(package.read(rels_name), rels_name)
        seen_ids: set[str] = set()
        owner = _owner_part_name(rels_name)
        for relationship in tree.getroot():
            if etree.QName(relationship).localname != "Relationship":
                continue
            relationship_id = relationship.get("Id", "")
            if not relationship_id or relationship_id in seen_ids:
                errors.append(
                    f"invalid or duplicate relationship id in {rels_name}: "
                    f"{relationship_id!r}"
                )
            seen_ids.add(relationship_id)
            if relationship.get("TargetMode") == "External":
                continue
            target = relationship.get("Target", "")
            resolved = _resolve_target(owner, target)
            if not _safe_part_name(resolved):
                errors.append(
                    f"unsafe relationship target in {rels_name}: {target}"
                )
            elif resolved not in names:
                errors.append(
                    f"broken relationship in {rels_name}: "
                    f"{relationship_id} -> {resolved}"
                )
    return errors


def _owner_part_name(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return ""
    directory = posixpath.dirname(rels_name)
    if not directory.endswith("/_rels"):
        raise InvalidPptxError(f"invalid relationship part path: {rels_name}")
    owner_directory = directory[: -len("/_rels")]
    filename = posixpath.basename(rels_name)
    if not filename.endswith(".rels"):
        raise InvalidPptxError(f"invalid relationship part path: {rels_name}")
    return posixpath.join(owner_directory, filename[: -len(".rels")])


def _slide_order_errors(package: zipfile.ZipFile) -> list[str]:
    try:
        parts = _ordered_slide_parts(package)
    except InvalidPptxError as exc:
        return [str(exc)]
    if not parts:
        return ["presentation contains no slides"]
    return []


def _external_relationship_count(package: zipfile.ZipFile) -> int:
    count = 0
    for name in package.namelist():
        if not name.endswith(".rels"):
            continue
        tree = _parse_xml(package.read(name), name)
        count += len(
            tree.xpath(
                "/*[local-name()='Relationships']/*"
                "[local-name()='Relationship' and @TargetMode='External']"
            )
        )
    return count


def _relationship_snapshot(
    package: zipfile.ZipFile,
) -> dict[str, list[dict[str, str]]]:
    snapshot: dict[str, list[dict[str, str]]] = {}
    for name in sorted(
        member for member in package.namelist() if member.endswith(".rels")
    ):
        tree = _parse_xml(package.read(name), name)
        relationships = []
        for node in tree.getroot():
            if etree.QName(node).localname != "Relationship":
                continue
            relationships.append(
                {
                    "id": node.get("Id", ""),
                    "type": node.get("Type", ""),
                    "target": node.get("Target", ""),
                    "target_mode": node.get("TargetMode", "Internal"),
                }
            )
        snapshot[name] = sorted(
            relationships,
            key=lambda item: (
                item["id"],
                item["type"],
                item["target"],
                item["target_mode"],
            ),
        )
    return snapshot


def _part_tree(
    package: zipfile.ZipFile,
    part_name: str,
    cache: dict[str, etree._ElementTree],
) -> etree._ElementTree:
    if part_name not in cache:
        cache[part_name] = _parse_xml(package.read(part_name), part_name)
    return cache[part_name]


def _shape_elements(tree: etree._ElementTree) -> list[etree._Element]:
    """Return the effective slide shapes in visual tree order.

    A slide shape tree can contain Markup Compatibility ``AlternateContent``
    nodes whose Choice and Fallback branches describe the same logical shape.
    Descendant XPath would count both branches and would also mistake preview
    ``p:pic`` nodes nested inside graphic frames/OLE objects for slide shapes.
    Traverse only direct logical children of ``p:spTree`` and ``p:grpSp`` and
    recurse into the selected compatibility branch and real groups.
    """

    shape_trees = tree.xpath("/p:sld/p:cSld/p:spTree", namespaces=NS)
    if len(shape_trees) != 1:
        raise InvalidPptxError(
            "slide XML must contain exactly one p:cSld/p:spTree"
        )
    return list(_logical_shape_children(shape_trees[0]))


def _logical_shape_children(container: etree._Element) -> Iterable[etree._Element]:
    for child in container:
        if child.tag in _SHAPE_TAGS:
            yield child
            if child.tag == f"{P}grpSp":
                yield from _logical_shape_children(child)
            continue
        if child.tag == f"{MC}AlternateContent":
            branch = _selected_alternate_content_branch(child)
            yield from _logical_shape_children(branch)


def _selected_alternate_content_branch(
    alternate_content: etree._Element,
) -> etree._Element:
    fallback: etree._Element | None = None
    for child in alternate_content:
        if child.tag == f"{MC}Choice" and _supported_mc_choice(child):
            return child
        if child.tag == f"{MC}Fallback" and fallback is None:
            fallback = child
    if fallback is not None:
        return fallback
    raise InvalidPptxError(
        "mc:AlternateContent has no supported Choice or Fallback in shape tree"
    )


def _supported_mc_choice(choice: etree._Element) -> bool:
    required_prefixes = str(choice.get("Requires", "") or "").split()
    if not required_prefixes:
        return False
    return all(
        choice.nsmap.get(prefix) in _SUPPORTED_MC_CHOICE_NAMESPACES
        for prefix in required_prefixes
    )


def _logical_chart_references(
    tree: etree._ElementTree,
) -> Iterable[tuple[etree._Element, etree._Element]]:
    for shape in _shape_elements(tree):
        if shape.tag != f"{P}graphicFrame":
            continue
        for chart_reference in shape.xpath(".//c:chart", namespaces=NS):
            if _nearest_shape(chart_reference) is shape:
                yield shape, chart_reference


def _nearest_shape(node: etree._Element) -> etree._Element | None:
    return next(
        (
            ancestor
            for ancestor in node.iterancestors()
            if ancestor.tag in _SHAPE_TAGS
        ),
        None,
    )


def _shape_identity(
    shape: etree._Element,
    tree: etree._ElementTree,
) -> tuple[str, str]:
    nodes = shape.xpath(
        "./p:nvSpPr/p:cNvPr | ./p:nvPicPr/p:cNvPr | "
        "./p:nvGraphicFramePr/p:cNvPr | ./p:nvCxnSpPr/p:cNvPr | "
        "./p:nvGrpSpPr/p:cNvPr",
        namespaces=NS,
    )
    if nodes:
        shape_id = nodes[0].get("id") or ""
        name = nodes[0].get("name") or ""
        if shape_id:
            return str(shape_id), str(name)
    xpath = tree.getpath(shape)
    fallback = hashlib.sha256(xpath.encode("utf-8")).hexdigest()[:12]
    return f"xpath-{fallback}", ""


def _shape_type(shape: etree._Element) -> str:
    local_name = etree.QName(shape).localname
    return {
        "sp": "shape",
        "pic": "picture",
        "graphicFrame": "graphic_frame",
        "cxnSp": "connector",
        "grpSp": "group",
    }.get(local_name, local_name)


def _shape_bbox(
    package: zipfile.ZipFile,
    slide_part: str,
    shape: etree._Element,
    cache: dict[str, etree._ElementTree],
) -> tuple[int, int, int, int] | None:
    bbox = _local_shape_bbox(shape)
    if bbox is None and _shape_type(shape) == "shape":
        bbox = _placeholder_bbox(package, slide_part, shape, cache)
    if bbox is None:
        return None
    x, y, width, height = bbox
    ancestor_groups = [
        ancestor
        for ancestor in shape.iterancestors()
        if ancestor.tag == f"{P}grpSp"
    ]
    for group in ancestor_groups:
        transform = _group_transform(group)
        if transform is None:
            continue
        off_x, off_y, ext_x, ext_y, child_x, child_y, child_w, child_h = (
            transform
        )
        scale_x = ext_x / child_w if child_w else 1.0
        scale_y = ext_y / child_h if child_h else 1.0
        x = round(off_x + (x - child_x) * scale_x)
        y = round(off_y + (y - child_y) * scale_y)
        width = round(width * scale_x)
        height = round(height * scale_y)
    return x, y, width, height


def _local_shape_bbox(
    shape: etree._Element,
) -> tuple[int, int, int, int] | None:
    local_name = etree.QName(shape).localname
    if local_name == "graphicFrame":
        transforms = shape.xpath("./p:xfrm", namespaces=NS)
    elif local_name == "grpSp":
        transforms = shape.xpath("./p:grpSpPr/a:xfrm", namespaces=NS)
    else:
        transforms = shape.xpath("./p:spPr/a:xfrm", namespaces=NS)
    if not transforms:
        return None
    transform = transforms[0]
    offsets = transform.xpath("./a:off", namespaces=NS)
    extents = transform.xpath("./a:ext", namespaces=NS)
    if not offsets or not extents:
        return None
    try:
        return (
            int(offsets[0].get("x", "")),
            int(offsets[0].get("y", "")),
            int(extents[0].get("cx", "")),
            int(extents[0].get("cy", "")),
        )
    except ValueError:
        return None


def _group_transform(
    group: etree._Element,
) -> tuple[int, int, int, int, int, int, int, int] | None:
    transforms = group.xpath("./p:grpSpPr/a:xfrm", namespaces=NS)
    if not transforms:
        return None
    transform = transforms[0]
    off = transform.xpath("./a:off", namespaces=NS)
    ext = transform.xpath("./a:ext", namespaces=NS)
    child_off = transform.xpath("./a:chOff", namespaces=NS)
    child_ext = transform.xpath("./a:chExt", namespaces=NS)
    if not off or not ext or not child_off or not child_ext:
        return None
    try:
        return (
            int(off[0].get("x", "")),
            int(off[0].get("y", "")),
            int(ext[0].get("cx", "")),
            int(ext[0].get("cy", "")),
            int(child_off[0].get("x", "")),
            int(child_off[0].get("y", "")),
            int(child_ext[0].get("cx", "")),
            int(child_ext[0].get("cy", "")),
        )
    except ValueError:
        return None


def _placeholder_bbox(
    package: zipfile.ZipFile,
    slide_part: str,
    shape: etree._Element,
    cache: dict[str, etree._ElementTree],
) -> tuple[int, int, int, int] | None:
    placeholders = shape.xpath(
        "./p:nvSpPr/p:nvPr/p:ph",
        namespaces=NS,
    )
    if not placeholders:
        return None
    placeholder = placeholders[0]
    placeholder_index = placeholder.get("idx")
    placeholder_type = placeholder.get("type")
    layout_part = _related_part(
        package,
        slide_part,
        LAYOUT_RELATIONSHIP_TYPE,
    )
    search_parts = [layout_part] if layout_part else []
    if layout_part:
        master_part = _related_part(
            package,
            layout_part,
            MASTER_RELATIONSHIP_TYPE,
        )
        if master_part:
            search_parts.append(master_part)
    for part_name in search_parts:
        tree = _part_tree(package, part_name, cache)
        candidates = tree.xpath(
            "//p:sp[p:nvSpPr/p:nvPr/p:ph]",
            namespaces=NS,
        )
        ranked: list[tuple[int, etree._Element]] = []
        for candidate in candidates:
            candidate_ph = candidate.xpath(
                "./p:nvSpPr/p:nvPr/p:ph",
                namespaces=NS,
            )[0]
            score = 0
            if (
                placeholder_index is not None
                and candidate_ph.get("idx") == placeholder_index
            ):
                score += 4
            if (
                placeholder_type is not None
                and candidate_ph.get("type") == placeholder_type
            ):
                score += 2
            if placeholder_index is None and placeholder_type is None:
                score = 1
            if score:
                ranked.append((score, candidate))
        for _, candidate in sorted(ranked, key=lambda item: item[0], reverse=True):
            bbox = _local_shape_bbox(candidate)
            if bbox is not None:
                return bbox
    return None


def _related_part(
    package: zipfile.ZipFile,
    part_name: str,
    relationship_type: str,
) -> str | None:
    matches = [
        relationship
        for relationship in _relationships(package, part_name)
        if relationship["type"] == relationship_type
        and relationship["target_mode"] != "External"
    ]
    if not matches:
        return None
    resolved = _resolve_target(part_name, matches[0]["target"])
    return resolved if resolved in package.namelist() else None


def _translatable_text_nodes(
    paragraph: etree._Element,
) -> list[etree._Element]:
    nodes = []
    for node in paragraph.xpath(".//a:t", namespaces=NS):
        if _nearest_paragraph(node) is not paragraph:
            continue
        if any(ancestor.tag == f"{A}fld" for ancestor in node.iterancestors()):
            continue
        nodes.append(node)
    return nodes


def _nearest_paragraph(node: etree._Element) -> etree._Element | None:
    return next(
        (
            ancestor
            for ancestor in node.iterancestors()
            if ancestor.tag == f"{A}p"
        ),
        None,
    )


def _run_style_id(node: etree._Element) -> str:
    parent = node.getparent()
    properties = None
    if parent is not None:
        candidates = parent.xpath("./a:rPr", namespaces=NS)
        if candidates:
            properties = candidates[0]
    serialized = (
        etree.tostring(properties, with_tail=False)
        if properties is not None
        else b""
    )
    return hashlib.sha256(serialized).hexdigest()[:16]


def _body_properties(shape: etree._Element) -> dict[str, Any] | None:
    nodes = shape.xpath("./p:txBody/a:bodyPr", namespaces=NS)
    if not nodes:
        return None
    body = nodes[0]
    autofit = "none"
    normal_autofit = body.find(f"{A}normAutofit")
    if normal_autofit is not None:
        autofit = "normal"
    elif body.find(f"{A}spAutoFit") is not None:
        autofit = "shape"
    elif body.find(f"{A}noAutofit") is not None:
        autofit = "disabled"
    return {
        "autofit": autofit,
        "autofit_font_scale": (
            int(normal_autofit.get("fontScale", "100000"))
            if normal_autofit is not None
            else None
        ),
        "wrap": body.get("wrap", "square"),
        "vertical_anchor": body.get("anchor"),
        "margins_emu": {
            name: int(body.get(name, str(default)))
            for name, default in DEFAULT_MARGIN_EMU.items()
        },
    }


def _font_sizes_pt(shape: etree._Element) -> list[float]:
    values: set[float] = set()
    for node in shape.xpath(
        ".//a:rPr[@sz] | .//a:defRPr[@sz] | .//a:endParaRPr[@sz]",
        namespaces=NS,
    ):
        try:
            values.add(int(node.get("sz", "")) / 100)
        except ValueError:
            continue
    return sorted(values)


def _shape_capacity(
    shape: etree._Element,
    bbox: tuple[int, int, int, int] | None,
    *,
    text: str | None = None,
) -> dict[str, Any] | None:
    if bbox is None or _shape_type(shape) != "shape":
        return None
    body = _body_properties(shape)
    if body is None:
        return None
    _, _, width_emu, height_emu = bbox
    margins = body["margins_emu"]
    usable_width_pt = max(
        1.0,
        (
            width_emu
            - int(margins["lIns"])
            - int(margins["rIns"])
        )
        / EMUS_PER_POINT,
    )
    usable_height_pt = max(
        1.0,
        (
            height_emu
            - int(margins["tIns"])
            - int(margins["bIns"])
        )
        / EMUS_PER_POINT,
    )
    font_sizes = _font_sizes_pt(shape)
    nominal_font_pt = (
        sum(font_sizes) / len(font_sizes) if font_sizes else 18.0
    )
    autofit_scale = (
        float(body["autofit_font_scale"]) / 100_000
        if body["autofit"] == "normal"
        and body["autofit_font_scale"] is not None
        else 1.0
    )
    representative_font_pt = nominal_font_pt * autofit_scale
    line_height_pt = max(1.0, representative_font_pt * 1.20)
    estimated_line_capacity = max(0.01, usable_height_pt / line_height_pt)
    estimated_lines = max(1, math.floor(estimated_line_capacity))
    average_character_width_pt = max(1.0, representative_font_pt * 0.62)
    units_per_line = max(
        1.0,
        usable_width_pt / average_character_width_pt,
    )
    estimated_capacity_chars = max(
        1,
        round(estimated_line_capacity * units_per_line),
    )
    actual_text = (
        text
        if text is not None
        else "\n".join(
            str(node.text or "")
            for node in shape.xpath(".//a:t", namespaces=NS)
        )
    )
    visual_units = _text_visual_units(actual_text)
    explicit_lines = max(1, actual_text.count("\n") + 1)
    line_pressure = explicit_lines / estimated_line_capacity
    load_ratio = max(
        visual_units / estimated_capacity_chars,
        line_pressure,
    )
    return {
        "usable_width_pt": round(usable_width_pt, 4),
        "usable_height_pt": round(usable_height_pt, 4),
        "representative_font_pt": round(representative_font_pt, 4),
        "nominal_font_pt": round(nominal_font_pt, 4),
        "autofit_font_scale": round(autofit_scale, 6),
        "estimated_line_capacity": round(estimated_line_capacity, 6),
        "estimated_lines": estimated_lines,
        "estimated_capacity_chars": estimated_capacity_chars,
        "visual_character_units": round(visual_units, 4),
        "load_ratio": round(load_ratio, 6),
        "autofit": body["autofit"],
        "estimate_only": True,
    }


def _text_visual_units(text: str) -> float:
    units = 0.0
    for character in text:
        if character in "\r\n":
            continue
        if character.isspace():
            units += 0.3
        elif unicodedata.east_asian_width(character) in {"W", "F"}:
            units += 1.0
        else:
            units += 0.55
    return units


def _prompt_leakage_rules(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return [
        name
        for name, required_fragments in PROMPT_LEAKAGE_RULES
        if all(fragment in normalized for fragment in required_fragments)
    ]


def _translation_types() -> tuple[type[Any], type[Any]]:
    try:
        from .models import TextSegment, TranslationBlock
    except (ImportError, AttributeError) as exc:
        raise PptxEngineError(
            "format_translator.models translation types are unavailable"
        ) from exc
    return TranslationBlock, TextSegment


def _stable_block_id(part_name: str, shape_id: str, xpath: str) -> str:
    digest = hashlib.sha256(
        f"{part_name}\0{shape_id}\0{xpath}".encode("utf-8")
    ).hexdigest()[:24]
    return f"pptx-{digest}"


def _required_int(
    node: etree._Element,
    attribute: str,
    label: str,
) -> int:
    value = node.get(attribute)
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise InvalidPptxError(f"{label} is not a valid integer: {value!r}") from exc
    if parsed <= 0:
        raise InvalidPptxError(f"{label} must be positive")
    return parsed


def _new_output_path(
    source: Path,
    output_path: str | os.PathLike[str],
) -> Path:
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".pptx":
        raise UnsafePptxPatchError(
            f"PPTX output path must end with .pptx: {destination}"
        )
    if destination == source:
        raise UnsafePptxPatchError(
            "Output must be a new file; source is never overwritten"
        )
    if destination.exists():
        raise UnsafePptxPatchError(
            f"Refusing to overwrite existing output: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _unique_by_block_id(
    values: Iterable[Any],
    label: str,
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for value in values:
        block_id = str(_field(value, "block_id", "") or "")
        if not block_id:
            raise UnsafePptxPatchError(f"{label} contain an item without block_id")
        if block_id in output:
            raise UnsafePptxPatchError(
                f"{label} contain duplicate block_id: {block_id}"
            )
        output[block_id] = value
    return output


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _block_metadata(block: Any, name: str, default: Any = None) -> Any:
    metadata = _field(block, "metadata", {}) or {}
    return _field(metadata, name, default)


def _linked_translation_targets(
    *,
    package: zipfile.ZipFile,
    modified_parts: dict[str, etree._ElementTree],
    embedded_tree_cache: dict[tuple[str, str], etree._ElementTree],
    block: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    targets: list[dict[str, Any]] = []
    for location in _block_metadata(block, "linked_locations", []) or []:
        storage_kind = str(_field(location, "storage_kind", "xml") or "xml")
        part_name = str(_field(location, "part_name", "") or "")
        inner_part_name = str(
            _field(location, "inner_part_name", "") or ""
        )
        if not part_name or part_name not in package.namelist():
            return [], _linked_location_failure(
                f"linked translation part is missing: {part_name!r}",
                storage_kind=storage_kind,
                part_name=part_name,
                inner_part_name=inner_part_name,
            )
        if storage_kind == "xml":
            if part_name not in modified_parts:
                modified_parts[part_name] = _parse_xml(
                    package.read(part_name),
                    part_name,
                )
            tree = modified_parts[part_name]
        elif storage_kind == "embedded_xlsx":
            if not inner_part_name:
                return [], _linked_location_failure(
                    "linked workbook location has no inner part",
                    storage_kind=storage_kind,
                    part_name=part_name,
                    inner_part_name=inner_part_name,
                )
            try:
                tree = _embedded_part_tree(
                    package,
                    part_name,
                    inner_part_name,
                    embedded_tree_cache,
                )
            except PptxEngineError as exc:
                return [], _linked_location_failure(
                    str(exc),
                    storage_kind=storage_kind,
                    part_name=part_name,
                    inner_part_name=inner_part_name,
                )
        else:
            return [], _linked_location_failure(
                f"unsupported linked storage kind: {storage_kind!r}",
                storage_kind=storage_kind,
                part_name=part_name,
                inner_part_name=inner_part_name,
            )
        nodes, reason = _location_text_nodes(tree, location)
        if reason:
            return [], _linked_location_failure(
                reason,
                storage_kind=storage_kind,
                part_name=part_name,
                inner_part_name=inner_part_name,
            )
        targets.append(
            {
                "storage_kind": storage_kind,
                "part_name": part_name,
                "inner_part_name": inner_part_name,
                "nodes": nodes,
                "source_segments": [str(node.text or "") for node in nodes],
            }
        )
    return targets, None


def _linked_location_failure(
    cause: str,
    *,
    storage_kind: str,
    part_name: str,
    inner_part_name: str,
) -> dict[str, Any]:
    reason_code, separator, value = cause.partition(":")
    details: dict[str, Any] = {
        "code": "PPTX_LINKED_LOCATION_INVALID",
        "storage_kind": storage_kind,
        "part_name": part_name,
        "inner_part_name": inner_part_name,
    }
    message = "Linked translation location could not be resolved."
    if reason_code == "translation_xpath_unknown_namespace_prefix":
        details["code"] = (
            "PPTX_LINKED_LOCATION_XPATH_UNKNOWN_NAMESPACE_PREFIX"
        )
        details["prefix"] = value.strip() if separator else ""
        message = (
            "Linked translation location XPath uses an unknown namespace "
            "prefix."
        )
    elif reason_code == "translation_xpath_conflicting_namespace_prefix":
        details["code"] = (
            "PPTX_LINKED_LOCATION_XPATH_CONFLICTING_NAMESPACE_PREFIX"
        )
        details["prefix"] = value.strip() if separator else ""
        message = (
            "Linked translation location XPath uses a conflicting namespace "
            "prefix."
        )
    else:
        details["cause"] = cause
    return {"message": message, "details": details}


def _embedded_part_tree(
    package: zipfile.ZipFile,
    outer_part: str,
    inner_part: str,
    cache: dict[tuple[str, str], etree._ElementTree],
) -> etree._ElementTree:
    cache_key = (outer_part, inner_part)
    if cache_key in cache:
        return cache[cache_key]
    try:
        with zipfile.ZipFile(io.BytesIO(package.read(outer_part))) as embedded:
            if inner_part not in embedded.namelist():
                raise InvalidPptxError(
                    f"embedded workbook part is missing: {outer_part}!{inner_part}"
                )
            tree = _parse_xml(
                embedded.read(inner_part),
                f"{outer_part}!{inner_part}",
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise InvalidPptxError(
            f"embedded chart workbook is not a readable ZIP: {outer_part}"
        ) from exc
    cache[cache_key] = tree
    return tree


def _location_text_nodes(
    tree: etree._ElementTree,
    location: Any,
) -> tuple[list[etree._Element], str | None]:
    container_xpath = str(
        _field(
            location,
            "container_xpath",
            _field(location, "paragraph_xpath", ""),
        )
        or ""
    )
    expected_paths = list(_field(location, "text_node_xpaths", []) or [])
    namespaces, reason = _stored_xpath_namespaces(
        tree,
        [container_xpath, *(str(path) for path in expected_paths)],
    )
    if reason:
        return [], reason
    containers, reason = _stored_xpath_matches(
        tree,
        container_xpath,
        namespaces,
    )
    if reason:
        return [], reason
    if len(containers) != 1 or not isinstance(containers[0], etree._Element):
        return [], "translation container XPath did not resolve uniquely"
    container = containers[0]
    if not expected_paths:
        return [], "translation location contains no text nodes"
    nodes: list[etree._Element] = []
    allowed_tags = {f"{A}t", f"{C}v", f"{X}t", f"{X}v"}
    for node_xpath in expected_paths:
        matches, reason = _stored_xpath_matches(
            tree,
            str(node_xpath),
            namespaces,
        )
        if reason:
            return [], reason
        if len(matches) != 1 or not isinstance(matches[0], etree._Element):
            return [], "text-node XPath did not resolve uniquely"
        node = matches[0]
        if node.tag not in allowed_tags:
            return [], "translation location resolved to an unsafe node type"
        if node is not container and container not in node.iterancestors():
            return [], "translation text node escaped its declared container"
        nodes.append(node)
    expected_source = _field(location, "source_text", None)
    actual_source = "".join(str(node.text or "") for node in nodes)
    if expected_source is not None and actual_source != str(expected_source):
        return [], "linked text changed since extraction"
    return nodes, None


def _stored_xpath_namespaces(
    tree: etree._ElementTree,
    xpaths: Sequence[str],
) -> tuple[dict[str, str], str | None]:
    used_prefixes = sorted(
        {
            match.group(1)
            for xpath in xpaths
            for match in _XPATH_QNAME_PREFIX.finditer(xpath)
        }
    )
    declarations: dict[str, set[str]] = {}
    for element in tree.iter():
        for prefix, namespace in element.nsmap.items():
            if prefix is None or not namespace:
                continue
            declarations.setdefault(str(prefix), set()).add(str(namespace))

    namespaces: dict[str, str] = {}
    for prefix in used_prefixes:
        declared = sorted(declarations.get(prefix, set()))
        if len(declared) > 1:
            return (
                {},
                "translation_xpath_conflicting_namespace_prefix: " + prefix,
            )
        if declared:
            namespaces[prefix] = declared[0]
            continue
        if prefix == "xml":
            namespaces[prefix] = XML_NS
            continue
        if prefix in NS:
            namespaces[prefix] = NS[prefix]
            continue
        return {}, "translation_xpath_unknown_namespace_prefix: " + prefix
    return namespaces, None


def _stored_xpath_matches(
    tree: etree._ElementTree,
    xpath: str,
    namespaces: Mapping[str, str],
) -> tuple[list[Any], str | None]:
    try:
        matches = tree.xpath(xpath, namespaces=namespaces)
    except etree.XPathError as exc:
        return [], f"translation_xpath_invalid: {type(exc).__name__}"
    if not isinstance(matches, list):
        return [], "translation_xpath_did_not_return_nodes"
    return matches, None


def _result_translation_text(block: Any, result: Any) -> str | None:
    segments = _normalized_segments(_field(result, "segments", None))
    if segments:
        expected_ids = [
            str(_field(segment, "segment_id", f"t{index}"))
            for index, segment in enumerate(_field(block, "segments", []) or [])
        ]
        if set(segments) != set(expected_ids):
            return None
        return "".join(segments[segment_id] for segment_id in expected_ids)
    translated_text = _field(result, "translated_text", None)
    if translated_text is None:
        translated_text = _field(result, "text", None)
    return None if translated_text is None else str(translated_text)


def _result_target_language(result: Any) -> str | None:
    metadata = _field(result, "metadata", {}) or {}
    language = str(_field(metadata, "target_language", "") or "").strip()
    if not language:
        return None
    return TARGET_LANGUAGE_TAGS.get(language.casefold())


def _write_linked_translation(
    target: Mapping[str, Any],
    translated_text: str,
    *,
    target_language: str | None,
) -> None:
    nodes = list(target["nodes"])
    source_segments = list(target["source_segments"])
    if len(nodes) == 1:
        _set_text(nodes[0], translated_text)
    else:
        _write_proportional_segments(nodes, translated_text, source_segments)
    _set_drawing_text_language(nodes, target_language)


def _set_drawing_text_language(
    nodes: Sequence[etree._Element],
    language: str | None,
) -> None:
    if language is None:
        return
    paragraphs: set[etree._Element] = set()
    for node in nodes:
        if node.tag != f"{A}t":
            continue
        parent = node.getparent()
        if parent is None or parent.tag != f"{A}r":
            continue
        run_properties = parent.find(f"{A}rPr")
        if run_properties is None:
            run_properties = etree.Element(f"{A}rPr")
            parent.insert(0, run_properties)
        run_properties.set("lang", language)
        paragraph = _nearest_paragraph(node)
        if paragraph is not None:
            paragraphs.add(paragraph)
    for paragraph in paragraphs:
        for end_properties in paragraph.xpath(
            "./a:endParaRPr",
            namespaces=NS,
        ):
            end_properties.set("lang", language)


def _patch_translation_block(
    tree: etree._ElementTree,
    block: Any,
    result: Any,
    *,
    strict: bool,
) -> str | None:
    container_xpath = str(
        _block_metadata(
            block,
            "container_xpath",
            _block_metadata(block, "paragraph_xpath", ""),
        )
        or ""
    )
    namespaces, xpath_reason = _stored_xpath_namespaces(
        tree,
        [container_xpath],
    )
    if xpath_reason:
        return xpath_reason
    matches, xpath_reason = _stored_xpath_matches(
        tree,
        container_xpath,
        namespaces,
    )
    if xpath_reason:
        return xpath_reason
    if len(matches) != 1:
        return "translation container XPath did not resolve uniquely"
    container = matches[0]
    block_kind = str(_block_metadata(block, "block_kind", "paragraph"))
    if block_kind == "paragraph":
        nodes = _translatable_text_nodes(container)
    else:
        nodes, reason = _location_text_nodes(
            tree,
            {
                "container_xpath": container_xpath,
                "text_node_xpaths": _block_metadata(
                    block,
                    "text_node_xpaths",
                    [],
                ),
                "source_text": _field(block, "source_text", None),
            },
        )
        if reason:
            return reason
    expected_paths = list(_block_metadata(block, "text_node_xpaths", []) or [])
    if expected_paths and [tree.getpath(node) for node in nodes] != expected_paths:
        return "text-node locations changed since extraction"
    segments = _normalized_segments(_field(result, "segments", None))
    expected_ids = [
        str(_field(segment, "segment_id", f"t{index}"))
        for index, segment in enumerate(_field(block, "segments", []) or [])
    ]
    if segments:
        if len(nodes) != len(expected_ids):
            return "text-node count changed since extraction"
        if set(segments) != set(expected_ids):
            return "translation segments do not match extracted run ids"
        for node, segment_id in zip(nodes, expected_ids):
            _set_text(node, segments[segment_id])
        _set_drawing_text_language(
            nodes,
            _result_target_language(result),
        )
        return None

    translated_text = _field(result, "translated_text", None)
    if translated_text is None:
        translated_text = _field(result, "text", None)
    if translated_text is None:
        return "translation result has no text"
    if str(_field(block, "protocol", "plain")) == "tagged" and strict:
        return "tagged PPTX text requires one translated segment per a:t run"
    source_segments = [
        str(_field(segment, "text", ""))
        for segment in (_field(block, "segments", []) or [])
    ]
    if len(nodes) == 1:
        _set_text(nodes[0], str(translated_text))
    elif len(nodes) == len(source_segments):
        _write_proportional_segments(
            nodes,
            str(translated_text),
            source_segments,
        )
    else:
        return "text-node count changed since extraction"
    _set_drawing_text_language(
        nodes,
        _result_target_language(result),
    )
    return None


def _normalized_segments(segments: Any) -> dict[str, str]:
    if not segments:
        return {}
    if isinstance(segments, Mapping):
        return {str(key): str(value) for key, value in segments.items()}
    output: dict[str, str] = {}
    for index, segment in enumerate(segments):
        if isinstance(segment, str):
            output[f"t{index}"] = segment
            continue
        segment_id = _field(
            segment,
            "segment_id",
            _field(segment, "id", _field(segment, "tag", None)),
        )
        text = _field(
            segment,
            "text",
            _field(segment, "translated_text", None),
        )
        if segment_id is None or text is None or str(segment_id) in output:
            return {}
        output[str(segment_id)] = str(text)
    return output


def _write_proportional_segments(
    nodes: Sequence[etree._Element],
    translated_text: str,
    source_segments: Sequence[str],
) -> None:
    weights = [max(1, len(segment)) for segment in source_segments]
    total = sum(weights)
    cursor = 0
    cumulative = 0
    for index, (node, weight) in enumerate(zip(nodes, weights)):
        cumulative += weight
        boundary = (
            len(translated_text)
            if index == len(nodes) - 1
            else round(len(translated_text) * cumulative / total)
        )
        _set_text(node, translated_text[cursor:boundary])
        cursor = boundary


def _set_text(node: etree._Element, value: str) -> None:
    node.text = value
    if value[:1].isspace() or value[-1:].isspace():
        node.set(XML_SPACE, "preserve")
    else:
        node.attrib.pop(XML_SPACE, None)


def _rewrite_embedded_package(
    source_data: bytes,
    modified_parts: Mapping[str, etree._ElementTree],
    outer_part_name: str,
) -> bytes:
    try:
        source_stream = io.BytesIO(source_data)
        with zipfile.ZipFile(source_stream) as source:
            unknown_parts = sorted(
                set(modified_parts) - set(source.namelist())
            )
            if unknown_parts:
                raise UnsafePptxPatchError(
                    "attempted to modify unknown embedded workbook parts: "
                    + ", ".join(unknown_parts)
                )
            output_stream = io.BytesIO()
            with zipfile.ZipFile(output_stream, "w") as output:
                output.comment = source.comment
                for info in source.infolist():
                    if info.filename in modified_parts:
                        data = etree.tostring(
                            modified_parts[info.filename],
                            xml_declaration=True,
                            encoding="UTF-8",
                            standalone=True,
                        )
                    else:
                        data = source.read(info.filename)
                    output.writestr(info, data)
            return output_stream.getvalue()
    except (OSError, zipfile.BadZipFile) as exc:
        raise InvalidPptxError(
            f"embedded chart workbook is not a readable ZIP: {outer_part_name}"
        ) from exc


def _write_package(
    package: zipfile.ZipFile,
    destination: Path,
    modified_parts: Mapping[str, etree._ElementTree],
    *,
    modified_binary_parts: Mapping[str, bytes] | None = None,
    baseline_snapshot: Mapping[str, Any],
    authorized_coordinate_changes: Mapping[
        str, Sequence[int]
    ] | None = None,
) -> None:
    binary_parts = modified_binary_parts or {}
    overlap = sorted(set(modified_parts) & set(binary_parts))
    if overlap:
        raise UnsafePptxPatchError(
            "a package part cannot be modified as both XML and binary: "
            + ", ".join(overlap)
        )
    unknown_parts = sorted(
        (set(modified_parts) | set(binary_parts)) - set(package.namelist())
    )
    if unknown_parts:
        raise UnsafePptxPatchError(
            "attempted to modify unknown package parts: "
            + ", ".join(unknown_parts)
        )
    handle, temporary_name = tempfile.mkstemp(
        suffix=".pptx",
        prefix=f".{destination.stem}-",
        dir=destination.parent,
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as output:
            output.comment = package.comment
            for info in package.infolist():
                if info.filename in modified_parts:
                    data = etree.tostring(
                        modified_parts[info.filename],
                        xml_declaration=True,
                        encoding="UTF-8",
                        standalone=True,
                    )
                elif info.filename in binary_parts:
                    data = binary_parts[info.filename]
                else:
                    data = package.read(info.filename)
                output.writestr(info, data)
        validation = validate_pptx(
            temporary,
            baseline_snapshot,
            authorized_coordinate_changes=authorized_coordinate_changes,
        )
        if not validation["valid"]:
            raise InvalidPptxError(
                "Patched PPTX failed validation: "
                + "; ".join(validation["errors"])
            )
        if destination.exists():
            raise UnsafePptxPatchError(
                f"Refusing concurrent overwrite of existing output: "
                f"{destination}"
            )
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise UnsafePptxPatchError(
                f"Refusing concurrent overwrite of existing output: "
                f"{destination}"
            ) from exc
        except OSError as exc:
            raise UnsafePptxPatchError(
                "Could not atomically publish PPTX with no-overwrite "
                f"semantics: {destination}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _baseline_errors(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    authorized_coordinate_changes: Mapping[
        str, Sequence[int]
    ] | None = None,
) -> list[str]:
    errors: list[str] = []
    if set(source.get("package_entries", [])) != set(
        candidate.get("package_entries", [])
    ):
        errors.append("package entry set changed")
    if source.get("slide_size_emu") != candidate.get("slide_size_emu"):
        errors.append("slide canvas size changed")
    if source.get("slide_count") != candidate.get("slide_count"):
        errors.append("slide count changed")
    if source.get("media_hashes") != candidate.get("media_hashes"):
        errors.append("embedded media content changed")
    if source.get("relationships") != candidate.get("relationships"):
        errors.append("OOXML relationships changed")
    source_slides = {
        int(slide["number"]): slide for slide in source.get("slides", [])
    }
    candidate_slides = {
        int(slide["number"]): slide for slide in candidate.get("slides", [])
    }
    for slide_number, source_slide in source_slides.items():
        candidate_slide = candidate_slides.get(slide_number)
        if candidate_slide is None:
            errors.append(f"slide {slide_number} was removed")
            continue
        if source_slide.get("shape_count") != candidate_slide.get("shape_count"):
            errors.append(f"shape count changed on slide {slide_number}")
        source_shapes = _shape_snapshot_map(source_slide)
        candidate_shapes = _shape_snapshot_map(candidate_slide)
        if set(source_shapes) != set(candidate_shapes):
            errors.append(f"shape ids changed on slide {slide_number}")
            continue
        for shape_id, source_shape in source_shapes.items():
            candidate_shape = candidate_shapes[shape_id]
            if source_shape.get("shape_type") != candidate_shape.get("shape_type"):
                errors.append(
                    f"shape type changed on slide {slide_number}: {shape_id}"
                )
            if source_shape.get("bbox_emu") != candidate_shape.get("bbox_emu"):
                authorization_key = _coordinate_change_key(
                    slide_number,
                    shape_id,
                )
                authorized_bbox = (
                    authorized_coordinate_changes.get(authorization_key)
                    if authorized_coordinate_changes
                    else None
                )
                if authorized_bbox is None or not _bbox_equal(
                    authorized_bbox,
                    candidate_shape.get("bbox_emu"),
                    0,
                ):
                    errors.append(
                        f"shape coordinates changed on slide {slide_number}: "
                        f"{shape_id}"
                    )
            if source_shape.get("text_block_count") != candidate_shape.get(
                "text_block_count"
            ):
                errors.append(
                    f"text paragraph count changed on slide {slide_number}: "
                    f"{shape_id}"
                )
    return errors


def _skipped_message(label: str, skipped: Sequence[Mapping[str, Any]]) -> str:
    detail = "; ".join(
        f"{item.get('block_id', item.get('request_index', '?'))}: "
        f"{item.get('reason', 'unknown error')}"
        for item in skipped[:5]
    )
    if len(skipped) > 5:
        detail += f"; and {len(skipped) - 5} more"
    return f"Unsafe {label} rejected: {detail}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _shape_snapshot_map(
    slide: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(shape["shape_id"]): shape
        for shape in slide.get("shapes", [])
    }


def _bbox_equal(
    left: Sequence[int] | None,
    right: Sequence[int] | None,
    tolerance: int,
) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return len(left) == len(right) == 4 and all(
        abs(int(left_value) - int(right_value)) <= tolerance
        for left_value, right_value in zip(left, right)
    )


def _bbox_outside(
    bbox: Sequence[int],
    slide_width: int,
    slide_height: int,
    tolerance: int,
) -> bool:
    x, y, width, height = (int(value) for value in bbox)
    return (
        x < -tolerance
        or y < -tolerance
        or x + width > slide_width + tolerance
        or y + height > slide_height + tolerance
    )


def _shape_overlaps(
    slide: Mapping[str, Any],
    threshold: float,
) -> dict[tuple[str, str], dict[str, Any]]:
    shapes = [
        shape
        for shape in slide.get("shapes", [])
        if shape.get("text") and shape.get("bbox_emu") is not None
    ]
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for left_index in range(len(shapes)):
        left = shapes[left_index]
        left_bbox = left["bbox_emu"]
        left_area = _bbox_area(left_bbox)
        if left_area <= 0:
            continue
        for right_index in range(left_index + 1, len(shapes)):
            right = shapes[right_index]
            right_bbox = right["bbox_emu"]
            right_area = _bbox_area(right_bbox)
            if right_area <= 0:
                continue
            intersection = _bbox_intersection_area(left_bbox, right_bbox)
            if intersection <= 0:
                continue
            ratio = intersection / min(left_area, right_area)
            if ratio < threshold:
                continue
            left_id = str(left["shape_id"])
            right_id = str(right["shape_id"])
            key = tuple(sorted((left_id, right_id)))
            output[key] = {
                "left_shape_id": left_id,
                "left_shape_name": left.get("name"),
                "left_bbox_emu": left_bbox,
                "right_shape_id": right_id,
                "right_shape_name": right.get("name"),
                "right_bbox_emu": right_bbox,
                "intersection_ratio": round(ratio, 6),
                "intersection_area_emu2": intersection,
                "threshold": threshold,
            }
    return output


def _bbox_area(bbox: Sequence[int]) -> int:
    _, _, width, height = (int(value) for value in bbox)
    return max(0, width) * max(0, height)


def _bbox_intersection_area(
    left: Sequence[int],
    right: Sequence[int],
) -> int:
    left_x, left_y, left_width, left_height = (
        int(value) for value in left
    )
    right_x, right_y, right_width, right_height = (
        int(value) for value in right
    )
    x0 = max(left_x, right_x)
    y0 = max(left_y, right_y)
    x1 = min(left_x + left_width, right_x + right_width)
    y1 = min(left_y + left_height, right_y + right_height)
    return max(0, x1 - x0) * max(0, y1 - y0)


def _visible_character_count(text: str) -> int:
    return sum(not character.isspace() for character in text)


def _suspicious_character_counts(
    text: str,
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    tofu = {"\u25a0", "\u25a1", "\u25ad"}
    safe_private_bullets = {"\uf0a7", "\uf0b7", "\uf0d8", "\uf0fc"}
    for index, character in enumerate(text):
        codepoint = ord(character)
        category = unicodedata.category(character)
        reason: str | None = None
        if character == "\ufffd":
            reason = "replacement_character"
        elif category == "Cs" or (
            category == "Co" and character not in safe_private_bullets
        ):
            reason = "surrogate_or_private_use"
        elif category == "Cc" and character not in {"\t", "\n", "\r", "\f"}:
            reason = "unexpected_control"
        elif _unicode_noncharacter(codepoint):
            reason = "unicode_noncharacter"
        elif character in tofu and (
            (index > 0 and text[index - 1] in tofu)
            or (index + 1 < len(text) and text[index + 1] in tofu)
        ):
            reason = "repeated_missing_glyph_box"
        if reason is not None:
            key = (f"U+{codepoint:04X}", reason)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _new_suspicious_characters(
    source: str,
    candidate: str,
) -> list[dict[str, Any]]:
    source_counts = _suspicious_character_counts(source)
    candidate_counts = _suspicious_character_counts(candidate)
    output = []
    for (codepoint, reason), count in sorted(candidate_counts.items()):
        difference = count - source_counts.get((codepoint, reason), 0)
        if difference > 0:
            output.append(
                {
                    "codepoint": codepoint,
                    "reason": reason,
                    "count": difference,
                    "source_count": source_counts.get((codepoint, reason), 0),
                    "candidate_count": count,
                }
            )
    return output


def _unicode_noncharacter(codepoint: int) -> bool:
    return 0xFDD0 <= codepoint <= 0xFDEF or (codepoint & 0xFFFF) in {
        0xFFFE,
        0xFFFF,
    }


def _mapping_difference(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    source_keys = set(source)
    candidate_keys = set(candidate)
    return {
        "removed": sorted(source_keys - candidate_keys),
        "added": sorted(candidate_keys - source_keys),
        "changed": sorted(
            key
            for key in source_keys & candidate_keys
            if source[key] != candidate[key]
        ),
    }


def _qa_issue(
    *,
    code: str,
    message: str,
    severity: str,
    slide: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> Any:
    from .models import QaIssue

    normalized_details = dict(details or {})
    normalized_details.setdefault(
        "repair_hint",
        _pptx_repair_hint(
            code,
            slide,
            str(normalized_details.get("shape_id", "")) or None,
        ),
    )
    return QaIssue(
        code=code,
        message=message,
        severity=severity,
        stage="pptx_qa",
        slide=slide,
        recoverable=code
        not in {
            "PPTX_MEDIA_CHANGED",
            "PPTX_RELATIONSHIPS_CHANGED",
            "PPTX_SLIDE_COUNT_MISMATCH",
        },
        details=normalized_details,
    )


def _pptx_repair_hint(
    code: str,
    slide: int | None,
    shape_id: str | None,
) -> dict[str, Any]:
    action_by_code = {
        "PPTX_TEXT_CAPACITY_RISK": [
            "resolve_text_overflow",
            "set_autofit",
            "adjust_margins",
            "adjust_font_size",
            "set_font_size",
            "set_shape_bounds",
            "replace_translation",
        ],
        "PPTX_TRANSLATION_EXPANSION_RISK": [
            "resolve_text_overflow",
            "set_autofit",
            "adjust_margins",
            "adjust_font_size",
            "set_font_size",
            "set_shape_bounds",
            "replace_translation",
        ],
        "PPTX_SHAPE_COORDINATE_CHANGED": [
            "restore_shape_coordinates",
        ],
        "PPTX_SHAPE_OUT_OF_BOUNDS": [
            "restore_shape_coordinates",
            "set_shape_bounds",
        ],
        "PPTX_SUSPECTED_SHAPE_OVERLAP": [
            "set_shape_bounds",
        ],
        "PPTX_NEW_GARBLED_TEXT": ["replace_translation"],
        "PPTX_TRANSLATION_PROMPT_LEAKAGE": ["replace_translation"],
    }
    actions = action_by_code.get(code, [])
    preferred_action = actions[0] if actions else None
    return {
        "allowed_actions": actions,
        "priority_order": actions,
        "preferred_action": preferred_action,
        "constraints": {
            "set_shape_bounds": {
                "requires_explicit_bounds": True,
                "clamp_to_slide": code == "PPTX_SHAPE_OUT_OF_BOUNDS",
                "must_not_introduce_collision": True,
            }
        }
        if "set_shape_bounds" in actions
        else {},
        "slide": slide,
        "shape_id": shape_id,
        "automatic": False,
        "reason": (
            "Recommendation only; apply through execute_pptx_repairs to create "
            "a separately audited PPTX."
        ),
    }


def _deduplicate_qa_issues(issues: Sequence[Any]) -> list[Any]:
    output = []
    seen: set[tuple[str, int | None, str]] = set()
    for issue in issues:
        key = (str(issue.code), issue.slide, str(issue.message))
        if key in seen:
            continue
        seen.add(key)
        output.append(issue)
    return output


def _repair_shape_formatting(
    package: zipfile.ZipFile,
    modified_parts: dict[str, etree._ElementTree],
    slide_parts: Sequence[str],
    action: Mapping[str, Any],
    *,
    baseline_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        slide_number = int(action.get("slide", 0))
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "repair action slide must be a one-based integer"
        ) from exc
    if slide_number < 1 or slide_number > len(slide_parts):
        raise UnsupportedPptxRepairError(
            f"repair slide is outside 1..{len(slide_parts)}: {slide_number}"
        )
    shape_id = str(action.get("shape_id", "") or "")
    if not shape_id:
        raise UnsupportedPptxRepairError("repair action requires shape_id")
    part_name = slide_parts[slide_number - 1]
    if part_name not in modified_parts:
        modified_parts[part_name] = _parse_xml(
            package.read(part_name),
            part_name,
        )
    tree = modified_parts[part_name]
    shape = _shape_by_id(tree, shape_id)
    if shape is None:
        raise UnsupportedPptxRepairError(
            f"shape {shape_id} does not exist on slide {slide_number}"
        )
    action_name = str(action["action"])
    if action_name in {
        "set_shape_bounds",
        "adjust_shape_geometry",
        "restore_shape_coordinates",
    }:
        return _repair_shape_geometry(
            package=package,
            tree=tree,
            shape=shape,
            slide_number=slide_number,
            shape_id=shape_id,
            part_name=part_name,
            action=action,
            baseline_snapshot=baseline_snapshot,
        )
    if action_name == "restore_table_row_heights":
        return _repair_table_row_heights(
            shape=shape,
            slide_number=slide_number,
            shape_id=shape_id,
            part_name=part_name,
            action=action,
        )

    body_nodes = shape.xpath("./p:txBody/a:bodyPr", namespaces=NS)
    if not body_nodes:
        raise UnsupportedPptxRepairError(
            f"shape {shape_id} has no editable text body"
        )
    body = body_nodes[0]
    if action_name == "set_autofit":
        _validate_autofit_mode(action)
        before = _body_properties(shape)
        autofit_audit = _set_safe_normal_autofit(shape, body, action)
        return {
            "slide": slide_number,
            "shape_id": shape_id,
            "part_name": part_name,
            **autofit_audit,
            "before": before,
            "after": _body_properties(shape),
        }
    if action_name == "adjust_margins":
        if action.get("margins_emu") is not None:
            before, after = _set_explicit_body_margins(
                body,
                action["margins_emu"],
            )
            scale = None
        else:
            scale, before, after = _scale_body_margins(
                body,
                action.get("scale", 0.75),
            )
        return {
            "slide": slide_number,
            "shape_id": shape_id,
            "part_name": part_name,
            "scale": scale,
            "before": {"margins_emu": before},
            "after": {"margins_emu": after},
        }
    if action_name in {"adjust_font_size", "set_font_size"}:
        before_font_sizes = _font_sizes_pt(shape)
        changes, font_audit = _apply_font_size_action(
            tree,
            shape,
            action,
            action_name=action_name,
        )
        return {
            "slide": slide_number,
            "shape_id": shape_id,
            "part_name": part_name,
            **font_audit,
            "before": {"font_sizes_pt": before_font_sizes},
            "after": {"font_sizes_pt": _font_sizes_pt(shape)},
            "changes": changes,
        }
    if action_name == "resolve_text_overflow":
        return _resolve_text_overflow(
            package=package,
            tree=tree,
            shape=shape,
            body=body,
            slide_number=slide_number,
            shape_id=shape_id,
            part_name=part_name,
            action=action,
            baseline_snapshot=baseline_snapshot,
        )
    raise UnsupportedPptxRepairError(
        f"formatting repair is unsupported: {action_name}"
    )


def _repair_table_row_heights(
    *,
    shape: etree._Element,
    slide_number: int,
    shape_id: str,
    part_name: str,
    action: Mapping[str, Any],
) -> dict[str, Any]:
    if _shape_type(shape) != "graphic_frame":
        raise UnsupportedPptxRepairError(
            "restore_table_row_heights requires a graphicFrame table"
        )
    tables = shape.xpath(
        "./a:graphic/a:graphicData/a:tbl",
        namespaces=NS,
    )
    if len(tables) != 1:
        raise UnsupportedPptxRepairError(
            f"shape {shape_id} is not an editable native PowerPoint table"
        )
    rows = tables[0].xpath("./a:tr", namespaces=NS)
    requested_rows = action.get("rows")
    if (
        not isinstance(requested_rows, Sequence)
        or isinstance(requested_rows, (str, bytes, bytearray))
        or not requested_rows
    ):
        raise UnsupportedPptxRepairError(
            "restore_table_row_heights requires a non-empty rows list"
        )

    frame_bbox = _direct_shape_bbox(shape)
    seen_indices: set[int] = set()
    pending: list[dict[str, Any]] = []
    for request_index, value in enumerate(requested_rows):
        row_request = _model_or_mapping(value, f"rows[{request_index}]")
        row_index_value = row_request.get("row_index")
        if isinstance(row_index_value, bool) or not isinstance(
            row_index_value,
            int,
        ):
            raise UnsupportedPptxRepairError(
                "table row_index must be a one-based integer"
            )
        row_index = row_index_value
        if row_index in seen_indices:
            raise UnsupportedPptxRepairError(
                f"table row_index values must be unique: {row_index}"
            )
        seen_indices.add(row_index)
        if row_index < 1 or row_index > len(rows):
            raise UnsupportedPptxRepairError(
                f"table row_index is outside 1..{len(rows)}: {row_index}"
            )

        height_value = row_request.get("height_pt")
        if isinstance(height_value, bool):
            raise UnsupportedPptxRepairError(
                "table height_pt must be numeric"
            )
        try:
            height_pt = float(height_value)
        except (TypeError, ValueError) as exc:
            raise UnsupportedPptxRepairError(
                "table height_pt must be numeric"
            ) from exc
        if not math.isfinite(height_pt) or not 1.0 <= height_pt <= 1000.0:
            raise UnsupportedPptxRepairError(
                "table height_pt must be within 1..1000 points"
            )
        target_height_emu = round(height_pt * EMUS_PER_POINT)
        current_value = rows[row_index - 1].get("h")
        try:
            current_height_emu = int(str(current_value))
        except (TypeError, ValueError) as exc:
            raise UnsupportedPptxRepairError(
                f"table row {row_index} has no valid current height"
            ) from exc
        if current_height_emu <= 0:
            raise UnsupportedPptxRepairError(
                f"table row {row_index} has no valid current height"
            )
        if target_height_emu < current_height_emu:
            raise UnsupportedPptxRepairError(
                "restore_table_row_heights cannot reduce row "
                f"{row_index} from "
                f"{current_height_emu / EMUS_PER_POINT:.4f}pt to "
                f"{target_height_emu / EMUS_PER_POINT:.4f}pt"
            )
        pending.append(
            {
                "row": rows[row_index - 1],
                "row_index": row_index,
                "before_emu": current_height_emu,
                "after_emu": target_height_emu,
            }
        )

    before_rows = [
        _table_row_height_audit(
            change["row_index"],
            change["before_emu"],
        )
        for change in pending
    ]
    changes: list[dict[str, Any]] = []
    for change in pending:
        change["row"].set("h", str(change["after_emu"]))
        changes.append(
            {
                "row_index": change["row_index"],
                "before_height_emu": change["before_emu"],
                "after_height_emu": change["after_emu"],
                "changed": change["before_emu"] != change["after_emu"],
            }
        )
    after_rows = [
        _table_row_height_audit(
            change["row_index"],
            change["after_emu"],
        )
        for change in pending
    ]
    if _direct_shape_bbox(shape) != frame_bbox:
        raise UnsafePptxPatchError(
            "table row-height repair changed the table frame transform"
        )
    return {
        "slide": slide_number,
        "shape_id": shape_id,
        "shape_type": "table",
        "part_name": part_name,
        "before": {
            "frame_bbox_emu": list(frame_bbox) if frame_bbox else None,
            "rows": before_rows,
        },
        "after": {
            "frame_bbox_emu": list(frame_bbox) if frame_bbox else None,
            "rows": after_rows,
        },
        "changes": changes,
    }


def _table_row_height_audit(
    row_index: int,
    height_emu: int,
) -> dict[str, Any]:
    return {
        "row_index": row_index,
        "height_emu": height_emu,
        "height_pt": round(height_emu / EMUS_PER_POINT, 6),
    }


def _repair_shape_geometry(
    *,
    package: zipfile.ZipFile,
    tree: etree._ElementTree,
    shape: etree._Element,
    slide_number: int,
    shape_id: str,
    part_name: str,
    action: Mapping[str, Any],
    baseline_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    action_name = str(action["action"])
    shape_type = _shape_type(shape)
    if shape_type not in {"shape", "picture", "graphic_frame"}:
        raise UnsupportedPptxRepairError(
            f"{action_name} does not support shape type {shape_type}"
        )
    _assert_direct_geometry_safe(shape, action_name)
    before = _direct_shape_bbox(shape)
    if before is None:
        raise UnsupportedPptxRepairError(
            f"shape {shape_id} has no direct editable transform"
        )
    slide_size = baseline_snapshot["slide_size_emu"]
    slide_width = int(slide_size["width"])
    slide_height = int(slide_size["height"])

    if action_name == "restore_shape_coordinates":
        reference_value = action.get("source_pptx")
        if not reference_value:
            raise UnsupportedPptxRepairError(
                "restore_shape_coordinates requires a valid source_pptx; "
                "explicit bounds are not restoration evidence"
            )
        try:
            reference_path_obj = _validated_path(str(reference_value))
        except InvalidPptxError as exc:
            raise UnsupportedPptxRepairError(
                "restore_shape_coordinates source_pptx is not a valid PPTX"
            ) from exc
        reference = structure_snapshot(reference_path_obj)
        reference_slide = next(
            (
                slide
                for slide in reference["slides"]
                if int(slide["number"]) == slide_number
            ),
            None,
        )
        if reference_slide is None:
            raise UnsupportedPptxRepairError(
                f"source PPTX has no slide {slide_number}"
            )
        reference_shape = _shape_snapshot_map(reference_slide).get(shape_id)
        if reference_shape is None:
            raise UnsupportedPptxRepairError(
                f"source PPTX has no shape {shape_id} on slide "
                f"{slide_number}"
            )
        if reference_shape.get("shape_type") != shape_type:
            raise UnsupportedPptxRepairError(
                "source and candidate shape types do not match"
            )
        reference_bbox = reference_shape.get("bbox_emu")
        if reference_bbox is None:
            raise UnsupportedPptxRepairError(
                "source shape has no restorable coordinates"
            )
        requested = tuple(int(value) for value in reference_bbox)
        reference_path = str(reference_path_obj)
        reference_sha256 = _file_sha256(reference_path_obj)
        restoration_evidence = "source_pptx"
        clamp_to_slide = False
    else:
        requested = _requested_shape_bounds(action)
        clamp_to_slide = _action_bool(action, "clamp_to_slide", False)
        reference_path = None
        reference_sha256 = None
        restoration_evidence = None

    after, clamped, limits = _validated_shape_bounds(
        requested,
        slide_width=slide_width,
        slide_height=slide_height,
        clamp_to_slide=clamp_to_slide,
        min_width_emu=action.get("min_width_emu", DEFAULT_MIN_SHAPE_EMU),
        min_height_emu=action.get("min_height_emu", DEFAULT_MIN_SHAPE_EMU),
    )
    if not _geometry_change_is_safe(
        package=package,
        tree=tree,
        part_name=part_name,
        target_shape=shape,
        original=before,
        candidate=after,
    ):
        raise UnsupportedPptxRepairError(
            "shape bounds would introduce or increase a collision"
        )
    _set_direct_shape_bbox(shape, after)
    return {
        "slide": slide_number,
        "shape_id": shape_id,
        "shape_type": shape_type,
        "part_name": part_name,
        "before": {"bbox_emu": list(before)},
        "requested": {"bbox_emu": list(requested)},
        "after": {"bbox_emu": list(after)},
        "clamp_to_slide": clamp_to_slide,
        "clamped": clamped,
        "limits": limits,
        "reference_pptx": reference_path,
        "reference_sha256": reference_sha256,
        "restoration_evidence": restoration_evidence,
        "authorized_bbox_emu": list(after),
    }


def _requested_shape_bounds(
    action: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    bounds = action.get("bounds_emu")
    if bounds is not None:
        if (
            not isinstance(bounds, Sequence)
            or isinstance(bounds, (str, bytes))
            or len(bounds) != 4
        ):
            raise UnsupportedPptxRepairError(
                "bounds_emu must contain x, y, width, height"
            )
        values = list(bounds)
    else:
        missing = [
            name
            for name in ("x", "y", "width", "height")
            if name not in action
        ]
        if missing:
            raise UnsupportedPptxRepairError(
                "shape bounds require explicit x, y, width, height; missing "
                + ", ".join(missing)
            )
        values = [
            action["x"],
            action["y"],
            action["width"],
            action["height"],
        ]
    try:
        return tuple(int(value) for value in values)  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "shape bounds must be integer EMU values"
        ) from exc


def _validated_shape_bounds(
    bounds: Sequence[int],
    *,
    slide_width: int,
    slide_height: int,
    clamp_to_slide: bool,
    min_width_emu: Any,
    min_height_emu: Any,
) -> tuple[
    tuple[int, int, int, int],
    bool,
    dict[str, int],
]:
    try:
        x, y, width, height = (int(value) for value in bounds)
        min_width = max(EMUS_PER_POINT, int(min_width_emu))
        min_height = max(EMUS_PER_POINT, int(min_height_emu))
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "shape bounds and minimum dimensions must be integer EMUs"
        ) from exc
    if min_width > slide_width or min_height > slide_height:
        raise UnsupportedPptxRepairError(
            "minimum shape dimensions exceed the slide canvas"
        )
    if width < 0 or height < 0:
        raise UnsupportedPptxRepairError(
            "shape width and height cannot be negative"
        )
    requested = (x, y, width, height)
    if clamp_to_slide:
        width = min(slide_width, max(min_width, width))
        height = min(slide_height, max(min_height, height))
        x = min(slide_width - width, max(0, x))
        y = min(slide_height - height, max(0, y))
    else:
        if x < 0 or y < 0:
            raise UnsupportedPptxRepairError(
                "shape x and y coordinates cannot be negative"
            )
        if width < min_width or height < min_height:
            raise UnsupportedPptxRepairError(
                f"shape dimensions must be at least {min_width} x "
                f"{min_height} EMU"
            )
        if x + width > slide_width or y + height > slide_height:
            raise UnsupportedPptxRepairError(
                "shape bounds extend outside the slide; use clamp_to_slide "
                "to request bounded clamping"
            )
    output = (x, y, width, height)
    return (
        output,
        output != requested,
        {
            "slide_width_emu": slide_width,
            "slide_height_emu": slide_height,
            "min_width_emu": min_width,
            "min_height_emu": min_height,
        },
    )


def _assert_direct_geometry_safe(
    shape: etree._Element,
    action_name: str,
) -> None:
    if any(ancestor.tag == f"{P}grpSp" for ancestor in shape.iterancestors()):
        raise UnsupportedPptxRepairError(
            f"{action_name} rejects shapes nested inside groups"
        )
    if shape.tag == f"{P}grpSp":
        raise UnsupportedPptxRepairError(
            f"{action_name} rejects group shapes"
        )
    placeholders = shape.xpath(
        "./p:nvSpPr/p:nvPr/p:ph | "
        "./p:nvPicPr/p:nvPr/p:ph | "
        "./p:nvGraphicFramePr/p:nvPr/p:ph",
        namespaces=NS,
    )
    if placeholders:
        raise UnsupportedPptxRepairError(
            f"{action_name} rejects placeholder geometry overrides"
        )
    transform = _direct_transform(shape)
    if transform is not None and _transform_has_rotation_or_flip(transform):
        raise UnsupportedPptxRepairError(
            f"{action_name} rejects rotated or flipped target geometry"
        )


def _direct_transform(
    shape: etree._Element,
) -> etree._Element | None:
    shape_type = _shape_type(shape)
    if shape_type == "graphic_frame":
        nodes = shape.xpath("./p:xfrm", namespaces=NS)
    elif shape_type in {"shape", "picture", "connector"}:
        nodes = shape.xpath("./p:spPr/a:xfrm", namespaces=NS)
    else:
        nodes = []
    return nodes[0] if len(nodes) == 1 else None


def _transform_has_rotation_or_flip(transform: etree._Element) -> bool:
    rotation = transform.get("rot")
    if rotation is not None:
        try:
            if int(rotation) % 21_600_000:
                return True
        except ValueError:
            return True
    for attribute in ("flipH", "flipV"):
        value = transform.get(attribute)
        if value is None:
            continue
        if value.strip().casefold() not in {"0", "false", "off", "no"}:
            return True
    return False


def _shape_or_ancestor_group_has_rotation_or_flip(
    shape: etree._Element,
) -> bool:
    transform = _direct_transform(shape)
    if transform is not None and _transform_has_rotation_or_flip(transform):
        return True
    for ancestor in shape.iterancestors(f"{P}grpSp"):
        transforms = ancestor.xpath("./p:grpSpPr/a:xfrm", namespaces=NS)
        if any(
            _transform_has_rotation_or_flip(group_transform)
            for group_transform in transforms
        ):
            return True
    return False


def _direct_shape_bbox(
    shape: etree._Element,
) -> tuple[int, int, int, int] | None:
    transform = _direct_transform(shape)
    if transform is None:
        return None
    offsets = transform.xpath("./a:off", namespaces=NS)
    extents = transform.xpath("./a:ext", namespaces=NS)
    if len(offsets) != 1 or len(extents) != 1:
        return None
    try:
        return (
            int(offsets[0].get("x", "")),
            int(offsets[0].get("y", "")),
            int(extents[0].get("cx", "")),
            int(extents[0].get("cy", "")),
        )
    except ValueError:
        return None


def _set_direct_shape_bbox(
    shape: etree._Element,
    bounds: Sequence[int],
) -> None:
    transform = _direct_transform(shape)
    if transform is None:
        raise UnsupportedPptxRepairError(
            "shape has no direct editable transform"
        )
    offsets = transform.xpath("./a:off", namespaces=NS)
    extents = transform.xpath("./a:ext", namespaces=NS)
    if len(offsets) != 1 or len(extents) != 1:
        raise UnsupportedPptxRepairError(
            "shape transform does not contain exactly one offset and extent"
        )
    x, y, width, height = (int(value) for value in bounds)
    offsets[0].set("x", str(x))
    offsets[0].set("y", str(y))
    extents[0].set("cx", str(width))
    extents[0].set("cy", str(height))


def _geometry_change_is_safe(
    *,
    package: zipfile.ZipFile,
    tree: etree._ElementTree,
    part_name: str,
    target_shape: etree._Element,
    original: Sequence[int],
    candidate: Sequence[int],
) -> bool:
    cache = {part_name: tree}
    other_bounds = []
    for other in _shape_elements(tree):
        if other is target_shape or _shape_type(other) in {"group", "connector"}:
            continue
        if _shape_or_ancestor_group_has_rotation_or_flip(other):
            raise UnsupportedPptxRepairError(
                "geometry repair rejects rotated or flipped collision "
                "obstacles"
            )
        bbox = _shape_bbox(package, part_name, other, cache)
        if bbox is not None:
            other_bounds.append(bbox)
    return _expansion_collision_safe(original, candidate, other_bounds)


def _set_normal_autofit(
    body: etree._Element,
    *,
    font_scale: int,
) -> None:
    for child_name in ("noAutofit", "spAutoFit", "normAutofit"):
        for child in list(body.findall(f"{A}{child_name}")):
            body.remove(child)
    autofit = etree.SubElement(body, f"{A}normAutofit")
    autofit.set("fontScale", str(font_scale))
    autofit.set("lnSpcReduction", "10000")


def _set_safe_normal_autofit(
    shape: etree._Element,
    body: etree._Element,
    action: Mapping[str, Any],
) -> dict[str, Any]:
    size_nodes = shape.xpath(
        ".//a:rPr[@sz] | .//a:defRPr[@sz] | .//a:endParaRPr[@sz]",
        namespaces=NS,
    )
    if not size_nodes:
        raise UnsupportedPptxRepairError(
            "normal autofit requires explicit font sizes so the hard floor "
            "can be verified"
        )
    sizes: list[int] = []
    for node in size_nodes:
        try:
            size = int(node.get("sz", ""))
        except ValueError as exc:
            raise UnsupportedPptxRepairError(
                "shape contains an invalid font size"
            ) from exc
        if size <= 0:
            raise UnsupportedPptxRepairError(
                "shape contains a non-positive font size"
            )
        sizes.append(size)
    role = _shape_text_role(shape)
    floor_pt = _effective_font_floor_pt(action, role, sizes)
    floor_size = math.ceil(floor_pt * 100)
    try:
        requested_scale = int(action.get("autofit_font_scale", 90_000))
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "autofit_font_scale must be an integer"
        ) from exc
    if not 50_000 <= requested_scale <= 100_000:
        raise UnsupportedPptxRepairError(
            "autofit_font_scale must be between 50000 and 100000"
        )
    required_scale = max(
        math.ceil(floor_size * 100_000 / size)
        for size in sizes
    )
    font_scale = max(requested_scale, required_scale)
    if font_scale > 100_000:
        raise UnsupportedPptxRepairError(
            "normal autofit cannot preserve the effective minimum font size "
            f"of {floor_pt:g} pt"
        )
    _set_normal_autofit(body, font_scale=font_scale)
    return {
        "font_scale": font_scale,
        "requested_font_scale": requested_scale,
        "minimum_safe_font_scale": required_scale,
        "effective_min_font_size_pt": min(
            size * font_scale / 10_000_000
            for size in sizes
        ),
        "font_floor_pt": floor_pt,
        "text_role": role,
    }


def _validate_autofit_mode(action: Mapping[str, Any]) -> None:
    mode = action.get("autofit_mode")
    if mode in {None, "shrink_text"}:
        return
    raise UnsupportedPptxRepairError(
        "only shrink_text autofit is permitted; resize_shape would create "
        "an unaudited geometry change"
    )


def _scale_body_margins(
    body: etree._Element,
    scale_value: Any,
) -> tuple[float, dict[str, int], dict[str, int]]:
    try:
        scale = float(scale_value)
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "margin scale must be numeric"
        ) from exc
    if not 0.5 <= scale <= 1.0:
        raise UnsupportedPptxRepairError(
            "margin scale must be between 0.5 and 1.0"
        )
    before = {
        name: int(body.get(name, str(default)))
        for name, default in DEFAULT_MARGIN_EMU.items()
    }
    after = {
        name: max(0, round(value * scale))
        for name, value in before.items()
    }
    for name, value in after.items():
        body.set(name, str(value))
    return scale, before, after


def _set_explicit_body_margins(
    body: etree._Element,
    margins_value: Any,
) -> tuple[dict[str, int], dict[str, int]]:
    margins = _model_or_mapping(margins_value, "margins_emu")
    before = {
        name: int(body.get(name, str(default)))
        for name, default in DEFAULT_MARGIN_EMU.items()
    }
    try:
        after = {name: int(margins[name]) for name in DEFAULT_MARGIN_EMU}
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "margins_emu requires integer lIns, rIns, tIns, bIns"
        ) from exc
    if any(value < 0 for value in after.values()):
        raise UnsupportedPptxRepairError(
            "text box margins cannot be negative"
        )
    if any(after[name] > before[name] for name in DEFAULT_MARGIN_EMU):
        raise UnsupportedPptxRepairError(
            "repair margins may only preserve or reduce existing margins"
        )
    for name, value in after.items():
        body.set(name, str(value))
    return before, after


def _apply_font_size_action(
    tree: etree._ElementTree,
    shape: etree._Element,
    action: Mapping[str, Any],
    *,
    action_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _materialize_text_run_font_sizes(shape)
    size_nodes = shape.xpath(
        ".//a:rPr[@sz] | .//a:defRPr[@sz] | .//a:endParaRPr[@sz]",
        namespaces=NS,
    )
    if not size_nodes:
        raise UnsupportedPptxRepairError(
            "shape has no explicit font sizes to adjust safely"
        )
    original_sizes: list[int] = []
    for node in size_nodes:
        try:
            original_sizes.append(int(node.get("sz", "")))
        except ValueError as exc:
            raise UnsupportedPptxRepairError(
                "shape contains an invalid font size"
            ) from exc
    role = _shape_text_role(shape)
    floor_pt = _effective_font_floor_pt(action, role, original_sizes)
    floor_size = math.ceil(floor_pt * 100)

    absolute_value = action.get(
        "font_size_pt",
        action.get("size_pt"),
    )
    if action_name == "set_font_size" and absolute_value is None:
        raise UnsupportedPptxRepairError(
            "set_font_size requires font_size_pt"
        )
    if absolute_value is not None:
        try:
            requested_size = round(float(absolute_value) * 100)
        except (TypeError, ValueError) as exc:
            raise UnsupportedPptxRepairError(
                "font_size_pt must be numeric"
            ) from exc
        if requested_size <= 0:
            raise UnsupportedPptxRepairError(
                "font_size_pt must be greater than zero"
            )
        mode = "absolute"
        scale = None
    else:
        try:
            scale = float(action.get("scale", 0.90))
        except (TypeError, ValueError) as exc:
            raise UnsupportedPptxRepairError(
                "font scale must be numeric"
            ) from exc
        if not 0.5 <= scale <= 1.0:
            raise UnsupportedPptxRepairError(
                "font scale must be between 0.5 and 1.0"
            )
        requested_size = None
        mode = "scale"

    changes = []
    for node, old_size in zip(size_nodes, original_sizes):
        desired = (
            requested_size
            if requested_size is not None
            else round(old_size * float(scale))
        )
        if requested_size is not None and desired < floor_size:
            raise UnsupportedPptxRepairError(
                f"requested font size {desired / 100:g} pt is below the "
                f"effective minimum {floor_pt:g} pt"
            )
        new_size = max(floor_size, desired)
        node.set("sz", str(new_size))
        changes.append(
            {
                "before_hundredths_pt": old_size,
                "requested_hundredths_pt": desired,
                "after_hundredths_pt": new_size,
                "floor_hundredths_pt": floor_size,
                "clamped_to_floor": new_size != desired,
                "xml_path": tree.getpath(node),
            }
        )
    return (
        changes,
        {
            "mode": mode,
            "scale": scale,
            "requested_font_size_pt": (
                requested_size / 100 if requested_size is not None else None
            ),
            "text_role": role,
            "effective_min_font_size_pt": floor_pt,
        },
    )


def _materialize_text_run_font_sizes(shape: etree._Element) -> None:
    global_sizes = [
        int(value)
        for value in shape.xpath(
            ".//a:r/a:rPr[@sz]/@sz | "
            ".//a:defRPr[@sz]/@sz | "
            ".//a:endParaRPr[@sz]/@sz",
            namespaces=NS,
        )
        if str(value).isdigit() and int(value) > 0
    ]
    global_fallback = min(global_sizes) if global_sizes else None
    for paragraph in shape.xpath("./p:txBody/a:p", namespaces=NS):
        paragraph_sizes = [
            int(value)
            for value in paragraph.xpath(
                "./a:r/a:rPr[@sz]/@sz | "
                "./a:pPr/a:defRPr[@sz]/@sz | "
                "./a:endParaRPr[@sz]/@sz",
                namespaces=NS,
            )
            if str(value).isdigit() and int(value) > 0
        ]
        fallback = (
            min(paragraph_sizes)
            if paragraph_sizes
            else global_fallback
        )
        if fallback is None:
            continue
        for run in paragraph.xpath(
            "./a:r[a:t[string-length(.) > 0]] | "
            "./a:fld[a:t[string-length(.) > 0]]",
            namespaces=NS,
        ):
            properties = run.find(f"{A}rPr")
            if properties is None:
                properties = etree.Element(f"{A}rPr")
                run.insert(0, properties)
            if properties.get("sz") is None:
                properties.set("sz", str(fallback))


def _shape_text_role(shape: etree._Element) -> str:
    placeholders = shape.xpath(
        "./p:nvSpPr/p:nvPr/p:ph",
        namespaces=NS,
    )
    placeholder_type = placeholders[0].get("type") if placeholders else None
    if placeholder_type in {"title", "ctrTitle", "subTitle"}:
        return "title"
    names = shape.xpath(
        "./p:nvSpPr/p:cNvPr/@name",
        namespaces=NS,
    )
    if names and "title" in str(names[0]).casefold():
        return "title"
    return "body"


def _effective_font_floor_pt(
    action: Mapping[str, Any],
    role: str,
    original_sizes: Sequence[int],
) -> float:
    def numeric(name: str, default: Any) -> float:
        try:
            value = float(action.get(name, default))
        except (TypeError, ValueError) as exc:
            raise UnsupportedPptxRepairError(
                f"{name} must be numeric"
            ) from exc
        if value <= 0:
            raise UnsupportedPptxRepairError(
                f"{name} must be greater than zero"
            )
        return value

    global_floor = max(
        HARD_MIN_FONT_SIZE_PT,
        numeric(
            "minimum_font_size_pt",
            action.get(
                "min_font_size_pt",
                HARD_MIN_FONT_SIZE_PT,
            ),
        ),
    )
    role_name = (
        "title_min_font_size_pt"
        if role == "title"
        else "body_min_font_size_pt"
    )
    role_floor = (
        max(
            HARD_MIN_FONT_SIZE_PT,
            numeric(role_name, HARD_MIN_FONT_SIZE_PT),
        )
        if action.get(role_name) is not None
        else HARD_MIN_FONT_SIZE_PT
    )
    template_value = action.get("template_min_font_size_pt")
    template_floor = (
        numeric("template_min_font_size_pt", 1.0)
        if template_value is not None
        else 0.0
    )
    return max(
        HARD_MIN_FONT_SIZE_PT,
        global_floor,
        role_floor,
        template_floor,
    )


def _resolve_text_overflow(
    *,
    package: zipfile.ZipFile,
    tree: etree._ElementTree,
    shape: etree._Element,
    body: etree._Element,
    slide_number: int,
    shape_id: str,
    part_name: str,
    action: Mapping[str, Any],
    baseline_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    if _shape_type(shape) != "shape":
        raise UnsupportedPptxRepairError(
            "resolve_text_overflow supports only ordinary text shapes"
        )
    _validate_autofit_mode(action)
    try:
        target_load = float(action.get("target_load_ratio", 0.92))
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "target_load_ratio must be numeric"
        ) from exc
    if not 0.5 <= target_load <= 1.0:
        raise UnsupportedPptxRepairError(
            "target_load_ratio must be between 0.5 and 1.0"
        )
    try:
        rendered_load = float(action.get("rendered_load_ratio", 0.0))
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "rendered_load_ratio must be numeric"
        ) from exc
    if rendered_load < 0 or rendered_load > 10:
        raise UnsupportedPptxRepairError(
            "rendered_load_ratio must be between 0 and 10"
        )
    try:
        rendered_target_load = float(
            action.get(
                "rendered_target_load_ratio",
                min(target_load, 0.80),
            )
        )
    except (TypeError, ValueError) as exc:
        raise UnsupportedPptxRepairError(
            "rendered_target_load_ratio must be numeric"
        ) from exc
    if not 0.5 <= rendered_target_load <= 10.0 or (
        rendered_load and rendered_target_load > rendered_load + 0.001
    ):
        raise UnsupportedPptxRepairError(
            "rendered_target_load_ratio must be between 0.5 and 10 and "
            "must not exceed rendered_load_ratio"
        )
    effective_target_load = (
        rendered_target_load if rendered_load else target_load
    )
    cache = {part_name: tree}
    bbox = _shape_bbox(package, part_name, shape, cache)
    if bbox is None:
        raise UnsupportedPptxRepairError(
            "text shape has no resolvable bounds"
        )
    text = "\n".join(
        "".join(
            str(node.text or "")
            for node in paragraph.xpath(".//a:t", namespaces=NS)
        )
        for paragraph in shape.xpath("./p:txBody/a:p", namespaces=NS)
    )
    before = {
        "bbox_emu": list(bbox),
        "body_properties": _body_properties(shape),
        "font_sizes_pt": _font_sizes_pt(shape),
        "capacity": _shape_capacity(shape, bbox, text=text),
    }
    steps: list[dict[str, Any]] = []

    autofit_before = _body_properties(shape)
    preserve_autofit_mode = _action_bool(
        action,
        "preserve_autofit_mode",
        False,
    )
    if preserve_autofit_mode:
        autofit_audit = {
            "font_scale": 100_000,
            "requested_font_scale": None,
            "minimum_safe_font_scale": None,
            "effective_min_font_size_pt": None,
            "font_floor_pt": _effective_font_floor_pt(
                action,
                _shape_text_role(shape),
                [round(size * 100) for size in _font_sizes_pt(shape)],
            ),
            "text_role": _shape_text_role(shape),
            "status": "PRESERVED",
        }
    else:
        autofit_audit = _set_safe_normal_autofit(shape, body, action)
    steps.append(
        {
            "action": "set_autofit",
            **autofit_audit,
            "before": autofit_before,
            "after": _body_properties(shape),
        }
    )

    if action.get("margins_emu") is not None:
        margins_before, margins_after = _set_explicit_body_margins(
            body,
            action["margins_emu"],
        )
    else:
        _, margins_before, margins_after = _scale_body_margins(
            body,
            action.get("margin_scale", 0.75),
        )
    steps.append(
        {
            "action": "adjust_margins",
            "before": {"margins_emu": margins_before},
            "after": {"margins_emu": margins_after},
        }
    )

    capacity = _shape_capacity(shape, bbox, text=text)
    if capacity is None:
        raise UnsupportedPptxRepairError(
            "could not estimate text shape capacity"
        )
    rendered_load_after_repairs = (
        rendered_load * float(autofit_audit["font_scale"]) / 100_000
    )
    load_ratio = max(
        float(capacity["load_ratio"]),
        rendered_load_after_repairs,
    )
    load_tolerance = 0.001
    if load_ratio > effective_target_load + load_tolerance:
        required_scale = max(
            0.5,
            min(1.0, effective_target_load / load_ratio),
        )
        font_action = {
            **dict(action),
            "scale": required_scale,
        }
        try:
            changes, font_audit = _apply_font_size_action(
                tree,
                shape,
                font_action,
                action_name="adjust_font_size",
            )
        except UnsupportedPptxRepairError as exc:
            steps.append(
                {
                    "action": "adjust_font_size",
                    "status": "SKIPPED",
                    "reason": str(exc),
                }
            )
        else:
            applied_scale = max(
                change["after_hundredths_pt"]
                / change["before_hundredths_pt"]
                for change in changes
            )
            rendered_load_after_repairs *= applied_scale
            refreshed_autofit = (
                autofit_audit
                if preserve_autofit_mode
                else _set_safe_normal_autofit(shape, body, action)
            )
            steps.append(
                {
                    "action": "adjust_font_size",
                    "status": "APPLIED",
                    **font_audit,
                    "changes": changes,
                }
            )
            steps[0].update(refreshed_autofit)
            steps[0]["after"] = _body_properties(shape)
        capacity = _shape_capacity(shape, bbox, text=text)
        assert capacity is not None
        load_ratio = max(
            float(capacity["load_ratio"]),
            rendered_load_after_repairs,
        )

    geometry_changed = False
    if load_ratio > effective_target_load + load_tolerance and _action_bool(
        action,
        "allow_expand",
        True,
    ):
        _assert_direct_geometry_safe(shape, "resolve_text_overflow")
        direct_bbox = _direct_shape_bbox(shape)
        if direct_bbox is None:
            raise UnsupportedPptxRepairError(
                "text shape cannot be expanded without overriding a template"
            )
        slide_size = baseline_snapshot["slide_size_emu"]
        expanded = _find_safe_expanded_bounds(
            package=package,
            tree=tree,
            part_name=part_name,
            target_shape=shape,
            target_bbox=direct_bbox,
            required_area_scale=load_ratio / effective_target_load,
            slide_width=int(slide_size["width"]),
            slide_height=int(slide_size["height"]),
        )
        if expanded is None:
            raise UnsupportedPptxRepairError(
                "no collision-safe in-canvas text box expansion was found"
            )
        _set_direct_shape_bbox(shape, expanded)
        steps.append(
            {
                "action": "set_shape_bounds",
                "before": {"bbox_emu": list(direct_bbox)},
                "after": {"bbox_emu": list(expanded)},
                "collision_safe": True,
                "clamp_to_slide": True,
            }
        )
        bbox = expanded
        geometry_changed = expanded != direct_bbox
        area_scale = (
            (expanded[2] * expanded[3])
            / max(1, direct_bbox[2] * direct_bbox[3])
        )
        rendered_load_after_repairs /= math.sqrt(area_scale)
        capacity = _shape_capacity(shape, bbox, text=text)
        assert capacity is not None
        load_ratio = max(
            float(capacity["load_ratio"]),
            rendered_load_after_repairs,
        )

    if load_ratio > effective_target_load + load_tolerance:
        raise UnsupportedPptxRepairError(
            f"text overflow remains at estimated load {load_ratio:.3f}; "
            f"maximum accepted estimate is {effective_target_load:.3f}"
        )
    after = {
        "bbox_emu": list(bbox),
        "body_properties": _body_properties(shape),
        "font_sizes_pt": _font_sizes_pt(shape),
        "capacity": capacity,
    }
    result = {
        "slide": slide_number,
        "shape_id": shape_id,
        "shape_type": "shape",
        "part_name": part_name,
        "before": before,
        "after": after,
        "steps": steps,
        "target_load_ratio": target_load,
        "effective_target_load_ratio": effective_target_load,
        "rendered_load_ratio_before": rendered_load or None,
        "rendered_load_ratio_estimate_after": (
            round(rendered_load_after_repairs, 6)
            if rendered_load
            else None
        ),
        "resolved": load_ratio <= effective_target_load + load_tolerance,
        "requires_rendered_verification": bool(rendered_load),
    }
    if geometry_changed:
        result["authorized_bbox_emu"] = list(bbox)
    return result


def _find_safe_expanded_bounds(
    *,
    package: zipfile.ZipFile,
    tree: etree._ElementTree,
    part_name: str,
    target_shape: etree._Element,
    target_bbox: tuple[int, int, int, int],
    required_area_scale: float,
    slide_width: int,
    slide_height: int,
) -> tuple[int, int, int, int] | None:
    x, y, width, height = target_bbox
    area_scale = max(1.0, required_area_scale)
    balanced = math.sqrt(area_scale)
    dimensions = {
        (
            min(slide_width, max(width, math.ceil(width * balanced))),
            min(slide_height, max(height, math.ceil(height * balanced))),
        ),
        (
            min(slide_width, max(width, math.ceil(width * area_scale))),
            height,
        ),
        (
            width,
            min(slide_height, max(height, math.ceil(height * area_scale))),
        ),
    }
    other_bounds = []
    cache = {part_name: tree}
    for other in _shape_elements(tree):
        if other is target_shape or _shape_type(other) in {"group", "connector"}:
            continue
        if _shape_or_ancestor_group_has_rotation_or_flip(other):
            raise UnsupportedPptxRepairError(
                "text box expansion rejects rotated or flipped collision "
                "obstacles"
            )
        bbox = _shape_bbox(package, part_name, other, cache)
        if bbox is not None:
            other_bounds.append(bbox)
    candidates: list[
        tuple[float, int, tuple[int, int, int, int]]
    ] = []
    for candidate_width, candidate_height in dimensions:
        x_positions = {
            x,
            x + width - candidate_width,
            x + (width - candidate_width) // 2,
            0,
            slide_width - candidate_width,
        }
        y_positions = {
            y,
            y + height - candidate_height,
            y + (height - candidate_height) // 2,
            0,
            slide_height - candidate_height,
        }
        for candidate_x in x_positions:
            for candidate_y in y_positions:
                candidate = (
                    max(0, min(slide_width - candidate_width, candidate_x)),
                    max(0, min(slide_height - candidate_height, candidate_y)),
                    candidate_width,
                    candidate_height,
                )
                if not _expansion_collision_safe(
                    target_bbox,
                    candidate,
                    other_bounds,
                ):
                    continue
                movement = abs(candidate[0] - x) + abs(candidate[1] - y)
                candidates.append(
                    (
                        candidate_width * candidate_height,
                        movement,
                        candidate,
                    )
                )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def _expansion_collision_safe(
    original: Sequence[int],
    candidate: Sequence[int],
    other_bounds: Sequence[Sequence[int]],
) -> bool:
    tolerance_area = EMUS_PER_POINT * EMUS_PER_POINT
    for other in other_bounds:
        original_intersection = _bbox_intersection_area(original, other)
        candidate_intersection = _bbox_intersection_area(candidate, other)
        if candidate_intersection > original_intersection + tolerance_area:
            return False
    return True


def _action_bool(
    action: Mapping[str, Any],
    name: str,
    default: bool,
) -> bool:
    value = action.get(name, default)
    if isinstance(value, bool):
        return value
    raise UnsupportedPptxRepairError(f"{name} must be a boolean")


def _coordinate_change_key(slide_number: int, shape_id: str) -> str:
    return f"{slide_number}:{shape_id}"


def _repair_replace_translation(
    package: zipfile.ZipFile,
    modified_parts: dict[str, etree._ElementTree],
    block_map: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    strict: bool,
) -> dict[str, Any]:
    block_id = str(action.get("block_id", "") or "")
    if not block_id or block_id not in block_map:
        raise UnsupportedPptxRepairError(
            f"replace_translation references unknown block: {block_id!r}"
        )
    block = block_map[block_id]
    part_name = str(_block_metadata(block, "part_name", ""))
    if part_name not in modified_parts:
        modified_parts[part_name] = _parse_xml(
            package.read(part_name),
            part_name,
        )
    tree = modified_parts[part_name]
    paragraph_xpath = str(_block_metadata(block, "paragraph_xpath", ""))
    matches = tree.xpath(paragraph_xpath, namespaces=NS)
    if len(matches) != 1:
        raise UnsafePptxPatchError(
            f"block {block_id} paragraph XPath did not resolve uniquely"
        )
    before = "".join(
        node.text or "" for node in _translatable_text_nodes(matches[0])
    )
    result = {
        "translated_text": action.get("text"),
        "segments": action.get("segments"),
    }
    reason = _patch_translation_block(tree, block, result, strict=strict)
    if reason:
        raise UnsafePptxPatchError(f"block {block_id}: {reason}")
    matches_after = tree.xpath(paragraph_xpath, namespaces=NS)
    after = "".join(
        node.text or ""
        for node in _translatable_text_nodes(matches_after[0])
    )
    return {
        "block_id": block_id,
        "slide": _field(_field(block, "context", {}), "slide_number", None),
        "shape_id": _field(_field(block, "context", {}), "shape_id", None),
        "part_name": part_name,
        "before_text_sha256": _sha256(before.encode("utf-8")),
        "after_text_sha256": _sha256(after.encode("utf-8")),
        "before_character_count": _visible_character_count(before),
        "after_character_count": _visible_character_count(after),
    }


def _shape_by_id(
    tree: etree._ElementTree,
    shape_id: str,
) -> etree._Element | None:
    matches = []
    for shape in _shape_elements(tree):
        candidate_id, _ = _shape_identity(shape, tree)
        if candidate_id == shape_id:
            matches.append(shape)
    if len(matches) > 1:
        raise InvalidPptxError(f"duplicate shape id in slide XML: {shape_id}")
    return matches[0] if matches else None


# Familiar aliases for callers that use the DOCX engine naming convention.
validate_output_package = validate_pptx
snapshot_pptx = structure_snapshot
extract_blocks = extract_translation_blocks
apply_repairs = apply_whitelisted_repairs
