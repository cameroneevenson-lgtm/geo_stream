"""Canadian Hydrographic Service water-level client and pure helpers.

The Integrated Water Level System (IWLS) API exposes point observations and
tide predictions for stations across Canada.  This module deliberately owns
only transport, validation, normalization, and station ranking.  Polygon
geometry remains in :mod:`coastal_flood_explorer.geometry`; callers pass
precomputed point-to-ROI metrics to :func:`select_station_for_roi`.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any, TypeAlias, overload
from urllib.parse import quote, urlsplit, urlunsplit

import pandas as pd
import requests

from .api import REQUEST_TIMEOUT, _configure_session, _content_type

logger = logging.getLogger(__name__)

CHS_API_ROOT = "https://api-sine.dfo-mpo.gc.ca/api/v1"
DEFAULT_STATION_CODE = "00491"
OBSERVATION_SERIES_CODE = "wlo"
PREDICTION_SERIES_CODE = "wlp"
WATER_LEVEL_RESOLUTION = "FIFTEEN_MINUTES"
OBSERVATION_LOOKBACK = timedelta(hours=24)
PREDICTION_LOOKBACK = timedelta(hours=24)
PREDICTION_LOOKAHEAD = timedelta(hours=24)
ANCHOR_INTERVAL = timedelta(minutes=15)
MAX_CATALOG_ITEMS = 10_000
MAX_SERIES_POINTS = 10_000

QC_LABELS: Mapping[str, str] = {
    "1": "Good",
    "2": "Not evaluated, not available or unknown",
    "3": "Questionable/suspect",
}
UNKNOWN_QC_LABEL = "Unknown"

CHART_TIME_COLUMN = "Time (UTC)"
CHART_OBSERVED_COLUMN = "Observed water level (m)"
CHART_PREDICTED_COLUMN = "Predicted water level (m)"
CHART_OBSERVED_QC_COLUMN = "Observed QC"
CHART_PREDICTED_QC_COLUMN = "Predicted QC"
CHART_COLUMNS = (
    CHART_TIME_COLUMN,
    CHART_OBSERVED_COLUMN,
    CHART_PREDICTED_COLUMN,
    CHART_OBSERVED_QC_COLUMN,
    CHART_PREDICTED_QC_COLUMN,
)

_STATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_JSON_ATOMIC: TypeAlias = type(None) | bool | int | float | str
JSONValue: TypeAlias = (
    _JSON_ATOMIC | list["JSONValue"] | dict[str, "JSONValue"]
)
Clock: TypeAlias = Callable[[], datetime]


class CHSError(RuntimeError):
    """Base class for CHS errors whose messages are safe to show to users."""


class CHSConfigurationError(CHSError, ValueError):
    """Raised when client inputs or configuration are unsafe or invalid."""


class CHSRequestError(CHSError):
    """Raised when an IWLS HTTP request cannot be completed."""


class CHSResponseError(CHSError):
    """Raised when IWLS returns an invalid or unsupported response."""


class CHSDataUnavailableError(CHSError):
    """Raised when no usable requested water-level series is available."""

    def __init__(
        self,
        message: str,
        *,
        warnings: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.warnings = tuple(warnings)


@dataclass(frozen=True, slots=True)
class CHSStation:
    """One operating or historical IWLS station."""

    id: str
    code: str
    official_name: str
    latitude: float
    longitude: float
    operating: bool
    time_series_codes: tuple[str, ...]
    station_type: str | None = None
    alternative_name: str | None = None

    @property
    def label(self) -> str:
        """Return a compact label suitable for maps and selectors."""

        return f"{self.official_name} ({self.code})"

    def offers(self, series_code: str) -> bool:
        """Return whether the station advertises a time-series code."""

        normalized = str(series_code).strip().casefold()
        return any(
            isinstance(code, str) and code.casefold() == normalized
            for code in self.time_series_codes
        )

    def metadata(self) -> dict[str, JSONValue]:
        """Return stable, strict-JSON-compatible station metadata."""

        return {
            "id": self.id,
            "code": self.code,
            "official_name": self.official_name,
            "alternative_name": self.alternative_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "operating": self.operating,
            "station_type": self.station_type,
            "time_series_codes": list(self.time_series_codes),
        }


@dataclass(frozen=True, slots=True)
class WaterLevelPoint:
    """One normalized IWLS water-level value in metres."""

    timestamp: datetime
    value_m: float
    qc_code: str | None
    reviewed: bool | None = None
    uncertainty_m: float | None = None
    time_series_id: str | None = None
    qualifier: str | None = None

    @property
    def event_time(self) -> datetime:
        """Compatibility alias using the IWLS field's terminology."""

        return self.timestamp

    @property
    def qc_flag_code(self) -> str | None:
        """Compatibility alias for the source ``qcFlagCode`` field."""

        return self.qc_code

    @property
    def qc_label(self) -> str:
        """Return the documented human-readable CHS QC label."""

        if self.qc_code is None:
            return UNKNOWN_QC_LABEL
        return QC_LABELS.get(
            self.qc_code,
            f"{UNKNOWN_QC_LABEL} ({self.qc_code})",
        )


@dataclass(frozen=True, slots=True)
class WaterLevelSeries:
    """A normalized response for one IWLS station time series."""

    code: str
    label: str
    points: tuple[WaterLevelPoint, ...]
    start_time: datetime
    end_time: datetime
    resolution: str = WATER_LEVEL_RESOLUTION
    raw_point_count: int = 0
    skipped_count: int = 0
    diagnostics: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        """Return whether at least one usable point was decoded."""

        return bool(self.points)


@dataclass(frozen=True, slots=True)
class CHSRawDocument:
    """A strict-JSON snapshot of one exact decoded IWLS list response."""

    series_code: str
    url: str
    query: tuple[tuple[str, str], ...]
    payload_json: str = field(repr=False)

    @property
    def query_metadata(self) -> dict[str, str]:
        """Return query pairs as an insertion-ordered dictionary."""

        return dict(self.query)

    def payload(self) -> list[JSONValue]:
        """Return a fresh copy of the exact decoded list response."""

        value = json.loads(self.payload_json)
        if not isinstance(value, list):  # Defensive against manual instances.
            raise CHSResponseError(
                "Stored CHS source data is not a JSON list."
            )
        return value


@dataclass(frozen=True, slots=True)
class CHSWaterLevelBundle:
    """Station metadata, usable series, diagnostics, and raw source data."""

    station: CHSStation
    anchor_time: datetime
    fetched_at: datetime
    observed: WaterLevelSeries | None
    predicted: WaterLevelSeries | None
    warnings: tuple[str, ...] = ()
    raw_documents: tuple[CHSRawDocument, ...] = ()
    api_root: str = CHS_API_ROOT

    @property
    def observation_series(self) -> WaterLevelSeries | None:
        """Descriptive alias for :attr:`observed`."""

        return self.observed

    @property
    def prediction_series(self) -> WaterLevelSeries | None:
        """Descriptive alias for :attr:`predicted`."""

        return self.predicted

    @property
    def point_count(self) -> int:
        """Return the number of usable points across both series."""

        return sum(
            len(series.points)
            for series in (self.observed, self.predicted)
            if series is not None
        )


@dataclass(frozen=True, slots=True)
class CHSStationCatalog(Sequence[CHSStation]):
    """Operating observation stations plus catalogue diagnostics."""

    stations: tuple[CHSStation, ...]
    fetched_at: datetime
    skipped_count: int = 0
    warnings: tuple[str, ...] = ()
    raw_payload_json: str = field(default="[]", repr=False)

    @overload
    def __getitem__(self, index: int) -> CHSStation:
        ...

    @overload
    def __getitem__(self, index: slice) -> tuple[CHSStation, ...]:
        ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> CHSStation | tuple[CHSStation, ...]:
        return self.stations[index]

    def __len__(self) -> int:
        return len(self.stations)

    def raw_payload(self) -> list[JSONValue]:
        """Return a fresh copy of the source station-list response."""

        value = json.loads(self.raw_payload_json)
        if not isinstance(value, list):  # Defensive against manual instances.
            raise CHSResponseError(
                "Stored CHS station catalogue is not a JSON list."
            )
        return value


@dataclass(frozen=True, slots=True)
class StationProximity:
    """Geometry-owned metrics for one station point relative to an ROI."""

    station_id: str
    inside_roi: bool
    distance_to_roi_km: float
    distance_to_center_km: float


@dataclass(frozen=True, slots=True)
class CHSStationMatch:
    """The deterministic result of ROI-based station selection."""

    station: CHSStation
    inside_roi: bool
    distance_to_roi_km: float
    distance_to_center_km: float


class CHSClient:
    """Fetch normalized station observations and astronomical predictions."""

    def __init__(
        self,
        api_root: str = CHS_API_ROOT,
        *,
        session: requests.Session | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.api_root = _validate_api_root(api_root)
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)
        self.clock = clock if clock is not None else _system_utc_now

    def fetch_catalog(self) -> CHSStationCatalog:
        """Fetch operating stations that advertise official observations.

        The IWLS station filter also returns non-operating historical
        stations, so the operating and ``wlo`` checks are repeated locally.
        A malformed individual entry is skipped without discarding the rest
        of the catalogue.
        """

        url = f"{self.api_root}/stations"
        params = {"time-series-code": OBSERVATION_SERIES_CODE}
        payload, payload_json = self._get_json_list(
            url,
            params=params,
            resource_label="CHS station catalogue",
            max_items=MAX_CATALOG_ITEMS,
        )

        stations: list[CHSStation] = []
        diagnostics: list[str] = []
        skipped = 0
        for index, item in enumerate(payload):
            try:
                station = _parse_station(item)
            except ValueError as exc:
                skipped += 1
                diagnostics.append(
                    f"Skipped CHS station item {index + 1}: {exc}"
                )
                continue
            if not station.operating:
                continue
            if not station.offers(OBSERVATION_SERIES_CODE):
                skipped += 1
                diagnostics.append(
                    f"Skipped CHS station item {index + 1}: it did not "
                    "advertise official water-level observations."
                )
                continue
            stations.append(station)

        # A station ID is the API path identifier.  Keep one deterministic
        # station per ID if an upstream response happens to repeat an item.
        unique = {station.id: station for station in stations}
        duplicate_count = len(stations) - len(unique)
        if duplicate_count:
            skipped += duplicate_count
            diagnostics.append(
                f"Skipped {duplicate_count} duplicate CHS station "
                f"{'entry' if duplicate_count == 1 else 'entries'}."
            )
        ordered = tuple(
            sorted(
                unique.values(),
                key=lambda station: (
                    station.official_name.casefold(),
                    station.code,
                    station.id,
                ),
            )
        )
        if not ordered:
            raise CHSDataUnavailableError(
                "CHS did not return any usable operating water-level "
                "stations.",
                warnings=diagnostics,
            )
        return CHSStationCatalog(
            stations=ordered,
            fetched_at=_clock_time(self.clock),
            skipped_count=skipped,
            warnings=tuple(diagnostics),
            raw_payload_json=payload_json,
        )

    def fetch_bundle(
        self,
        station: CHSStation,
        anchor: datetime | None = None,
    ) -> CHSWaterLevelBundle:
        """Fetch a 24-hour observation window and a 48-hour tide window.

        An explicit ``anchor`` must be timezone-aware and exactly aligned to a
        15-minute UTC boundary.  When omitted, the injected clock is floored
        to the previous such boundary.
        """

        valid_station = _validate_station_for_fetch(station)
        anchor_time = (
            floor_to_anchor(_clock_time(self.clock))
            if anchor is None
            else validate_anchor(anchor)
        )

        requested: list[
            tuple[str, str, datetime, datetime]
        ] = [
            (
                OBSERVATION_SERIES_CODE,
                "Observed water level",
                anchor_time - OBSERVATION_LOOKBACK,
                anchor_time,
            )
        ]
        warnings: list[str] = []
        if valid_station.offers(PREDICTION_SERIES_CODE):
            requested.append(
                (
                    PREDICTION_SERIES_CODE,
                    "Predicted water level",
                    anchor_time - PREDICTION_LOOKBACK,
                    anchor_time + PREDICTION_LOOKAHEAD,
                )
            )
        else:
            warnings.append(
                f"{valid_station.label} does not advertise astronomical "
                "water-level predictions."
            )

        series_by_code: dict[str, WaterLevelSeries | None] = {
            OBSERVATION_SERIES_CODE: None,
            PREDICTION_SERIES_CODE: None,
        }
        raw_documents: list[CHSRawDocument] = []
        for code, label, start_time, end_time in requested:
            try:
                series, document = self._fetch_series(
                    valid_station,
                    code=code,
                    label=label,
                    start_time=start_time,
                    end_time=end_time,
                )
            except (CHSRequestError, CHSResponseError) as exc:
                warnings.append(
                    f"{label} data was unavailable: {exc}"
                )
                logger.warning(
                    "CHS %s series failed for station %s",
                    code,
                    valid_station.id,
                    exc_info=True,
                )
                continue

            series_by_code[code] = series
            raw_documents.append(document)
            warnings.extend(series.diagnostics)
            if not series.points:
                warnings.append(
                    f"CHS returned no usable {label.lower()} points for "
                    f"{valid_station.label} in the requested window."
                )

        observed = series_by_code[OBSERVATION_SERIES_CODE]
        predicted = series_by_code[PREDICTION_SERIES_CODE]
        if not any(
            series is not None and series.points
            for series in (observed, predicted)
        ):
            raise CHSDataUnavailableError(
                "CHS returned no usable water-level data for the selected "
                "station and time window.",
                warnings=warnings,
            )

        return CHSWaterLevelBundle(
            station=valid_station,
            anchor_time=anchor_time,
            fetched_at=_clock_time(self.clock),
            observed=observed,
            predicted=predicted,
            warnings=tuple(warnings),
            raw_documents=tuple(raw_documents),
            api_root=self.api_root,
        )

    def _fetch_series(
        self,
        station: CHSStation,
        *,
        code: str,
        label: str,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[WaterLevelSeries, CHSRawDocument]:
        station_id = quote(station.id, safe="")
        url = f"{self.api_root}/stations/{station_id}/data"
        params = {
            "time-series-code": code,
            "from": _utc_text(start_time),
            "to": _utc_text(end_time),
            "resolution": WATER_LEVEL_RESOLUTION,
        }
        payload, payload_json = self._get_json_list(
            url,
            params=params,
            resource_label=f"CHS {label.lower()} response",
            max_items=MAX_SERIES_POINTS,
        )
        series = _parse_series(
            payload,
            code=code,
            label=label,
            start_time=start_time,
            end_time=end_time,
        )
        document = CHSRawDocument(
            series_code=code,
            url=url,
            query=tuple(params.items()),
            payload_json=payload_json,
        )
        return series, document

    def _get_json_list(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        resource_label: str,
        max_items: int,
    ) -> tuple[list[Any], str]:
        try:
            response = self.session.get(
                url,
                params=dict(params),
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            logger.warning(
                "%s timed out for %s", resource_label, url, exc_info=True
            )
            raise CHSRequestError(
                "The CHS request timed out. Please try again."
            ) from exc
        except requests.ConnectionError as exc:
            logger.warning(
                "Could not connect to %s at %s",
                resource_label,
                url,
                exc_info=True,
            )
            raise CHSRequestError(
                "Could not connect to the CHS water-level service. Check the "
                "network connection and try again."
            ) from exc
        except requests.RequestException as exc:
            logger.warning(
                "%s request failed for %s",
                resource_label,
                url,
                exc_info=True,
            )
            raise CHSRequestError(
                "The CHS request could not be completed. Please try again."
            ) from exc

        _raise_for_status(response.status_code, resource_label)
        media_type = _response_media_type(response)
        if not _is_json_media_type(media_type):
            raise CHSResponseError(
                f"The {resource_label} did not return a supported JSON "
                "content type."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning(
                "%s contained invalid JSON", resource_label, exc_info=True
            )
            raise CHSResponseError(
                f"The {resource_label} did not contain valid JSON."
            ) from exc
        if not isinstance(payload, list):
            raise CHSResponseError(
                f"The {resource_label} was not a JSON list."
            )
        if len(payload) > max_items:
            raise CHSResponseError(
                f"The {resource_label} contained more than {max_items} "
                "items, so retrieval was stopped."
            )
        try:
            payload_json = _strict_json_text(payload)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "%s contained non-JSON values",
                resource_label,
                exc_info=True,
            )
            raise CHSResponseError(
                f"The {resource_label} contained invalid JSON values."
            ) from exc
        return payload, payload_json


def validate_anchor(value: datetime) -> datetime:
    """Validate an aware datetime on an exact 15-minute UTC boundary."""

    normalized = _aware_utc(value, "The CHS query anchor")
    if (
        normalized.minute % 15 != 0
        or normalized.second != 0
        or normalized.microsecond != 0
    ):
        raise CHSConfigurationError(
            "The CHS query anchor must be aligned to an exact 15-minute UTC "
            "boundary."
        )
    return normalized


def floor_to_anchor(value: datetime) -> datetime:
    """Floor an aware datetime to the preceding 15-minute UTC boundary."""

    normalized = _aware_utc(value, "The CHS query clock")
    minute = normalized.minute - (normalized.minute % 15)
    return normalized.replace(minute=minute, second=0, microsecond=0)


def select_station_for_roi(
    stations: Iterable[CHSStation],
    point_matches: (
        Mapping[str, StationProximity] | Iterable[StationProximity]
    ),
) -> CHSStationMatch:
    """Select an observation station using geometry-owned ROI metrics.

    Stations inside the exact ROI are preferred.  Among those, a station that
    also offers predictions wins before distance to the ROI representative
    centre.  If no station is inside, distance to the ROI boundary wins first,
    followed by prediction availability and centre distance.
    """

    metrics = _proximity_by_station_id(point_matches)
    candidates: list[CHSStationMatch] = []
    for station in stations:
        if (
            not isinstance(station, CHSStation)
            or not station.operating
            or not station.offers(OBSERVATION_SERIES_CODE)
        ):
            continue
        proximity = metrics.get(station.id)
        if proximity is None:
            continue
        candidates.append(
            CHSStationMatch(
                station=station,
                inside_roi=proximity.inside_roi,
                distance_to_roi_km=proximity.distance_to_roi_km,
                distance_to_center_km=proximity.distance_to_center_km,
            )
        )

    if not candidates:
        raise CHSDataUnavailableError(
            "No operating CHS observation station could be matched to the "
            "selected region."
        )

    inside = [match for match in candidates if match.inside_roi]
    if inside:
        return min(
            inside,
            key=lambda match: (
                not match.station.offers(PREDICTION_SERIES_CODE),
                match.distance_to_center_km,
                match.distance_to_roi_km,
                match.station.code,
                match.station.id,
            ),
        )
    return min(
        candidates,
        key=lambda match: (
            match.distance_to_roi_km,
            not match.station.offers(PREDICTION_SERIES_CODE),
            match.distance_to_center_km,
            match.station.code,
            match.station.id,
        ),
    )


def select_station(
    stations: Iterable[CHSStation],
    point_matches: (
        Mapping[str, StationProximity] | Iterable[StationProximity]
    ),
) -> CHSStationMatch:
    """Short alias for :func:`select_station_for_roi`."""

    return select_station_for_roi(stations, point_matches)


def latest_point(
    series: WaterLevelSeries | None,
) -> WaterLevelPoint | None:
    """Return the latest point in a normalized series."""

    if series is None or not series.points:
        return None
    return series.points[-1]


def nearest_point(
    series: WaterLevelSeries | None,
    when: datetime,
    *,
    tolerance: timedelta | None = None,
) -> WaterLevelPoint | None:
    """Return the point nearest an aware instant, preferring earlier ties."""

    target = _aware_utc(when, "The comparison time")
    if tolerance is not None and (
        not isinstance(tolerance, timedelta)
        or tolerance < timedelta(0)
    ):
        raise CHSConfigurationError(
            "The nearest-point tolerance must be a non-negative duration."
        )
    if series is None or not series.points:
        return None
    point = min(
        series.points,
        key=lambda candidate: (
            abs(candidate.timestamp - target),
            candidate.timestamp,
        ),
    )
    if (
        tolerance is not None
        and abs(point.timestamp - target) > tolerance
    ):
        return None
    return point


def series_statistics(
    series: WaterLevelSeries | None,
) -> dict[str, int | float | datetime | None]:
    """Return stable basic statistics for one normalized series."""

    if series is None or not series.points:
        return {
            "count": 0,
            "minimum_m": None,
            "maximum_m": None,
            "mean_m": None,
            "latest_m": None,
            "latest_time_utc": None,
        }
    values = [point.value_m for point in series.points]
    latest = series.points[-1]
    return {
        "count": len(values),
        "minimum_m": min(values),
        "maximum_m": max(values),
        "mean_m": fmean(values),
        "latest_m": latest.value_m,
        "latest_time_utc": latest.timestamp,
    }


def water_level_chart_frame(bundle: CHSWaterLevelBundle) -> pd.DataFrame:
    """Return a stable wide chart frame with nullable values and QC labels."""

    if not isinstance(bundle, CHSWaterLevelBundle):
        raise CHSConfigurationError(
            "A valid CHS water-level bundle is required for charting."
        )
    rows: dict[datetime, dict[str, Any]] = {}
    for series, value_column, qc_column in (
        (
            bundle.observed,
            CHART_OBSERVED_COLUMN,
            CHART_OBSERVED_QC_COLUMN,
        ),
        (
            bundle.predicted,
            CHART_PREDICTED_COLUMN,
            CHART_PREDICTED_QC_COLUMN,
        ),
    ):
        if series is None:
            continue
        for point in series.points:
            row = rows.setdefault(
                point.timestamp,
                {
                    CHART_TIME_COLUMN: point.timestamp,
                    CHART_OBSERVED_COLUMN: None,
                    CHART_PREDICTED_COLUMN: None,
                    CHART_OBSERVED_QC_COLUMN: None,
                    CHART_PREDICTED_QC_COLUMN: None,
                },
            )
            row[value_column] = point.value_m
            row[qc_column] = point.qc_label

    if not rows:
        return _empty_chart_frame()
    frame = pd.DataFrame(
        [rows[timestamp] for timestamp in sorted(rows)],
        columns=CHART_COLUMNS,
    )
    # Pin nanosecond resolution so the populated frame matches
    # ``_empty_chart_frame`` regardless of the pandas default datetime unit
    # (pandas >= 2.2 can otherwise infer microseconds here).
    frame[CHART_TIME_COLUMN] = pd.to_datetime(
        frame[CHART_TIME_COLUMN],
        utc=True,
    ).astype("datetime64[ns, UTC]")
    for column in (CHART_OBSERVED_COLUMN, CHART_PREDICTED_COLUMN):
        frame[column] = pd.array(frame[column], dtype="Float64")
    for column in (CHART_OBSERVED_QC_COLUMN, CHART_PREDICTED_QC_COLUMN):
        frame[column] = pd.array(frame[column], dtype="string")
    return frame


def chart_frame(bundle: CHSWaterLevelBundle) -> pd.DataFrame:
    """Short alias for :func:`water_level_chart_frame`."""

    return water_level_chart_frame(bundle)


def raw_bundle_bytes(bundle: CHSWaterLevelBundle) -> bytes:
    """Serialize a strict UTF-8 bundle with raw responses and query metadata."""

    if not isinstance(bundle, CHSWaterLevelBundle):
        raise CHSConfigurationError(
            "A valid CHS water-level bundle is required for raw export."
        )
    responses: list[dict[str, JSONValue]] = []
    for document in bundle.raw_documents:
        if not isinstance(document, CHSRawDocument):
            raise CHSConfigurationError(
                "The CHS bundle contains invalid raw source data."
            )
        responses.append(
            {
                "series_code": document.series_code,
                "url": document.url,
                "query": dict(document.query),
                "payload": document.payload(),
            }
        )

    result: dict[str, JSONValue] = {
        "source": (
            "Canadian Hydrographic Service Integrated Water Level System"
        ),
        "api_root": bundle.api_root,
        "station": bundle.station.metadata(),
        "query_metadata": {
            "anchor_time": _utc_text(bundle.anchor_time),
            "resolution": WATER_LEVEL_RESOLUTION,
            "observation_lookback_hours": 24,
            "prediction_lookback_hours": 24,
            "prediction_lookahead_hours": 24,
        },
        "fetched_at": _utc_text(bundle.fetched_at),
        "warnings": list(bundle.warnings),
        "responses": responses,
    }
    try:
        return _strict_json_text(result).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CHSConfigurationError(
            "The CHS raw download contains a non-JSON value."
        ) from exc


def _parse_station(value: Any) -> CHSStation:
    if not isinstance(value, Mapping):
        raise ValueError("the entry was not a JSON object.")
    station_id = _required_text(value.get("id"), "station ID")
    if _STATION_ID.fullmatch(station_id) is None:
        raise ValueError("the station ID was invalid.")
    code = _required_text(value.get("code"), "station code")
    official_name = _required_text(
        value.get("officialName"),
        "official station name",
    )
    latitude = _finite_number(value.get("latitude"), "station latitude")
    longitude = _finite_number(value.get("longitude"), "station longitude")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("the station latitude was outside -90 to 90.")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("the station longitude was outside -180 to 180.")
    operating = value.get("operating")
    if not isinstance(operating, bool):
        raise ValueError("the station operating flag was invalid.")
    time_series = value.get("timeSeries")
    if not isinstance(time_series, list):
        raise ValueError("the station time-series list was invalid.")
    codes: list[str] = []
    for entry in time_series:
        if not isinstance(entry, Mapping):
            continue
        candidate = entry.get("code")
        if isinstance(candidate, str) and candidate.strip():
            codes.append(candidate.strip().casefold())
    unique_codes = tuple(dict.fromkeys(codes))
    station_type = _optional_text(value.get("type"))
    alternative_name = _optional_text(value.get("alternativeName"))
    return CHSStation(
        id=station_id,
        code=code,
        official_name=official_name,
        latitude=latitude,
        longitude=longitude,
        operating=operating,
        time_series_codes=unique_codes,
        station_type=station_type,
        alternative_name=alternative_name,
    )


def _parse_series(
    payload: Sequence[Any],
    *,
    code: str,
    label: str,
    start_time: datetime,
    end_time: datetime,
) -> WaterLevelSeries:
    points_by_time: dict[datetime, WaterLevelPoint] = {}
    diagnostics: list[str] = []
    skipped = 0
    duplicates = 0
    for index, value in enumerate(payload):
        try:
            point = _parse_point(value)
        except ValueError as exc:
            skipped += 1
            diagnostics.append(
                f"Skipped CHS {code} point {index + 1}: {exc}"
            )
            continue
        if point.timestamp in points_by_time:
            duplicates += 1
        # The later occurrence in an IWLS response deterministically wins.
        points_by_time[point.timestamp] = point
    if duplicates:
        skipped += duplicates
        diagnostics.append(
            f"Deduplicated {duplicates} repeated CHS {code} "
            f"{'timestamp' if duplicates == 1 else 'timestamps'}."
        )
    return WaterLevelSeries(
        code=code,
        label=label,
        points=tuple(
            points_by_time[timestamp]
            for timestamp in sorted(points_by_time)
        ),
        start_time=start_time,
        end_time=end_time,
        resolution=WATER_LEVEL_RESOLUTION,
        raw_point_count=len(payload),
        skipped_count=skipped,
        diagnostics=tuple(diagnostics),
    )


def _parse_point(value: Any) -> WaterLevelPoint:
    if not isinstance(value, Mapping):
        raise ValueError("the entry was not a JSON object.")
    timestamp_text = value.get("eventDate")
    if not isinstance(timestamp_text, str) or not timestamp_text.strip():
        raise ValueError("the event time was missing or invalid.")
    try:
        timestamp = _parse_utc_text(timestamp_text)
    except ValueError as exc:
        raise ValueError(
            "the event time was not an aware ISO 8601 timestamp."
        ) from exc
    level = _finite_number(value.get("value"), "water-level value")

    qc_value = value.get("qcFlagCode")
    if qc_value is None:
        qc_code = None
    elif isinstance(qc_value, bool):
        raise ValueError("the quality-control flag was invalid.")
    elif isinstance(qc_value, (str, int)):
        qc_code = str(qc_value).strip() or None
    else:
        raise ValueError("the quality-control flag was invalid.")

    reviewed_value = value.get("reviewed")
    reviewed = reviewed_value if isinstance(reviewed_value, bool) else None
    uncertainty_value = value.get("uncertainty")
    uncertainty = (
        None
        if uncertainty_value is None
        else _finite_number(uncertainty_value, "uncertainty value")
    )
    time_series_id = _optional_text(value.get("timeSeriesId"))
    qualifier = _optional_text(value.get("qualifier"))
    return WaterLevelPoint(
        timestamp=timestamp,
        value_m=level,
        qc_code=qc_code,
        reviewed=reviewed,
        uncertainty_m=uncertainty,
        time_series_id=time_series_id,
        qualifier=qualifier,
    )


def _validate_station_for_fetch(station: CHSStation) -> CHSStation:
    if not isinstance(station, CHSStation):
        raise CHSConfigurationError(
            "A valid CHS station is required for water-level retrieval."
        )
    if (
        _STATION_ID.fullmatch(station.id) is None
        or not station.operating
        or not station.offers(OBSERVATION_SERIES_CODE)
    ):
        raise CHSConfigurationError(
            "Water-level retrieval requires an operating CHS station that "
            "advertises official observations."
        )
    if (
        not math.isfinite(station.latitude)
        or not -90.0 <= station.latitude <= 90.0
        or not math.isfinite(station.longitude)
        or not -180.0 <= station.longitude <= 180.0
    ):
        raise CHSConfigurationError(
            "The selected CHS station has invalid coordinates."
        )
    return station


def _proximity_by_station_id(
    point_matches: (
        Mapping[str, StationProximity] | Iterable[StationProximity]
    ),
) -> dict[str, StationProximity]:
    if isinstance(point_matches, Mapping):
        values = point_matches.values()
    elif isinstance(point_matches, (str, bytes, bytearray)):
        raise CHSConfigurationError(
            "Station proximity metrics must be keyed by station ID."
        )
    else:
        try:
            values = iter(point_matches)
        except TypeError as exc:
            raise CHSConfigurationError(
                "Station proximity metrics must be keyed by station ID."
            ) from exc
    result: dict[str, StationProximity] = {}
    for value in values:
        if not isinstance(value, StationProximity):
            raise CHSConfigurationError(
                "Every station proximity metric must be a StationProximity."
            )
        if (
            not isinstance(value.station_id, str)
            or not value.station_id
            or not isinstance(value.inside_roi, bool)
            or not _is_nonnegative_finite(value.distance_to_roi_km)
            or not _is_nonnegative_finite(value.distance_to_center_km)
        ):
            raise CHSConfigurationError(
                "A station proximity metric contains an invalid value."
            )
        result[value.station_id] = value
    return result


def _validate_api_root(api_root: str) -> str:
    if not isinstance(api_root, str) or not api_root.strip():
        raise CHSConfigurationError("The CHS API root URL is not configured.")
    candidate = api_root.strip().rstrip("/")
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CHSConfigurationError(
            "The CHS API root URL has an invalid port."
        ) from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise CHSConfigurationError(
            "The CHS API root must be a valid HTTPS URL without credentials, "
            "a query, or a fragment."
        )
    # Normalize only a trailing slash; keep a custom test/deployment path.
    return urlunsplit(
        (
            "https",
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


def _raise_for_status(status_code: int, resource_label: str) -> None:
    if 200 <= status_code < 300:
        return
    if status_code == 429:
        message = (
            "CHS is temporarily limiting water-level requests (HTTP 429). "
            "Please wait and try again."
        )
    elif 500 <= status_code < 600:
        message = (
            "The CHS water-level service is temporarily unavailable "
            f"(HTTP {status_code}). Please try again."
        )
    elif 400 <= status_code < 500:
        message = (
            f"CHS rejected the water-level request (HTTP {status_code}). "
            "Please try again."
        )
    else:
        message = (
            "CHS returned an unexpected water-level HTTP status "
            f"({status_code}). Please try again."
        )
    logger.warning("%s returned HTTP %s", resource_label, status_code)
    raise CHSRequestError(message)


def _response_media_type(response: requests.Response) -> str | None:
    return _content_type(response)


def _is_json_media_type(media_type: str | None) -> bool:
    return (
        media_type == "application/json"
        or (
            isinstance(media_type, str)
            and media_type.startswith("application/")
            and media_type.endswith("+json")
        )
    )


def _strict_json_text(value: Any) -> str:
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite floats are not valid JSON.")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings.")
            _validate_json_value(item)
        return
    raise TypeError(f"{type(value).__name__} is not a JSON value.")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"the {label} was missing or invalid.")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"the {label} was not finite.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"the {label} was not numeric.") from exc
    if not math.isfinite(result):
        raise ValueError(f"the {label} was not finite.")
    return result


def _is_nonnegative_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _parse_utc_text(value: str) -> datetime:
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp is not timezone-aware.")
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CHSConfigurationError(
            f"{label} must be a timezone-aware datetime."
        )
    return value.astimezone(timezone.utc)


def _clock_time(clock: Clock) -> datetime:
    if not callable(clock):
        raise CHSConfigurationError("The CHS client clock is invalid.")
    try:
        value = clock()
    except Exception as exc:
        raise CHSConfigurationError(
            "The CHS client clock could not provide the current time."
        ) from exc
    return _aware_utc(value, "The CHS client clock value")


def _system_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    normalized = _aware_utc(value, "The CHS timestamp")
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty_chart_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            CHART_TIME_COLUMN: pd.Series(
                [],
                dtype="datetime64[ns, UTC]",
            ),
            CHART_OBSERVED_COLUMN: pd.Series([], dtype="Float64"),
            CHART_PREDICTED_COLUMN: pd.Series([], dtype="Float64"),
            CHART_OBSERVED_QC_COLUMN: pd.Series([], dtype="string"),
            CHART_PREDICTED_QC_COLUMN: pd.Series([], dtype="string"),
        },
        columns=CHART_COLUMNS,
    )
