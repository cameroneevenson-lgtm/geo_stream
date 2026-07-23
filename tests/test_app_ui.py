from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from streamlit.testing.v1 import AppTest


ROI = {
    "type": "Feature",
    "properties": {},
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [-65.0, 45.0],
                [-64.0, 45.0],
                [-64.0, 46.0],
                [-65.0, 46.0],
                [-65.0, 45.0],
            ]
        ],
    },
}


def _app_with_fake_chs(
    extra_setup: str = "",
    extra_teardown: str = "",
) -> str:
    """Return an app script whose automatic CHS calls are fully offline."""

    return f"""
from datetime import timedelta
import app as app_module
from coastal_flood_explorer.chs import (
    CHSStation,
    CHSStationCatalog,
    CHSWaterLevelBundle,
    WaterLevelPoint,
    WaterLevelSeries,
)

original_catalog = app_module._cached_chs_catalog
original_bundle = app_module._cached_chs_bundle

bedford = CHSStation(
    id="bedford-id",
    code="00491",
    official_name="Bedford Institute",
    latitude=44.682262,
    longitude=-63.613239,
    operating=True,
    time_series_codes=("wlo", "wlp"),
    station_type="PERMANENT",
)
roi_gauge = CHSStation(
    id="roi-gauge-id",
    code="99991",
    official_name="ROI Gauge",
    latitude=45.5,
    longitude=-64.5,
    operating=True,
    time_series_codes=("wlo", "wlp"),
    station_type="PERMANENT",
)
catalog = CHSStationCatalog(
    stations=(bedford, roi_gauge),
    fetched_at=app_module.datetime(2026, 7, 23, 20, 0, tzinfo=app_module.timezone.utc),
)

def fake_catalog(api_root):
    return catalog, None, ()

def fake_bundle(api_root, station, anchor):
    observed = WaterLevelSeries(
        code="wlo",
        label="Observed water level",
        points=(
            WaterLevelPoint(
                timestamp=anchor - timedelta(minutes=15),
                value_m=1.234,
                qc_code="1",
                reviewed=False,
            ),
        ),
        start_time=anchor - timedelta(hours=24),
        end_time=anchor,
        raw_point_count=1,
    )
    predicted = WaterLevelSeries(
        code="wlp",
        label="Predicted water level",
        points=(
            WaterLevelPoint(
                timestamp=anchor,
                value_m=1.111,
                qc_code="2",
                reviewed=False,
            ),
        ),
        start_time=anchor - timedelta(hours=24),
        end_time=anchor + timedelta(hours=24),
        raw_point_count=1,
    )
    return CHSWaterLevelBundle(
        station=station,
        anchor_time=anchor,
        fetched_at=anchor,
        observed=observed,
        predicted=predicted,
    ), None, ()

fake_catalog.clear = lambda: None
fake_bundle.clear = lambda: None
app_module._cached_chs_catalog = fake_catalog
app_module._cached_chs_bundle = fake_bundle
{extra_setup}
try:
    app_module.main()
finally:
    {extra_teardown}
    app_module._cached_chs_catalog = original_catalog
    app_module._cached_chs_bundle = original_bundle
"""


def test_archive_clipped_filename_marks_only_partial_ranges() -> None:
    from app import _archive_clipped_filename

    archive_range = (date(2026, 6, 24), date(2026, 7, 23))
    base_name = "eccc_coastal_flooding_20260723T181500Z.geojson"

    assert _archive_clipped_filename(
        base_name,
        archive_range,
        30,
        30,
    ) == (
        "eccc_coastal_flooding_20260624_20260723_"
        "20260723T181500Z.geojson"
    )
    assert _archive_clipped_filename(
        base_name,
        archive_range,
        30,
        29,
    ) == (
        "eccc_coastal_flooding_20260624_20260723_partial_29of30_"
        "20260723T181500Z.geojson"
    )


def test_last_successful_archive_fetch_has_prominent_feedback() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()
    app.session_state["current_source_mode"] = "archive"
    app.session_state["fetch_timestamp"] = datetime(
        2026,
        7,
        23,
        20,
        15,
        tzinfo=timezone.utc,
    )
    loaded_range = (date(2026, 6, 24), date(2026, 7, 23))
    app.session_state["last_requested_archive_range"] = loaded_range
    app.session_state["selected_archive_range"] = loaded_range
    app.session_state["archive_product_count"] = 13
    app.session_state["archive_requested_date_count"] = 30
    app.session_state["archive_successful_date_count"] = 30
    app.session_state["raw_feature_count"] = 7
    app.session_state["clipped_feature_count"] = 3

    app.run()

    assert not list(app.exception)
    messages = [element.value for element in app.success]
    assert any(
        "Loaded ECCC archive range 2026-06-24 through 2026-07-23"
        in message
        and "30/30 issue date(s)" in message
        and "13 file(s)" in message
        and "7 feature(s)" in message
        and "3 intersected" in message
        for message in messages
    )


def test_archive_fetch_action_shows_completion_status_without_network() -> None:
    extra_setup = """
from coastal_flood_explorer.archive import ArchiveFetchResult

original_fetch = app_module._cached_archive_fetch

def fake_fetch(archive_root, archive_date):
    return (
        ArchiveFetchResult(
            collection={"type": "FeatureCollection", "features": []},
            products=(),
            documents=(),
        ),
        None,
        False,
    )

app_module._cached_archive_fetch = fake_fetch
"""
    script = _app_with_fake_chs(
        extra_setup,
        "app_module._cached_archive_fetch = original_fetch",
    )
    app = AppTest.from_string(script, default_timeout=20).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI
    app.run()

    fetch_button = next(
        button
        for button in app.button
        if button.label.startswith("Fetch ECCC archive range")
    )
    fetch_button.click().run()

    assert not list(app.exception)
    statuses = app.get("status")
    archive_status = next(
        status
        for status in statuses
        if "ECCC archive range fetch complete" in status.label
    )
    assert archive_status.state == "complete"
    assert "30/30 date(s)" in archive_status.label
    assert "0 file(s)" in archive_status.label
    assert "0 feature(s)" in archive_status.label
    assert app.session_state["current_source_mode"] == "archive"
    assert app.session_state["raw_archive_download"] is not None
    downloads = {
        button.label: button for button in app.get("download_button")
    }
    assert set(downloads) == {
        "Download raw fetched CHS JSON",
        "Download clipped GeoJSON",
        "Download raw fetched ECCC JSON",
    }
    assert downloads["Download raw fetched ECCC JSON"].disabled is False
    raw_payload = json.loads(
        app.session_state["raw_archive_download"].decode("utf-8")
    )
    assert raw_payload["date_range"]["inclusive_day_count"] == 30
    assert raw_payload["summary"]["successful_date_count"] == 30
    assert raw_payload["summary"]["not_loaded_date_count"] == 0


def test_archive_fetch_calls_every_date_in_short_inclusive_range() -> None:
    extra_setup = """
from coastal_flood_explorer.archive import ArchiveFetchResult

original_fetch = app_module._cached_archive_fetch

def fake_fetch(archive_root, archive_date):
    calls = list(app_module.st.session_state.get("fake_archive_calls", []))
    calls.append(archive_date)
    app_module.st.session_state["fake_archive_calls"] = calls
    return (
        ArchiveFetchResult(
            collection={"type": "FeatureCollection", "features": []},
            products=(),
            documents=(),
        ),
        None,
        False,
    )

app_module._cached_archive_fetch = fake_fetch
"""
    script = _app_with_fake_chs(
        extra_setup,
        "app_module._cached_archive_fetch = original_fetch",
    )
    app = AppTest.from_string(script, default_timeout=20).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI
    app.run()

    range_picker = next(
        element
        for element in app.get("date_input")
        if element.label == "Archived ECCC issue-date range (UTC)"
    )
    start = range_picker.value[0]
    selected = (start, start + timedelta(days=2))
    range_picker.set_value(selected).run()
    fetch_button = next(
        button
        for button in app.button
        if button.label == "Fetch ECCC archive range (3 days)"
    )
    fetch_button.click().run()

    assert not list(app.exception)
    assert app.session_state["fake_archive_calls"] == [
        (start + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(3)
    ]
    assert app.session_state["archive_requested_date_count"] == 3
    assert app.session_state["archive_successful_date_count"] == 3


def test_systemic_archive_failure_stops_remaining_date_requests() -> None:
    extra_setup = """
original_fetch = app_module._cached_archive_fetch

def fake_fetch(archive_root, archive_date):
    calls = list(app_module.st.session_state.get("fake_archive_calls", []))
    calls.append(archive_date)
    app_module.st.session_state["fake_archive_calls"] = calls
    return None, "The ECCC archive service is unavailable.", True

app_module._cached_archive_fetch = fake_fetch
"""
    script = _app_with_fake_chs(
        extra_setup,
        "app_module._cached_archive_fetch = original_fetch",
    )
    app = AppTest.from_string(script, default_timeout=20).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI
    app.run()

    range_picker = next(
        element
        for element in app.get("date_input")
        if element.label == "Archived ECCC issue-date range (UTC)"
    )
    start = range_picker.value[0]
    range_picker.set_value((start, start + timedelta(days=2))).run()
    fetch_button = next(
        button
        for button in app.button
        if button.label == "Fetch ECCC archive range (3 days)"
    )
    fetch_button.click().run()

    assert not list(app.exception)
    assert app.session_state["fake_archive_calls"] == [
        start.strftime("%Y%m%d")
    ]
    assert any(
        "previous results were kept" in status.label
        and status.state == "error"
        for status in app.get("status")
    )


def test_archive_range_stops_after_reaching_cumulative_feature_limit() -> None:
    extra_setup = """
from coastal_flood_explorer.archive import ArchiveFetchResult

original_fetch = app_module._cached_archive_fetch
original_feature_limit = app_module.MAX_TOTAL_FEATURES
app_module.MAX_TOTAL_FEATURES = 1

def fake_fetch(archive_root, archive_date):
    calls = list(app_module.st.session_state.get("fake_archive_calls", []))
    calls.append(archive_date)
    app_module.st.session_state["fake_archive_calls"] = calls
    return (
        ArchiveFetchResult(
            collection={
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "id": archive_date,
                    "geometry": None,
                    "properties": {},
                }],
            },
            products=(),
            documents=(),
        ),
        None,
        False,
    )

app_module._cached_archive_fetch = fake_fetch
"""
    teardown = (
        "app_module.MAX_TOTAL_FEATURES = original_feature_limit\n"
        "    app_module._cached_archive_fetch = original_fetch"
    )
    app = AppTest.from_string(
        _app_with_fake_chs(extra_setup, teardown),
        default_timeout=20,
    ).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI
    app.run()

    range_picker = next(
        element
        for element in app.get("date_input")
        if element.label == "Archived ECCC issue-date range (UTC)"
    )
    start = range_picker.value[0]
    range_picker.set_value((start, start + timedelta(days=2))).run()
    fetch_button = next(
        button
        for button in app.button
        if button.label == "Fetch ECCC archive range (3 days)"
    )
    fetch_button.click().run()

    assert not list(app.exception)
    assert app.session_state["fake_archive_calls"] == [
        start.strftime("%Y%m%d")
    ]
    assert app.session_state["archive_requested_date_count"] == 3
    assert app.session_state["archive_successful_date_count"] == 1
    assert len(app.session_state["archive_date_failures"]) == 2


def test_archive_range_retains_partial_dates_and_labels_them() -> None:
    extra_setup = """
from coastal_flood_explorer.archive import ArchiveFetchResult

original_fetch = app_module._cached_archive_fetch
failed_date = app_module.recent_archive_window().newest.strftime("%Y%m%d")

def fake_fetch(archive_root, archive_date):
    if archive_date == failed_date:
        return None, "That daily partition is unavailable.", False
    return (
        ArchiveFetchResult(
            collection={"type": "FeatureCollection", "features": []},
            products=(),
            documents=(),
        ),
        None,
        False,
    )

app_module._cached_archive_fetch = fake_fetch
"""
    script = _app_with_fake_chs(
        extra_setup,
        "app_module._cached_archive_fetch = original_fetch",
    )
    app = AppTest.from_string(script, default_timeout=20).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI
    app.run()

    fetch_button = next(
        button
        for button in app.button
        if button.label.startswith("Fetch ECCC archive range")
    )
    fetch_button.click().run()

    assert not list(app.exception)
    assert app.session_state["archive_requested_date_count"] == 30
    assert app.session_state["archive_successful_date_count"] == 29
    assert len(app.session_state["archive_date_failures"]) == 1
    assert any(
        "loaded partially with 1 date not loaded" in status.label
        and "29/30 date(s)" in status.label
        for status in app.get("status")
    )
    assert any(
        "1 selected archive date was not loaded" in warning.value
        for warning in app.warning
    )
    raw_payload = json.loads(
        app.session_state["raw_archive_download"].decode("utf-8")
    )
    assert raw_payload["summary"]["successful_date_count"] == 29
    assert raw_payload["summary"]["not_loaded_date_count"] == 1


def test_all_failed_archive_range_keeps_previous_dataset() -> None:
    extra_setup = """
original_fetch = app_module._cached_archive_fetch

def fake_fetch(archive_root, archive_date):
    return None, "That daily partition is unavailable.", False

app_module._cached_archive_fetch = fake_fetch
"""
    script = _app_with_fake_chs(
        extra_setup,
        "app_module._cached_archive_fetch = original_fetch",
    )
    app = AppTest.from_string(script, default_timeout=20).run()
    previous = {"type": "FeatureCollection", "features": []}
    previous_raw = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": None, "properties": {}}],
    }
    previous_range = (date(2026, 7, 1), date(2026, 7, 2))
    previous_download = b'{"previous":true}'
    previous_failures = [
        {
            "issue_date": "20260702",
            "error_type": "ArchiveDateFailure",
            "message": "Previously unavailable.",
        }
    ]
    app.session_state["current_source_mode"] = "archive"
    app.session_state["clipped_data"] = previous
    app.session_state["last_successful_archive_response"] = previous_raw
    app.session_state["last_requested_archive_range"] = previous_range
    app.session_state["archive_product_count"] = 4
    app.session_state["archive_requested_date_count"] = 2
    app.session_state["archive_successful_date_count"] = 1
    app.session_state["archive_date_failures"] = previous_failures
    app.session_state["raw_archive_download"] = previous_download
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI
    app.run()

    fetch_button = next(
        button
        for button in app.button
        if button.label.startswith("Fetch ECCC archive range")
    )
    fetch_button.click().run()

    assert not list(app.exception)
    assert app.session_state["current_source_mode"] == "archive"
    assert app.session_state["clipped_data"] == previous
    assert (
        app.session_state["last_successful_archive_response"]
        == previous_raw
    )
    assert app.session_state["last_requested_archive_range"] == previous_range
    assert app.session_state["archive_product_count"] == 4
    assert app.session_state["archive_requested_date_count"] == 2
    assert app.session_state["archive_successful_date_count"] == 1
    assert app.session_state["archive_date_failures"] == previous_failures
    assert app.session_state["raw_archive_download"] == previous_download
    assert any(
        "previous results were kept" in status.label
        and status.state == "error"
        for status in app.get("status")
    )
    assert any(
        "None of the selected ECCC archive dates could be loaded"
        in error.value
        for error in app.error
    )


def test_incomplete_archive_range_cannot_be_fetched() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()
    oldest = app.session_state["selected_archive_range"][0]
    app.session_state["selected_archive_range"] = (oldest,)

    app.run()

    assert not list(app.exception)
    fetch_button = next(
        button
        for button in app.button
        if button.label == "Fetch ECCC archive range"
    )
    assert fetch_button.disabled is True
    assert any(
        "Choose both a start date and an end date" in warning.value
        for warning in app.warning
    )


def test_initial_render_explains_archive_without_fetching() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()

    assert not list(app.exception)
    range_picker = next(
        element
        for element in app.get("date_input")
        if element.label == "Archived ECCC issue-date range (UTC)"
    )
    assert isinstance(range_picker.value, tuple)
    assert len(range_picker.value) == 2
    assert (range_picker.value[1] - range_picker.value[0]).days + 1 == 30
    captions = [element.value for element in app.caption]
    assert any("not a 30-day average" in value for value in captions)
    assert any("does not contact ECCC" in value for value in captions)


def test_map_has_space_below_it_for_viewport_centering() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()

    assert not list(app.exception)
    assert any(
        "geo-stream-map-scroll-space" in element.value
        and "min-height:280px" in element.value
        for element in app.markdown
    )


def test_map_payload_accepts_new_region_after_explicit_deletion() -> None:
    replacement = {
        **ROI,
        "geometry": {
            **ROI["geometry"],
            "coordinates": [
                [
                    [-63.0, 44.0],
                    [-62.0, 44.0],
                    [-62.0, 45.0],
                    [-63.0, 45.0],
                    [-63.0, 44.0],
                ]
            ],
        },
    }
    script = f"""
import streamlit as st
import app as app_module

st.session_state["drawings"] = [{ROI!r}]
st.session_state["active_roi"] = {ROI!r}
st.session_state["drawing_warnings"] = []
st.session_state["deleted_changed"] = app_module._apply_map_drawings_payload(
    {{"all_drawings": []}}
)
st.session_state["replacement_changed"] = (
    app_module._apply_map_drawings_payload(
        {{"all_drawings": [{replacement!r}]}}
    )
)
"""
    app = AppTest.from_string(script, default_timeout=20).run()

    assert not list(app.exception)
    assert app.session_state["deleted_changed"] is True
    assert app.session_state["replacement_changed"] is True
    assert app.session_state["drawings"] == [replacement]
    assert app.session_state["active_roi"] == replacement


def test_returned_map_payload_triggers_one_state_transition_then_stabilizes() -> None:
    replacement = {
        **ROI,
        "geometry": {
            **ROI["geometry"],
            "coordinates": [
                [
                    [-63.0, 44.0],
                    [-62.0, 44.0],
                    [-62.0, 45.0],
                    [-63.0, 45.0],
                    [-63.0, 44.0],
                ]
            ],
        },
    }
    extra_setup = f"""
original_st_folium = app_module.st_folium
replacement_roi = {replacement!r}

def fake_st_folium(*args, **kwargs):
    calls = int(app_module.st.session_state.get("fake_map_calls", 0))
    app_module.st.session_state["fake_map_calls"] = calls + 1
    return {{"all_drawings": [replacement_roi]}}

app_module.st_folium = fake_st_folium
"""
    app = AppTest.from_string(
        _app_with_fake_chs(
            extra_setup,
            "app_module.st_folium = original_st_folium",
        ),
        default_timeout=20,
    ).run()

    assert not list(app.exception)
    assert app.session_state["fake_map_calls"] == 2
    assert app.session_state["drawings"] == [replacement]
    assert app.session_state["active_roi"] == replacement


def test_initial_render_loads_default_chs_water_levels_without_roi() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()

    assert not list(app.exception)
    assert app.session_state["selected_chs_station_id"] == "bedford-id"
    statuses = app.get("status")
    assert any(
        "CHS water levels loaded for Bedford Institute (00491)"
        in status.label
        and status.state == "complete"
        for status in statuses
    )
    assert any(
        "Official CHS observations loaded" in element.value
        for element in app.success
    )
    metric_values = {
        metric.label: metric.value for metric in app.metric
    }
    assert metric_values["Latest observation"] == "1.234 m"
    assert metric_values["Observation QC"] == "Good"
    assert any(
        button.label == "Download raw fetched CHS JSON"
        and not button.disabled
        for button in app.get("download_button")
    )


def test_observation_age_uses_current_time_not_rounded_query_anchor() -> None:
    extra_setup = """
original_utc_now = app_module._utc_now
app_module._utc_now = lambda: app_module.datetime(
    2026, 7, 23, 20, 14, tzinfo=app_module.timezone.utc
)
"""
    script = _app_with_fake_chs(
        extra_setup,
        "app_module._utc_now = original_utc_now",
    )

    app = AppTest.from_string(script, default_timeout=20).run()

    assert not list(app.exception)
    metric_values = {
        metric.label: metric.value for metric in app.metric
    }
    assert metric_values["Observation age"] == "29 min"


def test_drawing_automatically_selects_station_inside_exact_roi() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI

    app.run()

    assert not list(app.exception)
    assert app.session_state["selected_chs_station_id"] == "roi-gauge-id"
    assert any(
        "ROI Gauge (99991) lies inside the exact drawn region"
        in element.value
        for element in app.success
    )


def test_drawing_without_station_uses_nearest_and_reports_distance() -> None:
    small_halifax_roi = {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-63.59, 44.65],
                    [-63.57, 44.65],
                    [-63.57, 44.67],
                    [-63.59, 44.67],
                    [-63.59, 44.65],
                ]
            ],
        },
    }
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()
    app.session_state["drawings"] = [small_halifax_roi]
    app.session_state["active_roi"] = small_halifax_roi

    app.run()

    assert not list(app.exception)
    assert app.session_state["selected_chs_station_id"] == "bedford-id"
    assert any(
        "No operating CHS observation station lies inside this region"
        in element.value
        and "km outside the exact boundary" in element.value
        for element in app.info
    )


def test_failed_roi_station_keeps_explicitly_labelled_fallback_data() -> None:
    extra_setup = """
successful_bundle = fake_bundle

def selective_bundle(api_root, station, anchor):
    if station.id == "roi-gauge-id":
        return None, "The selected gauge returned no usable recent series.", ()
    return successful_bundle(api_root, station, anchor)

selective_bundle.clear = lambda: None
app_module._cached_chs_bundle = selective_bundle
"""
    app = AppTest.from_string(
        _app_with_fake_chs(extra_setup),
        default_timeout=20,
    ).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI

    app.run()

    assert not list(app.exception)
    assert app.session_state["selected_chs_station_id"] == "roi-gauge-id"
    assert any(
        "showing fallback data from Bedford Institute (00491)"
        in status.label
        and status.state == "error"
        for status in app.get("status")
    )
    assert any(
        "Water-level data for ROI Gauge (99991) could not be loaded"
        in warning.value
        and "Bedford Institute (00491)" in warning.value
        for warning in app.warning
    )
    metric_values = {
        metric.label: metric.value for metric in app.metric
    }
    assert metric_values["Latest observation"] == "1.234 m"


def test_manual_station_without_roi_is_not_called_the_default() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()
    station_select = next(
        element
        for element in app.selectbox
        if element.label == "CHS water-level station"
    )

    station_select.select("roi-gauge-id").run()

    assert not list(app.exception)
    assert any(
        "ROI Gauge (99991) is your manual station selection"
        in element.value
        for element in app.info
    )
    assert not any(
        "ROI Gauge (99991) is the national default" in element.value
        for element in app.info
    )
