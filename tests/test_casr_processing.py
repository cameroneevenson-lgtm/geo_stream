"""Unit tests for CaSR-Rivers NetCDF processing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from coastal_flood_explorer.casr_processing import (
    grid_bbox_from_bytes,
    process_rivers_bytes,
    subset_intersects_roi,
)


def _roi(west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        },
    }


def _netcdf_bytes(
    *,
    variable: str = "disc",
    lon0_360: float = 296.4,
) -> bytes:
    times = pd.date_range("1980-01-02", periods=3, freq="D")
    lats = np.array([44.5, 44.6, 44.7], dtype=np.float32)
    lons = np.array(
        [lon0_360 - 0.1, lon0_360, lon0_360 + 0.1],
        dtype=np.float32,
    )
    data = np.arange(3 * 3 * 3, dtype=np.float32).reshape(3, 3, 3)
    ds = xr.Dataset(
        {variable: (("time", "lat", "lon"), data)},
        coords={"time": times, "lat": lats, "lon": lons},
    )
    ds[variable].attrs["units"] = "m**3/s"
    path = Path("/tmp/casr_test_grid.nc")
    ds.to_netcdf(path)
    return path.read_bytes()


def test_grid_bbox_converts_lon_to_wgs84() -> None:
    bbox = grid_bbox_from_bytes(_netcdf_bytes(lon0_360=296.4))
    assert bbox[0] == pytest.approx(296.4 - 0.1 - 360.0)
    assert bbox[2] == pytest.approx(296.4 + 0.1 - 360.0)
    assert bbox[1] == pytest.approx(44.5)
    assert bbox[3] == pytest.approx(44.7)


def test_subset_intersects_and_processes() -> None:
    data = _netcdf_bytes()
    roi = _roi(-64.0, 44.4, -63.0, 44.9)
    assert subset_intersects_roi(data, roi)
    subset = process_rivers_bytes(
        data,
        roi=roi,
        variable="RiverDischarge",
        subbasin_id="01TEST",
        valid_time=datetime(1980, 1, 3, tzinfo=timezone.utc),
    )
    assert subset.subbasin_id == "01TEST"
    assert subset.variable == "RiverDischarge"
    assert subset.overlay_png.startswith(b"\x89PNG")
    assert len(subset.point_series) == 3
    assert subset.selected_time == datetime(1980, 1, 3, tzinfo=timezone.utc)
