"""Date-window helpers for ECCC's rolling recent archive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


ARCHIVE_RETENTION_DAYS = 30


@dataclass(frozen=True)
class ArchiveDateWindow:
    """Inclusive UTC issue-date bounds and the initial selection."""

    oldest: date
    newest: date
    default: date

    def contains(self, value: date) -> bool:
        """Return whether an issue date is within the advertised window."""

        return self.oldest <= value <= self.newest


def recent_archive_window(
    now: datetime | date | None = None,
) -> ArchiveDateWindow:
    """Return the current inclusive 30-day archive selection window.

    Directory dates are interpreted in UTC. Today is selected initially so the
    latest archived forecasts are one deliberate click away, although the UI
    must explain that today's publication may still be in progress. This helper
    performs no network activity; choosing a date must not fetch data.
    """

    today = _utc_date(now)
    return ArchiveDateWindow(
        oldest=today - timedelta(days=ARCHIVE_RETENTION_DAYS - 1),
        newest=today,
        default=today,
    )


def _utc_date(value: datetime | date | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Archive clock datetimes must include a timezone.")
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    raise TypeError("Archive clock must be a date, datetime, or None.")
