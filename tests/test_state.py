from __future__ import annotations

from coastal_flood_explorer.state import (
    MAP_RETURNED_OBJECTS,
    reconcile_drawings,
    roi_matches,
)


def _feature(x0: float) -> dict:
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [x0, 45.0],
                    [x0 + 1.0, 45.0],
                    [x0 + 1.0, 46.0],
                    [x0, 46.0],
                    [x0, 45.0],
                ]
            ],
        },
    }


def test_newest_valid_drawing_is_active() -> None:
    first = _feature(-65.0)
    second = _feature(-63.0)
    state = reconcile_drawings([first, second])
    assert len(state.drawings) == 2
    assert state.active_roi == second


def test_explicit_empty_drawings_clears_active_roi() -> None:
    state = reconcile_drawings([])
    assert state.drawings == ()
    assert state.active_roi is None


def test_deleting_active_drawing_uses_newest_remaining_roi() -> None:
    first = _feature(-65.0)
    second = _feature(-63.0)
    before = reconcile_drawings([first, second])
    after = reconcile_drawings([first])

    assert before.active_roi == second
    assert after.active_roi == first


def test_edited_geometry_is_preserved_as_active() -> None:
    edited = _feature(-65.0)
    edited["geometry"]["coordinates"][0][1][0] = -63.5

    state = reconcile_drawings([edited])

    assert state.active_roi == edited
    assert state.drawings[0]["geometry"] == edited["geometry"]


def test_unsupported_drawing_is_skipped() -> None:
    state = reconcile_drawings(
        [{"type": "Feature", "properties": {}, "geometry": {"type": "Point"}}]
    )
    assert state.active_roi is None
    assert state.warnings


def test_roi_match_is_topological() -> None:
    left = _feature(-65.0)
    right = _feature(-65.0)
    right["geometry"]["coordinates"][0] = list(
        reversed(right["geometry"]["coordinates"][0])
    )
    assert roi_matches(left, right)


def test_changed_roi_does_not_match_previous_results() -> None:
    assert not roi_matches(_feature(-65.0), _feature(-64.5))


def test_only_drawing_changes_trigger_map_reruns() -> None:
    assert MAP_RETURNED_OBJECTS == ("all_drawings",)
