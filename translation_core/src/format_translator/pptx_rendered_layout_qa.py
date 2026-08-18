"""Post-repair PPTX text layout QA using PowerPoint's rendered text bounds."""

from __future__ import annotations

import os
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeAlias

from .legacy import LegacyConversionError, resolve_soffice


PathLike: TypeAlias = str | os.PathLike[str]
LayoutRunner: TypeAlias = Callable[
    [Path, list[dict[str, object]]],
    Mapping[str, Any],
]

ABSOLUTE_TOLERANCE_PT = 1.0
RELATIVE_TOLERANCE = 0.01
# PowerPoint's TextRange2.BoundWidth includes italic glyph overhang and can
# report fully-justified CJK lines slightly wider than the usable text frame
# even when the rendered slide is not clipped.  Keep vertical detection tight,
# but allow this measured horizontal-only overhang before raising an error.
HORIZONTAL_RELATIVE_TOLERANCE = 0.02
POSITION_TOLERANCE_PT = 1.0
OVERLAP_MIN_DIMENSION_PT = 1.0
OVERLAP_MIN_AREA_PT2 = 16.0
OVERLAP_WORSENING_RATIO = 0.25
BACKGROUND_IMAGE_AREA_RATIO = 0.5
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_COM_MAX_WORKERS = 3
DEFAULT_COM_QUEUE_TIMEOUT_SECONDS = 120.0
COM_MAX_WORKERS_ENV = "FORMAT_TRANSLATOR_PPTX_COM_MAX_WORKERS"
COM_QUEUE_TIMEOUT_ENV = "FORMAT_TRANSLATOR_PPTX_COM_QUEUE_TIMEOUT_SECONDS"
COM_LOCK_DIRECTORY_ENV = "FORMAT_TRANSLATOR_PPTX_COM_LOCK_DIRECTORY"
LAYOUT_BACKEND_ENV = "FORMAT_TRANSLATOR_PPTX_LAYOUT_BACKEND"
SOFFICE_PATH_ENV = "FORMAT_TRANSLATOR_SOFFICE_PATH"
LIBREOFFICE_MAX_WORKERS_ENV = "FORMAT_TRANSLATOR_LIBREOFFICE_MAX_WORKERS"
LIBREOFFICE_QUEUE_TIMEOUT_ENV = "FORMAT_TRANSLATOR_LIBREOFFICE_QUEUE_TIMEOUT_SECONDS"
LIBREOFFICE_LOCK_DIRECTORY_ENV = "FORMAT_TRANSLATOR_LIBREOFFICE_LOCK_DIRECTORY"
LIBREOFFICE_TIMEOUT_ENV = "FORMAT_TRANSLATOR_LIBREOFFICE_TIMEOUT_SECONDS"
LAYOUT_BACKENDS = frozenset({"auto", "powerpoint-com", "libreoffice", "disabled"})

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_FORBIDDEN_LINE_START = frozenset("，。！？；：、）》】〕〉」』〗〙〛”’…—～·,.!?;:)]}")
_PUNCTUATION_ONLY = frozenset(
    "，。！？；：、）》】〕〉」』〗〙〛”’…—～·,.!?;:)]}（《【〔〈「『〖〘〚“‘([{"
)
_NUMBER_LINE_END_RE = re.compile(r"[0-9０-９](?:[\s,，.．]*)$")
_UNIT_LINE_START_RE = re.compile(
    r"^(?:"
    r"万|亿|兆|千|百|十|元|美元|欧元|英镑|卢布|日元|韩元|港元|"
    r"人|家|个|项|次|倍|户|吨|公里|千米|米|平方米|平方公里|"
    r"百分点|年|月|日|%|％|bn\b|mn\b|trn\b|million\b|billion\b|"
    r"trillion\b"
    r")",
    re.IGNORECASE,
)


class _WorkerSlotLease:
    def __init__(
        self,
        slot: int | None,
        release: Callable[[], None],
    ) -> None:
        self.slot = slot
        self._release = release

    def release(self) -> None:
        self._release()


class PowerPointComWorkerPool:
    """Bound concurrent calls to a PowerPoint-compatible layout runner."""

    def __init__(
        self,
        *,
        runner: LayoutRunner,
        max_workers: int = 3,
        queue_timeout_seconds: float = 120,
        lock_directory: PathLike | None = None,
        worker_label: str = "PowerPoint COM",
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        self._runner = runner
        self.max_workers = max_workers
        self.queue_timeout_seconds = float(queue_timeout_seconds)
        self.worker_label = worker_label
        self._slots = threading.BoundedSemaphore(max_workers)
        self.lock_directory = (
            Path(lock_directory).expanduser().resolve()
            if lock_directory is not None
            else None
        )
        if self.lock_directory is not None:
            self.lock_directory.mkdir(parents=True, exist_ok=True)

    def __call__(
        self,
        document: Path,
        targets: list[dict[str, object]],
    ) -> Mapping[str, Any]:
        queued_at = time.perf_counter()
        lease = self._acquire_slot()
        queue_wait_ms = round((time.perf_counter() - queued_at) * 1000)
        metrics = {
            "pool_max_workers": self.max_workers,
            "pool_scope": ("machine" if self.lock_directory is not None else "process"),
            "queue_timeout_seconds": self.queue_timeout_seconds,
            "queue_wait_ms": queue_wait_ms,
            "worker_id": f"{os.getpid()}:{threading.get_ident()}",
            "worker_slot": lease.slot if lease is not None else None,
        }
        if lease is None:
            return {
                "provider": "powerpoint-com-pool",
                "available": False,
                "measurements": [],
                "unavailable_reason": (
                    f"Timed out waiting for an available {self.worker_label} worker "
                    f"after {self.queue_timeout_seconds:g} seconds."
                ),
                **metrics,
            }
        try:
            result = dict(self._runner(document, targets))
            result.update(metrics)
            return result
        finally:
            lease.release()

    def _acquire_slot(self) -> _WorkerSlotLease | None:
        if self.lock_directory is None:
            acquired = self._slots.acquire(timeout=self.queue_timeout_seconds)
            if not acquired:
                return None
            return _WorkerSlotLease(None, self._slots.release)

        deadline = time.perf_counter() + self.queue_timeout_seconds
        while True:
            for slot in range(self.max_workers):
                lease = self._try_machine_slot(slot)
                if lease is not None:
                    return lease
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            time.sleep(min(0.05, remaining))

    def _try_machine_slot(self, slot: int) -> _WorkerSlotLease | None:
        assert self.lock_directory is not None
        lock_path = self.lock_directory / f"slot-{slot}.lock"
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return None

        def release() -> None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

        return _WorkerSlotLease(slot, release)


_POWERPOINT_COM_SCRIPT = r"""
param(
    [Parameter(Mandatory=$true)][string]$InputJson,
    [Parameter(Mandatory=$true)][string]$OutputJson
)
$ErrorActionPreference = 'Stop'

function Get-RenderedLines {
    param(
        [Parameter(Mandatory=$true)]$TextRange,
        [double]$OffsetLeft = 0.0,
        [double]$OffsetTop = 0.0
    )
    $renderedLines = @()
    $lineCollection = $TextRange.Lines()
    for ($lineIndex = 1; $lineIndex -le $lineCollection.Count; $lineIndex++) {
        $line = $TextRange.Lines($lineIndex, 1)
        $lineText = [string]$line.Text
        $renderedLines += [pscustomobject][ordered]@{
            line_index = $lineIndex
            start = [int]$line.Start
            length = [int]$line.Length
            text = $lineText
            ends_with_paragraph_break = $lineText.EndsWith("`r")
            ends_with_manual_break = $lineText.EndsWith("`v")
            bound_left_pt = $OffsetLeft + [double]$line.BoundLeft
            bound_top_pt = $OffsetTop + [double]$line.BoundTop
            bound_width_pt = [double]$line.BoundWidth
            bound_height_pt = [double]$line.BoundHeight
        }
    }
    return $renderedLines
}

function Find-ShapeById {
    param(
        [Parameter(Mandatory=$true)]$Shapes,
        [Parameter(Mandatory=$true)][string]$ShapeId
    )
    for ($shapeIndex = 1; $shapeIndex -le $Shapes.Count; $shapeIndex++) {
        $candidate = $Shapes.Item($shapeIndex)
        if ([string]$candidate.Id -eq $ShapeId) {
            return $candidate
        }
        if ([int]$candidate.Type -eq 6) {
            $match = Find-ShapeById `
                -Shapes $candidate.GroupItems `
                -ShapeId $ShapeId
            if ($null -ne $match) {
                return $match
            }
        }
    }
    return $null
}

function New-InheritedObjectRecord {
    param(
        [Parameter(Mandatory=$true)]$Shape,
        [Parameter(Mandatory=$true)][string]$Scope,
        [Parameter(Mandatory=$true)][int]$SlideNumber,
        [Parameter(Mandatory=$true)][double]$SlideArea
    )
    if ([int]$Shape.Type -eq 14 -or [int]$Shape.Visible -eq 0) {
        return $null
    }
    $text = ''
    if ([int]$Shape.HasTextFrame -ne 0) {
        try { $text = [string]$Shape.TextFrame.TextRange.Text } catch {}
    }
    $width = [double]$Shape.Width
    $height = [double]$Shape.Height
    $area = [Math]::Max(0.0, $width) * [Math]::Max(0.0, $height)
    return [pscustomobject][ordered]@{
        slide = $SlideNumber
        scope = $Scope
        shape_id = [string]$Shape.Id
        name = [string]$Shape.Name
        shape_type = [int]$Shape.Type
        left_pt = [double]$Shape.Left
        top_pt = [double]$Shape.Top
        width_pt = $width
        height_pt = $height
        visible = $true
        has_text_content = -not [string]::IsNullOrWhiteSpace($text)
        text = $text
        area_ratio = $(if ($SlideArea -gt 0) { $area / $SlideArea } else { 0.0 })
    }
}

function Get-SlideObjectKind {
    param([Parameter(Mandatory=$true)]$Shape)
    try {
        if ([int]$Shape.HasChart -ne 0) {
            return 'chart'
        }
    } catch {}
    if ([int]$Shape.Type -in @(11, 13)) {
        return 'image'
    }
    return 'shape'
}

function New-SlideObjectRecord {
    param(
        [Parameter(Mandatory=$true)]$Shape,
        [Parameter(Mandatory=$true)][int]$SlideNumber,
        [Parameter(Mandatory=$true)][double]$SlideArea,
        [object[]]$GroupPath = @()
    )
    if ([int]$Shape.Visible -eq 0) {
        return $null
    }
    $text = ''
    if ([int]$Shape.HasTextFrame -ne 0) {
        try { $text = [string]$Shape.TextFrame.TextRange.Text } catch {}
    }
    $shapeId = [string]$Shape.Id
    $width = [double]$Shape.Width
    $height = [double]$Shape.Height
    $area = [Math]::Max(0.0, $width) * [Math]::Max(0.0, $height)
    $zOrderPosition = $null
    try { $zOrderPosition = [int]$Shape.ZOrderPosition } catch {}
    return [pscustomobject][ordered]@{
        slide = $SlideNumber
        scope = 'slide'
        shape_id = $shapeId
        object_identity = ('slide:{0}:shape:{1}' -f $SlideNumber, $shapeId)
        identity_stable = $true
        object_kind = Get-SlideObjectKind -Shape $Shape
        group_path_shape_ids = @($GroupPath)
        name = [string]$Shape.Name
        shape_type = [int]$Shape.Type
        z_order_position = $zOrderPosition
        left_pt = [double]$Shape.Left
        top_pt = [double]$Shape.Top
        width_pt = $width
        height_pt = $height
        visible = $true
        has_text_content = -not [string]::IsNullOrWhiteSpace($text)
        text = $text
        area_ratio = $(if ($SlideArea -gt 0) { $area / $SlideArea } else { 0.0 })
    }
}

function Get-SlideObjectRecords {
    param(
        [Parameter(Mandatory=$true)]$Shapes,
        [Parameter(Mandatory=$true)][int]$SlideNumber,
        [Parameter(Mandatory=$true)][double]$SlideArea,
        [object[]]$GroupPath = @()
    )
    $records = @()
    for ($shapeIndex = 1; $shapeIndex -le $Shapes.Count; $shapeIndex++) {
        $shape = $Shapes.Item($shapeIndex)
        if ([int]$shape.Visible -eq 0) {
            continue
        }
        if ([int]$shape.Type -eq 6) {
            $childPath = @($GroupPath) + @([string]$shape.Id)
            $records += @(
                Get-SlideObjectRecords `
                    -Shapes $shape.GroupItems `
                    -SlideNumber $SlideNumber `
                    -SlideArea $SlideArea `
                    -GroupPath $childPath
            )
            continue
        }
        $record = New-SlideObjectRecord `
            -Shape $shape `
            -SlideNumber $SlideNumber `
            -SlideArea $SlideArea `
            -GroupPath $GroupPath
        if ($null -ne $record) {
            $records += $record
        }
    }
    return $records
}

$powerPoint = $null
$presentation = $null
$started = Get-Date
$result = [ordered]@{
    provider = 'powerpoint-com'
    provider_version = $null
    available = $false
    capabilities = @(
        'shape_text_bounds',
        'table_runtime_geometry',
        'rendered_text_lines',
        'table_text_lines',
        'inherited_object_bounds',
        'slide_object_bounds'
    )
    precision = 'shape'
    measurements = @()
    inherited_objects = @()
    slide_objects = @()
    slide_object_errors = @()
    unavailable_reason = $null
}
try {
    $payload = Get-Content -LiteralPath $InputJson -Raw -Encoding UTF8 |
        ConvertFrom-Json
    $powerPoint = New-Object -ComObject PowerPoint.Application
    $result.provider_version = [string]$powerPoint.Version
    $presentation = $powerPoint.Presentations.Open(
        [string]$payload.document,
        -1,
        0,
        0
    )
    $result.available = $true
    $measuredSlides = @{}
    $slideArea = (
        [double]$presentation.PageSetup.SlideWidth *
        [double]$presentation.PageSetup.SlideHeight
    )
    foreach ($target in @($payload.targets)) {
        $slideNumber = [int]$target.slide
        $shapeId = [string]$target.shape_id
        $measurement = [ordered]@{
            slide = $slideNumber
            shape_id = $shapeId
            z_order_position = $null
            bound_left_pt = $null
            bound_top_pt = $null
            bound_width_pt = $null
            bound_height_pt = $null
            available_width_pt = $null
            available_height_pt = $null
            margin_left_pt = $null
            margin_right_pt = $null
            margin_top_pt = $null
            margin_bottom_pt = $null
            autosize = $null
            word_wrap = $null
            is_table = $false
            table_frame_left_pt = $null
            table_frame_top_pt = $null
            table_frame_width_pt = $null
            table_frame_height_pt = $null
            table_row_heights_pt = @()
            text_regions = @()
            measurement_error = $null
        }
        try {
            $slide = $presentation.Slides.Item($slideNumber)
            $slideKey = [string]$slideNumber
            if (-not $measuredSlides.ContainsKey($slideKey)) {
                $measuredSlides[$slideKey] = $true
                try {
                    $result.slide_objects += @(
                        Get-SlideObjectRecords `
                            -Shapes $slide.Shapes `
                            -SlideNumber $slideNumber `
                            -SlideArea $slideArea
                    )
                }
                catch {
                    $result.slide_object_errors += [pscustomobject][ordered]@{
                        slide = $slideNumber
                        capability = 'slide_object_bounds'
                        reason = $_.Exception.Message
                    }
                }
                if ([int]$slide.DisplayMasterShapes -ne 0) {
                    $layout = $slide.CustomLayout
                    for ($objectIndex = 1; $objectIndex -le $layout.Shapes.Count; $objectIndex++) {
                        try {
                            $record = New-InheritedObjectRecord `
                                -Shape $layout.Shapes.Item($objectIndex) `
                                -Scope 'layout' `
                                -SlideNumber $slideNumber `
                                -SlideArea $slideArea
                            if ($null -ne $record) {
                                $result.inherited_objects += $record
                            }
                        } catch {}
                    }
                    if ([int]$layout.DisplayMasterShapes -ne 0) {
                        $master = $presentation.SlideMaster
                        for ($objectIndex = 1; $objectIndex -le $master.Shapes.Count; $objectIndex++) {
                            try {
                                $record = New-InheritedObjectRecord `
                                    -Shape $master.Shapes.Item($objectIndex) `
                                    -Scope 'master' `
                                    -SlideNumber $slideNumber `
                                    -SlideArea $slideArea
                                if ($null -ne $record) {
                                    $result.inherited_objects += $record
                                }
                            } catch {}
                        }
                    }
                }
            }
            $shape = Find-ShapeById -Shapes $slide.Shapes -ShapeId $shapeId
            if ($null -eq $shape) {
                throw "Shape $shapeId was not found on slide $slideNumber."
            }
            try { $measurement.z_order_position = [int]$shape.ZOrderPosition } catch {}
            if ([int]$shape.HasTable -ne 0) {
                $measurement.is_table = $true
                $measurement.table_frame_left_pt = [double]$shape.Left
                $measurement.table_frame_top_pt = [double]$shape.Top
                $measurement.table_frame_width_pt = [double]$shape.Width
                $measurement.table_frame_height_pt = [double]$shape.Height
                $rowHeights = @()
                $table = $shape.Table
                for ($rowIndex = 1; $rowIndex -le $table.Rows.Count; $rowIndex++) {
                    $rowHeights += [double]$table.Rows.Item($rowIndex).Height
                }
                $measurement.table_row_heights_pt = $rowHeights
                $textRegions = @()
                for ($rowIndex = 1; $rowIndex -le $table.Rows.Count; $rowIndex++) {
                    for ($columnIndex = 1; $columnIndex -le $table.Columns.Count; $columnIndex++) {
                        $cellShape = $table.Cell($rowIndex, $columnIndex).Shape
                        if ([int]$cellShape.HasTextFrame -eq 0) {
                            continue
                        }
                        $cellRange = $cellShape.TextFrame.TextRange
                        $cellText = [string]$cellRange.Text
                        if ([string]::IsNullOrWhiteSpace($cellText)) {
                            continue
                        }
                        $cellLines = @(
                            Get-RenderedLines `
                                -TextRange $cellRange `
                                -OffsetLeft ([double]$cellShape.Left) `
                                -OffsetTop ([double]$cellShape.Top)
                        )
                        $textRegions += [pscustomobject][ordered]@{
                            region_id = "cell-$rowIndex-$columnIndex"
                            kind = 'table_cell'
                            cell_row = $rowIndex
                            cell_column = $columnIndex
                            text = $cellText
                            rendered_lines = $cellLines
                        }
                    }
                }
                $measurement.text_regions = $textRegions
            }
            else {
                if ([int]$shape.HasTextFrame -eq 0) {
                    throw "Shape $shapeId does not have a text frame."
                }
                $frame = $shape.TextFrame2
                $range = $frame.TextRange
                $measurement.bound_left_pt = [double]$range.BoundLeft
                $measurement.bound_top_pt = [double]$range.BoundTop
                $measurement.bound_width_pt = [double]$range.BoundWidth
                $measurement.bound_height_pt = [double]$range.BoundHeight
                $measurement.margin_left_pt = [double]$frame.MarginLeft
                $measurement.margin_right_pt = [double]$frame.MarginRight
                $measurement.margin_top_pt = [double]$frame.MarginTop
                $measurement.margin_bottom_pt = [double]$frame.MarginBottom
                $measurement.available_width_pt = [Math]::Max(
                    0.0,
                    [double]$shape.Width -
                        $measurement.margin_left_pt -
                        $measurement.margin_right_pt
                )
                $measurement.available_height_pt = [Math]::Max(
                    0.0,
                    [double]$shape.Height -
                        $measurement.margin_top_pt -
                        $measurement.margin_bottom_pt
                )
                $measurement.autosize = [int]$frame.AutoSize
                $measurement.word_wrap = [int]$frame.WordWrap
                $legacyRange = $shape.TextFrame.TextRange
                $shapeLines = @(
                    Get-RenderedLines -TextRange $legacyRange
                )
                $measurement.text_regions = @(
                    [pscustomobject][ordered]@{
                        region_id = 'shape'
                        kind = 'shape'
                        cell_row = $null
                        cell_column = $null
                        text = [string]$legacyRange.Text
                        rendered_lines = $shapeLines
                    }
                )
            }
        }
        catch {
            $measurement.measurement_error = $_.Exception.Message
        }
        $result.measurements += [pscustomobject]$measurement
    }
}
catch {
    $result.available = $false
    $result.unavailable_reason = $_.Exception.Message
}
finally {
    if ($null -ne $presentation) {
        try { $presentation.Close() } catch {}
        try {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject(
                $presentation
            )
        } catch {}
    }
    if ($null -ne $powerPoint) {
        try { $powerPoint.Quit() } catch {}
        try {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject(
                $powerPoint
            )
        } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
$result.duration_ms = [int](((Get-Date) - $started).TotalMilliseconds)
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputJson -Encoding UTF8
"""


def run_pptx_rendered_layout_qa(
    document: PathLike,
    targets: Sequence[Mapping[str, Any]],
    *,
    runner: LayoutRunner | None = None,
    required: bool = False,
) -> dict[str, Any]:
    """Measure selected text shapes and report rendered overflow.

    ``runner`` is injectable so callers can use another rendering service and
    tests do not require a local Office installation.
    """

    pptx = Path(document).expanduser().resolve()
    if not pptx.is_file():
        raise FileNotFoundError(f"PPTX does not exist: {pptx}")
    if pptx.suffix.lower() != ".pptx":
        raise ValueError("document must end with .pptx")
    normalized_targets = _normalize_targets(targets)
    if not normalized_targets:
        return {
            "provider": "not-required",
            "available": True,
            "targets": [],
            "measurements": [],
            "issues": [],
            "unavailable_reason": None,
            "document_sha256": _sha256(pptx),
        }
    selected_runner = runner or _default_layout_runner()
    try:
        raw = dict(selected_runner(pptx, normalized_targets))
    except Exception as error:
        raw = {
            "provider": _runner_name(selected_runner),
            "available": False,
            "measurements": [],
            "unavailable_reason": f"{type(error).__name__}: {error}",
        }

    report = _normalize_report(raw, normalized_targets)
    report["document_sha256"] = _sha256(pptx)
    if required and not report["available"]:
        report["issues"].append(
            {
                "code": "PPTX_RENDERED_LAYOUT_UNAVAILABLE",
                "severity": "error",
                "message": (
                    "Rendered PPTX layout QA is required but its provider "
                    "is unavailable."
                ),
                "provider": report["provider"],
                "reason": report.get("unavailable_reason"),
                "details": {
                    "provider": report["provider"],
                    "reason": report.get("unavailable_reason"),
                },
            }
        )
    elif required:
        required_capability = None
        message = None
        if "shape_text_bounds" not in report["capabilities"]:
            required_capability = "shape_text_bounds"
            message = (
                "The selected layout backend can open PPTX files but "
                "cannot provide shape-level rendered text bounds."
            )
        elif (
            str(report["provider"]).casefold().startswith("powerpoint-com")
            and "slide_object_bounds" not in report["capabilities"]
        ):
            required_capability = "slide_object_bounds"
            message = (
                "PowerPoint COM can measure rendered text but cannot safely "
                "identify non-self slide-local objects for overlap QA."
            )
        if required_capability is not None:
            report["issues"].append(
                {
                    "code": "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT",
                    "severity": "error",
                    "message": message,
                    "details": {
                        "provider": report["provider"],
                        "capabilities": report["capabilities"],
                        "required_capability": required_capability,
                    },
                }
            )
    return report


def pptx_text_shape_targets(document: PathLike) -> list[dict[str, object]]:
    """Return every non-empty native text shape or inline table in a PPTX."""

    from .pptx_engine import structure_snapshot

    snapshot = structure_snapshot(document)
    return [
        {
            "slide": int(slide["number"]),
            "shape_id": str(shape["shape_id"]),
        }
        for slide in snapshot["slides"]
        for shape in slide["shapes"]
        if shape.get("shape_type") in {"shape", "graphic_frame"}
        and str(shape.get("text", "")).strip()
    ]


def compare_pptx_rendered_layout(
    source_document: PathLike,
    candidate_document: PathLike,
    targets: Sequence[Mapping[str, Any]],
    *,
    runner: LayoutRunner | None = None,
    required: bool = False,
) -> dict[str, Any]:
    """Report only new or materially worsened candidate layout failures."""

    reference = run_pptx_rendered_layout_qa(
        source_document,
        targets,
        runner=runner,
        required=required,
    )
    candidate = run_pptx_rendered_layout_qa(
        candidate_document,
        targets,
        runner=runner,
        required=required,
    )
    raw_issues = list(candidate["issues"])
    reference_measurements = {
        (int(value["slide"]), str(value["shape_id"])): value
        for value in reference["measurements"]
    }
    effective_issues: list[dict[str, Any]] = []
    for issue in raw_issues:
        if issue.get("code") != "PPTX_RENDERED_TEXT_OVERFLOW":
            effective_issues.append(issue)
            continue
        key = (int(issue["slide"]), str(issue["shape_id"]))
        candidate_measurement = next(
            (
                value
                for value in candidate["measurements"]
                if (
                    int(value["slide"]),
                    str(value["shape_id"]),
                )
                == key
            ),
            None,
        )
        reference_measurement = reference_measurements.get(key)
        if candidate_measurement is None or reference_measurement is None:
            effective_issues.append(issue)
            continue
        if _materially_worse(candidate_measurement, reference_measurement):
            issue = dict(issue)
            issue["details"] = {
                **dict(issue.get("details") or {}),
                "reference_overflow_width_pt": reference_measurement.get(
                    "overflow_width_pt"
                ),
                "reference_overflow_height_pt": reference_measurement.get(
                    "overflow_height_pt"
                ),
            }
            effective_issues.append(issue)
    candidate_measurements = {
        (int(value["slide"]), str(value["shape_id"])): value
        for value in candidate["measurements"]
    }
    for key, candidate_measurement in candidate_measurements.items():
        if candidate_measurement.get("is_table") is not True:
            continue
        reference_measurement = reference_measurements.get(key)
        if (
            reference_measurement is None
            or reference_measurement.get("is_table") is not True
        ):
            continue
        change = _table_geometry_change(
            candidate_measurement,
            reference_measurement,
        )
        if change is None:
            continue
        slide, shape_id = key
        details = {
            "shape_id": shape_id,
            **change,
            "provider": candidate.get("provider"),
            "provider_version": candidate.get("provider_version"),
        }
        if change["automatic_row_height_restore_safe"]:
            reference_rows = change["reference_row_heights_pt"]
            details["repair_hint"] = {
                "action": "restore_table_row_heights",
                "automatic": True,
                "slide": slide,
                "shape_id": shape_id,
                "rows": [
                    {
                        "row_index": row_index,
                        "height_pt": reference_rows[row_index - 1],
                    }
                    for row_index in change["shortened_row_indices"]
                ],
            }
        effective_issues.append(
            {
                "code": "PPTX_RENDERED_TABLE_GEOMETRY_CHANGED",
                "severity": "error",
                "message": (
                    "PowerPoint recalculated the translated table's runtime "
                    "frame or row heights beyond the allowed tolerance."
                ),
                "slide": slide,
                "shape_id": shape_id,
                "details": details,
            }
        )
    reference_line_violations = _rendered_line_violations(reference)
    candidate_line_violations = _rendered_line_violations(candidate)
    for key, candidate_value in candidate_line_violations.items():
        reference_value = reference_line_violations.get(key)
        reference_count = (
            len(reference_value["violations"]) if reference_value is not None else 0
        )
        candidate_count = len(candidate_value["violations"])
        if candidate_count <= reference_count:
            continue
        slide, shape_id, region_id = key
        effective_issues.append(
            {
                "code": "PPTX_RENDERED_LINE_BREAK_VIOLATION",
                "severity": "warning",
                "message": (
                    "PowerPoint introduced additional Chinese line-break "
                    "violations in the translated text."
                ),
                "slide": slide,
                "shape_id": shape_id,
                "details": {
                    "shape_id": shape_id,
                    "region_id": region_id,
                    "kind": candidate_value["kind"],
                    "cell_row": candidate_value.get("cell_row"),
                    "cell_column": candidate_value.get("cell_column"),
                    "reference_violation_count": reference_count,
                    "candidate_violation_count": candidate_count,
                    "violations": candidate_value["violations"],
                    "provider": candidate.get("provider"),
                    "provider_version": candidate.get("provider_version"),
                },
            }
        )

    reference_overlaps = _rendered_text_object_overlaps(reference)
    candidate_overlaps = _rendered_text_object_overlaps(candidate)
    reference_slide_object_error_slides = {
        int(error["slide"])
        for error in reference.get("slide_object_errors", [])
        if error.get("slide") is not None
    }
    reference_slide_object_error_global = any(
        error.get("slide") is None for error in reference.get("slide_object_errors", [])
    )
    for key, candidate_value in candidate_overlaps.items():
        slide, _shape_id, _region_id, scope, _object_identity = key
        if scope == "slide" and (
            reference_slide_object_error_global
            or slide in reference_slide_object_error_slides
        ):
            continue
        candidate_area = candidate_value["overlap_area_pt2"]
        if candidate_area <= OVERLAP_MIN_AREA_PT2:
            continue
        reference_value = reference_overlaps.get(key)
        reference_area = (
            reference_value["overlap_area_pt2"] if reference_value is not None else 0.0
        )
        worsening_tolerance = max(
            OVERLAP_MIN_AREA_PT2,
            reference_area * OVERLAP_WORSENING_RATIO,
        )
        if candidate_area <= reference_area + worsening_tolerance:
            continue
        slide, shape_id, region_id, scope, object_identity = key
        overlap_object = candidate_value["object"]
        object_shape_id = str(overlap_object["shape_id"])
        effective_issues.append(
            {
                "code": "PPTX_RENDERED_TEXT_OBJECT_OVERLAP",
                "severity": "error",
                "message": (
                    "Translated rendered text newly overlaps a non-self "
                    "slide, layout, or master object."
                ),
                "slide": slide,
                "shape_id": shape_id,
                "details": {
                    "shape_id": shape_id,
                    "region_id": region_id,
                    "kind": candidate_value["kind"],
                    "cell_row": candidate_value.get("cell_row"),
                    "cell_column": candidate_value.get("cell_column"),
                    "object_scope": scope,
                    "object_shape_id": object_shape_id,
                    "object_identity": object_identity,
                    # Backward-compatible aliases consumed by the existing
                    # repair adapter's stable issue identity.
                    "inherited_scope": scope,
                    "inherited_shape_id": object_shape_id,
                    "reference_overlap_area_pt2": reference_area,
                    "candidate_overlap_area_pt2": candidate_area,
                    "worsening_tolerance_pt2": worsening_tolerance,
                    "object": candidate_value["object"],
                    "overlaps": candidate_value["overlaps"],
                    "provider": candidate.get("provider"),
                    "provider_version": candidate.get("provider_version"),
                },
            }
        )
    existing_reference_failure_keys = {
        (
            str(issue.get("code") or ""),
            issue.get("slide"),
            str(dict(issue.get("details") or {}).get("reason") or ""),
        )
        for issue in effective_issues
    }
    for reference_issue in reference["issues"]:
        if reference_issue.get("code") not in {
            "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT",
            "PPTX_RENDERED_SLIDE_OBJECT_MEASUREMENT_FAILED",
        }:
            continue
        reference_failure_key = (
            str(reference_issue["code"]),
            reference_issue.get("slide"),
            str(dict(reference_issue.get("details") or {}).get("reason") or ""),
        )
        if reference_failure_key in existing_reference_failure_keys:
            continue
        effective_issues.append(dict(reference_issue))
        existing_reference_failure_keys.add(reference_failure_key)
    if required and reference["available"] and candidate["available"]:
        reference_provider = str(reference.get("provider") or "unknown")
        candidate_provider = str(candidate.get("provider") or "unknown")
        reference_version = reference.get("provider_version")
        candidate_version = candidate.get("provider_version")
        provider_changed = reference_provider != candidate_provider
        version_changed = (
            reference_version is not None
            and candidate_version is not None
            and str(reference_version) != str(candidate_version)
        )
        if provider_changed or version_changed:
            effective_issues.append(
                {
                    "code": "PPTX_RENDERED_LAYOUT_PROVIDER_MISMATCH",
                    "severity": "error",
                    "message": (
                        "Source and candidate rendered-layout measurements "
                        "came from different provider environments."
                    ),
                    "details": {
                        "reference_provider": reference_provider,
                        "reference_provider_version": reference_version,
                        "candidate_provider": candidate_provider,
                        "candidate_provider_version": candidate_version,
                    },
                }
            )
        reference_capabilities = sorted(
            str(value) for value in reference.get("capabilities", [])
        )
        candidate_capabilities = sorted(
            str(value) for value in candidate.get("capabilities", [])
        )
        capability_already_insufficient = any(
            issue.get("code") == "PPTX_RENDERED_LAYOUT_CAPABILITY_INSUFFICIENT"
            for issue in (*reference["issues"], *candidate["issues"])
        )
        if (
            reference_capabilities != candidate_capabilities
            and not capability_already_insufficient
        ):
            effective_issues.append(
                {
                    "code": "PPTX_RENDERED_LAYOUT_CAPABILITY_MISMATCH",
                    "severity": "error",
                    "message": (
                        "Source and candidate rendered-layout measurements "
                        "used different provider capabilities."
                    ),
                    "details": {
                        "provider": reference_provider,
                        "reference_capabilities": reference_capabilities,
                        "candidate_capabilities": candidate_capabilities,
                    },
                }
            )
    if required and not reference["available"]:
        if not any(
            issue.get("code") == "PPTX_RENDERED_LAYOUT_UNAVAILABLE"
            for issue in effective_issues
        ):
            effective_issues.extend(
                issue
                for issue in reference["issues"]
                if issue.get("code") == "PPTX_RENDERED_LAYOUT_UNAVAILABLE"
            )
    candidate["raw_issues"] = raw_issues
    candidate["issues"] = effective_issues
    candidate["reference"] = reference
    candidate["comparison"] = {
        "policy": "new_or_materially_worsened",
        "raw_candidate_issue_count": len(raw_issues),
        "effective_issue_count": len(effective_issues),
    }
    return candidate


def run_pptx_qa_with_rendered_layout(
    source_path: PathLike,
    candidate_path: PathLike,
    *,
    layout_runner: LayoutRunner | None = None,
    rendered_layout_required: bool = False,
    **native_options: Any,
) -> dict[str, Any]:
    """Combine native PPTX QA with actual PowerPoint text measurements."""

    from .pptx_engine import run_pptx_qa

    supported_native_options = {
        "coordinate_tolerance_emu",
        "overlap_threshold",
        "expansion_ratio_threshold",
        "capacity_warning_ratio",
        "blocks",
        "translations",
        "authorized_coordinate_changes",
    }
    native_issues = run_pptx_qa(
        source_path,
        candidate_path,
        **{
            key: value
            for key, value in native_options.items()
            if key in supported_native_options
        },
    )
    rendered = compare_pptx_rendered_layout(
        source_path,
        candidate_path,
        pptx_text_shape_targets(candidate_path),
        runner=layout_runner,
        required=rendered_layout_required,
    )
    authoritative_shapes = _authoritative_rendered_shape_keys(rendered)
    reconciled_native_issues = []
    superseded_capacity_issues = []
    for issue in native_issues:
        code = str(getattr(issue, "code", ""))
        details = getattr(issue, "details", {})
        details = details if isinstance(details, Mapping) else {}
        capacity = details.get("capacity")
        capacity = capacity if isinstance(capacity, Mapping) else {}
        slide = getattr(issue, "slide", None)
        shape_id = details.get("shape_id")
        key = (
            (int(slide), str(shape_id))
            if slide is not None and shape_id is not None
            else None
        )
        if (
            code == "PPTX_TEXT_CAPACITY_RISK"
            and capacity.get("estimate_only") is True
            and key in authoritative_shapes
        ):
            superseded_capacity_issues.append(
                {
                    "code": code,
                    "slide": int(slide),
                    "shape_id": str(shape_id),
                    "reason": "authoritative_rendered_bounds_available",
                }
            )
            continue
        reconciled_native_issues.append(issue)
    from .models import QaIssue

    rendered_issues = [
        QaIssue(
            code=str(issue["code"]),
            message=str(issue["message"]),
            severity=str(issue["severity"]),
            stage="pptx_rendered_layout_qa",
            slide=(int(issue["slide"]) if issue.get("slide") is not None else None),
            recoverable=(
                str(issue["code"]) == "PPTX_RENDERED_TEXT_OVERFLOW"
                or bool(dict(issue.get("details") or {}).get("repair_hint"))
            ),
            details=dict(issue.get("details") or {}),
        )
        for issue in rendered["issues"]
    ]
    return {
        "issues": [*reconciled_native_issues, *rendered_issues],
        "rendered_layout_qa": rendered,
        "native_issue_reconciliation": {
            "policy": "rendered_bounds_supersede_estimated_capacity_per_shape",
            "superseded_capacity_issues": superseded_capacity_issues,
        },
    }


def _authoritative_rendered_shape_keys(
    report: Mapping[str, Any],
) -> set[tuple[int, str]]:
    """Return shapes with successful shape-level rendered measurements."""

    if report.get("available") is not True:
        return set()
    capabilities = report.get("capabilities")
    if not isinstance(capabilities, Sequence) or isinstance(
        capabilities, (str, bytes, bytearray)
    ):
        return set()
    if "shape_text_bounds" not in {str(value) for value in capabilities}:
        return set()
    keys: set[tuple[int, str]] = set()
    for measurement in report.get("measurements", []):
        if not isinstance(measurement, Mapping):
            continue
        if measurement.get("measurement_error") or measurement.get("is_table") is True:
            continue
        if measurement.get("overflow") not in {True, False}:
            continue
        try:
            key = (int(measurement["slide"]), str(measurement["shape_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        keys.add(key)
    return keys


def _materially_worse(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> bool:
    if candidate.get("overflow") is not True:
        return False
    for dimension in ("width", "height"):
        candidate_excess = float(candidate.get(f"overflow_{dimension}_pt", 0.0))
        reference_excess = float(reference.get(f"overflow_{dimension}_pt", 0.0))
        tolerance = max(
            float(candidate.get(f"{dimension}_tolerance_pt", 0.0)),
            float(reference.get(f"{dimension}_tolerance_pt", 0.0)),
        )
        if dimension == "height" and reference.get("overflow") is not True:
            if candidate_excess > tolerance:
                return True
            continue
        # BoundWidth contains a small source-dependent glyph overhang even
        # when PowerPoint renders the frame without clipping.  Apply the
        # horizontal tolerance to the increase over that measured baseline,
        # not to the candidate's absolute overhang.
        if candidate_excess > reference_excess + tolerance:
            return True
    return False


def _table_geometry_change(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> dict[str, Any] | None:
    reference_height = _number(reference.get("table_frame_height_pt"))
    candidate_height = _number(candidate.get("table_frame_height_pt"))
    frame_tolerance = max(
        ABSOLUTE_TOLERANCE_PT,
        abs(reference_height) * RELATIVE_TOLERANCE,
    )
    frame_delta = candidate_height - reference_height

    reference_left = _number(reference.get("table_frame_left_pt"))
    candidate_left = _number(candidate.get("table_frame_left_pt"))
    reference_top = _number(reference.get("table_frame_top_pt"))
    candidate_top = _number(candidate.get("table_frame_top_pt"))
    reference_width = _number(reference.get("table_frame_width_pt"))
    candidate_width = _number(candidate.get("table_frame_width_pt"))
    left_delta = candidate_left - reference_left
    top_delta = candidate_top - reference_top
    width_delta = candidate_width - reference_width
    width_tolerance = POSITION_TOLERANCE_PT
    coordinates_unchanged = (
        abs(left_delta) <= POSITION_TOLERANCE_PT
        and abs(top_delta) <= POSITION_TOLERANCE_PT
        and abs(width_delta) <= width_tolerance
    )

    reference_rows = _numeric_sequence(
        reference.get("table_row_heights_pt"),
        field="table_row_heights_pt",
    )
    candidate_rows = _numeric_sequence(
        candidate.get("table_row_heights_pt"),
        field="table_row_heights_pt",
    )
    changed_rows: list[int] = []
    shortened_rows: list[int] = []
    taller_rows: list[int] = []
    row_deltas: list[float] = []
    row_tolerances: list[float] = []
    for index, (reference_row, candidate_row) in enumerate(
        zip(reference_rows, candidate_rows),
        start=1,
    ):
        tolerance = max(
            ABSOLUTE_TOLERANCE_PT,
            abs(reference_row) * RELATIVE_TOLERANCE,
        )
        delta = candidate_row - reference_row
        row_deltas.append(delta)
        row_tolerances.append(tolerance)
        if abs(delta) > tolerance:
            changed_rows.append(index)
            if delta < 0:
                shortened_rows.append(index)
            else:
                taller_rows.append(index)

    row_count_changed = len(reference_rows) != len(candidate_rows)
    frame_changed = abs(frame_delta) > frame_tolerance
    if not frame_changed and not changed_rows and not row_count_changed:
        return None
    automatic_restore_safe = (
        bool(shortened_rows)
        and not taller_rows
        and len(shortened_rows) == len(changed_rows)
        and not row_count_changed
        and coordinates_unchanged
        and frame_delta <= POSITION_TOLERANCE_PT
    )
    return {
        "reference_frame_height_pt": reference_height,
        "candidate_frame_height_pt": candidate_height,
        "frame_height_delta_pt": frame_delta,
        "frame_height_tolerance_pt": frame_tolerance,
        "reference_frame_left_pt": reference_left,
        "candidate_frame_left_pt": candidate_left,
        "frame_left_delta_pt": left_delta,
        "frame_left_tolerance_pt": POSITION_TOLERANCE_PT,
        "reference_frame_top_pt": reference_top,
        "candidate_frame_top_pt": candidate_top,
        "frame_top_delta_pt": top_delta,
        "frame_top_tolerance_pt": POSITION_TOLERANCE_PT,
        "reference_frame_width_pt": reference_width,
        "candidate_frame_width_pt": candidate_width,
        "frame_width_delta_pt": width_delta,
        "frame_width_tolerance_pt": width_tolerance,
        "table_coordinates_unchanged": coordinates_unchanged,
        "reference_row_heights_pt": reference_rows,
        "candidate_row_heights_pt": candidate_rows,
        "reference_row_count": len(reference_rows),
        "candidate_row_count": len(candidate_rows),
        "row_count_changed": row_count_changed,
        "changed_row_indices": changed_rows,
        "shortened_row_indices": shortened_rows,
        "taller_row_indices": taller_rows,
        "row_height_deltas_pt": row_deltas,
        "row_height_tolerances_pt": row_tolerances,
        "automatic_row_height_restore_safe": automatic_restore_safe,
    }


def _rendered_line_violations(
    report: Mapping[str, Any],
) -> dict[tuple[int, str, str], dict[str, Any]]:
    results: dict[tuple[int, str, str], dict[str, Any]] = {}
    for measurement in report.get("measurements", []):
        slide = int(measurement["slide"])
        shape_id = str(measurement["shape_id"])
        for region in measurement.get("text_regions", []):
            lines = list(region.get("rendered_lines", []))
            region_text = str(region.get("text") or "")
            if not _CJK_RE.search(
                region_text + "".join(str(line.get("text") or "") for line in lines)
            ):
                continue
            violations: list[dict[str, Any]] = []
            for index in range(1, len(lines)):
                previous = lines[index - 1]
                line = lines[index]
                if (
                    previous.get("ends_with_paragraph_break") is True
                    or previous.get("ends_with_manual_break") is True
                ):
                    continue
                text = _visible_rendered_line_text(line.get("text"))
                previous_text = _visible_rendered_line_text(previous.get("text"))
                if not text:
                    continue
                punctuation_only = all(
                    character in _PUNCTUATION_ONLY for character in text
                )
                if punctuation_only:
                    violations.append(
                        _line_violation(
                            "punctuation_only_line",
                            line,
                            previous_line=previous,
                        )
                    )
                elif text[0] in _FORBIDDEN_LINE_START:
                    violations.append(
                        _line_violation(
                            "forbidden_line_start",
                            line,
                            previous_line=previous,
                        )
                    )
                if _NUMBER_LINE_END_RE.search(
                    previous_text
                ) and _UNIT_LINE_START_RE.search(text):
                    violations.append(
                        _line_violation(
                            "number_unit_split",
                            line,
                            previous_line=previous,
                        )
                    )
            if not violations:
                continue
            region_id = str(region["region_id"])
            results[(slide, shape_id, region_id)] = {
                "kind": str(region.get("kind") or "shape"),
                "cell_row": region.get("cell_row"),
                "cell_column": region.get("cell_column"),
                "violations": violations,
            }
    return results


def _line_violation(
    violation_type: str,
    line: Mapping[str, Any],
    *,
    previous_line: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "type": violation_type,
        "line_index": int(line["line_index"]),
        "text": _visible_rendered_line_text(line.get("text")),
        "previous_line_index": int(previous_line["line_index"]),
        "previous_text": _visible_rendered_line_text(previous_line.get("text")),
        "bound_left_pt": float(line["bound_left_pt"]),
        "bound_top_pt": float(line["bound_top_pt"]),
        "bound_width_pt": float(line["bound_width_pt"]),
        "bound_height_pt": float(line["bound_height_pt"]),
    }


def _visible_rendered_line_text(value: Any) -> str:
    return str(value or "").rstrip("\r\n\v").strip()


def _rendered_text_object_overlaps(
    report: Mapping[str, Any],
) -> dict[tuple[int, str, str, str, str], dict[str, Any]]:
    objects_by_slide: dict[int, list[dict[str, Any]]] = {}
    text_objects_by_location = {
        (int(item["slide"]), str(item["shape_id"])): item
        for item in report.get("slide_objects", [])
        if str(item.get("scope") or "") == "slide"
    }
    overlap_objects = [
        *report.get("inherited_objects", []),
        *report.get("slide_objects", []),
    ]
    for overlap_object in overlap_objects:
        if overlap_object.get("visible") is not True:
            continue
        scope = str(overlap_object.get("scope") or "")
        if scope != "slide" and int(overlap_object.get("shape_type", 0)) == 14:
            continue
        width = float(overlap_object["width_pt"])
        height = float(overlap_object["height_pt"])
        if width <= 0 or height <= 0:
            continue
        objects_by_slide.setdefault(int(overlap_object["slide"]), []).append(
            dict(overlap_object)
        )

    overlaps: dict[tuple[int, str, str, str, str], dict[str, Any]] = {}
    for measurement in report.get("measurements", []):
        slide = int(measurement["slide"])
        slide_objects = objects_by_slide.get(slide, [])
        if not slide_objects:
            continue
        shape_id = str(measurement["shape_id"])
        text_object = text_objects_by_location.get((slide, shape_id))
        for region in measurement.get("text_regions", []):
            region_id = str(region["region_id"])
            for line in region.get("rendered_lines", []):
                if not _visible_rendered_line_text(line.get("text")):
                    continue
                line_box = (
                    float(line["bound_left_pt"]),
                    float(line["bound_top_pt"]),
                    float(line["bound_width_pt"]),
                    float(line["bound_height_pt"]),
                )
                for overlap_object in slide_objects:
                    if (
                        str(overlap_object["scope"]) == "slide"
                        and str(overlap_object["shape_id"]) == shape_id
                    ):
                        continue
                    if _is_large_background_image_behind_text(
                        text_object,
                        overlap_object,
                    ):
                        continue
                    object_box = (
                        float(overlap_object["left_pt"]),
                        float(overlap_object["top_pt"]),
                        float(overlap_object["width_pt"]),
                        float(overlap_object["height_pt"]),
                    )
                    intersection = _intersection(line_box, object_box)
                    if intersection is None:
                        continue
                    intersection_width, intersection_height = intersection
                    if (
                        intersection_width <= OVERLAP_MIN_DIMENSION_PT
                        or intersection_height <= OVERLAP_MIN_DIMENSION_PT
                    ):
                        continue
                    area = intersection_width * intersection_height
                    key = (
                        slide,
                        shape_id,
                        region_id,
                        str(overlap_object["scope"]),
                        str(overlap_object["object_identity"]),
                    )
                    value = overlaps.setdefault(
                        key,
                        {
                            "kind": str(region.get("kind") or "shape"),
                            "cell_row": region.get("cell_row"),
                            "cell_column": region.get("cell_column"),
                            "object": overlap_object,
                            "overlap_area_pt2": 0.0,
                            "overlaps": [],
                        },
                    )
                    value["overlap_area_pt2"] += area
                    value["overlaps"].append(
                        {
                            "line_index": int(line["line_index"]),
                            "text": _visible_rendered_line_text(line.get("text")),
                            "intersection_width_pt": intersection_width,
                            "intersection_height_pt": intersection_height,
                            "intersection_area_pt2": area,
                            "line_bbox_pt": list(line_box),
                        }
                    )
    return overlaps


def _is_large_background_image_behind_text(
    text_object: Mapping[str, Any] | None,
    overlap_object: Mapping[str, Any],
) -> bool:
    """Ignore only large, ungrouped slide images proven behind the text shape."""

    if (
        text_object is None
        or str(text_object.get("scope") or "") != "slide"
        or list(text_object.get("group_path_shape_ids") or [])
        or str(overlap_object.get("scope") or "") != "slide"
        or str(overlap_object.get("object_kind") or "") != "image"
        or overlap_object.get("has_text_content") is True
        or float(overlap_object.get("area_ratio", 0.0)) < BACKGROUND_IMAGE_AREA_RATIO
        or list(overlap_object.get("group_path_shape_ids") or [])
    ):
        return False
    object_z_order = overlap_object.get("z_order_position")
    text_z_order = text_object.get("z_order_position")
    if object_z_order is None or text_z_order is None:
        return False
    try:
        return int(object_z_order) < int(text_z_order)
    except (TypeError, ValueError):
        return False


def _intersection(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float] | None:
    first_left, first_top, first_width, first_height = first
    second_left, second_top, second_width, second_height = second
    width = min(
        first_left + first_width,
        second_left + second_width,
    ) - max(first_left, second_left)
    height = min(
        first_top + first_height,
        second_top + second_height,
    ) - max(first_top, second_top)
    if width <= 0 or height <= 0:
        return None
    return width, height


def _normalize_targets(
    targets: Sequence[Mapping[str, Any]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for target in targets:
        slide = int(target["slide"])
        shape_id = str(target["shape_id"])
        if slide < 1 or not shape_id:
            raise ValueError("Rendered-layout targets require slide >= 1 and shape_id")
        key = (slide, shape_id)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"slide": slide, "shape_id": shape_id})
    return normalized


def _normalize_report(
    raw: Mapping[str, Any],
    targets: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    measurements: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    raw_capabilities = raw.get("capabilities")
    capabilities = (
        sorted({str(value) for value in raw_capabilities})
        if isinstance(raw_capabilities, Sequence)
        and not isinstance(raw_capabilities, (str, bytes))
        else ["shape_text_bounds"]
    )
    measured_targets: set[tuple[int, str]] = set()
    for value in raw.get("measurements", []):
        measurement = dict(value)
        slide = int(measurement["slide"])
        shape_id = str(measurement["shape_id"])
        measured_targets.add((slide, shape_id))
        measurement_error = measurement.get("measurement_error")
        if measurement_error:
            measurement["overflow"] = None
            measurements.append(measurement)
            issues.append(
                {
                    "code": "PPTX_RENDERED_LAYOUT_MEASUREMENT_FAILED",
                    "severity": "error",
                    "message": str(measurement_error),
                    "slide": slide,
                    "shape_id": shape_id,
                    "details": {
                        "shape_id": shape_id,
                        "measurement_error": str(measurement_error),
                    },
                }
            )
            continue
        measurement["text_regions"] = _normalize_text_regions(
            measurement.get("text_regions", [])
        )
        if measurement.get("is_table") is True:
            measurement.update(
                {
                    "table_frame_left_pt": _number(
                        measurement.get("table_frame_left_pt")
                    ),
                    "table_frame_top_pt": _number(
                        measurement.get("table_frame_top_pt")
                    ),
                    "table_frame_width_pt": _number(
                        measurement.get("table_frame_width_pt")
                    ),
                    "table_frame_height_pt": _number(
                        measurement.get("table_frame_height_pt")
                    ),
                    "table_row_heights_pt": _numeric_sequence(
                        measurement.get("table_row_heights_pt"),
                        field="table_row_heights_pt",
                    ),
                    "overflow": False,
                }
            )
            measurements.append(measurement)
            continue
        width = _number(measurement.get("bound_width_pt"))
        height = _number(measurement.get("bound_height_pt"))
        available_width = _number(measurement.get("available_width_pt"))
        available_height = _number(measurement.get("available_height_pt"))
        if available_width <= 0.0 or available_height <= 0.0:
            reason = "non_positive_available_text_bounds"
            measurement_error = (
                "The rendered-layout provider returned non-positive available "
                "text bounds."
            )
            measurement.pop("rendered_load_ratio", None)
            measurement.update(
                {
                    "bound_width_pt": width,
                    "bound_height_pt": height,
                    "available_width_pt": available_width,
                    "available_height_pt": available_height,
                    "measurement_error": measurement_error,
                    "overflow": None,
                }
            )
            measurements.append(measurement)
            issues.append(
                {
                    "code": "PPTX_RENDERED_LAYOUT_MEASUREMENT_FAILED",
                    "severity": "error",
                    "message": measurement_error,
                    "slide": slide,
                    "shape_id": shape_id,
                    "details": {
                        "shape_id": shape_id,
                        "measurement_error": measurement_error,
                        "reason": reason,
                        "available_width_pt": available_width,
                        "available_height_pt": available_height,
                        "provider": str(raw.get("provider") or "unknown"),
                    },
                }
            )
            continue
        overflow_width = max(0.0, width - available_width)
        overflow_height = max(0.0, height - available_height)
        width_tolerance = max(
            ABSOLUTE_TOLERANCE_PT,
            available_width * HORIZONTAL_RELATIVE_TOLERANCE,
        )
        height_tolerance = max(
            ABSOLUTE_TOLERANCE_PT,
            available_height * RELATIVE_TOLERANCE,
        )
        overflow = (
            overflow_width > width_tolerance or overflow_height > height_tolerance
        )
        rendered_load_ratio = max(
            width / available_width,
            height / available_height,
        )
        measurement.update(
            {
                "bound_width_pt": width,
                "bound_height_pt": height,
                "available_width_pt": available_width,
                "available_height_pt": available_height,
                "overflow_width_pt": overflow_width,
                "overflow_height_pt": overflow_height,
                "width_tolerance_pt": width_tolerance,
                "height_tolerance_pt": height_tolerance,
                "rendered_load_ratio": rendered_load_ratio,
                "overflow": overflow,
            }
        )
        measurements.append(measurement)
        if overflow:
            repair_hint = {
                "action": "resolve_text_overflow",
                "action_type": "resolve_text_overflow",
                "slide": slide,
                "shape_id": shape_id,
                "rendered_load_ratio": rendered_load_ratio,
                "automatic": True,
            }
            issues.append(
                {
                    "code": "PPTX_RENDERED_TEXT_OVERFLOW",
                    "severity": "error",
                    "message": (
                        "PowerPoint rendered text exceeds the text frame's "
                        "available bounds."
                    ),
                    "slide": int(measurement["slide"]),
                    "shape_id": str(measurement["shape_id"]),
                    "overflow_width_pt": overflow_width,
                    "overflow_height_pt": overflow_height,
                    "details": {
                        "shape_id": str(measurement["shape_id"]),
                        "bound_width_pt": width,
                        "bound_height_pt": height,
                        "available_width_pt": available_width,
                        "available_height_pt": available_height,
                        "overflow_width_pt": overflow_width,
                        "overflow_height_pt": overflow_height,
                        "rendered_load_ratio": rendered_load_ratio,
                        "width_tolerance_pt": width_tolerance,
                        "height_tolerance_pt": height_tolerance,
                        "provider": str(raw.get("provider") or "unknown"),
                        "provider_version": raw.get("provider_version"),
                        "repair_hint": repair_hint,
                    },
                }
            )
    if bool(raw.get("available", False)) and "shape_text_bounds" in capabilities:
        for target in targets:
            key = (int(target["slide"]), str(target["shape_id"]))
            if key in measured_targets:
                continue
            issues.append(
                {
                    "code": "PPTX_RENDERED_LAYOUT_TARGET_NOT_MEASURED",
                    "severity": "error",
                    "message": (
                        "PowerPoint did not return a measurement for the target."
                    ),
                    "slide": key[0],
                    "shape_id": key[1],
                    "details": {"shape_id": key[1]},
                }
            )
    if bool(raw.get("fail_closed", False)) and not bool(raw.get("available", False)):
        issues.append(
            {
                "code": "PPTX_LAYOUT_PROVIDER_UNAVAILABLE",
                "severity": "error",
                "message": (
                    "The configured PPTX layout backend could not open or "
                    "validate the presentation."
                ),
                "details": {
                    "provider": str(raw.get("provider") or "unknown"),
                    "reason": raw.get("unavailable_reason"),
                },
            }
        )
    inherited_objects = _normalize_inherited_objects(raw.get("inherited_objects", []))
    slide_object_errors: list[dict[str, Any]] = []
    try:
        slide_object_errors.extend(
            _normalize_slide_object_errors(raw.get("slide_object_errors", []))
        )
    except (KeyError, TypeError, ValueError) as error:
        slide_object_errors.append(
            {
                "slide": None,
                "capability": "slide_object_bounds",
                "reason": f"Invalid slide object capability error: {error}",
            }
        )
    try:
        slide_objects = _normalize_slide_objects(raw.get("slide_objects", []))
    except (KeyError, TypeError, ValueError) as error:
        slide_objects = []
        slide_object_errors.append(
            {
                "slide": None,
                "capability": "slide_object_bounds",
                "reason": f"Invalid slide object measurement: {error}",
            }
        )
    for capability_error in slide_object_errors:
        slide = capability_error.get("slide")
        issue = {
            "code": "PPTX_RENDERED_SLIDE_OBJECT_MEASUREMENT_FAILED",
            "severity": "error",
            "message": (
                "PowerPoint COM could not safely identify slide-local "
                "objects for rendered overlap QA."
            ),
            "details": {
                "provider": str(raw.get("provider") or "unknown"),
                "required_capability": "slide_object_bounds",
                "reason": capability_error["reason"],
            },
        }
        if slide is not None:
            issue["slide"] = int(slide)
        issues.append(issue)
    return {
        "provider": str(raw.get("provider") or "unknown"),
        "provider_version": raw.get("provider_version"),
        "available": bool(raw.get("available", False)),
        "capabilities": capabilities,
        "precision": raw.get("precision", "shape"),
        "targets": [dict(target) for target in targets],
        "measurements": measurements,
        "inherited_objects": inherited_objects,
        "slide_objects": slide_objects,
        "slide_object_errors": slide_object_errors,
        "issues": issues,
        "unavailable_reason": raw.get("unavailable_reason"),
        "duration_ms": raw.get("duration_ms"),
        "timeout_seconds": raw.get("timeout_seconds"),
        "attempt": raw.get("attempt", 1),
        "pool_max_workers": raw.get("pool_max_workers"),
        "pool_scope": raw.get("pool_scope"),
        "queue_timeout_seconds": raw.get("queue_timeout_seconds"),
        "queue_wait_ms": raw.get("queue_wait_ms"),
        "worker_id": raw.get("worker_id"),
        "worker_slot": raw.get("worker_slot"),
        "roundtrip_sha256": raw.get("roundtrip_sha256"),
        "target_count": raw.get("target_count"),
        "fail_closed": bool(raw.get("fail_closed", False)),
        "tolerance": {
            "absolute_pt": ABSOLUTE_TOLERANCE_PT,
            "relative": RELATIVE_TOLERANCE,
        },
    }


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("PowerPoint layout measurements must be numeric")
    return float(value)


def _numeric_sequence(value: Any, *, field: str) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"PowerPoint {field} must be a numeric sequence")
    return [_number(item) for item in value]


def _normalize_text_regions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("PowerPoint text_regions must be a sequence")
    regions: list[dict[str, Any]] = []
    for raw_region in value:
        if not isinstance(raw_region, Mapping):
            raise ValueError("PowerPoint text regions must be objects")
        region = dict(raw_region)
        region_id = str(region.get("region_id") or "")
        if not region_id:
            raise ValueError("PowerPoint text regions require region_id")
        raw_lines = region.get("rendered_lines", [])
        if not isinstance(raw_lines, Sequence) or isinstance(
            raw_lines, (str, bytes, bytearray)
        ):
            raise ValueError("PowerPoint rendered_lines must be a sequence")
        lines: list[dict[str, Any]] = []
        for raw_line in raw_lines:
            if not isinstance(raw_line, Mapping):
                raise ValueError("PowerPoint rendered lines must be objects")
            line = dict(raw_line)
            line.update(
                {
                    "line_index": int(line["line_index"]),
                    "text": str(line.get("text") or ""),
                    "bound_left_pt": _number(line.get("bound_left_pt")),
                    "bound_top_pt": _number(line.get("bound_top_pt")),
                    "bound_width_pt": _number(line.get("bound_width_pt")),
                    "bound_height_pt": _number(line.get("bound_height_pt")),
                    "ends_with_paragraph_break": bool(
                        line.get("ends_with_paragraph_break", False)
                    ),
                    "ends_with_manual_break": bool(
                        line.get("ends_with_manual_break", False)
                    ),
                }
            )
            if line.get("start") is not None:
                line["start"] = int(line["start"])
            if line.get("length") is not None:
                line["length"] = int(line["length"])
            lines.append(line)
        region.update(
            {
                "region_id": region_id,
                "kind": str(region.get("kind") or "shape"),
                "text": str(region.get("text") or ""),
                "cell_row": (
                    int(region["cell_row"])
                    if region.get("cell_row") is not None
                    else None
                ),
                "cell_column": (
                    int(region["cell_column"])
                    if region.get("cell_column") is not None
                    else None
                ),
                "rendered_lines": lines,
            }
        )
        regions.append(region)
    return regions


def _normalize_inherited_objects(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("PowerPoint inherited_objects must be a sequence")
    objects: list[dict[str, Any]] = []
    for raw_object in value:
        if not isinstance(raw_object, Mapping):
            raise ValueError("PowerPoint inherited objects must be objects")
        item = dict(raw_object)
        scope = str(item["scope"])
        shape_id = str(item["shape_id"])
        item.update(
            {
                "slide": int(item["slide"]),
                "scope": scope,
                "shape_id": shape_id,
                "object_identity": str(
                    item.get("object_identity") or f"{scope}:shape:{shape_id}"
                ),
                "object_kind": str(item.get("object_kind") or "shape"),
                "name": str(item.get("name") or ""),
                "shape_type": int(item.get("shape_type", 0)),
                "z_order_position": (
                    int(item["z_order_position"])
                    if item.get("z_order_position") is not None
                    else None
                ),
                "left_pt": _number(item.get("left_pt")),
                "top_pt": _number(item.get("top_pt")),
                "width_pt": _number(item.get("width_pt")),
                "height_pt": _number(item.get("height_pt")),
                "visible": bool(item.get("visible", True)),
                "has_text_content": bool(item.get("has_text_content", False)),
                "text": str(item.get("text") or ""),
                "area_ratio": _number(item.get("area_ratio", 0.0)),
            }
        )
        objects.append(item)
    return objects


def _normalize_slide_objects(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("PowerPoint slide_objects must be a sequence")
    objects: list[dict[str, Any]] = []
    identities: set[tuple[int, str]] = set()
    for raw_object in value:
        if not isinstance(raw_object, Mapping):
            raise ValueError("PowerPoint slide objects must be objects")
        item = dict(raw_object)
        slide = int(item["slide"])
        scope = str(item.get("scope") or "")
        if scope != "slide":
            raise ValueError("PowerPoint slide objects require scope='slide'")
        shape_id = str(item["shape_id"])
        object_identity = str(item.get("object_identity") or "")
        if not shape_id or not object_identity:
            raise ValueError(
                "PowerPoint slide objects require shape_id and object_identity"
            )
        if item.get("identity_stable", True) is not True:
            raise ValueError("PowerPoint slide object identity must be stable")
        identity_key = (slide, object_identity)
        if identity_key in identities:
            raise ValueError(
                "PowerPoint slide object identities must be unique per slide"
            )
        identities.add(identity_key)
        raw_group_path = item.get("group_path_shape_ids", [])
        if not isinstance(raw_group_path, Sequence) or isinstance(
            raw_group_path, (str, bytes, bytearray)
        ):
            raise ValueError("PowerPoint group_path_shape_ids must be a sequence")
        item.update(
            {
                "slide": slide,
                "scope": "slide",
                "shape_id": shape_id,
                "object_identity": object_identity,
                "identity_stable": True,
                "object_kind": str(item.get("object_kind") or "shape"),
                "group_path_shape_ids": [str(value) for value in raw_group_path],
                "name": str(item.get("name") or ""),
                "shape_type": int(item.get("shape_type", 0)),
                "z_order_position": (
                    int(item["z_order_position"])
                    if item.get("z_order_position") is not None
                    else None
                ),
                "left_pt": _number(item.get("left_pt")),
                "top_pt": _number(item.get("top_pt")),
                "width_pt": _number(item.get("width_pt")),
                "height_pt": _number(item.get("height_pt")),
                "visible": bool(item.get("visible", True)),
                "has_text_content": bool(item.get("has_text_content", False)),
                "text": str(item.get("text") or ""),
                "area_ratio": _number(item.get("area_ratio", 0.0)),
            }
        )
        objects.append(item)
    return objects


def _normalize_slide_object_errors(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("PowerPoint slide_object_errors must be a sequence")
    errors: list[dict[str, Any]] = []
    for raw_error in value:
        if not isinstance(raw_error, Mapping):
            raise ValueError("PowerPoint slide object errors must be objects")
        reason = str(raw_error.get("reason") or "").strip()
        if not reason:
            raise ValueError("PowerPoint slide object errors require a reason")
        errors.append(
            {
                "slide": (
                    int(raw_error["slide"])
                    if raw_error.get("slide") is not None
                    else None
                ),
                "capability": str(raw_error.get("capability") or "slide_object_bounds"),
                "reason": reason,
            }
        )
    return errors


def _runner_name(runner: object) -> str:
    return str(getattr(runner, "__name__", type(runner).__name__))


_default_pool_lock = threading.Lock()
_default_pools: dict[str, PowerPointComWorkerPool] = {}


def _default_layout_runner() -> LayoutRunner:
    backend = os.getenv(LAYOUT_BACKEND_ENV, "auto").strip().casefold()
    if backend not in LAYOUT_BACKENDS:
        raise ValueError(
            f"{LAYOUT_BACKEND_ENV} must be one of: "
            + ", ".join(sorted(LAYOUT_BACKENDS))
        )
    if backend == "auto":
        backend = "powerpoint-com" if platform.system() == "Windows" else "libreoffice"
    if backend == "powerpoint-com":
        return _default_powerpoint_com_runner()
    if backend == "libreoffice":
        return _default_libreoffice_runner()
    return _run_disabled_backend


def _default_powerpoint_com_runner() -> PowerPointComWorkerPool:
    existing = _default_pools.get("powerpoint-com")
    if existing is not None:
        return existing
    with _default_pool_lock:
        if "powerpoint-com" not in _default_pools:
            _default_pools["powerpoint-com"] = PowerPointComWorkerPool(
                runner=_run_powerpoint_com,
                max_workers=_positive_int_environment(
                    COM_MAX_WORKERS_ENV,
                    DEFAULT_COM_MAX_WORKERS,
                ),
                queue_timeout_seconds=_positive_float_environment(
                    COM_QUEUE_TIMEOUT_ENV,
                    DEFAULT_COM_QUEUE_TIMEOUT_SECONDS,
                ),
                lock_directory=Path(
                    os.getenv(COM_LOCK_DIRECTORY_ENV)
                    or (
                        Path(tempfile.gettempdir())
                        / "format-translator-powerpoint-com-slots"
                    )
                ),
            )
    return _default_pools["powerpoint-com"]


def _default_libreoffice_runner() -> PowerPointComWorkerPool:
    existing = _default_pools.get("libreoffice")
    if existing is not None:
        return existing
    with _default_pool_lock:
        if "libreoffice" not in _default_pools:
            _default_pools["libreoffice"] = PowerPointComWorkerPool(
                runner=_run_libreoffice_openability,
                max_workers=_positive_int_environment(
                    LIBREOFFICE_MAX_WORKERS_ENV,
                    DEFAULT_COM_MAX_WORKERS,
                ),
                queue_timeout_seconds=_positive_float_environment(
                    LIBREOFFICE_QUEUE_TIMEOUT_ENV,
                    DEFAULT_COM_QUEUE_TIMEOUT_SECONDS,
                ),
                lock_directory=Path(
                    os.getenv(LIBREOFFICE_LOCK_DIRECTORY_ENV)
                    or (
                        Path(tempfile.gettempdir())
                        / "format-translator-libreoffice-slots"
                    )
                ),
                worker_label="LibreOffice",
            )
    return _default_pools["libreoffice"]


def _positive_int_environment(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _positive_float_environment(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _run_disabled_backend(
    _document: Path,
    _targets: list[dict[str, object]],
) -> Mapping[str, Any]:
    return {
        "provider": "disabled",
        "available": False,
        "capabilities": [],
        "precision": "none",
        "measurements": [],
        "unavailable_reason": (f"PPTX layout QA is disabled by {LAYOUT_BACKEND_ENV}."),
    }


def _run_libreoffice_openability(
    document: Path,
    targets: list[dict[str, object]],
) -> Mapping[str, Any]:
    """Open and re-save a PPTX with isolated headless LibreOffice.

    LibreOffice does not expose PowerPoint-compatible TextRange bounds here;
    this backend deliberately reports only package openability.
    """

    started = time.perf_counter()
    timeout_seconds = _positive_float_environment(
        LIBREOFFICE_TIMEOUT_ENV,
        DEFAULT_TIMEOUT_SECONDS,
    )
    try:
        executable = resolve_soffice(os.getenv(SOFFICE_PATH_ENV))
    except LegacyConversionError as error:
        return {
            "provider": "libreoffice-headless",
            "available": False,
            "capabilities": ["package_openability"],
            "precision": "package",
            "measurements": [],
            "unavailable_reason": str(error),
            "fail_closed": True,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "timeout_seconds": timeout_seconds,
        }

    try:
        version_result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(timeout_seconds, 15.0),
        )
        version = (version_result.stdout or version_result.stderr or "").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        version = None

    with tempfile.TemporaryDirectory(prefix="pptx-libreoffice-qa-") as directory:
        workspace = Path(directory)
        profile = workspace / "profile"
        output_dir = workspace / "output"
        profile.mkdir()
        output_dir.mkdir()
        command = [
            str(executable),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--convert-to",
            "pptx:Impress MS PowerPoint 2007 XML",
            "--outdir",
            str(output_dir),
            str(document),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {
                "provider": "libreoffice-headless",
                "provider_version": version,
                "available": False,
                "capabilities": ["package_openability"],
                "precision": "package",
                "measurements": [],
                "unavailable_reason": (
                    "LibreOffice PPTX openability check timed out after "
                    f"{timeout_seconds:g} seconds."
                ),
                "fail_closed": True,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "timeout_seconds": timeout_seconds,
            }
        except OSError as error:
            return {
                "provider": "libreoffice-headless",
                "provider_version": version,
                "available": False,
                "capabilities": ["package_openability"],
                "precision": "package",
                "measurements": [],
                "unavailable_reason": (f"Could not start LibreOffice: {error}"),
                "fail_closed": True,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "timeout_seconds": timeout_seconds,
            }

        produced = output_dir / document.name
        if completed.returncode != 0 or not produced.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()
            return {
                "provider": "libreoffice-headless",
                "provider_version": version,
                "available": False,
                "capabilities": ["package_openability"],
                "precision": "package",
                "measurements": [],
                "unavailable_reason": (
                    f"LibreOffice could not open and re-save {document.name} "
                    f"(exit code {completed.returncode})"
                    + (f": {detail[:500]}" if detail else ".")
                ),
                "fail_closed": True,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "timeout_seconds": timeout_seconds,
            }

        roundtrip_error = _pptx_roundtrip_error(produced)
        if roundtrip_error is not None:
            return {
                "provider": "libreoffice-headless",
                "provider_version": version,
                "available": False,
                "capabilities": ["package_openability"],
                "precision": "package",
                "measurements": [],
                "unavailable_reason": (
                    "LibreOffice re-saved an unreadable PPTX: " + roundtrip_error
                ),
                "fail_closed": True,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "timeout_seconds": timeout_seconds,
            }
        return {
            "provider": "libreoffice-headless",
            "provider_version": version,
            "available": True,
            "capabilities": ["package_openability"],
            "precision": "package",
            "measurements": [],
            "unavailable_reason": None,
            "fail_closed": True,
            "roundtrip_sha256": _sha256(produced),
            "target_count": len(targets),
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "timeout_seconds": timeout_seconds,
        }


def _pptx_roundtrip_error(path: Path) -> str | None:
    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels",
    }
    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            missing = sorted(required - names)
            corrupt = package.testzip()
    except (OSError, zipfile.BadZipFile) as error:
        return f"not a readable ZIP package: {error}"
    if missing:
        return "missing required entries: " + ", ".join(missing)
    if corrupt:
        return f"corrupt ZIP member: {corrupt}"
    return None


def _run_powerpoint_com(
    document: Path,
    targets: list[dict[str, object]],
) -> Mapping[str, Any]:
    """Run the bundled PowerShell COM probe without interpolating user paths."""

    if platform.system() != "Windows":
        return {
            "provider": "powerpoint-com",
            "available": False,
            "measurements": [],
            "unavailable_reason": "PowerPoint COM is available only on Windows.",
        }
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        return {
            "provider": "powerpoint-com",
            "available": False,
            "measurements": [],
            "unavailable_reason": "PowerShell executable was not found.",
        }
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pptx-layout-qa-") as directory:
        workspace = Path(directory)
        input_json = workspace / "input.json"
        output_json = workspace / "output.json"
        script = workspace / "measure.ps1"
        input_json.write_text(
            json.dumps(
                {
                    "document": str(document),
                    "targets": targets,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        script.write_text(_POWERPOINT_COM_SCRIPT, encoding="utf-8-sig")
        command = [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InputJson",
            str(input_json),
            "-OutputJson",
            str(output_json),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                shell=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired:
            return {
                "provider": "powerpoint-com",
                "available": False,
                "measurements": [],
                "unavailable_reason": (
                    f"PowerPoint COM measurement timed out after "
                    f"{DEFAULT_TIMEOUT_SECONDS} seconds."
                ),
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                "attempt": 1,
            }
        if not output_json.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()
            return {
                "provider": "powerpoint-com",
                "available": False,
                "measurements": [],
                "unavailable_reason": (
                    f"PowerPoint COM probe exited with {completed.returncode}"
                    + (f": {detail[:500]}" if detail else ".")
                ),
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                "attempt": 1,
            }
        result = json.loads(output_json.read_text(encoding="utf-8-sig"))
        result.setdefault(
            "duration_ms",
            round((time.perf_counter() - started) * 1000),
        )
        result["timeout_seconds"] = DEFAULT_TIMEOUT_SECONDS
        result["attempt"] = 1
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
