"""Hardened GeoMet WCS client for GDSPS numerical retrieval.

The Web Coverage Service is the preferred numerical path: a ``GetCoverage``
request returns only the requested ROI window as NetCDF.  As with WMS, the
coverage identifier is discovered from ``GetCapabilities`` rather than assumed.
When no matching coverage exists for the requested variable the client raises
:class:`GDSPSDataUnavailableError`, which the caller treats as the documented
signal to fall back to the Datamart NetCDF path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests

from .api import (
    REQUEST_TIMEOUT,
    _configure_session,
    _content_type,
)
from .gdsps_common import (
    GEOMET_ENDPOINT,
    RESPS_MODEL,
    GDSPSConfigurationError,
    GDSPSCoverageInfo,
    GDSPSDataUnavailableError,
    GDSPSDiscoveryError,
    GDSPSError,
    GDSPSRequestError,
    GDSPSResponseError,
    classify_model,
    classify_variable,
    normalize_variable,
    resps_member,
    validate_bbox,
)

logger = logging.getLogger(__name__)

GEOMET_WCS_URL = GEOMET_ENDPOINT
WCS_VERSION = "2.0.1"
XML_MEDIA_TYPES = frozenset(
    {"text/xml", "application/xml", "application/vnd.ogc.se_xml"}
)
NETCDF_MEDIA_TYPES = frozenset(
    {
        "application/x-netcdf",
        "application/netcdf",
        "image/netcdf",
        "application/octet-stream",
    }
)
MAX_CAPABILITIES_BYTES = 96_000_000
MAX_COVERAGE_BYTES = 128_000_000
# EPSG:4326 axis labels used by the WCS 2.0.1 SUBSET operators. GeoMet follows
# the OGC geographic convention; the Datamart path remains the guaranteed
# alternative if a deployment differs.
LONGITUDE_AXIS = "Long"
LATITUDE_AXIS = "Lat"
TIME_AXIS = "time"


class GDSPSWCSClient:
    """Discover GDSPS WCS coverages and fetch ROI-subset NetCDF."""

    def __init__(
        self,
        endpoint: str = GEOMET_WCS_URL,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint = _validate_endpoint(endpoint)
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)

    def discover_coverages(self) -> tuple[GDSPSCoverageInfo, ...]:
        """Return every GDSPS storm-surge coverage advertised by GeoMet."""

        params = {
            "service": "WCS",
            "version": WCS_VERSION,
            "request": "GetCapabilities",
        }
        response = self._get(params)
        text = _capabilities_text(response)
        root = _parse_xml(text)
        return _collect_coverages(root)

    def fetch_coverage(
        self,
        coverage: GDSPSCoverageInfo | str,
        *,
        bbox: tuple[float, float, float, float],
        time: datetime | None = None,
    ) -> bytes:
        """Fetch a NetCDF subset for one coverage over the ROI bounds.

        ``bbox`` is validated CRS84 ``(min_lon, min_lat, max_lon, max_lat)`` and
        constrains the download to the ROI; only the requested ``time`` is
        retrieved.  The whole global grid is never requested.
        """

        coverage_id = _coverage_id(coverage)
        min_lon, min_lat, max_lon, max_lat = validate_bbox(bbox)
        subsets = [
            f"{LONGITUDE_AXIS}({min_lon},{max_lon})",
            f"{LATITUDE_AXIS}({min_lat},{max_lat})",
        ]
        if time is not None:
            if not isinstance(time, datetime):
                raise GDSPSConfigurationError(
                    "The coverage time must be a datetime or None."
                )
            aware = time if time.tzinfo is not None else time.replace(
                tzinfo=timezone.utc
            )
            stamp = aware.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            subsets.append(f'{TIME_AXIS}("{stamp}")')
        params = {
            "service": "WCS",
            "version": WCS_VERSION,
            "request": "GetCoverage",
            "coverageId": coverage_id,
            "format": "image/netcdf",
            "subset": subsets,
        }
        response = self._get(params)
        return _coverage_bytes(response, coverage_id)

    def _get(self, params: dict[str, object]) -> requests.Response:
        try:
            response = self.session.get(
                self.endpoint,
                params=params,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            logger.warning("GeoMet WCS request timed out", exc_info=True)
            raise GDSPSRequestError(
                "The GeoMet WCS request timed out. Please try again."
            ) from exc
        except requests.ConnectionError as exc:
            logger.warning("Could not connect to GeoMet WCS", exc_info=True)
            raise GDSPSRequestError(
                "Could not connect to GeoMet. Check the network connection and "
                "try again."
            ) from exc
        except requests.RequestException as exc:
            logger.warning("GeoMet WCS request failed", exc_info=True)
            raise GDSPSRequestError(
                "The GeoMet WCS request could not be completed. Please try "
                "again."
            ) from exc

        if 200 <= response.status_code < 300:
            return response
        raise GDSPSRequestError(_status_message(response.status_code))


def find_coverage_for_variable(
    coverages: tuple[GDSPSCoverageInfo, ...],
    variable: str,
) -> GDSPSCoverageInfo:
    """Return the discovered coverage for a variable, or raise unavailable.

    Raising :class:`GDSPSDataUnavailableError` here is the documented trigger
    for the Datamart fallback.
    """

    target = normalize_variable(variable)
    if target is None:
        raise GDSPSConfigurationError(
            "A GDSPS variable (ETAS or SSH) is required for coverage lookup."
        )
    for coverage in coverages:
        if coverage.variable == target:
            return coverage
    raise GDSPSDataUnavailableError(
        f"GeoMet WCS does not advertise a {target} coverage for GDSPS. Use the "
        "Datamart NetCDF source instead."
    )


def _collect_coverages(root: ET.Element) -> tuple[GDSPSCoverageInfo, ...]:
    discovered: dict[str, GDSPSCoverageInfo] = {}
    for summary in _iter_local(root, "CoverageSummary"):
        coverage_id = _first_text(summary, "CoverageId")
        if not coverage_id:
            continue
        title = _first_text(summary, "Title") or coverage_id
        model = classify_model(coverage_id, title)
        if model is None:
            # Names no model — a container or unrelated coverage, not usable
            # storm-surge data. Keeps GDSPS and RESPS strictly separate.
            continue
        if coverage_id not in discovered:
            member = resps_member(coverage_id) if model == RESPS_MODEL else None
            discovered[coverage_id] = GDSPSCoverageInfo(
                coverage_id=coverage_id,
                title=title,
                variable=classify_variable(coverage_id, title),
                model=model,
                member=member,
            )
    return tuple(sorted(discovered.values(), key=lambda item: item.coverage_id))


def _coverage_id(coverage: GDSPSCoverageInfo | str) -> str:
    if isinstance(coverage, GDSPSCoverageInfo):
        return coverage.coverage_id
    if isinstance(coverage, str) and coverage.strip():
        return coverage.strip()
    raise GDSPSConfigurationError(
        "A discovered GDSPS coverage or coverage id is required."
    )


def _coverage_bytes(response: requests.Response, coverage_id: str) -> bytes:
    media_type = _content_type(response)
    content = response.content
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise GDSPSResponseError(
            f"GeoMet returned no data for coverage {coverage_id}."
        )
    if len(content) > MAX_COVERAGE_BYTES:
        raise GDSPSResponseError(
            "The GeoMet coverage response was unexpectedly large, so retrieval "
            "was stopped."
        )
    if media_type in XML_MEDIA_TYPES:
        # WCS reports request errors as an XML ExceptionReport even with 200.
        raise _exception_report_error(response.text, coverage_id)
    if media_type is not None and media_type not in NETCDF_MEDIA_TYPES:
        raise GDSPSResponseError(
            f"GeoMet returned an unsupported content type for coverage "
            f"{coverage_id}."
        )
    return bytes(content)


def _exception_report_error(text: str, coverage_id: str) -> GDSPSError:
    lowered = text.lower() if isinstance(text, str) else ""
    if "notfound" in lowered or "no such coverage" in lowered:
        return GDSPSDataUnavailableError(
            f"GeoMet WCS has no data for coverage {coverage_id}. Use the "
            "Datamart NetCDF source instead."
        )
    return GDSPSResponseError(
        f"GeoMet WCS rejected the coverage request for {coverage_id}."
    )


def _iter_local(root: ET.Element, local: str):
    for element in root.iter():
        if _local_name(element.tag) == local:
            yield element


def _first_text(element: ET.Element, local: str) -> str | None:
    for descendant in element.iter():
        if _local_name(descendant.tag) == local:
            text = (descendant.text or "").strip()
            if text:
                return text
    return None


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _capabilities_text(response: requests.Response) -> str:
    media_type = _content_type(response)
    if media_type not in XML_MEDIA_TYPES:
        raise GDSPSDiscoveryError(
            "GeoMet did not return an XML capabilities document."
        )
    content = response.content
    if not isinstance(content, (bytes, bytearray)):
        raise GDSPSDiscoveryError("The GeoMet capabilities response was empty.")
    if len(content) > MAX_CAPABILITIES_BYTES:
        raise GDSPSDiscoveryError(
            "The GeoMet capabilities document was unexpectedly large, so "
            "discovery was stopped."
        )
    return response.text


def _parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        logger.warning("Could not parse GeoMet WCS capabilities", exc_info=True)
        raise GDSPSDiscoveryError(
            "The GeoMet capabilities document could not be parsed."
        ) from exc


def _validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise GDSPSConfigurationError("The GeoMet endpoint is not configured.")
    candidate = endpoint.strip()
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GDSPSConfigurationError(
            "The GeoMet endpoint has an invalid port."
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise GDSPSConfigurationError(
            "The GeoMet endpoint must be an HTTPS URL without credentials, a "
            "query, or a fragment."
        )
    return candidate


def _status_message(status_code: int) -> str:
    if status_code == 429:
        return (
            "GeoMet is temporarily limiting requests (HTTP 429). Please wait "
            "and try again."
        )
    if 500 <= status_code < 600:
        return (
            f"GeoMet is temporarily unavailable (HTTP {status_code}). Please "
            "try again."
        )
    if 400 <= status_code < 500:
        return (
            f"GeoMet rejected the request (HTTP {status_code}). Please try "
            "again."
        )
    return (
        f"GeoMet returned an unexpected HTTP status ({status_code}). Please "
        "try again."
    )
