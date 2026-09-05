import pytest

from pintxos.config import data_dir

# Well into the future so cookies are never seen as expired.
FUTURE_EXPIRY = 4102444800  # 2100-01-01T00:00:00Z


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own PINTXOS_DATA_DIR so nothing touches ./data."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PINTXOS_NO_SCHEDULER", "1")  # never start a real poller in tests
    # A developer's shell env can pin these; strip them so default-behavior tests
    # (filter off, no extra patterns) aren't at the mercy of the ambient environment.
    monkeypatch.delenv("PINTXOS_FILTER_ADS", raising=False)
    monkeypatch.delenv("PINTXOS_AD_TITLE_PATTERNS", raising=False)


def write_cookies(text: str) -> None:
    """Write `text` to data_dir()/cookies.txt, prepending the Netscape header if missing."""
    if "# Netscape HTTP Cookie File" not in text:
        text = "# Netscape HTTP Cookie File\n" + text
    (data_dir() / "cookies.txt").write_text(text)
