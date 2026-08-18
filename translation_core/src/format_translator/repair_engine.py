"""Controlled, auditable, and reversible DOCX repairs.

The repair engine deliberately exposes a small allow-list of mutations.  Every
run reads one DOCX and writes a different DOCX, validates the resulting OOXML
package, and emits a self-contained audit that can restore every modified XML
part byte-for-byte.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .docx_engine import (
    NS,
    W,
    _patch_paragraph,
    extract_translation_blocks,
    structure_snapshot,
    validate_output_package,
)
from .models import TranslationResult


class RepairEngineError(RuntimeError):
    """A requested repair or rollback could not be performed safely."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExplicitPageBreakAction(_StrictModel):
    """Insert or remove a page-break-only paragraph before one text block."""

    type: Literal["explicit_page_break"] = "explicit_page_break"
    block_id: str = Field(min_length=1)
    operation: Literal["insert", "remove"]
    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


class ParagraphFormatAction(_StrictModel):
    """Set supported paragraph pagination and spacing properties."""

    type: Literal["paragraph_format"] = "paragraph_format"
    block_id: str = Field(min_length=1)
    keep_next: bool | None = None
    keep_lines: bool | None = None
    page_break_before: bool | None = None
    spacing_before_twips: int | None = Field(default=None, ge=0)
    spacing_after_twips: int | None = Field(default=None, ge=0)
    spacing_line_twips: int | None = Field(default=None, gt=0)
    spacing_line_rule: Literal["auto", "atLeast", "exact"] | None = None
    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


class ReplaceTextAction(_StrictModel):
    """Apply one normalized TranslationResult through the DOCX patch policy."""

    type: Literal["replace_text"] = "replace_text"
    result: TranslationResult
    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


RepairAction = Annotated[
    ExplicitPageBreakAction | ParagraphFormatAction | ReplaceTextAction,
    Field(discriminator="type"),
]
_ACTION_ADAPTER = TypeAdapter(RepairAction)


class RepairAuditEntry(_StrictModel):
    action_id: str
    action_type: str
    block_id: str
    part_name: str
    xpath: str
    changed: bool
    before: dict[str, Any]
    after: dict[str, Any]
    inverse: dict[str, Any] | None = None


class PartBackup(_StrictModel):
    part_name: str
    before_sha256: str
    after_sha256: str
    before_base64: str
    after_base64: str


class RepairAudit(_StrictModel):
    audit_version: Literal[1] = 1
    audit_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_path: Path
    output_path: Path
    audit_path: Path
    source_sha256: str
    output_sha256: str
    entries: list[RepairAuditEntry]
    part_backups: list[PartBackup]
    validation: dict[str, Any]

    def write_json(self, path: str | os.PathLike[str]) -> Path:
        destination = Path(path).expanduser().resolve()
        if destination.exists():
            raise RepairEngineError(
                f"Refusing to overwrite an existing repair audit: {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            destination,
            self.model_dump_json(indent=2),
        )
        return destination


def execute_repairs(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    actions: list[RepairAction | Mapping[str, Any]],
    *,
    audit_path: str | os.PathLike[str] | None = None,
) -> RepairAudit:
    """Apply controlled actions to a new DOCX and write its reversible audit."""

    source = _existing_docx(source_path)
    output = _new_docx_path(output_path, source)
    parsed_actions = [_parse_action(action) for action in actions]
    if not parsed_actions:
        raise RepairEngineError("At least one repair action is required")

    blocks = extract_translation_blocks(source)
    block_map = {block.block_id: block for block in blocks}
    if len(block_map) != len(blocks):
        raise RepairEngineError("Extraction returned duplicate block IDs")

    with zipfile.ZipFile(source) as package:
        original_part_bytes: dict[str, bytes] = {}
        trees: dict[str, etree._ElementTree] = {}
        targets: dict[str, tuple[str, str, Any, etree._Element]] = {}
        entries: list[RepairAuditEntry] = []

        # Resolve every target before structural actions can shift sibling XPaths.
        for action in parsed_actions:
            block_id = (
                action.result.block_id
                if isinstance(action, ReplaceTextAction)
                else action.block_id
            )
            block = block_map.get(block_id)
            if block is None:
                raise RepairEngineError(
                    f"Unknown or non-translatable block: {block_id}"
                )
            part_name = _block_field(block, "part_name")
            xpath = _block_field(block, "xpath")
            if part_name not in trees:
                try:
                    data = package.read(part_name)
                except KeyError as exc:
                    raise RepairEngineError(
                        f"Block part is missing from package: {part_name}"
                    ) from exc
                original_part_bytes[part_name] = data
                trees[part_name] = _parse_xml(data, part_name)
            tree = trees[part_name]
            matches = tree.xpath(xpath, namespaces=NS)
            if len(matches) != 1:
                raise RepairEngineError(
                    f"Block XPath does not resolve uniquely: {block_id} ({xpath})"
                )
            targets[block_id] = (part_name, xpath, block, matches[0])

        for action in parsed_actions:
            block_id = (
                action.result.block_id
                if isinstance(action, ReplaceTextAction)
                else action.block_id
            )
            part_name, xpath, block, paragraph = targets[block_id]
            entries.append(_apply_action(action, block, paragraph, part_name, xpath))

        dirty_parts = {entry.part_name for entry in entries if entry.changed}
        modified_part_bytes = {
            part_name: _serialize_xml(trees[part_name]) for part_name in dirty_parts
        }
        temp_path = _write_package_copy(
            package,
            output,
            modified_part_bytes,
        )

    try:
        validation = validate_output_package(temp_path)
        _validate_format_invariants(source, temp_path, validation)
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)
    validation = validate_output_package(output)
    if not validation["valid"]:
        output.unlink(missing_ok=True)
        raise RepairEngineError(
            "Finalized repair output failed validation: "
            + "; ".join(validation["errors"])
        )

    backups = [
        PartBackup(
            part_name=part_name,
            before_sha256=_sha256_bytes(original_part_bytes[part_name]),
            after_sha256=_sha256_bytes(modified_part_bytes[part_name]),
            before_base64=_encode(original_part_bytes[part_name]),
            after_base64=_encode(modified_part_bytes[part_name]),
        )
        for part_name in sorted(modified_part_bytes)
    ]
    audit_destination = (
        Path(audit_path).expanduser().resolve()
        if audit_path is not None
        else output.with_suffix(output.suffix + ".repair-audit.json")
    )
    audit = RepairAudit(
        source_path=source,
        output_path=output,
        audit_path=audit_destination,
        source_sha256=_sha256_file(source),
        output_sha256=_sha256_file(output),
        entries=entries,
        part_backups=backups,
        validation=validation,
    )
    try:
        audit.write_json(audit_destination)
    except Exception:
        # A repair is not deliverable without its required recovery record.
        output.unlink(missing_ok=True)
        raise
    return audit


def rollback_repairs(
    repaired_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    audit: RepairAudit | Mapping[str, Any] | str | os.PathLike[str],
) -> dict[str, Any]:
    """Restore audited OOXML parts into a new file.

    Rollback refuses a repaired file whose hash differs from the audited output,
    preventing an old audit from overwriting subsequent user edits.
    """

    repaired = _existing_docx(repaired_path)
    output = _new_docx_path(output_path, repaired)
    parsed_audit = _load_audit(audit)
    actual_hash = _sha256_file(repaired)
    if actual_hash != parsed_audit.output_sha256:
        raise RepairEngineError(
            "Repaired file hash does not match the audit; refusing rollback"
        )

    replacements: dict[str, bytes] = {}
    with zipfile.ZipFile(repaired) as package:
        names = set(package.namelist())
        for backup in parsed_audit.part_backups:
            if backup.part_name not in names:
                raise RepairEngineError(
                    f"Audited part is missing from repaired package: {backup.part_name}"
                )
            current = package.read(backup.part_name)
            if _sha256_bytes(current) != backup.after_sha256:
                raise RepairEngineError(
                    f"Audited part changed after repair: {backup.part_name}"
                )
            original = _decode(backup.before_base64, backup.part_name)
            if _sha256_bytes(original) != backup.before_sha256:
                raise RepairEngineError(
                    f"Audit backup checksum failed: {backup.part_name}"
                )
            replacements[backup.part_name] = original
        temp_path = _write_package_copy(package, output, replacements)

    try:
        validation = validate_output_package(temp_path)
        if not validation["valid"]:
            raise RepairEngineError(
                "Rolled-back DOCX failed validation: " + "; ".join(validation["errors"])
            )
        for backup in parsed_audit.part_backups:
            with zipfile.ZipFile(temp_path) as package:
                restored = package.read(backup.part_name)
            if _sha256_bytes(restored) != backup.before_sha256:
                raise RepairEngineError(
                    f"Rollback did not restore audited part: {backup.part_name}"
                )
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)

    return {
        "output_path": str(output),
        "output_sha256": _sha256_file(output),
        "restored_parts": sorted(replacements),
        "validation": validation,
    }


def _parse_action(action: RepairAction | Mapping[str, Any]) -> RepairAction:
    if isinstance(
        action,
        (ExplicitPageBreakAction, ParagraphFormatAction, ReplaceTextAction),
    ):
        return action
    try:
        return _ACTION_ADAPTER.validate_python(action)
    except Exception as exc:
        raise RepairEngineError(f"Invalid repair action: {exc}") from exc


def _apply_action(
    action: RepairAction,
    block: Any,
    paragraph: etree._Element,
    part_name: str,
    xpath: str,
) -> RepairAuditEntry:
    before_xml = etree.tostring(paragraph, encoding="UTF-8")
    if isinstance(action, ExplicitPageBreakAction):
        before = _explicit_break_snapshot(paragraph)
        changed = _set_explicit_break(paragraph, action.operation)
        after = _explicit_break_snapshot(paragraph)
        action_type = action.type
        block_id = action.block_id
    elif isinstance(action, ParagraphFormatAction):
        before = _paragraph_format_snapshot(paragraph)
        _set_paragraph_format(paragraph, action)
        after = _paragraph_format_snapshot(paragraph)
        changed = before != after
        action_type = action.type
        block_id = action.block_id
    else:
        before = {"text": _paragraph_text(paragraph)}
        reason = _patch_paragraph(paragraph, block, action.result)
        if reason:
            raise RepairEngineError(
                f"Unsafe replacement for {action.result.block_id}: {reason}"
            )
        after = {"text": _paragraph_text(paragraph)}
        changed = before != after
        action_type = action.type
        block_id = action.result.block_id

    after_xml = etree.tostring(paragraph, encoding="UTF-8")
    if before_xml != after_xml:
        changed = True
    inverse = (
        {
            "type": "restore_part_from_audit",
            "part_name": part_name,
        }
        if changed
        else None
    )
    return RepairAuditEntry(
        action_id=action.action_id,
        action_type=action_type,
        block_id=block_id,
        part_name=part_name,
        xpath=xpath,
        changed=changed,
        before=before,
        after=after,
        inverse=inverse,
    )


def _set_explicit_break(
    paragraph: etree._Element, operation: Literal["insert", "remove"]
) -> bool:
    previous = paragraph.getprevious()
    present = previous is not None and _is_page_break_only_paragraph(previous)
    if operation == "insert":
        if present:
            return False
        page_break_paragraph = etree.Element(f"{W}p")
        run = etree.SubElement(page_break_paragraph, f"{W}r")
        page_break = etree.SubElement(run, f"{W}br")
        page_break.set(f"{W}type", "page")
        paragraph.addprevious(page_break_paragraph)
        return True
    if not present:
        return False
    previous.getparent().remove(previous)
    return True


def _is_page_break_only_paragraph(paragraph: etree._Element) -> bool:
    if paragraph.tag != f"{W}p":
        return False
    breaks = paragraph.xpath(".//w:br[@w:type='page']", namespaces=NS)
    if len(breaks) != 1:
        return False
    disallowed = paragraph.xpath(
        ".//w:t | .//w:tab | .//w:drawing | .//w:object | "
        ".//w:fldChar | .//w:instrText",
        namespaces=NS,
    )
    other_breaks = paragraph.xpath(".//w:br[not(@w:type='page')]", namespaces=NS)
    return not disallowed and not other_breaks


def _explicit_break_snapshot(paragraph: etree._Element) -> dict[str, Any]:
    previous = paragraph.getprevious()
    return {
        "present": bool(
            previous is not None and _is_page_break_only_paragraph(previous)
        )
    }


def _set_paragraph_format(
    paragraph: etree._Element, action: ParagraphFormatAction
) -> bool:
    ppr = paragraph.find(f"{W}pPr")
    requested = any(
        value is not None
        for value in (
            action.keep_next,
            action.keep_lines,
            action.page_break_before,
            action.spacing_before_twips,
            action.spacing_after_twips,
            action.spacing_line_twips,
            action.spacing_line_rule,
        )
    )
    if not requested:
        raise RepairEngineError(
            f"Paragraph format action {action.action_id} contains no changes"
        )
    if ppr is None:
        ppr = etree.Element(f"{W}pPr")
        paragraph.insert(0, ppr)

    _set_on_off(ppr, "keepNext", action.keep_next)
    _set_on_off(ppr, "keepLines", action.keep_lines)
    _set_on_off(ppr, "pageBreakBefore", action.page_break_before)

    spacing_values = (
        action.spacing_before_twips,
        action.spacing_after_twips,
        action.spacing_line_twips,
        action.spacing_line_rule,
    )
    if any(value is not None for value in spacing_values):
        spacing = ppr.find(f"{W}spacing")
        if spacing is None:
            spacing = etree.SubElement(ppr, f"{W}spacing")
        for name, value in (
            ("before", action.spacing_before_twips),
            ("after", action.spacing_after_twips),
            ("line", action.spacing_line_twips),
            ("lineRule", action.spacing_line_rule),
        ):
            if value is not None:
                spacing.set(f"{W}{name}", str(value))
    return True


def _set_on_off(ppr: etree._Element, local_name: str, value: bool | None) -> None:
    if value is None:
        return
    element = ppr.find(f"{W}{local_name}")
    if element is None:
        element = etree.SubElement(ppr, f"{W}{local_name}")
    element.set(f"{W}val", "1" if value else "0")


def _paragraph_format_snapshot(paragraph: etree._Element) -> dict[str, Any]:
    ppr = paragraph.find(f"{W}pPr")
    if ppr is None:
        return {
            "keep_next": None,
            "keep_lines": None,
            "page_break_before": None,
            "spacing": {},
        }
    spacing = ppr.find(f"{W}spacing")
    return {
        "keep_next": _on_off_snapshot(ppr.find(f"{W}keepNext")),
        "keep_lines": _on_off_snapshot(ppr.find(f"{W}keepLines")),
        "page_break_before": _on_off_snapshot(ppr.find(f"{W}pageBreakBefore")),
        "spacing": (
            {
                name: spacing.get(f"{W}{name}")
                for name in ("before", "after", "line", "lineRule")
                if spacing.get(f"{W}{name}") is not None
            }
            if spacing is not None
            else {}
        ),
    }


def _on_off_snapshot(element: etree._Element | None) -> bool | None:
    if element is None:
        return None
    value = element.get(f"{W}val")
    return value not in {"0", "false", "off"}


def _paragraph_text(paragraph: etree._Element) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def _validate_format_invariants(
    source: Path,
    candidate: Path,
    validation: dict[str, Any],
) -> None:
    if not validation["valid"]:
        raise RepairEngineError(
            "Repaired DOCX failed validation: " + "; ".join(validation["errors"])
        )
    before = structure_snapshot(source)
    after = structure_snapshot(candidate)
    if before["package_entries"] != after["package_entries"]:
        raise RepairEngineError("Repair changed the DOCX package entry set")
    if before["media_hashes"] != after["media_hashes"]:
        raise RepairEngineError("Repair changed embedded media")
    if before["section_sizes"] != after["section_sizes"]:
        raise RepairEngineError("Repair changed section page geometry")


def _write_package_copy(
    package: zipfile.ZipFile,
    output: Path,
    replacements: Mapping[str, bytes],
) -> Path:
    handle, name = tempfile.mkstemp(
        suffix=".docx",
        prefix=f".{output.stem}-",
        dir=output.parent,
    )
    os.close(handle)
    temp_path = Path(name)
    try:
        if not replacements and isinstance(package.filename, (str, os.PathLike)):
            shutil.copyfile(package.filename, temp_path)
            return temp_path
        with zipfile.ZipFile(temp_path, "w") as target:
            target.comment = package.comment
            for info in package.infolist():
                target.writestr(
                    info,
                    replacements.get(info.filename, package.read(info.filename)),
                )
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


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
        raise RepairEngineError(f"Invalid XML in {part_name}: {exc}") from exc


def _serialize_xml(tree: etree._ElementTree) -> bytes:
    return etree.tostring(
        tree,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


def _block_field(block: Any, name: str) -> Any:
    value = getattr(block, name, None)
    if value is not None:
        return value
    metadata = getattr(block, "metadata", {}) or {}
    value = metadata.get(name)
    if value is None:
        raise RepairEngineError(f"Translation block is missing {name}")
    return value


def _existing_docx(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file() or candidate.suffix.lower() != ".docx":
        raise RepairEngineError(f"Readable .docx input required: {candidate}")
    validation = validate_output_package(candidate)
    if not validation["valid"]:
        raise RepairEngineError(
            "Input DOCX failed validation: " + "; ".join(validation["errors"])
        )
    return candidate


def _new_docx_path(path: str | os.PathLike[str], input_path: Path) -> Path:
    destination = Path(path).expanduser().resolve()
    if destination.suffix.lower() != ".docx":
        raise RepairEngineError(f"Output must use the .docx suffix: {destination}")
    if destination == input_path:
        raise RepairEngineError("Repair output must not overwrite its input")
    if destination.exists():
        raise RepairEngineError(
            f"Refusing to overwrite an existing repair output: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _load_audit(
    audit: RepairAudit | Mapping[str, Any] | str | os.PathLike[str],
) -> RepairAudit:
    if isinstance(audit, RepairAudit):
        return audit
    if isinstance(audit, Mapping):
        return RepairAudit.model_validate(audit)
    path = Path(audit).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RepairAudit.model_validate(data)
    except Exception as exc:
        raise RepairEngineError(f"Could not read repair audit {path}: {exc}") from exc


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(value: str, part_name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise RepairEngineError(f"Invalid base64 backup for {part_name}") from exc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    handle, name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(handle)
    temp = Path(name)
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
