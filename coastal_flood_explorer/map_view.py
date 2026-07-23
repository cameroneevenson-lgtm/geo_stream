"""Folium map construction and safe feature presentation."""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from typing import Any

import folium
from branca.element import MacroElement
from folium.plugins import Draw, Fullscreen
from folium.template import Template

from coastal_flood_explorer.properties import (
    RISK_COLOURS,
    STORM_SURGE_PROPERTY,
    TIDE_PROPERTY,
    WAVES_PROPERTY,
    display_value,
    get_property,
    normalize_contributor,
    normalize_risk,
)


DEFAULT_CENTER = (49.0, -62.0)
DEFAULT_ZOOM = 5
ROI_COLOUR = "#2563eb"
UNKNOWN_COLOUR = "#64748b"

POPUP_FIELDS: tuple[tuple[str, str], ...] = (
    ("Risk level", "metobject.risk.value"),
    ("Impact", "metobject.impact.value"),
    ("Likelihood", "metobject.likelihood.value"),
    ("Tide contribution", "metobject.tide.value"),
    ("Storm-surge contribution", "metobject.storm_surge.value"),
    ("Wave contribution", "metobject.waves.value"),
    ("Forecast validity", "validity_datetime"),
    ("Publication time", "publication_datetime"),
    ("Expiration time", "expiration_datetime"),
    ("Amendment", "amendment"),
    ("Domain", "domain"),
    ("Status", "status"),
    ("File ID", "file_id"),
)


class RiskLegend(MacroElement):
    """Fixed map legend for normalized risk colours."""

    _template = Template(
        """
        {% macro html(this, kwargs) %}
        <div style="
          position: fixed; bottom: 28px; right: 12px; z-index: 9999;
          background: rgba(255,255,255,0.96); border: 1px solid #94a3b8;
          border-radius: 6px; padding: 9px 11px; color: #0f172a;
          font: 12px/1.35 Arial, sans-serif; box-shadow: 0 1px 5px #64748b;">
          <div style="font-weight:700;margin-bottom:5px;">Coastal flood risk</div>
          {% for label, colour in this.items %}
          <div><span style="display:inline-block;width:11px;height:11px;
            margin-right:6px;background:{{ colour }};border:1px solid #475569;">
            </span>{{ label }}</div>
          {% endfor %}
        </div>
        {% endmacro %}
        """
    )

    def __init__(self) -> None:
        super().__init__()
        self._name = "RiskLegend"
        self.items = list(RISK_COLOURS.items())


class SyntheticBanner(MacroElement):
    """Prominent banner shown whenever the current layer is synthetic."""

    _template = Template(
        """
        {% macro html(this, kwargs) %}
        <div style="
          position: fixed; top: 10px; left: 50%; transform: translateX(-50%);
          z-index: 9999; background: #7c2d12; color: white; border-radius: 6px;
          padding: 7px 12px; font: 700 12px Arial, sans-serif;
          box-shadow: 0 1px 5px #431407; pointer-events:none;">
          SYNTHETIC TEST DATA — NOT ECCC DATA
        </div>
        {% endmacro %}
        """
    )

    def __init__(self) -> None:
        super().__init__()
        self._name = "SyntheticBanner"


def _rings_to_locations(coordinates: object) -> list[list[tuple[float, float]]]:
    locations: list[list[tuple[float, float]]] = []
    if not isinstance(coordinates, list):
        return locations
    for ring in coordinates:
        if not isinstance(ring, list):
            continue
        converted: list[tuple[float, float]] = []
        for point in ring:
            if (
                isinstance(point, (list, tuple))
                and len(point) >= 2
                and isinstance(point[0], (int, float))
                and isinstance(point[1], (int, float))
            ):
                converted.append((float(point[1]), float(point[0])))
        if len(converted) >= 3:
            locations.append(converted)
    return locations


def _add_editable_geometry(
    feature_group: folium.FeatureGroup,
    geometry: Mapping[str, Any],
) -> None:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    polygons: Iterable[object]
    if geometry_type == "Polygon":
        polygons = [coordinates]
    elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        polygons = coordinates
    else:
        return

    for polygon in polygons:
        locations = _rings_to_locations(polygon)
        if not locations:
            continue
        folium.Polygon(
            locations=locations,
            color=ROI_COLOUR,
            weight=3,
            fill=True,
            fill_color=ROI_COLOUR,
            fill_opacity=0.08,
        ).add_to(feature_group)


def build_base_map(
    drawings: Iterable[Mapping[str, Any]],
    *,
    synthetic: bool = False,
) -> folium.Map:
    """Build the stable base map and editable ROI feature group."""

    map_object = folium.Map(
        location=DEFAULT_CENTER,
        zoom_start=DEFAULT_ZOOM,
        tiles="OpenStreetMap",
        zoom_control=True,
        control_scale=True,
        prefer_canvas=True,
    )
    roi_group = folium.FeatureGroup(
        name="Region of interest",
        control=True,
        show=True,
    )
    for feature in drawings:
        geometry = feature.get("geometry")
        if isinstance(geometry, Mapping):
            _add_editable_geometry(roi_group, geometry)
    roi_group.add_to(map_object)

    Draw(
        export=False,
        feature_group=roi_group,
        position="topleft",
        show_geometry_on_click=False,
        draw_options={
            "polyline": False,
            "polygon": {
                "allowIntersection": False,
                "showArea": True,
            },
            "rectangle": {},
            "circle": False,
            "marker": False,
            "circlemarker": False,
        },
        edit_options={
            "edit": {"selectedPathOptions": {"maintainColor": True}},
            "remove": {},
        },
    ).add_to(map_object)
    Fullscreen(
        position="topleft",
        force_separate_button=True,
        title="Enter fullscreen",
        title_cancel="Exit fullscreen",
    ).add_to(map_object)
    RiskLegend().add_to(map_object)
    if synthetic:
        SyntheticBanner().add_to(map_object)
    return map_object


def _escaped(value: object) -> str:
    return html.escape(display_value(value), quote=True)


def _feature_risk(properties: Mapping[str, Any]) -> str:
    return normalize_risk(get_property(properties, "metobject.risk.value"))


def risk_style(feature: Mapping[str, Any]) -> dict[str, Any]:
    """Return a defensive Folium style for a GeoJSON feature."""

    properties = feature.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    risk = _feature_risk(properties)
    colour = RISK_COLOURS.get(risk, UNKNOWN_COLOUR)
    return {
        "color": colour,
        "weight": 2,
        "fillColor": colour,
        "fillOpacity": 0.48,
    }


def _tooltip_html(feature: Mapping[str, Any], *, synthetic: bool) -> str:
    properties = feature.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    parts: list[str] = []
    if synthetic:
        parts.append("<strong>SYNTHETIC TEST DATA</strong>")
    risk = _feature_risk(properties)
    parts.append(f"<strong>Risk:</strong> {_escaped(risk)}")
    validity = get_property(properties, "validity_datetime")
    domain = get_property(properties, "domain")
    if validity not in (None, ""):
        parts.append(f"<strong>Validity:</strong> {_escaped(validity)}")
    if domain not in (None, ""):
        parts.append(f"<strong>Domain:</strong> {_escaped(domain)}")
    return "<br>".join(parts)


def _popup_html(feature: Mapping[str, Any], *, synthetic: bool) -> str:
    properties = feature.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    rows: list[str] = []
    if synthetic:
        rows.append(
            "<tr><th colspan='2' style='color:#9a3412'>"
            "SYNTHETIC TEST DATA — NOT ECCC DATA</th></tr>"
        )
    for label, path in POPUP_FIELDS:
        value = get_property(properties, path)
        if value in (None, ""):
            continue
        if path == "metobject.risk.value":
            value = _feature_risk(properties)
        elif path in {
            TIDE_PROPERTY,
            STORM_SURGE_PROPERTY,
            WAVES_PROPERTY,
        }:
            value = normalize_contributor(value)
        rows.append(
            "<tr>"
            f"<th style='text-align:left;padding:2px 8px 2px 0'>{_escaped(label)}</th>"
            f"<td style='padding:2px 0'>{_escaped(value)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td>No feature details were supplied.</td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def build_result_layer(
    feature_collection: Mapping[str, Any] | None,
    *,
    synthetic: bool = False,
) -> folium.FeatureGroup:
    """Build a noneditable result layer with escaped popups and tooltips."""

    name = (
        "SYNTHETIC TEST DATA — NOT ECCC"
        if synthetic
        else "ECCC coastal flood risk"
    )
    group = folium.FeatureGroup(name=name, control=True, show=True)
    if not isinstance(feature_collection, Mapping):
        return group
    features = feature_collection.get("features")
    if not isinstance(features, list):
        return group

    for feature in features:
        if not isinstance(feature, dict) or not isinstance(
            feature.get("geometry"), dict
        ):
            continue
        layer = folium.GeoJson(
            data=feature,
            style_function=risk_style,
            highlight_function=lambda _: {
                "weight": 4,
                "fillOpacity": 0.65,
            },
        )
        folium.Tooltip(
            _tooltip_html(feature, synthetic=synthetic),
            sticky=True,
        ).add_to(layer)
        folium.Popup(
            _popup_html(feature, synthetic=synthetic),
            max_width=430,
        ).add_to(layer)
        layer.add_to(group)
    return group
