from __future__ import annotations

from datetime import datetime, timezone

from coastal_flood_explorer.filtering import (
    ALL_FORECAST_PERIODS,
    ANY_CONTRIBUTOR,
    UNKNOWN_FORECAST_PERIOD,
    FilterCriteria,
    apply_filters,
    feature_matches,
    forecast_period_options,
    summarize_features,
    validity_option,
)
from coastal_flood_explorer.properties import RISK_LEVELS


def _feature(
    feature_id: str,
    risk: object,
    validity: object,
    *,
    tide: object = None,
    storm_surge: object = None,
    waves: object = None,
    publication: object = None,
) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": {"type": "Polygon", "coordinates": []},
        "properties": {
            "metobject.risk.value": risk,
            "metobject.tide.value": tide,
            "metobject": {
                "storm_surge": {"value": storm_surge},
                "waves.value": waves,
            },
            "validity_datetime": validity,
            "publication_datetime": publication,
        },
    }


FEATURES = [
    _feature(
        "low",
        1,
        "2026-07-23T12:00:00Z",
        tide=True,
        storm_surge=False,
        waves=None,
        publication="2026-07-23T09:00:00Z",
    ),
    _feature(
        "high",
        "High",
        "2026-07-23T10:00:00-04:00",
        tide=False,
        storm_surge=True,
        waves=True,
        publication="2026-07-23T11:00:00Z",
    ),
    _feature(
        "unknown",
        None,
        None,
        tide=None,
        storm_surge=None,
        waves=False,
        publication="invalid",
    ),
]
COLLECTION = {"type": "FeatureCollection", "features": FEATURES}


def test_filter_criteria_defaults_include_all_risks_and_any_contributors() -> None:
    criteria = FilterCriteria()

    assert criteria.validity == ALL_FORECAST_PERIODS
    assert criteria.risks == RISK_LEVELS
    assert criteria.tide == ANY_CONTRIBUTOR
    assert criteria.storm_surge == ANY_CONTRIBUTOR
    assert criteria.waves == ANY_CONTRIBUTOR


def test_forecast_options_are_unique_sorted_utc_and_include_unknown_last() -> None:
    collection = {
        "type": "FeatureCollection",
        "features": [
            _feature("later", 1, "2026-07-23T16:00:00Z"),
            _feature("earlier", 1, "2026-07-23T10:00:00-04:00"),
            _feature("duplicate", 1, "2026-07-23T14:00:00Z"),
            _feature("missing", 1, None),
            _feature("invalid", 1, "tomorrow-ish"),
        ],
    }

    assert forecast_period_options(collection) == [
        ALL_FORECAST_PERIODS,
        "2026-07-23T14:00:00Z",
        "2026-07-23T16:00:00Z",
        UNKNOWN_FORECAST_PERIOD,
    ]
    assert forecast_period_options(None) == [ALL_FORECAST_PERIODS]


def test_validity_option_normalizes_timezone_and_unknown_values() -> None:
    assert validity_option("2026-07-23T10:00:00-04:00") == (
        "2026-07-23T14:00:00Z"
    )
    assert validity_option("invalid") == UNKNOWN_FORECAST_PERIOD
    assert validity_option(None) == UNKNOWN_FORECAST_PERIOD


def test_filter_by_forecast_validity_uses_canonical_utc_instant() -> None:
    filtered = apply_filters(
        COLLECTION,
        FilterCriteria(
            validity="2026-07-23T14:00:00Z",
            risks=RISK_LEVELS,
        ),
    )

    assert [feature["id"] for feature in filtered["features"]] == ["high"]


def test_filter_by_unknown_forecast_validity_matches_missing_and_invalid() -> None:
    collection = {
        "type": "FeatureCollection",
        "features": [
            *FEATURES,
            _feature("invalid-date", 2, "not-a-date"),
        ],
    }

    filtered = apply_filters(
        collection,
        FilterCriteria(validity=UNKNOWN_FORECAST_PERIOD, risks=RISK_LEVELS),
    )

    assert [feature["id"] for feature in filtered["features"]] == [
        "unknown",
        "invalid-date",
    ]


def test_filter_by_multiple_risks_normalizes_numeric_and_label_selections() -> None:
    filtered = apply_filters(
        COLLECTION,
        FilterCriteria(risks=("1", "HIGH")),
    )

    assert [feature["id"] for feature in filtered["features"]] == ["low", "high"]


def test_cleared_risk_multiselect_returns_empty_collection() -> None:
    filtered = apply_filters(COLLECTION, FilterCriteria(risks=()))

    assert filtered == {"type": "FeatureCollection", "features": []}


def test_filter_by_each_contributing_factor_and_unknown() -> None:
    tide_yes = apply_filters(
        COLLECTION,
        FilterCriteria(risks=RISK_LEVELS, tide="Yes"),
    )
    surge_yes = apply_filters(
        COLLECTION,
        FilterCriteria(risks=RISK_LEVELS, storm_surge="Yes"),
    )
    waves_no = apply_filters(
        COLLECTION,
        FilterCriteria(risks=RISK_LEVELS, waves="No"),
    )
    unknown_tide = apply_filters(
        COLLECTION,
        FilterCriteria(risks=RISK_LEVELS, tide="Unknown"),
    )

    assert [feature["id"] for feature in tide_yes["features"]] == ["low"]
    assert [feature["id"] for feature in surge_yes["features"]] == ["high"]
    assert [feature["id"] for feature in waves_no["features"]] == ["unknown"]
    assert [feature["id"] for feature in unknown_tide["features"]] == ["unknown"]


def test_combined_filters_use_and_semantics() -> None:
    criteria = FilterCriteria(
        validity="2026-07-23T14:00:00Z",
        risks=("High",),
        tide="No",
        storm_surge="Yes",
        waves="Yes",
    )

    assert feature_matches(FEATURES[1], criteria)
    assert not feature_matches(FEATURES[0], criteria)
    assert [item["id"] for item in apply_filters(COLLECTION, criteria)["features"]] == [
        "high"
    ]


def test_filtering_is_non_mutating_and_returns_fresh_feature_collection() -> None:
    original_features = list(FEATURES)

    filtered = apply_filters(COLLECTION, FilterCriteria(risks=("Low",)))

    assert filtered is not COLLECTION
    assert filtered["features"] is not COLLECTION["features"]
    assert filtered["features"][0] is FEATURES[0]
    assert COLLECTION["features"] == original_features
    assert "links" not in filtered


def test_malformed_feature_collection_and_properties_are_handled_defensively() -> None:
    assert apply_filters(None, FilterCriteria()) == {
        "type": "FeatureCollection",
        "features": [],
    }
    assert apply_filters({"features": "not-a-list"}, FilterCriteria()) == {
        "type": "FeatureCollection",
        "features": [],
    }
    malformed_feature = {"type": "Feature", "properties": []}
    assert feature_matches(malformed_feature, FilterCriteria())
    filtered = apply_filters(
        {"features": [malformed_feature, "not-a-feature"]},
        FilterCriteria(),
    )
    assert filtered["features"] == [malformed_feature]


def test_summary_reports_all_risk_counts_and_available_datetime_ranges() -> None:
    summary = summarize_features(COLLECTION)

    assert summary.feature_count == 3
    assert summary.risk_counts == {
        "Low": 1,
        "Moderate": 0,
        "High": 1,
        "Extreme": 0,
        "Unknown": 1,
    }
    assert summary.earliest_validity == datetime(
        2026, 7, 23, 12, tzinfo=timezone.utc
    )
    assert summary.latest_validity == datetime(
        2026, 7, 23, 14, tzinfo=timezone.utc
    )
    assert summary.earliest_publication == datetime(
        2026, 7, 23, 9, tzinfo=timezone.utc
    )
    assert summary.latest_publication == datetime(
        2026, 7, 23, 11, tzinfo=timezone.utc
    )
    assert summary.as_dict() == {
        "feature_count": 3,
        "risk_counts": summary.risk_counts,
        "earliest_validity": "2026-07-23T12:00:00Z",
        "latest_validity": "2026-07-23T14:00:00Z",
        "earliest_publication": "2026-07-23T09:00:00Z",
        "latest_publication": "2026-07-23T11:00:00Z",
    }


def test_empty_summary_is_stable() -> None:
    summary = summarize_features({"type": "FeatureCollection", "features": []})

    assert summary.feature_count == 0
    assert summary.risk_counts == {risk: 0 for risk in RISK_LEVELS}
    assert summary.validity_range == (None, None)
    assert summary.publication_range == (None, None)
