"""Optional, endpoint-gated THREDDS/OPeNDAP client for GDSPS.

No official ECCC/DFO THREDDS endpoint for GDSPS is confirmed, so this client is
**inert by default**: :data:`GDSPS_THREDDS_CATALOG_URL` is ``None`` and
:func:`is_configured` returns ``False``, so the UI never offers it.  An operator
who has verified an official HTTPS catalog can set the constant (or pass the
URL explicitly); only then does the client parse the THREDDS ``catalog.xml`` for
GDSPS OPeNDAP datasets and open them with Xarray.  This satisfies the
requirement to document a working endpoint before using it and keeps WCS →
Datamart as the default numerical path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import requests

from .api import (
    REQUEST_TIMEOUT,
    _configure_session,
    _content_type,
)
from .gdsps_common import (
    GDSPSConfigurationError,
    GDSPSDiscoveryError,
    GDSPSRequestError,
    classify_variable,
    is_gdsps_identifier,
)

logger = logging.getLogger(__name__)

# Intentionally unset: no confirmed official GDSPS THREDDS endpoint exists.
GDSPS_THREDDS_CATALOG_URL: str | None = None

XML_MEDIA_TYPES = frozenset({"text/xml", "application/xml"})
MAX_CATALOG_BYTES = 8_000_000

DatasetOpener = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class GDSPSThreddsDataset:
    """A discovered GDSPS OPeNDAP dataset access URL."""

    name: str
    url: str
    variable: str | None


def is_configured(catalog_url: str | None = GDSPS_THREDDS_CATALOG_URL) -> bool:
    """Return whether a THREDDS catalog URL has been configured."""

    return isinstance(catalog_url, str) and bool(catalog_url.strip())


class GDSPSThreddsClient:
    """Discover and open GDSPS OPeNDAP datasets from a THREDDS catalog.

    Constructing this client without a configured catalog URL raises, so the
    caller must check :func:`is_configured` first.
    """

    def __init__(
        self,
        catalog_url: str | None = GDSPS_THREDDS_CATALOG_URL,
        *,
        session: requests.Session | None = None,
        opener: DatasetOpener | None = None,
    ) -> None:
        if not is_configured(catalog_url):
            raise GDSPSConfigurationError(
                "No GDSPS THREDDS/OPeNDAP endpoint is configured. Set "
                "GDSPS_THREDDS_CATALOG_URL to a verified official catalog first."
            )
        assert catalog_url is not None  # narrowed by is_configured
        self.catalog_url = _validate_https_url(catalog_url)
        self._origin = urlsplit(self.catalog_url)
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)
        self._opener = opener

    def discover_datasets(self) -> tuple[GDSPSThreddsDataset, ...]:
        """Parse the THREDDS catalog for GDSPS OPeNDAP dataset URLs."""

        response = self._get(self.catalog_url)
        text = _catalog_text(response)
        root = _parse_xml(text)
        opendap_base = _opendap_service_base(root)
        if opendap_base is None:
            return ()

        discovered: dict[str, GDSPSThreddsDataset] = {}
        for dataset in _iter_local(root, "dataset"):
            url_path = dataset.get("urlPath")
            if not url_path:
                continue
            name = dataset.get("name") or url_path
            identifier = f"{name} {url_path}"
            if not is_gdsps_identifier(identifier):
                continue
            access_url = urljoin(
                self.catalog_url,
                _join_service(opendap_base, url_path),
            )
            if access_url in discovered:
                continue
            discovered[access_url] = GDSPSThreddsDataset(
                name=name,
                url=access_url,
                variable=classify_variable(name, url_path),
            )
        return tuple(sorted(discovered.values(), key=lambda item: item.url))

    def open_dataset(self, dataset: GDSPSThreddsDataset | str) -> Any:
        """Open one OPeNDAP dataset lazily with Xarray.

        The returned dataset is lazy: coordinate subsetting and masking happen
        in :mod:`coastal_flood_explorer.gdsps_processing`, so only the requested
        window is transferred when the data is finally loaded.
        """

        url = _dataset_url(dataset)
        opener = self._opener
        if opener is None:
            try:
                import xarray as xr
            except ImportError as exc:  # pragma: no cover - xarray is required.
                raise GDSPSConfigurationError(
                    "Xarray is required to open OPeNDAP datasets."
                ) from exc
            opener = xr.open_dataset
        try:
            return opener(url)
        except Exception as exc:  # noqa: BLE001 - opener errors are made safe.
            logger.warning("Could not open GDSPS OPeNDAP dataset", exc_info=True)
            raise GDSPSRequestError(
                "The GDSPS OPeNDAP dataset could not be opened."
            ) from exc

    def _get(self, url: str) -> requests.Response:
        try:
            response = self.session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            logger.warning("GDSPS THREDDS request timed out", exc_info=True)
            raise GDSPSRequestError(
                "The GDSPS THREDDS request timed out. Please try again."
            ) from exc
        except requests.ConnectionError as exc:
            logger.warning("Could not connect to GDSPS THREDDS", exc_info=True)
            raise GDSPSRequestError(
                "Could not connect to the GDSPS THREDDS catalog. Check the "
                "network connection and try again."
            ) from exc
        except requests.RequestException as exc:
            logger.warning("GDSPS THREDDS request failed", exc_info=True)
            raise GDSPSRequestError(
                "The GDSPS THREDDS request could not be completed. Please try "
                "again."
            ) from exc

        if 200 <= response.status_code < 300:
            return response
        raise GDSPSRequestError(
            f"The GDSPS THREDDS catalog returned HTTP {response.status_code}."
        )


def _opendap_service_base(root: ET.Element) -> str | None:
    # ``_iter_local`` already descends into nested compound services, so a flat
    # scan finds the OPeNDAP base without recursion.
    for service in _iter_local(root, "service"):
        service_type = (service.get("serviceType") or "").strip().lower()
        base = service.get("base")
        if service_type == "opendap" and base:
            return base
    return None


def _join_service(base: str, url_path: str) -> str:
    return f"{base.rstrip('/')}/{url_path.lstrip('/')}"


def _dataset_url(dataset: GDSPSThreddsDataset | str) -> str:
    if isinstance(dataset, GDSPSThreddsDataset):
        return dataset.url
    if isinstance(dataset, str) and dataset.strip():
        return dataset.strip()
    raise GDSPSConfigurationError(
        "A discovered GDSPS OPeNDAP dataset or URL is required."
    )


def _iter_local(root: ET.Element, local: str):
    for element in root.iter():
        if _local_name(element.tag) == local:
            yield element


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _catalog_text(response: requests.Response) -> str:
    media_type = _content_type(response)
    if media_type not in XML_MEDIA_TYPES:
        raise GDSPSDiscoveryError(
            "The GDSPS THREDDS catalog did not return XML."
        )
    content = response.content
    if not isinstance(content, (bytes, bytearray)):
        raise GDSPSDiscoveryError("The GDSPS THREDDS catalog was empty.")
    if len(content) > MAX_CATALOG_BYTES:
        raise GDSPSDiscoveryError(
            "The GDSPS THREDDS catalog was unexpectedly large, so discovery "
            "was stopped."
        )
    return response.text


def _parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        logger.warning("Could not parse GDSPS THREDDS catalog", exc_info=True)
        raise GDSPSDiscoveryError(
            "The GDSPS THREDDS catalog could not be parsed."
        ) from exc


def _validate_https_url(url: str) -> str:
    candidate = url.strip()
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GDSPSConfigurationError(
            "The GDSPS THREDDS catalog URL has an invalid port."
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise GDSPSConfigurationError(
            "The GDSPS THREDDS catalog URL must be a valid HTTPS URL without "
            "credentials."
        )
    return candidate
