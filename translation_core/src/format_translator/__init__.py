"""Format-faithful document translation core."""

from .pipeline import (
    build_page_sample,
    cleanup_jobs,
    inspect_document,
    translate_document,
)
from .pptx_engine import PptxEngine, run_pptx_qa
from .pptx_repair_adapter import (
    PptxRepairAdaptation,
    PptxRepairAdapterError,
    adapt_pptx_repairs,
    execute_pptx_repair_adapter,
)
from .pptx_rendered_layout_qa import (
    PowerPointComWorkerPool,
    compare_pptx_rendered_layout,
    pptx_text_shape_targets,
    run_pptx_qa_with_rendered_layout,
    run_pptx_rendered_layout_qa,
)
from .qa_agent import run_qa_repair_agent, run_qa_repair_agent_async
from .word_qa import run_docx_qa

__all__ = [
    "PptxRepairAdaptation",
    "PptxRepairAdapterError",
    "PowerPointComWorkerPool",
    "build_page_sample",
    "cleanup_jobs",
    "inspect_document",
    "PptxEngine",
    "adapt_pptx_repairs",
    "compare_pptx_rendered_layout",
    "execute_pptx_repair_adapter",
    "pptx_text_shape_targets",
    "run_docx_qa",
    "run_pptx_qa",
    "run_pptx_qa_with_rendered_layout",
    "run_pptx_rendered_layout_qa",
    "run_qa_repair_agent",
    "run_qa_repair_agent_async",
    "translate_document",
]
