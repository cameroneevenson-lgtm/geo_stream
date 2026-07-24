"""Unit tests for the pure GDSPS selection + fetch orchestration."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from coastal_flood_explorer import gdsps_service
from coastal_flood_explorer.gdsps_common import (
    ETAS,
    SSH,
    GDSPSCoverageInfo,
    GDSPSDatamartFile,
    GDSPSDataUnavailableError,
    GDSPSLayerInfo,
    GDSPSRun,
)

RUN_00 = GDSPSRun(datetime(2026, 7, 22, 0, tzinfo=timezone.utc), "00")
RUN_12 = GDSPSRun(datetime(2026, 7, 22, 12, tzinfo=timezone.utc), "12")


def _file(variable: str, run: GDSPSRun, lead: int) -> GDSPSDatamartFile:
    valid = run.issue_time.replace() + pd.Timedelta(hours=lead).to_pytimedelta()
    return GDSPSDatamartFile(
        filename=f"{run.stamp}_MSC_GDSPS_{variable}_LatLon_PT{lead:03d}H.nc",
        url=f"https://dd.weather.gc.ca/model_gdsps/{run.cycle}/{variable}_{lead}.nc",
        variable=variable,
        run=run,
        lead_hours=lead,
        valid_time=valid,
    )


def test_variable_options_only_discovered() -> None:
    layers = (GDSPSLayerInfo("GDSPS.ETAS", "t", ETAS),)
    files = (_file(SSH, RUN_00, 3),)
    assert gdsps_service.variable_options(layers, files) == (ETAS, SSH)
    assert gdsps_service.variable_options((), ()) == ()


def test_layer_for_variable() -> None:
    layers = (
        GDSPSLayerInfo("GDSPS.ETAS", "t", ETAS),
        GDSPSLayerInfo("GDSPS.SSH", "t", SSH),
    )
    assert gdsps_service.layer_for_variable(layers, SSH).name == "GDSPS.SSH"
    assert gdsps_service.layer_for_variable(layers, "ZZZ") is None


def test_runs_from_files_newest_first() -> None:
    files = (_file(ETAS, RUN_00, 3), _file(ETAS, RUN_12, 3), _file(SSH, RUN_12, 3))
    runs = gdsps_service.runs_from_files(files, ETAS)
    assert [run.stamp for run in runs] == ["20260722T12Z", "20260722T00Z"]


def test_valid_times_prefers_layer_dimension() -> None:
    layer = GDSPSLayerInfo(
        "GDSPS.ETAS",
        "t",
        ETAS,
        available_times=(datetime(2026, 7, 22, 1, tzinfo=timezone.utc),),
    )
    files = (_file(ETAS, RUN_00, 6),)
    # Layer times win when present.
    assert gdsps_service.valid_times(layer, files, ETAS, None) == (
        datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
    )
    # Fall back to Datamart file valid times when no layer times.
    times = gdsps_service.valid_times(None, files, ETAS, RUN_00)
    assert times == (datetime(2026, 7, 22, 6, tzinfo=timezone.utc),)


def test_select_datamart_file_nearest_time() -> None:
    files = (_file(ETAS, RUN_00, 3), _file(ETAS, RUN_00, 9))
    chosen = gdsps_service.select_datamart_file(
        files, ETAS, RUN_00, datetime(2026, 7, 22, 8, tzinfo=timezone.utc)
    )
    assert chosen.lead_hours == 9
    with pytest.raises(GDSPSDataUnavailableError):
        gdsps_service.select_datamart_file((), ETAS, RUN_00, None)


def _dataset_bytes(variable: str, tmp_path) -> bytes:
    lon = np.linspace(-64.0, -62.0, 5)
    lat = np.linspace(44.0, 45.0, 4)
    times = pd.date_range("2026-07-22T00:00", periods=2, freq="1h")
    data = np.ones((times.size, lat.size, lon.size))
    dataset = xr.Dataset(
        {variable: (("time", "lat", "lon"), data, {"units": "m"})},
        coords={"time": times, "lat": lat, "lon": lon},
    )
    path = tmp_path / f"{variable}.nc"
    dataset.to_netcdf(path)
    return path.read_bytes()


ROI = {
    "type": "Polygon",
    "coordinates": [
        [[-63.5, 44.2], [-62.5, 44.2], [-62.5, 44.8], [-63.5, 44.8], [-63.5, 44.2]]
    ],
}


def test_fetch_numeric_prefers_wcs(tmp_path) -> None:
    calls: list[str] = []

    def wcs_coverages():
        return (GDSPSCoverageInfo("GDSPS.ETAS", "t", ETAS),), None

    def wcs_bytes(coverage_id, bbox, time):
        calls.append("wcs")
        return _dataset_bytes(ETAS, tmp_path)

    def datamart_files():
        calls.append("datamart_files")
        return (), None

    def datamart_bytes(url):
        raise AssertionError("Datamart must not be used when WCS succeeds")

    subset, service = gdsps_service.fetch_numeric(
        ETAS,
        (-63.5, 44.2, -62.5, 44.8),
        ROI,
        datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
        RUN_00,
        wcs_coverages=wcs_coverages,
        wcs_bytes=wcs_bytes,
        datamart_files=datamart_files,
        datamart_bytes=datamart_bytes,
    )
    assert service == "GeoMet WCS"
    assert subset.variable == ETAS
    assert calls == ["wcs"]


def test_fetch_numeric_falls_back_to_datamart(tmp_path) -> None:
    def wcs_coverages():
        # No ETAS coverage advertised -> find_coverage_for_variable raises
        # GDSPSDataUnavailableError, triggering the documented fallback.
        return (GDSPSCoverageInfo("GDSPS.SSH", "t", SSH),), None

    def wcs_bytes(coverage_id, bbox, time):
        raise AssertionError("WCS bytes must not be fetched without a coverage")

    def datamart_files():
        return (_file(ETAS, RUN_00, 1),), None

    fetched: list[str] = []

    def datamart_bytes(url):
        fetched.append(url)
        return _dataset_bytes(ETAS, tmp_path)

    subset, service = gdsps_service.fetch_numeric(
        ETAS,
        (-63.5, 44.2, -62.5, 44.8),
        ROI,
        None,
        RUN_00,
        wcs_coverages=wcs_coverages,
        wcs_bytes=wcs_bytes,
        datamart_files=datamart_files,
        datamart_bytes=datamart_bytes,
    )
    assert service == "MSC Datamart"
    assert subset.variable == ETAS
    assert len(fetched) == 1


def test_fetch_numeric_unavailable_when_neither_source(tmp_path) -> None:
    with pytest.raises(GDSPSDataUnavailableError):
        gdsps_service.fetch_numeric(
            ETAS,
            (-63.5, 44.2, -62.5, 44.8),
            ROI,
            None,
            RUN_00,
            wcs_coverages=lambda: ((), None),
            wcs_bytes=lambda *a: b"",
            datamart_files=lambda: ((), None),
            datamart_bytes=lambda url: b"",
        )
