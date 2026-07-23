"""Folium map construction and safe feature presentation."""

from __future__ import annotations

import hashlib
import html
import json
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


CANADA_BOUNDS = ((41.5, -141.0), (83.2, -52.0))
DEFAULT_CENTER = (58.0, -96.0)
DEFAULT_ZOOM = 4
MIN_ZOOM = 4
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
        {% macro script(this, kwargs) %}
        var syntheticBanner = document.getElementById(
          "geo-stream-synthetic-banner"
        );
        if ({{ this.visible|tojson }} && !syntheticBanner) {
          syntheticBanner = document.createElement("div");
          syntheticBanner.id = "geo-stream-synthetic-banner";
          syntheticBanner.textContent = "SYNTHETIC TEST DATA — NOT ECCC DATA";
          syntheticBanner.style.cssText = [
            "position:fixed",
            "top:10px",
            "left:50%",
            "transform:translateX(-50%)",
            "z-index:9999",
            "background:#7c2d12",
            "color:white",
            "border-radius:6px",
            "padding:7px 12px",
            "font:700 12px Arial,sans-serif",
            "box-shadow:0 1px 5px #431407",
            "pointer-events:none"
          ].join(";");
          document.body.appendChild(syntheticBanner);
        } else if (!{{ this.visible|tojson }} && syntheticBanner) {
          syntheticBanner.remove();
        }
        {% endmacro %}
        """
    )

    def __init__(self, *, visible: bool) -> None:
        super().__init__()
        self._name = "SyntheticBanner"
        self.visible = visible


class _DrawingFeatureMetadata(MacroElement):
    """Restore GeoJSON feature metadata on a rehydrated Leaflet polygon."""

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        {{ this._parent.get_name() }}.feature = {{ this.feature|tojson }};
        {% endmacro %}
        """
    )

    def __init__(self, feature: Mapping[str, Any]) -> None:
        super().__init__()
        self._name = "DrawingFeatureMetadata"
        self.feature = dict(feature)


class _DrawingHydrator(MacroElement):
    """Move dynamic polygons into Folium Draw's stable editable group."""

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        var incomingDrawings = {{ this._parent.get_name() }};
        var incomingDrawingLayers = incomingDrawings.getLayers().slice();
        if (
          window.drawnItems &&
          window.__geoStreamDrawingFingerprint !== {{ this.fingerprint|tojson }}
        ) {
          window.drawnItems.clearLayers();
          incomingDrawingLayers.forEach(function(layer) {
            incomingDrawings.removeLayer(layer);
            window.drawnItems.addLayer(layer);
          });
          window.__geoStreamDrawingFingerprint = {{ this.fingerprint|tojson }};
        } else {
          incomingDrawings.clearLayers();
        }
        {% endmacro %}
        """
    )

    def __init__(self, fingerprint: str) -> None:
        super().__init__()
        self._name = "DrawingHydrator"
        self.fingerprint = fingerprint


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
    *,
    feature: Mapping[str, Any] | None = None,
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
        layer = folium.Polygon(
            locations=locations,
            color=ROI_COLOUR,
            weight=3,
            fill=True,
            fill_color=ROI_COLOUR,
            fill_opacity=0.08,
        )
        if feature is not None:
            _DrawingFeatureMetadata(feature).add_to(layer)
        layer.add_to(feature_group)


def build_base_map() -> folium.Map:
    """Build the stable base map and editable ROI feature group."""

    map_object = folium.Map(
        location=DEFAULT_CENTER,
        zoom_start=DEFAULT_ZOOM,
        tiles=None,
        min_zoom=MIN_ZOOM,
        minZoom=MIN_ZOOM,
        min_lat=CANADA_BOUNDS[0][0],
        max_lat=CANADA_BOUNDS[1][0],
        min_lon=CANADA_BOUNDS[0][1],
        max_lon=CANADA_BOUNDS[1][1],
        max_bounds=True,
        max_bounds_viscosity=1.0,
        world_copy_jump=False,
        zoom_control=True,
        control_scale=True,
        prefer_canvas=True,
    )
    folium.TileLayer(
        "OpenStreetMap",
        name="OpenStreetMap",
        min_zoom=MIN_ZOOM,
        no_wrap=True,
    ).add_to(map_object)
    roi_group = folium.FeatureGroup(
        name="Region of interest",
        control=True,
        show=True,
    )
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
    return map_object


def build_drawing_hydration_layer(
    drawings: Iterable[Mapping[str, Any]],
) -> folium.FeatureGroup:
    """Build dynamic polygons that are moved into the stable Draw edit group."""

    stored_drawings = [dict(feature) for feature in drawings]
    serialized = json.dumps(
        stored_drawings,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    group = folium.FeatureGroup(
        name="Drawing hydration",
        control=False,
        show=True,
    )
    for feature in stored_drawings:
        geometry = feature.get("geometry")
        if isinstance(geometry, Mapping):
            _add_editable_geometry(
                group,
                geometry,
                feature=feature,
            )
    _DrawingHydrator(fingerprint).add_to(group)
    return group


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
    SyntheticBanner(visible=synthetic).add_to(group)
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
