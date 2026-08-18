"""Safe conversion of legacy Microsoft Office files.

The rest of the translation pipeline operates on OOXML packages.  This module
contains the deliberately small boundary that turns legacy ``.doc``/``.ppt``
files into ``.docx``/``.pptx`` without ever opening the user's source in-place.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path


class LegacyConversionError(RuntimeError):
    """LibreOffice could not produce a valid OOXML document."""


class LegacyConversionTimeout(LegacyConversionError):
    """LibreOffice exceeded the configured conversion deadline."""


SOFFICE_PATH_ENV = "FORMAT_TRANSLATOR_SOFFICE_PATH"


def resolve_soffice(executable: str | os.PathLike[str] | None = None) -> Path:
    """Return the LibreOffice command-line executable or raise a clear error."""

    configured = executable or os.getenv(SOFFICE_PATH_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        discovered = shutil.which(str(configured))
        if discovered:
            return Path(discovered).resolve()
        raise LegacyConversionError(
            f"LibreOffice executable does not exist: {candidate}"
        )

    for name in ("soffice", "soffice.com", "libreoffice"):
        discovered = shutil.which(name)
        if discovered:
            return Path(discovered).resolve()

    common_windows = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "LibreOffice"
        / "program"
        / "soffice.com"
    )
    if common_windows.is_file():
        return common_windows.resolve()

    raise LegacyConversionError(
        "LibreOffice was not found. Install LibreOffice or configure "
        "the soffice executable path."
    )


def convert_doc_to_docx(
    source_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    timeout_seconds: float = 180.0,
    soffice_path: str | os.PathLike[str] | None = None,
    profile_parent: str | os.PathLike[str] | None = None,
) -> Path:
    """Convert one ``.doc`` file to a new ``.docx`` file.

    Each invocation owns a unique LibreOffice user profile.  Conversion first
    happens in a private staging directory and the result is then moved into
    ``output_dir``.  The source is never overwritten and an existing output is
    treated as an error rather than silently replaced.
    """

    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise LegacyConversionError(f"Source DOC file does not exist: {source}")
    if source.suffix.lower() != ".doc":
        raise LegacyConversionError(
            f"Legacy conversion accepts only .doc input, got: {source.name}"
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    destination_dir = Path(output_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{source.stem}.docx"
    if destination.exists():
        raise LegacyConversionError(
            f"Refusing to overwrite an existing conversion result: {destination}"
        )

    executable = resolve_soffice(soffice_path)
    profile_base = (
        Path(profile_parent).expanduser().resolve()
        if profile_parent is not None
        else None
    )
    if profile_base is not None:
        profile_base.mkdir(parents=True, exist_ok=True)

    profile_prefix = f"format-translator-lo-{uuid.uuid4().hex}-"
    try:
        with (
            tempfile.TemporaryDirectory(
                prefix=profile_prefix, dir=profile_base
            ) as profile_name,
            tempfile.TemporaryDirectory(
                prefix="format-translator-convert-", dir=destination_dir
            ) as stage_name,
        ):
            profile = Path(profile_name).resolve()
            stage = Path(stage_name).resolve()
            profile_uri = profile.as_uri()
            command = [
                str(executable),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--norestore",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                "docx:Office Open XML Text",
                "--outdir",
                str(stage),
                str(source),
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LegacyConversionTimeout(
                    f"DOC conversion exceeded {timeout_seconds:g}s for {source.name}"
                ) from exc
            except OSError as exc:
                raise LegacyConversionError(
                    f"Could not start LibreOffice for {source.name}: {exc}"
                ) from exc

            produced = stage / f"{source.stem}.docx"
            if completed.returncode != 0 or not produced.is_file():
                detail = _command_detail(completed.stdout, completed.stderr)
                raise LegacyConversionError(
                    f"LibreOffice failed to convert {source.name} "
                    f"(exit code {completed.returncode}).{detail}"
                )
            if produced.stat().st_size == 0 or not zipfile.is_zipfile(produced):
                raise LegacyConversionError(
                    f"LibreOffice produced an invalid DOCX for {source.name}"
                )

            _validate_minimal_docx(produced)
            produced.replace(destination)
    except LegacyConversionError:
        raise
    except OSError as exc:
        raise LegacyConversionError(
            f"Could not finalize conversion of {source.name}: {exc}"
        ) from exc

    return destination


def convert_ppt_to_pptx(
    source_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    timeout_seconds: float = 180.0,
    soffice_path: str | os.PathLike[str] | None = None,
    profile_parent: str | os.PathLike[str] | None = None,
) -> Path:
    """Convert one legacy ``.ppt`` into a new ``.pptx`` package."""

    return _convert_legacy_office(
        source_path,
        output_dir,
        source_suffix=".ppt",
        destination_suffix=".pptx",
        libreoffice_filter="pptx:Impress MS PowerPoint 2007 XML",
        required_parts={
            "[Content_Types].xml",
            "_rels/.rels",
            "ppt/presentation.xml",
        },
        timeout_seconds=timeout_seconds,
        soffice_path=soffice_path,
        profile_parent=profile_parent,
    )


def _convert_legacy_office(
    source_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    source_suffix: str,
    destination_suffix: str,
    libreoffice_filter: str,
    required_parts: set[str],
    timeout_seconds: float,
    soffice_path: str | os.PathLike[str] | None,
    profile_parent: str | os.PathLike[str] | None,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise LegacyConversionError(f"Source file does not exist: {source}")
    if source.suffix.lower() != source_suffix:
        raise LegacyConversionError(
            f"Legacy conversion accepts only {source_suffix} input, "
            f"got: {source.name}"
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    destination_dir = Path(output_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{source.stem}{destination_suffix}"
    if destination.exists():
        raise LegacyConversionError(
            f"Refusing to overwrite an existing conversion result: {destination}"
        )

    executable = resolve_soffice(soffice_path)
    profile_base = (
        Path(profile_parent).expanduser().resolve()
        if profile_parent is not None
        else None
    )
    if profile_base is not None:
        profile_base.mkdir(parents=True, exist_ok=True)

    profile_prefix = f"format-translator-lo-{uuid.uuid4().hex}-"
    try:
        with (
            tempfile.TemporaryDirectory(
                prefix=profile_prefix, dir=profile_base
            ) as profile_name,
            tempfile.TemporaryDirectory(
                prefix="format-translator-convert-", dir=destination_dir
            ) as stage_name,
        ):
            profile = Path(profile_name).resolve()
            stage = Path(stage_name).resolve()
            command = [
                str(executable),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--norestore",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                libreoffice_filter,
                "--outdir",
                str(stage),
                str(source),
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LegacyConversionTimeout(
                    f"Office conversion exceeded {timeout_seconds:g}s "
                    f"for {source.name}"
                ) from exc
            except OSError as exc:
                raise LegacyConversionError(
                    f"Could not start LibreOffice for {source.name}: {exc}"
                ) from exc

            produced = stage / f"{source.stem}{destination_suffix}"
            if completed.returncode != 0 or not produced.is_file():
                detail = _command_detail(completed.stdout, completed.stderr)
                raise LegacyConversionError(
                    f"LibreOffice failed to convert {source.name} "
                    f"(exit code {completed.returncode}).{detail}"
                )
            _validate_minimal_ooxml(produced, required_parts)
            produced.replace(destination)
    except LegacyConversionError:
        raise
    except OSError as exc:
        raise LegacyConversionError(
            f"Could not finalize conversion of {source.name}: {exc}"
        ) from exc
    return destination


def _command_detail(stdout: str | None, stderr: str | None) -> str:
    pieces = [piece.strip() for piece in (stderr, stdout) if piece and piece.strip()]
    if not pieces:
        return ""
    detail = " | ".join(pieces)
    if len(detail) > 2_000:
        detail = detail[:2_000] + "…"
    return f" LibreOffice output: {detail}"


def _validate_minimal_docx(path: Path) -> None:
    required = {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
    }
    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            missing = sorted(required - names)
            corrupt = package.testzip()
    except (OSError, zipfile.BadZipFile) as exc:
        raise LegacyConversionError(
            f"Converted file is not a readable DOCX package: {path}"
        ) from exc
    if missing:
        raise LegacyConversionError(
            "Converted DOCX is missing required package entries: " + ", ".join(missing)
        )
    if corrupt:
        raise LegacyConversionError(
            f"Converted DOCX contains a corrupt ZIP member: {corrupt}"
        )


def _validate_minimal_ooxml(path: Path, required: set[str]) -> None:
    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
            missing = sorted(required - names)
            corrupt = package.testzip()
    except (OSError, zipfile.BadZipFile) as exc:
        raise LegacyConversionError(
            f"Converted file is not a readable OOXML package: {path}"
        ) from exc
    if missing:
        raise LegacyConversionError(
            "Converted OOXML package is missing required entries: "
            + ", ".join(missing)
        )
    if corrupt:
        raise LegacyConversionError(
            f"Converted OOXML package contains a corrupt ZIP member: {corrupt}"
        )


# Backwards-friendly name for callers that treat conversion as a generic action.
convert_legacy_doc = convert_doc_to_docx
convert_legacy_ppt = convert_ppt_to_pptx
