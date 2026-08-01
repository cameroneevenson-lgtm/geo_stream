"""Unit tests for native HPFX CaSR v3.2 tiled access."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from coastal_flood_explorer.casr_common import CASRConfigurationError
from coastal_flood_explorer.casr_v32 import (
    PRECIP_24H,
    V32_NETCDF_NAMES,
    V32_VARIABLE_LABELS,
    fetch_latest_for_roi,
    geo_to_rotated,
    normalize_v32_variable,
    period_for_day,
    tile_url,
    tiles_for_bbox,
)


# Compact Halifax-harbour ROI that maps to a single HPFX tile.
ROI = {
    "type": "Feature",
    "properties": {},
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [-63.7, 44.5],
                [-63.5, 44.5],
                [-63.5, 44.7],
                [-63.7, 44.7],
                [-63.7, 44.5],
            ]
        ],
    },
}


def _tile_bytes() -> bytes:
    # Build a tiny rotated-pole tile covering the ROI in geographic space.
    rlat = np.linspace(-9.5, -8.5, 5)
    rlon = np.linspace(19.5, 21.0, 5)
    lon2d = np.zeros((5, 5), dtype=float)
    lat2d = np.zeros((5, 5), dtype=float)
    for i in range(5):
        for j in range(5):
            lon2d[i, j] = -63.75 + j * 0.05
            lat2d[i, j] = 44.45 + i * 0.05
    times = pd.date_range("2024-12-01T12:00:00", periods=31, freq="D")
    values = np.linspace(0.0, 0.02, 31 * 5 * 5, dtype=float).reshape(31, 5, 5)
    name = V32_NETCDF_NAMES[PRECIP_24H]
    dataset = xr.Dataset(
        {name: (("time", "rlat", "rlon"), values)},
        coords={
            "time": times,
            "rlat": rlat,
            "rlon": rlon,
            "lat": (("rlat", "rlon"), lat2d),
            "lon": (("rlat", "rlon"), lon2d),
        },
    )
    dataset[name].attrs["units"] = "m"
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


def test_normalize_and_labels() -> None:
    assert normalize_v32_variable("precip_24h") == PRECIP_24H
    assert V32_VARIABLE_LABELS[PRECIP_24H] == "Precipitation"
    assert all(len(label) <= 20 for label in V32_VARIABLE_LABELS.values())


def test_period_for_day() -> None:
    assert period_for_day(date(2024, 12, 31)) == "2024-2024"
    assert period_for_day(date(2020, 6, 1)) == "2020-2023"


def test_geo_to_rotated_halifax_maps_near_expected_tile() -> None:
    rlon, rlat = geo_to_rotated(-63.6, 44.65)
    tiles = tiles_for_bbox((-64.0, 44.0, -63.0, 45.0))
    assert any(tile.startswith("rlon596-630_") for tile in tiles)
    assert rlon == pytest.approx(20.32, abs=0.1)


def test_tile_url_shape() -> None:
    url = tile_url(
        V32_NETCDF_NAMES[PRECIP_24H],
        "rlon596-630_rlat386-420",
        "2024-2024",
    )
    assert url.endswith(
        "CaSR_v3.2_A_PR24_SFC_rlon596-630_rlat386-420_2024-2024.nc"
    )


def test_fetch_latest_for_roi_uses_injected_tiles() -> None:
    payload = _tile_bytes()
    calls: list[str] = []

    def fake_download(url: str) -> bytes:
        calls.append(url)
        return payload

    subset = fetch_latest_for_roi(
        roi=ROI,
        variable=PRECIP_24H,
        day=date(2024, 12, 31),
        series_days=10,
        download=fake_download,
    )
    assert calls
    assert subset.variable == PRECIP_24H
    assert subset.units == "mm"
    assert subset.selected_time == datetime(
        2024, 12, 31, 12, tzinfo=timezone.utc
    )
    assert subset.overlay_png.startswith(b"\x89PNG")
    assert any("not coastal water levels" in w.lower() for w in subset.warnings)


def test_fetch_rejects_unsupported_variable() -> None:
    with pytest.raises(CASRConfigurationError):
        fetch_latest_for_roi(
            roi=ROI,
            variable="temperature",
            day=date(2024, 12, 31),
            download=lambda _url: _tile_bytes(),
        )
