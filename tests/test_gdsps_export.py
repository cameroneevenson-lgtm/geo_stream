"""Unit tests for the GDSPS model-neutral export package."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from coastal_flood_explorer.gdsps_common import ETAS, GDSPSConfigurationError, GDSPSRun
from coastal_flood_explorer.gdsps_export import (
    CSV_MEMBER,
    GEOJSON_MEMBER,
    METADATA_MEMBER,
    NETCDF_MEMBER,
    README_MEMBER,
    build_export_zip,
)
from coastal_flood_explorer.gdsps_processing import GDSPSSubset


def build_subset(variable: str = "ETAS") -> GDSPSSubset:
    lon = np.array([-63.0, -62.5])
    lat = np.array([44.0, 44.5])
    times = pd.date_range("2026-07-22T00:00", periods=2, freq="1h")
    data = np.ones((times.size, lat.size, lon.size))
    dataset = xr.Dataset(
        {variable: (("time", "lat", "lon"), data, {"units": "m"})},
        coords={"time": times, "lat": lat, "lon": lon},
    )
    series = pd.DataFrame(
        {"time_utc": times, f"{variable}_value": [1.0, 1.0]}
    )
    return GDSPSSubset(
        dataset=dataset,
        point_series=series,
        variable=variable,
        variable_name=variable,
        longitude_name="lon",
        latitude_name="lat",
        time_name="time",
        bbox=(-63.0, 44.0, -62.5, 44.5),
        roi_point=(-62.75, 44.25),
        units="m",
        warnings=(),
    )


ROI = {
    "type": "Polygon",
    "coordinates": [
        [[-63.0, 44.0], [-62.5, 44.0], [-62.5, 44.5], [-63.0, 44.5], [-63.0, 44.0]]
    ],
}


def read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_export_contains_all_members() -> None:
    run = GDSPSRun(datetime(2026, 7, 22, tzinfo=timezone.utc), "00")
    data = build_export_zip(
        build_subset(),
        roi=ROI,
        source_service="GeoMet WCS",
        run=run,
        generated_at=datetime(2026, 7, 22, 6, tzinfo=timezone.utc),
    )
    members = read_zip(data)
    assert set(members) == {
        NETCDF_MEMBER,
        CSV_MEMBER,
        GEOJSON_MEMBER,
        METADATA_MEMBER,
        README_MEMBER,
    }


def test_metadata_has_variable_definition_and_service() -> None:
    data = build_export_zip(
        build_subset("SSH"), roi=ROI, source_service="MSC Datamart"
    )
    metadata = json.loads(read_zip(data)[METADATA_MEMBER])
    assert metadata["variable"] == "SSH"
    assert metadata["source_service"] == "MSC Datamart"
    assert "not an engineering" in metadata["variable_definition"].lower()
    assert metadata["roi_bbox_crs84"]["min_lon"] == -63.0


def test_readme_and_netcdf_are_model_neutral(tmp_path) -> None:
    data = build_export_zip(build_subset(), roi=ROI, source_service="GeoMet WCS")
    members = read_zip(data)
    readme = members[README_MEMBER].decode("utf-8")
    assert "MIKE" in readme and "HEC-RAS" in readme
    assert "not a warning service" in readme.lower()
    # NetCDF member is a real, re-openable dataset.
    path = tmp_path / "member.nc"
    path.write_bytes(members[NETCDF_MEMBER])
    with xr.open_dataset(path) as roundtrip:
        assert "ETAS" in roundtrip.data_vars


def test_geojson_roundtrips_to_roi() -> None:
    data = build_export_zip(build_subset(), roi=ROI, source_service="GeoMet WCS")
    geojson = json.loads(read_zip(data)[GEOJSON_MEMBER])
    assert geojson["type"] == "FeatureCollection"
    assert geojson["features"][0]["geometry"]["type"] == "Polygon"


def test_csv_has_point_series() -> None:
    data = build_export_zip(build_subset(), roi=ROI, source_service="GeoMet WCS")
    csv_text = read_zip(data)[CSV_MEMBER].decode("utf-8")
    assert "ETAS_value" in csv_text.splitlines()[0]


def test_deterministic_for_identical_inputs() -> None:
    kwargs = dict(
        roi=ROI,
        source_service="GeoMet WCS",
        generated_at=datetime(2026, 7, 22, 6, tzinfo=timezone.utc),
    )
    first = build_export_zip(build_subset(), **kwargs)
    second = build_export_zip(build_subset(), **kwargs)
    assert first == second


def test_invalid_inputs_raise() -> None:
    with pytest.raises(GDSPSConfigurationError):
        build_export_zip(object(), roi=ROI, source_service="x")  # type: ignore[arg-type]
    with pytest.raises(GDSPSConfigurationError):
        build_export_zip(build_subset(), roi=ROI, source_service="  ")
