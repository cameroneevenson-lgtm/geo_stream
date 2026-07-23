from __future__ import annotations

from datetime import datetime, timezone
import json
import math

import pytest
from shapely.geometry import Polygon, box, shape

from coastal_flood_explorer.geometry import (
    ClipResult,
    GeometryError,
    MAX_ROI_VERTICES,
    clip_feature_collection,
    extract_bbox,
    feature_collection,
    parse_roi,
    sanitize_for_json,
    serialize_feature_collection,
)


def polygon_feature(
    coordinates: list[list[list[float]]],
    *,
    feature_id: str | None = "feature-1",
    properties: dict | None = None,
) -> dict:
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coordinates},
        "properties": properties if properties is not None else {"name": "source"},
    }
    if feature_id is not None:
        feature["id"] = feature_id
    return feature


def square_feature(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    **kwargs,
) -> dict:
    return polygon_feature(
        [
            [
                [min_x, min_y],
                [max_x, min_y],
                [max_x, max_y],
                [min_x, max_y],
                [min_x, min_y],
            ]
        ],
        **kwargs,
    )


def test_extract_bbox_from_polygon_feature() -> None:
    roi = square_feature(-66.5, 43.25, -59.0, 48.75)

    assert extract_bbox(roi) == (-66.5, 43.25, -59.0, 48.75)


def test_parse_roi_accepts_multipolygon() -> None:
    roi = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[-66, 43], [-65, 43], [-65, 44], [-66, 44], [-66, 43]]],
            [[[-64, 45], [-63, 45], [-63, 46], [-64, 46], [-64, 45]]],
        ],
    }

    parsed = parse_roi(roi)

    assert parsed.geom_type == "MultiPolygon"
    assert parsed.area == pytest.approx(2.0)


def test_parse_roi_repairs_self_intersecting_polygon() -> None:
    bow_tie = {
        "type": "Polygon",
        "coordinates": [
            [[-66, 43], [-64, 45], [-66, 45], [-64, 43], [-66, 43]]
        ],
    }

    parsed = parse_roi(bow_tie)

    assert parsed.is_valid
    assert not parsed.is_empty
    assert parsed.geom_type in {"Polygon", "MultiPolygon"}


@pytest.mark.parametrize(
    "roi, message",
    [
        ({"type": "Point", "coordinates": [-63, 45]}, "Polygon or MultiPolygon"),
        ({"type": "Feature", "properties": {}, "geometry": None}, "no GeoJSON"),
        ({"type": "FeatureCollection", "features": []}, "FeatureCollection"),
    ],
)
def test_parse_roi_rejects_unsupported_or_missing_geometry(
    roi: dict, message: str
) -> None:
    with pytest.raises(GeometryError, match=message):
        parse_roi(roi)


def test_parse_roi_rejects_out_of_range_wgs84_coordinates() -> None:
    roi = square_feature(-181, 43, -179, 44)

    with pytest.raises(GeometryError, match="outside valid WGS84"):
        parse_roi(roi)


def test_clip_polygon_preserves_id_and_properties_without_mutation() -> None:
    source = square_feature(
        -66,
        43,
        -62,
        47,
        feature_id="risk-7",
        properties={"domain": "marine", "nested": {"value": 3}},
    )
    source["bbox"] = [-66, 43, -62, 47]
    collection = feature_collection([source])
    roi = square_feature(-65, 44, -63, 46)

    result = clip_feature_collection(collection, roi)

    assert isinstance(result, ClipResult)
    assert result.skipped_count == 0
    assert result.warnings == ()
    assert len(result.feature_collection["features"]) == 1
    clipped = result.feature_collection["features"][0]
    assert clipped["id"] == "risk-7"
    assert clipped["properties"] == {
        "domain": "marine",
        "nested": {"value": 3},
    }
    assert "bbox" not in clipped
    assert shape(clipped["geometry"]).equals(box(-65, 44, -63, 46))
    assert source["geometry"] == collection["features"][0]["geometry"]
    assert source["bbox"] == [-66, 43, -62, 47]


def test_clip_multipolygon_to_exact_roi() -> None:
    source = {
        "type": "Feature",
        "id": "multi",
        "properties": {"risk": 4},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                [[[-67, 43], [-65, 43], [-65, 45], [-67, 45], [-67, 43]]],
                [[[-63, 43], [-61, 43], [-61, 45], [-63, 45], [-63, 43]]],
            ],
        },
    }
    roi = square_feature(-66, 42, -62, 46)

    result = clip_feature_collection(feature_collection([source]), roi)

    clipped = shape(result.feature_collection["features"][0]["geometry"])
    assert clipped.geom_type == "MultiPolygon"
    assert clipped.area == pytest.approx(4.0)
    assert clipped.within(parse_roi(roi))


def test_empty_and_boundary_only_intersections_are_discarded_not_skipped() -> None:
    features = [
        square_feature(-70, 40, -69, 41, feature_id="far"),
        square_feature(-64, 44, -63, 45, feature_id="touching"),
    ]
    roi = square_feature(-63, 44, -62, 45)

    result = clip_feature_collection(feature_collection(features), roi)

    assert result.feature_collection == {"type": "FeatureCollection", "features": []}
    assert result.skipped_count == 0
    assert result.warnings == ()


def test_invalid_source_geometry_is_repaired_before_clipping() -> None:
    bow_tie = polygon_feature(
        [[[-66, 43], [-64, 45], [-66, 45], [-64, 43], [-66, 43]]],
        feature_id="repair-me",
    )
    roi = square_feature(-67, 42, -63, 46)

    result = clip_feature_collection(feature_collection([bow_tie]), roi)

    assert result.skipped_count == 0
    output_geometry = shape(result.feature_collection["features"][0]["geometry"])
    assert output_geometry.is_valid
    assert output_geometry.area > 0


def test_malformed_features_are_skipped_independently_with_warnings() -> None:
    features = [
        None,
        {"type": "Feature", "id": "missing-geometry", "properties": {}},
        {
            "type": "Feature",
            "id": "a-point",
            "geometry": {"type": "Point", "coordinates": [-63, 45]},
            "properties": {},
        },
        square_feature(-64, 44, -62, 46, feature_id="good"),
    ]
    collection = {"type": "FeatureCollection", "features": features}

    result = clip_feature_collection(
        collection, square_feature(-65, 43, -61, 47)
    )

    assert [item["id"] for item in result.feature_collection["features"]] == [
        "good"
    ]
    assert result.skipped_count == 3
    assert len(result.warnings) == 3
    assert "index 0" in result.warnings[0]
    assert "missing-geometry" in result.warnings[1]
    assert "a-point" in result.warnings[2]


def test_non_object_feature_properties_are_skipped() -> None:
    bad = square_feature(-64, 44, -62, 46, feature_id="bad-properties")
    bad["properties"] = ["not", "an", "object"]

    result = clip_feature_collection(
        feature_collection([bad]),
        square_feature(-65, 43, -61, 47),
    )

    assert result.feature_collection["features"] == []
    assert result.skipped_count == 1
    assert "properties must be a JSON object or null" in result.warnings[0]


def test_roi_vertex_limit_rejects_unreasonably_complex_drawings() -> None:
    point_count = MAX_ROI_VERTICES + 1
    ring = [
        [
            -63.0 + math.cos(index * 2 * math.pi / point_count),
            46.0 + math.sin(index * 2 * math.pi / point_count),
        ]
        for index in range(point_count)
    ]
    ring.append(ring[0])

    with pytest.raises(GeometryError, match="too many vertices"):
        parse_roi({"type": "Polygon", "coordinates": [ring]})


@pytest.mark.parametrize(
    "collection",
    [
        {},
        {"type": "Feature"},
        {"type": "FeatureCollection", "features": None},
    ],
)
def test_clip_rejects_malformed_collection(collection: dict) -> None:
    with pytest.raises(GeometryError):
        clip_feature_collection(collection, square_feature(-65, 43, -61, 47))


def test_feature_collection_creates_an_independent_copy() -> None:
    original = square_feature(-64, 44, -62, 46)

    collection = feature_collection([original])
    collection["features"][0]["properties"]["name"] = "changed"

    assert original["properties"]["name"] == "source"


def test_json_export_is_valid_for_empty_collection_and_utf8() -> None:
    text = serialize_feature_collection(feature_collection([]))

    assert json.loads(text) == {"type": "FeatureCollection", "features": []}
    assert "\\u" not in text


def test_json_export_sanitizes_datetime_nonfinite_and_complex_values() -> None:
    collection = feature_collection(
        [
            {
                "type": "Feature",
                "id": "special",
                "geometry": None,
                "properties": {
                    "when": datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc),
                    "nan": math.nan,
                    "infinity": math.inf,
                    "tuple": ("é", 2),
                },
            }
        ]
    )

    decoded = json.loads(serialize_feature_collection(collection, indent=None))

    properties = decoded["features"][0]["properties"]
    assert properties["when"] == "2026-07-23T14:30:00Z"
    assert properties["nan"] is None
    assert properties["infinity"] is None
    assert properties["tuple"] == ["é", 2]


def test_sanitize_for_json_does_not_leave_nonfinite_values() -> None:
    safe = sanitize_for_json({"values": [1.0, math.nan, -math.inf]})

    assert safe == {"values": [1.0, None, None]}
