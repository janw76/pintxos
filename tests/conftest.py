import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own PINTXOS_DATA_DIR so nothing touches ./data."""
    monkeypatch.setenv("PINTXOS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PINTXOS_NO_SCHEDULER", "1")  # never start a real poller in tests


@pytest.fixture
def quiet_engine(monkeypatch):
    """Replace the engine's injected poll_fn with a no-op that finishes instantly.

    Exercises the real queue/worker path (unlike monkeypatching poll_feed,
    which the engine already captured a reference to at import time).
    """
    import pintxos.app as app_module

    def _quiet_poll(feed_id, reporter):
        reporter.finished(feed_id, 0, 0)
        return True

    monkeypatch.setattr(app_module.engine, "_poll_fn", _quiet_poll)
