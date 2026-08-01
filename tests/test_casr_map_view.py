"""Unit tests for the CaSR Folium overlay builder."""

from __future__ import annotations

import base64
import io

from PIL import Image

from coastal_flood_explorer.map_view import (
    build_casr_overlay_layer,
    build_chs_station_layer,
    build_gdsps_overlay_layer,
    build_result_layer,
)


def _png_b64() -> str:
    image = Image.new("RGBA", (2, 2), (20, 180, 160, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_build_casr_overlay_adds_image_and_marker() -> None:
    group = build_casr_overlay_layer(
        {
            "label": "CaSR-Land · TJ_1.5m · 2017-12-31",
            "opacity": 0.5,
            "overlays": [
                {
                    "png_b64": _png_b64(),
                    "bounds": [[44.0, -64.0], [45.0, -63.0]],
                    "point": [-63.5, 44.5],
                    "subbasin_id": "20171231",
                }
            ],
        },
        enabled=True,
    )
    assert group.layer_name == "CaSR-Land · TJ_1.5m · 2017-12-31"
    assert group.show is True
    assert len(group._children) >= 2


def test_disabled_casr_overlay_is_empty_group() -> None:
    group = build_casr_overlay_layer(
        {
            "overlays": [
                {
                    "png_b64": _png_b64(),
                    "bounds": [[0.0, 0.0], [1.0, 1.0]],
                }
            ]
        },
        enabled=False,
    )
    assert group.layer_name == "CaSR-Land"
    assert len(group._children) == 0
    # Library builders for hidden layers remain importable for later restore.
    assert build_result_layer(None) is not None
    assert build_chs_station_layer([]) is not None
    assert build_gdsps_overlay_layer(None, enabled=False).show is False
