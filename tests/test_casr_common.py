"""Unit tests for CaSR shared helpers."""

from __future__ import annotations

from datetime import date

import pytest

from coastal_flood_explorer.casr_common import (
    CASR_RIVERS_DEFAULT,
    CASR_RIVERS_END,
    CASR_RIVERS_START,
    CASRConfigurationError,
    bbox_intersects,
    lon_to_wgs84,
    netcdf_variable_name,
    normalize_variable,
    parse_month_token,
    parse_rivers_filename,
)


def test_parse_rivers_filename() -> None:
    parsed = parse_rivers_filename(
        "198001_01AA000_MSC_CaSR-Rivers-Analysis_RiverDischarge_"
        "Sfc_LatLon0.00833_PT0H.nc"
    )
    assert parsed is not None
    assert parsed.year_month == "198001"
    assert parsed.subbasin_id == "01AA000"
    assert parsed.variable == "RiverDischarge"


def test_normalize_and_netcdf_variable() -> None:
    assert normalize_variable("riverdischarge") == "RiverDischarge"
    assert netcdf_variable_name("RiverChannelStorage") == "stor"
    with pytest.raises(CASRConfigurationError):
        netcdf_variable_name("SSH")


def test_parse_month_token_and_lon() -> None:
    assert parse_month_token(date(1980, 1, 15)) == "198001"
    assert parse_month_token("198012") == "198012"
    assert lon_to_wgs84(289.6) == pytest.approx(-70.4)
    assert bbox_intersects((-64.0, 44.0, -63.0, 45.0), (-63.5, 44.5, -62.0, 46.0))


def test_published_rivers_window_is_usable_offline() -> None:
    assert CASR_RIVERS_START <= CASR_RIVERS_DEFAULT <= CASR_RIVERS_END
    assert parse_month_token(CASR_RIVERS_START) == "198001"
    assert parse_month_token(CASR_RIVERS_DEFAULT) == "201712"
    assert parse_month_token(CASR_RIVERS_END) == "201712"
