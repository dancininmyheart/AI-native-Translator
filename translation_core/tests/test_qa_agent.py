from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from format_translator.docx_engine import (
    apply_translations,
    extract_translation_blocks,
)
from format_translator.models import QaIssue, TranslationResult
from format_translator.providers import ProviderProtocolError
from format_translator.qa_agent import (
    QaAgentState,
    QaRepairSpec,
    _actions_are_repairable,
    _atomic_restore,
    _has_regression,
    run_qa_repair_agent,
    run_qa_repair_agent_async,
)
from test_pptx_engine import SOURCE_TEXT, _write_pptx, _write_table_pptx


class OfficeQaRunner:
    def __init__(self, quality_units: dict[Path, int]) -> None:
        self.quality_units = quality_units
        self.calls: list[tuple[str, Path, Path]] = []

    def run_docx_qa(
        self,
        source_document: Path,
        candidate_document: Path,
        **_kwargs: Any,
    ) -> list[QaIssue]:
        return self._run("docx", source_document, candidate_document)

    def run_pptx_qa(
        self,
        source_document: Path,
        candidate_document: Path,
        **_kwargs: Any,
    ) -> list[QaIssue]:
        return self._run("pptx", source_document, candidate_document)

    def _run(
        self,
        document_format: str,
        source_document: Path,
        candidate_document: Path,
    ) -> list[QaIssue]:
        source = Path(source_document)
        candidate = Path(candidate_document)
        self.calls.append((document_format, source, candidate))
        expected = self.quality_units[source.resolve()]
        actual = self.quality_units[candidate.resolve()]
        return [
            QaIssue(
                issue_id=f"{document_format}-difference-{index}",
                code=f"{document_format.upper()}_LAYOUT_DIFFERENCE",
                message=f"Office layout unit {index} differs.",
                block_id=f"{document_format}-block-{index}",
                page=index if document_format == "docx" else None,
                slide=index if document_format == "pptx" else None,
                details={
                    "source_units": expected,
                    "candidate_units": actual,
                    "office_location": {
                        "page": index if document_format == "docx" else None,
                        "slide": index if document_format == "pptx" else None,
                        "shape": index if document_format == "pptx" else None,
                    },
                },
            )
            for index in range(1, abs(expected - actual) + 1)
        ]


class TextQaAwareRunner:
    """Exercise the QA runner contract used when extraction inputs are omitted."""

    def __init__(self) -> None:
        self.block_arguments: list[Any] = []

    def run_docx_qa(
        self,
        source_document: Path,
        candidate_document: Path,
        *,
        blocks: Any = None,
        **_kwargs: Any,
    ) -> list[QaIssue]:
        del source_document, candidate_document
        self.block_arguments.append(blocks)
        if blocks is not None:
            return []
        return [
            QaIssue(
                code="DOCX_TEXT_QA_EXECUTED",
                message="Default block extraction found a text-level issue.",
                block_id="detected-by-runner",
            )
        ]


class FakeOfficeRepairEngine:
    def __init__(
        self,
        output_units: list[int],
        quality_units: dict[Path, int],
        *,
        unsafe_action: bool = False,
        mutate_output: bool = True,
    ) -> None:
        self.output_units = list(output_units)
        self.quality_units = quality_units
        self.unsafe_action = unsafe_action
        self.mutate_output = mutate_output
        self.calls: list[dict[str, Any]] = []

    def plan(self, **_kwargs: Any) -> dict[str, Any]:
        action = (
            "delete_all_document_content"
            if self.unsafe_action
            else "replace_translation"
        )
        return {
            "repairable": True,
            "actions": [
                {
                    "action": action,
                    "block_id": "office-block-1",
                    "reason": "controlled fixture repair",
                }
            ],
        }

    def apply(
        self,
        source_path: Path,
        output_document: Path,
        actions: list[dict[str, Any]],
    ) -> dict[str, str]:
        self.calls.append(
            {
                "source_path": Path(source_path),
                "destination_path": Path(output_document),
                "actions": actions,
            }
        )
        shutil.copy2(source_path, output_document)
        if self.mutate_output:
            with zipfile.ZipFile(
                output_document,
                "a",
                zipfile.ZIP_DEFLATED,
            ) as package:
                package.writestr(
                    f"docProps/repair-fixture-{len(self.calls)}.xml",
                    "<repair-fixture/>",
                )
        self.quality_units[Path(output_document).resolve()] = self.output_units.pop(0)
        return {"output_path": str(output_document)}


def _write_minimal_docx(path: Path, document_xml: str | None = None) -> None:
    document = document_xml or """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Sanitized QA fixture text.</w:t></w:r></w:p>
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>"""
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
        "word/document.xml": document,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, payload in parts.items():
            package.writestr(name, payload)


def _spec(
    tmp_path: Path,
    *,
    suffix: str,
    source_units: int,
    candidate_units: int,
    max_rounds: int = 2,
) -> tuple[QaRepairSpec, dict[Path, int]]:
    source = tmp_path / f"source.{suffix}"
    candidate = tmp_path / f"candidate.{suffix}"
    if suffix == "docx":
        _write_minimal_docx(source)
    elif suffix == "pptx":
        _write_pptx(source)
    else:
        raise AssertionError(f"unsupported Office fixture suffix: {suffix}")
    shutil.copy2(source, candidate)
    spec = QaRepairSpec(
        job_id=f"{suffix}-qa",
        source_document=source,
        candidate_document=candidate,
        work_dir=tmp_path / "work",
        max_rounds=max_rounds,
        strict_page_count=True,
    )
    return spec, {
        source.resolve(): source_units,
        candidate.resolve(): candidate_units,
    }


def _write_tagged_repair_docx(path: Path) -> None:
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:bookmarkStart w:id="0" w:name="unit-title"/>
      <w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t xml:space="preserve">_____________ (Department) </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>Public </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>Information </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>Coordinator </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>Register</w:t></w:r>
      <w:bookmarkEnd w:id="0"/>
    </w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">3. Please submit by </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>March 31 at 18:00</w:t></w:r>
      <w:r><w:t xml:space="preserve"> using the internal records portal</w:t></w:r>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
"""
    _write_minimal_docx(path, document_xml)


def _tagged_translation(
    block: Any,
    segment_texts: list[str],
) -> TranslationResult:
    assert len(block.segments) == len(segment_texts)
    segments = [
        segment.model_copy(update={"text": text})
        for segment, text in zip(block.segments, segment_texts)
    ]
    return TranslationResult(
        block_id=block.block_id,
        translated_text="".join(segment_texts),
        segments=segments,
        provider="tagged-repair-fixture",
    )


def _tagged_repair_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, list[Any], Any, Any, TranslationResult, TranslationResult]:
    source = tmp_path / "source.docx"
    candidate = tmp_path / "candidate.docx"
    _write_tagged_repair_docx(source)
    blocks = extract_translation_blocks(source)
    title = next(block for block in blocks if block.source_text.startswith("___"))
    deadline = next(block for block in blocks if block.source_text.startswith("3."))
    title_translation = _tagged_translation(
        title,
        [
            "_____________ (Unit) ",
            "Information Disclosure ",
            "Work ",
            "Supervising Leaders and Information Officers ",
            "Statistics Form",
        ],
    )
    deadline_translation = _tagged_translation(
        deadline,
        [
            "3. Please submit by ",
            "March 31 at 18:00",
            (
                " the electronic copy via OA memo to Lou Yuzhen, Information "
                "Supervision Section, School Office"
            ),
            ".",
        ],
    )
    apply_translations(
        source,
        candidate,
        [title_translation, deadline_translation],
        blocks=blocks,
    )
    return (
        source,
        candidate,
        blocks,
        title,
        deadline,
        title_translation,
        deadline_translation,
    )


def test_public_qa_repair_contract_has_no_pdf_fields() -> None:
    spec_fields = {field.name for field in dataclasses.fields(QaRepairSpec)}
    signature = inspect.signature(run_qa_repair_agent)

    assert {"source_document", "candidate_document"} <= spec_fields
    assert "source_pdf" not in spec_fields
    assert "candidate_pdf" not in spec_fields
    assert "source_pdf" not in signature.parameters
    assert "candidate_pdf" not in signature.parameters


def test_async_qa_agent_keeps_event_loop_responsive_for_sync_runner(
    tmp_path: Path,
) -> None:
    spec, _quality_units = _spec(
        tmp_path,
        suffix="docx",
        source_units=1,
        candidate_units=1,
        max_rounds=0,
    )
    timing: dict[str, float] = {}

    def slow_qa_runner(**_kwargs: Any) -> list[QaIssue]:
        timing["runner_started"] = time.perf_counter()
        time.sleep(0.1)
        timing["runner_finished"] = time.perf_counter()
        return []

    async def observe_event_loop() -> None:
        await asyncio.sleep(0.02)
        timing["event_loop_tick"] = time.perf_counter()

    async def scenario() -> Any:
        return await asyncio.gather(
            run_qa_repair_agent_async(spec, qa_runner=slow_qa_runner),
            observe_event_loop(),
        )

    report, _ = asyncio.run(scenario())

    assert report.status == QaAgentState.COMPLETED
    assert timing["runner_started"] < timing["event_loop_tick"]
    assert timing["event_loop_tick"] < timing["runner_finished"]


def test_qa_agent_preserves_omitted_blocks_for_runner_default_extraction(
    tmp_path: Path,
) -> None:
    spec, _quality_units = _spec(
        tmp_path,
        suffix="docx",
        source_units=1,
        candidate_units=1,
        max_rounds=0,
    )
    runner = TextQaAwareRunner()

    report = run_qa_repair_agent(spec, qa_runner=runner)

    assert runner.block_arguments == [None]
    assert report.status == QaAgentState.NEEDS_REVIEW
    assert [issue["code"] for issue in report.accepted_issues] == [
        "DOCX_TEXT_QA_EXECUTED"
    ]


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
def test_qa_agent_completes_docx_and_pptx_without_pdf(
    tmp_path: Path,
    suffix: str,
) -> None:
    spec, quality_units = _spec(
        tmp_path,
        suffix=suffix,
        source_units=3,
        candidate_units=1,
        max_rounds=1,
    )
    runner = OfficeQaRunner(quality_units)
    engine = FakeOfficeRepairEngine([3], quality_units)

    report = run_qa_repair_agent(
        spec,
        repair_engine=engine,
        qa_runner=runner,
    )

    assert report.status == QaAgentState.COMPLETED
    assert report.accepted_document.suffix == f".{suffix}"
    assert quality_units[report.accepted_document.resolve()] == 3
    assert len(engine.calls) == 1
    assert engine.calls[0]["destination_path"] != spec.candidate_document
    assert engine.calls[0]["destination_path"].suffix == f".{suffix}"
    repair_relative = engine.calls[0]["destination_path"].relative_to(spec.work_dir)
    assert len(str(repair_relative)) <= 48
    assert {call[0] for call in runner.calls} == {suffix}
    serialized = report.to_dict()
    assert "pdf" not in json.dumps(serialized).casefold()


def test_qa_agent_only_accepts_strict_improvement_and_rolls_back_regression(
    tmp_path: Path,
) -> None:
    spec, quality_units = _spec(
        tmp_path,
        suffix="docx",
        source_units=3,
        candidate_units=2,
        max_rounds=1,
    )
    original = spec.candidate_document.read_bytes()
    runner = OfficeQaRunner(quality_units)
    engine = FakeOfficeRepairEngine([0], quality_units)

    report = run_qa_repair_agent(
        spec,
        repair_engine=engine,
        qa_runner=runner,
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    assert report.accepted_document == spec.candidate_document.resolve()
    assert spec.candidate_document.read_bytes() == original
    assert report.rounds[0]["accepted"] is False
    assert report.rounds[0]["rolled_back"] is True
    assert report.rounds[0]["after_score"] < report.rounds[0]["before_score"]


def test_precomputed_semantic_baseline_is_not_rerun_and_noop_is_rejected(
    tmp_path: Path,
) -> None:
    spec, quality_units = _spec(
        tmp_path,
        suffix="docx",
        source_units=2,
        candidate_units=1,
        max_rounds=1,
    )
    runner = OfficeQaRunner(quality_units)
    engine = FakeOfficeRepairEngine(
        [2],
        quality_units,
        mutate_output=False,
    )
    semantic_issue = QaIssue(
        issue_id="semantic-omission-1",
        code="SEMANTIC_OMISSION",
        message="The translated candidate omitted a source statement.",
        block_id="docx-block-1",
        details={"origin": "persisted-semantic-qa"},
    )

    report = run_qa_repair_agent(
        spec,
        repair_engine=engine,
        qa_runner=runner,
        baseline_issues=[semantic_issue],
    )

    assert len(runner.calls) == 1
    assert runner.calls[0][2] != spec.candidate_document.resolve()
    assert report.rounds[0]["before_issues"] == [semantic_issue.model_dump(mode="json")]
    assert report.rounds[0]["identical_to_input"] is True
    assert report.rounds[0]["outcome"] == "rejected_no_change"
    assert report.rounds[0]["accepted"] is False
    assert report.status == QaAgentState.NEEDS_REVIEW
    assert report.accepted_document == spec.candidate_document.resolve()


def test_processing_planner_receives_its_typed_diagnosis(tmp_path: Path) -> None:
    spec, quality_units = _spec(
        tmp_path,
        suffix="docx",
        source_units=2,
        candidate_units=1,
        max_rounds=1,
    )

    class Diagnosis:
        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {"summary": "typed diagnosis"}

    class ProcessingProvider:
        def __init__(self) -> None:
            self.diagnosis = Diagnosis()
            self.planner_input: Any = None

        async def diagnose(self, **_kwargs: Any) -> Diagnosis:
            return self.diagnosis

        async def plan(self, *, diagnosis: Any, **_kwargs: Any) -> dict[str, Any]:
            diagnosis.model_dump(mode="json")
            self.planner_input = diagnosis
            return {"actions": [], "needs_human_review": True}

    provider = ProcessingProvider()
    report = run_qa_repair_agent(
        spec,
        qa_runner=OfficeQaRunner(quality_units),
        processing_provider=provider,
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    assert provider.planner_input is provider.diagnosis
    assert report.rounds[0]["outcome"] == "needs_review"


def test_semantic_label_drift_at_same_location_is_not_a_new_regression() -> None:
    before = [
        QaIssue(
            code="OMISSION_VERTICAL_TEST_STAND",
            message="Vertical test stand wording is missing.",
            stage="semantic_qa",
            block_id="docx:p:7",
        )
    ]
    relabeled_same_location = [
        QaIssue(
            code="omission",
            message="A statement is still missing.",
            stage="semantic_qa",
            block_id="docx:p:7",
        )
    ]
    genuinely_new_location = [
        *relabeled_same_location,
        QaIssue(
            code="omission",
            message="Another statement is missing.",
            stage="semantic_qa",
            block_id="docx:p:9",
        ),
    ]

    assert _has_regression(before, relabeled_same_location) is False
    assert _has_regression(before, genuinely_new_location) is True


def test_qa_agent_rejects_new_overlap_object_at_same_text_shape(
    tmp_path: Path,
) -> None:
    spec, quality_units = _spec(
        tmp_path,
        suffix="pptx",
        source_units=1,
        candidate_units=1,
        max_rounds=1,
    )
    original_candidate = spec.candidate_document.resolve()
    before = [
        QaIssue(
            code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
            message="Text overlaps object A.",
            slide=1,
            details={
                "shape_id": "17",
                "region_id": "shape",
                "object_scope": "master",
                "object_shape_id": "41",
                "object_identity": "master:shape:41",
            },
        ),
        QaIssue(
            code="PPTX_TRANSLATION_EXPANSION_RISK",
            message="Translation expansion may affect layout.",
            severity="warning",
            slide=1,
            details={"shape_id": "17"},
        ),
    ]
    after = [
        QaIssue(
            code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
            message="Text now overlaps object B.",
            slide=1,
            details={
                "shape_id": "17",
                "region_id": "shape",
                "object_scope": "master",
                "object_shape_id": "42",
                "object_identity": "master:shape:42",
            },
        )
    ]

    def qa_runner(
        source_document: Path,
        candidate_document: Path,
        **_kwargs: Any,
    ) -> list[QaIssue]:
        del source_document
        if Path(candidate_document).resolve() == original_candidate:
            return before
        return after

    report = run_qa_repair_agent(
        spec,
        repair_engine=FakeOfficeRepairEngine([1], quality_units),
        qa_runner=qa_runner,
    )

    assert report.rounds[0]["after_score"] > report.rounds[0]["before_score"]
    assert report.rounds[0]["regression"] is True
    assert report.rounds[0]["outcome"] == "rejected_regression"
    assert report.accepted_document == original_candidate


def test_overlap_fingerprint_matches_legacy_inherited_object_aliases() -> None:
    before = [
        QaIssue(
            code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
            message="Legacy overlap evidence.",
            slide=3,
            details={
                "shape_id": "17",
                "region_id": "shape",
                "inherited_scope": "layout",
                "inherited_shape_id": "41",
            },
        )
    ]
    after = [
        QaIssue(
            code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
            message="Current overlap evidence.",
            slide=3,
            details={
                "shape_id": "17",
                "region_id": "shape",
                "object_scope": "layout",
                "object_shape_id": "41",
                "object_identity": "layout:shape:41",
            },
        )
    ]

    assert _has_regression(before, after) is False


@pytest.mark.parametrize(
    "changed_issue",
    [
        QaIssue(
            code="PPTX_RENDERED_LINE_BREAK_VIOLATION",
            message="A different failure appeared at the same location.",
            stage="pptx_qa",
            slide=3,
            details={"shape_id": "17", "cell_row": 2, "cell_column": 4},
        ),
        QaIssue(
            code="PPTX_TEXT_CAPACITY_RISK",
            message="The same failure moved to a different shape.",
            stage="pptx_qa",
            slide=3,
            details={"shape_id": "18", "cell_row": 2, "cell_column": 4},
        ),
        QaIssue(
            code="PPTX_TEXT_CAPACITY_RISK",
            message="The same failure moved to a different table cell.",
            stage="pptx_qa",
            slide=3,
            details={"shape_id": "17", "cell_row": 2, "cell_column": 5},
        ),
    ],
    ids=["different-code", "different-shape", "different-cell"],
)
def test_office_issue_fingerprint_detects_precise_location_regressions(
    changed_issue: QaIssue,
) -> None:
    before = [
        QaIssue(
            code="PPTX_TEXT_CAPACITY_RISK",
            message="Baseline table-cell capacity failure.",
            stage="pptx_qa",
            slide=3,
            details={"shape_id": "17", "cell_row": 2, "cell_column": 4},
        )
    ]

    assert _has_regression(before, [changed_issue]) is True


def test_atomic_restore_uses_bounded_temporary_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "backup.pptx"
    backup.write_bytes(b"restored")
    destination = tmp_path / (("d" * 215) + ".pptx")
    calls: dict[str, Path] = {}

    def copy2(source: Path, temporary: Path) -> None:
        assert Path(source) == backup
        calls["temporary"] = Path(temporary)

    def replace(temporary: Path, restored_destination: Path) -> None:
        assert Path(temporary) == calls["temporary"]
        assert Path(restored_destination) == destination

    monkeypatch.setattr(shutil, "copy2", copy2)
    monkeypatch.setattr("format_translator.qa_agent.os.replace", replace)

    _atomic_restore(backup, destination)

    assert calls["temporary"].parent == destination.parent
    assert len(calls["temporary"].name) <= 64


def test_qa_agent_uses_new_office_file_each_round_and_stops_at_limit(
    tmp_path: Path,
) -> None:
    spec, quality_units = _spec(
        tmp_path,
        suffix="pptx",
        source_units=4,
        candidate_units=1,
        max_rounds=2,
    )
    runner = OfficeQaRunner(quality_units)
    engine = FakeOfficeRepairEngine([2, 3], quality_units)

    report = run_qa_repair_agent(
        spec,
        repair_engine=engine,
        qa_runner=runner,
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    assert len(engine.calls) == spec.max_rounds
    destinations = [call["destination_path"] for call in engine.calls]
    assert len(set(destinations)) == spec.max_rounds
    assert engine.calls[1]["source_path"] == destinations[0]
    assert report.accepted_document == destinations[-1]
    assert all(round_["accepted"] for round_ in report.rounds)


def test_qa_agent_rejects_non_whitelisted_action_before_engine(
    tmp_path: Path,
) -> None:
    spec, quality_units = _spec(
        tmp_path,
        suffix="pptx",
        source_units=2,
        candidate_units=1,
        max_rounds=1,
    )
    engine = FakeOfficeRepairEngine(
        [2],
        quality_units,
        unsafe_action=True,
    )

    report = run_qa_repair_agent(
        spec,
        repair_engine=engine,
        qa_runner=OfficeQaRunner(quality_units),
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    assert engine.calls == []
    persisted = json.loads(report.report_path.read_text(encoding="utf-8"))
    assert persisted == report.to_dict()
    assert "pdf" not in json.dumps(persisted).casefold()


def test_qa_agent_executes_safe_repairs_alongside_review_metadata(
    tmp_path: Path,
) -> None:
    spec, _quality_units = _spec(
        tmp_path,
        suffix="docx",
        source_units=2,
        candidate_units=1,
        max_rounds=1,
    )
    provider_issue = QaIssue(
        issue_id="semantic-provider-unavailable",
        code="SEMANTIC_QA_PROVIDER_FAILED",
        message="One block was not semantically verified.",
        severity="error",
        stage="semantic_qa",
        recoverable=True,
    )
    layout_issue = QaIssue(
        issue_id="docx-layout-risk",
        code="docx_translation_expansion_risk",
        message="Translated paragraph may expand.",
        severity="error",
        stage="DOCX_QA",
        block_id="office-block-1",
        recoverable=True,
    )

    class MixedActionEngine(FakeOfficeRepairEngine):
        def plan(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repairable": True,
                "actions": [
                    {
                        "action": "replace_translation",
                        "block_id": "office-block-1",
                        "proposed_text": "Concise translated text.",
                    },
                    {
                        "action": "flag_for_review",
                        "issue_ids": [provider_issue.issue_id],
                        "reason": "Semantic QA provider is unavailable.",
                    },
                ],
            }

    engine = MixedActionEngine([2], {})

    def qa_runner(candidate_document: Path, **_kwargs: Any) -> list[QaIssue]:
        if Path(candidate_document).resolve() == spec.candidate_document.resolve():
            return [layout_issue, provider_issue]
        return [provider_issue]

    report = run_qa_repair_agent(
        spec,
        repair_engine=engine,
        qa_runner=qa_runner,
    )

    assert len(engine.calls) == 1
    assert [action["action"] for action in engine.calls[0]["actions"]] == [
        "replace_translation"
    ]
    assert report.accepted_document != spec.candidate_document.resolve()
    assert report.status == QaAgentState.NEEDS_REVIEW
    assert [issue["code"] for issue in report.accepted_issues] == [
        "SEMANTIC_QA_PROVIDER_FAILED"
    ]


def test_qa_agent_review_metadata_only_does_not_create_empty_candidate(
    tmp_path: Path,
) -> None:
    spec, _quality_units = _spec(
        tmp_path,
        suffix="docx",
        source_units=1,
        candidate_units=1,
        max_rounds=1,
    )
    provider_issue = QaIssue(
        issue_id="semantic-provider-unavailable",
        code="SEMANTIC_QA_PROVIDER_FAILED",
        message="Semantic QA is unavailable.",
        severity="error",
        stage="semantic_qa",
        recoverable=True,
    )

    class ReviewOnlyEngine(FakeOfficeRepairEngine):
        def plan(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repairable": False,
                "actions": [
                    {
                        "action": "flag_for_review",
                        "issue_ids": [provider_issue.issue_id],
                        "reason": "Semantic QA provider is unavailable.",
                    }
                ],
            }

    engine = ReviewOnlyEngine([1], {})
    report = run_qa_repair_agent(
        spec,
        repair_engine=engine,
        qa_runner=lambda **_kwargs: [provider_issue],
    )

    round_record = report.rounds[0]
    assert engine.calls == []
    assert report.status == QaAgentState.NEEDS_REVIEW
    assert report.accepted_document == spec.candidate_document.resolve()
    assert not Path(round_record["candidate_document"]).exists()
    assert "repair_audit" not in round_record
    assert round_record["outcome"] == "needs_review"
    assert round_record["non_executable_actions"][0]["action"] == "flag_for_review"


def test_qa_agent_repairs_tagged_docx_runs_without_losing_protected_content(
    tmp_path: Path,
) -> None:
    (
        source,
        candidate,
        blocks,
        title,
        deadline,
        title_translation,
        deadline_translation,
    ) = _tagged_repair_fixture(tmp_path)
    assert title.protocol == "tagged"
    assert deadline.protocol == "tagged"
    initial_blocks = {
        block.block_id: block for block in extract_translation_blocks(candidate)
    }
    proposed_title = (
        "_____________ (Unit) Information Disclosure Supervisors and Officers "
        "Statistical Table"
    )
    proposed_deadline = (
        "3. By March 31 at 18:00, submit the electronic copy via OA memo to "
        "Lou Yuzhen, Information Supervision Section, School Office."
    )
    provider_issue = QaIssue(
        issue_id="semantic-provider-unavailable",
        code="SEMANTIC_QA_PROVIDER_FAILED",
        message="One block was not semantically verified.",
        severity="error",
        stage="semantic_qa",
        recoverable=True,
    )
    repair_issues = [
        QaIssue(
            issue_id=f"expansion-{block.block_id}",
            code="docx_translation_expansion_risk",
            message="Translated paragraph may expand.",
            severity="error",
            stage="DOCX_QA",
            block_id=block.block_id,
            recoverable=True,
        )
        for block in (title, deadline)
    ]

    class TaggedPlanProvider:
        def diagnose(self, **_kwargs: Any) -> dict[str, Any]:
            return {"repairable": True}

        def plan(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repairable": True,
                "actions": [
                    {
                        "action": "replace_translation",
                        "block_id": title.block_id,
                        "proposed_text": proposed_title,
                    },
                    {
                        "action": "replace_translation",
                        "block_id": deadline.block_id,
                        "proposed_text": proposed_deadline,
                    },
                    {
                        "action": "flag_for_review",
                        "issue_ids": [provider_issue.issue_id],
                    },
                ],
            }

    def qa_runner(candidate_document: Path, **_kwargs: Any) -> list[QaIssue]:
        translated = {
            block.block_id: block.source_text
            for block in extract_translation_blocks(candidate_document)
        }
        if (
            translated.get(title.block_id) == proposed_title
            and translated.get(deadline.block_id) == proposed_deadline
        ):
            return [provider_issue]
        return [*repair_issues, provider_issue]

    report = run_qa_repair_agent(
        {
            "job_id": "tagged-docx-repair",
            "source_document": source,
            "candidate_document": candidate,
            "work_dir": tmp_path / "work",
            "max_rounds": 1,
            "blocks": blocks,
            "translations": [title_translation, deadline_translation],
        },
        processing_provider=TaggedPlanProvider(),
        qa_runner=qa_runner,
        baseline_issues=[*repair_issues, provider_issue],
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    assert report.failure is None
    assert report.accepted_document != candidate.resolve()
    assert [issue["code"] for issue in report.accepted_issues] == [
        "SEMANTIC_QA_PROVIDER_FAILED"
    ]
    repaired = {
        block.block_id: block
        for block in extract_translation_blocks(report.accepted_document)
    }
    for original in (initial_blocks[title.block_id], initial_blocks[deadline.block_id]):
        updated = repaired[original.block_id]
        assert [segment.segment_id for segment in updated.segments] == [
            segment.segment_id for segment in original.segments
        ]
        assert [segment.style_id for segment in updated.segments] == [
            segment.style_id for segment in original.segments
        ]
        assert [segment.metadata for segment in updated.segments] == [
            segment.metadata for segment in original.segments
        ]
    assert repaired[title.block_id].segments[0].text.startswith("_____________")
    deadline_segments = repaired[deadline.block_id].segments
    assert "March 31" in deadline_segments[1].text
    assert "18:00" in deadline_segments[1].text
    assert "Lou Yuzhen" in repaired[deadline.block_id].source_text


def test_qa_agent_blocks_unsafe_tagged_date_but_repairs_other_actions(
    tmp_path: Path,
) -> None:
    (
        source,
        candidate,
        blocks,
        title,
        deadline,
        title_translation,
        deadline_translation,
    ) = _tagged_repair_fixture(tmp_path)
    proposed_title = (
        "_____________ (Unit) Information Disclosure Supervisors and Officers "
        "Statistical Table"
    )
    unsafe_deadline = (
        "3. By 31 at 18:00, submit the electronic copy via OA memo to Lou "
        "Yuzhen, Information Supervision Section, School Office."
    )
    title_issue = QaIssue(
        issue_id="title-expansion",
        code="docx_translation_expansion_risk",
        message="Title may expand.",
        severity="error",
        block_id=title.block_id,
    )
    deadline_issue = QaIssue(
        issue_id="deadline-expansion",
        code="docx_translation_expansion_risk",
        message="Deadline paragraph may expand.",
        severity="error",
        block_id=deadline.block_id,
    )
    provider_issue = QaIssue(
        issue_id="semantic-provider-unavailable",
        code="SEMANTIC_QA_PROVIDER_FAILED",
        message="One block was not semantically verified.",
        severity="error",
        stage="semantic_qa",
    )

    class UnsafeDatePlanProvider:
        def diagnose(self, **_kwargs: Any) -> dict[str, Any]:
            return {"repairable": True}

        def plan(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repairable": True,
                "actions": [
                    {
                        "action": "replace_translation",
                        "block_id": title.block_id,
                        "proposed_text": proposed_title,
                    },
                    {
                        "action": "replace_translation",
                        "block_id": deadline.block_id,
                        "proposed_text": unsafe_deadline,
                    },
                    {"action": "flag_for_review"},
                ],
            }

    def qa_runner(candidate_document: Path, **_kwargs: Any) -> list[QaIssue]:
        translated = {
            block.block_id: block.source_text
            for block in extract_translation_blocks(candidate_document)
        }
        if (
            translated.get(title.block_id) == proposed_title
            and translated.get(deadline.block_id)
            == deadline_translation.translated_text
        ):
            return [deadline_issue, provider_issue]
        return [title_issue, deadline_issue, provider_issue]

    report = run_qa_repair_agent(
        {
            "job_id": "tagged-docx-unsafe-date",
            "source_document": source,
            "candidate_document": candidate,
            "work_dir": tmp_path / "work",
            "max_rounds": 1,
            "blocks": blocks,
            "translations": [title_translation, deadline_translation],
        },
        processing_provider=UnsafeDatePlanProvider(),
        qa_runner=qa_runner,
        baseline_issues=[title_issue, deadline_issue, provider_issue],
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    assert report.failure is None
    assert report.accepted_document != candidate.resolve()
    repaired = {
        block.block_id: block.source_text
        for block in extract_translation_blocks(report.accepted_document)
    }
    assert repaired[title.block_id] == proposed_title
    assert repaired[deadline.block_id] == deadline_translation.translated_text
    assert "March 31" in repaired[deadline.block_id]
    blocked = {
        action["block_id"]: action
        for action in report.rounds[0]["non_executable_actions"]
        if action.get("block_id")
    }
    assert blocked[deadline.block_id]["blocked"] is True
    assert (
        "missing protected textual_date value 'March 31'"
        in blocked[deadline.block_id]["blocked_reason"]
    )


def test_qa_agent_maps_pptx_capacity_and_coordinate_issues_and_improves_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(
        candidate,
        text=SOURCE_TEXT * 5,
        shape_x=2_500_000,
        shape_width=1_200_000,
        shape_height=500_000,
    )
    candidate_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    spec = QaRepairSpec(
        job_id="pptx-native-repair",
        source_document=source,
        candidate_document=candidate,
        work_dir=tmp_path / "work",
        max_rounds=1,
        strict_page_count=True,
    )

    report = run_qa_repair_agent(spec)

    round_record = report.rounds[0]
    actions = {action["action"]: action for action in round_record["prepared_actions"]}
    assert Path(actions["restore_shape_coordinates"]["source_pptx"]) == source.resolve()
    assert actions["resolve_text_overflow"]["target_load_ratio"] == 0.92
    assert actions["resolve_text_overflow"]["allow_expand"] is True
    before_errors = sum(
        issue["severity"] == "error" for issue in round_record["before_issues"]
    )
    after_errors = sum(
        issue["severity"] == "error" for issue in round_record["after_issues"]
    )
    assert after_errors < before_errors
    assert round_record["accepted"] is True
    assert report.accepted_document != candidate.resolve()
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == candidate_digest


def test_qa_agent_sends_trusted_table_row_restore_hint_to_executor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)
    initial_candidate = candidate.resolve()

    def qa_runner(
        source_document: Path,
        candidate_document: Path,
        **_kwargs: Any,
    ) -> list[QaIssue]:
        del source_document
        if Path(candidate_document).resolve() != initial_candidate:
            return []
        return [
            QaIssue(
                code="PPTX_RENDERED_TABLE_GEOMETRY_CHANGED",
                message="PowerPoint shortened two translated table rows.",
                severity="error",
                slide=1,
                details={
                    "shape_id": "2",
                    "row_count_changed": False,
                    "reference_row_count": 3,
                    "candidate_row_count": 3,
                    "reference_row_heights_pt": [20.0, 30.0, 24.0],
                    "candidate_row_heights_pt": [20.0, 20.0, 20.0],
                    "row_height_deltas_pt": [0.0, -10.0, -4.0],
                    "row_height_tolerances_pt": [1.0, 1.0, 1.0],
                    "changed_row_indices": [2, 3],
                    "table_coordinates_unchanged": True,
                    "automatic_row_height_restore_safe": True,
                    "repair_hint": {
                        "action": "restore_table_row_heights",
                        "action_type": "restore_table_row_heights",
                        "slide": 1,
                        "shape_id": "2",
                        "rows": [
                            {"row_index": 2, "height_pt": 30.0},
                            {"row_index": 3, "height_pt": 24.0},
                        ],
                        "automatic": True,
                    },
                },
            )
        ]

    def table_layout_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fixture-powerpoint",
            "available": True,
            "capabilities": [
                "shape_text_bounds",
                "table_runtime_geometry",
            ],
            "measurements": [
                {
                    **target,
                    "is_table": True,
                    "table_frame_left_pt": 10.0,
                    "table_frame_top_pt": 20.0,
                    "table_frame_width_pt": 400.0,
                    "table_frame_height_pt": 74.0,
                    "table_row_heights_pt": [20.0, 30.0, 24.0],
                }
                for target in targets
            ],
        }

    report = run_qa_repair_agent(
        QaRepairSpec(
            job_id="trusted-table-row-restore",
            source_document=source,
            candidate_document=candidate,
            work_dir=tmp_path / "work",
            max_rounds=1,
            rendered_layout_required=True,
        ),
        qa_runner=qa_runner,
        pptx_layout_runner=table_layout_runner,
    )

    assert report.status == QaAgentState.COMPLETED
    round_record = report.rounds[0]
    assert [action["action"] for action in round_record["prepared_actions"]] == [
        "restore_table_row_heights"
    ]
    executor_result = round_record["repair_audit"]["executor_result"]
    assert executor_result["normalized_actions"][0]["action"] == (
        "restore_table_row_heights"
    )
    applied = executor_result["audit"]["engine_result"]["audit"]["actions"]
    assert applied[0]["action"] == "restore_table_row_heights"


def test_strict_layout_does_not_execute_unregistered_table_restore(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)
    initial_candidate = candidate.resolve()

    table_issue = QaIssue(
        code="PPTX_RENDERED_TABLE_GEOMETRY_CHANGED",
        message="PowerPoint shortened translated table rows.",
        severity="error",
        slide=1,
        details={
            "shape_id": "2",
            "row_count_changed": False,
            "reference_row_count": 3,
            "candidate_row_count": 3,
            "reference_row_heights_pt": [20.0, 30.0, 24.0],
            "candidate_row_heights_pt": [20.0, 20.0, 20.0],
            "row_height_deltas_pt": [0.0, -10.0, -4.0],
            "row_height_tolerances_pt": [1.0, 1.0, 1.0],
            "changed_row_indices": [2, 3],
            "table_coordinates_unchanged": True,
            "automatic_row_height_restore_safe": True,
            "repair_hint": {
                "action": "restore_table_row_heights",
                "action_type": "restore_table_row_heights",
                "slide": 1,
                "shape_id": "2",
                "rows": [
                    {"row_index": 2, "height_pt": 30.0},
                    {"row_index": 3, "height_pt": 24.0},
                ],
                "automatic": True,
            },
        },
    )
    overflow_issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="A separate text box still overflows.",
        severity="error",
        slide=15,
        details={"shape_id": "9"},
    )

    def qa_runner(
        source_document: Path,
        candidate_document: Path,
        **_kwargs: Any,
    ) -> list[QaIssue]:
        del source_document
        if Path(candidate_document).resolve() == initial_candidate:
            return [table_issue, overflow_issue]
        return [overflow_issue]

    class ProtocolFailingPlanner:
        def diagnose(self, **_kwargs: Any) -> dict[str, str]:
            return {"summary": "The planner bundles safe and unsafe work."}

        def plan(self, **_kwargs: Any) -> dict[str, Any]:
            raise ProviderProtocolError(
                "repair plan action 5 has unexpected shape coordinates"
            )

    def table_layout_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fixture-powerpoint",
            "available": True,
            "capabilities": [
                "shape_text_bounds",
                "table_runtime_geometry",
            ],
            "measurements": [
                {
                    **target,
                    "is_table": True,
                    "table_frame_left_pt": 10.0,
                    "table_frame_top_pt": 20.0,
                    "table_frame_width_pt": 400.0,
                    "table_frame_height_pt": 74.0,
                    "table_row_heights_pt": [20.0, 30.0, 24.0],
                }
                for target in targets
            ],
        }

    report = run_qa_repair_agent(
        QaRepairSpec(
            job_id="strict-safe-table-first",
            source_document=source,
            candidate_document=candidate,
            work_dir=tmp_path / "work",
            max_rounds=1,
            strict_layout=True,
            rendered_layout_required=True,
        ),
        processing_provider=ProtocolFailingPlanner(),
        qa_runner=qa_runner,
        pptx_layout_runner=table_layout_runner,
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    round_record = report.rounds[0]
    assert round_record["accepted"] is False
    assert round_record["provider_error"] == {
        "stage": "plan",
        "type": "ProviderProtocolError",
        "message": "repair plan action 5 has unexpected shape coordinates",
        "fallback": "default_plan",
    }
    assert [action["action"] for action in round_record["prepared_actions"]] == [
        "restore_table_row_heights"
    ]
    assert round_record["strict_layout_policy"]["allowed_actions"] == [
        "replace_translation",
        "resolve_text_overflow",
    ]
    assert (
        round_record["strict_layout_policy"]["rejected_plan_actions"][0]["code"]
        == "STRICT_LAYOUT_ACTION_FORBIDDEN"
    )
    assert round_record["outcome"] == "needs_review"
    assert "repair_audit" not in round_record
    assert report.accepted_document == initial_candidate


def test_strict_layout_secondary_gate_allows_trusted_rendered_overflow() -> None:
    issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="PowerPoint measured translated text overflow.",
        severity="error",
        slide=1,
        details={"shape_id": "2", "rendered_load_ratio": 1.4},
    )
    action = {
        "action": "resolve_text_overflow",
        "slide": 1,
        "shape_id": "2",
        "adapter_source": "issue[0]",
        "trusted_rendered_evidence": True,
        "rendered_load_ratio": 1.4,
        "rendered_evidence": {
            "issue_codes": ["PPTX_RENDERED_TEXT_OVERFLOW"],
            "rendered_load_ratio": 1.4,
        },
    }

    assert _actions_are_repairable(
        [action],
        document_type="pptx",
        repair_engine=None,
        allowed_actions={"resolve_text_overflow"},
        strict_layout=True,
        issues=[issue],
    )


def test_issue_synthesized_action_cannot_bypass_registered_allowed_actions() -> None:
    issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="PowerPoint measured translated text overflow.",
        severity="error",
        slide=1,
        details={"shape_id": "2", "rendered_load_ratio": 1.4},
    )
    action = {
        "action": "resolve_text_overflow",
        "slide": 1,
        "shape_id": "2",
        "adapter_source": "issue[0]",
        "trusted_rendered_evidence": True,
        "rendered_load_ratio": 1.4,
        "rendered_evidence": {
            "issue_codes": ["PPTX_RENDERED_TEXT_OVERFLOW"],
            "rendered_load_ratio": 1.4,
        },
    }

    assert not _actions_are_repairable(
        [action],
        document_type="pptx",
        repair_engine=None,
        allowed_actions={"replace_translation"},
        strict_layout=True,
        issues=[issue],
    )


@pytest.mark.parametrize(
    ("action_update", "issues"),
    [
        ({}, []),
        ({"adapter_source": "issue[1]"}, []),
        ({"adapter_source": "issue[0]-forged"}, []),
        (
            {"slide": 2},
            [
                QaIssue(
                    code="PPTX_RENDERED_TEXT_OVERFLOW",
                    message="Overflow is on another slide.",
                    slide=1,
                    details={"shape_id": "2", "rendered_load_ratio": 1.4},
                )
            ],
        ),
        (
            {"shape_id": "3"},
            [
                QaIssue(
                    code="PPTX_RENDERED_TEXT_OVERFLOW",
                    message="Overflow is in another shape.",
                    slide=1,
                    details={"shape_id": "2", "rendered_load_ratio": 1.4},
                )
            ],
        ),
        (
            {"rendered_evidence": {"issue_codes": ["PPTX_UNRELATED_ISSUE"]}},
            [
                QaIssue(
                    code="PPTX_RENDERED_TEXT_OVERFLOW",
                    message="The evidence marker names a different issue.",
                    slide=1,
                    details={"shape_id": "2", "rendered_load_ratio": 1.4},
                )
            ],
        ),
        (
            {},
            [
                QaIssue(
                    code="PPTX_TEXT_CAPACITY_RISK",
                    message="This is estimated, not rendered evidence.",
                    slide=1,
                    details={"shape_id": "2"},
                )
            ],
        ),
    ],
    ids=[
        "marker-without-issue-context",
        "out-of-range-issue-index",
        "forged-issue-source-suffix",
        "mismatched-slide",
        "mismatched-shape",
        "mismatched-evidence-code",
        "untrusted-issue-code",
    ],
)
def test_strict_layout_secondary_gate_rejects_forged_issue_provenance(
    action_update: dict[str, Any],
    issues: list[QaIssue],
) -> None:
    action = {
        "action": "resolve_text_overflow",
        "slide": 1,
        "shape_id": "2",
        "adapter_source": "issue[0]",
        "trusted_rendered_evidence": True,
        "rendered_load_ratio": 1.4,
        "rendered_evidence": {
            "issue_codes": ["PPTX_RENDERED_TEXT_OVERFLOW"],
            "rendered_load_ratio": 1.4,
        },
        **action_update,
    }

    assert not _actions_are_repairable(
        [action],
        document_type="pptx",
        repair_engine=None,
        allowed_actions={"resolve_text_overflow"},
        strict_layout=True,
        issues=issues,
    )


@pytest.mark.parametrize(
    "action",
    [
        {
            "action": "resolve_text_overflow",
            "slide": 1,
            "shape_id": "2",
            "adapter_source": "plan[0]",
            "trusted_rendered_evidence": True,
        },
        {
            "action": "resolve_text_overflow",
            "slide": 1,
            "shape_id": "2",
            "adapter_source": "issue[0]",
        },
        {
            "action": "resolve_text_overflow",
            "slide": 1,
            "shape_id": "2",
            "adapter_source": "issue[0]",
            "trusted_rendered_evidence": False,
        },
        {
            "action": "resolve_text_overflow",
            "adapter_source": "issue[0]",
            "trusted_rendered_evidence": True,
        },
        {
            "action": "resolve_text_overflow",
            "slide": True,
            "shape_id": "2",
            "adapter_source": "issue[0]",
            "trusted_rendered_evidence": True,
        },
        {
            "action": "set_shape_bounds",
            "slide": 1,
            "shape_id": "2",
            "adapter_source": "issue[0]",
            "trusted_rendered_evidence": True,
        },
    ],
    ids=[
        "untrusted-plan",
        "missing-marker",
        "estimate-only",
        "global-without-location",
        "invalid-boolean-slide",
        "global-geometry-action",
    ],
)
def test_strict_layout_secondary_gate_rejects_untrusted_overflow_actions(
    action: dict[str, Any],
) -> None:
    assert not _actions_are_repairable(
        [action],
        document_type="pptx",
        repair_engine=None,
        allowed_actions=None,
        strict_layout=True,
    )


def test_strict_layout_executes_adapter_trusted_rendered_overflow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    initial_candidate = candidate.resolve()
    overflow_issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="PowerPoint measured translated text overflow.",
        severity="error",
        slide=1,
        details={
            "shape_id": "2",
            "rendered_load_ratio": 1.4,
            "provider": "powerpoint-com",
            "repair_hint": {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "rendered_load_ratio": 1.4,
                "automatic": True,
            },
        },
    )

    def qa_runner(candidate_document: Path, **_kwargs: Any) -> list[QaIssue]:
        return (
            [overflow_issue]
            if Path(candidate_document).resolve() == initial_candidate
            else []
        )

    def fitting_layout_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fixture-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [
                {
                    **target,
                    "bound_width_pt": 100.0,
                    "bound_height_pt": 20.0,
                    "available_width_pt": 300.0,
                    "available_height_pt": 80.0,
                }
                for target in targets
            ],
        }

    report = run_qa_repair_agent(
        QaRepairSpec(
            job_id="strict-trusted-rendered-overflow",
            source_document=source,
            candidate_document=candidate,
            work_dir=tmp_path / "work",
            max_rounds=1,
            strict_layout=True,
            rendered_layout_required=True,
        ),
        qa_runner=qa_runner,
        pptx_layout_runner=fitting_layout_runner,
    )

    assert report.status == QaAgentState.COMPLETED
    round_record = report.rounds[0]
    assert (
        "resolve_text_overflow"
        in round_record["strict_layout_policy"]["allowed_actions"]
    )
    assert len(round_record["prepared_actions"]) == 1
    action = round_record["prepared_actions"][0]
    assert action["action"] == "resolve_text_overflow"
    assert action["slide"] == 1
    assert action["shape_id"] == "2"
    assert action["adapter_source"] == "issue[0]"
    assert action["trusted_rendered_evidence"] is True
    # Expansion is only a location-scoped last resort after autofit, margins,
    # and the font floor; the engine bounds it to collision-safe slide space.
    assert action["allow_expand"] is True


def test_provider_diagnosis_error_without_safe_action_needs_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    issue = QaIssue(
        code="PPTX_MANUAL_REVIEW_ONLY",
        message="No trusted automatic repair exists.",
        severity="error",
        slide=1,
        details={
            "shape_id": "2",
            "repair_hint": {
                "action": "set_shape_bounds",
                "action_type": "set_shape_bounds",
                "slide": 1,
                "shape_id": "2",
                "bbox_emu": [600_000, 500_000, 4_000_000, 1_000_000],
                "automatic": True,
            },
        },
    )

    class DiagnosisFailingProvider:
        def diagnose(self, **_kwargs: Any) -> dict[str, Any]:
            raise ProviderProtocolError("diagnosis output is invalid")

        def plan(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("plan must not run after diagnosis fallback")

    report = run_qa_repair_agent(
        QaRepairSpec(
            job_id="provider-diagnosis-fail-soft",
            source_document=source,
            candidate_document=candidate,
            work_dir=tmp_path / "work",
            max_rounds=1,
            strict_layout=True,
        ),
        processing_provider=DiagnosisFailingProvider(),
        qa_runner=lambda **_kwargs: [issue],
    )

    assert report.status == QaAgentState.NEEDS_REVIEW
    assert report.failure is None
    assert report.accepted_document == candidate.resolve()
    round_record = report.rounds[0]
    assert round_record["outcome"] == "needs_review"
    assert round_record["prepared_actions"] == []
    assert (
        round_record["strict_layout_policy"]["rejected_plan_actions"][0]["action"]
        == "set_shape_bounds"
    )
    assert (
        round_record["strict_layout_policy"]["rejected_plan_actions"][0]["code"]
        == "STRICT_LAYOUT_ACTION_FORBIDDEN"
    )
    assert round_record["provider_error"] == {
        "stage": "diagnose",
        "type": "ProviderProtocolError",
        "message": "diagnosis output is invalid",
        "fallback": "deterministic_diagnosis_and_default_plan",
    }
    assert round_record["diagnosis"]["issue_codes"] == {"PPTX_MANUAL_REVIEW_ONLY": 1}


def test_qa_agent_verification_preserves_authorized_geometry_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    initial_candidate = candidate.resolve()
    requested_bounds = [600_000, 500_000, 4_000_000, 1_000_000]
    verification_authorizations: list[Any] = []

    def qa_runner(
        source_document: Path,
        candidate_document: Path,
        *,
        authorized_coordinate_changes: Any = None,
        **_kwargs: Any,
    ) -> list[QaIssue]:
        del source_document
        if Path(candidate_document).resolve() == initial_candidate:
            return [
                QaIssue(
                    code="PPTX_TEST_GEOMETRY_REPAIR",
                    message="Fixture requests a bounded coordinate repair.",
                    severity="error",
                    slide=1,
                    details={
                        "shape_id": "2",
                        "repair_hint": {
                            "action": "set_shape_bounds",
                            "action_type": "set_shape_bounds",
                            "slide": 1,
                            "shape_id": "2",
                            "bounds_emu": requested_bounds,
                            "automatic": True,
                        },
                    },
                )
            ]
        verification_authorizations.append(authorized_coordinate_changes)
        return (
            []
            if authorized_coordinate_changes == {"1:2": requested_bounds}
            else [
                QaIssue(
                    code="PPTX_SHAPE_COORDINATE_CHANGED",
                    message="Authorized geometry was not forwarded to QA.",
                    severity="error",
                    slide=1,
                    details={"shape_id": "2"},
                )
            ]
        )

    def fitting_layout_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fixture",
            "available": True,
            "measurements": [
                {
                    **target,
                    "bound_width_pt": 100.0,
                    "bound_height_pt": 20.0,
                    "available_width_pt": 300.0,
                    "available_height_pt": 80.0,
                }
                for target in targets
            ],
        }

    report = run_qa_repair_agent(
        QaRepairSpec(
            job_id="authorized-pptx-geometry",
            source_document=source,
            candidate_document=candidate,
            work_dir=tmp_path / "work",
            max_rounds=1,
        ),
        qa_runner=qa_runner,
        pptx_layout_runner=fitting_layout_runner,
    )

    assert report.status == QaAgentState.COMPLETED
    assert verification_authorizations == [{"1:2": requested_bounds}]
