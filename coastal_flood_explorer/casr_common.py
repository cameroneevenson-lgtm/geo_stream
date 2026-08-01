"""Shared types and helpers for ECCC CaSR-Rivers support.

CaSR-Rivers v2.1 is published as static NetCDF on ECCC's collaborative HPFX
host (not GeoMet WMS). Errors belong to the same safe-message family as other
ECCC clients: :class:`CASRError` subclasses
:class:`coastal_flood_explorer.api.ECCCError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .api import ECCCError

CASR_HPFX_ROOT = "https://hpfx.collab.science.gc.ca"
CASR_RIVERS_BASE_PATH = "/~scar700/rcas-casr/data/CaSR-Rivers_v2.1/per_subbasin/"

RIVER_DISCHARGE = "RiverDischarge"
RIVER_CHANNEL_STORAGE = "RiverChannelStorage"
DEEP_RESERVOIR_STORAGE = "DeepReservoirStorage"

CASR_RIVERS_VARIABLES: tuple[str, ...] = (
    RIVER_DISCHARGE,
    RIVER_CHANNEL_STORAGE,
    DEEP_RESERVOIR_STORAGE,
)

# Filename product token → NetCDF data-variable name (verified on live samples).
VARIABLE_NETCDF_NAMES: dict[str, str] = {
    RIVER_DISCHARGE: "disc",
    RIVER_CHANNEL_STORAGE: "stor",
    DEEP_RESERVOIR_STORAGE: "lzs",
}

VARIABLE_DEFINITIONS: dict[str, str] = {
    RIVER_DISCHARGE: (
        "Mean streamflow discharge exiting the river channel over the hour "
        "ending at the indicated time (m³/s). Historical reanalysis — not a "
        "live forecast or flood warning."
    ),
    RIVER_CHANNEL_STORAGE: (
        "Water stored in the river channel (m³). Historical reanalysis — not "
        "a live forecast or flood warning."
    ),
    DEEP_RESERVOIR_STORAGE: (
        "Lower-zone / deep reservoir storage depth (kg/m²). Historical "
        "reanalysis — not a live forecast or flood warning."
    ),
}

# Smallest product — used only to probe basin bounding boxes cheaply.
PROBE_VARIABLE = DEEP_RESERVOIR_STORAGE

_FILENAME = re.compile(
    r"^(?P<yyyymm>\d{6})_(?P<subbasin>[0-9A-Za-z]+)_MSC_CaSR-Rivers-Analysis_"
    r"(?P<variable>RiverDischarge|RiverChannelStorage|DeepReservoirStorage)_"
    r"Sfc_LatLon0\.00833_PT0H\.nc$"
)
_MONTH_DIR = re.compile(r"^(?P<yyyymm>\d{6})/?$")


class CASRError(ECCCError):
    """Base class for CaSR errors safe to display to users."""


class CASRConfigurationError(CASRError, ValueError):
    """Raised when CaSR client inputs are unsafe or invalid."""


class CASRRequestError(CASRError):
    """Raised when an HPFX CaSR resource cannot be retrieved."""


class CASRResponseError(CASRError):
    """Raised when a CaSR listing or NetCDF payload is unusable."""


class CASRDataUnavailableError(CASRError):
    """Raised when no CaSR product matches the request."""


@dataclass(frozen=True, slots=True)
class CASRRiversFile:
    """One discovered per-subbasin CaSR-Rivers NetCDF file."""

    year_month: str
    subbasin_id: str
    variable: str
    filename: str
    url: str


@dataclass(frozen=True, slots=True)
class CASRBasinHit:
    """A sub-basin whose grid intersects the drawn ROI."""

    subbasin_id: str
    bbox: tuple[float, float, float, float]
    probe_url: str


def normalize_variable(value: str | None) -> str | None:
    """Return a canonical CaSR-Rivers variable token, or ``None``."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    for name in CASR_RIVERS_VARIABLES:
        if cleaned.lower() == name.lower():
            return name
    return None


def netcdf_variable_name(variable: str) -> str:
    """Return the NetCDF data-variable name for a product token."""

    canonical = normalize_variable(variable)
    if canonical is None:
        raise CASRConfigurationError(
            "CaSR-Rivers variable must be RiverDischarge, "
            "RiverChannelStorage, or DeepReservoirStorage."
        )
    return VARIABLE_NETCDF_NAMES[canonical]


def parse_rivers_filename(filename: str) -> CASRRiversFile | None:
    """Parse a CaSR-Rivers per-subbasin filename, or return ``None``."""

    if not isinstance(filename, str):
        return None
    name = filename.rsplit("/", 1)[-1]
    match = _FILENAME.match(name)
    if match is None:
        return None
    return CASRRiversFile(
        year_month=match.group("yyyymm"),
        subbasin_id=match.group("subbasin"),
        variable=match.group("variable"),
        filename=name,
        url="",
    )


def parse_month_token(value: str | date) -> str:
    """Return a ``YYYYMM`` month token."""

    if isinstance(value, date):
        return f"{value.year:04d}{value.month:02d}"
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}", value.strip()):
        raise CASRConfigurationError(
            "CaSR month must be a YYYYMM value or a date."
        )
    token = value.strip()
    year = int(token[:4])
    month = int(token[4:])
    if month < 1 or month > 12 or year < 1968 or year > 2100:
        raise CASRConfigurationError(
            "CaSR month is outside the supported calendar range."
        )
    return token


def month_directory_name(year_month: str) -> str | None:
    """Return the directory name when ``year_month`` looks like ``YYYYMM``."""

    match = _MONTH_DIR.match(year_month.strip())
    return None if match is None else match.group("yyyymm")


def lon_to_wgs84(lon: float) -> float:
    """Convert 0–360 longitudes to the ``[-180, 180]`` range Folium expects."""

    value = float(lon)
    if value > 180.0:
        return value - 360.0
    return value


def bbox_intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    """Return whether two ``(min_lon, min_lat, max_lon, max_lat)`` boxes overlap."""

    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )
