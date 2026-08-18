from __future__ import annotations

import asyncio
import math
import zipfile
from pathlib import Path

import httpx
import pytest

import format_translator.pipeline as pipeline
from format_translator.models import (
    ArtifactType,
    IssueSeverity,
    JobSpec,
    JobStatus,
    ProtectedText,
    ProtectedToken,
    QaIssue,
    SemanticQaResult,
    TextSegment,
    TextFitResult,
    TranslationBlock,
    TranslationResult,
)
from format_translator.pipeline import (
    _attempt_text_fit,
    _make_batches,
    _protect_segments,
    _semantic_qa,
    _translate_blocks,
    _validate_and_restore_results,
    inspect_document,
    translate_document,
)
from format_translator.processing_provider import OpenAIProcessingProvider
from format_translator.providers import (
    FixtureProvider,
    ProviderError,
    ProviderProtocolError,
    RetryableProviderError,
)

class _RecordingFixtureProvider(FixtureProvider):
    def __init__(self) -> None:
        super().__init__({}, missing="passthrough")
        self.calls: list[tuple[str, str]] = []

    async def translate(self, blocks, *, source_language, target_language, request_id):
        self.calls.append((source_language, target_language))
        return await super().translate(
            blocks,
            source_language=source_language,
            target_language=target_language,
            request_id=request_id,
        )


def _ordinary_docx_segment(
    index: int,
    text: str,
    *,
    style_id: str = "body-style",
    in_hyperlink: bool = False,
    in_field: bool = False,
    metadata: dict[str, object] | None = None,
) -> TextSegment:
    segment_metadata: dict[str, object] = {
        "xpath": f"/w:document/w:body/w:p/w:r[{index + 1}]/w:t",
        "run_index": index,
        "in_hyperlink": in_hyperlink,
        "in_field": in_field,
    }
    if metadata:
        segment_metadata.update(metadata)
    return TextSegment(
        segment_id=f"t{index}",
        text=text,
        style_id=style_id,
        metadata=segment_metadata,
    )


def _write_minimal_docx(path: Path, text: str) -> None:
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
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>""",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


def _write_minimal_pptx(path: Path, text: str) -> None:
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
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


@pytest.mark.parametrize("document_format", ["docx", "pptx"])
def test_pipeline_empty_office_document_is_verified_noop_without_provider_call(
    tmp_path: Path,
    document_format: str,
) -> None:
    source = tmp_path / f"empty.{document_format}"
    if document_format == "docx":
        _write_minimal_docx(source, "")
    else:
        _write_minimal_pptx(source, "")

    provider = _RecordingFixtureProvider()
    result = translate_document(
        JobSpec(
            job_id=f"empty-{document_format}",
            input_path=source,
            output_dir=tmp_path,
            source_language="auto",
            target_language="auto",
            semantic_qa=True,
            auto_qa_repair=False,
        ),
        provider,
    )

    assert result.status == JobStatus.COMPLETED
    assert provider.calls == []
    assert result.metadata["no_translatable_content"] is True
    assert result.metadata["provider_invoked"] is False
    assert result.metadata["block_count"] == 0
    assert result.metadata["source_language"] == "und"
    assert result.requested_target_language == "auto"
    assert result.resolved_target_language == "zh"
    assert [issue.code for issue in result.issues] == ["NO_TRANSLATABLE_CONTENT"]
    candidate = next(
        artifact
        for artifact in result.artifacts
        if artifact.artifact_type == ArtifactType.TRANSLATED_DOCUMENT
    )
    normalized = Path(result.metadata["normalized_path"])
    assert candidate.path.suffix.lower() == f".{document_format}"
    assert candidate.sha256 == pipeline.sha256_file(normalized)


def test_pipeline_auto_target_routes_explicit_english_to_chinese(
    tmp_path: Path,
) -> None:
    source = tmp_path / "english.docx"
    _write_minimal_docx(source, "Economic outlook for 2026")

    provider = _RecordingFixtureProvider()
    result = translate_document(
        JobSpec(
            job_id="auto-en-to-zh",
            input_path=source,
            output_dir=tmp_path,
            source_language="English",
            target_language="auto",
            semantic_qa=False,
            layout_qa=False,
            auto_qa_repair=False,
        ),
        provider,
    )

    assert result.status == JobStatus.COMPLETED
    assert provider.calls == [("en", "zh")]
    assert result.requested_target_language == "auto"
    assert result.resolved_target_language == "zh"
    assert result.target_language == "zh"


def test_pipeline_auto_target_routes_explicit_chinese_to_english(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chinese.docx"
    _write_minimal_docx(source, "二〇二六年经济展望")
    provider = _RecordingFixtureProvider()

    result = translate_document(
        JobSpec(
            job_id="auto-zh-to-en",
            input_path=source,
            output_dir=tmp_path,
            source_language="zh-CN",
            target_language="auto",
            semantic_qa=False,
            layout_qa=False,
            auto_qa_repair=False,
        ),
        provider,
    )

    assert result.status == JobStatus.COMPLETED
    assert provider.calls == [("zh", "en")]
    assert result.requested_target_language == "auto"
    assert result.resolved_target_language == "en"


def test_pipeline_auto_source_detection_routes_russian_to_chinese(
    tmp_path: Path,
) -> None:
    source = tmp_path / "russian.docx"
    _write_minimal_docx(source, "Экономический прогноз на 2026 год")
    provider = _RecordingFixtureProvider()

    result = translate_document(
        JobSpec(
            job_id="auto-ru-to-zh",
            input_path=source,
            output_dir=tmp_path,
            source_language="auto",
            target_language="auto",
            semantic_qa=False,
            layout_qa=False,
            auto_qa_repair=False,
        ),
        provider,
    )

    assert result.status == JobStatus.COMPLETED
    assert provider.calls == [("ru", "zh")]
    assert result.metadata["source_language"] == "ru"
    assert result.metadata["language_detection"] == {
        "family": "cyrillic",
        "confidence": 0.98,
        "method": "script",
        "ambiguous": False,
    }


def test_translation_results_carry_language_metadata_for_office_writeback() -> None:
    class EchoProvider:
        name = "echo"

        async def translate(self, blocks, **_kwargs):
            return [
                TranslationResult(
                    block_id=block.block_id,
                    translated_text="译文",
                    provider=self.name,
                    metadata={"existing": True},
                )
                for block in blocks
            ]

    results = asyncio.run(
        _translate_blocks(
            [TranslationBlock(block_id="b1", source_text="Source")],
            provider=EchoProvider(),
            source_language="en",
            target_language="zh",
            job_id="language-metadata",
            concurrency=1,
            retries=0,
        )
    )

    assert results[0].metadata == {
        "existing": True,
        "source_language": "en",
        "target_language": "zh",
    }


def test_semantic_qa_provider_outage_is_one_blocking_candidate_issue() -> None:
    class UnavailableProvider:
        calls = 0

        async def semantic_qa(self, **_kwargs):
            self.calls += 1
            raise RetryableProviderError(
                "upstream unavailable",
                http_attempts=2,
            )

    blocks = [
        TranslationBlock(
            block_id=f"b{index}",
            source_text=f"source {index}",
            source_language="en",
            target_language="zh",
        )
        for index in range(5)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = UnavailableProvider()
    issues = asyncio.run(
        _semantic_qa(
            blocks,
            results,
            provider,
            "semantic-retry",
            3,
        )
    )

    assert provider.calls == 2
    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    issue = issues[0]
    assert issue.severity.value == "error"
    assert issue.block_id is None
    assert issue.details["status"] == "qa_unavailable"
    assert issue.details["category"] == "infrastructure"
    assert issue.details["total_blocks"] == 5
    assert issue.details["checked_blocks"] == 0
    assert issue.details["unchecked_blocks"] == 5
    assert issue.details["provider_calls"] == 2
    assert issue.details["retry_attempts"] == 1
    assert issue.details["retryable_retry_attempts"] == 1
    assert issue.details["stop_reason"] == "provider_retryable_error_exhausted"


def test_semantic_qa_unavailability_is_not_duplicated_as_a_quality_defect(
    tmp_path: Path,
) -> None:
    class UnavailableProcessingProvider:
        name = "unavailable-processing"
        calls = 0

        async def semantic_qa(self, **_kwargs):
            self.calls += 1
            raise RetryableProviderError(
                "upstream unavailable",
                http_attempts=2,
            )

    source = tmp_path / "semantic-qa-source.docx"
    _write_minimal_docx(source, "A sanitized source paragraph for semantic QA.")
    processing_provider = UnavailableProcessingProvider()
    manifest = translate_document(
        JobSpec(
            job_id="semantic-qa-unavailable",
            input_path=source,
            output_dir=tmp_path,
            source_language="en",
            target_language="zh",
            max_translation_concurrency=2,
        ),
        FixtureProvider({}, missing="passthrough"),
        processing_provider,
    )

    assert processing_provider.calls == 2
    assert manifest.status == JobStatus.NEEDS_REVIEW
    error_issues = [
        issue for issue in manifest.issues if issue.severity == IssueSeverity.ERROR
    ]
    assert [issue.code for issue in error_issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    assert error_issues[0].details["status"] == "qa_unavailable"
    assert error_issues[0].details["category"] == "infrastructure"
    assert error_issues[0].details["http_attempts"] == 4
    assert error_issues[0].details["http_attempts_reporting_complete"] is True
    semantic_timing = next(
        timing for timing in manifest.timings if timing.stage == "semantic_qa"
    )
    assert semantic_timing.metadata["logical_provider_calls"] == 2
    assert semantic_timing.metadata["http_attempts"] == 4
    assert semantic_timing.metadata["http_attempts_reporting_complete"] is True


def test_semantic_qa_preserves_completed_chunks_when_provider_goes_down() -> None:
    completed_issue = QaIssue(
        code="SEMANTIC_STYLE_WARNING",
        message="The completed chunk needs a style review.",
        severity=IssueSeverity.WARNING,
        stage="semantic_qa",
        block_id="b0",
    )

    class PartiallyUnavailableProvider:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def semantic_qa(self, *, source, **_kwargs):
            self.calls[source.block_id] = self.calls.get(source.block_id, 0) + 1
            if source.block_id == "b1":
                raise RetryableProviderError("upstream unavailable")
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[completed_issue] if source.block_id == "b0" else [],
                provider="partial-fixture",
            )

    blocks = [
        TranslationBlock(
            block_id=f"b{index}",
            source_text=f"source {index}",
            source_language="en",
            target_language="zh",
        )
        for index in range(3)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = PartiallyUnavailableProvider()

    issues = asyncio.run(
        _semantic_qa(blocks, results, provider, "partial-semantic-qa", 1)
    )

    assert provider.calls == {"b0": 1, "b1": 2}
    assert [issue.code for issue in issues] == [
        "SEMANTIC_STYLE_WARNING",
        "SEMANTIC_QA_PROVIDER_FAILED",
    ]
    assert issues[0] == completed_issue
    assert issues[1].details["checked_blocks"] == 1
    assert issues[1].details["unchecked_block_ids"] == ["b1", "b2"]


def test_semantic_qa_retries_protocol_error_per_block_without_stopping_candidate() -> (
    None
):
    class ProtocolProvider:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def semantic_qa(self, *, source, **_kwargs):
            calls = self.calls.get(source.block_id, 0) + 1
            self.calls[source.block_id] = calls
            if source.block_id == "b0" and calls == 1:
                raise ProviderProtocolError("malformed block response")
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[],
                provider="protocol-fixture",
            )

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(3)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = ProtocolProvider()

    issues = asyncio.run(
        _semantic_qa(blocks, results, provider, "protocol-recovers", 2)
    )

    assert issues == []
    assert provider.calls == {"b0": 2, "b1": 1, "b2": 1}


def test_semantic_qa_persistent_protocol_error_is_block_scoped() -> None:
    class ProtocolProvider:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def semantic_qa(self, *, source, **_kwargs):
            self.calls[source.block_id] = self.calls.get(source.block_id, 0) + 1
            if source.block_id == "b0":
                raise ProviderProtocolError("malformed block response")
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[],
                provider="protocol-fixture",
            )

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(3)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = ProtocolProvider()

    issues = asyncio.run(
        _semantic_qa(blocks, results, provider, "protocol-persists", 2)
    )

    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    assert provider.calls == {"b0": 2, "b1": 1, "b2": 1}
    failure = issues[0]
    assert failure.details["checked_blocks"] == 2
    assert failure.details["unchecked_block_ids"] == ["b0"]
    assert failure.details["retry_attempts"] == 1
    assert failure.details["provider_calls"] == 4


def test_semantic_qa_systematic_protocol_outage_stops_after_three_blocks() -> None:
    class SystematicallyMalformedProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def semantic_qa(self, *, source, **_kwargs):
            self.calls[source.block_id] = self.calls.get(source.block_id, 0) + 1
            raise ProviderProtocolError(
                "malformed block response",
                http_attempts=3,
            )

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(9)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = SystematicallyMalformedProvider()

    issues = asyncio.run(
        _semantic_qa(blocks, results, provider, "systematic-protocol", 9)
    )

    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    assert provider.calls == {"b0": 2, "b1": 2, "b2": 2}
    failure = issues[0]
    assert failure.details["provider_calls"] == 6
    assert failure.details["http_attempts"] == 18
    assert failure.details["http_attempts_reporting_complete"] is True
    assert failure.details["logical_call_limit"] == 12
    assert failure.details["stop_reason"] == "systematic_protocol_error"
    assert failure.details["checked_blocks"] == 0
    assert failure.details["unchecked_blocks"] == 9


@pytest.mark.parametrize(
    ("concurrency", "expected_call_count"),
    [(1, 7), (9, 12)],
)
def test_semantic_qa_detects_protocol_outage_after_a_checked_block(
    concurrency: int,
    expected_call_count: int,
) -> None:
    class LaterMalformedProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def semantic_qa(self, *, source, **_kwargs):
            self.calls[source.block_id] = self.calls.get(source.block_id, 0) + 1
            if source.block_id != "b0":
                raise ProviderProtocolError("malformed block response")
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[],
                provider="later-protocol",
            )

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(9)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = LaterMalformedProvider()

    issues = asyncio.run(
        _semantic_qa(blocks, results, provider, "later-protocol", concurrency)
    )

    assert sum(provider.calls.values()) == expected_call_count
    if concurrency == 1:
        assert provider.calls == {"b0": 1, "b1": 2, "b2": 2, "b3": 2}
    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    failure = issues[0]
    assert failure.details["provider_calls"] == expected_call_count
    assert failure.details["retry_attempts"] == 3
    assert failure.details["stop_reason"] == "systematic_protocol_error"
    assert failure.details["checked_blocks"] == 1
    assert failure.details["unchecked_blocks"] == 8


def test_semantic_qa_first_terminal_provider_reason_wins_protocol_race() -> None:
    class RacingProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.protocol_retries_started = 0
            self.release_protocol_retries = asyncio.Event()

        async def semantic_qa(self, *, source, request_id, **_kwargs):
            if source.block_id == "b0":
                return SemanticQaResult(
                    block_id=source.block_id,
                    passed=True,
                    issues=[],
                    provider="racing",
                )
            if source.block_id == "b4":
                while self.protocol_retries_started < 3:
                    await asyncio.sleep(0)
                asyncio.get_running_loop().call_later(
                    0.01,
                    self.release_protocol_retries.set,
                )
                raise ProviderError("nonretryable terminal failure")
            if request_id.endswith("-protocol-r1"):
                self.protocol_retries_started += 1
                await self.release_protocol_retries.wait()
            raise ProviderProtocolError("malformed response")

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(5)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]

    issues = asyncio.run(
        _semantic_qa(blocks, results, RacingProvider(), "stop-race", 4)
    )

    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    failure = issues[0]
    assert failure.details["stop_reason"] == "provider_nonretryable_error"
    assert failure.details["secondary_stop_reasons"] == ["systematic_protocol_error"]
    assert failure.details["provider_error_types"] == [
        "ProviderError",
        "ProviderProtocolError",
    ]


def test_semantic_qa_first_terminal_budget_reason_wins_provider_race() -> None:
    class BudgetRacingProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.protocol_retries_started = 0
            self.release_in_flight = asyncio.Event()

        async def semantic_qa(self, *, source, request_id, **_kwargs):
            if source.block_id == "b0":
                return SemanticQaResult(
                    block_id=source.block_id,
                    passed=True,
                    issues=[],
                    provider="budget-racing",
                )
            if source.block_id == "b5":
                while self.protocol_retries_started < 3:
                    await asyncio.sleep(0)
                await self.release_in_flight.wait()
                raise ProviderError("late nonretryable failure")
            if request_id.endswith("-protocol-r1"):
                self.protocol_retries_started += 1
                if self.protocol_retries_started == 3:
                    asyncio.get_running_loop().call_later(
                        0.01,
                        self.release_in_flight.set,
                    )
                await self.release_in_flight.wait()
            raise ProviderProtocolError("malformed response")

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(6)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]

    issues = asyncio.run(
        _semantic_qa(
            blocks,
            results,
            BudgetRacingProvider(),
            "budget-stop-race",
            5,
        )
    )

    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    failure = issues[0]
    assert failure.details["stop_reason"] == "recovery_call_budget_exhausted"
    assert set(failure.details["secondary_stop_reasons"]) == {
        "provider_nonretryable_error",
        "systematic_protocol_error",
    }
    assert failure.details["recovery_provider_calls"] == 3


def test_semantic_qa_candidate_recovery_budget_is_three_for_100_blocks() -> None:
    class AlternatingMalformedProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.calls: dict[str, int] = {}
            self.request_ids: list[str] = []

        async def semantic_qa(self, *, source, request_id, **_kwargs):
            self.calls[source.block_id] = self.calls.get(source.block_id, 0) + 1
            self.request_ids.append(request_id)
            index = int(source.block_id.removeprefix("b"))
            if index % 2:
                raise ProviderProtocolError("malformed block response")
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[],
                provider="alternating-protocol",
            )

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(100)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = AlternatingMalformedProvider()
    audit_metadata: dict[str, object] = {}

    issues = asyncio.run(
        _semantic_qa(
            blocks,
            results,
            provider,
            "logical-budget",
            1,
            audit_metadata=audit_metadata,
        )
    )

    assert sum(provider.calls.values()) == 11
    assert len(provider.request_ids) == len(set(provider.request_ids)) == 11
    assert provider.request_ids[:3] == [
        "logical-budget-qa-b0",
        "logical-budget-qa-b1",
        "logical-budget-qa-b1-protocol-r1",
    ]
    assert audit_metadata["provider_call_attempts"][:3] == [
        {
            "request_id": "logical-budget-qa-b0",
            "parent_request_id": "logical-budget-qa-b0",
            "attempt": 1,
            "attempt_kind": "initial",
        },
        {
            "request_id": "logical-budget-qa-b1",
            "parent_request_id": "logical-budget-qa-b1",
            "attempt": 1,
            "attempt_kind": "initial",
        },
        {
            "request_id": "logical-budget-qa-b1-protocol-r1",
            "parent_request_id": "logical-budget-qa-b1",
            "attempt": 2,
            "attempt_kind": "protocol_retry",
        },
    ]
    assert "b8" not in provider.calls
    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    failure = issues[0]
    assert failure.details["logical_call_limit"] == 103
    assert failure.details["initial_provider_calls"] == 8
    assert failure.details["recovery_provider_calls"] == 3
    assert failure.details["recovery_call_limit"] == 3
    assert failure.details["provider_calls"] == 11
    assert failure.details["stop_reason"] == "recovery_call_budget_exhausted"


def test_semantic_qa_candidate_wall_clock_deadline_cancels_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "MAX_SEMANTIC_QA_SECONDS", 0.01)

    class BlockingProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.calls = 0
            self.cancelled = False

        async def semantic_qa(self, **_kwargs):
            self.calls += 1
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            source = _kwargs["source"]
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[],
                provider="blocking",
            )

    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(3)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]
    provider = BlockingProvider()

    issues = asyncio.run(
        _semantic_qa(blocks, results, provider, "semantic-deadline", 3)
    )

    assert provider.calls == 1
    assert provider.cancelled is True
    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    assert issues[0].details["stop_reason"] == "wall_clock_budget_exhausted"
    assert issues[0].details["wall_clock_limit_seconds"] == 0.01


def test_semantic_qa_external_cancellation_stops_queued_checks() -> None:
    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"source {index}")
        for index in range(3)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=f"translation {index}",
            provider="fixture",
        )
        for index, block in enumerate(blocks)
    ]

    class BlockingProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.cancelled = False

        async def semantic_qa(self, **_kwargs):
            self.calls += 1
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    async def cancel_candidate() -> tuple[int, bool]:
        provider = BlockingProvider()
        task = asyncio.create_task(
            _semantic_qa(
                blocks,
                results,
                provider,
                "semantic-cancelled",
                3,
            )
        )
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return provider.calls, provider.cancelled

    assert asyncio.run(cancel_candidate()) == (1, True)


def test_semantic_qa_pipeline_blocks_generic_failed_result_without_error() -> None:
    class GenericProcessingProvider:
        async def semantic_qa(self, *, source, **_kwargs):
            return SemanticQaResult(
                block_id=source.block_id,
                passed=False,
                issues=[],
                provider="generic-processing",
            )

    block = TranslationBlock(block_id="b0", source_text="source")
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="translation",
        provider="fixture",
    )

    issues = asyncio.run(
        _semantic_qa(
            [block],
            [result],
            GenericProcessingProvider(),
            "generic-failed",
            1,
        )
    )

    assert [issue.code for issue in issues] == ["SEMANTIC_QA_REVIEW_REQUIRED"]
    review = issues[0]
    assert review.severity == IssueSeverity.ERROR
    assert review.block_id == block.block_id
    assert review.details["status"] == "provider_output_inconsistent"
    assert review.details["provider_passed"] is False


def test_semantic_qa_wrong_result_block_id_retries_and_recovers() -> None:
    class WrongThenCorrectProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.calls = 0
            self.request_ids: list[str] = []

        async def semantic_qa(self, *, source, request_id, **_kwargs):
            self.calls += 1
            self.request_ids.append(request_id)
            if self.calls == 1:
                return SemanticQaResult(
                    block_id="other-block",
                    passed=True,
                    issues=[],
                    provider="wrong-then-correct",
                )
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[
                    QaIssue(
                        code="SEMANTIC_STYLE_WARNING",
                        message="Review the translated tone.",
                        severity=IssueSeverity.WARNING,
                        stage="semantic_qa",
                    )
                ],
                provider="wrong-then-correct",
            )

    block = TranslationBlock(block_id="b0", source_text="source")
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="translation",
        provider="fixture",
    )
    provider = WrongThenCorrectProvider()

    issues = asyncio.run(
        _semantic_qa([block], [result], provider, "wrong-block-recover", 1)
    )

    assert provider.request_ids == [
        "wrong-block-recover-qa-b0",
        "wrong-block-recover-qa-b0-protocol-r1",
    ]
    assert [issue.code for issue in issues] == ["SEMANTIC_STYLE_WARNING"]
    assert issues[0].block_id == "b0"


def test_semantic_qa_persistent_wrong_result_block_id_fails_closed() -> None:
    class WrongBlockProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def semantic_qa(self, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            return SemanticQaResult(
                block_id="other-block",
                passed=True,
                issues=[],
                provider="wrong-block",
            )

    block = TranslationBlock(block_id="b0", source_text="source")
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="translation",
        provider="fixture",
    )
    provider = WrongBlockProvider()

    issues = asyncio.run(
        _semantic_qa([block], [result], provider, "wrong-block-fails", 1)
    )

    assert provider.request_ids == [
        "wrong-block-fails-qa-b0",
        "wrong-block-fails-qa-b0-protocol-r1",
    ]
    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    failure = issues[0]
    assert failure.details["checked_blocks"] == 0
    assert failure.details["unchecked_block_ids"] == ["b0"]
    assert failure.details["provider_error_types"] == ["ProviderProtocolError"]
    assert failure.details["stop_reason"] == "provider_protocol_error"


def test_semantic_qa_cross_block_issue_retries_without_polluting_location() -> None:
    class CrossBlockIssueProvider:
        manages_semantic_qa_retries = True

        def __init__(self) -> None:
            self.calls = 0
            self.request_ids: list[str] = []

        async def semantic_qa(self, *, source, request_id, **_kwargs):
            self.calls += 1
            self.request_ids.append(request_id)
            issues = (
                [
                    QaIssue(
                        code="CROSS_BLOCK",
                        message="This issue belongs to another block.",
                        block_id="other-block",
                    )
                ]
                if self.calls == 1
                else []
            )
            return SemanticQaResult(
                block_id=source.block_id,
                passed=not issues,
                issues=issues,
                provider="cross-block-issue",
            )

    block = TranslationBlock(block_id="b0", source_text="source")
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="translation",
        provider="fixture",
    )
    provider = CrossBlockIssueProvider()

    issues = asyncio.run(
        _semantic_qa([block], [result], provider, "cross-block-issue", 1)
    )

    assert issues == []
    assert provider.request_ids == [
        "cross-block-issue-qa-b0",
        "cross-block-issue-qa-b0-protocol-r1",
    ]


@pytest.mark.parametrize("declares_capability", [True, False])
def test_semantic_qa_generic_provider_gets_one_bounded_retry(
    declares_capability: bool,
) -> None:
    class RetryOnceProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.request_ids: list[str] = []
            if declares_capability:
                self.manages_semantic_qa_retries = False

        async def semantic_qa(self, *, source, request_id, **_kwargs):
            self.calls += 1
            self.request_ids.append(request_id)
            if self.calls == 1:
                raise RetryableProviderError("temporary generic outage")
            return SemanticQaResult(
                block_id=source.block_id,
                passed=True,
                issues=[],
                provider="generic-processing",
            )

    block = TranslationBlock(block_id="b0", source_text="source")
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="translation",
        provider="fixture",
    )
    provider = RetryOnceProvider()

    issues = asyncio.run(_semantic_qa([block], [result], provider, "generic-retry", 1))

    assert issues == []
    assert provider.calls == 2
    assert provider.request_ids == [
        "generic-retry-qa-b0",
        "generic-retry-qa-b0-retryable-r1",
    ]


@pytest.mark.parametrize(
    ("scenario", "expected_suffix", "expected_reason"),
    [
        ("initial_nonretryable", None, "provider_nonretryable_error"),
        (
            "retryable_then_nonretryable",
            "-retryable-r1",
            "provider_nonretryable_error",
        ),
        (
            "protocol_then_nonretryable",
            "-protocol-r1",
            "provider_nonretryable_error",
        ),
        (
            "protocol_then_retryable",
            "-protocol-r1",
            "provider_retryable_error_exhausted",
        ),
    ],
)
def test_semantic_qa_terminal_stop_reason_is_typed_and_secret_free(
    scenario: str,
    expected_suffix: str | None,
    expected_reason: str,
) -> None:
    class TerminalProvider:
        manages_semantic_qa_retries = scenario != "retryable_then_nonretryable"

        def __init__(self) -> None:
            self.calls = 0
            self.request_ids: list[str] = []

        async def semantic_qa(self, *, request_id, **_kwargs):
            self.calls += 1
            self.request_ids.append(request_id)
            if self.calls == 1 and scenario == "retryable_then_nonretryable":
                raise RetryableProviderError("temporary")
            if self.calls == 1 and scenario == "protocol_then_nonretryable":
                raise ProviderProtocolError("malformed")
            if self.calls == 1 and scenario == "protocol_then_retryable":
                raise ProviderProtocolError("malformed")
            if scenario == "protocol_then_retryable":
                raise RetryableProviderError("super-secret-retryable-message")
            raise ProviderError("super-secret-provider-message")

    block = TranslationBlock(block_id="b0", source_text="source")
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="translation",
        provider="fixture",
    )
    provider = TerminalProvider()
    audit_metadata: dict[str, object] = {}

    issues = asyncio.run(
        _semantic_qa(
            [block],
            [result],
            provider,
            "typed-stop",
            1,
            audit_metadata=audit_metadata,
        )
    )

    expected_ids = ["typed-stop-qa-b0"]
    if expected_suffix is not None:
        expected_ids.append(f"typed-stop-qa-b0{expected_suffix}")
    assert provider.request_ids == expected_ids
    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    failure = issues[0]
    assert failure.details["stop_reason"] == expected_reason
    assert audit_metadata["stop_reason"] == expected_reason
    assert "super-secret" not in f"{failure.message}{failure.details}"


def test_semantic_qa_does_not_multiply_exhausted_internal_http_retries() -> None:
    http_attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_attempts
        http_attempts += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProcessingProvider(
        base_url="https://api.test/v1",
        model="qa-model",
        timeout_seconds=1,
        max_request_retries=2,
        retry_backoff_seconds=0,
        retry_jitter_seconds=0,
        client=client,
    )
    block = TranslationBlock(block_id="b0", source_text="source")
    result = TranslationResult(
        block_id="b0",
        translated_text="translation",
        provider="fixture",
    )
    audit_metadata: dict[str, object] = {}
    try:
        issues = asyncio.run(
            _semantic_qa(
                [block],
                [result],
                provider,
                "double-retry",
                1,
                audit_metadata=audit_metadata,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert http_attempts == 3
    assert [issue.code for issue in issues] == ["SEMANTIC_QA_PROVIDER_FAILED"]
    assert issues[0].details["provider_calls"] == 1
    assert issues[0].details["retry_attempts"] == 0
    assert issues[0].details["stop_reason"] == "provider_retryable_error_exhausted"
    assert issues[0].details["http_attempts"] == 3
    assert issues[0].details["http_attempts_reporting_complete"] is True
    assert audit_metadata["logical_provider_calls"] == 1
    assert audit_metadata["http_attempts"] == 3
    assert audit_metadata["http_attempts_reporting_complete"] is True
    assert audit_metadata["provider_call_attempts"] == [
        {
            "request_id": "double-retry-qa-b0",
            "parent_request_id": "double-retry-qa-b0",
            "attempt": 1,
            "attempt_kind": "initial",
        }
    ]


def test_batches_plain_and_tagged_blocks_without_fragmenting_on_order() -> None:
    translation_blocks = [
        TranslationBlock(
            block_id=f"b{index}",
            source_text=f"text {index}",
            protocol="tagged" if index % 4 == 0 else "plain",
            segments=(
                [TextSegment(segment_id=f"s{index}", text=f"text {index}")]
                if index % 4 == 0
                else []
            ),
        )
        for index in range(34)
    ]

    batches = _make_batches(translation_blocks)

    assert sum(len(batch) for batch in batches) == 34
    assert all(len({block.protocol for block in batch}) == 1 for batch in batches)
    assert max(len(batch) for batch in batches if batch[0].protocol == "plain") <= 20
    assert max(len(batch) for batch in batches if batch[0].protocol == "tagged") <= 4


@pytest.mark.parametrize("initial_batch_count", [0, 1, 36])
def test_provider_recovery_wall_time_keeps_small_candidates_at_default(
    initial_batch_count: int,
) -> None:
    assert (
        pipeline._provider_recovery_seconds_for_initial_batches(initial_batch_count)
        == pipeline.DEFAULT_PROVIDER_RECOVERY_SECONDS
    )


@pytest.mark.parametrize("initial_batch_count", [0, 1, 36])
def test_provider_recovery_call_cap_keeps_small_candidates_at_default(
    initial_batch_count: int,
) -> None:
    assert (
        pipeline._provider_recovery_calls_for_initial_batches(initial_batch_count)
        == pipeline.DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS
    )


@pytest.mark.parametrize(
    ("initial_batch_count", "expected_calls"),
    [(37, 17), (66, 24), (80, 27), (104, 32)],
)
def test_provider_recovery_call_cap_scales_with_large_candidates(
    initial_batch_count: int,
    expected_calls: int,
) -> None:
    assert (
        pipeline._provider_recovery_calls_for_initial_batches(initial_batch_count)
        == expected_calls
    )


@pytest.mark.parametrize("initial_batch_count", [100, 101, 10_000])
def test_provider_recovery_call_cap_stays_bounded_for_very_large_candidates(
    initial_batch_count: int,
) -> None:
    assert (
        pipeline._provider_recovery_calls_for_initial_batches(initial_batch_count)
        == pipeline.MAX_EXTRA_PROVIDER_RECOVERY_CALLS
    )


@pytest.mark.parametrize(
    ("initial_batch_count", "expected_seconds"),
    [(37, 187.5), (66, 405.0), (80, 510.0), (104, 600.0)],
)
def test_provider_recovery_wall_time_scales_with_large_candidates(
    initial_batch_count: int,
    expected_seconds: float,
) -> None:
    assert (
        pipeline._provider_recovery_seconds_for_initial_batches(initial_batch_count)
        == expected_seconds
    )


@pytest.mark.parametrize("initial_batch_count", [92, 93, 10_000])
def test_provider_recovery_wall_time_caps_very_large_candidates(
    initial_batch_count: int,
) -> None:
    assert (
        pipeline._provider_recovery_seconds_for_initial_batches(initial_batch_count)
        == pipeline.MAX_PROVIDER_RECOVERY_SECONDS
    )


def test_translate_blocks_uses_the_adaptive_candidate_recovery_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "DEFAULT_PROVIDER_RECOVERY_SECONDS", 0.02)
    monkeypatch.setattr(pipeline, "MIN_PROVIDER_RECOVERY_SECONDS", 0.02)
    monkeypatch.setattr(pipeline, "MAX_PROVIDER_RECOVERY_SECONDS", 0.25)
    monkeypatch.setattr(
        pipeline,
        "PROVIDER_RECOVERY_WALL_TIME_SCALING_START_BATCHES",
        0,
    )
    monkeypatch.setattr(
        pipeline,
        "PROVIDER_RECOVERY_SECONDS_PER_BATCH_ABOVE_DEFAULT",
        0.125,
    )
    blocks = [
        TranslationBlock(block_id="probe", source_text="probe"),
        TranslationBlock(block_id="main", source_text="main"),
    ]

    class SlowProbeRecoveryProvider:
        name = "slow-probe-recovery"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, batch, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            if request_id == "adaptive-budget-probe-b0-a0":
                return []
            if request_id == "adaptive-budget-probe-b0-strict-a0":
                await asyncio.sleep(0.08)
            return [
                TranslationResult(
                    block_id=block.block_id,
                    translated_text=f"translated {block.block_id}",
                    provider=self.name,
                )
                for block in batch
            ]

    provider = SlowProbeRecoveryProvider()
    results = asyncio.run(
        _translate_blocks(
            blocks,
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="adaptive-budget",
            concurrency=1,
            retries=0,
        )
    )

    assert [result.block_id for result in results] == ["probe", "main"]
    assert provider.request_ids == [
        "adaptive-budget-probe-b0-a0",
        "adaptive-budget-probe-b0-strict-a0",
        "adaptive-budget-main-b0-a0",
    ]


def test_translate_blocks_coalesces_large_equivalent_docx_runs_and_expands_exactly() -> (
    None
):
    original_segments = [
        _ordinary_docx_segment(index, f"word-{index} ") for index in range(32)
    ]
    block = TranslationBlock(
        block_id="docx-many-runs",
        source_text="".join(segment.text for segment in original_segments),
        protocol="tagged",
        segments=original_segments,
        metadata={
            "part_name": "word/document.xml",
            "risk_flags": ["multiple_text_nodes", "multiple_styles"],
        },
    )

    class CoalescedResponseProvider:
        name = "coalesced-response"

        def __init__(self) -> None:
            self.received: list[TranslationBlock] = []

        async def translate(self, blocks, **_kwargs):
            self.received.extend(blocks)
            provider_segment = blocks[0].segments[0]
            return [
                TranslationResult(
                    block_id=blocks[0].block_id,
                    translated_text="complete translation",
                    segments=[
                        provider_segment.model_copy(
                            update={"text": "complete translation"}
                        )
                    ],
                    provider=self.name,
                )
            ]

    provider = CoalescedResponseProvider()
    results = asyncio.run(
        _translate_blocks(
            [block],
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="docx-coalescing",
            concurrency=1,
            retries=0,
        )
    )

    assert len(provider.received) == 1
    assert [segment.segment_id for segment in provider.received[0].segments] == ["t0"]
    assert provider.received[0].segments[0].text == block.source_text
    assert [segment.segment_id for segment in results[0].segments] == [
        segment.segment_id for segment in original_segments
    ]
    assert [segment.text for segment in results[0].segments] == [
        "complete translation",
        *("" for _ in original_segments[1:]),
    ]
    assert [segment.style_id for segment in results[0].segments] == [
        segment.style_id for segment in original_segments
    ]
    assert [segment.metadata for segment in results[0].segments] == [
        segment.metadata for segment in original_segments
    ]
    assert "".join(segment.text for segment in results[0].segments) == (
        results[0].translated_text
    )


def test_docx_provider_coalescing_stops_at_format_and_context_boundaries() -> None:
    segments = [
        _ordinary_docx_segment(0, "A", style_id="style-a"),
        _ordinary_docx_segment(1, "B", style_id="style-a"),
        _ordinary_docx_segment(2, "C", style_id="style-b"),
        _ordinary_docx_segment(3, "D", style_id="style-b", in_hyperlink=True),
        _ordinary_docx_segment(4, "E", style_id="style-b"),
        _ordinary_docx_segment(5, "F", style_id="style-b", in_field=True),
        _ordinary_docx_segment(6, "G", style_id="style-b"),
        _ordinary_docx_segment(
            7,
            "H",
            style_id="style-b",
            metadata={"context_marker": "ambiguous"},
        ),
        _ordinary_docx_segment(8, "I", style_id="style-b"),
        _ordinary_docx_segment(9, "J", style_id="style-b"),
    ]
    block = TranslationBlock(
        block_id="docx-boundaries",
        source_text="ABCDEFGHIJ",
        protocol="tagged",
        segments=segments,
        metadata={
            "part_name": "word/document.xml",
            "risk_flags": ["multiple_text_nodes", "multiple_styles"],
        },
    )

    class BoundaryRecordingProvider:
        name = "boundary-recording"

        def __init__(self) -> None:
            self.received_segments: list[TextSegment] = []

        async def translate(self, blocks, **_kwargs):
            self.received_segments = list(blocks[0].segments)
            translated_segments = [
                segment.model_copy(update={"text": f"translated-{segment.segment_id}"})
                for segment in blocks[0].segments
            ]
            return [
                TranslationResult(
                    block_id=blocks[0].block_id,
                    translated_text="".join(
                        segment.text for segment in translated_segments
                    ),
                    segments=translated_segments,
                    provider=self.name,
                )
            ]

    provider = BoundaryRecordingProvider()
    result = asyncio.run(
        _translate_blocks(
            [block],
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="docx-boundaries",
            concurrency=1,
            retries=0,
        )
    )[0]

    assert [segment.segment_id for segment in provider.received_segments] == [
        "t0",
        "t2",
        "t3",
        "t4",
        "t5",
        "t6",
        "t7",
        "t8",
    ]
    assert [segment.text for segment in provider.received_segments] == [
        "AB",
        "C",
        "D",
        "E",
        "F",
        "G",
        "H",
        "IJ",
    ]
    assert [segment.segment_id for segment in result.segments] == [
        segment.segment_id for segment in segments
    ]
    assert [segment.text for segment in result.segments] == [
        "translated-t0",
        "",
        "translated-t2",
        "translated-t3",
        "translated-t4",
        "translated-t5",
        "translated-t6",
        "translated-t7",
        "translated-t8",
        "",
    ]
    assert result.segments[3].metadata["in_hyperlink"] is True
    assert result.segments[5].metadata["in_field"] is True
    assert result.segments[7].metadata["context_marker"] == "ambiguous"


@pytest.mark.parametrize(
    "structural_risk",
    [
        "manual_break",
        "tab",
        "bookmark",
        "comment_anchor",
        "tracked_change",
        "content_control",
        "drawing_or_object",
        "note_reference",
        "hyperlink",
        "field",
        "multiple_hyperlinks",
    ],
)
def test_docx_provider_coalescing_rejects_structural_risk_blocks(
    structural_risk: str,
) -> None:
    segments = [_ordinary_docx_segment(index, str(index)) for index in range(3)]
    block = TranslationBlock(
        block_id=f"docx-risk-{structural_risk}",
        source_text="012",
        protocol="tagged",
        segments=segments,
        metadata={
            "part_name": "word/document.xml",
            "risk_flags": ["multiple_text_nodes", structural_risk],
        },
    )

    class RiskRecordingProvider:
        name = "risk-recording"

        def __init__(self) -> None:
            self.received_ids: list[str] = []

        async def translate(self, blocks, **_kwargs):
            self.received_ids = [segment.segment_id for segment in blocks[0].segments]
            return [
                TranslationResult(
                    block_id=blocks[0].block_id,
                    translated_text=blocks[0].source_text,
                    segments=list(blocks[0].segments),
                    provider=self.name,
                )
            ]

    provider = RiskRecordingProvider()
    asyncio.run(
        _translate_blocks(
            [block],
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id=f"docx-risk-{structural_risk}",
            concurrency=1,
            retries=0,
        )
    )

    assert provider.received_ids == ["t0", "t1", "t2"]


def test_docx_provider_coalescing_preserves_protected_token_order() -> None:
    source_segments = [
        _ordinary_docx_segment(0, "Deadline "),
        _ordinary_docx_segment(1, "2026-"),
        _ordinary_docx_segment(2, "07-"),
        _ordinary_docx_segment(3, "29 now"),
    ]
    source_block = TranslationBlock(
        block_id="docx-protected",
        source_text="Deadline 2026-07-29 now",
        protocol="tagged",
        segments=source_segments,
        metadata={
            "part_name": "word/document.xml",
            "risk_flags": ["multiple_text_nodes", "multiple_styles"],
        },
    )
    prepared, protected_by_id = pipeline._prepare_blocks(
        [source_block],
        source_language="en",
        target_language="zh",
        glossary={},
    )

    class ProtectedTokenProvider:
        name = "protected-token-provider"

        def __init__(self) -> None:
            self.provider_text = ""

        async def translate(self, blocks, **_kwargs):
            assert len(blocks[0].segments) == 1
            placeholder = blocks[0].protected_tokens[0].placeholder
            self.provider_text = blocks[0].segments[0].text
            translated_text = f"Due {placeholder} now"
            return [
                TranslationResult(
                    block_id=blocks[0].block_id,
                    translated_text=translated_text,
                    segments=[
                        blocks[0]
                        .segments[0]
                        .model_copy(update={"text": translated_text})
                    ],
                    provider=self.name,
                )
            ]

    provider = ProtectedTokenProvider()
    raw_results = asyncio.run(
        _translate_blocks(
            prepared,
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="docx-protected",
            concurrency=1,
            retries=0,
        )
    )
    restored, issues = _validate_and_restore_results(
        [source_block],
        raw_results,
        protected_by_id,
        {},
        allow_fixture_unprotected=False,
    )

    placeholder = prepared[0].protected_tokens[0].placeholder
    assert provider.provider_text.count(placeholder) == 1
    assert provider.provider_text.index(placeholder) == len("Deadline ")
    assert issues == []
    assert restored[0].translated_text == "Due 2026-07-29 now"
    assert [segment.segment_id for segment in restored[0].segments] == [
        segment.segment_id for segment in source_segments
    ]
    assert [segment.text for segment in restored[0].segments] == [
        "Due 2026-07-29 now",
        "",
        "",
        "",
    ]


@pytest.mark.parametrize(
    "provider_segment_ids",
    [
        ["t1"],
        ["t0", "extra"],
        [],
    ],
)
def test_docx_provider_coalescing_fails_closed_on_malformed_segment_ids(
    provider_segment_ids: list[str],
) -> None:
    segments = [_ordinary_docx_segment(index, str(index)) for index in range(4)]
    block = TranslationBlock(
        block_id="docx-malformed-ids",
        source_text="0123",
        protocol="tagged",
        segments=segments,
        metadata={
            "part_name": "word/document.xml",
            "risk_flags": ["multiple_text_nodes", "multiple_styles"],
        },
    )

    class MalformedIdProvider:
        name = "malformed-id-provider"

        async def translate(self, blocks, **_kwargs):
            return [
                TranslationResult(
                    block_id=blocks[0].block_id,
                    translated_text="translation",
                    segments=[
                        TextSegment(segment_id=segment_id, text="translation")
                        for segment_id in provider_segment_ids
                    ],
                    provider=self.name,
                )
            ]

    with pytest.raises(
        ProviderProtocolError,
        match="DOCX provider segment mismatch",
    ):
        asyncio.run(
            _translate_blocks(
                [block],
                provider=MalformedIdProvider(),
                source_language="en",
                target_language="zh",
                job_id="docx-malformed-ids",
                concurrency=1,
                retries=0,
            )
        )


def test_provider_segment_coalescing_does_not_change_pptx_blocks() -> None:
    segments = [_ordinary_docx_segment(index, chr(65 + index)) for index in range(4)]
    block = TranslationBlock(
        block_id="pptx-unchanged",
        source_text="ABCD",
        protocol="tagged",
        segments=segments,
        metadata={"part_name": "ppt/slides/slide1.xml"},
    )

    class PptxRecordingProvider:
        name = "pptx-recording"

        def __init__(self) -> None:
            self.received: list[TextSegment] = []

        async def translate(self, blocks, **_kwargs):
            self.received = list(blocks[0].segments)
            translated_segments = [
                segment.model_copy(update={"text": segment.text.lower()})
                for segment in blocks[0].segments
            ]
            return [
                TranslationResult(
                    block_id=blocks[0].block_id,
                    translated_text="abcd",
                    segments=translated_segments,
                    provider=self.name,
                )
            ]

    provider = PptxRecordingProvider()
    result = asyncio.run(
        _translate_blocks(
            [block],
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="pptx-unchanged",
            concurrency=1,
            retries=0,
        )
    )[0]

    assert provider.received == segments
    assert [segment.segment_id for segment in result.segments] == [
        "t0",
        "t1",
        "t2",
        "t3",
    ]
    assert [segment.text for segment in result.segments] == ["a", "b", "c", "d"]


def test_docx_strict_retry_reuses_coalesced_protocol_and_expands_result() -> None:
    source_segments = [
        _ordinary_docx_segment(0, "Hel"),
        _ordinary_docx_segment(1, "lo "),
        _ordinary_docx_segment(2, "wor"),
        _ordinary_docx_segment(3, "ld"),
    ]
    source_block = TranslationBlock(
        block_id="docx-strict-coalesced",
        source_text="Hello world",
        protocol="tagged",
        segments=source_segments,
        metadata={
            "part_name": "word/document.xml",
            "risk_flags": ["multiple_text_nodes", "multiple_styles"],
        },
    )
    prepared, protected_by_id = pipeline._prepare_blocks(
        [source_block],
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_result = TranslationResult(
        block_id=source_block.block_id,
        translated_text="correct translation",
        segments=[
            source_segments[0].model_copy(update={"text": "wrong"}),
            *(
                segment.model_copy(update={"text": ""})
                for segment in source_segments[1:]
            ),
        ],
        provider="initial-mismatch",
    )

    class StrictCoalescedProvider:
        name = "strict-coalesced"

        def __init__(self) -> None:
            self.calls: list[TranslationBlock] = []

        async def translate(self, blocks, **_kwargs):
            self.calls.extend(blocks)
            translated_text = "correct translation"
            return [
                TranslationResult(
                    block_id=blocks[0].block_id,
                    translated_text=translated_text,
                    segments=[
                        blocks[0]
                        .segments[0]
                        .model_copy(update={"text": translated_text})
                    ],
                    provider=self.name,
                )
            ]

    provider = StrictCoalescedProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            [source_block],
            prepared,
            [raw_result],
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="docx-strict-coalesced",
            allow_fixture_unprotected=False,
            concurrency=1,
        )
    )

    assert issues == []
    assert len(provider.calls) == 1
    assert [segment.segment_id for segment in provider.calls[0].segments] == ["t0"]
    assert provider.calls[0].segments[0].text == "Hello world"
    assert provider.calls[0].context["translation_retry"]["mode"] == (
        "strict_single_block"
    )
    assert [segment.segment_id for segment in restored[0].segments] == [
        segment.segment_id for segment in source_segments
    ]
    assert [segment.text for segment in restored[0].segments] == [
        "correct translation",
        "",
        "",
        "",
    ]


def test_probe_and_main_share_one_adaptive_recovery_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "DEFAULT_PROVIDER_RECOVERY_SECONDS", 0.02)
    monkeypatch.setattr(pipeline, "MIN_PROVIDER_RECOVERY_SECONDS", 0.02)
    monkeypatch.setattr(pipeline, "MAX_PROVIDER_RECOVERY_SECONDS", 0.25)
    monkeypatch.setattr(
        pipeline,
        "PROVIDER_RECOVERY_WALL_TIME_SCALING_START_BATCHES",
        0,
    )
    monkeypatch.setattr(
        pipeline,
        "PROVIDER_RECOVERY_SECONDS_PER_BATCH_ABOVE_DEFAULT",
        0.125,
    )
    blocks = [
        TranslationBlock(block_id="probe", source_text="probe"),
        TranslationBlock(block_id="main", source_text="main"),
    ]

    class TwoPhaseSlowRecoveryProvider:
        name = "two-phase-slow-recovery"

        def __init__(self) -> None:
            self.request_ids: list[str] = []
            self.main_recovery_cancelled = False

        async def translate(self, batch, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            if request_id.endswith("-a0") and "-strict-" not in request_id:
                return []
            if request_id.endswith("-strict-a0"):
                try:
                    await asyncio.sleep(0.18)
                except asyncio.CancelledError:
                    if "-main-" in request_id:
                        self.main_recovery_cancelled = True
                    raise
            return [
                TranslationResult(
                    block_id=block.block_id,
                    translated_text=f"translated {block.block_id}",
                    provider=self.name,
                )
                for block in batch
            ]

    provider = TwoPhaseSlowRecoveryProvider()
    with pytest.raises(
        RetryableProviderError,
        match="provider recovery exceeded 0.25 seconds",
    ):
        asyncio.run(
            _translate_blocks(
                blocks,
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="shared-budget",
                concurrency=1,
                retries=0,
            )
        )

    assert provider.request_ids == [
        "shared-budget-probe-b0-a0",
        "shared-budget-probe-b0-strict-a0",
        "shared-budget-main-b0-a0",
        "shared-budget-main-b0-strict-a0",
    ]
    assert provider.main_recovery_cancelled is True


def test_adaptive_call_cap_is_frozen_before_recovery_split_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS", 1)
    monkeypatch.setattr(pipeline, "MIN_EXTRA_PROVIDER_RECOVERY_CALLS", 1)
    monkeypatch.setattr(pipeline, "MAX_EXTRA_PROVIDER_RECOVERY_CALLS", 3)
    monkeypatch.setattr(
        pipeline,
        "PROVIDER_RECOVERY_CALL_SCALING_START_BATCHES",
        1,
    )
    monkeypatch.setattr(
        pipeline,
        "PROVIDER_RECOVERY_INITIAL_BATCHES_PER_EXTRA_CALL",
        1,
    )
    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"text {index}")
        for index in range(3)
    ]

    class ProtocolRecoveryProvider:
        name = "protocol-recovery"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, batch, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            if request_id in {
                "frozen-call-budget-probe-b0-a0",
                "frozen-call-budget-main-b0-a0",
            }:
                return []
            return [
                TranslationResult(
                    block_id=block.block_id,
                    translated_text=f"translated {block.block_id}",
                    provider=self.name,
                )
                for block in batch
            ]

    provider = ProtocolRecoveryProvider()
    with pytest.raises(
        ProviderProtocolError,
        match="provider recovery stopped after 2 extra calls",
    ):
        asyncio.run(
            _translate_blocks(
                blocks,
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="frozen-call-budget",
                concurrency=1,
                retries=0,
            )
        )

    assert provider.request_ids == [
        "frozen-call-budget-probe-b0-a0",
        "frozen-call-budget-probe-b0-strict-a0",
        "frozen-call-budget-main-b0-a0",
        "frozen-call-budget-main-b0-s0-a0",
    ]


def test_retryable_batch_failure_is_bounded_then_adaptively_split() -> None:
    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"text {index}")
        for index in range(4)
    ]

    class PayloadSensitiveProvider:
        name = "payload-sensitive"

        def __init__(self) -> None:
            self.calls: list[tuple[list[str], str]] = []

        async def translate(self, batch, *, request_id, **_kwargs):
            block_ids = [block.block_id for block in batch]
            self.calls.append((block_ids, request_id))
            if len(batch) > 1:
                raise RetryableProviderError("provider returned HTTP 503")
            return [
                TranslationResult(
                    block_id=batch[0].block_id,
                    translated_text=f"translated {batch[0].block_id}",
                    provider=self.name,
                )
            ]

    provider = PayloadSensitiveProvider()
    results = asyncio.run(
        pipeline._run_batches(
            [blocks],
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="adaptive",
            concurrency=2,
            retries=1,
        )
    )

    assert [result.block_id for result in results] == ["b0", "b1", "b2", "b3"]
    assert provider.calls == [
        (["b0", "b1", "b2", "b3"], "adaptive-b0-a0"),
        (["b0", "b1", "b2", "b3"], "adaptive-b0-a1"),
        (["b0", "b1"], "adaptive-b0-s0-a0"),
        (["b2", "b3"], "adaptive-b0-s1-a0"),
        (["b0"], "adaptive-b0-s0-s0-a0"),
        (["b1"], "adaptive-b0-s0-s1-a0"),
        (["b2"], "adaptive-b0-s1-s0-a0"),
        (["b3"], "adaptive-b0-s1-s1-a0"),
    ]


def test_retryable_split_depth_caps_worst_case_request_amplification() -> None:
    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"text {index}")
        for index in range(20)
    ]

    class AlwaysUnavailableProvider:
        name = "always-unavailable"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            raise RetryableProviderError("provider returned HTTP 503")

    provider = AlwaysUnavailableProvider()
    with pytest.raises(RetryableProviderError, match="HTTP 503"):
        asyncio.run(
            pipeline._run_batches(
                [blocks],
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="bounded-amplification",
                concurrency=4,
                retries=1,
            )
        )

    # Two normal attempts plus a binary split capped at two recovery levels.
    assert len(provider.request_ids) <= 8


def test_rate_limit_exhaustion_does_not_amplify_requests_by_splitting() -> None:
    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text=f"text {index}")
        for index in range(4)
    ]

    class RateLimitedProvider:
        name = "rate-limited"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            raise RetryableProviderError("provider returned HTTP 429")

    provider = RateLimitedProvider()
    with pytest.raises(RetryableProviderError, match="HTTP 429"):
        asyncio.run(
            pipeline._run_batches(
                [blocks],
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="rate-limit",
                concurrency=2,
                retries=1,
            )
        )

    assert provider.request_ids == ["rate-limit-b0-a0", "rate-limit-b0-a1"]


def test_protocol_failure_retries_only_failed_batch_and_isolated_bad_block() -> None:
    failed_batch = [
        TranslationBlock(block_id="good", source_text="good source"),
        TranslationBlock(block_id="bad", source_text="bad source"),
    ]
    stable_batch = [TranslationBlock(block_id="stable", source_text="stable source")]

    class OneBadBlockProvider:
        name = "one-bad-block"

        def __init__(self) -> None:
            self.calls: list[tuple[list[str], str]] = []
            self.bad_singleton_calls = 0

        async def translate(self, batch, *, request_id, **_kwargs):
            block_ids = [block.block_id for block in batch]
            self.calls.append((block_ids, request_id))
            if block_ids == ["good", "bad"]:
                return [
                    TranslationResult(
                        block_id="good",
                        translated_text="translated good",
                        provider=self.name,
                    )
                ]
            if block_ids == ["bad"]:
                self.bad_singleton_calls += 1
                if self.bad_singleton_calls == 1:
                    raise ProviderProtocolError(
                        "segment mismatch for 'bad'; expected ['t0'], got ['t9']"
                    )
            return [
                TranslationResult(
                    block_id=block.block_id,
                    translated_text=f"translated {block.block_id}",
                    provider=self.name,
                )
                for block in batch
            ]

    provider = OneBadBlockProvider()
    results = asyncio.run(
        pipeline._run_batches(
            [failed_batch, stable_batch],
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="protocol",
            concurrency=1,
            retries=3,
        )
    )

    assert [result.block_id for result in results] == ["good", "bad", "stable"]
    assert sum(ids == ["stable"] for ids, _request_id in provider.calls) == 1
    assert sum(ids == ["good"] for ids, _request_id in provider.calls) == 1
    assert sum(ids == ["bad"] for ids, _request_id in provider.calls) == 2
    bad_request_ids = [
        request_id for ids, request_id in provider.calls if ids == ["bad"]
    ]
    assert bad_request_ids == [
        "protocol-b0-s1-a0",
        "protocol-b0-s1-strict-a0",
    ]


def test_protocol_recovery_has_a_hard_extra_call_budget() -> None:
    batches = [
        [
            TranslationBlock(
                block_id=f"batch{batch_index}-b{index}",
                source_text=f"text {batch_index}-{index}",
            )
            for index in range(20)
        ]
        for batch_index in range(4)
    ]

    class AlwaysMalformedProvider:
        name = "always-malformed"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            raise ProviderProtocolError("translation JSON results must be an array")

    provider = AlwaysMalformedProvider()
    with pytest.raises(ProviderProtocolError, match="extra calls"):
        asyncio.run(
            pipeline._run_batches(
                batches,
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="protocol-budget",
                concurrency=4,
                retries=3,
            )
        )

    assert (
        len(provider.request_ids)
        <= len(batches) + pipeline.DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS
    )


def test_terminal_recovery_failure_stops_unstarted_initial_batches() -> None:
    batches = [
        [TranslationBlock(block_id=f"b{index}", source_text="source")]
        for index in range(100)
    ]

    class AlwaysMalformedProvider:
        name = "always-malformed"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            raise ProviderProtocolError("malformed response")

    provider = AlwaysMalformedProvider()
    with pytest.raises(ProviderProtocolError, match="0 extra calls"):
        asyncio.run(
            pipeline._run_batches(
                batches,
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="lazy-initial",
                concurrency=1,
                retries=0,
                recovery_budget=pipeline._ProviderRecoveryBudget(max_calls=0),
            )
        )

    assert provider.request_ids == ["lazy-initial-b0-a0"]


def test_terminal_recovery_failure_cancels_and_awaits_inflight_siblings() -> None:
    batches = [
        [TranslationBlock(block_id=f"b{index}", source_text="source")]
        for index in range(10)
    ]

    class OneMalformedOneBlockingProvider:
        name = "malformed-and-blocking"

        def __init__(self) -> None:
            self.request_ids: list[str] = []
            self.second_started = asyncio.Event()
            self.second_cancelled = asyncio.Event()

        async def translate(self, blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            block_id = blocks[0].block_id
            if block_id == "b0":
                await self.second_started.wait()
                raise ProviderProtocolError("malformed response")
            if block_id == "b1":
                self.second_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.second_cancelled.set()
                    raise
            raise AssertionError("terminal failure must stop unstarted batches")

    async def exercise_failure() -> tuple[list[str], bool, int]:
        provider = OneMalformedOneBlockingProvider()
        tasks_before = asyncio.all_tasks()
        with pytest.raises(ProviderProtocolError, match="0 extra calls"):
            await pipeline._run_batches(
                batches,
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="cancel-sibling",
                concurrency=2,
                retries=0,
                recovery_budget=pipeline._ProviderRecoveryBudget(max_calls=0),
            )
        await asyncio.sleep(0)
        leaked_tasks = [
            task for task in asyncio.all_tasks() - tasks_before if not task.done()
        ]
        return (
            provider.request_ids,
            provider.second_cancelled.is_set(),
            len(leaked_tasks),
        )

    request_ids, sibling_cancelled, leaked_task_count = asyncio.run(exercise_failure())
    assert request_ids == ["cancel-sibling-b0-a0", "cancel-sibling-b1-a0"]
    assert sibling_cancelled is True
    assert leaked_task_count == 0


def test_candidate_budget_caps_probe_main_and_tagged_strict_recovery() -> None:
    source_blocks = [
        TranslationBlock(
            block_id=f"tagged-{index}",
            source_text="source word",
            protocol="tagged",
            segments=[TextSegment(segment_id="t0", text="source word")],
        )
        for index in range(40)
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )

    class CrossPhaseProvider:
        name = "cross-phase"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            if "-strict-b" not in request_id and request_id.endswith("-a0"):
                raise RetryableProviderError("provider returned HTTP 503")
            return [
                TranslationResult(
                    block_id=block.block_id,
                    translated_text=f"translation {block.block_id}",
                    segments=[
                        TextSegment(
                            segment_id="t0",
                            text=(
                                f"translation {block.block_id}"
                                if int(block.block_id.rsplit("-", 1)[1]) >= 8
                                or "-strict-b" in request_id
                                else "mismatch"
                            ),
                        )
                    ],
                    provider=self.name,
                )
                for block in blocks
            ]

    provider = CrossPhaseProvider()

    async def exhaust_across_phases() -> None:
        recovery_budget = pipeline._ProviderRecoveryBudget()
        raw_results = await _translate_blocks(
            prepared,
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="cross-phase",
            concurrency=1,
            retries=1,
            recovery_budget=recovery_budget,
        )
        await pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="cross-phase",
            allow_fixture_unprotected=False,
            concurrency=1,
            recovery_budget=recovery_budget,
        )

    with pytest.raises(ProviderProtocolError, match="16 extra calls"):
        asyncio.run(exhaust_across_phases())

    initial_ids = [
        request_id
        for request_id in provider.request_ids
        if request_id.endswith("-a0") and "-strict-b" not in request_id
    ]
    recovery_ids = [
        request_id
        for request_id in provider.request_ids
        if request_id not in initial_ids
    ]
    assert len(initial_ids) == 10
    assert len(recovery_ids) == pipeline.DEFAULT_EXTRA_PROVIDER_RECOVERY_CALLS
    assert len(provider.request_ids) == len(set(provider.request_ids))
    assert recovery_ids == [
        "cross-phase-probe-b0-a1",
        *(f"cross-phase-main-b{index}-a1" for index in range(9)),
        *(f"cross-phase-strict-b{index}-a0" for index in range(6)),
    ]


def test_initial_probe_and_main_batch_calls_do_not_consume_recovery_budget() -> None:
    blocks = [
        TranslationBlock(block_id=f"b{index}", source_text="source")
        for index in range(25)
    ]

    class SuccessfulProvider:
        name = "successful"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, batch, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            return [
                TranslationResult(
                    block_id=block.block_id,
                    translated_text="translation",
                    provider=self.name,
                )
                for block in batch
            ]

    provider = SuccessfulProvider()
    results = asyncio.run(
        _translate_blocks(
            blocks,
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="initial-only",
            concurrency=1,
            retries=3,
            recovery_budget=pipeline._ProviderRecoveryBudget(max_calls=0),
        )
    )

    assert len(results) == len(blocks)
    assert provider.request_ids == [
        "initial-only-probe-b0-a0",
        "initial-only-main-b0-a0",
        "initial-only-main-b1-a0",
    ]


def test_batch_recovery_propagates_cancellation_without_starting_more_calls() -> None:
    block = TranslationBlock(block_id="b0", source_text="text")

    class BlockingProvider:
        name = "blocking"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.calls = 0

        async def translate(self, _blocks, **_kwargs):
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def cancel_during_provider_call() -> int:
        provider = BlockingProvider()
        task = asyncio.create_task(
            pipeline._run_batches(
                [[block]],
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="cancelled",
                concurrency=1,
                retries=3,
            )
        )
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return provider.calls

    assert asyncio.run(cancel_during_provider_call()) == 1


def test_tagged_strict_recovery_cancellation_stops_queued_requests() -> None:
    source_blocks = [
        TranslationBlock(
            block_id=f"tagged-{index}",
            source_text="source word",
            protocol="tagged",
            segments=[TextSegment(segment_id="t0", text="source word")],
        )
        for index in range(2)
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text="translation",
            segments=[TextSegment(segment_id="t0", text="mismatch")],
            provider="broken",
        )
        for block in source_blocks
    ]

    class BlockingStrictProvider:
        name = "blocking-strict"

        def __init__(self) -> None:
            self.request_ids: list[str] = []
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            raise AssertionError("unreachable")

    async def cancel_strict_recovery() -> tuple[list[str], bool]:
        provider = BlockingStrictProvider()
        task = asyncio.create_task(
            pipeline._validate_and_restore_with_strict_retry(
                source_blocks,
                prepared,
                raw_results,
                protected_by_id,
                {},
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="cancel-strict",
                allow_fixture_unprotected=False,
                concurrency=1,
                recovery_budget=pipeline._ProviderRecoveryBudget(),
            )
        )
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return provider.request_ids, provider.cancelled.is_set()

    request_ids, provider_was_cancelled = asyncio.run(cancel_strict_recovery())
    assert request_ids == ["cancel-strict-strict-b0-a0"]
    assert provider_was_cancelled is True


def test_adaptive_recovery_obeys_a_wall_clock_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "DEFAULT_PROVIDER_RECOVERY_SECONDS", 0.01)
    blocks = [
        TranslationBlock(block_id="b0", source_text="text zero"),
        TranslationBlock(block_id="b1", source_text="text one"),
    ]

    class SlowRecoveryProvider:
        name = "slow-recovery"

        def __init__(self) -> None:
            self.calls = 0

        async def translate(self, _blocks, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RetryableProviderError("provider returned HTTP 503")
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    provider = SlowRecoveryProvider()
    with pytest.raises(RetryableProviderError, match="recovery exceeded 0.01"):
        asyncio.run(
            pipeline._run_batches(
                [blocks],
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="recovery-deadline",
                concurrency=2,
                retries=0,
            )
        )


def test_recovery_deadline_includes_retry_backoff_before_the_next_request() -> None:
    block = TranslationBlock(block_id="b0", source_text="text")

    class RecoveringProvider:
        name = "recovering"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, batch, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            if request_id.endswith("-a0"):
                raise RetryableProviderError("provider returned HTTP 429")
            return [
                TranslationResult(
                    block_id=batch[0].block_id,
                    translated_text="translation",
                    provider=self.name,
                )
            ]

    provider = RecoveringProvider()

    async def expire_during_backoff() -> float:
        started = asyncio.get_running_loop().time()
        with pytest.raises(
            RetryableProviderError,
            match="recovery exceeded 0.01 seconds",
        ):
            await pipeline._run_batches(
                [[block]],
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="backoff-deadline",
                concurrency=1,
                retries=1,
                recovery_budget=pipeline._ProviderRecoveryBudget(max_seconds=0.01),
            )
        return asyncio.get_running_loop().time() - started

    elapsed = asyncio.run(expire_during_backoff())
    assert provider.request_ids == ["backoff-deadline-b0-a0"]
    assert elapsed < 0.5


def test_tagged_text_mismatch_strictly_retries_only_the_bad_block() -> None:
    source_blocks = [
        TranslationBlock(block_id="plain", source_text="Plain source"),
        TranslationBlock(
            block_id="tagged",
            source_text="Hello world",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Hello "),
                TextSegment(segment_id="t1", text="world"),
            ],
        ),
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="plain",
            translated_text="普通译文",
            provider="recording",
        ),
        TranslationResult(
            block_id="tagged",
            translated_text="你好世界",
            segments=[
                TextSegment(segment_id="t0", text="你好"),
                TextSegment(segment_id="t1", text="错误"),
            ],
            provider="recording",
        ),
    ]

    class CorrectingProvider:
        name = "correcting"

        def __init__(self) -> None:
            self.calls: list[tuple[list[TranslationBlock], str]] = []

        async def translate(self, blocks, *, request_id, **_kwargs):
            self.calls.append((list(blocks), request_id))
            return [
                TranslationResult(
                    block_id="tagged",
                    translated_text="你好世界",
                    segments=[
                        TextSegment(segment_id="t0", text="你好"),
                        TextSegment(segment_id="t1", text="世界"),
                    ],
                    provider=self.name,
                )
            ]

    provider = CorrectingProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="tagged-recovery",
            allow_fixture_unprotected=False,
        )
    )

    assert [result.block_id for result in restored] == ["plain", "tagged"]
    assert issues == []
    assert len(provider.calls) == 1
    retried_blocks, request_id = provider.calls[0]
    assert [block.block_id for block in retried_blocks] == ["tagged"]
    assert request_id == "tagged-recovery-strict-b1-a0"
    assert retried_blocks[0].context["translation_retry"] == {
        "mode": "strict_single_block",
        "reason": "TAGGED_SEGMENT_TEXT_MISMATCH",
    }
    assert restored[0].translated_text == "普通译文"
    assert restored[1].metadata["pipeline_recovery"] == {
        "mode": "strict_single_block",
        "reason": "TAGGED_SEGMENT_TEXT_MISMATCH",
        "request_id": "tagged-recovery-strict-b1-a0",
    }


def test_tagged_strict_retry_uses_a_second_bounded_attempt_after_extra_segment() -> (
    None
):
    source_blocks = [
        TranslationBlock(
            block_id="tagged",
            source_text="Hello world",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Hello "),
                TextSegment(segment_id="t1", text="world"),
            ],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="tagged",
            translated_text="translated correct",
            segments=[
                TextSegment(segment_id="t0", text="translated "),
                TextSegment(segment_id="t1", text="wrong"),
            ],
            provider="broken",
        )
    ]

    class CorrectingOnSecondStrictAttemptProvider:
        name = "correcting-on-second-strict-attempt"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            segments = [
                TextSegment(segment_id="t0", text="translated "),
                TextSegment(segment_id="t1", text="correct"),
            ]
            if request_id.endswith("-a0"):
                segments.append(TextSegment(segment_id="t2", text="EXTRA"))
            return [
                TranslationResult(
                    block_id="tagged",
                    translated_text="translated correct",
                    segments=segments,
                    provider=self.name,
                )
            ]

    provider = CorrectingOnSecondStrictAttemptProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="tagged-second-strict",
            allow_fixture_unprotected=False,
        )
    )

    assert issues == []
    assert [result.block_id for result in restored] == ["tagged"]
    assert provider.request_ids == [
        "tagged-second-strict-strict-b0-a0",
        "tagged-second-strict-strict-b0-a1",
    ]
    assert restored[0].metadata["pipeline_recovery"]["request_id"] == (
        "tagged-second-strict-strict-b0-a1"
    )


def test_tagged_strict_retry_retries_complete_text_mismatch_then_succeeds() -> None:
    source_blocks = [
        TranslationBlock(
            block_id="tagged",
            source_text="Hello world",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Hello "),
                TextSegment(segment_id="t1", text="world"),
            ],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="tagged",
            translated_text="translated correct",
            segments=[
                TextSegment(segment_id="t0", text="translated "),
                TextSegment(segment_id="t1", text="wrong"),
            ],
            provider="broken",
        )
    ]

    class CorrectingCompleteTextOnSecondAttemptProvider:
        name = "correcting-complete-text-on-second-attempt"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            final_word = "wrong" if request_id.endswith("-a0") else "correct"
            return [
                TranslationResult(
                    block_id="tagged",
                    translated_text="translated correct",
                    segments=[
                        TextSegment(segment_id="t0", text="translated "),
                        TextSegment(segment_id="t1", text=final_word),
                    ],
                    provider=self.name,
                )
            ]

    provider = CorrectingCompleteTextOnSecondAttemptProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="tagged-complete-text",
            allow_fixture_unprotected=False,
        )
    )

    assert issues == []
    assert [result.block_id for result in restored] == ["tagged"]
    assert provider.request_ids == [
        "tagged-complete-text-strict-b0-a0",
        "tagged-complete-text-strict-b0-a1",
    ]
    assert restored[0].metadata["pipeline_recovery"]["request_id"] == (
        "tagged-complete-text-strict-b0-a1"
    )


def test_tagged_strict_retry_accepts_whitespace_only_segment_alignment() -> None:
    source_blocks = [
        TranslationBlock(
            block_id="tagged",
            source_text="Hello world",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Hello "),
                TextSegment(segment_id="t1", text="world"),
            ],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="tagged",
            translated_text="translated correct",
            segments=[
                TextSegment(segment_id="t0", text="translated "),
                TextSegment(segment_id="t1", text="wrong"),
            ],
            provider="broken",
        )
    ]

    class WhitespaceOnlyMismatchProvider:
        name = "whitespace-only-mismatch"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            return [
                TranslationResult(
                    block_id="tagged",
                    translated_text="translated correct",
                    segments=[
                        TextSegment(segment_id="t0", text="translated"),
                        TextSegment(segment_id="t1", text="correct"),
                    ],
                    provider=self.name,
                )
            ]

    provider = WhitespaceOnlyMismatchProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="tagged-whitespace-only",
            allow_fixture_unprotected=False,
        )
    )

    assert issues == []
    assert provider.request_ids == ["tagged-whitespace-only-strict-b0-a0"]
    assert "Tagged segment whitespace was aligned" in restored[0].warnings[0]
    assert "".join(segment.text for segment in restored[0].segments) == (
        restored[0].translated_text
    )


def test_tagged_strict_retry_fails_closed_after_repeated_complete_text_mismatch() -> (
    None
):
    source_blocks = [
        TranslationBlock(
            block_id="tagged",
            source_text="Hello world",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Hello "),
                TextSegment(segment_id="t1", text="world"),
            ],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="tagged",
            translated_text="translated correct",
            segments=[
                TextSegment(segment_id="t0", text="translated "),
                TextSegment(segment_id="t1", text="wrong"),
            ],
            provider="broken",
        )
    ]

    class AlwaysMismatchedCompleteTextProvider:
        name = "always-mismatched-complete-text"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            return [
                TranslationResult(
                    block_id="tagged",
                    translated_text="translated correct",
                    segments=[
                        TextSegment(segment_id="t0", text="translated "),
                        TextSegment(segment_id="t1", text="wrong"),
                    ],
                    provider=self.name,
                )
            ]

    provider = AlwaysMismatchedCompleteTextProvider()
    with pytest.raises(
        ProviderProtocolError,
        match="strict retry tagged text mismatch",
    ):
        asyncio.run(
            pipeline._validate_and_restore_with_strict_retry(
                source_blocks,
                prepared,
                raw_results,
                protected_by_id,
                {},
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="tagged-complete-text-still-invalid",
                allow_fixture_unprotected=False,
            )
        )

    assert provider.request_ids == [
        "tagged-complete-text-still-invalid-strict-b0-a0",
        "tagged-complete-text-still-invalid-strict-b0-a1",
    ]


def test_tagged_strict_retry_retries_provider_protocol_error_once() -> None:
    source_blocks = [
        TranslationBlock(
            block_id="tagged",
            source_text="Hello world",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Hello "),
                TextSegment(segment_id="t1", text="world"),
            ],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="tagged",
            translated_text="translated correct",
            segments=[
                TextSegment(segment_id="t0", text="translated "),
                TextSegment(segment_id="t1", text="wrong"),
            ],
            provider="broken",
        )
    ]

    class ProtocolErrorThenCorrectProvider:
        name = "protocol-error-then-correct"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            if request_id.endswith("-a0"):
                raise ProviderProtocolError("provider returned extra tagged segment t2")
            return [
                TranslationResult(
                    block_id="tagged",
                    translated_text="translated correct",
                    segments=[
                        TextSegment(segment_id="t0", text="translated "),
                        TextSegment(segment_id="t1", text="correct"),
                    ],
                    provider=self.name,
                )
            ]

    provider = ProtocolErrorThenCorrectProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="tagged-provider-protocol-error",
            allow_fixture_unprotected=False,
        )
    )

    assert issues == []
    assert [result.block_id for result in restored] == ["tagged"]
    assert provider.request_ids == [
        "tagged-provider-protocol-error-strict-b0-a0",
        "tagged-provider-protocol-error-strict-b0-a1",
    ]


def test_tagged_strict_retry_fails_closed_after_two_extra_segment_responses() -> None:
    source_blocks = [
        TranslationBlock(
            block_id="tagged",
            source_text="Hello world",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Hello "),
                TextSegment(segment_id="t1", text="world"),
            ],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="tagged",
            translated_text="translated correct",
            segments=[
                TextSegment(segment_id="t0", text="translated "),
                TextSegment(segment_id="t1", text="wrong"),
            ],
            provider="broken",
        )
    ]

    class AlwaysExtraSegmentProvider:
        name = "always-extra-segment"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, _blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            raise ProviderProtocolError(
                "strict retry segment mismatch for 'tagged'; "
                "expected ['t0', 't1'], got ['t0', 't1', 't2']"
            )

    provider = AlwaysExtraSegmentProvider()
    with pytest.raises(
        ProviderProtocolError,
        match=r"expected \['t0', 't1'\], got \['t0', 't1', 't2'\]",
    ):
        asyncio.run(
            pipeline._validate_and_restore_with_strict_retry(
                source_blocks,
                prepared,
                raw_results,
                protected_by_id,
                {},
                provider=provider,
                source_language="en",
                target_language="zh",
                job_id="tagged-still-invalid",
                allow_fixture_unprotected=False,
            )
        )

    assert provider.request_ids == [
        "tagged-still-invalid-strict-b0-a0",
        "tagged-still-invalid-strict-b0-a1",
    ]


def test_tagged_strict_retry_fails_closed_when_protected_token_is_lost() -> None:
    source_blocks = [
        TranslationBlock(
            block_id="protected-tagged",
            source_text="Deadline 2026-07-29",
            protocol="tagged",
            segments=[
                TextSegment(segment_id="t0", text="Deadline "),
                TextSegment(segment_id="t1", text="2026-07-29"),
            ],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    placeholder = protected_by_id["protected-tagged"].tokens[0].placeholder
    raw_results = [
        TranslationResult(
            block_id="protected-tagged",
            translated_text=f"截止{placeholder}",
            segments=[
                TextSegment(segment_id="t0", text="错误"),
                TextSegment(segment_id="t1", text=placeholder),
            ],
            provider="broken",
        )
    ]

    class TokenLosingProvider:
        name = "token-losing"
        calls = 0

        async def translate(self, _blocks, **_kwargs):
            self.calls += 1
            return [
                TranslationResult(
                    block_id="protected-tagged",
                    translated_text="截止日期",
                    segments=[
                        TextSegment(segment_id="t0", text="截止"),
                        TextSegment(segment_id="t1", text="日期"),
                    ],
                    provider=self.name,
                )
            ]

    provider = TokenLosingProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="protected-recovery",
            allow_fixture_unprotected=False,
        )
    )

    assert provider.calls == 1
    assert restored == []
    assert [issue.code for issue in issues] == [
        "PROTECTED_TOKEN_MISMATCH",
        "TRANSLATION_SET_INCOMPLETE",
    ]


def test_tagged_strict_retry_count_is_capped_and_excess_fails_closed() -> None:
    source_blocks = [
        TranslationBlock(
            block_id=f"tagged-{index}",
            source_text="source word",
            protocol="tagged",
            segments=[TextSegment(segment_id="t0", text="source word")],
        )
        for index in range(pipeline.MAX_STRICT_TAGGED_RETRY_BLOCKS + 1)
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text="translation",
            segments=[TextSegment(segment_id="t0", text="mismatch")],
            provider="broken",
        )
        for block in prepared
    ]

    class MustNotRunProvider:
        name = "must-not-run"
        calls = 0

        async def translate(self, _blocks, **_kwargs):
            self.calls += 1
            raise AssertionError(
                "strict recovery must stop before request amplification"
            )

    provider = MustNotRunProvider()
    restored, issues = asyncio.run(
        pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="strict-cap",
            allow_fixture_unprotected=False,
        )
    )

    assert provider.calls == 0
    assert restored == []
    assert sum(issue.code == "TAGGED_SEGMENT_TEXT_MISMATCH" for issue in issues) == len(
        source_blocks
    )
    assert issues[-1].code == "TRANSLATION_SET_INCOMPLETE"


def test_candidate_recovery_deadline_carries_from_batch_into_tagged_strict() -> None:
    source_blocks = [
        TranslationBlock(
            block_id="slow-tagged",
            source_text="source word",
            protocol="tagged",
            segments=[TextSegment(segment_id="t0", text="source word")],
        )
    ]
    prepared, protected_by_id = pipeline._prepare_blocks(
        source_blocks,
        source_language="en",
        target_language="zh",
        glossary={},
    )
    raw_results = [
        TranslationResult(
            block_id="slow-tagged",
            translated_text="translation",
            segments=[TextSegment(segment_id="t0", text="mismatch")],
            provider="broken",
        )
    ]

    class DeadlineProvider:
        name = "deadline"

        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def translate(self, blocks, *, request_id, **_kwargs):
            self.request_ids.append(request_id)
            if request_id == "candidate-deadline-b0-a0":
                raise ProviderProtocolError("malformed response")
            if request_id == "candidate-deadline-b0-strict-a0":
                return [
                    TranslationResult(
                        block_id=blocks[0].block_id,
                        translated_text="translation",
                        provider=self.name,
                    )
                ]
            raise AssertionError("expired strict recovery must not reach the provider")

    provider = DeadlineProvider()

    async def expire_between_phases() -> None:
        recovery_budget = pipeline._ProviderRecoveryBudget(max_seconds=0.01)
        await pipeline._run_batches(
            [[TranslationBlock(block_id="seed", source_text="seed")]],
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="candidate-deadline",
            concurrency=1,
            retries=0,
            recovery_budget=recovery_budget,
        )
        await asyncio.sleep(0.02)
        await pipeline._validate_and_restore_with_strict_retry(
            source_blocks,
            prepared,
            raw_results,
            protected_by_id,
            {},
            provider=provider,
            source_language="en",
            target_language="zh",
            job_id="candidate-deadline",
            allow_fixture_unprotected=False,
            recovery_budget=recovery_budget,
        )

    with pytest.raises(RetryableProviderError, match="recovery exceeded 0.01 seconds"):
        asyncio.run(expire_between_phases())

    assert provider.request_ids == [
        "candidate-deadline-b0-a0",
        "candidate-deadline-b0-strict-a0",
    ]


def test_protected_value_is_projected_across_tagged_segments() -> None:
    segments = [
        TextSegment(segment_id="s1", text="Date 20"),
        TextSegment(segment_id="s2", text="26-07"),
        TextSegment(segment_id="s3", text="-29 end"),
    ]
    token = ProtectedToken(
        placeholder="__FT_PROTECTED_0000__",
        value="2026-07-29",
        kind="date",
        start=5,
        end=15,
    )

    protected = _protect_segments(segments, [token])

    assert [segment.text for segment in protected] == [
        "Date __FT_PROTECTED_0000__",
        "",
        " end",
    ]
    assert "".join(segment.text for segment in protected) == (
        "Date __FT_PROTECTED_0000__ end"
    )


def test_tagged_result_restores_whitespace_from_complete_translation() -> None:
    block = TranslationBlock(
        block_id="b1",
        source_text="Email: hering@pakislaw.com",
        protocol="tagged",
        source_language="en",
        target_language="zh",
        segments=[
            TextSegment(segment_id="t0", text="Email: "),
            TextSegment(segment_id="t1", text="hering@pakislaw.com"),
        ],
    )
    result = TranslationResult(
        block_id="b1",
        translated_text="电子邮件： hering@pakislaw.com",
        segments=[
            TextSegment(segment_id="t0", text="电子邮件："),
            TextSegment(segment_id="t1", text="hering@pakislaw.com"),
        ],
        provider="openai_chat_completions",
    )

    restored, issues = _validate_and_restore_results(
        [block],
        [result],
        {
            "b1": ProtectedText(
                original_text=block.source_text,
                protected_text=block.source_text,
            )
        },
        {},
        allow_fixture_unprotected=False,
    )

    assert issues == []
    assert "".join(segment.text for segment in restored[0].segments) == (
        restored[0].translated_text
    )
    assert restored[0].segments[1].text == " hering@pakislaw.com"


def test_text_fit_targets_rendered_overflow_even_when_chinese_is_shorter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocks = [
        TranslationBlock(
            block_id="target",
            source_text="A considerably longer English source sentence",
            context={
                "document_type": "pptx",
                "slide_number": 15,
                "shape_id": "6",
                "shape_type": "shape",
                "paragraph_index": 1,
            },
        ),
        TranslationBlock(
            block_id="other-shape",
            source_text="Another long source sentence",
            context={
                "document_type": "pptx",
                "slide_number": 15,
                "shape_id": "9",
                "shape_type": "shape",
                "paragraph_index": 1,
            },
        ),
        TranslationBlock(
            block_id="chart",
            source_text="Chart title",
            context={
                "document_type": "pptx",
                "content_scope": "chart",
                "slide_number": 15,
                "shape_id": "6",
                "shape_type": "chart",
            },
        ),
        TranslationBlock(
            block_id="table",
            source_text="Table cell",
            context={
                "document_type": "pptx",
                "slide_number": 15,
                "shape_id": "6",
                "shape_type": "graphic_frame",
            },
        ),
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text={
                "target": "中文译文仍溢出",
                "other-shape": "其他译文",
                "chart": "图表标题",
                "table": "表格单元格",
            }[block.block_id],
            provider="fixture",
        )
        for block in blocks
    ]
    protected = {
        block.block_id: ProtectedText(
            original_text=block.source_text,
            protected_text=block.source_text,
        )
        for block in blocks
    }

    class RecordingProvider:
        calls: list[tuple[str, int]] = []

        async def fit_text(self, *, source, translation, max_length, **_kwargs):
            self.calls.append((source.block_id, max_length))
            return TextFitResult(
                block_id=source.block_id,
                fitted_text=translation.translated_text[:max_length],
                provider="recording",
            )

    class RecordingEngine:
        def patch(self, _source, output, translations, **_kwargs):
            Path(output).write_bytes(b"trial")
            return {"translations": translations}

    initial_issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="overflow",
        severity=IssueSeverity.ERROR,
        slide=15,
        details={
            "shape_id": "6",
            "rendered_load_ratio": 1.084,
        },
    )
    monkeypatch.setattr(pipeline, "_run_office_qa", lambda *_args, **_kwargs: [])

    _document, fitted_results, remaining, _fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=blocks,
            results=results,
            protected_by_id=protected,
            processing_provider=RecordingProvider(),
            engine=RecordingEngine(),
            work=tmp_path,
            job_id="issue-fit",
            rounds=1,
            concurrency=2,
            layout_issues=[initial_issue],
        )
    )

    assert [block_id for block_id, _max_length in RecordingProvider.calls] == ["target"]
    assert RecordingProvider.calls[0][1] < len(results[0].translated_text)
    assert fitted_results[0].translated_text != results[0].translated_text
    assert [result.translated_text for result in fitted_results[1:]] == [
        result.translated_text for result in results[1:]
    ]
    assert remaining == []


def test_text_fit_overlap_uses_rendered_line_to_select_one_paragraph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    translations = {
        "intro": "欢迎与我们联系",
        "address": "联系：user@example.com",
        "hours": "办公时间为工作日",
    }
    blocks = [
        TranslationBlock(
            block_id=block_id,
            source_text=f"Long English source for {block_id}",
            context={
                "document_type": "pptx",
                "slide_number": 16,
                "shape_id": "3",
                "shape_type": "shape",
                "paragraph_index": paragraph_index,
            },
        )
        for paragraph_index, block_id in enumerate(translations, start=1)
    ]
    results = [
        TranslationResult(
            block_id=block.block_id,
            translated_text=translations[block.block_id],
            provider="fixture",
        )
        for block in blocks
    ]
    protected = {
        block.block_id: ProtectedText(
            original_text=block.source_text,
            protected_text=block.source_text,
        )
        for block in blocks
    }

    class RecordingProvider:
        calls: list[tuple[str, int]] = []

        async def fit_text(self, *, source, translation, max_length, **_kwargs):
            self.calls.append((source.block_id, max_length))
            return TextFitResult(
                block_id=source.block_id,
                fitted_text=translation.translated_text[:max_length],
                provider="recording",
            )

    class RecordingEngine:
        def patch(self, _source, output, _translations, **_kwargs):
            Path(output).write_bytes(b"trial")

    overlap_issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
        message="overlap",
        severity=IssueSeverity.ERROR,
        slide=16,
        details={
            "shape_id": "3",
            "candidate_overlap_area_pt2": 180.0,
            "overlaps": [
                {
                    "text": "联 系: user@example.com",
                    "intersection_width_pt": 30.0,
                    "line_bbox_pt": [0.0, 0.0, 100.0, 12.0],
                }
            ],
        },
    )
    monkeypatch.setattr(pipeline, "_run_office_qa", lambda *_args, **_kwargs: [])

    asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=blocks,
            results=results,
            protected_by_id=protected,
            processing_provider=RecordingProvider(),
            engine=RecordingEngine(),
            work=tmp_path,
            job_id="overlap-fit",
            rounds=1,
            concurrency=2,
            layout_issues=[overlap_issue],
        )
    )

    assert [block_id for block_id, _max_length in RecordingProvider.calls] == [
        "address"
    ]
    assert RecordingProvider.calls[0][1] <= math.floor(
        len(translations["address"]) * 0.70
    )


def test_text_fit_preserves_tagged_runs_and_protected_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    email = "support@example.com"
    block = TranslationBlock(
        block_id="tagged-address",
        source_text=f"Contact email address: {email}",
        protocol="tagged",
        segments=[
            TextSegment(
                segment_id="t0",
                text="Contact email address: ",
                style_id="label-style",
                metadata={"run_index": 0},
            ),
            TextSegment(
                segment_id="t1",
                text=email,
                style_id="address-style",
                metadata={"run_index": 1},
            ),
        ],
        context={
            "document_type": "pptx",
            "slide_number": 16,
            "shape_id": "3",
            "shape_type": "shape",
            "paragraph_index": 17,
        },
    )
    original_translation = f"电子邮箱地址：{email}"
    result = TranslationResult(
        block_id=block.block_id,
        translated_text=original_translation,
        segments=[
            TextSegment(
                segment_id="t0",
                text="电子邮箱地址：",
                style_id="label-style",
                metadata={"run_index": 0},
            ),
            TextSegment(
                segment_id="t1",
                text=email,
                style_id="address-style",
                metadata={"run_index": 1},
            ),
        ],
        provider="fixture",
    )
    token = ProtectedToken(
        placeholder="__FT_PROTECTED_0000__",
        value=email,
        kind="email",
        start=23,
        end=23 + len(email),
    )
    protected = ProtectedText(
        original_text=block.source_text,
        protected_text="Contact email address: __FT_PROTECTED_0000__",
        tokens=[token],
    )
    fitted_text = email
    requested_lengths: list[int] = []

    class FittingProvider:
        async def fit_text(self, *, max_length, **_kwargs):
            requested_lengths.append(max_length)
            return TextFitResult(
                block_id=block.block_id,
                fitted_text=fitted_text,
                provider="recording",
            )

    patched: list[TranslationResult] = []

    class StrictTaggedEngine:
        def patch(self, _source, output, translations, **_kwargs):
            fitted = next(
                translation
                for translation in translations
                if translation.block_id == block.block_id
            )
            assert [segment.segment_id for segment in fitted.segments] == [
                "t0",
                "t1",
            ]
            assert "".join(segment.text for segment in fitted.segments) == (
                fitted.translated_text
            )
            patched.append(fitted)
            Path(output).write_bytes(b"trial")

    issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
        message="overlap",
        severity=IssueSeverity.ERROR,
        slide=16,
        details={
            "shape_id": "3",
            "overlaps": [
                {
                    "text": original_translation,
                    "intersection_width_pt": 40.0,
                    "line_bbox_pt": [0.0, 0.0, 100.0, 12.0],
                }
            ],
        },
    )
    monkeypatch.setattr(pipeline, "_run_office_qa", lambda *_args, **_kwargs: [])

    _document, fitted_results, _issues, _fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=[block],
            results=[result],
            protected_by_id={block.block_id: protected},
            processing_provider=FittingProvider(),
            engine=StrictTaggedEngine(),
            work=tmp_path,
            job_id="tagged-fit",
            rounds=1,
            concurrency=1,
            layout_issues=[issue],
        )
    )

    assert len(patched) == 1
    assert len(email) <= requested_lengths[0] < len(original_translation)
    fitted = fitted_results[0]
    assert fitted.translated_text == fitted_text
    assert email in fitted.translated_text
    assert [segment.style_id for segment in fitted.segments] == [
        "label-style",
        "address-style",
    ]
    assert [segment.metadata for segment in fitted.segments] == [
        {"run_index": 0},
        {"run_index": 1},
    ]


def test_text_fit_accepts_lower_overflow_score_with_same_error_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    block = TranslationBlock(
        block_id="overflow",
        source_text="A longer English sentence for the constrained text box",
        context={
            "document_type": "pptx",
            "slide_number": 15,
            "shape_id": "6",
            "shape_type": "shape",
            "paragraph_index": 1,
        },
    )
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="精简前仍然溢出的中文文本",
        provider="fixture",
    )
    protected = ProtectedText(
        original_text=block.source_text,
        protected_text=block.source_text,
    )

    class FittingProvider:
        async def fit_text(self, *, translation, max_length, **_kwargs):
            return TextFitResult(
                block_id=translation.block_id,
                fitted_text=translation.translated_text[:max_length],
                provider="recording",
            )

    class RecordingEngine:
        def patch(self, _source, output, _translations, **_kwargs):
            Path(output).write_bytes(b"trial")

    initial_issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="overflow",
        severity=IssueSeverity.ERROR,
        slide=15,
        details={
            "shape_id": "6",
            "rendered_load_ratio": 1.10,
            "overflow_width_pt": 10.0,
            "overflow_height_pt": 2.0,
            "available_width_pt": 100.0,
            "available_height_pt": 20.0,
        },
    )
    improved_issue = initial_issue.model_copy(
        update={
            "details": {
                **initial_issue.details,
                "rendered_load_ratio": 1.03,
                "overflow_width_pt": 3.0,
                "overflow_height_pt": 0.5,
            }
        }
    )
    monkeypatch.setattr(
        pipeline,
        "_run_office_qa",
        lambda *_args, **_kwargs: [improved_issue],
    )

    document, fitted_results, issues, fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=[block],
            results=[result],
            protected_by_id={block.block_id: protected},
            processing_provider=FittingProvider(),
            engine=RecordingEngine(),
            work=tmp_path,
            job_id="score-fit",
            rounds=1,
            concurrency=1,
            layout_issues=[initial_issue],
        )
    )

    assert document == tmp_path / "translated-r1.pptx"
    assert fitted_results[0].translated_text != result.translated_text
    assert issues == [improved_issue]
    assert all(issue.code != "TEXT_FIT_NO_IMPROVEMENT" for issue in fit_issues)


def test_text_fit_rejects_trial_when_weighted_layout_score_worsens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    block = TranslationBlock(
        block_id="address",
        source_text="Contact us at the address shown below",
        context={
            "document_type": "pptx",
            "slide_number": 16,
            "shape_id": "3",
            "shape_type": "shape",
            "paragraph_index": 17,
        },
    )
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="联系地址：北京市朝阳区示例路",
        provider="fixture",
    )

    class FittingProvider:
        async def fit_text(self, *, translation, max_length, **_kwargs):
            return TextFitResult(
                block_id=translation.block_id,
                fitted_text=translation.translated_text[:max_length],
                provider="recording",
            )

    class RecordingEngine:
        def patch(self, _source, output, _translations, **_kwargs):
            Path(output).write_bytes(b"trial")

    initial_issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
        message="overlap",
        severity=IssueSeverity.ERROR,
        slide=16,
        details={
            "shape_id": "3",
            "candidate_overlap_area_pt2": 100.0,
            "reference_overlap_area_pt2": 0.0,
            "worsening_tolerance_pt2": 10.0,
            "overlaps": [{"text": result.translated_text}],
        },
    )
    worse_issue = initial_issue.model_copy(
        update={
            "details": {
                **initial_issue.details,
                "candidate_overlap_area_pt2": 120.0,
            }
        }
    )
    monkeypatch.setattr(
        pipeline,
        "_run_office_qa",
        lambda *_args, **_kwargs: [worse_issue],
    )

    document, fitted_results, issues, fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=[block],
            results=[result],
            protected_by_id={
                block.block_id: ProtectedText(
                    original_text=block.source_text,
                    protected_text=block.source_text,
                )
            },
            processing_provider=FittingProvider(),
            engine=RecordingEngine(),
            work=tmp_path,
            job_id="reject-score-fit",
            rounds=1,
            concurrency=1,
            layout_issues=[initial_issue],
        )
    )

    assert document == tmp_path / "translated-r0.pptx"
    assert fitted_results == [result]
    assert issues == [initial_issue]
    rejection = next(
        issue for issue in fit_issues if issue.code == "TEXT_FIT_NO_IMPROVEMENT"
    )
    assert rejection.details["after_score"] > rejection.details["before_score"]


def test_text_fit_rejects_fitted_text_that_loses_a_protected_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    block = TranslationBlock(
        block_id="protected",
        source_text="Outlook for 2026",
        context={
            "document_type": "pptx",
            "slide_number": 15,
            "shape_id": "9",
            "shape_type": "shape",
            "paragraph_index": 1,
        },
    )
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="2026年展望",
        provider="fixture",
    )
    token = ProtectedToken(
        placeholder="__FT_PROTECTED_0000__",
        value="2026",
        kind="year",
        start=12,
        end=16,
    )

    class LosingProvider:
        async def fit_text(self, **_kwargs):
            return TextFitResult(
                block_id=block.block_id,
                fitted_text="年度展望",
                provider="recording",
            )

    class EngineThatMustNotPatch:
        def patch(self, *_args, **_kwargs):
            raise AssertionError("a rejected fit must not be patched")

    issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="overflow",
        severity=IssueSeverity.ERROR,
        slide=15,
        details={"shape_id": "9", "rendered_load_ratio": 1.019},
    )
    monkeypatch.setattr(
        pipeline,
        "_run_office_qa",
        lambda *_args, **_kwargs: pytest.fail("QA should not run"),
    )

    document, fitted_results, issues, fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=[block],
            results=[result],
            protected_by_id={
                block.block_id: ProtectedText(
                    original_text=block.source_text,
                    protected_text="Outlook for __FT_PROTECTED_0000__",
                    tokens=[token],
                )
            },
            processing_provider=LosingProvider(),
            engine=EngineThatMustNotPatch(),
            work=tmp_path,
            job_id="protected-fit",
            rounds=1,
            concurrency=1,
            layout_issues=[issue],
        )
    )

    assert document == tmp_path / "translated-r0.pptx"
    assert fitted_results == [result]
    assert issues == [issue]
    assert [fit_issue.code for fit_issue in fit_issues] == ["TEXT_FIT_REJECTED"]
    assert "2026" in fit_issues[0].message


def test_text_fit_retries_one_length_overrun_with_a_tighter_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    block = TranslationBlock(
        block_id="retry-fit",
        source_text="A source sentence that is longer than its translation",
        context={
            "document_type": "pptx",
            "slide_number": 15,
            "shape_id": "6",
            "shape_type": "shape",
            "paragraph_index": 1,
        },
    )
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="首次拟合返回超长文本需要重试",
        provider="fixture",
    )
    calls: list[tuple[int, str]] = []

    class RetryProvider:
        async def fit_text(
            self,
            *,
            translation,
            max_length,
            request_id,
            **_kwargs,
        ):
            calls.append((max_length, request_id))
            if len(calls) == 1:
                raise ProviderProtocolError(
                    f"fitted text has {max_length + 4} characters; "
                    f"maximum is {max_length}"
                )
            fitted_text = translation.translated_text[:max_length]
            return TextFitResult(
                block_id=translation.block_id,
                fitted_text=fitted_text,
                provider="retry",
            )

    class RecordingEngine:
        def patch(self, _source, output, _translations, **_kwargs):
            Path(output).write_bytes(b"trial")

    issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="overflow",
        severity=IssueSeverity.ERROR,
        slide=15,
        details={"shape_id": "6", "rendered_load_ratio": 1.084},
    )
    monkeypatch.setattr(pipeline, "_run_office_qa", lambda *_args, **_kwargs: [])

    _document, fitted_results, _issues, fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=[block],
            results=[result],
            protected_by_id={
                block.block_id: ProtectedText(
                    original_text=block.source_text,
                    protected_text=block.source_text,
                )
            },
            processing_provider=RetryProvider(),
            engine=RecordingEngine(),
            work=tmp_path,
            job_id="bounded-retry",
            rounds=1,
            concurrency=1,
            layout_issues=[issue],
        )
    )

    assert len(calls) == 2
    assert calls[1][0] < calls[0][0]
    assert calls[1][1] != calls[0][1]
    assert fitted_results[0].translated_text == result.translated_text[: calls[1][0]]
    assert fit_issues == []


def test_text_fit_warns_and_keeps_translation_after_two_length_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    block = TranslationBlock(
        block_id="failed-retry",
        source_text="A source sentence that is longer than its translation",
        context={
            "document_type": "pptx",
            "slide_number": 15,
            "shape_id": "6",
            "shape_type": "shape",
            "paragraph_index": 2,
        },
    )
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="两次返回都超过限制",
        provider="fixture",
    )
    calls: list[tuple[int, str]] = []

    class AlwaysTooLongProvider:
        async def fit_text(self, *, max_length, request_id, **_kwargs):
            calls.append((max_length, request_id))
            raise ProviderProtocolError(
                f"fitted text has {max_length + 3} characters; maximum is {max_length}"
            )

    class EngineThatMustNotPatch:
        def patch(self, *_args, **_kwargs):
            raise AssertionError("failed fits must not be patched")

    issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="overflow",
        severity=IssueSeverity.ERROR,
        slide=15,
        details={"shape_id": "6", "rendered_load_ratio": 1.084},
    )
    monkeypatch.setattr(
        pipeline,
        "_run_office_qa",
        lambda *_args, **_kwargs: pytest.fail("QA should not run"),
    )

    document, fitted_results, issues, fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=[block],
            results=[result],
            protected_by_id={
                block.block_id: ProtectedText(
                    original_text=block.source_text,
                    protected_text=block.source_text,
                )
            },
            processing_provider=AlwaysTooLongProvider(),
            engine=EngineThatMustNotPatch(),
            work=tmp_path,
            job_id="failed-bounded-retry",
            rounds=1,
            concurrency=1,
            layout_issues=[issue],
        )
    )

    assert len(calls) == 2
    assert calls[1][0] < calls[0][0]
    assert calls[1][1] != calls[0][1]
    assert document == tmp_path / "translated-r0.pptx"
    assert fitted_results == [result]
    assert issues == [issue]
    assert [fit_issue.code for fit_issue in fit_issues] == ["TEXT_FIT_REJECTED"]
    assert f"maximum is {calls[1][0]}" in fit_issues[0].message


def test_text_fit_does_not_retry_an_unrelated_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    block = TranslationBlock(
        block_id="ordinary-failure",
        source_text="A source sentence that is longer than its translation",
        context={
            "document_type": "pptx",
            "slide_number": 15,
            "shape_id": "9",
            "shape_type": "shape",
            "paragraph_index": 1,
        },
    )
    result = TranslationResult(
        block_id=block.block_id,
        translated_text="普通异常不应重试",
        provider="fixture",
    )
    calls: list[str] = []

    class FailingProvider:
        async def fit_text(self, *, request_id, **_kwargs):
            calls.append(request_id)
            raise RuntimeError("temporary transport failure")

    class EngineThatMustNotPatch:
        def patch(self, *_args, **_kwargs):
            raise AssertionError("failed fits must not be patched")

    issue = QaIssue(
        code="PPTX_RENDERED_TEXT_OVERFLOW",
        message="overflow",
        severity=IssueSeverity.ERROR,
        slide=15,
        details={"shape_id": "9", "rendered_load_ratio": 1.019},
    )
    monkeypatch.setattr(
        pipeline,
        "_run_office_qa",
        lambda *_args, **_kwargs: pytest.fail("QA should not run"),
    )

    _document, fitted_results, _issues, fit_issues = asyncio.run(
        _attempt_text_fit(
            source_path=tmp_path / "source.pptx",
            source_blocks=[block],
            results=[result],
            protected_by_id={
                block.block_id: ProtectedText(
                    original_text=block.source_text,
                    protected_text=block.source_text,
                )
            },
            processing_provider=FailingProvider(),
            engine=EngineThatMustNotPatch(),
            work=tmp_path,
            job_id="ordinary-failure",
            rounds=1,
            concurrency=1,
            layout_issues=[issue],
        )
    )

    assert len(calls) == 1
    assert fitted_results == [result]
    assert [fit_issue.code for fit_issue in fit_issues] == ["TEXT_FIT_REJECTED"]
    assert fit_issues[0].message == "temporary transport failure"


def test_inspect_generated_docx(tmp_path: Path) -> None:
    source = tmp_path / "inspect.docx"
    _write_minimal_docx(source, "A sanitized document inspection fixture.")

    manifest = inspect_document(source)

    assert manifest.block_count == 1
    assert manifest.page_count is None


def test_end_to_end_passthrough_pipeline(tmp_path: Path) -> None:
    source = tmp_path / "passthrough.docx"
    _write_minimal_docx(source, "A sanitized end-to-end translation fixture.")
    spec = JobSpec(
        job_id="passthrough",
        input_path=source,
        output_dir=tmp_path,
        source_language="en",
        target_language="zh",
        semantic_qa=False,
    )
    result = translate_document(
        spec,
        FixtureProvider({}, missing="passthrough"),
    )
    assert result.status == JobStatus.COMPLETED
    artifact_types = {artifact.artifact_type for artifact in result.artifacts}
    assert ArtifactType.TRANSLATED_DOCUMENT in artifact_types
    assert ArtifactType.QA_REPORT in artifact_types
    assert ArtifactType.TIMING_REPORT in artifact_types
    assert all(artifact.path.is_file() for artifact in result.artifacts)


def test_inspect_and_translate_generated_pptx_without_pdf(tmp_path: Path) -> None:
    source = tmp_path / "economic-review.pptx"
    _write_minimal_pptx(source, "A sanitized economic review and outlook fixture.")
    inspected = inspect_document(source)

    assert inspected.source_format == "pptx"
    assert inspected.normalized_format == "pptx"
    assert inspected.page_count == 1

    result = translate_document(
        JobSpec(
            job_id="pptx-passthrough",
            input_path=source,
            output_dir=tmp_path,
            source_language="en",
            target_language="zh",
            semantic_qa=False,
        ),
        FixtureProvider({}, missing="passthrough"),
    )

    assert result.status == JobStatus.COMPLETED
    artifact_types = {artifact.artifact_type for artifact in result.artifacts}
    assert ArtifactType.TRANSLATED_DOCUMENT in artifact_types
    assert ArtifactType.QA_REPORT in artifact_types
    assert ArtifactType.TIMING_REPORT in artifact_types
    assert all(artifact.path.is_file() for artifact in result.artifacts)
