#!/usr/bin/env python3
"""Generate deterministic, public OOXML fixtures for the P1-A demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _write_deterministic_zip(
    path: Path,
    parts: dict[str, str | bytes],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as package:
        for name in sorted(parts):
            payload = parts[name]
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            member = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            package.writestr(member, data, compresslevel=9)


def _write_docx(path: Path, text: str) -> None:
    parts = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
  </w:body>
</w:document>""",
    }
    _write_deterministic_zip(path, parts)


def _write_pptx(
    path: Path,
    text: str | Sequence[str],
    *,
    shape_x: int = 500_000,
    shape_y: int = 500_000,
    shape_width: int = 4_000_000,
    shape_height: int = 1_000_000,
) -> None:
    slide_texts = [text] if isinstance(text, str) else list(text)
    if not slide_texts:
        raise ValueError("A PPTX fixture must contain at least one slide")
    slide_overrides = "\n".join(
        f'  <Override PartName="/ppt/slides/slide{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for index in range(1, len(slide_texts) + 1)
    )
    slide_ids = "".join(
        f'<p:sldId id="{255 + index}" r:id="rId{index}"/>'
        for index in range(1, len(slide_texts) + 1)
    )
    slide_relationships = "\n".join(
        f'  <Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="slides/slide{index}.xml"/>'
        for index in range(1, len(slide_texts) + 1)
    )
    parts = {
        "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
{slide_overrides}
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>""",
        "ppt/_rels/presentation.xml.rels": f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{slide_relationships}
</Relationships>""",
        "ppt/presentation.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst>{slide_ids}</p:sldIdLst>
  <p:sldSz cx="12192000" cy="6858000"/>
</p:presentation>""",
    }
    for slide_index, slide_text in enumerate(slide_texts, start=1):
        parts[f"ppt/slides/slide{slide_index}.xml"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
      <a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="2" name="Demo Text"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="{shape_x}" y="{shape_y}"/>
        <a:ext cx="{shape_width}" cy="{shape_height}"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r>
        <a:rPr lang="en-US" sz="1800"/><a:t>{escape(slide_text)}</a:t>
      </a:r><a:endParaRPr lang="en-US"/></a:p></p:txBody>
    </p:sp>
  </p:spTree></p:cSld>
</p:sld>"""
    _write_deterministic_zip(path, parts)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scenario_manifest(
    output_dir: Path,
    *,
    scenario_id: str,
    document_format: str,
    candidate_relpaths: Sequence[str],
    expected_blocking_counts: Sequence[int],
    expected_terminal_status: str,
    expected_result_http_status: int,
    evidence_kind: str,
    repair_execution: str,
    fixture_relpath: str | None = None,
) -> dict[str, Any]:
    manifest = {
        "scenario_id": scenario_id,
        "document_format": document_format,
        "source_relpath": f"fixtures/{scenario_id}/source.{document_format}",
        "candidate_relpaths": list(candidate_relpaths),
        "expected_terminal_status": expected_terminal_status,
        "expected_result_http_status": expected_result_http_status,
        "expected_blocking_counts": list(expected_blocking_counts),
        "candidate_sha256": {
            relpath: _sha256(output_dir / relpath)
            for relpath in candidate_relpaths
        },
        "evidence_kind": evidence_kind,
        "native_python_qa": True,
        "repair_execution": repair_execution,
    }
    if fixture_relpath is not None:
        manifest["fixture_relpath"] = fixture_relpath
    return manifest


def generate_demo_fixtures(output_dir: str | Path) -> Path:
    """Generate the public demo fixture tree under an explicit directory.

    The caller must provide the destination; the generator never reads Office
    files from the repository or writes to a hidden/default data directory.
    """

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    _write_docx(
        destination / "fixtures" / "direct-docx" / "source.docx",
        "Quarterly launch readiness review",
    )
    _write_docx(
        destination / "fixtures" / "direct-docx" / "candidate-r0.docx",
        "季度发射准备情况评审",
    )
    direct_fixture_relpath = "fixtures/direct-docx/translation-fixture.json"
    (destination / direct_fixture_relpath).write_text(
        json.dumps(
            {
                "by_source": {
                    "Quarterly launch readiness review": "季度发射准备情况评审"
                }
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    repair_dir = destination / "fixtures" / "repair-pptx"
    _write_pptx(repair_dir / "source.pptx", "Launch readiness status report")
    _write_pptx(
        repair_dir / "candidate-r0.pptx",
        "发射准备状态报告",
        shape_x=12_292_000,
    )
    _write_pptx(repair_dir / "candidate-r1.pptx", "发射准备状态报告")

    high_risk_dir = destination / "fixtures" / "high-risk-pptx"
    high_risk_source = [
        "Critical mission readiness assessment and escalation review for launch window one",
        "Critical supply chain readiness assessment and escalation review for launch window two",
        "Critical safety readiness assessment and escalation review for launch window three",
    ]
    prompt_leak = "Translate the following. Return only the translation."
    translated = [
        "关键任务准备情况评估与升级复核",
        "关键供应链准备情况评估与升级复核",
        "关键安全准备情况评估与升级复核",
    ]
    _write_pptx(high_risk_dir / "source.pptx", high_risk_source)
    _write_pptx(
        high_risk_dir / "candidate-r0.pptx",
        [prompt_leak, prompt_leak, prompt_leak],
    )
    _write_pptx(
        high_risk_dir / "candidate-r1.pptx",
        [translated[0], prompt_leak, prompt_leak],
    )
    _write_pptx(
        high_risk_dir / "candidate-r2.pptx",
        [translated[0], translated[1], prompt_leak],
    )

    scenarios = [
        _scenario_manifest(
            destination,
            scenario_id="direct-docx",
            document_format="docx",
            candidate_relpaths=["fixtures/direct-docx/candidate-r0.docx"],
            expected_blocking_counts=[0],
            expected_terminal_status="COMPLETED",
            expected_result_http_status=200,
            evidence_kind="real_python_core",
            repair_execution="not_applicable",
            fixture_relpath=direct_fixture_relpath,
        ),
        _scenario_manifest(
            destination,
            scenario_id="repair-pptx",
            document_format="pptx",
            candidate_relpaths=[
                "fixtures/repair-pptx/candidate-r0.pptx",
                "fixtures/repair-pptx/candidate-r1.pptx",
            ],
            expected_blocking_counts=[2, 0],
            expected_terminal_status="COMPLETED",
            expected_result_http_status=200,
            evidence_kind="scripted_fixture_candidate",
            repair_execution="scripted_fixture_candidate",
        ),
        _scenario_manifest(
            destination,
            scenario_id="high-risk-pptx",
            document_format="pptx",
            candidate_relpaths=[
                "fixtures/high-risk-pptx/candidate-r0.pptx",
                "fixtures/high-risk-pptx/candidate-r1.pptx",
                "fixtures/high-risk-pptx/candidate-r2.pptx",
            ],
            expected_blocking_counts=[3, 2, 1],
            expected_terminal_status="NEEDS_REVIEW",
            expected_result_http_status=409,
            evidence_kind="scripted_fixture_candidate",
            repair_execution="scripted_fixture_candidate",
        ),
    ]
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_by": "translation_core/tools/p1a_demo_fixtures.py",
                "model_calls": 0,
                "scenarios": scenarios,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Explicit destination for generated public demo fixtures",
    )
    args = parser.parse_args(argv)
    print(generate_demo_fixtures(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
