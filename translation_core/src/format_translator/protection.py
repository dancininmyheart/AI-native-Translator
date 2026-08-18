"""Protection and restoration of literals that translation must not alter."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .models import ProtectedText, ProtectedToken


class ProtectionError(ValueError):
    """Base error for placeholder protocol failures."""


class ProtectionValidationError(ProtectionError):
    """Raised when translated text loses or duplicates protected placeholders."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True, slots=True)
class ProtectionPattern:
    kind: str
    pattern: re.Pattern[str]


# Priority matters.  More specific patterns are selected before the generic
# number pattern, and overlaps are resolved deterministically.
DEFAULT_PATTERNS: tuple[ProtectionPattern, ...] = (
    ProtectionPattern(
        "url",
        re.compile(
            r"(?i)\b(?:https?://|ftp://|www\.)[^\s<>{}\[\]\"']*[^\s<>{}\[\]\"'.,;:!?)]"
        ),
    ),
    ProtectionPattern(
        "email",
        re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b"),
    ),
    ProtectionPattern(
        "numeric_sequence_label",
        re.compile(r"\A[ \t]*(?:10|[1-9])\.[ \t]*\Z"),
    ),
    ProtectionPattern(
        "business_period",
        re.compile(
            r"(?i)(?:"
            r"\([ \t]*(?:Q[1-4][ \t]+(?:['’]\d{2}|\d{4})|FY\d{4})"
            r"[ \t]*\)"
            r"|(?<!\w)(?:Q[1-4][ \t]+(?:['’]\d{2}|\d{4})|FY\d{4})"
            r"(?!\w)"
            r")"
        ),
    ),
    ProtectionPattern(
        "naira_exchange_rate",
        re.compile(
            r"(?<!\w)(?:N|₦)"
            r"(?=[\d,.]*(?:,|[ \t]*[-–—]|[ \t]*/[ \t]*\$))"
            r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
            r"(?:[ \t]*[-–—][ \t]*(?:N|₦)?"
            r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
            r")?"
            r"(?:[ \t]*/[ \t]*\$)?"
            r"(?!\w)"
        ),
    ),
    ProtectionPattern(
        "date",
        re.compile(
            r"(?<!\w)(?:"
            r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
            r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
            r")(?!\w)"
        ),
    ),
    ProtectionPattern(
        "time",
        re.compile(r"(?<!\w)(?:[01]?\d|2[0-3]):[0-5]\d(?:\s?[APap][Mm])?(?!\w)"),
    ),
    ProtectionPattern(
        "financial_amount",
        re.compile(
            r"(?<![\w])"
            r"(?:"
            r"(?:US\$|C\$|A\$|HK\$|NZ\$|SG\$|[$€£¥￥₦]|N|"
            r"(?i:USD|EUR|GBP|JPY|CNY|RMB|NGN|AUD|CAD|HKD|NZD|SGD))"
            r"\s*"
            r")?"
            r"[+\-−]?(?:\d{1,3}(?:[,\s]\d{3})+|\d+)(?:[.,]\d+)?"
            r"\s*(?i:trn|tn|bn|mln|mn)"
            r"(?![\w])"
        ),
    ),
    ProtectionPattern(
        "number",
        re.compile(
            r"(?<![\w])"
            r"(?:[$€£¥￥]\s*)?"
            r"[+\-−]?(?:\d{1,3}(?:[,\s]\d{3})+|\d+)(?:[.,]\d+)?"
            r"(?:\s?(?:%|％|‰|bps?|[KMBTkmbt]|kg|g|mg|km|m|cm|mm|GB|MB|KB|Hz|MHz|GHz|°C|°F))?"
            r"(?![\w])"
        ),
    ),
)

_PLACEHOLDER_RE = re.compile(r"__FT_PROTECTED_\d{4,}__")


def _compile_extra_patterns(
    extra_patterns: Mapping[str, str | re.Pattern[str]] | None,
) -> list[ProtectionPattern]:
    compiled: list[ProtectionPattern] = []
    for kind, pattern in (extra_patterns or {}).items():
        compiled.append(
            ProtectionPattern(
                kind, re.compile(pattern) if isinstance(pattern, str) else pattern
            )
        )
    return compiled


def _select_non_overlapping_matches(
    text: str,
    patterns: Iterable[ProtectionPattern],
) -> list[tuple[int, int, str, str]]:
    candidates: list[tuple[int, int, int, str, str]] = []
    for priority, protection_pattern in enumerate(patterns):
        for match in protection_pattern.pattern.finditer(text):
            if match.start() != match.end():
                candidates.append(
                    (
                        match.start(),
                        match.end(),
                        priority,
                        protection_pattern.kind,
                        match.group(0),
                    )
                )

    # Earliest match wins.  At the same position prefer pattern priority, then
    # the longest match.  This keeps date/URL matches intact.
    candidates.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))
    selected: list[tuple[int, int, str, str]] = []
    occupied_until = -1
    for start, end, _priority, kind, value in candidates:
        if start >= occupied_until:
            selected.append((start, end, kind, value))
            occupied_until = end
    return selected


def protect_text(
    text: str,
    *,
    extra_patterns: Mapping[str, str | re.Pattern[str]] | None = None,
) -> ProtectedText:
    """Replace protected literals with stable, provider-safe placeholders.

    Extra patterns are evaluated before built-ins and therefore take precedence
    when they overlap.
    """

    patterns = [*_compile_extra_patterns(extra_patterns), *DEFAULT_PATTERNS]
    matches = _select_non_overlapping_matches(text, patterns)
    tokens: list[ProtectedToken] = []
    chunks: list[str] = []
    cursor = 0
    for index, (start, end, kind, value) in enumerate(matches):
        placeholder = f"__FT_PROTECTED_{index:04d}__"
        chunks.append(text[cursor:start])
        chunks.append(placeholder)
        tokens.append(
            ProtectedToken(
                placeholder=placeholder,
                value=value,
                kind=kind,
                start=start,
                end=end,
            )
        )
        cursor = end
    chunks.append(text[cursor:])
    return ProtectedText(
        original_text=text,
        protected_text="".join(chunks),
        tokens=tokens,
    )


def validate_protected_tokens(
    text: str,
    tokens: Iterable[ProtectedToken],
) -> list[str]:
    """Return protocol errors found in translated protected text."""

    expected = list(tokens)
    expected_names = {token.placeholder for token in expected}
    errors: list[str] = []

    for token in expected:
        count = text.count(token.placeholder)
        if count == 0:
            errors.append(f"missing protected token {token.placeholder}")
        elif count > 1:
            errors.append(f"duplicated protected token {token.placeholder}")

    actual_names = set(_PLACEHOLDER_RE.findall(text))
    for unexpected in sorted(actual_names - expected_names):
        errors.append(f"unexpected protected token {unexpected}")
    return errors


def restore_text(
    text: str,
    tokens: Iterable[ProtectedToken],
    *,
    strict: bool = True,
) -> str:
    """Restore original literals after validating placeholders.

    In non-strict mode all known placeholders are restored even if some are
    missing or duplicated; callers can use this for diagnostics only.
    """

    materialized = list(tokens)
    errors = validate_protected_tokens(text, materialized)
    if strict and errors:
        raise ProtectionValidationError(errors)
    restored = text
    for token in materialized:
        restored = restored.replace(token.placeholder, token.value)
    return restored


def validate_restored_values(
    source_tokens: Iterable[ProtectedToken],
    restored_text: str,
) -> list[str]:
    """Check that each protected source value survived restoration."""

    errors: list[str] = []
    for token in source_tokens:
        if restored_text.count(token.value) == 0:
            errors.append(f"missing protected {token.kind} value {token.value!r}")
    return errors
