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
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_package_root_str = str(_PACKAGE_ROOT)
if _package_root_str not in sys.path:
    # Insert after the empty cwd entry when present so interactive runs from
    # the repo root keep their usual first-hit, but Tools-only consumers still
    # get flat coastal_flood_explorer once geo_stream is imported.
    _insert_at = 1 if sys.path and sys.path[0] in ("", ".") else 0
    sys.path.insert(_insert_at, _package_root_str)

del _package_root_str
