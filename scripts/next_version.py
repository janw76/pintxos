#!/usr/bin/env python3
"""Compute the next release version in the YY.MM.N scheme.

Versions are always three parts: two-digit year, two-digit month and a
sequence number N that restarts at 1 in every new month. Git tags carry a
leading "v" (v26.09.1); the printed version does not.
"""

from __future__ import annotations

import re
import subprocess
from datetime import date

TAG_RE = re.compile(r"^v(\d\d)\.(\d\d)\.(\d+)$")


def next_version(tags: list[str], today: date) -> str:
    """Return the next YY.MM.N version for ``today`` given existing ``tags``."""
    prefix = today.strftime("%y.%m")
    numbers = [
        int(m.group(3))
        for tag in tags
        if (m := TAG_RE.match(tag.strip())) and f"{m.group(1)}.{m.group(2)}" == prefix
    ]
    return f"{prefix}.{max(numbers) + 1 if numbers else 1}"


def main() -> None:
    out = subprocess.run(
        ["git", "tag", "--list"], capture_output=True, text=True, check=True
    ).stdout
    print(next_version(out.splitlines(), date.today()))


if __name__ == "__main__":
    main()
