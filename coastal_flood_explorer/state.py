"""Pure helpers for reconciling map drawings with application state."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from coastal_flood_explorer.geometry import GeometryError, parse_roi


GeoJSONFeature = dict[str, Any]


@dataclass(frozen=True)
class DrawingState:
    """Validated drawing payload and its newest valid active ROI."""

    drawings: tuple[GeoJSONFeature, ...]
    active_roi: GeoJSONFeature | None
    warnings: tuple[str, ...] = ()


def reconcile_drawings(value: object) -> DrawingState:
    """Validate an ``all_drawings`` value from streamlit-folium.

    The frontend returns ``None`` before a drawing event and an explicit empty
    list after every drawing has been deleted. Callers handle ``None`` before
    invoking this function so an empty list can intentionally clear state.
    """

    if not isinstance(value, list):
        return DrawingState((), None, ("Drawing data was not a list.",))

    drawings: list[GeoJSONFeature] = []
    active: GeoJSONFeature | None = None
    warnings: list[str] = []

    for index, candidate in enumerate(value, start=1):
        if not isinstance(candidate, dict):
            warnings.append(f"Drawing {index} was not a GeoJSON object.")
            continue

        feature: GeoJSONFeature
        if candidate.get("type") == "Feature":
            feature = copy.deepcopy(candidate)
        elif candidate.get("type") in {"Polygon", "MultiPolygon"}:
            feature = {
                "type": "Feature",
                "properties": {},
                "geometry": copy.deepcopy(candidate),
            }
        else:
            warnings.append(f"Drawing {index} used an unsupported geometry type.")
            continue

        try:
            parse_roi(feature)
        except GeometryError as exc:
            warnings.append(f"Drawing {index} is invalid: {exc}")
            continue

        drawings.append(feature)
        active = feature

    return DrawingState(tuple(drawings), active, tuple(warnings))


def roi_matches(left: object, right: object) -> bool:
    """Return whether two stored ROI objects are topologically equivalent."""

    if left is None or right is None:
        return False
    try:
        return bool(parse_roi(left).equals(parse_roi(right)))
    except GeometryError:
        return False


def viewport_from_map_payload(
    payload: Mapping[str, Any],
) -> tuple[tuple[float, float] | None, int | None]:
    """Extract a safe center and zoom from a streamlit-folium payload."""

    center: tuple[float, float] | None = None
    bounds = payload.get("bounds")
    if isinstance(bounds, Mapping):
        southwest = bounds.get("_southWest")
        northeast = bounds.get("_northEast")
        if isinstance(southwest, Mapping) and isinstance(northeast, Mapping):
            try:
                south = float(southwest["lat"])
                west = float(southwest["lng"])
                north = float(northeast["lat"])
                east = float(northeast["lng"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                if (
                    all(math.isfinite(value) for value in (south, west, north, east))
                    and -90 <= south <= north <= 90
                    and -180 <= west <= 180
                    and -180 <= east <= 180
                ):
                    center = ((south + north) / 2, (west + east) / 2)

    zoom: int | None = None
    raw_zoom = payload.get("zoom")
    if isinstance(raw_zoom, (int, float)) and not isinstance(raw_zoom, bool):
        if math.isfinite(float(raw_zoom)) and 0 <= float(raw_zoom) <= 24:
            zoom = int(round(float(raw_zoom)))
    return center, zoom
