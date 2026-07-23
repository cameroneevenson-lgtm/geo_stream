"""Pure helpers for reconciling map drawings with application state."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from coastal_flood_explorer.geometry import GeometryError, parse_roi


GeoJSONFeature = dict[str, Any]
# Viewport values must stay client-side. Returning bounds/zoom makes every pan
# rerun Streamlit and can create a recentering feedback loop.
MAP_RETURNED_OBJECTS: tuple[str, ...] = ("all_drawings",)


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
