"""Tests for pintxos.stats."""

from __future__ import annotations

import pytest

from pintxos.stats import WORDS_PER_MINUTE, format_stats, reading_minutes, word_count


def test_word_count_empty() -> None:
    assert word_count("") == 0


def test_word_count_whitespace_only() -> None:
    assert word_count("   \n\t ") == 0


def test_word_count_multiple_words() -> None:
    assert word_count("one two\nthree   four") == 4


@pytest.mark.parametrize(
    "words,expected",
    [
        (0, 1),
        (1, 1),
        (238, 1),
        (239, 2),
        (2380, 10),
    ],
)
def test_reading_minutes(words: int, expected: int) -> None:
    assert reading_minutes(words) == expected


def test_format_stats_1200_words() -> None:
    assert format_stats(1200) == "About 1,200 words · 6 min read"


def test_format_stats_42_words() -> None:
    assert format_stats(42) == "About 42 words · 1 min read"


def test_words_per_minute_constant() -> None:
    assert WORDS_PER_MINUTE == 238
