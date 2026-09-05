import os

import pytest

from pintxos import cli


@pytest.fixture(autouse=True)
def _clean_host_port_env(monkeypatch):
    monkeypatch.delenv("PINTXOS_HOST", raising=False)
    monkeypatch.delenv("PINTXOS_PORT", raising=False)
    # cli.main() may call dotenv.load_dotenv(), which mutates os.environ directly
    # (monkeypatch cannot track/undo that). Explicitly scrub these vars afterward so
    # a .env picked up by one test can't leak into the next.
    yield
    os.environ.pop("PINTXOS_HOST", None)
    os.environ.pop("PINTXOS_PORT", None)


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    def fake_run(app, host=None, port=None):
        calls.append({"app": app, "host": host, "port": port})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return calls


def test_defaults(recorder):
    cli.main([])
    assert len(recorder) == 1
    assert recorder[0]["host"] == "127.0.0.1"
    assert recorder[0]["port"] == 8000


def test_env_override(monkeypatch, recorder):
    monkeypatch.setenv("PINTXOS_HOST", "0.0.0.0")
    monkeypatch.setenv("PINTXOS_PORT", "9001")
    cli.main([])
    assert recorder[0]["host"] == "0.0.0.0"
    assert recorder[0]["port"] == 9001


def test_cli_flags_override_env(monkeypatch, recorder):
    monkeypatch.setenv("PINTXOS_PORT", "9001")
    cli.main(["--port", "9002"])
    assert recorder[0]["port"] == 9002


def test_invalid_port_non_integer(monkeypatch, recorder):
    monkeypatch.setenv("PINTXOS_PORT", "abc")
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    assert exc_info.value.code == 2
    assert recorder == []


def test_invalid_port_out_of_range(monkeypatch, recorder):
    monkeypatch.setenv("PINTXOS_PORT", "70000")
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    assert exc_info.value.code == 2
    assert recorder == []


def test_dotenv_loaded_from_cwd(monkeypatch, tmp_path, recorder):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PINTXOS_PORT=9123\n")
    cli.main([])
    assert recorder[0]["port"] == 9123


def test_dotenv_does_not_override_real_env(monkeypatch, tmp_path, recorder):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PINTXOS_PORT=9123\n")
    monkeypatch.setenv("PINTXOS_PORT", "9999")
    cli.main([])
    assert recorder[0]["port"] == 9999


def test_no_dotenv_falls_back_to_defaults(monkeypatch, tmp_path, recorder):
    monkeypatch.chdir(tmp_path)
    cli.main([])
    assert recorder[0]["host"] == "127.0.0.1"
    assert recorder[0]["port"] == 8000


def test_dotenv_in_parent_directory_is_ignored(monkeypatch, tmp_path, recorder):
    (tmp_path / ".env").write_text("PINTXOS_PORT=9123\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    cli.main([])
    assert recorder[0]["port"] == 8000
