"""Deterministic article stats: word count and estimated reading time."""

from __future__ import annotations

import math

# Average adult silent reading rate (Brysbaert 2019 meta-analysis).
WORDS_PER_MINUTE = 238


def word_count(text: str) -> int:
    """Count words by whitespace splitting. Empty/whitespace-only text is 0."""
    return len(text.split())


def reading_minutes(words: int) -> int:
    """Estimate reading time in minutes, rounded up, never less than 1."""
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


def format_stats(words: int) -> str:
    """Render a human-readable word count and reading-time summary."""
    return f"About {words:,} words · {reading_minutes(words)} min read"
