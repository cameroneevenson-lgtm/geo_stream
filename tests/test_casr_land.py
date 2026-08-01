"""Unit tests for CaSR-Land HPFX fetch and ROI processing."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from coastal_flood_explorer.casr_common import (
    CASRConfigurationError,
    CASRDataUnavailableError,
)
from coastal_flood_explorer.casr_land import (
    CASR_LAND_DEFAULT,
    LAND_AIR_TEMP,
    LAND_NETCDF_NAMES,
    download_land_day,
    fetch_land_for_roi,
    land_day_url,
    normalize_land_variable,
    process_land_bytes,
)


ROI = {
    "type": "Feature",
    "properties": {},
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [-65.0, 44.0],
                [-63.0, 44.0],
                [-63.0, 46.0],
                [-65.0, 46.0],
                [-65.0, 44.0],
            ]
        ],
    },
}


def _land_netcdf_bytes() -> bytes:
    rlat = np.linspace(44.0, 46.0, 5)
    rlon = np.linspace(-65.0, -63.0, 5)
    lon2d, lat2d = np.meshgrid(rlon, rlat)
    # Naive UTC timestamps — NetCDF4 encoding rejects tz-aware datetime64.
    times = pd.date_range("2017-12-31T12:00:00", periods=3, freq="h")
    values = np.linspace(0.0, 10.0, 3 * 5 * 5, dtype=float).reshape(3, 5, 5)
    dataset = xr.Dataset(
        {
            LAND_NETCDF_NAMES[LAND_AIR_TEMP]: (
                ("time", "rlat", "rlon"),
                values,
            ),
        },
        coords={
            "time": times,
            "rlat": rlat,
            "rlon": rlon,
            "lat": (("rlat", "rlon"), lat2d),
            "lon": (("rlat", "rlon"), lon2d),
        },
    )
    dataset[LAND_NETCDF_NAMES[LAND_AIR_TEMP]].attrs["units"] = "deg_C"
    handle = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
    try:
        handle.close()
        dataset.to_netcdf(handle.name, engine="netcdf4")
        with open(handle.name, "rb") as stream:
            return stream.read()
    finally:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover
            pass


def test_land_day_url_uses_dated_path() -> None:
    url = land_day_url(date(2017, 12, 31))
    assert url.endswith("/CaSR-Land_v2.1/netcdf/2017123112.nc")
    assert url.startswith("https://")


def test_land_day_url_rejects_out_of_window() -> None:
    with pytest.raises(CASRConfigurationError):
        land_day_url(date(2018, 1, 1))


def test_normalize_land_variable_accepts_short_and_full_names() -> None:
    assert normalize_land_variable("tj_1.5m") == LAND_AIR_TEMP
    assert (
        normalize_land_variable(LAND_NETCDF_NAMES[LAND_AIR_TEMP])
        == LAND_AIR_TEMP
    )
    assert normalize_land_variable("not-a-var") is None


def test_process_land_bytes_masks_roi_and_builds_series() -> None:
    subset = process_land_bytes(
        _land_netcdf_bytes(),
        roi=ROI,
        variable=LAND_AIR_TEMP,
        day_label="20171231",
    )
    assert subset.variable == LAND_AIR_TEMP
    assert subset.units == "deg_C"
    assert subset.overlay_png.startswith(b"\x89PNG")
    assert len(subset.point_series) == 3
    assert subset.point[0] == pytest.approx(-64.0, abs=1.0)
    assert subset.selected_time in subset.valid_times
    assert subset.selected_time.tzinfo is timezone.utc


def test_fetch_land_for_roi_uses_injected_download() -> None:
    payload = _land_netcdf_bytes()
    calls: list[date] = []

    def fake_download(day: date) -> bytes:
        calls.append(day)
        return payload

    subset = fetch_land_for_roi(
        day=CASR_LAND_DEFAULT,
        variable=LAND_AIR_TEMP,
        roi=ROI,
        download=fake_download,
    )
    assert calls == [CASR_LAND_DEFAULT]
    assert subset.subbasin_id == "20171231"


def test_download_land_day_rejects_non_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coastal_flood_explorer.casr_land.land_day_url",
        lambda day, root="https://example.test": "http://example.test/x.nc",
    )
    with pytest.raises(CASRConfigurationError):
        download_land_day(CASR_LAND_DEFAULT)


def test_process_land_bytes_raises_when_roi_misses_grid() -> None:
    far_roi = {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [10.0, 10.0],
                    [11.0, 10.0],
                    [11.0, 11.0],
                    [10.0, 11.0],
                    [10.0, 10.0],
                ]
            ],
        },
    }
    with pytest.raises(CASRDataUnavailableError):
        process_land_bytes(
            _land_netcdf_bytes(),
            roi=far_roi,
            variable=LAND_AIR_TEMP,
        )
