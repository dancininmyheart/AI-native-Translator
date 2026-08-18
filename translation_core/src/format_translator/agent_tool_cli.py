"""Single-shot JSON/JSONL CLI for the controlled Pi agent tools.

Protocol::

    python -m format_translator.agent_tool_cli <tool> \
        --job-root /data/jobs --job-id job_123

The request-specific object is read from stdin.  Stdout contains exactly one
compact JSON object; operational diagnostics use stderr and stable exit codes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from .agent_contracts import REQUEST_MODELS
from .agent_tools import (
    AgentConfigurationError,
    AgentPathError,
    AgentStateError,
    AgentToolFailure,
    invoke_tool,
)

EXIT_OK = 0
EXIT_REQUEST_INVALID = 2
EXIT_PATH_REJECTED = 3
EXIT_STATE_CONFLICT = 4
EXIT_EXECUTION_FAILED = 5
MAX_STDIN_BYTES = 1024 * 1024


class _CliInputError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliInputError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="format-translator-agent-tool")
    parser.add_argument("tool", choices=sorted(REQUEST_MODELS))
    parser.add_argument("--job-root", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    tool: str | None = None
    job_id = "invalid-job"
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
        tool = str(args.tool)
        job_id = str(args.job_id) or "invalid-job"
        raw = input_stream.read(MAX_STDIN_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
            raise _CliInputError("stdin JSON exceeds the 1 MiB tool limit")
        if not raw.strip():
            raise _CliInputError("stdin must contain one JSON object")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise _CliInputError("stdin is not one valid JSON object") from error
        if not isinstance(payload, dict):
            raise _CliInputError("stdin JSON root must be an object")
        if "job_root" in payload or "job_id" in payload:
            raise _CliInputError(
                "job_root and job_id are accepted only as trusted CLI arguments"
            )
        payload = {
            **payload,
            "job_root": args.job_root,
            "job_id": args.job_id,
        }
        try:
            REQUEST_MODELS[tool].model_validate(payload)
        except ValidationError as error:
            _emit_error(
                output_stream,
                error_stream,
                tool=tool,
                job_id=job_id,
                code="INVALID_REQUEST",
                message="agent tool request failed JSON or schema validation",
                exception=error,
            )
            return EXIT_REQUEST_INVALID
        response = invoke_tool(tool, payload)
        _emit(output_stream, response.model_dump(mode="json"))
        return EXIT_OK
    except _CliInputError as error:
        _emit_error(
            output_stream,
            error_stream,
            tool=tool,
            job_id=job_id,
            code="INVALID_REQUEST",
            message="agent tool request failed JSON or schema validation",
            exception=error,
        )
        return EXIT_REQUEST_INVALID
    except AgentPathError as error:
        _emit_error(
            output_stream,
            error_stream,
            tool=tool,
            job_id=job_id,
            code=error.code,
            message=_safe_message(error),
            exception=error,
        )
        return EXIT_PATH_REJECTED
    except (AgentStateError, FileNotFoundError, FileExistsError) as error:
        _emit_error(
            output_stream,
            error_stream,
            tool=tool,
            job_id=job_id,
            code=getattr(error, "code", "JOB_STATE_CONFLICT"),
            message=_safe_message(error),
            exception=error,
        )
        return EXIT_STATE_CONFLICT
    except (AgentConfigurationError, AgentToolFailure) as error:
        _emit_error(
            output_stream,
            error_stream,
            tool=tool,
            job_id=job_id,
            code=error.code,
            message=_safe_message(error),
            exception=error,
        )
        return EXIT_EXECUTION_FAILED
    except Exception as error:  # noqa: BLE001 - CLI must emit one fail-closed envelope.
        _emit_error(
            output_stream,
            error_stream,
            tool=tool,
            job_id=job_id,
            code="TOOL_EXECUTION_FAILED",
            message="agent tool execution failed",
            exception=error,
        )
        return EXIT_EXECUTION_FAILED


def _emit(stream: TextIO, payload: MappingLike) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")
    stream.flush()


def _emit_error(
    output: TextIO,
    error_output: TextIO,
    *,
    tool: str | None,
    job_id: str,
    code: str,
    message: str,
    exception: BaseException,
) -> None:
    _emit(
        output,
        {
            "tool": tool,
            "job_id": job_id,
            "status": "FAILED",
            "summary": {},
            "artifacts": [],
            "issues": [],
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
            },
        },
    )
    error_output.write(f"[{code}] {type(exception).__name__}\n")
    error_output.flush()


def _safe_message(error: BaseException) -> str:
    value = " ".join(str(error).split())
    return (value or type(error).__name__)[:500]


MappingLike = dict[str, Any]


if __name__ == "__main__":
    raise SystemExit(main())
