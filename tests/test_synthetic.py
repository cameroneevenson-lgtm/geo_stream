from __future__ import annotations

from datetime import datetime, timezone

import pytest
from shapely.geometry import shape
from shapely.ops import unary_union

from coastal_flood_explorer.geometry import GeometryError, parse_roi
from coastal_flood_explorer.synthetic import (
    SYNTHETIC_DOMAIN,
    SYNTHETIC_SOURCE,
    generate_synthetic_data,
    generate_synthetic_feature_collection,
)


FIXED_TIME = datetime(2026, 1, 15, 12, 34, 56, tzinfo=timezone.utc)


def rectangular_roi() -> dict:
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-66, 43],
                    [-58, 43],
                    [-58, 47],
                    [-66, 47],
                    [-66, 43],
                ]
            ],
        },
    }


def test_synthetic_generation_includes_four_risk_levels() -> None:
    result = generate_synthetic_data(rectangular_roi(), clock=lambda: FIXED_TIME)

    assert result["type"] == "FeatureCollection"
    assert len(result["features"]) == 4
    assert [
        feature["properties"]["metobject.risk.value"]
        for feature in result["features"]
    ] == [1, 2, 3, 4]


def test_every_synthetic_feature_is_unmistakably_labelled() -> None:
    result = generate_synthetic_data(rectangular_roi(), clock=lambda: FIXED_TIME)

    for index, feature in enumerate(result["features"], start=1):
        properties = feature["properties"]
        assert feature["id"] == f"synthetic-coastal-risk-{index}"
        assert properties["synthetic"] is True
        assert properties["source"] == SYNTHETIC_SOURCE
        assert properties["source_mode"] == "synthetic"
        assert properties["domain"] == SYNTHETIC_DOMAIN
        assert "SYNTHETIC" in properties["status"]
        assert properties["file_id"].startswith("SYNTHETIC-")


def test_synthetic_geometry_is_inside_exact_roi_and_regions_do_not_overlap() -> None:
    roi = parse_roi(rectangular_roi())
    result = generate_synthetic_data(rectangular_roi(), clock=lambda: FIXED_TIME)
    geometries = [shape(feature["geometry"]) for feature in result["features"]]

    assert all(geometry.difference(roi).is_empty for geometry in geometries)
    assert unary_union(geometries).area == pytest.approx(roi.area)
    for left_index, left in enumerate(geometries):
        for right in geometries[left_index + 1 :]:
            assert left.intersection(right).area == pytest.approx(0)


def test_synthetic_generation_clips_bands_to_irregular_roi() -> None:
    triangular_roi = {
        "type": "Polygon",
        "coordinates": [
            [[-66, 43], [-58, 43], [-62, 47], [-66, 43]]
        ],
    }
    roi = parse_roi(triangular_roi)

    result = generate_synthetic_data(triangular_roi, clock=lambda: FIXED_TIME)

    assert 1 <= len(result["features"]) <= 4
    assert all(
        shape(feature["geometry"]).difference(roi).is_empty
        for feature in result["features"]
    )


def test_injected_clock_makes_timestamps_and_file_ids_deterministic() -> None:
    result = generate_synthetic_data(rectangular_roi(), clock=lambda: FIXED_TIME)
    first = result["features"][0]["properties"]
    fourth = result["features"][3]["properties"]

    assert first["validity_datetime"] == "2026-01-15T12:34:56Z"
    assert first["publication_datetime"] == "2026-01-15T11:34:56Z"
    assert first["expiration_datetime"] == "2026-01-16T00:34:56Z"
    assert first["file_id"] == "SYNTHETIC-20260115T123456Z-1"
    assert fourth["validity_datetime"] == "2026-01-16T06:34:56Z"


def test_naive_injected_clock_is_explicitly_treated_as_utc() -> None:
    naive = datetime(2026, 1, 15, 12, 34, 56)

    result = generate_synthetic_data(rectangular_roi(), clock=lambda: naive)

    assert (
        result["features"][0]["properties"]["validity_datetime"]
        == "2026-01-15T12:34:56Z"
    )


def test_alias_returns_same_deterministic_collection() -> None:
    direct = generate_synthetic_data(rectangular_roi(), clock=lambda: FIXED_TIME)
    aliased = generate_synthetic_feature_collection(
        rectangular_roi(), clock=lambda: FIXED_TIME
    )

    assert aliased == direct


def test_synthetic_generation_rejects_unsupported_roi() -> None:
    with pytest.raises(GeometryError, match="Polygon or MultiPolygon"):
        generate_synthetic_data(
            {"type": "Point", "coordinates": [-63, 45]},
            clock=lambda: FIXED_TIME,
        )


def test_synthetic_generation_requires_datetime_clock_value() -> None:
    with pytest.raises(TypeError, match="must return a datetime"):
        generate_synthetic_data(rectangular_roi(), clock=lambda: "not a date")  # type: ignore[return-value]
