from __future__ import annotations

import json
import platform
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import format_translator.pptx_rendered_layout_qa as rendered_layout_module
from format_translator.pptx_rendered_layout_qa import (
    PowerPointComWorkerPool,
    compare_pptx_rendered_layout,
    pptx_text_shape_targets,
    run_pptx_qa_with_rendered_layout,
    run_pptx_rendered_layout_qa,
)
from format_translator.pptx_repair_adapter import (
    PptxRepairAdapterError,
    execute_pptx_repair_adapter,
)
from format_translator.qa_agent import (
    QaAgentState,
    QaRepairSpec,
    run_qa_repair_agent,
)
from test_pptx_engine import _write_pptx


def _write_table_pptx(path: Path) -> None:
    _write_pptx(path)
    with zipfile.ZipFile(path) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    slide_name = "ppt/slides/slide1.xml"
    slide = parts[slide_name].decode("utf-8")
    shape_start = slide.index("    <p:sp>")
    shape_end = slide.index("    </p:sp>", shape_start) + len("    </p:sp>")
    table = """    <p:graphicFrame>
      <p:nvGraphicFramePr><p:cNvPr id="2" name="Table 1"/>
        <p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
      <p:xfrm><a:off x="500000" y="500000"/>
        <a:ext cx="4000000" cy="1000000"/></p:xfrm>
      <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">
        <a:tbl><a:tblPr firstRow="1"/><a:tblGrid>
          <a:gridCol w="4000000"/></a:tblGrid>
          <a:tr h="1000000"><a:tc><a:txBody><a:bodyPr/>
            <a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="1800"/>
              <a:t>Translated table cell</a:t></a:r>
              <a:endParaRPr lang="en-US"/></a:p></a:txBody>
            <a:tcPr/></a:tc></a:tr>
        </a:tbl>
      </a:graphicData></a:graphic>
    </p:graphicFrame>"""
    parts[slide_name] = (slide[:shape_start] + table + slide[shape_end:]).encode(
        "utf-8"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)


def _write_grouped_text_pptx(path: Path) -> None:
    _write_pptx(path)
    with zipfile.ZipFile(path) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    slide_name = "ppt/slides/slide1.xml"
    slide = parts[slide_name].decode("utf-8")
    shape_start = slide.index("    <p:sp>")
    shape_end = slide.index("    </p:sp>", shape_start) + len("    </p:sp>")
    grouped_shape = """    <p:grpSp>
      <p:nvGrpSpPr><p:cNvPr id="2" name="Group 1"/>
        <p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="500000" y="500000"/>
        <a:ext cx="4000000" cy="1000000"/><a:chOff x="0" y="0"/>
        <a:chExt cx="4000000" cy="1000000"/></a:xfrm></p:grpSpPr>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="Grouped Text"/>
          <p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="0" y="0"/>
          <a:ext cx="4000000" cy="1000000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r>
          <a:rPr lang="en-US" sz="1800"/><a:t>Grouped text</a:t></a:r>
          <a:endParaRPr lang="en-US"/></a:p></p:txBody>
      </p:sp>
    </p:grpSp>"""
    parts[slide_name] = (
        slide[:shape_start] + grouped_shape + slide[shape_end:]
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)


def test_powerpoint_com_json_transport_uses_explicit_utf8() -> None:
    script = rendered_layout_module._POWERPOINT_COM_SCRIPT

    assert "Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8" in script
    assert "Set-Content -LiteralPath $OutputJson -Encoding UTF8" in script


def test_grouped_text_target_is_resolved_through_com_group_items(
    tmp_path: Path,
) -> None:
    document = tmp_path / "grouped-text.pptx"
    _write_grouped_text_pptx(document)

    assert pptx_text_shape_targets(document) == [{"slide": 1, "shape_id": "3"}]
    script = rendered_layout_module._POWERPOINT_COM_SCRIPT
    assert "function Find-ShapeById" in script
    assert "-Shapes $candidate.GroupItems" in script
    assert "$shape = Find-ShapeById -Shapes $slide.Shapes" in script


def test_powerpoint_com_probe_collects_stable_slide_local_objects() -> None:
    script = rendered_layout_module._POWERPOINT_COM_SCRIPT

    assert "'slide_object_bounds'" in script
    assert "slide_objects = @()" in script
    assert "object_identity" in script
    assert "Get-SlideObjectRecords" in script


def test_ordinary_shape_measurement_failure_remains_blocking(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)

    report = run_pptx_rendered_layout_qa(
        document,
        [{"slide": 1, "shape_id": "2"}],
        runner=lambda _document, _targets: {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    "slide": 1,
                    "shape_id": "2",
                    "measurement_error": "ordinary shape probe failed",
                }
            ],
        },
    )

    assert report["issues"] == [
        {
            "code": "PPTX_RENDERED_LAYOUT_MEASUREMENT_FAILED",
            "severity": "error",
            "message": "ordinary shape probe failed",
            "slide": 1,
            "shape_id": "2",
            "details": {
                "shape_id": "2",
                "measurement_error": "ordinary shape probe failed",
            },
        }
    ]


def test_non_positive_available_bounds_fail_closed_and_preserve_other_measurements(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)

    report = run_pptx_rendered_layout_qa(
        document,
        [
            {"slide": 1, "shape_id": "2"},
            {"slide": 1, "shape_id": "3"},
            {"slide": 1, "shape_id": "4"},
        ],
        runner=lambda _document, _targets: {
            "provider": "fake-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [
                {
                    "slide": 1,
                    "shape_id": "2",
                    "bound_width_pt": 50.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 0.0,
                    "available_height_pt": 20.0,
                },
                {
                    "slide": 1,
                    "shape_id": "3",
                    "bound_width_pt": 50.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 0.0,
                },
                {
                    "slide": 1,
                    "shape_id": "4",
                    "bound_width_pt": 120.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                },
            ],
        },
        required=True,
    )

    measurements = {
        str(measurement["shape_id"]): measurement
        for measurement in report["measurements"]
    }
    assert set(measurements) == {"2", "3", "4"}
    for shape_id in ("2", "3"):
        assert measurements[shape_id]["overflow"] is None
        assert "rendered_load_ratio" not in measurements[shape_id]
    assert measurements["4"]["overflow"] is True
    assert measurements["4"]["rendered_load_ratio"] == pytest.approx(1.2)

    invalid_bound_issues = [
        issue
        for issue in report["issues"]
        if issue["code"] == "PPTX_RENDERED_LAYOUT_MEASUREMENT_FAILED"
    ]
    assert [issue["shape_id"] for issue in invalid_bound_issues] == ["2", "3"]
    assert all(issue["severity"] == "error" for issue in invalid_bound_issues)
    assert all(
        issue["details"]["reason"] == "non_positive_available_text_bounds"
        for issue in invalid_bound_issues
    )
    assert all(
        issue["code"] != "PPTX_RENDERED_LAYOUT_TARGET_NOT_MEASURED"
        for issue in report["issues"]
    )
    assert any(
        issue["code"] == "PPTX_RENDERED_TEXT_OVERFLOW"
        and issue["shape_id"] == "4"
        for issue in report["issues"]
    )


def test_powerpoint_worker_pool_limits_twenty_parallel_jobs(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def slow_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.03)
            return {
                "provider": "fake-powerpoint",
                "available": True,
                "measurements": [
                    {
                        **targets[0],
                        "bound_width_pt": 300.0,
                        "bound_height_pt": 80.0,
                        "available_width_pt": 400.0,
                        "available_height_pt": 100.0,
                    }
                ],
            }
        finally:
            with state_lock:
                active -= 1

    pool = PowerPointComWorkerPool(
        runner=slow_runner,
        max_workers=3,
        queue_timeout_seconds=2,
    )

    def run_one(_index: int) -> dict[str, object]:
        return run_pptx_rendered_layout_qa(
            document,
            [{"slide": 1, "shape_id": "2"}],
            runner=pool,
            required=True,
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        reports = list(executor.map(run_one, range(20)))

    assert maximum_active == 3
    assert all(report["available"] is True for report in reports)
    assert all(report["pool_max_workers"] == 3 for report in reports)
    assert all(report["queue_wait_ms"] >= 0 for report in reports)


def test_linux_auto_backend_uses_libreoffice_openability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = tmp_path / "candidate.pptx"
    soffice = tmp_path / "soffice"
    _write_pptx(document)
    soffice.write_text("#!/bin/sh\n", encoding="utf-8")

    def fake_run(command: list[str], **_kwargs: object) -> object:
        if "--version" in command:
            return type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": "LibreOffice 24.2.7.2\n",
                    "stderr": "",
                },
            )()
        output_dir = Path(command[command.index("--outdir") + 1])
        source = Path(command[-1])
        (output_dir / source.name).write_bytes(source.read_bytes())
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )()

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("FORMAT_TRANSLATOR_PPTX_LAYOUT_BACKEND", "auto")
    monkeypatch.setenv("FORMAT_TRANSLATOR_SOFFICE_PATH", str(soffice))
    monkeypatch.setattr(rendered_layout_module.subprocess, "run", fake_run)
    monkeypatch.setattr(rendered_layout_module, "_default_pools", {})

    report = run_pptx_rendered_layout_qa(
        document,
        [{"slide": 1, "shape_id": "2"}],
    )

    assert report["provider"] == "libreoffice-headless"
    assert report["available"] is True
    assert report["capabilities"] == ["package_openability"]
    assert report["measurements"] == []
    assert report["issues"] == []


def test_required_shape_bounds_rejects_openability_only_backend(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)

    report = run_pptx_rendered_layout_qa(
        document,
        [{"slide": 1, "shape_id": "2"}],
        runner=lambda _document, _targets: {
            "provider": "libreoffice-headless",
            "available": True,
            "capabilities": ["package_openability"],
            "precision": "package",
            "measurements": [],
        },
        required=True,
    )

    assert report["available"] is True
    assert report["issues"][0]["code"] == (
        "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT"
    )
    assert report["issues"][0]["severity"] == "error"


def test_libreoffice_openability_failure_is_fail_closed(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)

    report = run_pptx_rendered_layout_qa(
        document,
        [{"slide": 1, "shape_id": "2"}],
        runner=lambda _document, _targets: {
            "provider": "libreoffice-headless",
            "available": False,
            "capabilities": ["package_openability"],
            "precision": "package",
            "measurements": [],
            "unavailable_reason": "LibreOffice could not open the file.",
            "fail_closed": True,
        },
    )

    assert report["issues"][0]["code"] == ("PPTX_LAYOUT_PROVIDER_UNAVAILABLE")
    assert report["issues"][0]["severity"] == "error"


def test_powerpoint_worker_pools_share_machine_slots(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)
    lock_directory = tmp_path / "com-slots"
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def slow_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.03)
            return {
                "provider": "fake-powerpoint",
                "available": True,
                "measurements": [
                    {
                        **targets[0],
                        "bound_width_pt": 300.0,
                        "bound_height_pt": 80.0,
                        "available_width_pt": 400.0,
                        "available_height_pt": 100.0,
                    }
                ],
            }
        finally:
            with state_lock:
                active -= 1

    pools = [
        PowerPointComWorkerPool(
            runner=slow_runner,
            max_workers=2,
            queue_timeout_seconds=2,
            lock_directory=lock_directory,
        )
        for _ in range(2)
    ]

    def run_one(index: int) -> dict[str, object]:
        return run_pptx_rendered_layout_qa(
            document,
            [{"slide": 1, "shape_id": "2"}],
            runner=pools[index % len(pools)],
            required=True,
        )

    with ThreadPoolExecutor(max_workers=10) as executor:
        reports = list(executor.map(run_one, range(10)))

    assert maximum_active == 2
    assert all(report["available"] is True for report in reports)
    assert all(report["pool_scope"] == "machine" for report in reports)
    assert {report["worker_slot"] for report in reports} == {0, 1}


def test_powerpoint_worker_pool_recovers_after_queue_timeout(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)
    entered = threading.Event()
    release = threading.Event()

    def blocking_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        entered.set()
        release.wait(timeout=2)
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 300.0,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
            ],
        }

    pool = PowerPointComWorkerPool(
        runner=blocking_runner,
        max_workers=1,
        queue_timeout_seconds=0.05,
    )
    targets = [{"slide": 1, "shape_id": "2"}]
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            run_pptx_rendered_layout_qa,
            document,
            targets,
            runner=pool,
            required=True,
        )
        assert entered.wait(timeout=1)
        timed_out = run_pptx_rendered_layout_qa(
            document,
            targets,
            runner=pool,
            required=True,
        )
        release.set()
        assert first.result(timeout=1)["available"] is True

    recovered = run_pptx_rendered_layout_qa(
        document,
        targets,
        runner=pool,
        required=True,
    )

    assert timed_out["available"] is False
    assert timed_out["issues"][0]["code"] == "PPTX_RENDERED_LAYOUT_UNAVAILABLE"
    assert (
        timed_out["issues"][0]["details"]["reason"] == (timed_out["unavailable_reason"])
    )
    assert (
        "waiting for an available PowerPoint COM worker"
        in (timed_out["unavailable_reason"])
    )
    assert recovered["available"] is True


def test_rendered_layout_qa_reports_actual_powerpoint_overflow(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)

    def overflowing_runner(
        _document: Path,
        _targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    "slide": 1,
                    "shape_id": "2",
                    "bound_width_pt": 410.0,
                    "bound_height_pt": 120.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
            ],
        }

    report = run_pptx_rendered_layout_qa(
        document,
        [{"slide": 1, "shape_id": "2"}],
        runner=overflowing_runner,
    )

    assert report["provider"] == "fake-powerpoint"
    assert report["available"] is True
    assert report["measurements"][0]["overflow"] is True
    assert report["issues"][0]["code"] == "PPTX_RENDERED_TEXT_OVERFLOW"
    assert report["issues"][0]["severity"] == "error"


def test_rendered_layout_qa_tolerates_small_horizontal_glyph_overhang(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)

    def glyph_overhang_runner(
        _document: Path,
        _targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    "slide": 1,
                    "shape_id": "2",
                    "bound_width_pt": 407.5,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
            ],
        }

    report = run_pptx_rendered_layout_qa(
        document,
        [{"slide": 1, "shape_id": "2"}],
        runner=glyph_overhang_runner,
    )

    assert report["measurements"][0]["overflow"] is False
    assert report["measurements"][0]["width_tolerance_pt"] == 8.0
    assert report["issues"] == []


@pytest.mark.parametrize(
    ("candidate_bound_width", "expected_rendered_overflow"),
    [(300.0, False), (500.0, True)],
    ids=["rendered-safe", "rendered-overflow"],
)
def test_authoritative_rendered_measurement_supersedes_estimated_capacity_issue(
    tmp_path: Path,
    candidate_bound_width: float,
    expected_rendered_overflow: bool,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(
        candidate,
        text="Translated content " * 30,
        shape_width=1_200_000,
        shape_height=500_000,
    )

    def authoritative_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        bound_width = (
            candidate_bound_width
            if document.resolve() == candidate.resolve()
            else 300.0
        )
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [
                {
                    **target,
                    "bound_width_pt": bound_width,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
                for target in targets
            ],
        }

    report = run_pptx_qa_with_rendered_layout(
        source,
        candidate,
        layout_runner=authoritative_runner,
        rendered_layout_required=True,
    )
    codes = [issue.code for issue in report["issues"]]

    assert "PPTX_TEXT_CAPACITY_RISK" not in codes
    assert ("PPTX_RENDERED_TEXT_OVERFLOW" in codes) is expected_rendered_overflow


def test_estimated_capacity_is_retained_when_rendered_bounds_are_unavailable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(
        candidate,
        text="Translated content " * 30,
        shape_width=1_200_000,
        shape_height=500_000,
    )

    def openability_only_runner(
        _document: Path,
        _targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "openability-only",
            "available": True,
            "capabilities": ["package_openability"],
            "measurements": [],
        }

    report = run_pptx_qa_with_rendered_layout(
        source,
        candidate,
        layout_runner=openability_only_runner,
        rendered_layout_required=True,
    )
    codes = [issue.code for issue in report["issues"]]

    assert "PPTX_TEXT_CAPACITY_RISK" in codes
    assert "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT" in codes


def test_adapter_deletes_output_when_powerpoint_reports_overflow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    audit = tmp_path / "repair-audit.json"
    _write_pptx(source)
    _write_pptx(candidate)

    def overflowing_runner(
        document: Path,
        _targets: list[dict[str, object]],
    ) -> dict[str, object]:
        overflow = document.name == "output.pptx"
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    "slide": 1,
                    "shape_id": "2",
                    "bound_width_pt": 410.0 if overflow else 300.0,
                    "bound_height_pt": 120.0 if overflow else 80.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
            ],
        }

    with pytest.raises(
        PptxRepairAdapterError,
        match="PPTX_RENDERED_TEXT_OVERFLOW",
    ):
        execute_pptx_repair_adapter(
            source,
            candidate,
            output,
            actions=[
                {
                    "action": "set_autofit",
                    "slide": 1,
                    "shape_id": "2",
                }
            ],
            layout_runner=overflowing_runner,
            audit_path=audit,
        )

    assert not output.exists()
    failed_audit = json.loads(audit.read_text(encoding="utf-8"))
    assert failed_audit["status"] == "rendered_layout_qa_failed"
    assert failed_audit["rendered_layout_qa"]["provider"] == "fake-powerpoint"
    assert failed_audit["rendered_layout_qa"]["issues"][0]["code"] == (
        "PPTX_RENDERED_TEXT_OVERFLOW"
    )


def test_reference_existing_overflow_is_not_a_new_candidate_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def same_overflow_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 300.0,
                    "bound_height_pt": 120.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=same_overflow_runner,
        required=True,
    )

    assert report["raw_issues"][0]["code"] == "PPTX_RENDERED_TEXT_OVERFLOW"
    assert report["issues"] == []
    assert report["comparison"]["policy"] == "new_or_materially_worsened"


def test_reference_horizontal_overhang_is_subtracted_before_tolerance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def baseline_overhang_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        overflow_width = 4.456 if document.name == "source.pptx" else 17.956
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 799.419 + overflow_width,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 799.419,
                    "available_height_pt": 100.0,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 15, "shape_id": "6"}],
        runner=baseline_overhang_runner,
        required=True,
    )

    assert report["raw_issues"][0]["code"] == "PPTX_RENDERED_TEXT_OVERFLOW"
    assert report["measurements"][0]["width_tolerance_pt"] == pytest.approx(15.98838)
    assert report["issues"] == []


def test_reference_horizontal_overhang_does_not_hide_material_increase(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def worsened_overhang_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        overflow_width = 4.456 if document.name == "source.pptx" else 20.5
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 799.419 + overflow_width,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 799.419,
                    "available_height_pt": 100.0,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 15, "shape_id": "6"}],
        runner=worsened_overhang_runner,
        required=True,
    )

    assert [issue["code"] for issue in report["issues"]] == [
        "PPTX_RENDERED_TEXT_OVERFLOW"
    ]
    assert report["issues"][0]["details"][
        "reference_overflow_width_pt"
    ] == pytest.approx(4.456)


def test_missing_reference_measurement_does_not_hide_candidate_overflow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def missing_reference_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        measurements: list[dict[str, object]] = []
        if document.name == "candidate.pptx":
            measurements.append(
                {
                    **targets[0],
                    "bound_width_pt": 820.0,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 799.419,
                    "available_height_pt": 100.0,
                }
            )
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": measurements,
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 15, "shape_id": "6"}],
        runner=missing_reference_runner,
        required=True,
    )

    assert [issue["code"] for issue in report["issues"]] == [
        "PPTX_RENDERED_TEXT_OVERFLOW"
    ]


def test_runtime_table_geometry_change_is_reported_against_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def table_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "capabilities": [
                "shape_text_bounds",
                "table_runtime_geometry",
            ],
            "measurements": [
                {
                    **targets[0],
                    "is_table": True,
                    "table_frame_left_pt": 120.0,
                    "table_frame_top_pt": 90.0,
                    "table_frame_width_pt": 600.0,
                    "table_frame_height_pt": 419.7 if translated else 441.4,
                    "table_row_heights_pt": (
                        [70.0, 89.7, 130.0, 130.0]
                        if translated
                        else [70.0, 101.4, 135.0, 135.0]
                    ),
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=table_runner,
        required=True,
    )

    assert report["raw_issues"] == []
    assert report["issues"][0]["code"] == ("PPTX_RENDERED_TABLE_GEOMETRY_CHANGED")
    assert report["issues"][0]["severity"] == "error"
    assert report["issues"][0]["details"]["reference_frame_height_pt"] == (441.4)
    assert report["issues"][0]["details"]["candidate_frame_height_pt"] == (419.7)
    assert report["issues"][0]["details"]["changed_row_indices"] == [2, 3, 4]
    assert report["issues"][0]["details"]["shortened_row_indices"] == [2, 3, 4]
    assert report["issues"][0]["details"]["taller_row_indices"] == []
    assert report["issues"][0]["details"]["repair_hint"] == {
        "action": "restore_table_row_heights",
        "automatic": True,
        "slide": 1,
        "shape_id": "2",
        "rows": [
            {"row_index": 2, "height_pt": 101.4},
            {"row_index": 3, "height_pt": 135.0},
            {"row_index": 4, "height_pt": 135.0},
        ],
    }
    assert report["comparison"]["policy"] == "new_or_materially_worsened"


def test_inline_table_is_included_in_powerpoint_measurement_targets(
    tmp_path: Path,
) -> None:
    document = tmp_path / "table.pptx"
    _write_table_pptx(document)

    assert pptx_text_shape_targets(document) == [{"slide": 1, "shape_id": "2"}]


@pytest.mark.parametrize(
    ("candidate_frame_height", "candidate_rows"),
    [
        (441.4, [70.0, 101.4, 135.0, 135.0]),
        (445.0, [70.8, 100.5, 135.9, 134.1]),
    ],
)
def test_existing_or_subtolerance_table_geometry_is_not_reported(
    tmp_path: Path,
    candidate_frame_height: float,
    candidate_rows: list[float],
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def stable_table_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "capabilities": [
                "shape_text_bounds",
                "table_runtime_geometry",
            ],
            "measurements": [
                {
                    **targets[0],
                    "is_table": True,
                    "table_frame_left_pt": 120.0,
                    "table_frame_top_pt": 90.0,
                    "table_frame_width_pt": 600.0,
                    "table_frame_height_pt": (
                        candidate_frame_height if translated else 441.4
                    ),
                    "table_row_heights_pt": (
                        candidate_rows if translated else [70.0, 101.4, 135.0, 135.0]
                    ),
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=stable_table_runner,
        required=True,
    )

    assert report["issues"] == []
    assert report["comparison"]["effective_issue_count"] == 0


def test_table_row_change_is_reported_when_total_frame_height_is_stable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def row_change_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "is_table": True,
                    "table_frame_left_pt": 120.0,
                    "table_frame_top_pt": 90.0,
                    "table_frame_width_pt": 600.0,
                    "table_frame_height_pt": 200.0,
                    "table_row_heights_pt": (
                        [80.0, 120.0] if translated else [100.0, 100.0]
                    ),
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=row_change_runner,
    )

    issue = report["issues"][0]
    assert issue["code"] == "PPTX_RENDERED_TABLE_GEOMETRY_CHANGED"
    assert issue["details"]["frame_height_delta_pt"] == 0.0
    assert issue["details"]["changed_row_indices"] == [1, 2]
    assert issue["details"]["shortened_row_indices"] == [1]
    assert issue["details"]["taller_row_indices"] == [2]
    assert "repair_hint" not in issue["details"]


def test_new_rendered_cjk_line_break_violations_are_reported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def line_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        lines = (
            [
                {
                    "line_index": 1,
                    "text": "经济",
                    "bound_left_pt": 10.0,
                    "bound_top_pt": 10.0,
                    "bound_width_pt": 40.0,
                    "bound_height_pt": 12.0,
                    "ends_with_paragraph_break": False,
                    "ends_with_manual_break": False,
                },
                {
                    "line_index": 2,
                    "text": "，继续增长",
                    "bound_left_pt": 10.0,
                    "bound_top_pt": 22.0,
                    "bound_width_pt": 70.0,
                    "bound_height_pt": 12.0,
                    "ends_with_paragraph_break": False,
                    "ends_with_manual_break": False,
                },
                {
                    "line_index": 3,
                    "text": "。",
                    "bound_left_pt": 10.0,
                    "bound_top_pt": 34.0,
                    "bound_width_pt": 10.0,
                    "bound_height_pt": 12.0,
                    "ends_with_paragraph_break": True,
                    "ends_with_manual_break": False,
                },
                {
                    "line_index": 4,
                    "text": "2026",
                    "bound_left_pt": 10.0,
                    "bound_top_pt": 46.0,
                    "bound_width_pt": 30.0,
                    "bound_height_pt": 12.0,
                    "ends_with_paragraph_break": False,
                    "ends_with_manual_break": False,
                },
                {
                    "line_index": 5,
                    "text": "亿元",
                    "bound_left_pt": 10.0,
                    "bound_top_pt": 58.0,
                    "bound_width_pt": 24.0,
                    "bound_height_pt": 12.0,
                    "ends_with_paragraph_break": True,
                    "ends_with_manual_break": False,
                },
            ]
            if translated
            else [
                {
                    "line_index": 1,
                    "text": "Clean source\r",
                    "bound_left_pt": 10.0,
                    "bound_top_pt": 10.0,
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 12.0,
                    "ends_with_paragraph_break": True,
                    "ends_with_manual_break": False,
                },
            ]
        )
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds", "rendered_text_lines"],
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 100.0,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 200.0,
                    "available_height_pt": 100.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "".join(line["text"] for line in lines),
                            "rendered_lines": lines,
                        }
                    ],
                }
            ],
            "inherited_objects": [],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=line_runner,
    )

    issue = report["issues"][0]
    assert issue["code"] == "PPTX_RENDERED_LINE_BREAK_VIOLATION"
    assert issue["severity"] == "warning"
    assert issue["details"]["reference_violation_count"] == 0
    assert issue["details"]["candidate_violation_count"] == 3
    assert [violation["type"] for violation in issue["details"]["violations"]] == [
        "forbidden_line_start",
        "punctuation_only_line",
        "number_unit_split",
    ]


def test_new_table_cell_line_violation_keeps_cell_location(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def table_line_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        first = "，恶化" if translated else "Source"
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "is_table": True,
                    "table_frame_left_pt": 10.0,
                    "table_frame_top_pt": 10.0,
                    "table_frame_width_pt": 200.0,
                    "table_frame_height_pt": 40.0,
                    "table_row_heights_pt": [40.0],
                    "text_regions": [
                        {
                            "region_id": "cell-1-2",
                            "kind": "table_cell",
                            "cell_row": 1,
                            "cell_column": 2,
                            "text": "上一行" + first,
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "上一行",
                                    "bound_left_pt": 10.0,
                                    "bound_top_pt": 10.0,
                                    "bound_width_pt": 40.0,
                                    "bound_height_pt": 12.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                },
                                {
                                    "line_index": 2,
                                    "text": first,
                                    "bound_left_pt": 10.0,
                                    "bound_top_pt": 22.0,
                                    "bound_width_pt": 40.0,
                                    "bound_height_pt": 12.0,
                                    "ends_with_paragraph_break": True,
                                    "ends_with_manual_break": False,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=table_line_runner,
    )

    details = report["issues"][0]["details"]
    assert details["region_id"] == "cell-1-2"
    assert details["cell_row"] == 1
    assert details["cell_column"] == 2
    assert details["violations"][0]["line_index"] == 2


def test_new_rendered_text_overlap_with_master_object_is_reported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def overlap_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        line_width = 813.57 if translated else 788.99
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": line_width,
                    "bound_height_pt": 16.0,
                    "available_width_pt": 900.0,
                    "available_height_pt": 30.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "footer",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "footer",
                                    "bound_left_pt": 48.5,
                                    "bound_top_pt": 500.0,
                                    "bound_width_pt": line_width,
                                    "bound_height_pt": 16.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                },
                            ],
                        }
                    ],
                }
            ],
            "inherited_objects": [
                {
                    "slide": 1,
                    "scope": "master",
                    "shape_id": "10",
                    "name": "Object 9",
                    "shape_type": 7,
                    "left_pt": 856.12,
                    "top_pt": 474.0,
                    "width_pt": 103.88,
                    "height_pt": 66.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": 0.9,
                },
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=overlap_runner,
    )

    issue = report["issues"][0]
    assert issue["code"] == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP"
    assert issue["severity"] == "error"
    assert issue["details"]["reference_overlap_area_pt2"] == 0.0
    assert issue["details"]["candidate_overlap_area_pt2"] == pytest.approx(
        95.2,
        abs=0.2,
    )
    assert issue["details"]["object"]["scope"] == "master"
    assert issue["details"]["object"]["shape_id"] == "10"
    assert issue["details"]["overlaps"][0]["line_index"] == 1


@pytest.mark.parametrize(
    ("object_kind", "shape_type"),
    [("image", 13), ("chart", 3), ("shape", 1)],
)
def test_new_rendered_text_overlap_with_slide_local_object_is_reported(
    tmp_path: Path,
    object_kind: str,
    shape_type: int,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def overlap_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        line_width = 80.0 if translated else 40.0
        return {
            "provider": "powerpoint-com",
            "available": True,
            "capabilities": [
                "shape_text_bounds",
                "rendered_text_lines",
                "slide_object_bounds",
            ],
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": line_width,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "translated",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "translated",
                                    "bound_left_pt": 0.0,
                                    "bound_top_pt": 0.0,
                                    "bound_width_pt": line_width,
                                    "bound_height_pt": 10.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                }
                            ],
                        }
                    ],
                }
            ],
            "slide_objects": [
                {
                    "slide": 1,
                    "scope": "slide",
                    "shape_id": "2",
                    "object_identity": "slide:1:shape:2",
                    "object_kind": "shape",
                    "name": "Target text box",
                    "shape_type": 1,
                    "left_pt": 0.0,
                    "top_pt": 0.0,
                    "width_pt": 100.0,
                    "height_pt": 20.0,
                    "visible": True,
                    "has_text_content": True,
                    "area_ratio": 0.01,
                },
                {
                    "slide": 1,
                    "scope": "slide",
                    "shape_id": "9",
                    "object_identity": "slide:1:shape:9",
                    "object_kind": object_kind,
                    "name": f"Local {object_kind}",
                    "shape_type": shape_type,
                    "left_pt": 50.0,
                    "top_pt": 0.0,
                    "width_pt": 30.0,
                    "height_pt": 10.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": 0.9,
                },
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=overlap_runner,
        required=True,
    )

    overlap_issues = [
        issue
        for issue in report["issues"]
        if issue["code"] == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP"
    ]
    assert len(overlap_issues) == 1
    details = overlap_issues[0]["details"]
    assert details["object_scope"] == "slide"
    assert details["object_shape_id"] == "9"
    assert details["object_identity"] == "slide:1:shape:9"
    assert details["object"]["object_kind"] == object_kind
    assert details["reference_overlap_area_pt2"] == 0.0
    assert details["candidate_overlap_area_pt2"] == pytest.approx(300.0)


@pytest.mark.parametrize(
    ("image_z_order", "text_group_path", "expected_issue_count"),
    [(1, [], 0), (6, [], 1), (1, ["100"], 1)],
)
def test_large_slide_image_is_ignored_only_when_it_is_behind_text(
    tmp_path: Path,
    image_z_order: int,
    text_group_path: list[str],
    expected_issue_count: int,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def overlap_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        line_width = 80.0 if translated else 40.0
        return {
            "provider": "powerpoint-com",
            "available": True,
            "capabilities": [
                "shape_text_bounds",
                "rendered_text_lines",
                "slide_object_bounds",
            ],
            "measurements": [
                {
                    **targets[0],
                    "z_order_position": 5,
                    "bound_width_pt": line_width,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "translated",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "translated",
                                    "bound_left_pt": 0.0,
                                    "bound_top_pt": 0.0,
                                    "bound_width_pt": line_width,
                                    "bound_height_pt": 10.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                }
                            ],
                        }
                    ],
                }
            ],
            "slide_objects": [
                {
                    "slide": 1,
                    "scope": "slide",
                    "shape_id": "2",
                    "object_identity": "slide:1:shape:2",
                    "object_kind": "shape",
                    "group_path_shape_ids": text_group_path,
                    "name": "Target text",
                    "shape_type": 1,
                    "z_order_position": 5,
                    "left_pt": 0.0,
                    "top_pt": 0.0,
                    "width_pt": 100.0,
                    "height_pt": 20.0,
                    "visible": True,
                    "has_text_content": True,
                    "area_ratio": 0.01,
                },
                {
                    "slide": 1,
                    "scope": "slide",
                    "shape_id": "9",
                    "object_identity": "slide:1:shape:9",
                    "object_kind": "image",
                    "group_path_shape_ids": [],
                    "name": "Large slide image",
                    "shape_type": 13,
                    "z_order_position": image_z_order,
                    "left_pt": 50.0,
                    "top_pt": 0.0,
                    "width_pt": 600.0,
                    "height_pt": 400.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": 0.9,
                },
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=overlap_runner,
        required=True,
    )

    overlap_issues = [
        issue
        for issue in report["issues"]
        if issue["code"] == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP"
    ]
    assert len(overlap_issues) == expected_issue_count


def test_existing_slide_local_overlap_is_suppressed_by_source_baseline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def existing_overlap_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "powerpoint-com",
            "available": True,
            "capabilities": ["shape_text_bounds", "slide_object_bounds"],
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "intentional overlay",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "intentional overlay",
                                    "bound_left_pt": 0.0,
                                    "bound_top_pt": 0.0,
                                    "bound_width_pt": 80.0,
                                    "bound_height_pt": 10.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                }
                            ],
                        }
                    ],
                }
            ],
            "slide_objects": [
                {
                    "slide": 1,
                    "scope": "slide",
                    "shape_id": "9",
                    "object_identity": "slide:1:shape:9",
                    "object_kind": "image",
                    "name": "Intentional overlay image",
                    "shape_type": 13,
                    "left_pt": 50.0,
                    "top_pt": 0.0,
                    "width_pt": 30.0,
                    "height_pt": 10.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": 0.01,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=existing_overlap_runner,
        required=True,
    )

    assert not any(
        issue["code"] == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP"
        for issue in report["issues"]
    )


def test_required_powerpoint_com_reports_missing_slide_object_capability(
    tmp_path: Path,
) -> None:
    document = tmp_path / "candidate.pptx"
    _write_pptx(document)

    report = run_pptx_rendered_layout_qa(
        document,
        [{"slide": 1, "shape_id": "2"}],
        runner=lambda _document, targets: {
            "provider": "powerpoint-com",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                }
            ],
        },
        required=True,
    )

    issue = next(
        issue
        for issue in report["issues"]
        if issue["code"] == "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT"
    )
    assert issue["severity"] == "error"
    assert issue["details"]["required_capability"] == "slide_object_bounds"


def test_compare_propagates_reference_only_capability_insufficiency(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def capability_changes_between_documents(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        capabilities = ["shape_text_bounds"]
        if document.name == "candidate.pptx":
            capabilities.append("slide_object_bounds")
        return {
            "provider": "powerpoint-com",
            "available": True,
            "capabilities": capabilities,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=capability_changes_between_documents,
        required=True,
    )

    capability_issue = next(
        issue
        for issue in report["issues"]
        if issue["code"] == "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT"
    )
    assert capability_issue["severity"] == "error"
    assert capability_issue["details"]["provider"] == "powerpoint-com"
    assert capability_issue["details"]["required_capability"] == ("slide_object_bounds")
    assert report["reference"]["issues"] == [capability_issue]


def test_source_slide_object_measurement_failure_is_fail_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def source_failure_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": "powerpoint-com",
            "available": True,
            "capabilities": ["shape_text_bounds", "slide_object_bounds"],
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "translated",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "translated",
                                    "bound_left_pt": 0.0,
                                    "bound_top_pt": 0.0,
                                    "bound_width_pt": 80.0,
                                    "bound_height_pt": 10.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        if document.name == "source.pptx":
            result["slide_object_errors"] = [
                {
                    "slide": 1,
                    "capability": "slide_object_bounds",
                    "reason": "could not enumerate source slide shapes",
                }
            ]
        else:
            result["slide_objects"] = [
                {
                    "slide": 1,
                    "scope": "slide",
                    "shape_id": "9",
                    "object_identity": "slide:1:shape:9",
                    "object_kind": "image",
                    "name": "Local image",
                    "shape_type": 13,
                    "left_pt": 50.0,
                    "top_pt": 0.0,
                    "width_pt": 30.0,
                    "height_pt": 10.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": 0.01,
                }
            ]
        return result

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=source_failure_runner,
        required=True,
    )

    codes = [issue["code"] for issue in report["issues"]]
    assert codes == ["PPTX_RENDERED_SLIDE_OBJECT_MEASUREMENT_FAILED"]
    assert report["issues"][0]["slide"] == 1
    assert (
        report["issues"][0]["details"]["reason"]
        == "could not enumerate source slide shapes"
    )


def test_source_unstable_slide_object_identity_is_fail_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def unstable_source_identity_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "powerpoint-com",
            "available": True,
            "capabilities": ["shape_text_bounds", "slide_object_bounds"],
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                }
            ],
            "slide_objects": [
                {
                    "slide": 1,
                    "scope": "slide",
                    "shape_id": "9",
                    "object_identity": "slide:1:shape:9",
                    "identity_stable": document.name != "source.pptx",
                    "object_kind": "image",
                    "shape_type": 13,
                    "left_pt": 50.0,
                    "top_pt": 0.0,
                    "width_pt": 30.0,
                    "height_pt": 10.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": 0.01,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=unstable_source_identity_runner,
        required=True,
    )

    assert [issue["code"] for issue in report["issues"]] == [
        "PPTX_RENDERED_SLIDE_OBJECT_MEASUREMENT_FAILED"
    ]
    assert "identity must be stable" in report["issues"][0]["details"]["reason"]


def test_compare_fails_closed_when_reference_and_candidate_providers_differ(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def provider_changes_between_documents(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": (
                "powerpoint-com"
                if document.name == "source.pptx"
                else "powerpoint-com-secondary"
            ),
            "provider_version": "16.0",
            "available": True,
            "capabilities": ["shape_text_bounds", "slide_object_bounds"],
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=provider_changes_between_documents,
        required=True,
    )

    assert [issue["code"] for issue in report["issues"]] == [
        "PPTX_RENDERED_LAYOUT_PROVIDER_MISMATCH"
    ]
    details = report["issues"][0]["details"]
    assert details["reference_provider"] == "powerpoint-com"
    assert details["candidate_provider"] == "powerpoint-com-secondary"


def test_compare_fails_closed_when_provider_capabilities_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def capabilities_change_between_documents(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        capabilities = ["shape_text_bounds", "slide_object_bounds"]
        if document.name == "candidate.pptx":
            capabilities.append("rendered_text_lines")
        return {
            "provider": "powerpoint-com",
            "provider_version": "16.0",
            "available": True,
            "capabilities": capabilities,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 80.0,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=capabilities_change_between_documents,
        required=True,
    )

    assert [issue["code"] for issue in report["issues"]] == [
        "PPTX_RENDERED_LAYOUT_CAPABILITY_MISMATCH"
    ]
    details = report["issues"][0]["details"]
    assert details["reference_capabilities"] == [
        "shape_text_bounds",
        "slide_object_bounds",
    ]
    assert details["candidate_capabilities"] == [
        "rendered_text_lines",
        "shape_text_bounds",
        "slide_object_bounds",
    ]


def test_existing_line_violation_and_hard_break_are_suppressed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def same_violation_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 100.0,
                    "bound_height_pt": 40.0,
                    "available_width_pt": 200.0,
                    "available_height_pt": 100.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "中文\r，硬换行，自动换行",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "中文\r",
                                    "bound_left_pt": 10.0,
                                    "bound_top_pt": 10.0,
                                    "bound_width_pt": 40.0,
                                    "bound_height_pt": 12.0,
                                    "ends_with_paragraph_break": True,
                                    "ends_with_manual_break": False,
                                },
                                {
                                    "line_index": 2,
                                    "text": "，硬换行",
                                    "bound_left_pt": 10.0,
                                    "bound_top_pt": 22.0,
                                    "bound_width_pt": 60.0,
                                    "bound_height_pt": 12.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                },
                                {
                                    "line_index": 3,
                                    "text": "，自动换行",
                                    "bound_left_pt": 10.0,
                                    "bound_top_pt": 34.0,
                                    "bound_width_pt": 70.0,
                                    "bound_height_pt": 12.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                },
                            ],
                        }
                    ],
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=same_violation_runner,
    )

    assert report["issues"] == []


@pytest.mark.parametrize(("candidate_width", "expected"), [(62.0, 0), (70.0, 1)])
def test_existing_overlap_requires_material_worsening(
    tmp_path: Path,
    candidate_width: float,
    expected: int,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def existing_overlap_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        width = candidate_width if document.name == "candidate.pptx" else 60.0
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": width,
                    "bound_height_pt": 10.0,
                    "available_width_pt": 100.0,
                    "available_height_pt": 20.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "x",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "x",
                                    "bound_left_pt": 0.0,
                                    "bound_top_pt": 0.0,
                                    "bound_width_pt": width,
                                    "bound_height_pt": 10.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                },
                            ],
                        },
                    ],
                }
            ],
            "inherited_objects": [
                {
                    "slide": 1,
                    "scope": "master",
                    "shape_id": "10",
                    "name": "Watermark",
                    "shape_type": 13,
                    "left_pt": 50.0,
                    "top_pt": 0.0,
                    "width_pt": 50.0,
                    "height_pt": 10.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": 0.01,
                },
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=existing_overlap_runner,
    )

    assert len(report["issues"]) == expected


@pytest.mark.parametrize(
    ("shape_type", "area_ratio", "expected_issue_count"),
    [(13, 0.9, 1), (14, 0.01, 0)],
)
def test_overlap_keeps_large_inherited_images_but_ignores_placeholders(
    tmp_path: Path,
    shape_type: int,
    area_ratio: float,
    expected_issue_count: int,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def decorative_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        left = 10.0 if translated else 200.0
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 100.0,
                    "bound_height_pt": 20.0,
                    "available_width_pt": 200.0,
                    "available_height_pt": 50.0,
                    "text_regions": [
                        {
                            "region_id": "shape",
                            "kind": "shape",
                            "text": "x",
                            "rendered_lines": [
                                {
                                    "line_index": 1,
                                    "text": "x",
                                    "bound_left_pt": 0.0,
                                    "bound_top_pt": 0.0,
                                    "bound_width_pt": 100.0,
                                    "bound_height_pt": 20.0,
                                    "ends_with_paragraph_break": False,
                                    "ends_with_manual_break": False,
                                },
                            ],
                        },
                    ],
                }
            ],
            "inherited_objects": [
                {
                    "slide": 1,
                    "scope": "master",
                    "shape_id": "10",
                    "name": "Inherited",
                    "shape_type": shape_type,
                    "left_pt": left,
                    "top_pt": 0.0,
                    "width_pt": 100.0,
                    "height_pt": 20.0,
                    "visible": True,
                    "has_text_content": False,
                    "area_ratio": area_ratio,
                },
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=decorative_runner,
    )

    overlap_issues = [
        issue
        for issue in report["issues"]
        if issue["code"] == "PPTX_RENDERED_TEXT_OBJECT_OVERLAP"
    ]
    assert len(overlap_issues) == expected_issue_count


def test_table_restore_hint_requires_unchanged_coordinates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def moved_table_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        translated = document.name == "candidate.pptx"
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "is_table": True,
                    "table_frame_left_pt": 122.0 if translated else 120.0,
                    "table_frame_top_pt": 90.0,
                    "table_frame_width_pt": 600.0,
                    "table_frame_height_pt": 180.0 if translated else 200.0,
                    "table_row_heights_pt": (
                        [80.0, 100.0] if translated else [100.0, 100.0]
                    ),
                }
            ],
        }

    report = compare_pptx_rendered_layout(
        source,
        candidate,
        [{"slide": 1, "shape_id": "2"}],
        runner=moved_table_runner,
    )

    details = report["issues"][0]["details"]
    assert details["table_coordinates_unchanged"] is False
    assert details["automatic_row_height_restore_safe"] is False
    assert "repair_hint" not in details


def test_adapter_returns_powerpoint_measurements_in_audit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def passing_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **targets[0],
                    "bound_width_pt": 300.0,
                    "bound_height_pt": 80.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
            ],
        }

    result = execute_pptx_repair_adapter(
        source,
        candidate,
        output,
        actions=[
            {
                "action": "adjust_margins",
                "slide": 1,
                "shape_id": "2",
                "margin_scale": 0.9,
            }
        ],
        layout_runner=passing_runner,
    )

    rendered = result["rendered_layout_qa"]
    assert rendered["provider"] == "fake-powerpoint"
    assert rendered["available"] is True
    assert rendered["measurements"][0]["overflow"] is False
    assert rendered["issues"] == []
    assert result["audit"]["rendered_layout_qa"] == rendered
    assert output.is_file()


def test_adapter_fails_closed_when_required_provider_is_unavailable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    def unavailable_runner(
        _document: Path,
        _targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "fake-powerpoint",
            "available": False,
            "measurements": [],
            "unavailable_reason": "Office is not installed",
        }

    with pytest.raises(
        PptxRepairAdapterError,
        match="PPTX_RENDERED_LAYOUT_UNAVAILABLE",
    ):
        execute_pptx_repair_adapter(
            source,
            candidate,
            output,
            actions=[
                {
                    "action": "set_font_size",
                    "slide": 1,
                    "shape_id": "2",
                    "font_size_pt": 10,
                }
            ],
            layout_runner=unavailable_runner,
            rendered_layout_required=True,
        )

    assert not output.exists()


def test_qa_agent_uses_rendered_layout_for_baseline_and_verification(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    initial_candidate = candidate.resolve()
    measured_documents: list[Path] = []

    def stateful_runner(
        document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        resolved = document.resolve()
        measured_documents.append(resolved)
        overflow = resolved == initial_candidate
        return {
            "provider": "fake-powerpoint",
            "available": True,
            "measurements": [
                {
                    **target,
                    "bound_width_pt": 410.0 if overflow else 300.0,
                    "bound_height_pt": 120.0 if overflow else 80.0,
                    "available_width_pt": 400.0,
                    "available_height_pt": 100.0,
                }
                for target in targets
            ],
        }

    report = run_qa_repair_agent(
        QaRepairSpec(
            job_id="rendered-layout-agent",
            source_document=source,
            candidate_document=candidate,
            work_dir=tmp_path / "work",
            max_rounds=1,
            rendered_layout_required=True,
        ),
        pptx_layout_runner=stateful_runner,
    )

    assert report.status == QaAgentState.COMPLETED
    assert report.rounds[0]["before_issues"][0]["code"] == (
        "PPTX_RENDERED_TEXT_OVERFLOW"
    )
    assert report.rounds[0]["prepared_actions"][0]["action"] == (
        "resolve_text_overflow"
    )
    assert report.rounds[0]["prepared_actions"][0]["rendered_load_ratio"] == 1.2
    assert initial_candidate in measured_documents
    assert report.accepted_document in measured_documents
