from __future__ import annotations

import math

import folium

from coastal_flood_explorer.map_view import (
    CANADA_BOUNDS,
    CANADA_NAVIGATION_BOUNDS,
    DEFAULT_CENTER,
    DEFAULT_ZOOM,
    MIN_ZOOM,
    build_base_map,
    build_drawing_hydration_layer,
    build_layer_control,
    build_result_layer,
    risk_legend_html,
    risk_style,
)
from coastal_flood_explorer.properties import RISK_COLOURS


def _feature(risk: object = None, domain: object = None) -> dict:
    return {
        "type": "Feature",
        "id": "sample",
        "properties": {
            "metobject.risk.value": risk,
            "domain": domain,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-65.0, 45.0],
                    [-64.0, 45.0],
                    [-64.0, 46.0],
                    [-65.0, 46.0],
                    [-65.0, 45.0],
                ]
            ],
        },
    }


def test_risk_style_uses_unknown_fallback() -> None:
    style = risk_style(_feature("not-a-risk"))
    assert style["fillColor"]
    assert style["fillOpacity"] > 0


def test_base_map_contains_draw_control_without_fixed_legend() -> None:
    map_object = build_base_map()
    rendered = map_object.get_root().render()
    assert "L.Control.Draw" in rendered
    assert "Coastal flood risk" not in rendered
    assert map_object.location == list(DEFAULT_CENTER)
    assert map_object.options["zoom"] == DEFAULT_ZOOM


def test_base_map_has_buffered_canada_navigation() -> None:
    map_object = build_base_map()
    rendered = map_object.get_root().render()
    tile_layers = [
        child
        for child in map_object._children.values()
        if isinstance(child, folium.TileLayer)
    ]

    assert map_object.options["max_bounds"] == [
        list(CANADA_NAVIGATION_BOUNDS[0]),
        list(CANADA_NAVIGATION_BOUNDS[1]),
    ]
    assert CANADA_NAVIGATION_BOUNDS[0][0] < CANADA_BOUNDS[0][0]
    assert CANADA_NAVIGATION_BOUNDS[0][1] < CANADA_BOUNDS[0][1]
    assert CANADA_NAVIGATION_BOUNDS[1][0] > CANADA_BOUNDS[1][0]
    assert CANADA_NAVIGATION_BOUNDS[1][1] > CANADA_BOUNDS[1][1]
    assert map_object.options["minZoom"] == MIN_ZOOM
    assert map_object.options["max_bounds_viscosity"] == 1.0
    assert map_object.options["world_copy_jump"] is False
    assert len(tile_layers) == 1
    assert tile_layers[0].options["min_zoom"] == MIN_ZOOM
    assert tile_layers[0].options["no_wrap"] is True
    assert tile_layers[0].options["bounds"] == CANADA_NAVIGATION_BOUNDS
    assert '"maxBoundsViscosity": 1.0' in rendered
    assert '"worldCopyJump": false' in rendered
    assert '"minZoom": 3' in rendered
    assert '"noWrap": true' in rendered
    assert ".fitBounds(" in rendered


def test_navigation_buffer_keeps_canadian_extremes_clear_of_controls() -> None:
    world_size = 256 * (2**MIN_ZOOM)

    def project(longitude: float, latitude: float) -> tuple[float, float]:
        latitude_radians = math.radians(latitude)
        x = (longitude + 180.0) / 360.0 * world_size
        y = (
            0.5
            - math.log(
                (1.0 + math.sin(latitude_radians))
                / (1.0 - math.sin(latitude_radians))
            )
            / (4.0 * math.pi)
        ) * world_size
        return x, y

    canada_southwest = project(
        CANADA_BOUNDS[0][1],
        CANADA_BOUNDS[0][0],
    )
    canada_northeast = project(
        CANADA_BOUNDS[1][1],
        CANADA_BOUNDS[1][0],
    )
    navigation_southwest = project(
        CANADA_NAVIGATION_BOUNDS[0][1],
        CANADA_NAVIGATION_BOUNDS[0][0],
    )
    navigation_northeast = project(
        CANADA_NAVIGATION_BOUNDS[1][1],
        CANADA_NAVIGATION_BOUNDS[1][0],
    )
    margins = (
        canada_southwest[0] - navigation_southwest[0],
        navigation_northeast[0] - canada_northeast[0],
        canada_northeast[1] - navigation_northeast[1],
        navigation_southwest[1] - canada_southwest[1],
    )

    assert all(margin >= 32.0 for margin in margins)


def test_risk_legend_is_accessible_and_does_not_overlay_map() -> None:
    rendered = risk_legend_html()

    assert 'aria-label="Coastal flood risk legend"' in rendered
    assert "position:fixed" not in rendered.replace(" ", "").lower()
    assert "position:absolute" not in rendered.replace(" ", "").lower()
    for label, colour in RISK_COLOURS.items():
        assert label in rendered
        assert colour in rendered


def test_layer_control_is_collapsed() -> None:
    control = build_layer_control()

    assert control.options["collapsed"] is True
    assert control.options["position"] == "topright"


def test_synthetic_map_has_banner() -> None:
    map_object = build_base_map()
    build_result_layer(None, synthetic=True).add_to(map_object)
    rendered = map_object.get_root().render()
    assert "SYNTHETIC TEST DATA" in rendered
    assert "geo-stream-synthetic-banner" in rendered


def test_drawings_are_hydrated_into_the_editable_group() -> None:
    map_object = build_base_map()
    layer = build_drawing_hydration_layer([_feature()])
    layer.add_to(map_object)
    rendered = map_object.get_root().render()

    assert "window.drawnItems.clearLayers()" in rendered
    assert "window.drawnItems.addLayer(layer)" in rendered
    assert "window.__geoStreamDrawingFingerprint" in rendered
    assert '"id": "sample"' in rendered


def test_drawing_hydration_layer_is_hidden_from_layer_control() -> None:
    layer = build_drawing_hydration_layer([])
    assert layer.control is False


def test_popup_escapes_property_values() -> None:
    collection = {
        "type": "FeatureCollection",
        "features": [_feature(3, "<script>alert(1)</script>")],
    }
    map_object = folium.Map()
    build_result_layer(collection).add_to(map_object)
    rendered = map_object.get_root().render()
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
