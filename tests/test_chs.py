"""Offline unit tests for the Canadian Hydrographic Service client."""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest
import requests

from coastal_flood_explorer.api import REQUEST_TIMEOUT, USER_AGENT
from coastal_flood_explorer.chs import (
    CHART_COLUMNS,
    CHART_OBSERVED_COLUMN,
    CHART_OBSERVED_QC_COLUMN,
    CHART_PREDICTED_COLUMN,
    CHART_TIME_COLUMN,
    CHS_API_ROOT,
    DEFAULT_STATION_CODE,
    OBSERVATION_SERIES_CODE,
    PREDICTION_SERIES_CODE,
    WATER_LEVEL_RESOLUTION,
    CHSClient,
    CHSConfigurationError,
    CHSDataUnavailableError,
    CHSRequestError,
    CHSResponseError,
    CHSStation,
    CHSWaterLevelBundle,
    StationProximity,
    WaterLevelPoint,
    WaterLevelSeries,
    chart_frame,
    floor_to_anchor,
    latest_point,
    nearest_point,
    raw_bundle_bytes,
    select_station,
    series_statistics,
    validate_anchor,
)

UTC = timezone.utc
ANCHOR = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        content_type: str = "application/json",
        json_error: ValueError | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(
        self,
        *responses: FakeResponse | BaseException,
    ) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}
        self.mounts: dict[str, Any] = {}

    def mount(self, prefix: str, adapter: Any) -> None:
        self.mounts[prefix] = adapter

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected mocked HTTP request")
        result = self.responses.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def station_payload(
    *,
    station_id: str = "5cebf1e23d0f4a073c4bbfac",
    code: str = DEFAULT_STATION_CODE,
    name: str = "Bedford Institute",
    latitude: float = 44.682262,
    longitude: float = -63.613239,
    operating: bool = True,
    series: tuple[str, ...] = ("wlo", "wlp"),
) -> dict[str, Any]:
    return {
        "id": station_id,
        "code": code,
        "officialName": name,
        "alternativeName": "BIO",
        "latitude": latitude,
        "longitude": longitude,
        "operating": operating,
        "type": "PERMANENT",
        "timeSeries": [
            {"code": series_code, "id": f"{station_id}-{series_code}"}
            for series_code in series
        ],
    }


def station(
    *,
    station_id: str = "5cebf1e23d0f4a073c4bbfac",
    code: str = DEFAULT_STATION_CODE,
    name: str = "Bedford Institute",
    latitude: float = 44.682262,
    longitude: float = -63.613239,
    operating: bool = True,
    series: tuple[str, ...] = ("wlo", "wlp"),
) -> CHSStation:
    return CHSStation(
        id=station_id,
        code=code,
        official_name=name,
        latitude=latitude,
        longitude=longitude,
        operating=operating,
        time_series_codes=series,
        station_type="PERMANENT",
    )


def raw_point(
    timestamp: str,
    value: Any,
    *,
    qc: Any = "1",
    reviewed: Any = True,
    uncertainty: Any = None,
) -> dict[str, Any]:
    result = {
        "eventDate": timestamp,
        "value": value,
        "qcFlagCode": qc,
        "reviewed": reviewed,
        "timeSeriesId": "series-id",
    }
    if uncertainty is not None:
        result["uncertainty"] = uncertainty
    return result


def normalized_point(
    minute: int,
    value: float,
    qc: str | None = "1",
) -> WaterLevelPoint:
    return WaterLevelPoint(
        timestamp=ANCHOR + timedelta(minutes=minute),
        value_m=value,
        qc_code=qc,
    )


def normalized_series(
    code: str,
    points: tuple[WaterLevelPoint, ...],
) -> WaterLevelSeries:
    return WaterLevelSeries(
        code=code,
        label=code,
        points=points,
        start_time=ANCHOR - timedelta(hours=24),
        end_time=ANCHOR + timedelta(hours=24),
        raw_point_count=len(points),
    )


def bundle(
    *,
    observed: WaterLevelSeries | None,
    predicted: WaterLevelSeries | None,
) -> CHSWaterLevelBundle:
    return CHSWaterLevelBundle(
        station=station(),
        anchor_time=ANCHOR,
        fetched_at=ANCHOR,
        observed=observed,
        predicted=predicted,
    )


def test_fetch_catalog_uses_official_filter_and_keeps_only_operating_wlo() -> None:
    invalid = station_payload(station_id="bad")
    invalid["latitude"] = "NaN"
    no_wlo = station_payload(station_id="no-wlo", series=("wlp",))
    offline = station_payload(station_id="offline", operating=False)
    session = FakeSession(
        FakeResponse(
            [
                station_payload(),
                offline,
                invalid,
                no_wlo,
                "not an object",
            ]
        )
    )

    result = CHSClient(
        session=session,
        clock=lambda: ANCHOR,
    ).fetch_catalog()

    assert [item.code for item in result] == [DEFAULT_STATION_CODE]
    assert result.fetched_at == ANCHOR
    assert result.skipped_count == 3
    assert len(result.warnings) == 3
    assert result.raw_payload()[0]["officialName"] == "Bedford Institute"
    assert session.headers["User-Agent"] == USER_AGENT
    assert session.calls == [
        (
            f"{CHS_API_ROOT}/stations",
            {
                "params": {"time-series-code": "wlo"},
                "timeout": REQUEST_TIMEOUT,
                "allow_redirects": False,
            },
        )
    ]


def test_fetch_catalog_is_stably_sorted_and_deduplicated_by_id() -> None:
    same_id = "5cebf1e23d0f4a073c4bbfac"
    payload = [
        station_payload(station_id="z-id", name="Zulu", code="99999"),
        station_payload(station_id=same_id, name="Alpha", code="00002"),
        station_payload(station_id=same_id, name="Alpha", code="00001"),
    ]
    result = CHSClient(
        session=FakeSession(FakeResponse(payload)),
        clock=lambda: ANCHOR,
    ).fetch_catalog()

    assert [item.official_name for item in result] == ["Alpha", "Zulu"]
    assert result[0].code == "00001"
    assert result.skipped_count == 1
    assert "duplicate" in result.warnings[-1]


def test_fetch_catalog_raises_when_every_station_is_unusable() -> None:
    client = CHSClient(
        session=FakeSession(
            FakeResponse([station_payload(operating=False), {}])
        ),
        clock=lambda: ANCHOR,
    )
    with pytest.raises(CHSDataUnavailableError, match="any usable"):
        client.fetch_catalog()


@pytest.mark.parametrize(
    "api_root",
    [
        "",
        "http://api-sine.dfo-mpo.gc.ca/api/v1",
        "https://user:secret@api-sine.dfo-mpo.gc.ca/api/v1",
        "https://api-sine.dfo-mpo.gc.ca/api/v1?unsafe=yes",
        "not a URL",
    ],
)
def test_client_rejects_unsafe_api_roots(api_root: str) -> None:
    with pytest.raises(CHSConfigurationError, match="API root"):
        CHSClient(api_root=api_root, session=FakeSession())


@pytest.mark.parametrize(
    ("response", "error_type", "message"),
    [
        (
            FakeResponse({}, content_type="application/json"),
            CHSResponseError,
            "JSON list",
        ),
        (
            FakeResponse([], content_type="text/html"),
            CHSResponseError,
            "content type",
        ),
        (
            FakeResponse([], json_error=ValueError("secret decoder detail")),
            CHSResponseError,
            "valid JSON",
        ),
        (
            FakeResponse([], status_code=429),
            CHSRequestError,
            "limiting",
        ),
        (
            FakeResponse([], status_code=503),
            CHSRequestError,
            "temporarily unavailable",
        ),
        (
            FakeResponse([], status_code=302),
            CHSRequestError,
            "unexpected",
        ),
    ],
)
def test_catalog_validates_media_status_json_and_list(
    response: FakeResponse,
    error_type: type[Exception],
    message: str,
) -> None:
    client = CHSClient(session=FakeSession(response))
    with pytest.raises(error_type, match=message) as exc:
        client.fetch_catalog()
    assert "secret decoder detail" not in str(exc.value)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (requests.Timeout("details"), "timed out"),
        (requests.ConnectionError("details"), "Could not connect"),
        (requests.RequestException("details"), "could not be completed"),
    ],
)
def test_catalog_translates_network_errors(
    failure: requests.RequestException,
    message: str,
) -> None:
    client = CHSClient(session=FakeSession(failure))
    with pytest.raises(CHSRequestError, match=message) as exc:
        client.fetch_catalog()
    assert "details" not in str(exc.value)


def test_fetch_bundle_requests_rate_safe_windows_and_normalizes_points() -> None:
    observations = [
        raw_point("2026-07-23T11:45:00Z", 0.9, qc="2"),
        raw_point("2026-07-22T12:00:00-04:00", 0.4, qc="3"),
        raw_point("2026-07-23T11:45:00Z", 1.1, qc="1"),
        raw_point("not-a-time", 99),
        raw_point("2026-07-23T11:30:00Z", "NaN"),
    ]
    predictions = [
        raw_point(
            "2026-07-23T12:00:00Z",
            1.25,
            qc=None,
            uncertainty=0.05,
        )
    ]
    session = FakeSession(
        FakeResponse(observations),
        FakeResponse(predictions, content_type="application/vnd.chs+json"),
    )

    result = CHSClient(
        session=session,
        clock=lambda: ANCHOR,
    ).fetch_bundle(station(), ANCHOR)

    assert result.anchor_time == ANCHOR
    assert result.point_count == 3
    assert result.observed is not None
    assert [point.value_m for point in result.observed.points] == [0.4, 1.1]
    assert result.observed.points[-1].qc_label == "Good"
    assert result.observed.skipped_count == 3
    assert result.predicted is not None
    assert result.predicted.points[0].qc_label == "Unknown"
    assert result.predicted.points[0].uncertainty_m == 0.05
    assert len(result.raw_documents) == 2

    endpoint = f"{CHS_API_ROOT}/stations/{station().id}/data"
    assert session.calls == [
        (
            endpoint,
            {
                "params": {
                    "time-series-code": OBSERVATION_SERIES_CODE,
                    "from": "2026-07-22T12:00:00Z",
                    "to": "2026-07-23T12:00:00Z",
                    "resolution": WATER_LEVEL_RESOLUTION,
                },
                "timeout": REQUEST_TIMEOUT,
                "allow_redirects": False,
            },
        ),
        (
            endpoint,
            {
                "params": {
                    "time-series-code": PREDICTION_SERIES_CODE,
                    "from": "2026-07-22T12:00:00Z",
                    "to": "2026-07-24T12:00:00Z",
                    "resolution": WATER_LEVEL_RESOLUTION,
                },
                "timeout": REQUEST_TIMEOUT,
                "allow_redirects": False,
            },
        ),
    ]


def test_fetch_bundle_keeps_prediction_when_observation_request_fails() -> None:
    session = FakeSession(
        FakeResponse([], status_code=503),
        FakeResponse([raw_point("2026-07-23T12:00:00Z", 1.25)]),
    )

    result = CHSClient(
        session=session,
        clock=lambda: ANCHOR,
    ).fetch_bundle(station(), ANCHOR)

    assert result.observed is None
    assert result.predicted is not None
    assert result.predicted.points
    assert any(
        "Observed water level data was unavailable" in item
        for item in result.warnings
    )


def test_fetch_bundle_skips_prediction_request_when_not_advertised() -> None:
    session = FakeSession(
        FakeResponse([raw_point("2026-07-23T12:00:00Z", 1.0)])
    )
    result = CHSClient(
        session=session,
        clock=lambda: ANCHOR,
    ).fetch_bundle(station(series=("wlo",)), ANCHOR)

    assert result.observed is not None
    assert result.predicted is None
    assert len(session.calls) == 1
    assert any("does not advertise" in item for item in result.warnings)


def test_fetch_bundle_raises_when_all_requested_series_are_empty() -> None:
    client = CHSClient(
        session=FakeSession(FakeResponse([]), FakeResponse([])),
        clock=lambda: ANCHOR,
    )
    with pytest.raises(
        CHSDataUnavailableError,
        match="no usable water-level data",
    ) as exc:
        client.fetch_bundle(station(), ANCHOR)
    assert len(exc.value.warnings) == 2


def test_fetch_bundle_requires_operating_observation_station() -> None:
    with pytest.raises(CHSConfigurationError, match="operating CHS"):
        CHSClient(session=FakeSession()).fetch_bundle(
            station(operating=False),
            ANCHOR,
        )
    with pytest.raises(CHSConfigurationError, match="operating CHS"):
        CHSClient(session=FakeSession()).fetch_bundle(
            station(series=("wlp",)),
            ANCHOR,
        )


@pytest.mark.parametrize(
    "anchor",
    [
        datetime(2026, 7, 23, 12, 0),
        datetime(2026, 7, 23, 12, 1, tzinfo=UTC),
        datetime(2026, 7, 23, 12, 15, 1, tzinfo=UTC),
    ],
)
def test_validate_anchor_requires_aware_exact_quarter_hour(
    anchor: datetime,
) -> None:
    with pytest.raises(CHSConfigurationError):
        validate_anchor(anchor)


def test_anchor_is_normalized_to_utc_and_injected_clock_is_floored() -> None:
    eastern = timezone(timedelta(hours=-4))
    assert validate_anchor(
        datetime(2026, 7, 23, 8, 0, tzinfo=eastern)
    ) == ANCHOR
    assert floor_to_anchor(
        datetime(2026, 7, 23, 12, 14, 59, tzinfo=UTC)
    ) == ANCHOR

    client = CHSClient(
        session=FakeSession(
            FakeResponse([raw_point("2026-07-23T12:00:00Z", 1.0)]),
        ),
        clock=lambda: datetime(2026, 7, 23, 12, 14, 59, tzinfo=UTC),
    )
    result = client.fetch_bundle(station(series=("wlo",)))
    assert result.anchor_time == ANCHOR


def test_station_selection_prefers_inside_with_predictions_then_center() -> None:
    no_prediction = station(
        station_id="inside-close",
        code="00001",
        name="Inside close",
        series=("wlo",),
    )
    with_prediction = station(
        station_id="inside-farther",
        code="00002",
        name="Inside farther",
    )
    outside = station(
        station_id="outside",
        code="00003",
        name="Outside",
    )
    metrics = {
        item.station_id: item
        for item in (
            StationProximity("inside-close", True, 0.0, 1.0),
            StationProximity("inside-farther", True, 0.0, 5.0),
            StationProximity("outside", False, 0.1, 0.1),
        )
    }

    match = select_station(
        (no_prediction, with_prediction, outside),
        metrics,
    )

    assert match.station.id == "inside-farther"
    assert match.inside_roi is True


def test_station_selection_uses_roi_distance_before_prediction_outside() -> None:
    nearest = station(
        station_id="nearest",
        code="00001",
        name="Nearest",
        series=("wlo",),
    )
    farther = station(
        station_id="farther",
        code="00002",
        name="Farther",
    )
    match = select_station(
        (nearest, farther),
        (
            StationProximity("nearest", False, 1.0, 8.0),
            StationProximity("farther", False, 2.0, 2.0),
        ),
    )
    assert match.station.id == "nearest"


def test_station_selection_rejects_missing_or_invalid_metrics() -> None:
    with pytest.raises(CHSDataUnavailableError, match="No operating"):
        select_station((station(),), ())
    with pytest.raises(CHSConfigurationError, match="StationProximity"):
        select_station(
            (station(),),
            {"id": (True, 0.0, 0.0)},  # type: ignore[dict-item]
        )
    with pytest.raises(CHSConfigurationError, match="invalid value"):
        select_station(
            (station(),),
            (StationProximity(station().id, False, float("nan"), 1.0),),
        )


def test_latest_nearest_and_statistics_helpers() -> None:
    first = normalized_point(-15, 1.0)
    second = normalized_point(15, 3.0)
    series = normalized_series("wlo", (first, second))

    assert latest_point(series) == second
    assert nearest_point(series, ANCHOR) == first
    assert nearest_point(
        series,
        ANCHOR + timedelta(hours=2),
        tolerance=timedelta(minutes=10),
    ) is None
    assert series_statistics(series) == {
        "count": 2,
        "minimum_m": 1.0,
        "maximum_m": 3.0,
        "mean_m": 2.0,
        "latest_m": 3.0,
        "latest_time_utc": second.timestamp,
    }
    assert series_statistics(None)["count"] == 0


def test_chart_frame_is_stable_wide_utc_frame() -> None:
    observations = normalized_series(
        "wlo",
        (
            normalized_point(0, 1.0, "1"),
            normalized_point(15, 2.0, "3"),
        ),
    )
    predictions = normalized_series(
        "wlp",
        (
            normalized_point(15, 1.8, None),
            normalized_point(30, 1.5, "2"),
        ),
    )

    frame = chart_frame(
        bundle(observed=observations, predicted=predictions)
    )

    assert tuple(frame.columns) == CHART_COLUMNS
    assert len(frame) == 3
    assert str(frame[CHART_TIME_COLUMN].dtype) == "datetime64[ns, UTC]"
    assert str(frame[CHART_OBSERVED_COLUMN].dtype) == "Float64"
    middle = frame.iloc[1]
    assert middle[CHART_OBSERVED_COLUMN] == 2.0
    assert middle[CHART_PREDICTED_COLUMN] == 1.8
    assert middle[CHART_OBSERVED_QC_COLUMN] == "Questionable/suspect"

    empty = chart_frame(bundle(observed=None, predicted=None))
    assert tuple(empty.columns) == CHART_COLUMNS
    assert empty.empty
    assert all(
        isinstance(dtype, pd.api.extensions.ExtensionDtype)
        or str(dtype).startswith("datetime64")
        for dtype in empty.dtypes
    )


def test_raw_bundle_contains_station_queries_and_unmodified_responses() -> None:
    source = [raw_point("2026-07-23T12:00:00Z", 1.234)]
    result = CHSClient(
        session=FakeSession(FakeResponse(source)),
        clock=lambda: ANCHOR,
    ).fetch_bundle(station(series=("wlo",)), ANCHOR)

    decoded = json.loads(raw_bundle_bytes(result).decode("utf-8"))

    assert decoded["station"]["code"] == DEFAULT_STATION_CODE
    assert decoded["query_metadata"] == {
        "anchor_time": "2026-07-23T12:00:00Z",
        "resolution": "FIFTEEN_MINUTES",
        "observation_lookback_hours": 24,
        "prediction_lookback_hours": 24,
        "prediction_lookahead_hours": 24,
    }
    assert decoded["responses"][0]["series_code"] == "wlo"
    assert decoded["responses"][0]["query"]["from"] == (
        "2026-07-22T12:00:00Z"
    )
    assert decoded["responses"][0]["payload"] == source
    assert b"NaN" not in raw_bundle_bytes(result)


def test_non_json_nan_in_response_is_rejected_before_raw_storage() -> None:
    client = CHSClient(
        session=FakeSession(FakeResponse([{"value": float("nan")}])),
        clock=lambda: ANCHOR,
    )
    with pytest.raises(CHSDataUnavailableError) as exc:
        client.fetch_bundle(station(series=("wlo",)), ANCHOR)
    assert any("invalid JSON values" in item for item in exc.value.warnings)
