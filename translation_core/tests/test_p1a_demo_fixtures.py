from __future__ import annotations

import importlib.util
import hashlib
import json
import zipfile
from pathlib import Path
from types import ModuleType

from format_translator.docx_engine import (
    extract_translation_blocks as extract_docx_blocks,
)
from format_translator.docx_engine import validate_output_package
from format_translator.pptx_engine import (
    extract_translation_blocks as extract_pptx_blocks,
)
from format_translator.pptx_engine import run_pptx_qa, validate_pptx
from format_translator.word_qa import run_docx_qa


def _load_fixture_generator() -> ModuleType:
    script = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "p1a_demo_fixtures.py"
    )
    spec = importlib.util.spec_from_file_location(
        "p1a_demo_fixtures_test_module", script
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURES = _load_fixture_generator()


def test_direct_docx_fixture_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    FIXTURES.generate_demo_fixtures(first)
    FIXTURES.generate_demo_fixtures(second)

    first_files = {
        path.relative_to(first).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files

    for office_file in sorted((first / "fixtures").rglob("*.docx")) + sorted(
        (first / "fixtures").rglob("*.pptx")
    ):
        with zipfile.ZipFile(office_file) as package:
            assert package.namelist() == sorted(package.namelist())
            assert {member.date_time for member in package.infolist()} == {
                (1980, 1, 1, 0, 0, 0)
            }


def test_manifest_declares_three_offline_scenarios_and_candidate_hashes(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "public-demo"

    manifest_path = FIXTURES.generate_demo_fixtures(output_dir)

    assert manifest_path == output_dir.resolve() / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["generated_by"] == (
        "translation_core/tools/p1a_demo_fixtures.py"
    )
    assert manifest["model_calls"] == 0
    assert [item["scenario_id"] for item in manifest["scenarios"]] == [
        "direct-docx",
        "repair-pptx",
        "high-risk-pptx",
    ]

    expected = {
        "direct-docx": ("docx", [0], "COMPLETED", 200, "real_python_core"),
        "repair-pptx": (
            "pptx",
            [2, 0],
            "COMPLETED",
            200,
            "scripted_fixture_candidate",
        ),
        "high-risk-pptx": (
            "pptx",
            [3, 2, 1],
            "NEEDS_REVIEW",
            409,
            "scripted_fixture_candidate",
        ),
    }
    for scenario in manifest["scenarios"]:
        scenario_id = scenario["scenario_id"]
        document_format, counts, status, result_status, evidence_kind = expected[
            scenario_id
        ]
        assert scenario["document_format"] == document_format
        assert scenario["expected_blocking_counts"] == counts
        assert scenario["expected_terminal_status"] == status
        assert scenario["expected_result_http_status"] == result_status
        assert scenario["evidence_kind"] == evidence_kind
        assert scenario["native_python_qa"] is True
        assert scenario["repair_execution"] == (
            "not_applicable"
            if scenario_id == "direct-docx"
            else "scripted_fixture_candidate"
        )
        assert "\\" not in scenario["source_relpath"]
        if scenario_id == "direct-docx":
            assert scenario["fixture_relpath"] == (
                "fixtures/direct-docx/translation-fixture.json"
            )
            fixture = json.loads(
                (output_dir / scenario["fixture_relpath"]).read_text(
                    encoding="utf-8"
                )
            )
            assert fixture == {
                "by_source": {
                    "Quarterly launch readiness review": "季度发射准备情况评审"
                }
            }
        assert scenario["candidate_relpaths"] == list(
            scenario["candidate_sha256"]
        )
        for relpath in scenario["candidate_relpaths"]:
            assert "\\" not in relpath
            candidate = output_dir / relpath
            assert scenario["candidate_sha256"][relpath] == hashlib.sha256(
                candidate.read_bytes()
            ).hexdigest()


def test_every_office_fixture_opens_in_core_and_high_risk_fingerprints_shrink(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "public-demo"
    FIXTURES.generate_demo_fixtures(output_dir)

    office_files = sorted((output_dir / "fixtures").rglob("*.docx")) + sorted(
        (output_dir / "fixtures").rglob("*.pptx")
    )
    assert len(office_files) == 9
    for office_file in office_files:
        if office_file.suffix == ".docx":
            assert validate_output_package(office_file)["valid"] is True
            assert extract_docx_blocks(office_file)
        else:
            assert validate_pptx(office_file)["valid"] is True
            assert extract_pptx_blocks(office_file)

    scenario_dir = output_dir / "fixtures" / "high-risk-pptx"
    source = scenario_dir / "source.pptx"
    assert len(extract_pptx_blocks(source)) == 3
    fingerprints = []
    for round_index in range(3):
        issues = run_pptx_qa(
            source,
            scenario_dir / f"candidate-r{round_index}.pptx",
        )
        fingerprints.append(
            {
                (issue.code, issue.slide, issue.details.get("shape_id"))
                for issue in issues
                if issue.severity.value == "error"
            }
        )

    assert fingerprints == [
        {
            ("PPTX_TRANSLATION_PROMPT_LEAKAGE", 1, "2"),
            ("PPTX_TRANSLATION_PROMPT_LEAKAGE", 2, "2"),
            ("PPTX_TRANSLATION_PROMPT_LEAKAGE", 3, "2"),
        },
        {
            ("PPTX_TRANSLATION_PROMPT_LEAKAGE", 2, "2"),
            ("PPTX_TRANSLATION_PROMPT_LEAKAGE", 3, "2"),
        },
        {("PPTX_TRANSLATION_PROMPT_LEAKAGE", 3, "2")},
    ]
    assert fingerprints[2] < fingerprints[1] < fingerprints[0]


def test_native_core_observes_claimed_direct_and_repair_qa_counts(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "public-demo"
    FIXTURES.generate_demo_fixtures(output_dir)

    direct_dir = output_dir / "fixtures" / "direct-docx"
    assert run_docx_qa(
        direct_dir / "source.docx",
        direct_dir / "candidate-r0.docx",
    ) == []

    repair_dir = output_dir / "fixtures" / "repair-pptx"
    source = repair_dir / "source.pptx"
    assert [
        len(
            [
                issue
                for issue in run_pptx_qa(
                    source,
                    repair_dir / f"candidate-r{round_index}.pptx",
                )
                if issue.severity.value == "error"
            ]
        )
        for round_index in range(2)
    ] == [2, 0]
