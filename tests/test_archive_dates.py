from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from coastal_flood_explorer.archive_dates import (
    ARCHIVE_RETENTION_DAYS,
    recent_archive_window,
)


def test_recent_archive_window_is_inclusive_and_defaults_to_today() -> None:
    window = recent_archive_window(date(2026, 7, 23))

    assert window.newest == date(2026, 7, 23)
    assert window.oldest == date(2026, 6, 24)
    assert window.default == date(2026, 7, 23)
    assert (window.newest - window.oldest).days + 1 == ARCHIVE_RETENTION_DAYS
    assert window.contains(window.oldest)
    assert window.contains(window.newest)
    assert not window.contains(window.oldest - timedelta(days=1))
    assert not window.contains(window.newest + timedelta(days=1))


def test_recent_archive_window_uses_the_utc_calendar_date() -> None:
    local_instant = datetime(
        2026,
        7,
        22,
        23,
        30,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    window = recent_archive_window(local_instant)

    assert window.newest == date(2026, 7, 23)
    assert window.default == date(2026, 7, 23)


def test_recent_archive_window_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone"):
        recent_archive_window(datetime(2026, 7, 23, 12, 0))
