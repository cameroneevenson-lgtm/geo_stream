from __future__ import annotations

from datetime import datetime, timezone

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
    app.session_state["last_requested_archive_date"] = datetime(
        2026,
        7,
        11,
        tzinfo=timezone.utc,
    ).date()
    app.session_state["selected_archive_date"] = datetime(
        2026,
        7,
        11,
        tzinfo=timezone.utc,
    ).date()
    app.session_state["archive_product_count"] = 13
    app.session_state["raw_feature_count"] = 7
    app.session_state["clipped_feature_count"] = 3

    app.run()

    assert not list(app.exception)
    messages = [element.value for element in app.success]
    assert any(
        "Loaded ECCC archive issue 2026-07-11" in message
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
    return ArchiveFetchResult(
        collection={"type": "FeatureCollection", "features": []},
        products=(),
        documents=(),
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
        if button.label == "Fetch archived ECCC forecast"
    )
    fetch_button.click().run()

    assert not list(app.exception)
    statuses = app.get("status")
    archive_status = next(
        status
        for status in statuses
        if "ECCC archive fetch complete" in status.label
    )
    assert archive_status.state == "complete"
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


def test_initial_render_explains_archive_without_fetching() -> None:
    app = AppTest.from_string(
        _app_with_fake_chs(),
        default_timeout=20,
    ).run()

    assert not list(app.exception)
    assert any(
        element.label == "Archived ECCC issue date (UTC)"
        for element in app.get("date_input")
    )
    captions = [element.value for element in app.caption]
    assert any("not observed floods" in value for value in captions)
    assert any("does not contact ECCC" in value for value in captions)


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
