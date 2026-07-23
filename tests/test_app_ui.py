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


def test_last_successful_live_fetch_has_prominent_feedback() -> None:
    app = AppTest.from_file("app.py", default_timeout=20).run()
    app.session_state["current_source_mode"] = "live"
    app.session_state["fetch_timestamp"] = datetime(
        2026,
        7,
        23,
        20,
        15,
        tzinfo=timezone.utc,
    )
    app.session_state["raw_feature_count"] = 7
    app.session_state["clipped_feature_count"] = 3

    app.run()

    assert not list(app.exception)
    messages = [element.value for element in app.success]
    assert any(
        "Last successful ECCC fetch" in message
        and "7 returned" in message
        and "3 intersected" in message
        for message in messages
    )


def test_live_fetch_action_shows_completion_status_without_network() -> None:
    script = """
import app as app_module

original_fetch = app_module._cached_fetch

def fake_fetch(api_url, language, rounded_bbox):
    return {"type": "FeatureCollection", "features": []}

app_module._cached_fetch = fake_fetch
try:
    app_module.main()
finally:
    app_module._cached_fetch = original_fetch
"""
    app = AppTest.from_string(script, default_timeout=20).run()
    app.session_state["drawings"] = [ROI]
    app.session_state["active_roi"] = ROI
    app.run()

    fetch_button = next(
        button for button in app.button if button.label == "Fetch ECCC data"
    )
    fetch_button.click().run()

    assert not list(app.exception)
    statuses = app.get("status")
    assert len(statuses) == 1
    assert statuses[0].state == "complete"
    assert "ECCC fetch complete" in statuses[0].label
    assert "0 returned" in statuses[0].label
