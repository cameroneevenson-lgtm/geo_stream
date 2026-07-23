from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import folium
import pytest
from shapely.geometry import shape

from coastal_flood_explorer.animation import (
    AnimationError,
    CANADA_NAVIGATION_BOUNDS,
    LAST_FRAME_DURATION,
    build_forecast_animation,
    filter_by_publication_time,
    prepare_timeline_data,
    publication_times,
)
from coastal_flood_explorer.properties import RISK_COLOURS


def _polygon(west: float, south: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [west + 0.5, south],
                [west + 0.5, south + 0.5],
                [west, south + 0.5],
                [west, south],
            ]
        ],
    }


def _feature(
    validity: object,
    risk: object = 1,
    *,
    publication: object | None = None,
    west: float = -64.0,
    south: float = 44.5,
) -> dict:
    properties = {
        "validity_datetime": validity,
        "metobject": {"risk": {"value": risk}},
        "domain": "<script>alert('source')</script>",
        "unneeded": {"large": ["source", "property"]},
    }
    if publication is not None:
        properties["publication_datetime"] = publication
    return {
        "type": "Feature",
        "id": "<source-id>",
        "geometry": _polygon(west, south),
        "properties": properties,
    }


def _collection(*features: object) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def test_publication_times_are_normalized_sorted_unique_and_nonmutating() -> None:
    source = _collection(
        _feature(
            "2026-07-15T00:00:00Z",
            publication="2026-07-14T20:00:00-04:00",
        ),
        _feature(
            "2026-07-14T18:00:00Z",
            publication="2026-07-14T12:00:00Z",
        ),
        _feature(
            "2026-07-14T19:00:00Z",
            publication="2026-07-14T12:00:00+00:00",
        ),
        _feature(
            "2026-07-14T20:00:00Z",
            publication="not-a-time",
        ),
        None,
    )
    original = deepcopy(source)

    result = publication_times(source)

    assert result == (
        datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 0, tzinfo=timezone.utc),
    )
    assert source == original


def test_filter_by_publication_time_is_exact_and_returns_fresh_features() -> None:
    selected = _feature(
        "2026-07-15T00:00:00Z",
        3,
        publication="2026-07-14T20:00:00-04:00",
    )
    same_instant = _feature(
        "2026-07-15T06:00:00Z",
        4,
        publication="2026-07-15T00:00:00Z",
        west=-65.0,
    )
    other = _feature(
        "2026-07-14T18:00:00Z",
        1,
        publication="2026-07-14T12:00:00Z",
    )
    source = _collection(selected, same_instant, other, None)
    original = deepcopy(source)

    filtered = filter_by_publication_time(
        source,
        datetime(2026, 7, 15, tzinfo=timezone.utc),
    )

    assert filtered["type"] == "FeatureCollection"
    risks = [
        feature["properties"]["metobject"]["risk"]["value"]
        for feature in filtered["features"]
    ]
    assert risks == [
        3,
        4,
    ]
    assert filtered["features"][0] is not selected
    filtered["features"][0]["properties"]["unneeded"]["large"].append("changed")
    assert source == original


def test_publication_filter_prevents_same_validity_from_mixing_issuances() -> None:
    validity = "2026-07-15T12:00:00Z"
    earlier = "2026-07-14T12:00:00Z"
    later = "2026-07-15T00:00:00Z"
    source = _collection(
        _feature(validity, 1, publication=earlier),
        _feature(validity, 4, publication=later, west=-65.0),
    )

    filtered = filter_by_publication_time(source, later)
    prepared = prepare_timeline_data(filtered)

    assert len(filtered["features"]) == 1
    assert len(prepared.feature_collection["features"]) == 1
    assert prepared.feature_collection["features"][0]["properties"]["risk"] == (
        "Extreme"
    )


@pytest.mark.parametrize(
    ("collection", "publication", "message"),
    [
        (None, "2026-07-14T12:00:00Z", "FeatureCollection"),
        ({}, "2026-07-14T12:00:00Z", "FeatureCollection"),
        (_collection(), "not-a-time", "valid forecast publication time"),
    ],
)
def test_publication_helpers_reject_invalid_inputs(
    collection: object,
    publication: object,
    message: str,
) -> None:
    with pytest.raises(AnimationError, match=message):
        filter_by_publication_time(
            collection,  # type: ignore[arg-type]
            publication,
        )


@pytest.mark.parametrize(
    "collection, message",
    [
        ({}, "FeatureCollection"),
        ({"type": "FeatureCollection"}, "feature list"),
        ({"type": "FeatureCollection", "features": None}, "feature list"),
        (_collection(), "No valid forecast frames"),
        (_collection(_feature("not-a-time")), "No valid forecast frames"),
        (
            _collection(
                {
                    **_feature("2026-07-14T12:00:00Z"),
                    "geometry": {"type": "Point", "coordinates": [-64, 45]},
                }
            ),
            "No valid forecast frames",
        ),
        (
            _collection(
                {
                    **_feature("2026-07-14T12:00:00Z"),
                    "geometry": {"type": "Polygon"},
                }
            ),
            "No valid forecast frames",
        ),
    ],
)
def test_invalid_or_empty_data_has_a_clear_error(
    collection: dict,
    message: str,
) -> None:
    with pytest.raises(AnimationError, match=message):
        prepare_timeline_data(collection)


def test_preparation_is_minimal_and_does_not_mutate_source() -> None:
    source = _collection(_feature("2026-07-14T12:00:00Z", 3))
    original = deepcopy(source)

    prepared = prepare_timeline_data(source)

    assert source == original
    output = prepared.feature_collection["features"][0]
    assert set(output) == {"type", "geometry", "properties"}
    assert set(output["properties"]) == {"start", "end", "risk", "style"}
    assert shape(output["geometry"]).equals(
        shape(source["features"][0]["geometry"])
    )
    assert output["geometry"] is not source["features"][0]["geometry"]
    assert output["properties"]["risk"] == "High"

    output["geometry"]["coordinates"] = ()
    assert source == original


def test_frames_persist_until_next_validity_and_last_for_24_hours() -> None:
    first = "2026-07-14T12:00:00Z"
    second = "2026-07-15T12:00:00Z"
    prepared = prepare_timeline_data(
        _collection(
            _feature(second, 4, west=-123.0),
            _feature(first, 1),
            _feature(first, 2, west=-65.0),
        )
    )

    assert prepared.frame_times == (
        datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
    )
    assert prepared.end_time == prepared.frame_times[-1] + LAST_FRAME_DURATION
    assert prepared.frame_count == 2

    features = prepared.feature_collection["features"]
    first_end = features[0]["properties"]["end"]
    assert features[1]["properties"]["end"] == first_end
    assert first_end == features[2]["properties"]["start"]
    assert (
        features[2]["properties"]["end"]
        - features[2]["properties"]["start"]
        == 24 * 60 * 60 * 1_000
    )


@pytest.mark.parametrize(
    ("risk", "label"),
    [
        (1, "Low"),
        ("moderate", "Moderate"),
        (3, "High"),
        ("Extreme", "Extreme"),
        ("unexpected", "Unknown"),
    ],
)
def test_normalized_risk_controls_animation_colour(
    risk: object,
    label: str,
) -> None:
    prepared = prepare_timeline_data(
        _collection(_feature("2026-07-14T12:00:00Z", risk))
    )
    properties = prepared.feature_collection["features"][0]["properties"]

    assert properties["risk"] == label
    assert properties["style"]["color"] == RISK_COLOURS[label]
    assert properties["style"]["fillColor"] == RISK_COLOURS[label]


def test_invalid_features_are_skipped_independently() -> None:
    prepared = prepare_timeline_data(
        _collection(
            None,
            _feature("invalid"),
            _feature("2026-07-14T12:00:00Z"),
        )
    )

    assert prepared.skipped_count == 2
    assert len(prepared.feature_collection["features"]) == 1


def test_source_text_is_not_embedded_in_animation_html() -> None:
    map_object = build_forecast_animation(
        _collection(_feature("2026-07-14T12:00:00Z", "<script>bad</script>"))
    )

    rendered = map_object.get_root().render()
    assert "<script>alert('source')</script>" not in rendered
    assert "<source-id>" not in rendered
    assert '"risk": "Unknown"' in rendered


def test_map_contains_no_wrap_tiles_and_visible_manual_timeline_controls() -> None:
    map_object = build_forecast_animation(
        _collection(
            _feature("2026-07-14T12:00:00Z"),
            _feature("2026-07-15T12:00:00Z", 4),
        )
    )
    rendered = map_object.get_root().render()
    tile_layers = [
        child
        for child in map_object._children.values()
        if isinstance(child, folium.TileLayer)
    ]

    assert map_object.options["max_bounds"] == [
        list(CANADA_NAVIGATION_BOUNDS[0]),
        list(CANADA_NAVIGATION_BOUNDS[1]),
    ]
    assert len(tile_layers) == 1
    assert tile_layers[0].options["no_wrap"] is True
    assert tile_layers[0].options["bounds"] == CANADA_NAVIGATION_BOUNDS
    assert "L.timeline(" in rendered
    assert "L.timelineSliderControl(" in rendered
    assert '"autoPlay": false' in rendered
    assert '"enablePlayback": true' in rendered
    assert '"enableKeyboardControls": true' in rendered
    assert '"showTicks": true' in rendered
    assert "leaflet-timeline-controls" in rendered
    assert "moment.utc(date)" in rendered
    assert '"noWrap": true' in rendered
    assert "endExclusive: true" in rendered


def test_map_fits_roi_before_feature_bounds() -> None:
    roi = {
        "type": "Feature",
        "properties": {},
        "geometry": _polygon(-63.75, 44.5),
    }
    map_object = build_forecast_animation(
        _collection(
            _feature(
                "2026-07-14T12:00:00Z",
                west=-123.5,
                south=49.0,
            )
        ),
        roi=roi,
    )
    rendered = map_object.get_root().render()

    assert ".fitBounds(" in rendered
    assert "[[44.5, -63.75], [45.0, -63.25]]" in rendered


def test_map_falls_back_to_combined_feature_bounds() -> None:
    map_object = build_forecast_animation(
        _collection(
            _feature("2026-07-14T12:00:00Z", west=-123.5, south=49.0),
            _feature("2026-07-15T12:00:00Z", west=-64.0, south=44.5),
        )
    )
    rendered = map_object.get_root().render()

    assert "[[44.5, -123.5], [49.5, -63.5]]" in rendered


def test_invalid_roi_is_not_silently_ignored() -> None:
    with pytest.raises(AnimationError, match="ROI must be a valid polygon"):
        build_forecast_animation(
            _collection(_feature("2026-07-14T12:00:00Z")),
            roi={"type": "Point", "coordinates": [-64.0, 45.0]},
        )
