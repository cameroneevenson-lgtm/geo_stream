"""geo_stream - coastal flood explorer.

The package marker exists so ``coastal_flood_explorer`` is reachable as
``geo_stream.coastal_flood_explorer`` from anywhere on the shared venv, rather
than only when this repo's own directory happens to be on sys.path. The shared
venv already puts ``C:\\Tools`` on sys.path via tools_bootstrap's
``_tools_root.pth``, so the folder name ``geo_stream`` becomes the top-level
package.

Intra-repo imports stay flat (``from coastal_flood_explorer import ...``),
which keeps ``app.py`` runnable as the script the .bat launchers invoke. This
module also puts the package root on ``sys.path`` so those flat imports still
resolve when the entry point is ``import geo_stream.app`` (or any other
``geo_stream.*`` load) under the Tools-only path layout.

Short aliases are also installed so ``import geo_stream.api`` (and the other
library modules) resolve to ``geo_stream.coastal_flood_explorer.api``. Real
top-level modules ``app``, ``watch_and_run``, and the
``coastal_flood_explorer`` package itself are not aliased.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
from pathlib import Path
from types import ModuleType

_LIBRARY_MODULES = frozenset(
    {
        "animation",
        "api",
        "archive",
        "archive_dates",
        "archive_range",
        "chs",
        "feedback",
        "filtering",
        "gdsps_common",
        "gdsps_datamart",
        "gdsps_export",
        "gdsps_processing",
        "gdsps_service",
        "gdsps_thredds",
        "gdsps_wcs",
        "gdsps_wms",
        "geometry",
        "map_view",
        "properties",
        "state",
        "synthetic",
    }
)

_PACKAGE_ROOT = Path(__file__).resolve().parent
_package_root_str = str(_PACKAGE_ROOT)
if _package_root_str not in sys.path:
    # Insert after the empty cwd entry when present so interactive runs from
    # the repo root keep their usual first-hit, but Tools-only consumers still
    # get flat coastal_flood_explorer once geo_stream is imported.
    _insert_at = 1 if sys.path and sys.path[0] in ("", ".") else 0
    sys.path.insert(_insert_at, _package_root_str)

del _package_root_str


class _LibraryAliasLoader(importlib.abc.Loader):
    """Load ``geo_stream.<name>`` as ``geo_stream.coastal_flood_explorer.<name>``."""

    def __init__(self, short_name: str) -> None:
        self._short_name = short_name

    def create_module(
        self,
        spec: importlib.machinery.ModuleSpec,
    ) -> ModuleType:
        return importlib.import_module(
            f"geo_stream.coastal_flood_explorer.{self._short_name}"
        )

    def exec_module(self, module: ModuleType) -> None:
        return


class _LibraryAliasFinder(importlib.abc.MetaPathFinder):
    """Resolve short ``geo_stream.x`` imports onto the library package."""

    def find_spec(
        self,
        fullname: str,
        path: object | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        prefix = "geo_stream."
        if not fullname.startswith(prefix):
            return None
        short_name = fullname[len(prefix) :]
        if short_name not in _LIBRARY_MODULES:
            return None
        return importlib.machinery.ModuleSpec(
            fullname,
            _LibraryAliasLoader(short_name),
            is_package=False,
        )


def _install_library_alias_finder() -> None:
    for finder in sys.meta_path:
        if isinstance(finder, _LibraryAliasFinder):
            return
    sys.meta_path.insert(0, _LibraryAliasFinder())


_install_library_alias_finder()


def __getattr__(name: str) -> ModuleType:
    """Support ``from geo_stream import api``-style short aliases."""
    if name in _LIBRARY_MODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(
        {
            *globals(),
            *_LIBRARY_MODULES,
            "app",
            "watch_and_run",
            "coastal_flood_explorer",
        }
    )
