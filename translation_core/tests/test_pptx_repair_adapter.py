from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import pytest
from lxml import etree

from format_translator.pptx_engine import (
    NS,
    UnsupportedPptxRepairError,
    extract_translation_blocks,
    execute_pptx_repairs,
    run_pptx_qa,
    structure_snapshot,
)
from format_translator.pptx_repair_adapter import (
    PptxRepairAdapterError,
    _new_or_worsened_rendered_errors,
    _rendered_regression_recovery_actions,
    adapt_pptx_repairs,
    execute_pptx_repair_adapter,
)
from test_pptx_engine import (
    SLIDE_HEIGHT,
    SLIDE_WIDTH,
    SOURCE_TEXT,
    _write_table_pptx,
    _write_pptx,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _shape(path: Path, shape_id: str) -> dict:
    snapshot = structure_snapshot(path)
    return next(
        shape
        for shape in snapshot["slides"][0]["shapes"]
        if shape["shape_id"] == shape_id
    )


def _rewrite_slide(path: Path, replace: Callable[[str], str]) -> None:
    with zipfile.ZipFile(path) as package:
        parts = {
            info.filename: (info, package.read(info)) for info in package.infolist()
        }
    info, slide = parts["ppt/slides/slide1.xml"]
    parts["ppt/slides/slide1.xml"] = (
        info,
        replace(slide.decode("utf-8")).encode("utf-8"),
    )
    with zipfile.ZipFile(path, "w") as package:
        for part_info, data in parts.values():
            package.writestr(part_info, data)


def _replace_primary_shape(path: Path, replacement: str) -> None:
    def replace(xml: str) -> str:
        start = xml.index("    <p:sp>\n")
        end = xml.index("    </p:sp>", start) + len("    </p:sp>")
        return f"{xml[:start]}{replacement}{xml[end:]}"

    _rewrite_slide(path, replace)


def _effective_autofit_font_size(path: Path) -> float:
    drawing = "http://schemas.openxmlformats.org/drawingml/2006/main"
    with zipfile.ZipFile(path) as package:
        root = ElementTree.fromstring(package.read("ppt/slides/slide1.xml"))
    autofit = root.find(f".//{{{drawing}}}normAutofit")
    assert autofit is not None
    scale = int(autofit.get("fontScale", "100000")) / 100_000
    sizes = [
        int(node.get("sz", "0")) / 100
        for node in root.findall(f".//{{{drawing}}}rPr")
        if node.get("sz") is not None
    ]
    assert sizes
    return min(sizes) * scale


def _rendered_table_geometry_issue(
    *,
    row_deltas: list[float] | None = None,
    hint_rows: list[dict[str, object]] | None = None,
    coordinates_unchanged: bool = True,
) -> dict[str, object]:
    deltas = row_deltas or [0.0, -10.0, -4.0]
    rows = (
        hint_rows
        if hint_rows is not None
        else [
            {"row_index": 2, "height_pt": 30.0},
            {"row_index": 3, "height_pt": 24.0},
        ]
    )
    return {
        "code": "PPTX_RENDERED_TABLE_GEOMETRY_CHANGED",
        "message": "PowerPoint recalculated table rows.",
        "severity": "error",
        "slide": 1,
        "details": {
            "shape_id": "2",
            "row_count_changed": False,
            "reference_row_count": 3,
            "candidate_row_count": 3,
            "reference_row_heights_pt": [20.0, 30.0, 24.0],
            "candidate_row_heights_pt": [20.0, 20.0, 20.0],
            "row_height_deltas_pt": deltas,
            "row_height_tolerances_pt": [1.0, 1.0, 1.0],
            "changed_row_indices": [
                index
                for index, (delta, tolerance) in enumerate(
                    zip(deltas, [1.0, 1.0, 1.0]),
                    start=1,
                )
                if abs(delta) > tolerance
            ],
            "table_coordinates_unchanged": coordinates_unchanged,
            "automatic_row_height_restore_safe": (
                coordinates_unchanged
                and bool(
                    [
                        delta
                        for delta, tolerance in zip(
                            deltas,
                            [1.0, 1.0, 1.0],
                        )
                        if delta < -tolerance
                    ]
                )
                and not any(delta > 0 for delta in deltas)
            ),
            "repair_hint": {
                "action": "restore_table_row_heights",
                "action_type": "restore_table_row_heights",
                "slide": 1,
                "shape_id": "2",
                "rows": rows,
                "automatic": True,
            },
        },
    }


def _rendered_overflow_issue(
    *,
    rendered_load_ratio: float = 1.4,
    slide: int = 1,
    shape_id: str = "2",
) -> dict[str, object]:
    return {
        "code": "PPTX_RENDERED_TEXT_OVERFLOW",
        "message": "PowerPoint measured translated text overflow.",
        "severity": "error",
        "slide": slide,
        "details": {
            "shape_id": shape_id,
            "rendered_load_ratio": rendered_load_ratio,
            "provider": "powerpoint-com",
            "repair_hint": {
                "action": "resolve_text_overflow",
                "action_type": "resolve_text_overflow",
                "slide": slide,
                "shape_id": shape_id,
                "rendered_load_ratio": rendered_load_ratio,
                "automatic": True,
            },
        },
    }


def _rendered_object_overlap_issue(
    *,
    reference_area: float = 100.0,
    candidate_area: float = 225.0,
    tolerance: float = 25.0,
) -> dict[str, object]:
    return {
        "code": "PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
        "message": "Translated text overlaps a master object.",
        "severity": "error",
        "slide": 1,
        "details": {
            "shape_id": "2",
            "region_id": "shape",
            "kind": "shape",
            "reference_overlap_area_pt2": reference_area,
            "candidate_overlap_area_pt2": candidate_area,
            "worsening_tolerance_pt2": tolerance,
            "provider": "powerpoint-com",
        },
    }


RENDERED_AUTHORITY_FATAL_CODES = (
    "PPTX_RENDERED_LAYOUT_UNAVAILABLE",
    "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT",
    "PPTX_RENDERED_LAYOUT_CAPABILITY_MISMATCH",
    "PPTX_RENDERED_LAYOUT_PROVIDER_MISMATCH",
    "PPTX_RENDERED_LAYOUT_MEASUREMENT_FAILED",
    "PPTX_RENDERED_LAYOUT_TARGET_NOT_MEASURED",
    "PPTX_RENDERED_SLIDE_OBJECT_MEASUREMENT_FAILED",
    "PPTX_RENDERED_TABLE_GEOMETRY_CAPABILITY_INSUFFICIENT",
)


@pytest.mark.parametrize("code", RENDERED_AUTHORITY_FATAL_CODES)
def test_rendered_residual_gate_always_blocks_persistent_authority_failure(
    code: str,
) -> None:
    issue = {
        "code": code,
        "severity": "error",
        "message": "Rendered comparison is not authoritative.",
        "details": {"provider": "powerpoint-com"},
    }

    assert _new_or_worsened_rendered_errors([issue], [issue]) == [issue]


@pytest.mark.parametrize("code", RENDERED_AUTHORITY_FATAL_CODES)
def test_rendered_residual_gate_blocks_after_only_authority_failure(
    code: str,
) -> None:
    issue = {
        "code": code,
        "severity": "error",
        "message": "Rendered comparison became non-authoritative.",
        "details": {"provider": "powerpoint-com"},
    }

    assert _new_or_worsened_rendered_errors([], [issue]) == [issue]


def test_rendered_residual_gate_blocks_new_shape_on_improving_slide() -> None:
    baseline = [
        _rendered_overflow_issue(shape_id=shape_id)
        for shape_id in ("2", "3", "4")
    ]
    new_overlap = {
        **_rendered_object_overlap_issue(),
        "details": {
            **_rendered_object_overlap_issue()["details"],
            "shape_id": "99",
        },
    }

    blocking = _new_or_worsened_rendered_errors(
        baseline,
        [_rendered_overflow_issue(shape_id="2"), new_overlap],
    )

    assert blocking == [new_overlap]


def test_rendered_residual_identity_uses_top_level_shape_id_fallback() -> None:
    def issue(shape_id: str) -> dict[str, object]:
        return {
            "code": "PPTX_RENDERED_TEXT_OVERFLOW",
            "severity": "error",
            "slide": 1,
            "shape_id": shape_id,
            "details": {"rendered_load_ratio": 1.4},
        }

    baseline = [issue("2"), issue("3"), issue("4")]
    new_shape = issue("99")

    blocking = _new_or_worsened_rendered_errors(
        baseline,
        [issue("2"), new_shape],
    )

    assert blocking == [new_shape]


def test_rendered_residual_gate_blocks_new_code_for_existing_shape() -> None:
    baseline = [
        _rendered_overflow_issue(shape_id=shape_id)
        for shape_id in ("2", "3", "4")
    ]
    new_overlap = _rendered_object_overlap_issue()

    blocking = _new_or_worsened_rendered_errors(
        baseline,
        [_rendered_overflow_issue(shape_id="2"), new_overlap],
    )

    assert blocking == [new_overlap]


def test_rendered_residual_gate_blocks_worsened_identity_during_total_improvement(
) -> None:
    baseline = [
        _rendered_overflow_issue(rendered_load_ratio=1.4, shape_id=shape_id)
        for shape_id in ("2", "3", "4")
    ]
    worsened = _rendered_overflow_issue(
        rendered_load_ratio=1.6,
        shape_id="2",
    )

    blocking = _new_or_worsened_rendered_errors(baseline, [worsened])

    assert blocking == [worsened]


def test_rendered_residual_gate_allows_improved_matching_identity() -> None:
    baseline = [_rendered_overflow_issue(rendered_load_ratio=1.6, shape_id="2")]
    improved = _rendered_overflow_issue(
        rendered_load_ratio=1.3,
        shape_id="2",
    )

    blocking = _new_or_worsened_rendered_errors(baseline, [improved])

    assert blocking == []


def test_rendered_residual_gate_blocks_worsened_table_geometry_identity() -> None:
    baseline = [_rendered_table_geometry_issue(row_deltas=[0.0, -10.0, -4.0])]
    worsened = _rendered_table_geometry_issue(
        row_deltas=[0.0, -12.0, -4.0],
    )

    blocking = _new_or_worsened_rendered_errors(baseline, [worsened])

    assert blocking == [worsened]


@pytest.mark.parametrize(
    "after_deltas",
    (
        [0.0, -10.0, -4.0],
        [0.0, -6.0, -2.0],
    ),
    ids=("unchanged", "improved"),
)
def test_rendered_residual_gate_allows_non_worsened_table_geometry_identity(
    after_deltas: list[float],
) -> None:
    baseline = [_rendered_table_geometry_issue(row_deltas=[0.0, -10.0, -4.0])]
    after = _rendered_table_geometry_issue(row_deltas=after_deltas)

    assert _new_or_worsened_rendered_errors(baseline, [after]) == []


def test_restore_shape_coordinates_preserves_input_and_audits_before_after(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    damaged = tmp_path / "damaged.pptx"
    repaired = tmp_path / "repaired.pptx"
    _write_pptx(source, shape_x=500_000)
    _write_pptx(damaged, shape_x=2_500_000)
    damaged_hash = _sha256(damaged)

    report = execute_pptx_repairs(
        damaged,
        repaired,
        [
            {
                "action": "restore_shape_coordinates",
                "slide": 1,
                "shape_id": "2",
                "source_pptx": str(source),
            }
        ],
    )

    assert _shape(repaired, "2")["bbox_emu"] == _shape(source, "2")["bbox_emu"]
    assert _sha256(damaged) == damaged_hash
    audit = report["audit"]["actions"][0]
    assert audit["before"]["bbox_emu"] == [2_500_000, 500_000, 4_000_000, 1_000_000]
    assert audit["after"]["bbox_emu"] == [500_000, 500_000, 4_000_000, 1_000_000]
    assert audit["before"] != audit["after"]


def test_set_shape_bounds_clamps_target_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    repaired = tmp_path / "repaired.pptx"
    _write_pptx(source, second_shape=True)
    secondary_before = _shape(source, "3")

    report = execute_pptx_repairs(
        source,
        repaired,
        [
            {
                "action": "set_shape_bounds",
                "slide": 1,
                "shape_id": "2",
                "x": -50_000,
                "y": -40_000,
                "width": 3_000_000,
                "height": 1_000_000,
                "clamp_to_slide": True,
            }
        ],
    )

    target = _shape(repaired, "2")
    secondary_after = _shape(repaired, "3")
    assert target["bbox_emu"] == [0, 0, 3_000_000, 1_000_000]
    assert secondary_after == secondary_before
    assert (
        report["audit"]["actions"][0]["before"]["bbox_emu"]
        != (report["audit"]["actions"][0]["after"]["bbox_emu"])
    )


def test_absolute_and_proportional_font_repairs_obey_minimum(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    proportional = tmp_path / "proportional.pptx"
    absolute = tmp_path / "absolute.pptx"
    rejected = tmp_path / "rejected.pptx"
    _write_pptx(source, font_size_hundredths=2_000)

    execute_pptx_repairs(
        source,
        proportional,
        [
            {
                "action": "adjust_font_size",
                "slide": 1,
                "shape_id": "2",
                "scale": 0.85,
                "minimum_font_size_pt": 8,
            }
        ],
    )
    execute_pptx_repairs(
        source,
        absolute,
        [
            {
                "action": "set_font_size",
                "slide": 1,
                "shape_id": "2",
                "font_size_pt": 14,
                "minimum_font_size_pt": 8,
            }
        ],
    )

    assert _shape(proportional, "2")["font_sizes_pt"] == [17.0]
    assert _shape(absolute, "2")["font_sizes_pt"] == [14.0]
    with pytest.raises(UnsupportedPptxRepairError):
        execute_pptx_repairs(
            source,
            rejected,
            [
                {
                    "action": "set_font_size",
                    "slide": 1,
                    "shape_id": "2",
                    "font_size_pt": 7,
                    "minimum_font_size_pt": 8,
                }
            ],
        )
    assert not rejected.exists()


def test_adapter_normalizes_absolute_font_size_action(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        actions=[
            {
                "action": "set_font_size",
                "slide": 1,
                "shape_id": "2",
                "font_size_pt": 14,
                "min_font_size_pt": 8,
            }
        ],
    )

    assert adaptation.rejected_actions == []
    assert adaptation.normalized_actions == [
        {
            "action": "set_font_size",
            "slide": 1,
            "shape_id": "2",
            "font_size_pt": 14.0,
            "min_font_size_pt": 8.0,
            "adapter_source": "plan[0]",
        }
    ]


def test_strict_layout_rejects_format_mutations_and_keeps_text_replacement(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    block = extract_translation_blocks(candidate)[0]

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        strict_layout=True,
        actions=[
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
            },
            {
                "action": "set_font_size",
                "slide": 1,
                "shape_id": "2",
                "font_size_pt": 14,
            },
            {
                "action": "restore_shape_coordinates",
                "slide": 1,
                "shape_id": "2",
            },
            {
                "action": "set_textbox_margins",
                "slide": 1,
                "shape_id": "2",
            },
            {
                "action": "enable_autofit",
                "slide": 1,
                "shape_id": "2",
            },
            {
                "action": "replace_translation",
                "block_id": block.block_id,
                "text": "Translated text only.",
            },
        ],
    )

    assert [action["action"] for action in adaptation.normalized_actions] == [
        "replace_translation"
    ]
    assert {rejection["action"] for rejection in adaptation.rejected_actions} == {
        "resolve_text_overflow",
        "set_font_size",
        "restore_shape_coordinates",
        "adjust_margins",
        "set_autofit",
    }
    assert {rejection["code"] for rejection in adaptation.rejected_actions} == {
        "STRICT_LAYOUT_ACTION_FORBIDDEN"
    }
    assert adaptation.strict_layout is True


def test_strict_layout_allows_only_trusted_rendered_overflow_issue_repair(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[_rendered_overflow_issue(rendered_load_ratio=1.4)],
        actions=[
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "rendered_load_ratio": 9.0,
            }
        ],
        strict_layout=True,
    )

    assert adaptation.rejected_actions[0]["code"] == (
        "STRICT_LAYOUT_ACTION_FORBIDDEN"
    )
    assert len(adaptation.normalized_actions) == 1
    action = adaptation.normalized_actions[0]
    assert action["action"] == "resolve_text_overflow"
    assert action["rendered_load_ratio"] == pytest.approx(1.4)
    assert action["allow_expand"] is True
    assert action["trusted_rendered_evidence"] is True
    assert action["rendered_evidence"]["issue_codes"] == [
        "PPTX_RENDERED_TEXT_OVERFLOW"
    ]
    assert action["adapter_source"] == "issue[0]"


def test_compatible_overflow_findings_merge_per_shape_and_keep_strongest_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    capacity_issue = {
        "code": "PPTX_TEXT_CAPACITY_RISK",
        "message": "Estimated capacity is high.",
        "severity": "error",
        "slide": 1,
        "details": {"shape_id": "2"},
    }

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[capacity_issue, _rendered_overflow_issue(rendered_load_ratio=1.7)],
    )

    assert len(adaptation.normalized_actions) == 1
    action = adaptation.normalized_actions[0]
    assert action["action"] == "resolve_text_overflow"
    assert action["rendered_load_ratio"] == pytest.approx(1.7)
    assert action["adapter_sources"] == ["issue[0]", "issue[1]"]
    assert action["trusted_rendered_evidence"] is True
    assert action["rendered_evidence"]["issue_codes"] == [
        "PPTX_RENDERED_TEXT_OVERFLOW"
    ]


def test_strict_action_limit_prioritizes_the_most_repairable_rendered_shape(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source, second_shape=True)
    _write_pptx(candidate, second_shape=True)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[
            _rendered_overflow_issue(
                shape_id="2",
                rendered_load_ratio=5.0,
            ),
            _rendered_overflow_issue(
                shape_id="3",
                rendered_load_ratio=1.1,
            ),
        ],
        max_actions=1,
        strict_layout=True,
    )

    assert [action["shape_id"] for action in adaptation.normalized_actions] == [
        "3"
    ]
    assert adaptation.normalized_actions[0]["adapter_source"] == "issue[1]"
    assert adaptation.rejected_actions[0]["code"] == "ACTION_LIMIT_EXCEEDED"
    assert adaptation.rejected_actions[0]["source"] == "issue[0]"


def test_same_shape_merge_preserves_bounded_per_issue_rendered_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    overflow = _rendered_overflow_issue(rendered_load_ratio=1.7)
    overflow["details"].update(
        {
            "bound_left_pt": 10.0,
            "bound_top_pt": 20.0,
            "bound_width_pt": 300.0,
            "bound_height_pt": 80.0,
        }
    )
    overlap = _rendered_object_overlap_issue()
    overlap["details"].update(
        {
            "object": {
                "scope": "master",
                "shape_id": "20487",
                "left_pt": 30.0,
                "top_pt": 18.0,
                "width_pt": 648.0,
                "height_pt": 48.0,
            },
            "overlaps": [
                {
                    "line_index": 1,
                    "overlap_area_pt2": 225.0,
                }
            ],
        }
    )

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[overflow, overlap],
        strict_layout=True,
    )

    assert len(adaptation.normalized_actions) == 1
    action = adaptation.normalized_actions[0]
    evidence = action["rendered_evidence"]
    assert evidence["issue_codes"] == [
        "PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
        "PPTX_RENDERED_TEXT_OVERFLOW",
    ]
    assert [item["code"] for item in evidence["evidence_items"]] == [
        "PPTX_RENDERED_TEXT_OVERFLOW",
        "PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
    ]
    assert evidence["evidence_items"][0] == {
        "code": "PPTX_RENDERED_TEXT_OVERFLOW",
        "severity": "error",
        "provider": "powerpoint-com",
        "measurements": {
            "rendered_load_ratio": 1.7,
            "rendered_target_load_ratio": 0.92,
        },
        "bounds": {
            "bound_left_pt": 10.0,
            "bound_top_pt": 20.0,
            "bound_width_pt": 300.0,
            "bound_height_pt": 80.0,
        },
    }
    assert evidence["evidence_items"][1]["severity"] == "error"
    assert evidence["evidence_items"][1]["provider"] == "powerpoint-com"
    assert evidence["evidence_items"][1]["measurements"] == {
        "reference_overlap_area_pt2": 100.0,
        "candidate_overlap_area_pt2": 225.0,
        "allowed_overlap_area_pt2": 125.0,
    }
    assert evidence["evidence_items"][1]["bounds"]["object"]["shape_id"] == (
        "20487"
    )
    assert evidence["evidence_items"][1]["bounds"]["overlaps"] == [
        {"line_index": 1, "overlap_area_pt2": 225.0}
    ]


def test_same_shape_rendered_evidence_list_is_bounded(tmp_path: Path) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[
            _rendered_overflow_issue(rendered_load_ratio=1.1 + index / 10)
            for index in range(10)
        ],
        strict_layout=True,
    )

    evidence = adaptation.normalized_actions[0]["rendered_evidence"]
    assert len(evidence["evidence_items"]) == 8
    assert evidence["truncated_evidence_count"] == 2


def test_rendered_overflow_targets_source_relative_safe_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    issue = _rendered_overflow_issue(rendered_load_ratio=4.0)
    issue["details"].update(
        {
            "bound_width_pt": 200.0,
            "bound_height_pt": 400.0,
            "available_width_pt": 100.0,
            "available_height_pt": 100.0,
            "reference_overflow_width_pt": 50.0,
            "reference_overflow_height_pt": 200.0,
            "width_tolerance_pt": 2.0,
            "height_tolerance_pt": 2.0,
        }
    )

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[issue],
        strict_layout=True,
    )

    action = adaptation.normalized_actions[0]
    assert action["rendered_load_ratio"] == 4.0
    assert action["rendered_target_load_ratio"] == pytest.approx(3.02)
    assert action["autofit_font_scale"] == 98_000
    assert action["preserve_autofit_mode"] is True


def test_engine_accepts_source_relative_rendered_target_above_one(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, font_size_hundredths=2_000)

    report = execute_pptx_repairs(
        source,
        output,
        [
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "rendered_load_ratio": 4.0,
                "rendered_target_load_ratio": 3.0,
                "target_load_ratio": 0.92,
                "autofit_font_scale": 98_000,
                "allow_expand": False,
            }
        ],
    )

    action = report["audit"]["actions"][0]
    assert action["resolved"] is True
    assert action["effective_target_load_ratio"] == 3.0
    assert action["rendered_load_ratio_estimate_after"] <= 3.001


def test_rendered_repair_can_preserve_existing_autofit_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, font_size_hundredths=2_000)

    report = execute_pptx_repairs(
        source,
        output,
        [
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "rendered_load_ratio": 1.2,
                "rendered_target_load_ratio": 1.0,
                "target_load_ratio": 0.92,
                "preserve_autofit_mode": True,
                "allow_expand": False,
            }
        ],
    )

    action = report["audit"]["actions"][0]
    assert action["steps"][0]["action"] == "set_autofit"
    assert action["steps"][0]["status"] == "PRESERVED"
    assert action["before"]["body_properties"]["autofit"] == "none"
    assert action["after"]["body_properties"]["autofit"] == "none"
    assert action["before"]["font_sizes_pt"] != action["after"]["font_sizes_pt"]


def test_rendered_object_overlap_uses_text_only_repair_when_boundary_is_measured(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[_rendered_object_overlap_issue()],
        strict_layout=True,
    )

    assert adaptation.rejected_actions == []
    action = adaptation.normalized_actions[0]
    assert action["action"] == "resolve_text_overflow"
    assert action["allow_expand"] is False
    assert action["margin_scale"] == 1.0
    assert action["rendered_load_ratio"] == pytest.approx((225.0 / 125.0) ** 0.5)
    assert action["rendered_target_load_ratio"] == 1.0
    assert action["trusted_rendered_evidence"] is True
    assert action["rendered_evidence"]["issue_codes"] == [
        "PPTX_RENDERED_TEXT_OBJECT_OVERLAP"
    ]


def test_rendered_object_overlap_without_safe_boundary_remains_manual_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)
    issue = _rendered_object_overlap_issue(candidate_area=225.0, tolerance=0.0)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[issue],
        strict_layout=True,
    )

    assert adaptation.normalized_actions == []
    assert adaptation.rejected_actions[0]["code"] == "MANUAL_REVIEW_REQUIRED"


def test_adapter_keeps_safe_shape_repairs_when_another_shape_hits_font_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, second_shape=True)
    _write_pptx(candidate, second_shape=True)
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.run_pptx_qa",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.compare_pptx_rendered_layout",
        lambda *_args, **_kwargs: {
            "provider": "fixture-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [],
            "issues": [],
        },
    )

    report = execute_pptx_repair_adapter(
        source,
        candidate,
        output,
        issues=[
            _rendered_overflow_issue(
                rendered_load_ratio=1.05,
                shape_id="2",
            ),
            _rendered_overflow_issue(
                rendered_load_ratio=10.0,
                shape_id="3",
            ),
        ],
        strict_layout=True,
        rendered_layout_required=True,
    )

    assert output.is_file()
    assert len(report["audit"]["engine_result"]["applied"]) == 1
    assert len(report["audit"]["engine_result"]["skipped"]) == 1
    assert report["audit"]["engine_result"]["skipped"][0]["action"] == (
        "resolve_text_overflow"
    )
    assert _shape(output, "2")["font_sizes_pt"] != _shape(candidate, "2")[
        "font_sizes_pt"
    ]
    assert _shape(output, "3")["font_sizes_pt"] == _shape(candidate, "3")[
        "font_sizes_pt"
    ]


def test_strict_adapter_retries_without_actions_that_cause_rendered_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, second_shape=True)
    _write_pptx(candidate, second_shape=True)
    baseline_issues = [
        _rendered_overflow_issue(shape_id="2"),
        _rendered_overflow_issue(shape_id="3"),
    ]
    introduced_overlap = {
        **_rendered_object_overlap_issue(),
        "details": {
            **_rendered_object_overlap_issue()["details"],
            "shape_id": "2",
            "object_shape_id": "99",
            "object_identity": "slide:1:shape:99",
        },
    }
    compared_issue_sets = [
        baseline_issues,
        [*baseline_issues, introduced_overlap],
        baseline_issues,
    ]
    executed_shape_ids: list[list[str]] = []

    def fake_compare(*_args: object, **_kwargs: object) -> dict[str, object]:
        issues = compared_issue_sets.pop(0)
        return {
            "provider": "fixture-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [],
            "issues": issues,
        }

    def fake_execute(
        current: Path,
        produced: Path,
        actions: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        executed_shape_ids.append([str(action["shape_id"]) for action in actions])
        produced.write_bytes(current.read_bytes())
        return {
            "applied": list(actions),
            "skipped": [],
            "qa_issues": [],
            "validation": {"valid": True, "errors": []},
            "audit": {"authorized_coordinate_changes": {}},
        }

    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.compare_pptx_rendered_layout",
        fake_compare,
    )
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.execute_pptx_repairs",
        fake_execute,
    )
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.run_pptx_qa",
        lambda *_args, **_kwargs: [],
    )

    report = execute_pptx_repair_adapter(
        source,
        candidate,
        output,
        issues=baseline_issues,
        strict_layout=True,
        rendered_layout_required=True,
    )

    assert output.is_file()
    assert executed_shape_ids == [["2", "3"], ["3"]]
    assert [action["shape_id"] for action in report["normalized_actions"]] == ["3"]
    recovery = report["audit"]["rendered_regression_recovery"]
    assert recovery["attempted"] is True
    assert recovery["initial_blocking_issue_count"] == 1
    assert recovery["excluded_targets"] == [
        {"slide": 1, "shape_id": "2"},
    ]
    assert recovery["remaining_blocking_issue_count"] == 0


def test_strict_adapter_isolates_regressing_replace_translation_by_block_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, second_shape=True)
    _write_pptx(candidate, second_shape=True)
    blocks_by_shape = {
        str(block.context["shape_id"]): block
        for block in extract_translation_blocks(candidate)
    }
    regressing_block = blocks_by_shape["2"]
    safe_block = blocks_by_shape["3"]
    introduced_overlap = {
        **_rendered_object_overlap_issue(),
        "details": {
            **_rendered_object_overlap_issue()["details"],
            "shape_id": "2",
            "object_shape_id": "99",
            "object_identity": "slide:1:shape:99",
        },
    }
    compared_issue_sets = [[], [introduced_overlap], []]
    executed_block_ids: list[list[str]] = []

    def fake_compare(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "provider": "fixture-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [],
            "issues": compared_issue_sets.pop(0),
        }

    def fake_execute(
        current: Path,
        produced: Path,
        actions: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        executed_block_ids.append(
            [str(action["block_id"]) for action in actions]
        )
        produced.write_bytes(current.read_bytes())
        return {
            "applied": list(actions),
            "skipped": [],
            "qa_issues": [],
            "validation": {"valid": True, "errors": []},
            "audit": {"authorized_coordinate_changes": {}},
        }

    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.compare_pptx_rendered_layout",
        fake_compare,
    )
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.execute_pptx_repairs",
        fake_execute,
    )
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.run_pptx_qa",
        lambda *_args, **_kwargs: [],
    )

    report = execute_pptx_repair_adapter(
        source,
        candidate,
        output,
        actions=[
            {
                "action": "replace_translation",
                "block_id": regressing_block.block_id,
                "text": "Regressing translation.",
            },
            {
                "action": "replace_translation",
                "block_id": safe_block.block_id,
                "text": "Safe translation.",
            },
        ],
        strict_layout=True,
        rendered_layout_required=True,
    )

    assert output.is_file()
    assert executed_block_ids == [
        [regressing_block.block_id, safe_block.block_id],
        [safe_block.block_id],
    ]
    assert [action["block_id"] for action in report["normalized_actions"]] == [
        safe_block.block_id,
    ]
    assert report["audit"]["rendered_regression_recovery"][
        "excluded_targets"
    ] == [{"slide": 1, "shape_id": "2"}]


def test_rendered_regression_recovery_fails_closed_for_unknown_block_mapping(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(candidate)

    kept, excluded = _rendered_regression_recovery_actions(
        [
            {
                "action": "replace_translation",
                "block_id": "unknown-block",
                "text": "Translation.",
            }
        ],
        [_rendered_object_overlap_issue()],
        candidate,
    )

    assert kept == []
    assert excluded == []


def test_non_strict_adapter_does_not_reclassify_residual_errors_as_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, second_shape=True)
    _write_pptx(candidate, second_shape=True)
    residual = {
        **_rendered_object_overlap_issue(),
        "details": {
            **_rendered_object_overlap_issue()["details"],
            "shape_id": "2",
        },
    }
    compared_issue_sets = [[], [residual], [residual]]
    execution_count = 0

    def fake_compare(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "provider": "fixture-powerpoint",
            "available": True,
            "capabilities": ["shape_text_bounds"],
            "measurements": [],
            "issues": compared_issue_sets.pop(0),
        }

    def fake_execute(
        current: Path,
        produced: Path,
        actions: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal execution_count
        execution_count += 1
        produced.write_bytes(current.read_bytes())
        return {
            "applied": list(actions),
            "skipped": [],
            "qa_issues": [],
            "validation": {"valid": True, "errors": []},
            "audit": {"authorized_coordinate_changes": {}},
        }

    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.compare_pptx_rendered_layout",
        fake_compare,
    )
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.execute_pptx_repairs",
        fake_execute,
    )
    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.run_pptx_qa",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(PptxRepairAdapterError):
        execute_pptx_repair_adapter(
            source,
            candidate,
            output,
            actions=[
                {"action": "set_autofit", "slide": 1, "shape_id": "2"},
                {"action": "set_autofit", "slide": 1, "shape_id": "3"},
            ],
            rendered_layout_required=True,
            strict_layout=False,
        )

    assert execution_count == 1
    assert output.exists() is False


def test_adapter_synthesizes_table_row_restore_only_from_rendered_issue(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)
    explicit = {
        "action": "restore_table_row_heights",
        "slide": 1,
        "shape_id": "2",
        "rows": [{"row_index": 2, "height_pt": 30.0}],
    }

    explicit_adaptation = adapt_pptx_repairs(
        source,
        candidate,
        actions=[explicit],
    )
    issue_adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[_rendered_table_geometry_issue()],
        strict_layout=True,
    )

    assert explicit_adaptation.normalized_actions == []
    assert explicit_adaptation.rejected_actions[0]["code"] == ("SYNTHETIC_ONLY_ACTION")
    assert issue_adaptation.rejected_actions == []
    assert issue_adaptation.normalized_actions == [
        {
            "action": "restore_table_row_heights",
            "slide": 1,
            "shape_id": "2",
            "rows": [
                {"row_index": 2, "height_pt": 30.0},
                {"row_index": 3, "height_pt": 24.0},
            ],
            "adapter_source": "issue[0]",
        }
    ]
    assert {action["action"] for action in issue_adaptation.normalized_actions} == {
        "restore_table_row_heights"
    }


@pytest.mark.parametrize(
    "hint_rows",
    [
        [],
        [
            {"row_index": 2, "height_pt": 30.0},
            {"row_index": 2, "height_pt": 30.0},
        ],
        [{"row_index": 0, "height_pt": 30.0}],
        [{"row_index": 2, "height_pt": 0.99}],
        [{"row_index": 2, "height_pt": 1000.01}],
    ],
    ids=["empty", "duplicate-row", "zero-row", "height-too-small", "height-too-large"],
)
def test_adapter_rejects_malformed_synthetic_table_row_restore(
    tmp_path: Path,
    hint_rows: list[dict[str, object]],
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[
            _rendered_table_geometry_issue(
                hint_rows=hint_rows,
            )
        ],
    )

    assert adaptation.normalized_actions == []
    assert adaptation.rejected_actions[0]["code"] == ("MANUAL_REVIEW_REQUIRED")


def test_adapter_rejects_table_row_restore_for_non_table_shape(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_pptx(source)
    _write_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[_rendered_table_geometry_issue()],
    )

    assert adaptation.normalized_actions == []
    assert adaptation.rejected_actions[0]["code"] == "INVALID_ACTION"
    assert "table graphic frame" in adaptation.rejected_actions[0]["reason"]


@pytest.mark.parametrize(
    "row_deltas",
    [
        [0.0, 10.0, 4.0],
        [0.0, -10.0, 4.0],
    ],
    ids=["positive", "mixed"],
)
def test_adapter_keeps_positive_or_mixed_table_delta_for_review(
    tmp_path: Path,
    row_deltas: list[float],
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[
            _rendered_table_geometry_issue(
                row_deltas=row_deltas,
            )
        ],
    )

    assert adaptation.normalized_actions == []
    assert adaptation.rejected_actions[0]["code"] == ("MANUAL_REVIEW_REQUIRED")


def test_adapter_keeps_table_coordinate_change_for_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=[
            _rendered_table_geometry_issue(
                coordinates_unchanged=False,
            )
        ],
    )

    assert adaptation.normalized_actions == []
    assert adaptation.rejected_actions[0]["code"] == ("MANUAL_REVIEW_REQUIRED")


def test_table_row_restore_requires_runtime_geometry_capability(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)

    def bounds_only_runner(
        _document: Path,
        targets: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "provider": "bounds-only",
            "available": True,
            "capabilities": ["shape_text_bounds"],
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

    with pytest.raises(PptxRepairAdapterError) as raised:
        execute_pptx_repair_adapter(
            source,
            candidate,
            output,
            issues=[_rendered_table_geometry_issue()],
            layout_runner=bounds_only_runner,
        )

    assert output.exists() is False
    assert raised.value.rendered_layout_qa is not None
    assert any(
        issue["code"] == "PPTX_RENDERED_TABLE_GEOMETRY_CAPABILITY_INSUFFICIENT"
        for issue in raised.value.rendered_layout_qa["issues"]
    )


def test_strict_table_restore_is_not_blocked_by_unrelated_baseline_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)
    unrelated_overflow = {
        "code": "PPTX_RENDERED_TEXT_OVERFLOW",
        "message": "An unrelated text box still overflows.",
        "severity": "error",
        "slide": 15,
        "details": {"shape_id": "9"},
    }

    def persistent_source_qa(*_args: object, **_kwargs: object) -> list[dict]:
        return [unrelated_overflow]

    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.run_pptx_qa",
        persistent_source_qa,
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

    report = execute_pptx_repair_adapter(
        source,
        candidate,
        output,
        issues=[_rendered_table_geometry_issue()],
        actions=[
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
            }
        ],
        layout_runner=table_layout_runner,
        strict_layout=True,
    )

    assert output.is_file()
    assert [action["action"] for action in report["normalized_actions"]] == [
        "restore_table_row_heights"
    ]
    assert report["rejected_actions"][0]["code"] == ("STRICT_LAYOUT_ACTION_FORBIDDEN")
    assert report["audit"]["baseline_source_qa_issues"] == [unrelated_overflow]
    assert report["audit"]["source_qa_issues"] == [unrelated_overflow]
    assert report["audit"]["blocking_source_qa_issues"] == []


def test_strict_relative_qa_still_fails_closed_on_new_layout_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_table_pptx(source)
    _write_table_pptx(candidate)
    unrelated_overflow = {
        "code": "PPTX_RENDERED_TEXT_OVERFLOW",
        "message": "An unrelated text box still overflows.",
        "severity": "error",
        "slide": 15,
        "details": {"shape_id": "9"},
    }
    new_overlap = {
        "code": "PPTX_SUSPECTED_SHAPE_OVERLAP",
        "message": "The repair introduced an overlap.",
        "severity": "error",
        "slide": 1,
        "details": {"shape_id": "2", "other_shape_id": "3"},
    }

    def regressive_source_qa(
        _source: Path,
        checked: Path,
        **_kwargs: object,
    ) -> list[dict[str, object]]:
        return (
            [unrelated_overflow]
            if Path(checked).resolve() == candidate.resolve()
            else [unrelated_overflow, new_overlap]
        )

    monkeypatch.setattr(
        "format_translator.pptx_repair_adapter.run_pptx_qa",
        regressive_source_qa,
    )

    with pytest.raises(PptxRepairAdapterError, match="PPTX_SUSPECTED_SHAPE_OVERLAP"):
        execute_pptx_repair_adapter(
            source,
            candidate,
            output,
            issues=[_rendered_table_geometry_issue()],
            strict_layout=True,
        )

    assert output.exists() is False


def test_text_overflow_cascade_reduces_qa_errors_and_records_steps(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    overflowing = tmp_path / "overflowing.pptx"
    repaired = tmp_path / "repaired.pptx"
    _write_pptx(source)
    _write_pptx(
        overflowing,
        text=SOURCE_TEXT * 10,
        shape_width=1_200_000,
        shape_height=500_000,
    )
    overflowing_hash = _sha256(overflowing)
    before = run_pptx_qa(source, overflowing)

    report = execute_pptx_repairs(
        overflowing,
        repaired,
        [
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "target_load_ratio": 0.92,
                "margin_scale": 0.75,
                "allow_expand": True,
                "minimum_font_size_pt": 8,
            }
        ],
    )
    after = run_pptx_qa(source, repaired)

    before_errors = sum(issue.severity.value == "error" for issue in before)
    after_errors = sum(issue.severity.value == "error" for issue in after)
    assert after_errors < before_errors
    assert _sha256(overflowing) == overflowing_hash
    audit = report["audit"]["actions"][0]
    assert [step["action"] for step in audit["steps"]][:2] == [
        "set_autofit",
        "adjust_margins",
    ]
    assert audit["before"] != audit["after"]


def test_overflow_repair_uses_fractional_vertical_capacity_for_one_line(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(
        source,
        text="Short title",
        shape_height=400_000,
    )

    report = execute_pptx_repairs(
        source,
        output,
        [
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "target_load_ratio": 0.92,
                "allow_expand": False,
                "rendered_load_ratio": 1.485,
            }
        ],
    )

    action = report["audit"]["actions"][0]
    assert action["resolved"] is True
    assert action["after"]["capacity"]["load_ratio"] <= 0.92
    assert any(step["action"] == "adjust_font_size" for step in action["steps"])


def test_overflow_repair_materializes_inherited_text_run_font_size(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(
        source,
        text="Inherited-size title",
        font_size_hundredths=2_800,
        omit_text_run_font_size=True,
    )

    execute_pptx_repairs(
        source,
        output,
        [
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "rendered_load_ratio": 1.5,
                "allow_expand": False,
            }
        ],
    )

    with zipfile.ZipFile(output) as package:
        slide = etree.fromstring(package.read("ppt/slides/slide1.xml"))
    text_run_sizes = slide.xpath(
        "//p:sp[p:nvSpPr/p:cNvPr/@id='2']"
        "/p:txBody/a:p/a:r[a:t[normalize-space()]]/a:rPr/@sz",
        namespaces=NS,
    )
    assert len(text_run_sizes) == 1
    assert 800 <= int(text_run_sizes[0]) < 2_800


def test_adapter_maps_capacity_and_coordinate_issues_and_rejects_unknown_action(
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
    issues = run_pptx_qa(source, candidate)

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        issues=issues,
        actions=[
            {
                "action": "delete_all_document_content",
                "slide": 1,
                "shape_id": "2",
            }
        ],
    )

    by_action = {action["action"]: action for action in adaptation.normalized_actions}
    assert (
        Path(by_action["restore_shape_coordinates"]["source_pptx"]) == source.resolve()
    )
    assert by_action["resolve_text_overflow"]["target_load_ratio"] == 0.92
    assert by_action["resolve_text_overflow"]["allow_expand"] is True
    assert adaptation.rejected_actions[0]["code"] == "UNKNOWN_ACTION"
    assert adaptation.rejected_actions[0]["action"] == ("delete_all_document_content")


def test_adapter_binds_restore_to_trusted_source_instead_of_malicious_reference(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    malicious = tmp_path / "malicious.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, second_shape=True)
    _write_pptx(candidate, shape_x=2_500_000, second_shape=True)
    _write_pptx(malicious, shape_x=6_500_000, second_shape=True)
    action = {
        "action": "restore_shape_coordinates",
        "slide": 1,
        "shape_id": "2",
        "source_pptx": str(malicious),
    }

    adaptation = adapt_pptx_repairs(
        source,
        candidate,
        actions=[action],
    )
    normalized = adaptation.normalized_actions[0]
    assert Path(normalized["source_pptx"]) == source.resolve()

    execute_pptx_repair_adapter(
        source,
        candidate,
        output,
        actions=[action],
    )

    assert _shape(output, "2")["bbox_emu"] == _shape(source, "2")["bbox_emu"]
    assert _shape(output, "2")["bbox_emu"] != _shape(malicious, "2")["bbox_emu"]


def test_font_size_hard_floor_cannot_be_lowered_by_custom_minimums(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, font_size_hundredths=1_000)

    with pytest.raises(UnsupportedPptxRepairError):
        execute_pptx_repairs(
            source,
            output,
            [
                {
                    "action": "set_font_size",
                    "slide": 1,
                    "shape_id": "2",
                    "font_size_pt": 7,
                    "minimum_font_size_pt": 1,
                    "body_min_font_size_pt": 1,
                    "template_min_font_size_pt": 1,
                }
            ],
        )

    assert not output.exists()


def test_autofit_effective_font_size_never_drops_below_hard_floor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, font_size_hundredths=800)

    execute_pptx_repairs(
        source,
        output,
        [
            {
                "action": "set_autofit",
                "slide": 1,
                "shape_id": "2",
                "minimum_font_size_pt": 1,
            }
        ],
    )

    assert _effective_autofit_font_size(output) >= 8


def test_replace_translation_runs_before_overflow_and_final_capacity_is_safe(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(
        source,
        text=SOURCE_TEXT * 10,
        shape_width=1_200_000,
        shape_height=500_000,
    )
    block = extract_translation_blocks(source)[0]

    report = execute_pptx_repairs(
        source,
        output,
        [
            {
                "action": "resolve_text_overflow",
                "slide": 1,
                "shape_id": "2",
                "target_load_ratio": 1.0,
                "allow_expand": False,
            },
            {
                "action": "replace_translation",
                "block_id": block.block_id,
                "text": "Tiny.",
            },
        ],
    )

    applied = report["audit"]["actions"]
    assert [action["action"] for action in applied] == [
        "replace_translation",
        "resolve_text_overflow",
    ]
    assert report["audit"]["priority_order"][0] == "replace_translation"
    repaired = _shape(output, "2")
    assert repaired["text"] == "Tiny."
    assert repaired["capacity"]["load_ratio"] <= 1.0


@pytest.mark.parametrize("shape_kind", ["picture", "graphic-frame"])
def test_placeholder_picture_and_graphic_frame_geometry_is_rejected(
    tmp_path: Path,
    shape_kind: str,
) -> None:
    source = tmp_path / f"{shape_kind}.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source)
    if shape_kind == "picture":
        replacement = """    <p:pic>
      <p:nvPicPr><p:cNvPr id="2" name="Picture 1"/><p:cNvPicPr/>
        <p:nvPr><p:ph type="pic"/></p:nvPr></p:nvPicPr>
      <p:blipFill><a:blip/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
      <p:spPr><a:xfrm><a:off x="500000" y="500000"/>
        <a:ext cx="4000000" cy="1000000"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
    </p:pic>"""
    else:
        replacement = """    <p:graphicFrame>
      <p:nvGraphicFramePr><p:cNvPr id="2" name="Chart 1"/>
        <p:cNvGraphicFramePr/><p:nvPr><p:ph type="body"/></p:nvPr>
      </p:nvGraphicFramePr>
      <p:xfrm><a:off x="500000" y="500000"/>
        <a:ext cx="4000000" cy="1000000"/></p:xfrm>
      <a:graphic><a:graphicData uri="urn:test"/></a:graphic>
    </p:graphicFrame>"""
    _replace_primary_shape(source, replacement)

    with pytest.raises(UnsupportedPptxRepairError):
        execute_pptx_repairs(
            source,
            output,
            [
                {
                    "action": "set_shape_bounds",
                    "slide": 1,
                    "shape_id": "2",
                    "bounds_emu": [600_000, 600_000, 4_000_000, 1_000_000],
                }
            ],
        )

    assert not output.exists()


@pytest.mark.parametrize("transform", ['rot="6000000"', 'flipH="1"'])
def test_rotated_and_flipped_shape_geometry_is_rejected(
    tmp_path: Path,
    transform: str,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source)
    _rewrite_slide(
        source,
        lambda xml: xml.replace(
            "<p:spPr><a:xfrm>",
            f"<p:spPr><a:xfrm {transform}>",
            1,
        ),
    )

    with pytest.raises(UnsupportedPptxRepairError):
        execute_pptx_repairs(
            source,
            output,
            [
                {
                    "action": "set_shape_bounds",
                    "slide": 1,
                    "shape_id": "2",
                    "bounds_emu": [600_000, 600_000, 4_000_000, 1_000_000],
                }
            ],
        )

    assert not output.exists()


def test_adapter_rejects_residual_error_qa_and_removes_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source)
    _write_pptx(candidate, text=SOURCE_TEXT * 5)

    with pytest.raises(PptxRepairAdapterError):
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
        )

    assert not output.exists()


def test_adapter_refuses_existing_output_without_overwriting_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pptx"
    candidate = tmp_path / "candidate.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source)
    _write_pptx(candidate, shape_x=2_500_000)
    output.write_bytes(b"existing-owner")

    with pytest.raises(FileExistsError):
        execute_pptx_repair_adapter(
            source,
            candidate,
            output,
            actions=[
                {
                    "action": "restore_shape_coordinates",
                    "slide": 1,
                    "shape_id": "2",
                }
            ],
        )

    assert output.read_bytes() == b"existing-owner"


@pytest.mark.parametrize(
    "action",
    [
        {
            "action": "set_shape_bounds",
            "slide": 1,
            "shape_id": "2",
            "x": -1,
            "y": 0,
            "width": 3_000_000,
            "height": 1_000_000,
            "clamp_to_slide": False,
        },
        {
            "action": "set_shape_bounds",
            "slide": 1,
            "shape_id": "2",
            "x": 6_500_000,
            "y": 500_000,
            "width": 4_000_000,
            "height": 1_000_000,
            "clamp_to_slide": False,
        },
        {
            "action": "delete_all_document_content",
            "slide": 1,
            "shape_id": "2",
        },
    ],
    ids=["out-of-bounds", "new-collision", "unknown-action"],
)
def test_unsafe_geometry_collision_and_unknown_actions_are_rejected(
    tmp_path: Path,
    action: dict,
) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, second_shape=True)
    source_hash = _sha256(source)

    with pytest.raises(UnsupportedPptxRepairError):
        execute_pptx_repairs(source, output, [action])

    assert not output.exists()
    assert _sha256(source) == source_hash
    assert SLIDE_WIDTH > 0 and SLIDE_HEIGHT > 0
