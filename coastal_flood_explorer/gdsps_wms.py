"""Hardened GeoMet WMS client for GDSPS layer discovery and map overlay.

GeoMet serves MSC layers through a standard OGC Web Map Service.  Because the
exact GDSPS layer name is discovered rather than assumed, this client fetches
``GetCapabilities`` and pattern-matches storm-surge layers, extracting each
layer's ``time`` dimension so the UI can offer real forecast-valid times.  The
overlay itself is drawn by the browser from tile requests, so nothing here
fetches map imagery.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
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
    GDSPSConfigurationError,
    GDSPSDiscoveryError,
    GDSPSLayerInfo,
    GDSPSRequestError,
    classify_variable,
    is_gdsps_identifier,
    parse_iso_utc,
)

logger = logging.getLogger(__name__)

GEOMET_WMS_URL = GEOMET_ENDPOINT
WMS_VERSION = "1.3.0"
XML_MEDIA_TYPES = frozenset(
    {
        "text/xml",
        "application/xml",
        "application/vnd.ogc.wms_xml",
    }
)
MAX_CAPABILITIES_BYTES = 96_000_000
MAX_TIMES_PER_LAYER = 5_000
WMS_TILE_FORMAT = "image/png"
WMS_ATTRIBUTION = "Environment and Climate Change Canada — GeoMet"

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class GDSPSWMSClient:
    """Discover GDSPS storm-surge WMS layers from GeoMet capabilities."""

    def __init__(
        self,
        endpoint: str = GEOMET_WMS_URL,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint = _validate_endpoint(endpoint)
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)

    def discover_layers(self) -> tuple[GDSPSLayerInfo, ...]:
        """Return every GDSPS storm-surge layer advertised by GeoMet.

        An empty tuple means GeoMet does not currently advertise a matching
        layer; that is a normal outcome, not an error, and the caller decides
        how to message it.
        """

        params = {
            "service": "WMS",
            "version": WMS_VERSION,
            "request": "GetCapabilities",
        }
        response = self._get(params)
        text = _capabilities_text(response)
        root = _parse_xml(text)
        layers = _collect_layers(root)
        return layers

    def _get(self, params: dict[str, str]) -> requests.Response:
        try:
            response = self.session.get(
                self.endpoint,
                params=params,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            logger.warning("GeoMet WMS request timed out", exc_info=True)
            raise GDSPSRequestError(
                "The GeoMet WMS request timed out. Please try again."
            ) from exc
        except requests.ConnectionError as exc:
            logger.warning("Could not connect to GeoMet WMS", exc_info=True)
            raise GDSPSRequestError(
                "Could not connect to GeoMet. Check the network connection and "
                "try again."
            ) from exc
        except requests.RequestException as exc:
            logger.warning("GeoMet WMS request failed", exc_info=True)
            raise GDSPSRequestError(
                "The GeoMet WMS request could not be completed. Please try "
                "again."
            ) from exc

        status_code = response.status_code
        if 200 <= status_code < 300:
            return response
        raise GDSPSRequestError(_status_message(status_code))


def build_wms_tile_params(
    layer: GDSPSLayerInfo,
    *,
    time: datetime | None = None,
    opacity: float = 0.7,
    endpoint: str = GEOMET_WMS_URL,
) -> dict[str, object]:
    """Return validated parameters for a GDSPS WMS overlay tile layer.

    The returned mapping is deliberately transport-agnostic: ``map_view`` builds
    the Folium ``WmsTileLayer`` from it, so this function never trusts a raw URL
    string blindly and the opacity is validated here rather than at the widget.
    """

    if not isinstance(layer, GDSPSLayerInfo):
        raise GDSPSConfigurationError(
            "A discovered GDSPS layer is required for the WMS overlay."
        )
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
        raise GDSPSConfigurationError("The overlay opacity must be a number.")
    opacity_value = float(opacity)
    if not 0.0 <= opacity_value <= 1.0:
        raise GDSPSConfigurationError(
            "The overlay opacity must be between 0 and 1."
        )
    time_text: str | None = None
    if time is not None:
        if not isinstance(time, datetime):
            raise GDSPSConfigurationError(
                "The overlay time must be a datetime or None."
            )
        aware = time if time.tzinfo is not None else time.replace(
            tzinfo=timezone.utc
        )
        time_text = aware.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    return {
        "url": _validate_endpoint(endpoint),
        "layers": layer.name,
        "styles": "",
        "fmt": WMS_TILE_FORMAT,
        "transparent": True,
        "version": WMS_VERSION,
        "opacity": opacity_value,
        "time": time_text,
        "attribution": WMS_ATTRIBUTION,
        "variable": layer.variable,
    }


def _collect_layers(root: ET.Element) -> tuple[GDSPSLayerInfo, ...]:
    discovered: dict[str, GDSPSLayerInfo] = {}
    _walk_layers(root, inherited_times=(), discovered=discovered)
    return tuple(
        sorted(discovered.values(), key=lambda item: item.name)
    )


def _walk_layers(
    element: ET.Element,
    *,
    inherited_times: tuple[datetime, ...],
    discovered: dict[str, GDSPSLayerInfo],
) -> None:
    for child in element:
        if _local_name(child.tag) != "Layer":
            # Descend through non-Layer containers (e.g. <Capability>) without
            # altering the inherited time dimension.
            _walk_layers(
                child,
                inherited_times=inherited_times,
                discovered=discovered,
            )
            continue
        own_times = _layer_time_dimension(child)
        effective_times = own_times or inherited_times
        name = _child_text(child, "Name")
        title = _child_text(child, "Title") or (name or "")
        if name and (
            is_gdsps_identifier(name) or is_gdsps_identifier(title)
        ):
            if name not in discovered:
                discovered[name] = GDSPSLayerInfo(
                    name=name,
                    title=title,
                    variable=classify_variable(name, title),
                    available_times=effective_times,
                )
        _walk_layers(
            child,
            inherited_times=effective_times,
            discovered=discovered,
        )


def _layer_time_dimension(layer: ET.Element) -> tuple[datetime, ...]:
    for child in layer:
        if _local_name(child.tag) != "Dimension":
            continue
        if (child.get("name") or "").strip().lower() != "time":
            continue
        return _parse_time_dimension(child.text or "")
    return ()


def _parse_time_dimension(text: str) -> tuple[datetime, ...]:
    times: list[datetime] = []
    for token in text.split(","):
        candidate = token.strip()
        if not candidate:
            continue
        if "/" in candidate:
            times.extend(_expand_interval(candidate))
        else:
            try:
                times.append(parse_iso_utc(candidate))
            except GDSPSDiscoveryError:
                continue
            except Exception:  # noqa: BLE001 - malformed token is skipped.
                continue
        if len(times) >= MAX_TIMES_PER_LAYER:
            break
    unique = sorted(set(times))
    return tuple(unique[:MAX_TIMES_PER_LAYER])


def _expand_interval(interval: str) -> list[datetime]:
    parts = interval.split("/")
    if len(parts) != 3:
        return []
    try:
        start = parse_iso_utc(parts[0])
        end = parse_iso_utc(parts[1])
    except Exception:  # noqa: BLE001 - malformed interval is skipped.
        return []
    step = _parse_duration(parts[2])
    if step is None or step <= timedelta(0) or end < start:
        return []
    result: list[datetime] = []
    current = start
    while current <= end and len(result) < MAX_TIMES_PER_LAYER:
        result.append(current)
        current = current + step
    return result


def _parse_duration(text: str) -> timedelta | None:
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        return None
    parts = {key: int(value) for key, value in match.groupdict().items() if value}
    if not parts:
        return None
    return timedelta(
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )


def _child_text(element: ET.Element, local: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == local:
            text = (child.text or "").strip()
            return text or None
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
    # ElementTree does not expand external entities, so this is safe against
    # XXE for the bounded, media-type-checked capabilities document.
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        logger.warning("Could not parse GeoMet WMS capabilities", exc_info=True)
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
