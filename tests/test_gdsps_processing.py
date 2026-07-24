"""Unit tests for GDSPS Xarray processing and ROI masking (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from coastal_flood_explorer.gdsps_common import (
    ETAS,
    SSH,
    GDSPSConfigurationError,
    GDSPSDataUnavailableError,
)
from coastal_flood_explorer.gdsps_processing import (
    GDSPSSubset,
    process_dataset,
    subset_netcdf_bytes,
)


def build_dataset() -> xr.Dataset:
    lon = np.linspace(-65.0, -60.0, 11)
    lat = np.linspace(43.0, 46.0, 7)
    times = pd.date_range("2026-07-22T00:00", periods=3, freq="1h")
    etas = np.zeros((times.size, lat.size, lon.size), dtype=float)
    ssh = np.ones((times.size, lat.size, lon.size), dtype=float) * 2.0
    # Give each grid cell a value equal to its lon so we can check masking.
    for ti in range(times.size):
        etas[ti, :, :] = lon[np.newaxis, :]
    dataset = xr.Dataset(
        {
            "ETAS": (("time", "lat", "lon"), etas, {"units": "m"}),
            "SSH": (("time", "lat", "lon"), ssh, {"units": "m"}),
        },
        coords={"time": times, "lat": lat, "lon": lon},
    )
    return dataset


def square_roi(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [min_lon, min_lat],
                [max_lon, min_lat],
                [max_lon, max_lat],
                [min_lon, max_lat],
                [min_lon, min_lat],
            ]
        ],
    }


def test_process_dataset_masks_and_selects_variable() -> None:
    dataset = build_dataset()
    roi = square_roi(-63.0, 44.0, -62.0, 45.0)
    subset = process_dataset(dataset, roi=roi, variable="ETAS")

    assert isinstance(subset, GDSPSSubset)
    assert subset.variable == ETAS
    assert subset.variable_name == "ETAS"
    assert subset.units == "m"
    # Values outside the ROI are masked to NaN; inside cells retain their lon.
    values = subset.dataset["ETAS"].values
    finite = values[np.isfinite(values)]
    assert finite.size > 0
    assert finite.min() >= -63.0 - 1e-9
    assert finite.max() <= -62.0 + 1e-9


def test_etas_and_ssh_are_not_conflated() -> None:
    dataset = build_dataset()
    roi = square_roi(-63.0, 44.0, -62.0, 45.0)
    etas = process_dataset(dataset, roi=roi, variable="ETAS")
    ssh = process_dataset(dataset, roi=roi, variable="SSH")
    assert etas.variable_name == "ETAS"
    assert ssh.variable_name == "SSH"
    ssh_finite = ssh.dataset["SSH"].values
    ssh_finite = ssh_finite[np.isfinite(ssh_finite)]
    # SSH is a constant 2.0 field; it must never pick up the ETAS lon values.
    assert np.allclose(ssh_finite, 2.0)


def test_point_series_has_one_row_per_time() -> None:
    dataset = build_dataset()
    roi = square_roi(-63.0, 44.0, -62.0, 45.0)
    subset = process_dataset(dataset, roi=roi, variable="ETAS")
    assert list(subset.point_series.columns) == ["time_utc", "ETAS_value"]
    assert len(subset.point_series) == 3


def test_valid_time_selection_reduces_frames() -> None:
    dataset = build_dataset()
    roi = square_roi(-63.0, 44.0, -62.0, 45.0)
    subset = process_dataset(
        dataset,
        roi=roi,
        variable="ETAS",
        valid_times=(datetime(2026, 7, 22, 1, tzinfo=timezone.utc),),
    )
    assert subset.dataset.sizes["time"] == 1
    assert len(subset.point_series) == 1


def test_missing_variable_raises_unavailable() -> None:
    dataset = build_dataset().drop_vars("SSH")
    roi = square_roi(-63.0, 44.0, -62.0, 45.0)
    with pytest.raises(GDSPSDataUnavailableError):
        process_dataset(dataset, roi=roi, variable="SSH")


def test_bad_variable_raises_configuration_error() -> None:
    dataset = build_dataset()
    roi = square_roi(-63.0, 44.0, -62.0, 45.0)
    with pytest.raises(GDSPSConfigurationError):
        process_dataset(dataset, roi=roi, variable="bogus")


def test_roi_outside_grid_raises_unavailable() -> None:
    dataset = build_dataset()
    roi = square_roi(10.0, 10.0, 11.0, 11.0)
    with pytest.raises(GDSPSDataUnavailableError):
        process_dataset(dataset, roi=roi, variable="ETAS")


def test_subset_netcdf_bytes_round_trip(tmp_path) -> None:
    dataset = build_dataset()
    path = tmp_path / "gdsps.nc"
    dataset.to_netcdf(path)
    data = path.read_bytes()
    roi = square_roi(-63.0, 44.0, -62.0, 45.0)
    subset = subset_netcdf_bytes(data, roi=roi, variable="ETAS")
    assert subset.variable == ETAS
    assert subset.dataset["ETAS"].values.size > 0
