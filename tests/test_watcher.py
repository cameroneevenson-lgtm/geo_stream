from __future__ import annotations

import pytest

import watch_and_run


def test_watcher_command_uses_shared_interpreter_and_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEO_STREAM_PORT", "8765")

    command = watch_and_run._command()

    assert command[0] == watch_and_run.sys.executable
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert command[command.index("--server.port") + 1] == "8765"
    assert command[command.index("--server.fileWatcherType") + 1] == "none"


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_watcher_rejects_invalid_ports(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("GEO_STREAM_PORT", value)
    with pytest.raises(ValueError, match="1 to 65535"):
        watch_and_run._command()
