from __future__ import annotations

import folium

from coastal_flood_explorer.map_view import (
    build_base_map,
    build_result_layer,
    risk_style,
)


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


def test_base_map_contains_draw_control_and_legend() -> None:
    rendered = build_base_map([_feature()]).get_root().render()
    assert "L.Control.Draw" in rendered
    assert "Coastal flood risk" in rendered


def test_synthetic_map_has_banner() -> None:
    rendered = build_base_map([], synthetic=True).get_root().render()
    assert "SYNTHETIC TEST DATA" in rendered


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
