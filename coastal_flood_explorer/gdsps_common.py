"""Shared types and helpers for ECCC GDSPS storm-surge support.

The Global Deterministic Storm Surge Prediction System (GDSPS) is an ECCC/MSC
ocean-model product.  Its errors therefore belong to the same safe-message
family as the Coastal Flooding archive: :class:`GDSPSError` subclasses
:class:`coastal_flood_explorer.api.ECCCError`, so every user-visible GDSPS
message is deliberately safe to display.

This module owns only value types, the variable vocabulary, and small pure
helpers.  Transport lives in the ``gdsps_wms``/``gdsps_wcs``/``gdsps_datamart``/
``gdsps_thredds`` clients; Xarray processing lives in ``gdsps_processing``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .api import ECCCError

# GDSPS is served from the same GeoMet OGC endpoint as other MSC NWP layers
# and from the same Datamart host already used by the Coastal Flooding archive.
GEOMET_ENDPOINT = "https://geo.weather.gc.ca/geomet"
GDSPS_DATAMART_ROOT = "https://dd.weather.gc.ca"
GDSPS_DATAMART_PATH = "/model_gdsps/"

# The two GDSPS variables must never be conflated.  ETAS is storm-surge
# elevation (derived from SSH by harmonic analysis); SSH is total water level
# and is not an engineering/chart datum.
ETAS = "ETAS"
SSH = "SSH"
GDSPS_VARIABLES: tuple[str, ...] = (ETAS, SSH)

VARIABLE_DEFINITIONS: dict[str, str] = {
    ETAS: (
        "Storm-surge elevation (metres). Derived from total water level (SSH) "
        "by harmonic analysis. This is the surge component, not the total "
        "water level."
    ),
    SSH: (
        "Total water level / sea-surface height (metres). This is the modelled "
        "total water level and is NOT an engineering or chart datum."
    ),
}

# A layer name, coverage id, or filename is treated as GDSPS storm-surge
# content when it mentions the model or either variable.  Kept intentionally
# broad because the exact GeoMet layer naming is discovered, not assumed.
_GDSPS_TOKEN = re.compile(
    r"(?:gdsps|storm[\s_-]*surge|\bETAS\b|\bSSH\b)",
    re.IGNORECASE,
)
_ETAS_TOKEN = re.compile(r"(?:\bETAS\b|storm[\s_-]*surge)", re.IGNORECASE)
_SSH_TOKEN = re.compile(r"(?:\bSSH\b|sea[\s_-]*surface|total[\s_-]*water)", re.IGNORECASE)


class GDSPSError(ECCCError):
    """Base class for GDSPS errors whose messages are safe to show users."""


class GDSPSConfigurationError(GDSPSError, ValueError):
    """Raised when a GDSPS client input or configuration is unsafe."""


class GDSPSRequestError(GDSPSError):
    """Raised when a GDSPS HTTP request cannot be completed."""


class GDSPSResponseError(GDSPSError):
    """Raised when a GDSPS response is malformed or unsupported."""


class GDSPSDiscoveryError(GDSPSError):
    """Raised when a capabilities/catalogue/directory cannot be parsed."""


class GDSPSDataUnavailableError(GDSPSError):
    """Raised when no usable GDSPS data exists for the request.

    This is the documented trigger for falling back from one numerical source
    to the next (WCS → Datamart), not necessarily a hard failure.
    """


@dataclass(frozen=True, slots=True)
class GDSPSRun:
    """One GDSPS model run (issuance)."""

    issue_time: datetime
    cycle: str

    @property
    def label(self) -> str:
        """Return a concise, human-readable run label."""

        return f"{self.issue_time.strftime('%Y-%m-%d')} {self.cycle}Z"

    @property
    def stamp(self) -> str:
        """Return a stable filename/key stamp for this run."""

        return f"{self.issue_time.strftime('%Y%m%d')}T{self.cycle}Z"

    def metadata(self) -> dict[str, Any]:
        """Return JSON-serializable run metadata."""

        return {
            "issue_time": utc_text(self.issue_time),
            "cycle": self.cycle,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class GDSPSLayerInfo:
    """A discovered GeoMet WMS layer for GDSPS storm-surge content."""

    name: str
    title: str
    variable: str | None
    available_times: tuple[datetime, ...] = ()

    def metadata(self) -> dict[str, Any]:
        """Return JSON-serializable layer metadata."""

        return {
            "name": self.name,
            "title": self.title,
            "variable": self.variable,
            "available_times": [utc_text(value) for value in self.available_times],
        }


@dataclass(frozen=True, slots=True)
class GDSPSCoverageInfo:
    """A discovered GeoMet WCS coverage for GDSPS storm-surge content."""

    coverage_id: str
    title: str
    variable: str | None

    def metadata(self) -> dict[str, Any]:
        """Return JSON-serializable coverage metadata."""

        return {
            "coverage_id": self.coverage_id,
            "title": self.title,
            "variable": self.variable,
        }


@dataclass(frozen=True, slots=True)
class GDSPSDatamartFile:
    """A discovered Datamart NetCDF file for one GDSPS variable and lead time."""

    filename: str
    url: str
    variable: str
    run: GDSPSRun
    lead_hours: int
    valid_time: datetime

    @property
    def label(self) -> str:
        """Return a concise, human-readable file label."""

        return f"{self.run.label} · {self.variable} · +{self.lead_hours}h"

    def metadata(self) -> dict[str, Any]:
        """Return JSON-serializable file metadata."""

        return {
            "filename": self.filename,
            "url": self.url,
            "variable": self.variable,
            "run": self.run.metadata(),
            "lead_hours": self.lead_hours,
            "valid_time": utc_text(self.valid_time),
            "label": self.label,
        }


def is_gdsps_identifier(value: str | None) -> bool:
    """Return whether a layer name/coverage id/filename looks like GDSPS."""

    return isinstance(value, str) and _GDSPS_TOKEN.search(value) is not None


def classify_variable(*candidates: str | None) -> str | None:
    """Return ``"ETAS"``/``"SSH"`` if any candidate names a GDSPS variable.

    ETAS is checked before SSH because storm-surge phrasing is more specific;
    an unrecognized candidate contributes nothing.  ``None`` means the variable
    could not be determined from the supplied text.
    """

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        if _ETAS_TOKEN.search(candidate):
            return ETAS
        if _SSH_TOKEN.search(candidate):
            return SSH
    return None


def variable_definition(variable: str) -> str:
    """Return the human-readable definition for a GDSPS variable code."""

    key = normalize_variable(variable)
    if key is None:
        raise GDSPSConfigurationError(
            "The GDSPS variable must be one of: "
            f"{', '.join(GDSPS_VARIABLES)}."
        )
    return VARIABLE_DEFINITIONS[key]


def normalize_variable(variable: Any) -> str | None:
    """Return the canonical variable code for a case-insensitive input."""

    if not isinstance(variable, str):
        return None
    candidate = variable.strip().upper()
    return candidate if candidate in GDSPS_VARIABLES else None


def utc_text(value: datetime) -> str:
    """Return an aware datetime as ISO-8601 UTC with a ``Z`` suffix."""

    if not isinstance(value, datetime):
        raise GDSPSConfigurationError("A datetime is required for UTC text.")
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware UTC datetime.

    A trailing ``Z`` is accepted, and an offset-naive timestamp is interpreted
    as UTC to match ECCC's dimension formatting.
    """

    if not isinstance(value, str) or not value.strip():
        raise GDSPSResponseError("A GDSPS timestamp was missing or invalid.")
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise GDSPSResponseError(
            "A GDSPS timestamp was not a valid ISO-8601 value."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Validate an ordered CRS84 ``(min_lon, min_lat, max_lon, max_lat)`` box."""

    if isinstance(bbox, (str, bytes)):
        raise GDSPSConfigurationError("The ROI bounds must be four numbers.")
    try:
        values = tuple(bbox)
    except TypeError as exc:
        raise GDSPSConfigurationError(
            "The ROI bounds must be four numbers."
        ) from exc
    if len(values) != 4 or any(isinstance(value, bool) for value in values):
        raise GDSPSConfigurationError("The ROI bounds must be four numbers.")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise GDSPSConfigurationError(
            "The ROI bounds contain a non-numeric value."
        ) from exc
    coordinates = (min_lon, min_lat, max_lon, max_lat)
    if not all(math.isfinite(value) for value in coordinates):
        raise GDSPSConfigurationError("The ROI bounds contain a non-finite value.")
    if not -180.0 <= min_lon <= 180.0 or not -180.0 <= max_lon <= 180.0:
        raise GDSPSConfigurationError("The ROI longitudes must be within -180..180.")
    if not -90.0 <= min_lat <= 90.0 or not -90.0 <= max_lat <= 90.0:
        raise GDSPSConfigurationError("The ROI latitudes must be within -90..90.")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise GDSPSConfigurationError("The ROI bounds must be ordered and non-zero.")
    return coordinates
