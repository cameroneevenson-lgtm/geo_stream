"""Clearly labelled synthetic coastal-flood features for UI development."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from shapely.geometry import box, mapping
from shapely.geometry.base import BaseGeometry

from .geometry import (
    FeatureCollection,
    GeometryError,
    _polygonal_parts,
    feature_collection,
    parse_roi,
)


SYNTHETIC_SOURCE = "SYNTHETIC TEST DATA — NOT ECCC"
SYNTHETIC_DOMAIN = "SYNTHETIC-DEVELOPMENT-ONLY"
RISK_LABELS = {
    1: "Low",
    2: "Moderate",
    3: "High",
    4: "Extreme",
}

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """Return the current aware UTC time."""

    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize an injected clock value to an aware UTC datetime."""

    if not isinstance(value, datetime):
        raise TypeError("The synthetic clock must return a datetime.")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    """Format a datetime with second precision and a UTC ``Z`` suffix."""

    return (
        _as_utc(value)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _risk_regions(roi: BaseGeometry) -> list[BaseGeometry]:
    """Partition the ROI bounds into up to four intersecting polygon bands."""

    min_x, min_y, max_x, max_y = roi.bounds
    width = max_x - min_x
    height = max_y - min_y
    if width <= 0 or height <= 0:
        raise GeometryError("The ROI has no area for synthetic polygons.")

    regions: list[BaseGeometry] = []
    if width >= height:
        step = width / 4
        candidate_boxes = [
            box(min_x + index * step, min_y, min_x + (index + 1) * step, max_y)
            for index in range(4)
        ]
    else:
        step = height / 4
        candidate_boxes = [
            box(min_x, min_y + index * step, max_x, min_y + (index + 1) * step)
            for index in range(4)
        ]

    for candidate in candidate_boxes:
        intersection = roi.intersection(candidate)
        parts = _polygonal_parts(intersection)
        if not parts:
            continue
        if len(parts) == 1:
            regions.append(parts[0])
        else:
            from shapely.geometry import MultiPolygon

            regions.append(MultiPolygon(parts))
    return regions


def generate_synthetic_data(
    roi: Mapping[str, Any] | BaseGeometry,
    *,
    clock: Clock = _utc_now,
) -> FeatureCollection:
    """Generate up to four unmistakably synthetic features within an ROI.

    The generated set replaces, rather than augments, a live dataset. Each
    polygon has a distinct risk value where the ROI shape permits four bands.
    ``clock`` is injectable so tests and screenshots can be deterministic.
    """

    roi_geometry = parse_roi(roi)
    generated_at = _as_utc(clock()).replace(microsecond=0)
    regions = _risk_regions(roi_geometry)
    features: list[dict[str, Any]] = []

    for index, region in enumerate(regions):
        risk_value = index + 1
        validity = generated_at + timedelta(hours=index * 6)
        publication = generated_at - timedelta(hours=1)
        expiration = validity + timedelta(hours=12)
        risk_label = RISK_LABELS[risk_value]
        feature_id = f"synthetic-coastal-risk-{risk_value}"

        features.append(
            {
                "type": "Feature",
                "id": feature_id,
                "geometry": mapping(region),
                "properties": {
                    "synthetic": True,
                    "source": SYNTHETIC_SOURCE,
                    "source_mode": "synthetic",
                    "metobject.risk.value": risk_value,
                    "metobject.risk.label": risk_label,
                    "metobject.impact.value": (
                        f"Synthetic impact example ({risk_label})"
                    ),
                    "metobject.likelihood.value": (
                        ["Unlikely", "Possible", "Likely", "Very likely"][index]
                    ),
                    "metobject.tide.value": index in {0, 2},
                    "metobject.storm_surge.value": index in {1, 2, 3},
                    "metobject.waves.value": index in {2, 3},
                    "validity_datetime": _iso_z(validity),
                    "publication_datetime": _iso_z(publication),
                    "expiration_datetime": _iso_z(expiration),
                    "amendment": False,
                    "domain": SYNTHETIC_DOMAIN,
                    "status": "SYNTHETIC — NOT AN OFFICIAL FORECAST",
                    "file_id": f"SYNTHETIC-{generated_at:%Y%m%dT%H%M%SZ}-{risk_value}",
                },
            }
        )

    return feature_collection(features)


def generate_synthetic_feature_collection(
    roi: Mapping[str, Any] | BaseGeometry,
    *,
    clock: Clock = _utc_now,
) -> FeatureCollection:
    """Alias with an explicit return-type name for callers."""

    return generate_synthetic_data(roi, clock=clock)
