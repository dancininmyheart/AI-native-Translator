from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .config import load_llm_settings
from .glossary import load_glossary
from .models import JobSpec, SampleSpec
from .pipeline import (
    build_page_sample,
    cleanup_jobs,
    inspect_document,
    translate_document,
)
from .processing_provider import OpenAIProcessingProvider
from .providers import (
    FixtureProvider,
    HttpJsonTranslationProvider,
    OpenAIChatCompletionsProvider,
)
from .qa_agent import QaRepairSpec, run_qa_repair_agent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="format-translator",
        description="Format-faithful Word/PowerPoint translation core",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="Inspect a Word/PowerPoint file")
    inspect.add_argument("input", type=Path)

    translate = commands.add_parser("translate", help="Translate a complete document")
    _add_common_translation_options(translate)
    translate.add_argument("--target", required=True)
    translate.add_argument("--no-auto-repair", action="store_true")
    translate.add_argument("--qa-repair-rounds", type=int, default=2)
    translate.add_argument(
        "--enable-processing-agent",
        action="store_true",
        help="Enable LLM semantic QA and text-fit using --llm-env settings",
    )

    sample = commands.add_parser("sample", help="Build a multilingual page sample")
    _add_common_translation_options(sample)
    sample.add_argument("--page", type=int, default=1)
    sample.add_argument("--targets", nargs="+", required=True)

    repair = commands.add_parser(
        "qa-repair",
        help="Run the bounded Word/PPT QA and repair agent",
    )
    repair.add_argument("source_document", type=Path)
    repair.add_argument("candidate_document", type=Path)
    repair.add_argument("--work-dir", type=Path, required=True)
    repair.add_argument("--job-id", default=None)
    repair.add_argument("--max-rounds", type=int, default=2)

    cleanup = commands.add_parser("cleanup", help="Remove expired job directories")
    cleanup.add_argument("output_dir", type=Path)
    cleanup.add_argument("--older-than", type=int, default=7, metavar="DAYS")
    return parser


def _add_common_translation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--source", default="auto", choices=["auto", "en", "zh"])
    parser.add_argument("--glossary", type=Path)
    provider = parser.add_mutually_exclusive_group(required=True)
    provider.add_argument("--fixture", type=Path)
    provider.add_argument("--provider-url")
    provider.add_argument(
        "--llm-env",
        type=Path,
        nargs="?",
        const=Path(".env"),
        help="Use an OpenAI-compatible model configured in an env file",
    )
    parser.add_argument("--provider-endpoint", default="/translate")
    parser.add_argument("--provider-key-env", default="TRANSLATION_API_KEY")
    parser.add_argument("--provider-timeout", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=4)


def _provider(args: argparse.Namespace):
    if args.fixture:
        return FixtureProvider(args.fixture)
    if args.llm_env:
        settings = load_llm_settings(args.llm_env)
        return OpenAIChatCompletionsProvider(
            base_url=settings.base_url,
            model=settings.model,
            api_key=settings.api_key,
            timeout_seconds=args.provider_timeout,
        )
    key = os.environ.get(args.provider_key_env)
    return HttpJsonTranslationProvider(
        base_url=args.provider_url,
        endpoint=args.provider_endpoint,
        api_key=key,
        timeout_seconds=args.provider_timeout,
    )


def _processing_provider(args: argparse.Namespace):
    if not args.enable_processing_agent:
        return None
    if not args.llm_env:
        raise ValueError("--enable-processing-agent requires --llm-env")
    settings = load_llm_settings(args.llm_env)
    return OpenAIProcessingProvider(
        base_url=settings.base_url,
        model=settings.model,
        api_key=settings.api_key,
        timeout_seconds=args.provider_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_document(args.input)
        elif args.command == "cleanup":
            result = cleanup_jobs(args.output_dir, args.older_than)
        elif args.command == "translate":
            spec = JobSpec(
                job_id=args.job_id or f"tr_{uuid.uuid4().hex}",
                input_path=args.input,
                output_dir=args.output_dir,
                source_language=args.source,
                target_language=args.target,
                glossary=load_glossary(args.glossary),
                max_translation_concurrency=args.concurrency,
                auto_qa_repair=not args.no_auto_repair,
                max_qa_repair_rounds=args.qa_repair_rounds,
            )
            result = translate_document(
                spec,
                _provider(args),
                _processing_provider(args),
            )
        elif args.command == "sample":
            spec = SampleSpec(
                job_id=args.job_id or f"sample_{uuid.uuid4().hex}",
                input_path=args.input,
                output_dir=args.output_dir,
                source_language=args.source,
                target_languages=args.targets,
                page=args.page,
                glossary=load_glossary(args.glossary),
                max_translation_concurrency=args.concurrency,
            )
            result = build_page_sample(spec, _provider(args))
        elif args.command == "qa-repair":
            result = run_qa_repair_agent(
                QaRepairSpec(
                    job_id=args.job_id or f"qa_{uuid.uuid4().hex}",
                    source_document=args.source_document,
                    candidate_document=args.candidate_document,
                    work_dir=args.work_dir,
                    max_rounds=args.max_rounds,
                )
            )
        else:
            raise AssertionError(args.command)
        if hasattr(result, "model_dump"):
            payload = result.model_dump(mode="json")
        elif hasattr(result, "to_dict"):
            payload = result.to_dict()
        else:
            payload = result
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        status = getattr(result, "status", None)
        status_value = getattr(status, "value", status)
        return 0 if status is None or status_value == "COMPLETED" else 2
    except Exception as exc:
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
