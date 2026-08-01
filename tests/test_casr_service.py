"""Unit tests for CaSR-Rivers fetch orchestration."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import xarray as xr

from coastal_flood_explorer.casr_common import CASRRiversFile
from coastal_flood_explorer.casr_service import (
    fetch_for_roi,
    files_for_variable,
    prioritize_probe_files,
)


def _roi() -> dict:
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-64.0, 44.4],
                    [-63.0, 44.4],
                    [-63.0, 44.9],
                    [-64.0, 44.9],
                    [-64.0, 44.4],
                ]
            ],
        },
    }


def _file(subbasin: str, variable: str) -> CASRRiversFile:
    name = (
        f"198001_{subbasin}_MSC_CaSR-Rivers-Analysis_{variable}_"
        "Sfc_LatLon0.00833_PT0H.nc"
    )
    return CASRRiversFile(
        year_month="198001",
        subbasin_id=subbasin,
        variable=variable,
        filename=name,
        url=f"https://hpfx.example.test/{name}",
    )


def _bytes_for(variable_nc: str = "disc") -> bytes:
    times = pd.date_range("1980-01-02", periods=2, freq="D")
    lats = np.array([44.5, 44.6], dtype=np.float32)
    lons = np.array([296.3, 296.5], dtype=np.float32)
    data = np.ones((2, 2, 2), dtype=np.float32)
    ds = xr.Dataset(
        {variable_nc: (("time", "lat", "lon"), data)},
        coords={"time": times, "lat": lats, "lon": lons},
    )
    path = "/tmp/casr_service_grid.nc"
    ds.to_netcdf(path)
    return open(path, "rb").read()


def test_prioritize_prefers_atlantic_prefixes() -> None:
    files = (
        _file("08AA000", "DeepReservoirStorage"),
        _file("01AA000", "DeepReservoirStorage"),
        _file("05AA000", "DeepReservoirStorage"),
    )
    ranked = prioritize_probe_files(
        files,
        roi_bbox_wgs84=(-64.0, 44.0, -63.0, 45.0),
        limit=3,
    )
    assert ranked[0].subbasin_id.startswith("01")


def test_fetch_for_roi_with_explicit_subbasin() -> None:
    payload = _bytes_for("disc")
    files = (
        _file("01TEST", "RiverDischarge"),
        _file("01TEST", "DeepReservoirStorage"),
    )

    def list_files(month: str, variable: str | None):
        del month
        return files_for_variable(files, variable or "RiverDischarge") if variable else files

    def download(file: CASRRiversFile) -> bytes:
        del file
        return payload

    result = fetch_for_roi(
        year_month="198001",
        variable="RiverDischarge",
        roi=_roi(),
        list_files=list_files,
        download=download,
        subbasin_id="01TEST",
        valid_time=datetime(1980, 1, 2, tzinfo=timezone.utc),
    )
    assert result.variable == "RiverDischarge"
    assert len(result.subsets) == 1
    assert result.subsets[0].subbasin_id == "01TEST"
