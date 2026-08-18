"""Low-level, format-preserving DOCX extraction and patching.

This module intentionally edits only text nodes in existing OOXML parts.  It
does not use python-docx for writes because reconstructing paragraphs through a
high-level API can discard fields, bookmarks, hyperlinks, and other markup.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from lxml import etree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {
    "w": W_NS,
    "r": R_NS,
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
}
W = f"{{{W_NS}}}"
XML_SPACE = f"{{{XML_NS}}}space"

REQUIRED_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
}
TRANSLATABLE_PART_PATTERNS = (
    re.compile(r"^word/document\.xml$"),
    re.compile(r"^word/header\d+\.xml$"),
    re.compile(r"^word/footer\d+\.xml$"),
    re.compile(r"^word/footnotes\.xml$"),
    re.compile(r"^word/endnotes\.xml$"),
    re.compile(r"^word/comments\.xml$"),
)


class DocxEngineError(RuntimeError):
    """Base error for DOCX parsing, patching, and validation."""


class InvalidDocxError(DocxEngineError):
    """The input is not a safe, structurally valid DOCX package."""


class UnsafePatchError(DocxEngineError):
    """A translation could not be written without risking document markup."""


def extract_translation_blocks(
    path: str | os.PathLike[str],
) -> list[Any]:
    """Extract translatable paragraphs as ``TranslationBlock`` objects.

    ``TranslationBlock`` is imported lazily so the engine can be imported by
    package inspection and conversion tools without loading the API models.
    """

    source = _validated_path(path)
    block_type = _translation_block_type()
    blocks: list[Any] = []

    with zipfile.ZipFile(source) as package:
        referenced_notes = _referenced_note_ids(package)
        for part_name in _translatable_parts(package.namelist()):
            tree = _parse_xml(package.read(part_name), part_name)
            for paragraph in tree.xpath("//w:p", namespaces=NS):
                if not _is_referenced_note_paragraph(
                    paragraph, part_name, referenced_notes
                ):
                    continue
                text_nodes = _paragraph_text_nodes(paragraph)
                source_text = "".join(node.text or "" for node in text_nodes)
                if not source_text.strip():
                    continue
                if _visible_text_is_entirely_field_result(paragraph, text_nodes):
                    continue

                xpath = tree.getpath(paragraph)
                risk_flags = _risk_flags(paragraph, text_nodes)
                units = _text_units(tree, paragraph, text_nodes)
                protocol = _choose_protocol(units, risk_flags)
                raw_payload = {
                    "block_id": _stable_block_id(part_name, xpath),
                    "part_name": part_name,
                    "xpath": xpath,
                    "source_text": source_text,
                    "protected_text": source_text,
                    "protocol": protocol,
                    "units": units,
                    "risk_flags": risk_flags,
                }
                blocks.append(_make_translation_block(block_type, raw_payload))
    return blocks


def apply_translations(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    results: Iterable[Any],
    *,
    blocks: Iterable[Any] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Write translation results into a copy of ``source_path``.

    Plain, uniform-style paragraphs are collapsed into their first text node.
    Tagged results are mapped back to their original text units.  Complex
    structures are never flattened: without complete, correctly tagged
    segments they are skipped (or rejected when ``strict`` is true).
    """

    source = _validated_path(source_path)
    destination = Path(output_path).expanduser().resolve()
    if source == destination:
        raise UnsafePatchError("Output must be a new file; source is never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise UnsafePatchError(f"Refusing to overwrite existing output: {destination}")

    extracted = (
        list(blocks) if blocks is not None else extract_translation_blocks(source)
    )
    block_map = {_field(block, "block_id"): block for block in extracted}
    if len(block_map) != len(extracted):
        raise UnsafePatchError(
            "Duplicate block_id values were found in extraction data"
        )

    result_map: dict[str, Any] = {}
    for result in results:
        block_id = _field(result, "block_id")
        if not block_id:
            raise UnsafePatchError("Translation result is missing block_id")
        if block_id in result_map:
            raise UnsafePatchError(f"Duplicate translation result: {block_id}")
        if block_id not in block_map:
            raise UnsafePatchError(f"Unknown translation block: {block_id}")
        result_map[block_id] = result

    parsed_parts: dict[str, etree._ElementTree] = {}
    applied: list[str] = []
    skipped: list[dict[str, str]] = []

    with zipfile.ZipFile(source) as package:
        for block_id, result in result_map.items():
            block = block_map[block_id]
            part_name = _block_field(block, "part_name")
            if part_name not in parsed_parts:
                parsed_parts[part_name] = _parse_xml(package.read(part_name), part_name)
            tree = parsed_parts[part_name]
            matches = tree.xpath(_block_field(block, "xpath"), namespaces=NS)
            if len(matches) != 1:
                skipped.append(
                    {
                        "block_id": block_id,
                        "reason": "paragraph XPath did not resolve uniquely",
                    }
                )
                continue
            paragraph = matches[0]
            reason = _patch_paragraph(paragraph, block, result)
            if reason:
                skipped.append({"block_id": block_id, "reason": reason})
            else:
                applied.append(block_id)

        if strict and skipped:
            detail = "; ".join(
                f"{entry['block_id']}: {entry['reason']}" for entry in skipped[:5]
            )
            if len(skipped) > 5:
                detail += f"; and {len(skipped) - 5} more"
            raise UnsafePatchError(f"Unsafe DOCX patch rejected: {detail}")

        temp_handle, temp_name = tempfile.mkstemp(
            suffix=".docx", prefix=f".{destination.stem}-", dir=destination.parent
        )
        os.close(temp_handle)
        temp_path = Path(temp_name)
        try:
            with zipfile.ZipFile(temp_path, "w") as target:
                target.comment = package.comment
                for info in package.infolist():
                    if info.filename in parsed_parts:
                        data = etree.tostring(
                            parsed_parts[info.filename],
                            xml_declaration=True,
                            encoding="UTF-8",
                            standalone=True,
                        )
                    else:
                        data = package.read(info.filename)
                    target.writestr(info, data)
            validation = validate_output_package(
                temp_path, baseline_snapshot=structure_snapshot(source)
            )
            if not validation["valid"]:
                raise InvalidDocxError(
                    "Patched DOCX failed validation: " + "; ".join(validation["errors"])
                )
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)

    return {
        "output_path": str(destination),
        "applied": applied,
        "skipped": skipped,
        "validation": validate_output_package(
            destination, baseline_snapshot=structure_snapshot(source)
        ),
    }


def structure_snapshot(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return structural facts used to detect accidental format damage."""

    source = _validated_path(path)
    with zipfile.ZipFile(source) as package:
        names = sorted(package.namelist())
        media_hashes = {
            name: _sha256(package.read(name))
            for name in names
            if name.startswith("word/media/") and not name.endswith("/")
        }
        part_details: dict[str, dict[str, Any]] = {}
        section_sizes: list[dict[str, str | None]] = []
        for part_name in _translatable_parts(names):
            tree = _parse_xml(package.read(part_name), part_name)
            part_details[part_name] = {
                "sha256": _sha256(package.read(part_name)),
                "paragraphs": len(tree.xpath("//w:p", namespaces=NS)),
                "text_nodes": len(tree.xpath("//w:t", namespaces=NS)),
            }
            if part_name == "word/document.xml":
                for index, page_size in enumerate(
                    tree.xpath("//w:sectPr/w:pgSz", namespaces=NS), start=1
                ):
                    section_sizes.append(
                        {
                            "section": str(index),
                            "width": page_size.get(f"{W}w"),
                            "height": page_size.get(f"{W}h"),
                            "orientation": page_size.get(f"{W}orient"),
                        }
                    )
        return {
            "path": str(source),
            "package_entries": names,
            "package_entry_count": len(names),
            "media_hashes": media_hashes,
            "parts": part_details,
            "section_sizes": section_sizes,
        }


def validate_output_package(
    path: str | os.PathLike[str],
    baseline_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate ZIP/XML/relationship integrity and optional format invariants."""

    errors: list[str] = []
    warnings: list[str] = []
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        return {
            "valid": False,
            "errors": [f"DOCX does not exist: {candidate}"],
            "warnings": [],
            "snapshot": None,
        }

    try:
        with zipfile.ZipFile(candidate) as package:
            names = set(package.namelist())
            missing = sorted(REQUIRED_PARTS - names)
            if missing:
                errors.append("missing package entries: " + ", ".join(missing))
            corrupt = package.testzip()
            if corrupt:
                errors.append(f"corrupt ZIP member: {corrupt}")

            for name in sorted(names):
                if name.endswith(".xml") or name.endswith(".rels"):
                    try:
                        _parse_xml(package.read(name), name)
                    except InvalidDocxError as exc:
                        errors.append(str(exc))
            errors.extend(_broken_relationships(package))
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"not a readable DOCX ZIP package: {exc}")

    snapshot: dict[str, Any] | None = None
    if not errors:
        try:
            snapshot = structure_snapshot(candidate)
        except DocxEngineError as exc:
            errors.append(str(exc))

    if baseline_snapshot is not None and snapshot is not None:
        original_entries = set(baseline_snapshot.get("package_entries", []))
        current_entries = set(snapshot.get("package_entries", []))
        if original_entries != current_entries:
            removed = sorted(original_entries - current_entries)
            added = sorted(current_entries - original_entries)
            errors.append(
                "package entry set changed"
                + (f"; removed={removed}" if removed else "")
                + (f"; added={added}" if added else "")
            )
        if baseline_snapshot.get("media_hashes", {}) != snapshot.get(
            "media_hashes", {}
        ):
            errors.append("embedded media content changed")
        if baseline_snapshot.get("section_sizes", []) != snapshot.get(
            "section_sizes", []
        ):
            errors.append("section page size or orientation changed")

        for part_name, before in baseline_snapshot.get("parts", {}).items():
            after = snapshot.get("parts", {}).get(part_name)
            if after is None:
                errors.append(f"translatable OOXML part was removed: {part_name}")
                continue
            if before.get("paragraphs") != after.get("paragraphs"):
                errors.append(f"paragraph count changed in {part_name}")
            if before.get("text_nodes") != after.get("text_nodes"):
                errors.append(f"text-node count changed in {part_name}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "snapshot": snapshot,
    }


class DocxEngine:
    """Small object facade for dependency injection in the pipeline."""

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
    ) -> dict[str, Any]:
        return validate_output_package(path, baseline_snapshot)


def _translation_block_type() -> type[Any]:
    try:
        from .models import TranslationBlock
    except (ImportError, AttributeError) as exc:
        raise DocxEngineError(
            "format_translator.models.TranslationBlock is unavailable"
        ) from exc
    return TranslationBlock


def _validated_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise InvalidDocxError(f"DOCX does not exist: {candidate}")
    if candidate.suffix.lower() != ".docx":
        raise InvalidDocxError(f"Expected .docx input, got: {candidate.name}")
    try:
        with zipfile.ZipFile(candidate) as package:
            names = set(package.namelist())
            missing = sorted(REQUIRED_PARTS - names)
            corrupt = package.testzip()
    except (OSError, zipfile.BadZipFile) as exc:
        raise InvalidDocxError(f"Not a readable DOCX package: {candidate}") from exc
    if missing:
        raise InvalidDocxError(
            "DOCX is missing required package entries: " + ", ".join(missing)
        )
    if corrupt:
        raise InvalidDocxError(f"DOCX contains a corrupt ZIP member: {corrupt}")
    return candidate


def _translatable_parts(names: Iterable[str]) -> list[str]:
    return sorted(
        name
        for name in names
        if any(pattern.match(name) for pattern in TRANSLATABLE_PART_PATTERNS)
    )


def _referenced_note_ids(
    package: zipfile.ZipFile,
) -> dict[str, set[int]]:
    """Collect positive note IDs actually referenced by the main document."""

    tree = _parse_xml(package.read("word/document.xml"), "word/document.xml")
    result: dict[str, set[int]] = {
        "word/footnotes.xml": set(),
        "word/endnotes.xml": set(),
    }
    for part_name, element_name in (
        ("word/footnotes.xml", "footnoteReference"),
        ("word/endnotes.xml", "endnoteReference"),
    ):
        values = tree.xpath(
            f"//w:{element_name}/@w:id",
            namespaces=NS,
        )
        for value in values:
            try:
                identifier = int(value)
            except (TypeError, ValueError):
                continue
            if identifier > 0:
                result[part_name].add(identifier)
    return result


def _is_referenced_note_paragraph(
    paragraph: etree._Element,
    part_name: str,
    referenced_notes: Mapping[str, set[int]],
) -> bool:
    if part_name not in referenced_notes:
        return True
    note_tag = f"{W}footnote" if part_name == "word/footnotes.xml" else f"{W}endnote"
    note = next(
        (
            ancestor
            for ancestor in paragraph.iterancestors()
            if ancestor.tag == note_tag
        ),
        None,
    )
    if note is None:
        return False
    try:
        identifier = int(note.get(f"{W}id"))
    except (TypeError, ValueError):
        return False
    return identifier > 0 and identifier in referenced_notes[part_name]


def _parse_xml(data: bytes, part_name: str) -> etree._ElementTree:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        remove_blank_text=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise InvalidDocxError(f"Invalid XML in {part_name}: {exc}") from exc
    return etree.ElementTree(root)


def _stable_block_id(part_name: str, xpath: str) -> str:
    digest = hashlib.sha256(f"{part_name}\0{xpath}".encode("utf-8")).hexdigest()
    return f"docx_{digest[:24]}"


def _text_units(
    tree: etree._ElementTree,
    paragraph: etree._Element,
    text_nodes: Sequence[etree._Element],
) -> list[dict[str, Any]]:
    run_positions = {
        tree.getpath(run): index
        for index, run in enumerate(
            (
                run
                for run in paragraph.xpath(".//w:r", namespaces=NS)
                if _nearest_paragraph(run) == paragraph
            ),
            start=1,
        )
    }
    units: list[dict[str, Any]] = []
    for index, node in enumerate(text_nodes):
        run = node.getparent()
        while run is not None and run.tag != f"{W}r":
            run = run.getparent()
        run_index = run_positions.get(tree.getpath(run)) if run is not None else None
        rpr = run.find(f"{W}rPr") if run is not None else None
        style_xml = (
            etree.tostring(rpr, method="c14n").decode("utf-8")
            if rpr is not None
            else ""
        )
        ancestors = set(node.iterancestors())
        units.append(
            {
                "unit_id": f"t{index}",
                "tag": f"t{index}",
                "index": index,
                "xpath": tree.getpath(node),
                "run_index": run_index,
                "text": node.text or "",
                "style_hash": _sha256(style_xml.encode("utf-8"))[:16],
                "in_hyperlink": any(item.tag == f"{W}hyperlink" for item in ancestors),
                "in_field": any(
                    item.tag in {f"{W}fldSimple", f"{W}instrText"} for item in ancestors
                ),
            }
        )
    return units


def _risk_flags(
    paragraph: etree._Element,
    text_nodes: Sequence[etree._Element],
) -> list[str]:
    checks = (
        ("hyperlink", ".//w:hyperlink"),
        ("field", ".//w:fldSimple | .//w:instrText | .//w:fldChar"),
        ("bookmark", ".//w:bookmarkStart | .//w:bookmarkEnd"),
        ("comment_anchor", ".//w:commentRangeStart | .//w:commentRangeEnd"),
        ("tracked_change", ".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo"),
        ("content_control", ".//w:sdt"),
        ("drawing_or_object", ".//w:drawing | .//w:object | .//w:pict"),
        ("manual_break", ".//w:br"),
        ("tab", ".//w:tab"),
        ("note_reference", ".//w:footnoteReference | .//w:endnoteReference"),
    )
    flags = [
        name
        for name, expression in checks
        if paragraph.xpath(expression, namespaces=NS)
    ]
    style_hashes = {
        unit["style_hash"]
        for unit in _text_units(paragraph.getroottree(), paragraph, text_nodes)
    }
    if len(style_hashes) > 1:
        flags.append("multiple_styles")
    if len(text_nodes) > 1:
        flags.append("multiple_text_nodes")
    hyperlink_ids = {
        link.get(f"{{{R_NS}}}id")
        for link in paragraph.xpath(".//w:hyperlink", namespaces=NS)
    }
    if len(hyperlink_ids) > 1:
        flags.append("multiple_hyperlinks")
    return flags


def _choose_protocol(
    units: Sequence[Mapping[str, Any]], risk_flags: Sequence[str]
) -> str:
    style_hashes = {str(unit.get("style_hash", "")) for unit in units}
    structural_risks = set(risk_flags) - {"multiple_text_nodes"}
    if len(style_hashes) <= 1 and not structural_risks:
        return "plain"
    # Tagged units preserve run-level styling and all risky inline structures.
    return "tagged"


def _patch_paragraph(paragraph: etree._Element, block: Any, result: Any) -> str | None:
    text_nodes = _paragraph_text_nodes(paragraph)
    if not text_nodes:
        return "paragraph no longer contains text nodes"

    translated = _field(result, "translated_text", "")
    segments = _field(result, "segments", None)
    protocol = _block_field(block, "protocol", "plain")
    risk_flags = set(_block_field(block, "risk_flags", []) or [])
    complex_flags = {
        "hyperlink",
        "field",
        "bookmark",
        "comment_anchor",
        "tracked_change",
        "content_control",
        "drawing_or_object",
        "multiple_hyperlinks",
    }

    if protocol == "plain" and not (risk_flags & complex_flags):
        if translated is None:
            return "translation text is missing"
        _set_text(text_nodes[0], str(translated))
        for node in text_nodes[1:]:
            _set_text(node, "")
        return None

    normalized = _normalize_segments(segments)
    units = _block_field(block, "units", []) or []
    expected_tags = [
        str(_field(unit, "tag", _field(unit, "unit_id", f"t{index}")))
        for index, unit in enumerate(units)
    ]
    if not normalized:
        if not (risk_flags & complex_flags) and translated is not None:
            _write_proportional_segments(
                text_nodes,
                str(translated),
                [str(_field(unit, "text", "")) for unit in units],
            )
            return None
        return "tagged translation requires segments"
    if len(text_nodes) != len(expected_tags):
        return "text-unit count changed since extraction"
    if set(normalized) != set(expected_tags):
        return "segments do not exactly match extracted tags"

    for node, tag in zip(text_nodes, expected_tags):
        _set_text(node, normalized[tag])
    return None


def _normalize_segments(segments: Any) -> dict[str, str]:
    if not segments:
        return {}
    if isinstance(segments, Mapping):
        return {str(key): str(value) for key, value in segments.items()}
    normalized: dict[str, str] = {}
    for index, segment in enumerate(segments):
        if isinstance(segment, str):
            normalized[f"t{index}"] = segment
            continue
        tag = _field(
            segment,
            "tag",
            _field(
                segment,
                "unit_id",
                _field(segment, "segment_id", _field(segment, "id", None)),
            ),
        )
        text = _field(
            segment,
            "text",
            _field(segment, "translated_text", _field(segment, "translation", None)),
        )
        if tag is None or text is None or str(tag) in normalized:
            return {}
        normalized[str(tag)] = str(text)
    return normalized


def _paragraph_text_nodes(paragraph: etree._Element) -> list[etree._Element]:
    """Return text owned by this paragraph, excluding nested textbox paragraphs."""

    return [
        node
        for node in paragraph.xpath(".//w:t", namespaces=NS)
        if _nearest_paragraph(node) == paragraph
    ]


def _visible_text_is_entirely_field_result(
    paragraph: etree._Element,
    text_nodes: Sequence[etree._Element],
) -> bool:
    """Identify cached field values such as PAGE that must not be translated."""

    if not paragraph.xpath(
        ".//w:fldSimple | .//w:instrText | .//w:fldChar",
        namespaces=NS,
    ):
        return False

    owned_ids = {id(node) for node in text_nodes}
    field_stack: list[str] = []
    field_text_ids: set[int] = set()
    for element in paragraph.iterdescendants():
        if _nearest_paragraph(element) != paragraph:
            continue
        if element.tag == f"{W}fldChar":
            kind = element.get(f"{W}fldCharType")
            if kind == "begin":
                field_stack.append("code")
            elif kind == "separate" and field_stack:
                field_stack[-1] = "result"
            elif kind == "end" and field_stack:
                field_stack.pop()
            continue
        if element.tag != f"{W}t" or id(element) not in owned_ids:
            continue
        if field_stack or any(
            ancestor.tag == f"{W}fldSimple" for ancestor in element.iterancestors()
        ):
            field_text_ids.add(id(element))

    return bool(text_nodes) and field_text_ids == owned_ids


def _write_proportional_segments(
    text_nodes: Sequence[etree._Element],
    translated: str,
    source_segments: Sequence[str],
) -> None:
    """Best-effort mapping for simple styled runs when tags are unavailable."""

    if len(text_nodes) != len(source_segments):
        raise UnsafePatchError("simple tagged paragraph changed text-unit count")
    weights = [max(1, len(value)) for value in source_segments]
    total_weight = sum(weights)
    cursor = 0
    cumulative = 0
    for index, (node, weight) in enumerate(zip(text_nodes, weights)):
        cumulative += weight
        boundary = (
            len(translated)
            if index == len(text_nodes) - 1
            else round(len(translated) * cumulative / total_weight)
        )
        _set_text(node, translated[cursor:boundary])
        cursor = boundary


def _nearest_paragraph(node: etree._Element) -> etree._Element | None:
    return next(
        (ancestor for ancestor in node.iterancestors() if ancestor.tag == f"{W}p"),
        None,
    )


def _set_text(node: etree._Element, value: str) -> None:
    node.text = value
    if value[:1].isspace() or value[-1:].isspace():
        node.set(XML_SPACE, "preserve")
    else:
        node.attrib.pop(XML_SPACE, None)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _block_field(value: Any, name: str, default: Any = None) -> Any:
    """Read both the explicit location schema and the metadata-compatible one."""

    direct = _field(value, name, None)
    if direct is not None:
        return direct
    metadata = _field(value, "metadata", {}) or {}
    return _field(metadata, name, default)


def _make_translation_block(block_type: type[Any], payload: dict[str, Any]) -> Any:
    """Construct either supported TranslationBlock schema.

    The core contract originally exposed OOXML coordinates as explicit fields.
    Some API model versions intentionally keep document-engine details inside
    ``metadata`` and represent run units as ``segments``.  Detecting the model
    fields here keeps extraction compatible with both without weakening strict
    Pydantic validation.
    """

    model_fields = getattr(block_type, "model_fields", None)
    if not model_fields or "part_name" in model_fields:
        return block_type(**payload)

    units = payload["units"]
    segments = [
        {
            "segment_id": unit["tag"],
            "text": unit["text"],
            "style_id": unit["style_hash"],
            "metadata": {
                "xpath": unit["xpath"],
                "run_index": unit["run_index"],
                "in_hyperlink": unit["in_hyperlink"],
                "in_field": unit["in_field"],
            },
        }
        for unit in units
    ]
    return block_type(
        block_id=payload["block_id"],
        source_text=payload["source_text"],
        protocol=payload["protocol"],
        segments=segments,
        metadata={
            "part_name": payload["part_name"],
            "xpath": payload["xpath"],
            "protected_text": payload["protected_text"],
            "units": units,
            "risk_flags": payload["risk_flags"],
        },
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _broken_relationships(package: zipfile.ZipFile) -> list[str]:
    names = set(package.namelist())
    errors: list[str] = []
    for rels_name in sorted(name for name in names if name.endswith(".rels")):
        try:
            tree = _parse_xml(package.read(rels_name), rels_name)
        except InvalidDocxError:
            continue
        source_part = _relationship_source_part(rels_name)
        source_dir = posixpath.dirname(source_part)
        for relationship in tree.getroot():
            if relationship.tag != f"{{{PKG_REL_NS}}}Relationship":
                continue
            if relationship.get("TargetMode") == "External":
                continue
            target = relationship.get("Target")
            if not target or target.startswith("#"):
                continue
            if target.startswith("/"):
                resolved = posixpath.normpath(target.lstrip("/"))
            else:
                resolved = posixpath.normpath(posixpath.join(source_dir, target))
            if resolved.startswith("../") or resolved not in names:
                rel_id = relationship.get("Id", "<unknown>")
                errors.append(
                    f"broken internal relationship {rel_id} in {rels_name}: {target}"
                )
    return errors


def _relationship_source_part(rels_name: str) -> str:
    if rels_name == "_rels/.rels":
        return ""
    directory, filename = posixpath.split(rels_name)
    if not directory.endswith("/_rels"):
        return ""
    source_directory = directory[: -len("/_rels")]
    source_filename = filename[: -len(".rels")]
    return posixpath.join(source_directory, source_filename)


# Compatibility aliases kept deliberately small for callers with intuitive names.
extract_docx_blocks = extract_translation_blocks
snapshot_docx = structure_snapshot
patch_docx = apply_translations
validate_docx = validate_output_package
