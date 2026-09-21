"""Tests for the YY.MM.N version computation in scripts/next_version.py."""

import importlib.util
from datetime import date
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "next_version", Path(__file__).parents[1] / "scripts" / "next_version.py"
)
next_version_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(next_version_module)
next_version = next_version_module.next_version


def test_ignores_other_months_and_junk_tags():
    tags = ["v26.09.1", "v26.09.2", "v26.08.7", "latest", "v1.2"]
    assert next_version(tags, date(2026, 9, 21)) == "26.09.3"


def test_no_tags_starts_at_one():
    assert next_version([], date(2026, 9, 21)) == "26.09.1"


def test_uses_numeric_max_not_lexical():
    assert next_version(["v26.09.9", "v26.09.10"], date(2026, 9, 21)) == "26.09.11"
