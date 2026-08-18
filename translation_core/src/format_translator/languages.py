from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_SOURCE_LANGUAGES = {"en", "zh", "ja", "ko", "fr", "de", "ru"}
SUPPORTED_TARGET_LANGUAGES = {"en", "zh", "ja", "ko", "fr", "de", "ru"}

_LANGUAGE_TAG_RE = re.compile(
    r"^(?:[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)$",
    re.IGNORECASE,
)
_LANGUAGE_NAMES = {
    "chinese": "zh",
    "mandarin": "zh",
    "english": "en",
    "japanese": "ja",
    "korean": "ko",
    "french": "fr",
    "german": "de",
    "russian": "ru",
}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u052f]")
_LATIN_RE = re.compile(r"[A-Za-z]")


@dataclass(frozen=True, slots=True)
class LanguageDetection:
    language: str
    family: str
    confidence: float
    method: str
    ambiguous: bool = False


def normalize_source_language(value: str, *, allow_auto: bool = True) -> str:
    """Normalize a safe source-language label while retaining foreign tags.

    Known human-readable names and BCP-47 region variants collapse to their
    provider-facing primary language.  Other syntactically safe primary tags
    remain available so auto-target routing is not limited to a hard-coded
    language list.
    """

    selected = value.strip()
    if allow_auto and selected.casefold() == "auto":
        return "auto"
    named = _LANGUAGE_NAMES.get(selected.casefold())
    if named is not None:
        return named
    if not _LANGUAGE_TAG_RE.fullmatch(selected):
        raise ValueError("source language must be a BCP-47-like tag or known name")
    canonical = selected.casefold()
    primary = canonical.split("-", 1)[0]
    return primary if primary in {"en", "zh"} else canonical


def normalize_target_language(value: str, *, allow_auto: bool = False) -> str:
    """Normalize a supported target label, optionally retaining ``auto``."""

    selected = value.strip()
    if selected.casefold() == "auto":
        if allow_auto:
            return "auto"
        raise ValueError("target language must be explicit")
    canonical = normalize_source_language(selected, allow_auto=False)
    primary = canonical.split("-", 1)[0]
    if primary not in SUPPORTED_TARGET_LANGUAGES:
        raise ValueError(
            f"Unsupported target language {value!r}; expected one of "
            f"{sorted(SUPPORTED_TARGET_LANGUAGES)}"
        )
    return primary


def resolve_target_language(source: str, requested_target: str) -> str:
    """Resolve ``auto`` to English for Chinese and Chinese for all foreign text."""

    canonical_source = normalize_source_language(source, allow_auto=False)
    canonical_target = normalize_target_language(requested_target, allow_auto=True)
    if canonical_target != "auto":
        return canonical_target
    return "en" if canonical_source.split("-", 1)[0] == "zh" else "zh"


def detect_source_language_details(text: str) -> LanguageDetection:
    """Detect script families needed by automatic target routing.

    Latin text intentionally remains ``und`` because script alone cannot tell
    English, French, German, and other Latin-script languages apart.  Han-only
    text defaults to Chinese with reduced confidence and an explicit ambiguity
    marker because Japanese text containing only kanji is indistinguishable
    without a statistical language model.
    """

    han = len(_CJK_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    hangul = len(_HANGUL_RE.findall(text))
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if kana:
        return LanguageDetection("ja", "japanese", 0.99, "script")
    if hangul:
        return LanguageDetection("ko", "hangul", 0.99, "script")
    if cyrillic:
        return LanguageDetection("ru", "cyrillic", 0.98, "script")
    if han and han >= max(1, latin // 3):
        return LanguageDetection(
            "zh",
            "han",
            0.60,
            "han_ambiguous_zh_default",
            ambiguous=True,
        )
    if latin:
        return LanguageDetection("und", "latin", 0.35, "script")
    if any(unicodedata.category(character).startswith("L") for character in text):
        return LanguageDetection("und", "foreign", 0.20, "unicode_letter")
    raise ValueError("Cannot detect source language from empty/non-letter text")


def detect_source_language(text: str) -> str:
    return detect_source_language_details(text).language


def validate_language_pair(source: str, target: str) -> None:
    canonical_source = normalize_source_language(source, allow_auto=False)
    canonical_target = normalize_target_language(target, allow_auto=False)
    if canonical_source.split("-", 1)[0] == canonical_target:
        raise ValueError("Source and target languages must differ")


def discover_system_fonts() -> dict[str, Path]:
    candidates = {
        "en": [
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ],
        "zh": [
            Path(r"C:\Windows\Fonts\msyh.ttc"),
            Path(r"C:\Windows\Fonts\simhei.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ],
        "ja": [
            Path(r"C:\Windows\Fonts\YuGothR.ttc"),
            Path(r"C:\Windows\Fonts\msgothic.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ],
        "ko": [
            Path(r"C:\Windows\Fonts\malgun.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ],
        "fr": [
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ],
        "de": [
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ],
        "ru": [
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ],
    }
    result: dict[str, Path] = {}
    for language, paths in candidates.items():
        match = next((path for path in paths if path.exists()), None)
        if match:
            result[language] = match
    return result
