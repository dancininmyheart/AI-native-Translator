from __future__ import annotations

import pytest

from format_translator.protection import protect_text, restore_text


def test_compact_financial_amounts_are_protected_and_restored_as_whole_tokens() -> None:
    source = (
        "Reserves fell from $569bn to $241.37bn; "
        "GDP was N59.11trn and revenue was N14.815bn."
    )

    protected = protect_text(source)

    assert [token.kind for token in protected.tokens] == [
        "financial_amount",
        "financial_amount",
        "financial_amount",
        "financial_amount",
    ]
    assert [token.value for token in protected.tokens] == [
        "$569bn",
        "$241.37bn",
        "N59.11trn",
        "N14.815bn",
    ]
    for token in protected.tokens:
        assert token.value not in protected.protected_text

    translated_with_placeholders = (
        protected.protected_text.replace("Reserves fell from", "储备从")
        .replace("GDP was", "国内生产总值为")
        .replace("revenue was", "收入为")
    )
    restored = restore_text(translated_with_placeholders, protected.tokens)

    for amount in ("$569bn", "$241.37bn", "N59.11trn", "N14.815bn"):
        assert restored.count(amount) == 1


@pytest.mark.parametrize(
    "period",
    [
        "(Q1 '26)",
        "Q1 '26",
        "Q4 2025",
        "FY2026",
        "(q2 '27)",
        "fy2027",
    ],
)
def test_business_period_is_protected_and_restored_as_one_token(
    period: str,
) -> None:
    source = f"Proshare Analysts {period}"

    protected = protect_text(source)

    assert [(token.kind, token.value) for token in protected.tokens] == [
        ("business_period", period)
    ]
    assert period not in protected.protected_text
    translated_with_placeholder = protected.protected_text.replace(
        "Proshare Analysts", "Proshare 分析师"
    )
    assert restore_text(translated_with_placeholder, protected.tokens) == (
        f"Proshare 分析师 {period}"
    )


@pytest.mark.parametrize(
    ("source", "expected_tokens"),
    [
        ("The letter Q remains Q.", []),
        ("Calendar year 2025", [("number", "2025")]),
        ("Q 2025", [("number", "2025")]),
        ("Q5 2025", [("number", "2025")]),
        ("As of 2026-07-29", [("date", "2026-07-29")]),
    ],
)
def test_business_period_does_not_consume_unrelated_literals(
    source: str,
    expected_tokens: list[tuple[str, str]],
) -> None:
    protected = protect_text(source)

    assert [(token.kind, token.value) for token in protected.tokens] == (
        expected_tokens
    )
    assert restore_text(protected.protected_text, protected.tokens) == source


@pytest.mark.parametrize(
    "rate",
    [
        "N1,300",
        "N1,440-N1500/$",
        "N1,475/$",
        "₦1,300",
        "N1500-N1600/$",
    ],
)
def test_naira_exchange_rate_is_protected_and_restored_as_one_token(
    rate: str,
) -> None:
    source = f"Exchange rate {rate}"

    protected = protect_text(source)

    assert [(token.kind, token.value) for token in protected.tokens] == [
        ("naira_exchange_rate", rate)
    ]
    translated_with_placeholder = protected.protected_text.replace(
        "Exchange rate", "汇率"
    )
    assert restore_text(translated_with_placeholder, protected.tokens) == (
        f"汇率 {rate}"
    )


@pytest.mark.parametrize("label", ["1.", "8.", "10.", " 9. "])
def test_standalone_numeric_sequence_label_is_protected_as_one_token(
    label: str,
) -> None:
    protected = protect_text(label)

    assert [(token.kind, token.value) for token in protected.tokens] == [
        ("numeric_sequence_label", label)
    ]
    assert restore_text(protected.protected_text, protected.tokens) == label


@pytest.mark.parametrize(
    ("source", "excluded_kind"),
    [
        ("Model N1 remains unchanged.", "naira_exchange_rate"),
        ("NGN 1,300", "naira_exchange_rate"),
        ("Version 1.", "numeric_sequence_label"),
        ("The rate is 8. Growth continued.", "numeric_sequence_label"),
        ("1.5", "numeric_sequence_label"),
        ("0.", "numeric_sequence_label"),
        ("11.", "numeric_sequence_label"),
        ("8. item", "numeric_sequence_label"),
    ],
)
def test_rate_and_sequence_patterns_do_not_consume_lookalikes(
    source: str,
    excluded_kind: str,
) -> None:
    protected = protect_text(source)

    assert all(token.kind != excluded_kind for token in protected.tokens)
    assert restore_text(protected.protected_text, protected.tokens) == source


@pytest.mark.parametrize(
    ("source", "expected_kind", "expected_value"),
    [
        ("Total: 1,250.00", "number", "1,250.00"),
        ("Growth: 12.5%", "number", "12.5%"),
        ("As of 2026-07-29", "date", "2026-07-29"),
        ("Capacity: 42GB", "number", "42GB"),
    ],
)
def test_existing_literal_protection_still_round_trips(
    source: str,
    expected_kind: str,
    expected_value: str,
) -> None:
    protected = protect_text(source)

    assert [(token.kind, token.value) for token in protected.tokens] == [
        (expected_kind, expected_value)
    ]
    assert restore_text(protected.protected_text, protected.tokens) == source
