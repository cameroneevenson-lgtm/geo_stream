"""GeoJSON and Shapely helpers for regions of interest and feature clipping."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import logging
import math
from pathlib import Path
from typing import Any, TypeAlias

from shapely.errors import GEOSException
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
    shape,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points, unary_union

try:
    from shapely.validation import make_valid
except ImportError:  # pragma: no cover - Shapely 2.x is a project requirement.
    make_valid = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
MAX_ROI_VERTICES = 10_000
MEAN_EARTH_RADIUS_KM = 6_371.0088

BBox: TypeAlias = tuple[float, float, float, float]
FeatureCollection: TypeAlias = dict[str, Any]
GeoJSONMapping: TypeAlias = Mapping[str, Any]


class GeometryError(ValueError):
    """Raised when a region of interest cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ClipResult:
    """The successfully clipped features and diagnostics for rejected features."""

    feature_collection: FeatureCollection
    skipped_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ROIPointMatch:
    """A valid point ranked against an exact polygonal ROI."""

    point_id: str
    inside_roi: bool
    distance_to_roi_km: float
    distance_to_center_km: float


def _repair_geometry(geometry: BaseGeometry) -> BaseGeometry:
    """Return a valid geometry when Shapely can repair it."""

    if geometry.is_empty or geometry.is_valid:
        return geometry

    repaired: BaseGeometry
    if make_valid is not None:
        repaired = make_valid(geometry)
    else:  # pragma: no cover - compatibility fallback for Shapely < 2.
        repaired = geometry.buffer(0)

    if not repaired.is_empty and not repaired.is_valid:
        repaired = repaired.buffer(0)
    return repaired


def _polygonal_parts(geometry: BaseGeometry) -> list[Polygon]:
    """Extract non-empty polygon members from a Shapely geometry."""

    if isinstance(geometry, Polygon):
        return [geometry] if not geometry.is_empty else []
    if isinstance(geometry, MultiPolygon):
        return [part for part in geometry.geoms if not part.is_empty]
    if isinstance(geometry, GeometryCollection):
        parts: list[Polygon] = []
        for member in geometry.geoms:
            parts.extend(_polygonal_parts(member))
        return parts
    return []


def _polygon_vertex_count(geometry: BaseGeometry) -> int:
    """Count exterior and interior coordinates in polygonal geometry."""

    count = 0
    for polygon in _polygonal_parts(geometry):
        count += len(polygon.exterior.coords)
        count += sum(len(interior.coords) for interior in polygon.interiors)
    return count


def _as_polygonal(geometry: BaseGeometry, *, context: str) -> BaseGeometry:
    """Repair a geometry and retain only its polygonal area."""

    try:
        repaired = _repair_geometry(geometry)
    except (GEOSException, ValueError, TypeError) as exc:
        raise GeometryError(f"{context} could not be repaired: {exc}") from exc

    parts = _polygonal_parts(repaired)
    if not parts:
        if repaired.is_empty:
            raise GeometryError(f"{context} is empty.")
        raise GeometryError(
            f"{context} must be a Polygon or MultiPolygon, not "
            f"{repaired.geom_type}."
        )

    if len(parts) == 1:
        polygonal: BaseGeometry = parts[0]
    else:
        try:
            polygonal = unary_union(parts)
        except (GEOSException, ValueError) as exc:
            raise GeometryError(
                f"{context} polygon parts could not be combined: {exc}"
            ) from exc

    polygonal = _repair_geometry(polygonal)
    final_parts = _polygonal_parts(polygonal)
    if not final_parts:
        raise GeometryError(f"{context} has no usable polygon area.")
    if len(final_parts) == 1:
        return final_parts[0]
    return MultiPolygon(final_parts)


def _geometry_payload(value: GeoJSONMapping) -> GeoJSONMapping:
    """Unwrap a GeoJSON Feature while rejecting other containers."""

    object_type = value.get("type")
    if object_type == "Feature":
        geometry = value.get("geometry")
        if not isinstance(geometry, Mapping):
            raise GeometryError("The ROI feature has no GeoJSON geometry.")
        return geometry
    if object_type == "FeatureCollection":
        raise GeometryError(
            "A FeatureCollection cannot be used as one ROI; select a polygon."
        )
    return value


def parse_roi(roi: GeoJSONMapping | BaseGeometry) -> BaseGeometry:
    """Parse, validate, and when possible repair a polygonal ROI.

    ``roi`` may be a GeoJSON Polygon/MultiPolygon, a GeoJSON Feature containing
    one, or an existing Shapely geometry. Rectangles drawn by Leaflet are
    represented as Polygons and require no special case.
    """

    if isinstance(roi, BaseGeometry):
        candidate = roi
    elif isinstance(roi, Mapping):
        payload = _geometry_payload(roi)
        geometry_type = payload.get("type")
        if geometry_type not in {"Polygon", "MultiPolygon"}:
            readable_type = geometry_type or "missing type"
            raise GeometryError(
                "The ROI must be a Polygon or MultiPolygon; "
                f"received {readable_type}."
            )
        try:
            candidate = shape(payload)
        except (GEOSException, ValueError, TypeError, KeyError) as exc:
            raise GeometryError(f"The ROI is not valid GeoJSON: {exc}") from exc
    else:
        raise GeometryError("The ROI must be a GeoJSON object or Shapely geometry.")

    polygonal = _as_polygonal(candidate, context="The ROI")
    if _polygon_vertex_count(polygonal) > MAX_ROI_VERTICES:
        raise GeometryError(
            "The ROI contains too many vertices. Simplify the drawing and "
            "try again."
        )
    _validate_finite_bounds(polygonal.bounds, context="The ROI")
    return polygonal


def _validate_finite_bounds(
    bounds: tuple[float, float, float, float], *, context: str
) -> BBox:
    """Validate and normalize a four-value Shapely bounds tuple."""

    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        raise GeometryError(f"{context} has non-finite bounds.")
    min_lon, min_lat, max_lon, max_lat = (float(value) for value in bounds)
    if min_lon >= max_lon or min_lat >= max_lat:
        raise GeometryError(f"{context} has no positive polygon area.")
    if min_lon < -180 or max_lon > 180 or min_lat < -90 or max_lat > 90:
        raise GeometryError(f"{context} extends outside valid WGS84 bounds.")
    return min_lon, min_lat, max_lon, max_lat


def roi_bbox(roi: GeoJSONMapping | BaseGeometry) -> BBox:
    """Return ``(min_lon, min_lat, max_lon, max_lat)`` for a valid ROI."""

    return _validate_finite_bounds(parse_roi(roi).bounds, context="The ROI")


def extract_bbox(roi: GeoJSONMapping | BaseGeometry) -> BBox:
    """Backward-compatible descriptive alias for :func:`roi_bbox`."""

    return roi_bbox(roi)


def _haversine_km(
    first_lon: float,
    first_lat: float,
    second_lon: float,
    second_lat: float,
) -> float:
    """Return great-circle distance in kilometres between two WGS84 points."""

    first_lat_radians = math.radians(first_lat)
    second_lat_radians = math.radians(second_lat)
    latitude_delta = second_lat_radians - first_lat_radians
    longitude_delta = math.radians(second_lon - first_lon)
    haversine = (
        math.sin(latitude_delta / 2.0) ** 2
        + math.cos(first_lat_radians)
        * math.cos(second_lat_radians)
        * math.sin(longitude_delta / 2.0) ** 2
    )
    angular_distance = 2.0 * math.asin(min(1.0, math.sqrt(haversine)))
    return MEAN_EARTH_RADIUS_KM * angular_distance


def _valid_wgs84_point(
    candidate: object,
) -> tuple[str, float, float, Point] | None:
    """Return a normalized point tuple, or ``None`` for an invalid candidate."""

    try:
        point_id, raw_lon, raw_lat = candidate  # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    if not isinstance(point_id, str):
        return None
    if isinstance(raw_lon, bool) or isinstance(raw_lat, bool):
        return None
    try:
        lon = float(raw_lon)
        lat = float(raw_lat)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(lon)
        or not math.isfinite(lat)
        or lon < -180.0
        or lon > 180.0
        or lat < -90.0
        or lat > 90.0
    ):
        return None
    return point_id, lon, lat, Point(lon, lat)


def rank_points_for_roi(
    roi: GeoJSONMapping | BaseGeometry,
    points: Iterable[tuple[str, float, float]],
) -> tuple[ROIPointMatch, ...]:
    """Rank valid WGS84 points against an exact polygonal ROI.

    Invalid point records and coordinates are skipped. Points covered by the
    ROI, including its boundaries, sort first by distance to a representative
    interior point. Outside points follow by their shortest distance to the
    exact ROI. Point IDs provide a deterministic tie-break in both groups.
    """

    roi_geometry = parse_roi(roi)
    roi_center = roi_geometry.representative_point()
    center_lon, center_lat = roi_center.x, roi_center.y
    matches: list[ROIPointMatch] = []

    for candidate in points:
        normalized = _valid_wgs84_point(candidate)
        if normalized is None:
            continue
        point_id, lon, lat, point = normalized
        try:
            inside_roi = bool(roi_geometry.covers(point))
            if inside_roi:
                distance_to_roi_km = 0.0
            else:
                nearest_roi_point = nearest_points(roi_geometry, point)[0]
                distance_to_roi_km = _haversine_km(
                    lon,
                    lat,
                    nearest_roi_point.x,
                    nearest_roi_point.y,
                )
        except (GEOSException, ValueError, TypeError) as exc:
            raise GeometryError(
                f"Point {point_id!r} could not be ranked against the ROI: {exc}"
            ) from exc

        matches.append(
            ROIPointMatch(
                point_id=point_id,
                inside_roi=inside_roi,
                distance_to_roi_km=distance_to_roi_km,
                distance_to_center_km=_haversine_km(
                    lon,
                    lat,
                    center_lon,
                    center_lat,
                ),
            )
        )

    matches.sort(
        key=lambda match: (
            0 if match.inside_roi else 1,
            (
                match.distance_to_center_km
                if match.inside_roi
                else match.distance_to_roi_km
            ),
            match.point_id,
        )
    )
    return tuple(matches)


def feature_collection(features: Iterable[Mapping[str, Any]]) -> FeatureCollection:
    """Build a fresh GeoJSON FeatureCollection from feature mappings."""

    return {
        "type": "FeatureCollection",
        "features": [deepcopy(dict(feature)) for feature in features],
    }


def _feature_label(feature: Any, index: int) -> str:
    """Return a safe label for diagnostics without exposing whole objects."""

    if isinstance(feature, Mapping) and feature.get("id") not in (None, ""):
        return f"feature {feature['id']!s}"
    return f"feature at index {index}"


def _clip_one_feature(
    feature: Mapping[str, Any],
    roi_geometry: BaseGeometry,
) -> dict[str, Any] | None:
    """Clip one polygon feature, returning ``None`` for an empty overlap."""

    if feature.get("type") != "Feature":
        raise GeometryError("the item is not a GeoJSON Feature")

    properties = feature.get("properties")
    if (
        "properties" in feature
        and properties is not None
        and not isinstance(properties, Mapping)
    ):
        raise GeometryError(
            "the feature properties must be a JSON object or null"
        )

    geometry_payload = feature.get("geometry")
    if not isinstance(geometry_payload, Mapping):
        raise GeometryError("the feature has no GeoJSON geometry")

    geometry_type = geometry_payload.get("type")
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise GeometryError(
            "the feature geometry must be Polygon or MultiPolygon, "
            f"not {geometry_type or 'missing type'}"
        )

    try:
        source = shape(geometry_payload)
    except (GEOSException, ValueError, TypeError, KeyError) as exc:
        raise GeometryError(f"the feature geometry is malformed: {exc}") from exc

    source = _as_polygonal(source, context="The feature geometry")
    try:
        clipped = source.intersection(roi_geometry)
    except (GEOSException, ValueError, TypeError) as exc:
        # A second repair pass is useful for geometries that only fail when
        # evaluated by GEOS in combination with another geometry.
        try:
            clipped = _repair_geometry(source).intersection(
                _repair_geometry(roi_geometry)
            )
        except (GEOSException, ValueError, TypeError) as retry_exc:
            raise GeometryError(
                f"local intersection failed: {retry_exc}"
            ) from retry_exc
        LOGGER.debug("Intersection succeeded after geometry repair", exc_info=exc)

    if clipped.is_empty:
        return None

    clipped_parts = _polygonal_parts(_repair_geometry(clipped))
    if not clipped_parts:
        # Boundary-only contacts are not flood-risk polygon results.
        return None
    if len(clipped_parts) == 1:
        clipped = clipped_parts[0]
    else:
        clipped = MultiPolygon(clipped_parts)

    output = deepcopy(dict(feature))
    output.pop("bbox", None)
    output["geometry"] = mapping(clipped)
    # Preserve a missing properties member as an empty mapping while retaining
    # all supplied property values unchanged.
    if "properties" not in output:
        output["properties"] = {}
    return output


def clip_feature_collection(
    collection: Mapping[str, Any],
    roi: GeoJSONMapping | BaseGeometry,
) -> ClipResult:
    """Intersect every usable polygon feature with an exact polygonal ROI.

    Empty intersections are normally discarded. Malformed individual features
    are skipped independently, counted, logged, and represented by a concise
    warning so one bad item cannot invalidate an otherwise useful API response.
    """

    if not isinstance(collection, Mapping):
        raise GeometryError("The source data must be a GeoJSON FeatureCollection.")
    if collection.get("type") != "FeatureCollection":
        raise GeometryError("The source data is not a GeoJSON FeatureCollection.")
    features = collection.get("features")
    if not isinstance(features, list):
        raise GeometryError("The source FeatureCollection has no feature list.")

    roi_geometry = parse_roi(roi)
    clipped_features: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, raw_feature in enumerate(features):
        label = _feature_label(raw_feature, index)
        if not isinstance(raw_feature, Mapping):
            warning = f"Skipped {label}: the item is not a GeoJSON object."
            LOGGER.warning(warning)
            warnings.append(warning)
            continue
        try:
            clipped = _clip_one_feature(raw_feature, roi_geometry)
        except GeometryError as exc:
            warning = f"Skipped {label}: {exc}."
            LOGGER.warning(warning, exc_info=True)
            warnings.append(warning)
            continue
        if clipped is not None:
            clipped_features.append(clipped)

    return ClipResult(
        feature_collection=feature_collection(clipped_features),
        skipped_count=len(warnings),
        warnings=tuple(warnings),
    )


def sanitize_for_json(value: Any) -> Any:
    """Recursively convert common property values to strict JSON values.

    Non-finite numbers become ``null``. Datetimes are serialized as ISO-8601;
    timezone-aware datetimes are converted to UTC and use a trailing ``Z``.
    Unknown scalar objects are converted to readable strings rather than raw
    Python container representations.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            utc_value = value.astimezone(timezone.utc)
            return utc_value.isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_for_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_for_json(item) for item in value]

    # NumPy/pandas scalar values expose ``item``; keep these optional
    # dependencies out of this focused module.
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            scalar = item_method()
        except (TypeError, ValueError):
            pass
        else:
            if scalar is not value:
                return sanitize_for_json(scalar)
    return str(value)


def serialize_feature_collection(
    collection: Mapping[str, Any], *, indent: int | None = 2
) -> str:
    """Serialize a FeatureCollection as strict, UTF-8-compatible GeoJSON text."""

    if collection.get("type") != "FeatureCollection":
        raise GeometryError("Only a GeoJSON FeatureCollection can be exported.")
    features = collection.get("features")
    if not isinstance(features, list):
        raise GeometryError("The FeatureCollection has no feature list.")
    safe_collection = sanitize_for_json(collection)
    return json.dumps(
        safe_collection,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
    )
