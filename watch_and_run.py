"""Restart the local Streamlit process when project source files change."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
WATCH_EXTENSIONS = {".bat", ".md", ".py", ".toml", ".txt"}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "_runtime",
    "__pycache__",
}
LOG = logging.getLogger("geo_stream.watcher")


def _interval_seconds() -> float:
    """Return the configured polling interval with a safe lower bound."""

    try:
        return max(0.25, float(os.getenv("GEO_STREAM_WATCH_INTERVAL", "1.0")))
    except ValueError:
        return 1.0


def _iter_files() -> list[Path]:
    """Return relevant project files in a deterministic order."""

    files: list[Path] = []
    for path in APP_ROOT.rglob("*"):
        if any(part in IGNORED_DIRECTORY_NAMES for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in WATCH_EXTENSIONS:
            files.append(path)
    return sorted(files)


def _snapshot() -> dict[Path, tuple[int, int]]:
    """Capture modification times and sizes for watched files."""

    snapshot: dict[Path, tuple[int, int]] = {}
    for path in _iter_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[path] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _command() -> list[str]:
    host = "127.0.0.1"
    raw_port = os.getenv("GEO_STREAM_PORT", "8501")
    try:
        port_number = int(raw_port)
    except ValueError as exc:
        raise ValueError(
            "GEO_STREAM_PORT must be an integer from 1 to 65535."
        ) from exc
    if not 1 <= port_number <= 65535:
        raise ValueError(
            "GEO_STREAM_PORT must be an integer from 1 to 65535."
        )
    port = str(port_number)
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_ROOT / "app.py"),
        "--server.address",
        host,
        "--server.port",
        port,
        "--server.fileWatcherType",
        "none",
    ]


def _start() -> subprocess.Popen[bytes]:
    command = _command()
    LOG.info("Starting Streamlit at http://%s:%s", command[-5], command[-3])
    return subprocess.Popen(command, cwd=APP_ROOT)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    LOG.info("Stopping Streamlit")
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        LOG.warning("Streamlit did not stop within 8 seconds; killing it")
        process.kill()
        process.wait(timeout=8)


def main() -> int:
    """Watch project files and supervise one Streamlit child process."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    interval = _interval_seconds()
    previous = _snapshot()
    try:
        process = _start()
    except ValueError as exc:
        LOG.error("%s", exc)
        return 2

    try:
        while True:
            time.sleep(interval)
            current = _snapshot()
            changed = current != previous
            exited = process.poll() is not None

            if changed:
                LOG.info("Project change detected; restarting Streamlit")
            elif exited:
                LOG.warning(
                    "Streamlit exited with code %s; restarting",
                    process.returncode,
                )

            if changed or exited:
                _stop(process)
                time.sleep(min(interval, 1.0))
                process = _start()
                previous = current
    except KeyboardInterrupt:
        LOG.info("Watcher interrupted")
    finally:
        _stop(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
