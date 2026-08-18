from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from format_translator.docx_engine import (
    apply_translations,
    extract_translation_blocks,
)
from format_translator.models import TranslationResult
from format_translator.word_qa import run_docx_qa

def _write_docx_with_modern_and_vml_textboxes(path: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
    xmlns:v="urn:schemas-microsoft-com:vml">
  <w:body>
    <w:p>
      <w:r>
        <mc:AlternateContent>
          <mc:Choice Requires="wps">
            <w:drawing>
              <wp:anchor>
                <a:graphic>
                  <a:graphicData>
                    <wps:wsp>
                      <wps:txbx>
                        <w:txbxContent>
                          <w:p><w:r><w:t>Modern text box</w:t></w:r></w:p>
                        </w:txbxContent>
                      </wps:txbx>
                    </wps:wsp>
                  </a:graphicData>
                </a:graphic>
              </wp:anchor>
            </w:drawing>
          </mc:Choice>
          <mc:Fallback>
            <w:pict>
              <v:shape>
                <v:textbox>
                  <w:txbxContent>
                    <w:p><w:r><w:t>VML text box</w:t></w:r></w:p>
                  </w:txbxContent>
                </v:textbox>
              </v:shape>
            </w:pict>
          </mc:Fallback>
        </mc:AlternateContent>
      </w:r>
    </w:p>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document)


def _write_namespace_variant_docx(path: Path, *, add_unused_namespace: bool) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    unused_namespace = (
        ' xmlns:unused="urn:format-translator:unused"' if add_unused_namespace else ""
    )
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"{unused_namespace}>
  <w:body>
    <w:p><w:r><w:t>Hello</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>"""
    styles = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"{unused_namespace}>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
</w:styles>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document)
        package.writestr("word/styles.xml", styles)


def _write_fixed_height_table_docx(path: Path, text: str) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tblGrid><w:gridCol w:w="2400"/></w:tblGrid>
      <w:tr>
        <w:trPr><w:trHeight w:val="240" w:hRule="exact"/></w:trPr>
        <w:tc>
          <w:tcPr><w:tcW w:w="2400" w:type="dxa"/></w:tcPr>
          <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document)


def _write_fixed_width_nowrap_table_docx(path: Path, text: str) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tblPr>
        <w:tblW w:w="2400" w:type="dxa"/>
        <w:tblLayout w:type="fixed"/>
      </w:tblPr>
      <w:tblGrid><w:gridCol w:w="2400"/></w:tblGrid>
      <w:tr>
        <w:tc>
          <w:tcPr>
            <w:tcW w:w="2400" w:type="dxa"/>
            <w:noWrap/>
          </w:tcPr>
          <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document)


def _write_reflowable_body_docx(path: Path, text: str) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
  </w:body>
</w:document>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", relationships)
        package.writestr("word/document.xml", document)


def test_docx_qa_ignores_unused_xml_namespace_serialization_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.docx"
    candidate = tmp_path / "candidate.docx"
    _write_namespace_variant_docx(source, add_unused_namespace=False)
    _write_namespace_variant_docx(candidate, add_unused_namespace=True)

    result = run_docx_qa(source, candidate)

    assert result == []
    assert result.quality_score == 100


def test_docx_qa_resolves_modern_and_vml_textbox_block_xpaths(
    tmp_path: Path,
) -> None:
    document = tmp_path / "textboxes.docx"
    _write_docx_with_modern_and_vml_textboxes(document)

    result = run_docx_qa(document, document)

    assert result == []
    assert result.summary["source_blocks"] == 2
    assert result.summary["deep_comparison_completed"] is True


@pytest.mark.parametrize(
    "text",
    [
        "A sanitized space exploration report fixture.",
        "A sanitized telecommunications filing fixture.",
    ],
)
def test_identical_docx_has_perfect_native_quality(
    tmp_path: Path,
    text: str,
) -> None:
    document = tmp_path / "identical.docx"
    _write_reflowable_body_docx(document, text)

    result = run_docx_qa(document, document)

    assert result == []
    assert result.quality_score == 100
    assert result.summary["errors"] == 0
    assert result.summary["warnings"] == 0
    assert result.summary["deep_comparison_completed"] is True


def test_docx_qa_does_not_treat_reflowable_body_expansion_as_overflow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "reflowable-source.docx"
    _write_reflowable_body_docx(
        source,
        "A reflowable paragraph with enough source text for expansion testing.",
    )
    blocks = extract_translation_blocks(source)
    block = next(
        block
        for block in blocks
        if block.protocol == "plain" and len(block.source_text.strip()) >= 20
    )
    translation = TranslationResult(
        block_id=block.block_id,
        translated_text="扩" * (len(block.source_text.strip()) * 3),
        provider="fixture",
        model="expansion-test",
    )
    candidate = tmp_path / "expanded.docx"
    apply_translations(
        source,
        candidate,
        [translation],
        blocks=blocks,
    )

    result = run_docx_qa(
        source,
        candidate,
        blocks=[block],
        translations=[translation],
    )

    assert not any(issue.code == "docx_translation_expansion_risk" for issue in result)
    assert result.quality_score == 100


def test_docx_qa_uses_script_aware_capacity_for_fixed_height_row(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixed-row-source.docx"
    _write_fixed_height_table_docx(source, "年度工作报告测试")
    blocks = extract_translation_blocks(source)
    block = blocks[0]
    translation = TranslationResult(
        block_id=block.block_id,
        translated_text="abcdefghijklmnopqrst",
        provider="fixture",
        model="capacity-test",
    )
    candidate = tmp_path / "fixed-row-balanced.docx"
    apply_translations(source, candidate, [translation], blocks=blocks)

    result = run_docx_qa(
        source,
        candidate,
        blocks=blocks,
        translations=[translation],
    )

    assert len(translation.translated_text) / len(block.source_text) > 1.8
    assert not any(issue.code == "docx_translation_expansion_risk" for issue in result)


def test_docx_qa_detects_constrained_expansion_at_exact_block_location(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixed-row-source.docx"
    _write_fixed_height_table_docx(source, "年度工作报告测试")
    blocks = extract_translation_blocks(source)
    block = blocks[0]
    translation = TranslationResult(
        block_id=block.block_id,
        translated_text=(
            "Annual work report with substantially expanded explanatory wording"
        ),
        provider="fixture",
        model="expansion-test",
    )
    candidate = tmp_path / "fixed-row-expanded.docx"
    apply_translations(source, candidate, [translation], blocks=blocks)

    result = run_docx_qa(
        source,
        candidate,
        blocks=blocks,
        translations=[translation],
    )

    issue = next(
        issue for issue in result if issue.code == "docx_translation_expansion_risk"
    )
    assert issue.block_id == block.block_id
    assert issue.details["part_name"] == block.metadata["part_name"]
    assert issue.details["xpath"] == block.metadata["xpath"]
    assert issue.details["capacity_ratio"] >= 2.5
    assert issue.details["capacity_metric"] == "estimated_glyph_advance_em"
    assert issue.details["layout_context"]["kind"] == "fixed_table_row"
    assert issue.details["layout_context"]["constrained"] is True
    hint = issue.details["repair_hint"]
    assert hint["action_type"] == "fit_text"
    assert hint["automatic"] is True
    assert hint["block_id"] == block.block_id
    assert hint["scope"] == {
        "part_name": block.metadata["part_name"],
        "xpath": block.metadata["xpath"],
        "block_id": block.block_id,
    }
    assert hint["max_length"] < len(translation.translated_text)
    assert hint["constraints"]["preserve_meaning"] is True
    assert hint["constraints"]["forbid_text_deletion"] is True
    assert hint["constraints"]["forbid_page_geometry_change"] is True
    assert hint["constraints"]["forbid_global_font_change"] is True
    assert result.quality_score < 100


def test_docx_qa_detects_visual_expansion_in_fixed_width_nowrap_cell(
    tmp_path: Path,
) -> None:
    source_text = "年度工作报告测试"
    candidate_text = (
        "Annual work report with substantially expanded explanatory wording"
    )
    source = tmp_path / "fixed-width-source.docx"
    candidate = tmp_path / "fixed-width-candidate.docx"
    _write_fixed_width_nowrap_table_docx(source, source_text)
    _write_fixed_width_nowrap_table_docx(candidate, candidate_text)
    xpath = "/w:document/w:body/w:tbl/w:tr/w:tc/w:p"
    block = {
        "block_id": "nowrap-cell-1",
        "source_text": source_text,
        "metadata": {
            "part_name": "word/document.xml",
            "xpath": xpath,
        },
    }

    result = run_docx_qa(
        source,
        candidate,
        blocks=[block],
        translations={
            "nowrap-cell-1": {"translated_text": candidate_text},
        },
    )

    issue = next(
        issue for issue in result if issue.code == "docx_translation_expansion_risk"
    )
    assert issue.block_id == "nowrap-cell-1"
    assert issue.details["source_script"] == "han"
    assert issue.details["candidate_script"] == "latin"
    assert issue.details["capacity_metric"] == "estimated_glyph_advance_em"
    assert issue.details["capacity_ratio"] >= 2.5
    assert issue.details["layout_context"] == {
        "kind": "fixed_width_no_wrap_table_cell",
        "constrained": True,
        "evidence": [
            {
                "cell_no_wrap": True,
                "cell_width": {"value": "2400", "type": "dxa"},
                "table_layout": "fixed",
                "grid_column_widths": ["2400"],
            }
        ],
    }


def test_docx_qa_reads_max_length_hint_from_mapping_metadata(
    tmp_path: Path,
) -> None:
    source_text = "年度工作报告测试"
    candidate_text = "Annual report"
    source = tmp_path / "metadata-hint-source.docx"
    candidate = tmp_path / "metadata-hint-candidate.docx"
    _write_reflowable_body_docx(source, source_text)
    _write_reflowable_body_docx(candidate, candidate_text)
    xpath = "/w:document/w:body/w:p"
    block = {
        "block_id": "metadata-hint-1",
        "source_text": source_text,
        "metadata": {
            "part_name": "word/document.xml",
            "xpath": xpath,
            "max_length_hint": "8",
        },
    }

    result = run_docx_qa(
        source,
        candidate,
        blocks=[block],
        translations={
            "metadata-hint-1": {"translated_text": candidate_text},
        },
    )

    issue = next(
        issue for issue in result if issue.code == "docx_translation_expansion_risk"
    )
    assert issue.block_id == "metadata-hint-1"
    assert issue.details["max_length_hint"] == 8
    assert issue.details["layout_context"] == {
        "kind": "explicit_length_budget",
        "constrained": True,
        "evidence": [
            {
                "max_length_hint": 8,
                "candidate_length": len(candidate_text),
            }
        ],
    }
    assert issue.details["repair_hint"]["max_length"] <= 8


def test_docx_qa_allows_cjk_to_latin_expansion_in_reflowable_body(
    tmp_path: Path,
) -> None:
    source_text = "年度工作报告测试"
    candidate_text = (
        "Annual work report with substantially expanded explanatory wording"
    )
    source = tmp_path / "reflowable-source.docx"
    candidate = tmp_path / "reflowable-candidate.docx"
    _write_reflowable_body_docx(source, source_text)
    _write_reflowable_body_docx(candidate, candidate_text)
    block = {
        "block_id": "reflowable-body-1",
        "source_text": source_text,
        "metadata": {
            "part_name": "word/document.xml",
            "xpath": "/w:document/w:body/w:p",
        },
    }

    result = run_docx_qa(
        source,
        candidate,
        blocks=[block],
        translations={
            "reflowable-body-1": {"translated_text": candidate_text},
        },
    )

    assert not any(issue.code == "docx_translation_expansion_risk" for issue in result)
    assert result.quality_score == 100


@pytest.mark.parametrize(
    "invalid_hint",
    ["bad", 0, -4, 8.0, 3.5, True, False],
    ids=[
        "non-numeric-string",
        "zero",
        "negative",
        "integral-float",
        "fractional-float",
        "true",
        "false",
    ],
)
def test_docx_qa_ignores_invalid_metadata_max_length_hint(
    tmp_path: Path,
    invalid_hint: object,
) -> None:
    source_text = "年度工作报告测试"
    candidate_text = "Annual report"
    source = tmp_path / "invalid-hint-source.docx"
    candidate = tmp_path / "invalid-hint-candidate.docx"
    _write_reflowable_body_docx(source, source_text)
    _write_reflowable_body_docx(candidate, candidate_text)
    block = {
        "block_id": "invalid-hint-1",
        "source_text": source_text,
        "metadata": {
            "part_name": "word/document.xml",
            "xpath": "/w:document/w:body/w:p",
            "max_length_hint": invalid_hint,
        },
    }

    result = run_docx_qa(
        source,
        candidate,
        blocks=[block],
        translations={
            "invalid-hint-1": {"translated_text": candidate_text},
        },
    )

    assert not any(issue.code == "docx_translation_expansion_risk" for issue in result)
    assert result.quality_score == 100
