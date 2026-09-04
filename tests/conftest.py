import pytest


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
