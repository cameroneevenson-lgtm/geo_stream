"""Unit tests for CaSR v3.2 PAVICS access (offline, injectable open)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from coastal_flood_explorer.casr_common import (
    CASRConfigurationError,
    CASRDataUnavailableError,
)
from coastal_flood_explorer.casr_pavics import (
    PAVICS_TAS,
    fetch_latest_for_roi,
    latest_available_day,
    normalize_pavics_variable,
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


def _pavics_dataset(*, n_times: int = 40) -> xr.Dataset:
    rlat = np.linspace(44.0, 46.0, 5)
    rlon = np.linspace(-65.0, -63.0, 5)
    lon2d, lat2d = np.meshgrid(rlon, rlat)
    times = pd.date_range("2024-11-22", periods=n_times, freq="D")
    values = np.linspace(250.0, 280.0, n_times * 5 * 5, dtype=float).reshape(
        n_times, 5, 5
    )
    dataset = xr.Dataset(
        {
            PAVICS_TAS: (("time", "rlat", "rlon"), values),
        },
        coords={
            "time": times,
            "rlat": rlat,
            "rlon": rlon,
            "lat": (("rlat", "rlon"), lat2d),
            "lon": (("rlat", "rlon"), lon2d),
        },
    )
    dataset[PAVICS_TAS].attrs["units"] = "K"
    return dataset


def test_normalize_pavics_variable() -> None:
    assert normalize_pavics_variable("tas") == PAVICS_TAS
    assert normalize_pavics_variable("TAS") == PAVICS_TAS
    assert normalize_pavics_variable("nope") is None


def test_latest_available_day_uses_last_time() -> None:
    dataset = _pavics_dataset()

    def open_dataset(_url: str) -> xr.Dataset:
        return dataset

    assert latest_available_day(open_dataset=open_dataset) == date(2024, 12, 31)


def test_fetch_latest_for_roi_masks_and_converts_kelvin() -> None:
    dataset = _pavics_dataset()

    def open_dataset(_url: str) -> xr.Dataset:
        return dataset.copy(deep=True)

    subset = fetch_latest_for_roi(
        roi=ROI,
        variable=PAVICS_TAS,
        series_days=10,
        open_dataset=open_dataset,
    )
    assert subset.variable == PAVICS_TAS
    assert subset.units == "deg_C"
    assert subset.selected_time == datetime(2024, 12, 31, tzinfo=timezone.utc)
    assert subset.subbasin_id == "20241231"
    assert subset.overlay_png.startswith(b"\x89PNG")
    assert len(subset.point_series) == 10
    # Kelvin ~250-280 -> Celsius roughly -23 to 7.
    assert float(subset.point_series["value"].iloc[-1]) < 50.0
    assert any("latest published PAVICS day" in w for w in subset.warnings)


def test_fetch_latest_rejects_unsupported_variable() -> None:
    with pytest.raises(CASRConfigurationError):
        fetch_latest_for_roi(
            roi=ROI,
            variable="not-a-var",
            open_dataset=lambda _url: _pavics_dataset(),
        )


def test_fetch_latest_raises_when_roi_misses_grid() -> None:
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
        fetch_latest_for_roi(
            roi=far_roi,
            variable=PAVICS_TAS,
            open_dataset=lambda _url: _pavics_dataset(),
        )
