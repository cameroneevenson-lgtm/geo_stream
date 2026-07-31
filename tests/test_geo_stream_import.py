"""Importability of geo_stream under the shared Tools path layout.

The shared venv puts ``C:\\Tools`` on ``sys.path``. With the repo at
``C:\\Tools\\geo_stream``, consumers should be able to import
``geo_stream.coastal_flood_explorer.*``, short ``geo_stream.x`` aliases, and
``geo_stream.app`` without the repo root itself being on ``sys.path``.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHORT_ALIAS_SAMPLES = ("api", "chs", "geometry", "archive_dates", "gdsps_common")


def _purge_geo_stream_modules() -> None:
    """Drop cached geo_stream / coastal_flood_explorer modules."""
    prefixes = ("geo_stream", "coastal_flood_explorer", "app", "watch_and_run")
    for name in list(sys.modules):
        if name == "geo_stream" or name.startswith("geo_stream."):
            del sys.modules[name]
        elif name == "coastal_flood_explorer" or name.startswith(
            "coastal_flood_explorer."
        ):
            del sys.modules[name]
        elif name in {"app", "watch_and_run"}:
            del sys.modules[name]


@pytest.fixture
def tools_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Simulate ``C:\\Tools`` on path with the repo as ``Tools/geo_stream``."""
    tools_root = tmp_path / "Tools"
    tools_root.mkdir()
    link = tools_root / "geo_stream"
    try:
        link.symlink_to(REPO_ROOT, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable for Tools-layout import test")

    _purge_geo_stream_modules()
    # No repo cwd and no repo root on path — only the Tools parent.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "path",
        [str(tools_root)]
        + [p for p in sys.path if p not in ("", ".", str(REPO_ROOT))],
    )
    try:
        yield tools_root
    finally:
        _purge_geo_stream_modules()


def test_geo_stream_package_importable_from_tools_parent(
    tools_layout: Path,
) -> None:
    geo_stream = importlib.import_module("geo_stream")
    assert Path(geo_stream.__file__).resolve().parent == REPO_ROOT


def test_nested_library_importable_from_tools_parent(
    tools_layout: Path,
) -> None:
    api = importlib.import_module("geo_stream.coastal_flood_explorer.api")
    assert api.MAX_TOTAL_FEATURES > 0
    assert "geo_stream.coastal_flood_explorer" in api.__name__


def test_flat_library_resolves_after_geo_stream_bootstrap(
    tools_layout: Path,
) -> None:
    """Importing geo_stream puts the package root on sys.path for flat imports."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("coastal_flood_explorer")

    importlib.import_module("geo_stream")
    flat = importlib.import_module("coastal_flood_explorer")
    nested = importlib.import_module("geo_stream.coastal_flood_explorer")
    assert Path(flat.__file__).resolve() == Path(nested.__file__).resolve()


def test_geo_stream_app_importable_from_tools_parent(
    tools_layout: Path,
) -> None:
    """geo_stream.app loads under Tools-only path (flat imports via bootstrap)."""
    app = importlib.import_module("geo_stream.app")
    assert Path(app.__file__).resolve() == REPO_ROOT / "app.py"
    assert hasattr(app, "main") or hasattr(app, "LOGGER")


@pytest.mark.parametrize("short_name", SHORT_ALIAS_SAMPLES)
def test_short_geo_stream_x_import_aliases_library(
    tools_layout: Path,
    short_name: str,
) -> None:
    """``import geo_stream.x`` resolves to coastal_flood_explorer.x."""
    aliased = importlib.import_module(f"geo_stream.{short_name}")
    nested = importlib.import_module(
        f"geo_stream.coastal_flood_explorer.{short_name}"
    )
    assert aliased is nested
    assert Path(aliased.__file__).resolve().parent == (
        REPO_ROOT / "coastal_flood_explorer"
    )


def test_from_geo_stream_import_short_alias(tools_layout: Path) -> None:
    geo_stream = importlib.import_module("geo_stream")
    assert geo_stream.api is importlib.import_module(
        "geo_stream.coastal_flood_explorer.api"
    )


def test_short_alias_does_not_shadow_real_app_module(
    tools_layout: Path,
) -> None:
    app = importlib.import_module("geo_stream.app")
    assert Path(app.__file__).resolve() == REPO_ROOT / "app.py"
