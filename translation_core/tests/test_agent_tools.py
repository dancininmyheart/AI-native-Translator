from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import format_translator.agent_tools as agent_tools_module
from format_translator.agent_contracts import (
    PreflightDocumentRequest,
    ToolStatus,
    TranslateOnceRequest,
)
from format_translator.agent_tool_cli import (
    EXIT_EXECUTION_FAILED,
    EXIT_OK,
    EXIT_REQUEST_INVALID,
)
from format_translator.agent_tool_cli import (
    main as agent_tool_main,
)
from format_translator.agent_tools import (
    AgentStateError,
    _load_provider_settings,
    apply_repair,
    finalize_result,
    preflight_document,
    run_document_qa,
    translate_once,
    verify_candidate,
)
from format_translator.legacy import LegacyConversionError, resolve_soffice
from format_translator.models import QaIssue
from format_translator.qa_agent import QaAgentState
from format_translator.utils import sha256_file


def _write_docx(path: Path, text: str = "Hello from a minimal Word document.") -> None:
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
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>
  </w:body>
</w:document>""",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)


def _write_pptx(
    path: Path,
    text: str = "Hello from a minimal PowerPoint presentation.",
) -> None:
    parts = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>""",
        "ppt/presentation.xml": """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
  <p:sldSz cx="12192000" cy="6858000"/>
</p:presentation>""",
        "ppt/_rels/presentation.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
</Relationships>""",
        "ppt/slides/slide1.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
      <a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="2" name="TextBox 1"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="500000" y="500000"/>
        <a:ext cx="5000000" cy="1000000"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r>
        <a:rPr lang="en-US" sz="1800"/><a:t>{text}</a:t>
      </a:r><a:endParaRPr lang="en-US"/></a:p></p:txBody>
    </p:sp>
  </p:spTree></p:cSld>
</p:sld>""",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)


def _workspace(
    tmp_path: Path,
    job_id: str = "agent-job",
    *,
    document_format: str = "docx",
) -> tuple[Path, Path]:
    jobs_root = tmp_path / "jobs"
    workspace = jobs_root / job_id
    input_dir = workspace / "input"
    fixture_dir = workspace / "fixtures"
    input_dir.mkdir(parents=True)
    fixture_dir.mkdir()
    source = input_dir / f"source.{document_format}"
    if document_format == "docx":
        _write_docx(source)
    elif document_format == "pptx":
        _write_pptx(source)
    else:
        raise AssertionError(document_format)
    (fixture_dir / "passthrough.json").write_text("{}", encoding="utf-8")
    return jobs_root, workspace


def _translation_request(jobs_root: Path, job_id: str) -> dict[str, object]:
    return {
        "job_root": jobs_root,
        "job_id": job_id,
        "input_relpath": "input/source.docx",
        "candidate_id": "r0",
        "source_language": "en",
        "target_language": "zh",
        "provider": {
            "kind": "fixture",
            "fixture_relpath": "fixtures/passthrough.json",
            "missing": "passthrough",
        },
    }


def test_relative_path_and_pretranslated_pair_contracts(tmp_path: Path) -> None:
    for unsafe in ("../secret.docx", "C:\\secret.docx"):
        with pytest.raises(ValidationError):
            PreflightDocumentRequest(
                job_root=tmp_path,
                job_id="safe-job",
                input_relpath=unsafe,
            )
    with pytest.raises(ValidationError, match="must appear together"):
        PreflightDocumentRequest(
            job_root=tmp_path,
            job_id="safe-job",
            input_relpath="input/source.docx",
            candidate_id="candidate-r0",
        )


def test_live_translation_contract_accepts_auto_target_and_foreign_bcp47_source(
    tmp_path: Path,
) -> None:
    request = TranslateOnceRequest(
        job_root=tmp_path,
        job_id="safe-job",
        input_relpath="input/source.docx",
        candidate_id="candidate-r0",
        source_language="es-MX",
        target_language="auto",
        provider={"kind": "fixture", "fixture_relpath": "fixtures/result.json"},
    )

    assert request.source_language == "es-mx"
    assert request.target_language == "auto"

    with pytest.raises(ValidationError):
        PreflightDocumentRequest(
            job_root=tmp_path,
            job_id="job-1",
            input_relpath="input/source.docx",
            existing_translation_relpath="input/translated.docx",
            candidate_id="candidate-r0",
            source_language="en",
            target_language="auto",
        )


def test_docx_flow_is_self_contained_and_replay_idempotent(tmp_path: Path) -> None:
    jobs_root, workspace = _workspace(tmp_path)
    source = workspace / "input" / "source.docx"
    source_digest = sha256_file(source)
    context = {"job_root": jobs_root, "job_id": "agent-job"}

    inspected = preflight_document(
        {**context, "input_relpath": "input/source.docx"}
    )
    assert inspected.status == ToolStatus.COMPLETED
    assert int(inspected.summary["block_count"]) > 0
    assert inspected.summary["normalized_relpath"] == "input/source.docx"
    assert inspected.artifacts[0].sha256 == source_digest

    request = _translation_request(jobs_root, "agent-job")
    translated = translate_once(request)
    replayed = translate_once(request)
    assert translated.status == replayed.status == ToolStatus.COMPLETED
    assert translated.summary["provider_invoked"] is True
    assert replayed.summary["provider_invoked"] is False
    assert replayed.summary["resumed"] is True
    candidate_r0 = workspace / "candidates" / "r0.docx"
    candidate_r0_digest = sha256_file(candidate_r0)

    with pytest.raises(AgentStateError, match="different translation request"):
        translate_once({**request, "target_language": "ja"})

    qa = run_document_qa({**context, "candidate_id": "r0"})
    assert qa.status == ToolStatus.COMPLETED
    repair_request = {
        **context,
        "candidate_id": "r0",
        "output_candidate_id": "r1",
        "max_rounds": 1,
    }
    repaired = apply_repair(repair_request)
    replayed_repair = apply_repair(repair_request)
    assert repaired.status == replayed_repair.status == ToolStatus.NEEDS_REVIEW
    assert repaired.summary["repair_invoked"] is True
    assert repaired.summary["output_created"] is False
    assert repaired.error is not None
    assert repaired.error.code == "REPAIR_NOT_IMPROVED"
    assert replayed_repair.summary["output_created"] is False
    assert not (workspace / "candidates" / "r1.docx").exists()

    verified = verify_candidate({**context, "candidate_id": "r0"})
    assert verified.status == ToolStatus.COMPLETED
    persisted = json.loads(
        (workspace / "agent-tool-state.json").read_text(encoding="utf-8")
    )
    candidate_state = persisted["candidates"]["r0"]
    assert candidate_state["latest_qa"]["report_relpath"].startswith("reports/qa/")
    assert candidate_state["verification"]["report_relpath"].startswith(
        "reports/verification/"
    )
    finalized = finalize_result(
        {**context, "candidate_id": "r0", "result_basename": "translated-result"}
    )
    assert finalized.status == ToolStatus.COMPLETED
    assert sha256_file(workspace / "results" / "translated-result.docx") == (
        candidate_r0_digest
    )
    assert sha256_file(source) == source_digest


@pytest.mark.parametrize("document_format", ["docx", "pptx"])
def test_empty_office_translation_registers_noop_candidate_for_normal_qa(
    tmp_path: Path,
    document_format: str,
) -> None:
    job_id = f"empty-{document_format}-job"
    jobs_root, workspace = _workspace(
        tmp_path,
        job_id=job_id,
        document_format=document_format,
    )
    source = workspace / "input" / f"source.{document_format}"
    if document_format == "docx":
        _write_docx(source, "")
    else:
        _write_pptx(source, "")
    context = {"job_root": jobs_root, "job_id": job_id}

    inspected = preflight_document(
        {**context, "input_relpath": f"input/source.{document_format}"}
    )
    assert inspected.status == ToolStatus.COMPLETED
    assert inspected.summary["block_count"] == 0

    translated = translate_once(
        {
            **context,
            "input_relpath": f"input/source.{document_format}",
            "candidate_id": "r0",
            "source_language": "auto",
            "target_language": "auto",
            "provider": {
                "kind": "fixture",
                "fixture_relpath": "fixtures/passthrough.json",
                "missing": "passthrough",
            },
        }
    )

    assert translated.status == ToolStatus.COMPLETED
    assert translated.summary["provider_invoked"] is False
    assert translated.summary["source_language"] == "und"
    assert translated.summary["resolved_target_language"] == "zh"
    candidate = workspace / "candidates" / f"r0.{document_format}"
    assert candidate.is_file()
    state = json.loads(
        (workspace / "agent-tool-state.json").read_text(encoding="utf-8")
    )
    normalized = workspace / Path(state["candidates"]["r0"]["source_relpath"])
    assert sha256_file(candidate) == sha256_file(normalized)

    qa = run_document_qa({**context, "candidate_id": "r0"})
    assert qa.status == ToolStatus.COMPLETED
    verified = verify_candidate({**context, "candidate_id": "r0"})
    assert verified.status == ToolStatus.COMPLETED


def test_translate_once_persists_requested_and_resolved_auto_target(
    tmp_path: Path,
) -> None:
    jobs_root, workspace = _workspace(tmp_path, job_id="auto-route-job")
    context = {"job_root": jobs_root, "job_id": "auto-route-job"}
    preflight_document({**context, "input_relpath": "input/source.docx"})
    request = {
        **_translation_request(jobs_root, "auto-route-job"),
        "source_language": "en-US",
        "target_language": "auto",
    }

    translated = translate_once(request)
    replayed = translate_once(request)

    assert translated.summary["source_language"] == "en"
    assert translated.summary["requested_target_language"] == "auto"
    assert translated.summary["resolved_target_language"] == "zh"
    assert translated.summary["target_language"] == "zh"
    assert replayed.summary["resolved_target_language"] == "zh"
    state = json.loads(
        (workspace / "agent-tool-state.json").read_text(encoding="utf-8")
    )
    candidate = state["candidates"]["r0"]
    assert candidate["requested_target_language"] == "auto"
    assert candidate["resolved_target_language"] == "zh"
    assert candidate["target_language"] == "zh"


def test_repair_uses_persisted_semantic_issues_and_semantic_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_MODEL", "fixture-model")
    jobs_root, workspace = _workspace(tmp_path, "semantic-repair-job")
    context = {"job_root": jobs_root, "job_id": "semantic-repair-job"}
    preflight_document({**context, "input_relpath": "input/source.docx"})
    translate_once(_translation_request(jobs_root, "semantic-repair-job"))

    semantic_issue = QaIssue(
        issue_id="semantic-omission-1",
        code="SEMANTIC_OMISSION",
        message="A statement from the source is missing in translation.",
        block_id="p:0",
        details={"origin": "semantic-provider"},
    )
    qa_calls: list[dict[str, Any]] = []

    def fake_qa_core(
        _context: Any,
        _record: Any,
        _source: Path,
        candidate: Path,
        **kwargs: Any,
    ) -> tuple[list[QaIssue], dict[str, Any]]:
        qa_calls.append(
            {
                "candidate": Path(candidate),
                "semantic_qa": kwargs["semantic_qa"],
                "max_processing_concurrency": kwargs[
                    "max_processing_concurrency"
                ],
            }
        )
        issues = (
            [semantic_issue]
            if Path(candidate).name == "r0.docx"
            else []
        )
        return issues, {"semantic_qa_issue_count": len(issues)}

    monkeypatch.setattr(agent_tools_module, "_run_qa_core", fake_qa_core)
    qa = run_document_qa(
        {**context, "candidate_id": "r0", "semantic_qa": True}
    )
    assert qa.status == ToolStatus.NEEDS_REVIEW

    captured: dict[str, Any] = {}

    class FakeProcessingProvider:
        async def aclose(self) -> None:
            captured["provider_closed"] = True

    monkeypatch.setattr(
        agent_tools_module,
        "_processing_provider",
        lambda _timeout: FakeProcessingProvider(),
    )

    async def fake_repair_agent(
        spec: Any,
        *,
        processing_provider: Any,
        qa_runner: Any,
        baseline_issues: list[QaIssue],
    ) -> Any:
        captured["baseline_issues"] = list(baseline_issues)
        captured["processing_provider"] = processing_provider
        repaired = spec.work_dir / "accepted.docx"
        _write_docx(repaired, "Repaired translated content.")
        verified = await asyncio.to_thread(
            qa_runner,
            source_document=spec.source_document,
            candidate_document=repaired,
        )
        captured["verified_issues"] = verified["issues"]
        spec.report_path.parent.mkdir(parents=True, exist_ok=True)
        spec.report_path.write_text(
            json.dumps(
                {
                    "accepted_issues": [],
                    "accepted_score": 1000,
                    "rounds": [{"accepted": True, "rolled_back": False}],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            status=QaAgentState.COMPLETED,
            rounds=[{"accepted": True, "rolled_back": False}],
            accepted_document=repaired,
            accepted_issues=[],
            accepted_score=1000,
            report_path=spec.report_path,
            rollback=False,
        )

    monkeypatch.setattr(
        agent_tools_module,
        "run_qa_repair_agent_async",
        fake_repair_agent,
    )
    repaired = apply_repair(
        {
            **context,
            "candidate_id": "r0",
            "output_candidate_id": "r1",
            "max_rounds": 1,
            "use_processing_provider": True,
        }
    )
    replayed = apply_repair(
        {
            **context,
            "candidate_id": "r0",
            "output_candidate_id": "r1",
            "max_rounds": 1,
            "use_processing_provider": True,
        }
    )

    assert repaired.status == ToolStatus.COMPLETED
    assert repaired.summary["output_created"] is True
    assert repaired.summary["created_now"] is True
    assert replayed.status == ToolStatus.COMPLETED
    assert replayed.summary["output_created"] is True
    assert replayed.summary["created_now"] is False
    assert replayed.summary["resumed"] is True
    assert replayed.summary["repair_invoked"] is False
    assert any(
        artifact.artifact_id == "candidate:r1"
        for artifact in replayed.artifacts
    )
    assert [issue.code for issue in captured["baseline_issues"]] == [
        "SEMANTIC_OMISSION"
    ]
    assert captured["verified_issues"] == []
    assert qa_calls[-1]["candidate"].name == "accepted.docx"
    assert qa_calls[-1]["semantic_qa"] is True
    assert qa_calls[-1]["max_processing_concurrency"] == 1
    assert captured["provider_closed"] is True
    assert (workspace / "candidates" / "r1.docx").is_file()


def test_same_job_concurrent_translate_replay_invokes_provider_once(
    tmp_path: Path,
) -> None:
    jobs_root, _workspace_path = _workspace(tmp_path, "concurrent-job")
    context = {"job_root": jobs_root, "job_id": "concurrent-job"}
    preflight_document({**context, "input_relpath": "input/source.docx"})
    request = _translation_request(jobs_root, "concurrent-job")

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _index: translate_once(request), range(2)))

    assert all(response.status == ToolStatus.COMPLETED for response in responses)
    assert sorted(response.summary["provider_invoked"] for response in responses) == [
        False,
        True,
    ]


def test_pretranslated_docx_registration_is_idempotent_and_hash_protected(
    tmp_path: Path,
) -> None:
    jobs_root, workspace = _workspace(tmp_path, "pretranslated-job")
    translation = workspace / "input" / "existing-translation.docx"
    _write_docx(translation, "Translated candidate text.")
    request = {
        "job_root": jobs_root,
        "job_id": "pretranslated-job",
        "input_relpath": "input/source.docx",
        "existing_translation_relpath": "input/existing-translation.docx",
        "candidate_id": "candidate-r0",
        "source_language": "en",
        "target_language": "zh",
    }

    first = preflight_document(request)
    second = preflight_document(request)

    assert first.status == second.status == ToolStatus.COMPLETED
    assert first.summary["pretranslated_candidate_id"] == "candidate-r0"
    assert any(
        artifact.artifact_id == "candidate:candidate-r0"
        for artifact in first.artifacts
    )
    state = json.loads(
        (workspace / "agent-tool-state.json").read_text(encoding="utf-8")
    )
    assert state["candidates"]["candidate-r0"]["source_language"] == "en"
    assert state["candidates"]["candidate-r0"]["target_language"] == "zh"
    qa = run_document_qa(
        {
            "job_root": jobs_root,
            "job_id": "pretranslated-job",
            "candidate_id": "candidate-r0",
        }
    )
    assert qa.status in {ToolStatus.COMPLETED, ToolStatus.NEEDS_REVIEW}

    _write_docx(translation, "Mutated candidate text.")
    with pytest.raises(AgentStateError, match="bound differently"):
        preflight_document(request)


def test_pretranslated_candidate_must_match_source_family(tmp_path: Path) -> None:
    jobs_root, workspace = _workspace(tmp_path, "family-job")
    _write_pptx(workspace / "input" / "wrong-family.pptx")

    with pytest.raises(AgentStateError, match="format family"):
        preflight_document(
            {
                "job_root": jobs_root,
                "job_id": "family-job",
                "input_relpath": "input/source.docx",
                "existing_translation_relpath": "input/wrong-family.pptx",
                "candidate_id": "candidate-r0",
                "source_language": "en",
                "target_language": "zh",
            }
        )


def test_pretranslated_candidate_must_be_a_readable_ooxml_package(
    tmp_path: Path,
) -> None:
    jobs_root, workspace = _workspace(tmp_path, "corrupt-pretranslated-job")
    corrupt = workspace / "input" / "corrupt.docx"
    corrupt.write_text("not an OOXML ZIP", encoding="utf-8")

    with pytest.raises(AgentStateError, match="valid OOXML package"):
        preflight_document(
            {
                "job_root": jobs_root,
                "job_id": "corrupt-pretranslated-job",
                "input_relpath": "input/source.docx",
                "existing_translation_relpath": "input/corrupt.docx",
                "candidate_id": "candidate-r0",
                "source_language": "en",
                "target_language": "zh",
            }
        )


def test_recovered_blocks_keep_candidate_language_context(tmp_path: Path) -> None:
    _jobs_root, workspace = _workspace(tmp_path, "language-context-job")
    source = workspace / "input" / "source.docx"
    candidate = workspace / "input" / "candidate.docx"
    _write_docx(candidate, "Translated text")

    blocks, _translations = agent_tools_module._recover_translation_context(
        source,
        candidate,
        source_language="en",
        target_language="ja",
    )

    assert blocks
    assert all(block.source_language == "en" for block in blocks)
    assert all(block.target_language == "ja" for block in blocks)


def test_translation_registration_failure_cleans_candidate_and_allows_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root, workspace = _workspace(tmp_path, "registration-retry-job")
    context = {"job_root": jobs_root, "job_id": "registration-retry-job"}
    preflight_document({**context, "input_relpath": "input/source.docx"})
    request = _translation_request(jobs_root, "registration-retry-job")
    original_save = agent_tools_module._save_state
    failed_once = False

    def fail_candidate_save(context_value, state_value) -> None:
        nonlocal failed_once
        if state_value.candidates and not failed_once:
            failed_once = True
            raise OSError("simulated state persistence failure")
        original_save(context_value, state_value)

    monkeypatch.setattr(agent_tools_module, "_save_state", fail_candidate_save)
    with pytest.raises(OSError, match="simulated state persistence failure"):
        translate_once(request)

    assert not (workspace / "candidates" / "r0.docx").exists()
    assert not (
        workspace / "work" / "translation" / "translate-r0"
    ).exists()
    translated = translate_once(request)
    assert translated.status == ToolStatus.COMPLETED
    assert (workspace / "candidates" / "r0.docx").is_file()


def test_pptx_bridge_flow_without_external_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORMAT_TRANSLATOR_PPTX_LAYOUT_BACKEND", "disabled")
    jobs_root, workspace = _workspace(
        tmp_path,
        "pptx-job",
        document_format="pptx",
    )
    context = {"job_root": jobs_root, "job_id": "pptx-job"}
    preflight = preflight_document(
        {**context, "input_relpath": "input/source.pptx"}
    )
    assert preflight.summary["normalized_format"] == "pptx"
    request = {
        **context,
        "input_relpath": "input/source.pptx",
        "candidate_id": "r0",
        "source_language": "en",
        "target_language": "zh",
        "provider": {
            "kind": "fixture",
            "fixture_relpath": "fixtures/passthrough.json",
            "missing": "passthrough",
        },
    }
    translated = translate_once(request)
    replayed = translate_once(request)
    assert translated.status == replayed.status == ToolStatus.COMPLETED
    assert replayed.summary["provider_invoked"] is False
    assert (workspace / "candidates" / "r0.pptx").is_file()
    qa = run_document_qa({**context, "candidate_id": "r0"})
    assert qa.status == ToolStatus.COMPLETED
    verified = verify_candidate({**context, "candidate_id": "r0"})
    assert verified.status == ToolStatus.COMPLETED


def _make_legacy_fixture(
    tmp_path: Path,
    *,
    source_format: str,
) -> Path:
    try:
        soffice = resolve_soffice()
    except LegacyConversionError as error:
        pytest.skip(str(error))
    modern_suffix = ".docx" if source_format == "doc" else ".pptx"
    modern = tmp_path / f"legacy-source{modern_suffix}"
    if source_format == "doc":
        _write_docx(modern)
        conversion_filter = "doc:MS Word 97"
    else:
        _write_pptx(modern)
        conversion_filter = "ppt:MS PowerPoint 97"
    output_dir = tmp_path / "legacy-output"
    profile = tmp_path / "legacy-profile"
    output_dir.mkdir()
    profile.mkdir()
    completed = subprocess.run(
        [
            str(soffice),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--convert-to",
            conversion_filter,
            "--outdir",
            str(output_dir),
            str(modern),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    produced = output_dir / f"{modern.stem}.{source_format}"
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert produced.is_file()
    return produced


@pytest.mark.office_integration
@pytest.mark.parametrize("source_format", ["doc", "ppt"])
def test_legacy_preflight_persists_and_reuses_normalized_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_format: str,
) -> None:
    legacy = _make_legacy_fixture(tmp_path / source_format, source_format=source_format)
    jobs_root = tmp_path / f"jobs-{source_format}"
    workspace = jobs_root / f"legacy-{source_format}"
    input_dir = workspace / "input"
    input_dir.mkdir(parents=True)
    source = input_dir / f"source.{source_format}"
    shutil.copy2(legacy, source)
    request = {
        "job_root": jobs_root,
        "job_id": f"legacy-{source_format}",
        "input_relpath": f"input/source.{source_format}",
    }

    first = preflight_document(request)
    normalized_relpath = str(first.summary["normalized_relpath"])
    normalized = workspace / normalized_relpath
    normalized_digest = sha256_file(normalized)
    expected_suffix = ".docx" if source_format == "doc" else ".pptx"
    assert normalized.suffix == expected_suffix
    assert normalized.is_file()
    assert any(
        artifact.artifact_id == "normalized_source" for artifact in first.artifacts
    )
    state = json.loads((workspace / "agent-tool-state.json").read_text("utf-8"))
    assert state["source"]["normalized_sha256"] == normalized_digest

    def unexpected_conversion(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("idempotent preflight must not reconvert legacy input")

    conversion_name = (
        "convert_doc_to_docx" if source_format == "doc" else "convert_ppt_to_pptx"
    )
    monkeypatch.setattr(agent_tools_module, conversion_name, unexpected_conversion)
    second = preflight_document(request)
    assert second.status == ToolStatus.COMPLETED
    assert sha256_file(normalized) == normalized_digest


def test_finalize_requires_current_successful_verification(tmp_path: Path) -> None:
    jobs_root, _workspace_path = _workspace(tmp_path, "verification-job")
    context = {"job_root": jobs_root, "job_id": "verification-job"}
    preflight_document({**context, "input_relpath": "input/source.docx"})
    assert translate_once(_translation_request(jobs_root, "verification-job")).status == (
        ToolStatus.COMPLETED
    )
    assert run_document_qa({**context, "candidate_id": "r0"}).status == (
        ToolStatus.COMPLETED
    )
    result = finalize_result({**context, "candidate_id": "r0"})
    assert result.status == ToolStatus.NEEDS_REVIEW
    assert result.error is not None
    assert result.error.code == "VERIFICATION_REQUIRED"


def test_verification_cannot_bypass_a_blocking_full_semantic_qa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_root, workspace = _workspace(tmp_path, "semantic-verification-job")
    context = {"job_root": jobs_root, "job_id": "semantic-verification-job"}
    preflight_document({**context, "input_relpath": "input/source.docx"})
    assert translate_once(
        _translation_request(jobs_root, "semantic-verification-job")
    ).status == ToolStatus.COMPLETED

    def qa_core(*_args: Any, semantic_qa: bool, **_kwargs: Any) -> tuple[list[QaIssue], dict[str, Any]]:
        if not semantic_qa:
            return [], {"semantic_qa_issue_count": 0}
        return (
            [
                QaIssue(
                    issue_id="semantic-blocker",
                    code="SEMANTIC_OMISSION",
                    message="A material source statement is missing.",
                    severity="error",
                    stage="semantic_qa",
                    block_id="docx-block-1",
                )
            ],
            {"semantic_qa_issue_count": 1},
        )

    monkeypatch.setattr(agent_tools_module, "_run_qa_core", qa_core)
    full_qa = run_document_qa(
        {**context, "candidate_id": "r0", "semantic_qa": True}
    )
    assert full_qa.status == ToolStatus.NEEDS_REVIEW

    verification = verify_candidate({**context, "candidate_id": "r0"})
    assert verification.status == ToolStatus.NEEDS_REVIEW
    assert verification.error is not None
    assert verification.error.code == "FULL_QA_REQUIRED"

    state = json.loads(
        (workspace / "agent-tool-state.json").read_text(encoding="utf-8")
    )
    candidate_state = state["candidates"]["r0"]
    assert candidate_state["latest_qa"]["status"] == "NEEDS_REVIEW"
    assert candidate_state["latest_qa"]["report_relpath"].startswith("reports/qa/")
    assert candidate_state["verification"] is None

    finalized = finalize_result({**context, "candidate_id": "r0"})
    assert finalized.status == ToolStatus.NEEDS_REVIEW
    assert finalized.error is not None
    assert finalized.error.code == "FULL_QA_REQUIRED"


def test_provider_environment_prefers_standard_openai_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://standard.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "standard-secret")
    monkeypatch.setenv("OPENAI_MODEL", "standard-model")
    monkeypatch.setenv("model_baseurl", "https://legacy.invalid/v1")
    monkeypatch.setenv("apil_key", "legacy-secret")
    monkeypatch.setenv("model_name", "legacy-model")
    settings = _load_provider_settings()
    assert settings.base_url == "https://standard.invalid/v1"
    assert settings.api_key == "standard-secret"
    assert settings.model == "standard-model"


def test_single_shot_cli_has_json_stdout_and_stable_validation_exit(
    tmp_path: Path,
) -> None:
    jobs_root, _workspace_path = _workspace(tmp_path, "cli-job")
    output = io.StringIO()
    errors = io.StringIO()
    exit_code = agent_tool_main(
        [
            "preflight_document",
            "--job-root",
            str(jobs_root),
            "--job-id",
            "cli-job",
        ],
        stdin=io.StringIO('{"input_relpath":"input/source.docx"}\n'),
        stdout=output,
        stderr=errors,
    )
    assert exit_code == EXIT_OK
    assert len(output.getvalue().splitlines()) == 1
    assert json.loads(output.getvalue())["status"] == "COMPLETED"
    assert errors.getvalue() == ""

    invalid_output = io.StringIO()
    invalid_errors = io.StringIO()
    invalid_exit = agent_tool_main(
        [
            "verify_candidate",
            "--job-root",
            str(jobs_root),
            "--job-id",
            "cli-job",
        ],
        stdin=io.StringIO("not-json"),
        stdout=invalid_output,
        stderr=invalid_errors,
    )
    assert invalid_exit == EXIT_REQUEST_INVALID
    assert json.loads(invalid_output.getvalue())["error"]["code"] == (
        "INVALID_REQUEST"
    )
    assert "not-json" not in invalid_errors.getvalue()


def test_cli_treats_execution_validation_error_as_safe_tool_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_value = "raw-provider-document-secret"
    with pytest.raises(ValidationError) as captured:
        PreflightDocumentRequest.model_validate(
            {
                "job_root": tmp_path,
                "job_id": "execution-validation",
                "input_relpath": {"sensitive": sensitive_value},
            }
        )

    def fail_during_execution(_tool: str, _payload: dict[str, Any]) -> None:
        raise captured.value

    monkeypatch.setattr(
        "format_translator.agent_tool_cli.invoke_tool",
        fail_during_execution,
    )
    output = io.StringIO()
    errors = io.StringIO()

    exit_code = agent_tool_main(
        [
            "preflight_document",
            "--job-root",
            str(tmp_path),
            "--job-id",
            "execution-validation",
        ],
        stdin=io.StringIO('{"input_relpath":"input/source.docx"}'),
        stdout=output,
        stderr=errors,
    )

    envelope = json.loads(output.getvalue())
    assert exit_code == EXIT_EXECUTION_FAILED
    assert len(output.getvalue().splitlines()) == 1
    assert envelope["status"] == "FAILED"
    assert envelope["error"] == {
        "code": "TOOL_EXECUTION_FAILED",
        "message": "agent tool execution failed",
        "retryable": False,
    }
    assert sensitive_value not in output.getvalue()
    assert sensitive_value not in errors.getvalue()
    assert errors.getvalue() == "[TOOL_EXECUTION_FAILED] ValidationError\n"


def test_cli_keeps_schema_invalid_payload_as_invalid_request(tmp_path: Path) -> None:
    sensitive_value = "raw-invalid-document-secret"
    output = io.StringIO()
    errors = io.StringIO()

    exit_code = agent_tool_main(
        [
            "preflight_document",
            "--job-root",
            str(tmp_path),
            "--job-id",
            "invalid-schema",
        ],
        stdin=io.StringIO(
            json.dumps({"input_relpath": {"sensitive": sensitive_value}})
        ),
        stdout=output,
        stderr=errors,
    )

    envelope = json.loads(output.getvalue())
    assert exit_code == EXIT_REQUEST_INVALID
    assert len(output.getvalue().splitlines()) == 1
    assert envelope["status"] == "FAILED"
    assert envelope["error"] == {
        "code": "INVALID_REQUEST",
        "message": "agent tool request failed JSON or schema validation",
        "retryable": False,
    }
    assert sensitive_value not in output.getvalue()
    assert sensitive_value not in errors.getvalue()
    assert errors.getvalue() == "[INVALID_REQUEST] ValidationError\n"
