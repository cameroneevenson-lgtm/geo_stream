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


def test_last_successful_archive_fetch_has_prominent_feedback() -> None:
    app = AppTest.from_file("app.py", default_timeout=20).run()
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
    script = """
import app as app_module
from coastal_flood_explorer.archive import ArchiveFetchResult

original_fetch = app_module._cached_archive_fetch

def fake_fetch(archive_root, archive_date):
    return ArchiveFetchResult(
        collection={"type": "FeatureCollection", "features": []},
        products=(),
        documents=(),
    )

app_module._cached_archive_fetch = fake_fetch
try:
    app_module.main()
finally:
    app_module._cached_archive_fetch = original_fetch
"""
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
    assert len(statuses) == 1
    assert statuses[0].state == "complete"
    assert "ECCC archive fetch complete" in statuses[0].label
    assert "0 file(s)" in statuses[0].label
    assert "0 feature(s)" in statuses[0].label
    assert app.session_state["current_source_mode"] == "archive"
    assert app.session_state["raw_archive_download"] is not None
    downloads = {
        button.label: button for button in app.get("download_button")
    }
    assert set(downloads) == {
        "Download clipped GeoJSON",
        "Download raw fetched ECCC JSON",
    }
    assert downloads["Download raw fetched ECCC JSON"].disabled is False


def test_initial_render_explains_archive_without_fetching() -> None:
    app = AppTest.from_file("app.py", default_timeout=20).run()

    assert not list(app.exception)
    assert any(
        element.label == "Archived ECCC issue date (UTC)"
        for element in app.get("date_input")
    )
    captions = [element.value for element in app.caption]
    assert any("not observed floods" in value for value in captions)
    assert any("does not contact ECCC" in value for value in captions)
