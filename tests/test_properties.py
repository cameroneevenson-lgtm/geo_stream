from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pandas as pd
import pytest

from coastal_flood_explorer.properties import (
    RISK_COLOURS,
    RISK_LEVELS,
    TABLE_COLUMNS,
    display_value,
    export_filename,
    feature_collection_bytes,
    feature_collection_to_dataframe,
    feature_record,
    format_utc_datetime,
    get_feature_property,
    get_property,
    html_value,
    json_safe,
    normalize_contributor,
    normalize_risk,
    parse_utc_datetime,
    risk_colour,
    risk_sort_key,
    safe_feature_collection,
    serialize_feature_collection,
)


def test_get_property_prefers_flattened_value_over_nested_value() -> None:
    properties = {
        "metobject.risk.value": 4,
        "metobject": {"risk": {"value": 1}},
    }

    assert get_property(properties, "metobject.risk.value") == 4


def test_get_property_reads_nested_and_mixed_structures() -> None:
    nested = {"metobject": {"risk": {"value": 3}}}
    mixed = {"metobject": {"risk.value": 2}}

    assert get_property(nested, "metobject.risk.value") == 3
    assert get_property(mixed, "metobject.risk.value") == 2


def test_get_property_treats_present_none_as_value_and_honours_fallback() -> None:
    properties = {
        "metobject.risk.value": None,
        "metobject": {"risk": {"value": 2}},
    }

    assert get_property(properties, "metobject.risk.value", 99) is None
    assert get_property(properties, "missing.path", 99) == 99
    assert get_property(None, "missing.path", 99) == 99
    assert get_property(properties, "", 99) == 99


def test_get_feature_property_handles_missing_or_malformed_properties() -> None:
    assert get_feature_property({"properties": {"domain": "Atlantic"}}, "domain") == (
        "Atlantic"
    )
    assert get_feature_property({"properties": []}, "domain", "fallback") == "fallback"
    assert get_feature_property(None, "domain", "fallback") == "fallback"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "Low"),
        (2.0, "Moderate"),
        (Decimal("3"), "High"),
        ("4", "Extreme"),
        (" 2.0 ", "Moderate"),
        ("low", "Low"),
        ("MODERATE", "Moderate"),
        ("High", "High"),
        (" extreme ", "Extreme"),
        ("unknown", "Unknown"),
        (None, "Unknown"),
        (True, "Unknown"),
        (0, "Unknown"),
        (5, "Unknown"),
        (1.5, "Unknown"),
        ("severe", "Unknown"),
        ("", "Unknown"),
        (float("nan"), "Unknown"),
        (float("inf"), "Unknown"),
        ({"value": 1}, "Unknown"),
    ],
)
def test_normalize_risk(value: object, expected: str) -> None:
    assert normalize_risk(value) == expected


def test_risk_order_and_colours_cover_all_normalized_levels() -> None:
    assert sorted(RISK_LEVELS, key=risk_sort_key) == list(RISK_LEVELS)
    assert set(RISK_COLOURS) == set(RISK_LEVELS)
    assert risk_colour("malformed") == RISK_COLOURS["Unknown"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "Yes"),
        (False, "No"),
        (1, "Yes"),
        (0.0, "No"),
        (" YES ", "Yes"),
        ("true", "Yes"),
        ("y", "Yes"),
        ("NO", "No"),
        ("false", "No"),
        ("n", "No"),
        (None, "Unknown"),
        (2, "Unknown"),
        ("sometimes", "Unknown"),
        (float("nan"), "Unknown"),
    ],
)
def test_normalize_contributor(value: object, expected: str) -> None:
    assert normalize_contributor(value) == expected


def test_parse_and_format_utc_datetime_normalize_offsets_and_naive_values() -> None:
    offset_value = "2026-07-23T10:30:00-04:00"
    naive_value = datetime(2026, 7, 23, 14, 30)

    assert parse_utc_datetime(offset_value) == datetime(
        2026, 7, 23, 14, 30, tzinfo=timezone.utc
    )
    assert format_utc_datetime(offset_value) == "2026-07-23T14:30:00Z"
    assert format_utc_datetime(naive_value) == "2026-07-23T14:30:00Z"
    assert parse_utc_datetime("not-a-date") is None
    assert parse_utc_datetime(1_721_745_000) is None
    assert format_utc_datetime(None) == ""


def test_display_and_html_values_are_deterministic_and_escaped() -> None:
    assert display_value({"b": "<tag>", "a": [1, True]}) == (
        '{"a": [1, true], "b": "<tag>"}'
    )
    assert display_value(float("nan"), missing="missing") == "missing"
    assert display_value(b"\xff") == "\ufffd"
    assert html_value('<script data-x="1">&</script>') == (
        "&lt;script data-x=&quot;1&quot;&gt;&amp;&lt;/script&gt;"
    )
    assert html_value(None, missing="Unknown") == "Unknown"


def _feature(
    feature_id: object = "feature-1",
    **properties: object,
) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": {"type": "Polygon", "coordinates": []},
        "properties": properties,
    }


def test_feature_record_normalizes_fields_without_mutating_properties() -> None:
    properties = {
        "metobject.risk.value": "3",
        "metobject": {
            "impact": {"value": {"en": "<major>"}},
            "likelihood": {"value": ["likely", 0.8]},
        },
        "metobject.tide.value": True,
        "metobject.storm_surge.value": "false",
        "metobject.waves.value": None,
        "validity_datetime": "2026-07-23T10:30:00-04:00",
        "publication_datetime": "bad-but-visible",
        "expiration_datetime": None,
        "amendment": False,
        "domain": "<Atlantic>",
        "status": "test",
        "file_id": 123,
        "source": "Synthetic test data",
    }
    feature = _feature(**properties)

    record = feature_record(feature)

    assert record == {
        "feature_id": "feature-1",
        "risk": "High",
        "impact": '{"en": "<major>"}',
        "likelihood": '["likely", 0.8]',
        "tide": "Yes",
        "storm_surge": "No",
        "waves": "Unknown",
        "validity_datetime": "2026-07-23T14:30:00Z",
        "publication_datetime": "bad-but-visible",
        "expiration_datetime": "",
        "amendment": "false",
        "domain": "<Atlantic>",
        "status": "test",
        "file_id": "123",
        "source": "Synthetic test data",
    }
    assert feature["properties"] == properties


def test_feature_record_derives_explicit_synthetic_and_live_source_labels() -> None:
    assert feature_record(_feature(synthetic=True))["source"] == "Synthetic test data"
    assert feature_record(_feature())["source"] == "ECCC GeoMet"


def test_feature_record_falls_back_to_property_id() -> None:
    feature = _feature(feature_id=None)
    feature["properties"]["id"] = "property-id"
    assert feature_record(feature)["feature_id"] == "property-id"


def test_dataframe_has_stable_columns_for_empty_and_populated_collections() -> None:
    empty = feature_collection_to_dataframe(
        {"type": "FeatureCollection", "features": []}
    )
    populated = feature_collection_to_dataframe(
        {
            "type": "FeatureCollection",
            "features": [_feature(**{"metobject.risk.value": 1}), "malformed"],
        }
    )

    assert isinstance(empty, pd.DataFrame)
    assert tuple(empty.columns) == TABLE_COLUMNS
    assert empty.empty
    assert tuple(populated.columns) == TABLE_COLUMNS
    assert populated.shape == (1, len(TABLE_COLUMNS))
    assert populated.iloc[0]["risk"] == "Low"


def test_json_safe_converts_nested_datetimes_decimals_sets_and_nonfinite_values() -> None:
    value = {
        "instant": datetime(
            2026, 7, 23, 10, 0, tzinfo=timezone(timedelta(hours=-4))
        ),
        "day": datetime(2026, 7, 24).date(),
        "finite": Decimal("1.25"),
        "integer_decimal": Decimal("2"),
        "invalid": float("inf"),
        "items": {"b", "a"},
        7: b"caf\xc3\xa9",
    }

    assert json_safe(value) == {
        "instant": "2026-07-23T14:00:00Z",
        "day": "2026-07-24",
        "finite": 1.25,
        "integer_decimal": 2,
        "invalid": None,
        "items": ["a", "b"],
        "7": "café",
    }


def test_geojson_export_is_strict_utf8_valid_and_does_not_mutate_input() -> None:
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            _feature(
                "é-1",
                forecast=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
                invalid=float("nan"),
            )
        ],
        "links": [{"rel": "next", "href": "must-not-be-exported"}],
    }

    text = serialize_feature_collection(feature_collection)
    decoded = json.loads(text)

    assert "NaN" not in text
    assert "links" not in decoded
    assert decoded["type"] == "FeatureCollection"
    assert decoded["features"][0]["id"] == "é-1"
    assert decoded["features"][0]["properties"]["forecast"] == "2026-07-23T12:00:00Z"
    assert decoded["features"][0]["properties"]["invalid"] is None
    assert feature_collection["features"][0]["properties"]["invalid"] != (
        feature_collection["features"][0]["properties"]["invalid"]
    )
    assert feature_collection_bytes(feature_collection).decode("utf-8") == text


def test_empty_or_malformed_collection_exports_as_valid_empty_feature_collection() -> None:
    expected = {"type": "FeatureCollection", "features": []}

    assert safe_feature_collection(None) == expected
    assert json.loads(serialize_feature_collection({"features": "bad"})) == expected


def test_export_filename_uses_injected_clock_in_utc() -> None:
    now = datetime(2026, 7, 23, 10, 11, 12, tzinfo=timezone(timedelta(hours=-4)))

    assert export_filename(now) == "eccc_coastal_flooding_20260723T141112Z.geojson"
