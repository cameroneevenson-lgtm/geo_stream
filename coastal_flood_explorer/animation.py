"""Pure Folium helpers for animating clipped ECCC forecast polygons."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, TypeAlias

import folium
from folium.plugins import Timeline, TimelineSlider
from folium.utilities import JsCode
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

from coastal_flood_explorer.properties import (
    RISK_COLOURS,
    RISK_PROPERTY,
    VALIDITY_PROPERTY,
    get_property,
    normalize_risk,
    parse_utc_datetime,
)


Bounds: TypeAlias = tuple[tuple[float, float], tuple[float, float]]

CANADA_BOUNDS: Bounds = ((41.5, -141.0), (83.2, -52.0))
CANADA_NAVIGATION_BOUNDS: Bounds = ((35.0, -150.0), (85.0, -45.0))
DEFAULT_CENTER = (58.0, -96.0)
DEFAULT_ZOOM = 4
MIN_ZOOM = 3
LAST_FRAME_DURATION = timedelta(hours=24)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SUPPORTED_GEOMETRIES = frozenset({"Polygon", "MultiPolygon"})
_TIMELINE_INTERVAL = JsCode(
    """
    function(feature) {
        return {
            start: feature.properties.start,
            end: feature.properties.end,
            startExclusive: false,
            endExclusive: true
        };
    }
    """
)
_TIMELINE_STYLE = JsCode(
    """
    function(feature) {
        return feature.properties.style;
    }
    """
)
_UTC_SLIDER_LABEL = JsCode(
    """
    function(date) {
        return moment.utc(date).format("YYYY-MM-DD HH:mm [UTC]");
    }
    """
)


class AnimationError(ValueError):
    """Raised when forecast data cannot produce a safe animation."""


@dataclass(frozen=True)
class TimelineData:
    """Prepared animation data and deterministic frame metadata."""

    feature_collection: dict[str, Any]
    frame_times: tuple[datetime, ...]
    end_time: datetime
    skipped_count: int
    bounds: Bounds

    @property
    def frame_count(self) -> int:
        """Return the number of distinct validity frames."""

        return len(self.frame_times)


@dataclass(frozen=True)
class _PreparedFeature:
    geometry: dict[str, Any]
    validity: datetime
    risk: str
    bounds: tuple[float, float, float, float]
    source_index: int


def _epoch_milliseconds(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _safe_polygon(
    geometry: Any,
) -> tuple[dict[str, Any], tuple[float, float, float, float]] | None:
    if not isinstance(geometry, Mapping):
        return None
    if geometry.get("type") not in _SUPPORTED_GEOMETRIES:
        return None

    try:
        parsed = shape(geometry)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if (
        parsed.geom_type not in _SUPPORTED_GEOMETRIES
        or parsed.is_empty
        or not parsed.is_valid
    ):
        return None

    bounds = tuple(float(value) for value in parsed.bounds)
    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        return None
    return dict(mapping(parsed)), bounds  # mapping() returns a fresh geometry.


def _collection_features(collection: Mapping[str, Any]) -> list[Any]:
    if collection.get("type") != "FeatureCollection":
        raise AnimationError("Animation data must be a GeoJSON FeatureCollection.")
    features = collection.get("features")
    if not isinstance(features, list):
        raise AnimationError("The animation FeatureCollection has no feature list.")
    return features


def _combined_bounds(
    features: list[_PreparedFeature],
) -> Bounds:
    minimum_x = min(feature.bounds[0] for feature in features)
    minimum_y = min(feature.bounds[1] for feature in features)
    maximum_x = max(feature.bounds[2] for feature in features)
    maximum_y = max(feature.bounds[3] for feature in features)
    return ((minimum_y, minimum_x), (maximum_y, maximum_x))


def prepare_timeline_data(collection: Mapping[str, Any]) -> TimelineData:
    """Build fresh, minimized timeline features from clipped forecast data.

    Invalid features are omitted independently. Source features and their
    properties are never mutated or reused by reference.
    """

    if not isinstance(collection, Mapping):
        raise AnimationError("Animation data must be a GeoJSON FeatureCollection.")

    prepared: list[_PreparedFeature] = []
    skipped_count = 0
    for source_index, feature in enumerate(_collection_features(collection)):
        if not isinstance(feature, Mapping) or feature.get("type") != "Feature":
            skipped_count += 1
            continue

        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            skipped_count += 1
            continue
        validity = parse_utc_datetime(
            get_property(properties, VALIDITY_PROPERTY)
        )
        safe_geometry = _safe_polygon(feature.get("geometry"))
        if validity is None or safe_geometry is None:
            skipped_count += 1
            continue

        geometry, bounds = safe_geometry
        prepared.append(
            _PreparedFeature(
                geometry=geometry,
                validity=validity,
                risk=normalize_risk(get_property(properties, RISK_PROPERTY)),
                bounds=bounds,
                source_index=source_index,
            )
        )

    if not prepared:
        raise AnimationError("No valid forecast frames are available to animate.")

    frame_times = tuple(sorted({feature.validity for feature in prepared}))
    try:
        end_time = frame_times[-1] + LAST_FRAME_DURATION
    except OverflowError as error:
        raise AnimationError(
            "The final forecast validity is outside the supported range."
        ) from error

    end_by_start = {
        frame_time: (
            frame_times[index + 1]
            if index + 1 < len(frame_times)
            else end_time
        )
        for index, frame_time in enumerate(frame_times)
    }

    timeline_features: list[dict[str, Any]] = []
    for feature in sorted(
        prepared,
        key=lambda item: (item.validity, item.source_index),
    ):
        colour = RISK_COLOURS[feature.risk]
        timeline_features.append(
            {
                "type": "Feature",
                "geometry": feature.geometry,
                "properties": {
                    "start": _epoch_milliseconds(feature.validity),
                    "end": _epoch_milliseconds(end_by_start[feature.validity]),
                    "risk": feature.risk,
                    "style": {
                        "color": colour,
                        "fillColor": colour,
                        "weight": 2,
                        "opacity": 0.9,
                        "fillOpacity": 0.45,
                    },
                },
            }
        )

    return TimelineData(
        feature_collection={
            "type": "FeatureCollection",
            "features": timeline_features,
        },
        frame_times=frame_times,
        end_time=end_time,
        skipped_count=skipped_count,
        bounds=_combined_bounds(prepared),
    )


def _polygon_from_roi(roi: Mapping[str, Any]) -> BaseGeometry:
    candidate: Any
    if roi.get("type") == "Feature":
        candidate = roi.get("geometry")
    else:
        candidate = roi
    if not isinstance(candidate, Mapping):
        raise AnimationError("The animation ROI has no polygon geometry.")

    try:
        parsed = shape(candidate)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise AnimationError("The animation ROI is not valid GeoJSON.") from error
    if (
        parsed.geom_type not in _SUPPORTED_GEOMETRIES
        or parsed.is_empty
        or not parsed.is_valid
        or not all(math.isfinite(float(value)) for value in parsed.bounds)
    ):
        raise AnimationError("The animation ROI must be a valid polygon.")
    return parsed


def _roi_bounds(roi: Mapping[str, Any]) -> Bounds:
    minimum_x, minimum_y, maximum_x, maximum_y = _polygon_from_roi(roi).bounds
    return ((minimum_y, minimum_x), (maximum_y, maximum_x))


def build_forecast_animation(
    collection: Mapping[str, Any],
    *,
    roi: Mapping[str, Any] | None = None,
) -> folium.Map:
    """Return a Canada-focused Folium timeline for clipped forecast features."""

    timeline_data = prepare_timeline_data(collection)
    fit_bounds = _roi_bounds(roi) if roi is not None else timeline_data.bounds

    map_object = folium.Map(
        location=DEFAULT_CENTER,
        zoom_start=DEFAULT_ZOOM,
        tiles=None,
        min_zoom=MIN_ZOOM,
        minZoom=MIN_ZOOM,
        min_lat=CANADA_NAVIGATION_BOUNDS[0][0],
        max_lat=CANADA_NAVIGATION_BOUNDS[1][0],
        min_lon=CANADA_NAVIGATION_BOUNDS[0][1],
        max_lon=CANADA_NAVIGATION_BOUNDS[1][1],
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
        bounds=CANADA_NAVIGATION_BOUNDS,
    ).add_to(map_object)
    map_object.fit_bounds(fit_bounds, padding=(24, 24), max_zoom=12)

    timeline = Timeline(
        timeline_data.feature_collection,
        get_interval=_TIMELINE_INTERVAL,
        style=_TIMELINE_STYLE,
    ).add_to(map_object)
    slider = TimelineSlider(
        auto_play=False,
        start=_epoch_milliseconds(timeline_data.frame_times[0]),
        end=_epoch_milliseconds(timeline_data.end_time),
        enable_playback=True,
        enable_keyboard_controls=True,
        show_ticks=True,
        steps=max(1, timeline_data.frame_count),
        playback_duration=max(4_000, timeline_data.frame_count * 1_500),
    )
    slider.options["format_output"] = _UTC_SLIDER_LABEL
    slider.add_timelines(timeline).add_to(map_object)

    return map_object
