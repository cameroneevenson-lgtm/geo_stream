"""Client for ECCC's recent Datamart Coastal Flooding archive.

The GeoMet collection represents the current publication state.  ECCC's
Datamart keeps recent daily snapshots under date-partitioned ``WXO-DD``
directories instead.  This module discovers the official GeoJSON products for
one archive date, selects the latest amendment of each logical product, and
combines the files into a fresh FeatureCollection.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, TypeAlias
from urllib.parse import urljoin, urlsplit

import requests

from .api import (
    JSON_MEDIA_TYPES,
    MAX_TOTAL_FEATURES,
    REQUEST_TIMEOUT,
    ECCCError,
    _configure_session,
    _content_type,
    _origin,
)
from .properties import json_safe

logger = logging.getLogger(__name__)

FeatureCollection: TypeAlias = dict[str, Any]

ARCHIVE_BASE_URL = "https://dd.weather.gc.ca"
ECCC_ARCHIVE_ROOT = ARCHIVE_BASE_URL
ARCHIVE_PATH_TEMPLATE = (
    "/{archive_date}/WXO-DD/coastal-flooding/risk-index/"
)
MAX_ARCHIVE_FILES = 500
MAX_DIRECTORY_BYTES = 2_000_000
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})

_PRODUCT_FILENAME = re.compile(
    r"^(?P<logical>"
    r"(?P<stamp>(?P<archive_date>\d{8})T\d{4}Z)"
    r"_MSC_CoastalFloodingRiskIndex"
    r"_(?P<office>[A-Za-z0-9]+)_(?P<coverage>[A-Za-z0-9]+)"
    r"_PT(?P<duration_hours>\d{3})H"
    r"(?:(?P<duration_minutes>\d{2})M)?"
    r")_v(?P<version>[1-9]\d*)\.json$"
)


class ArchiveError(ECCCError):
    """Base class for recent-archive errors safe to display to users."""


class ECCCArchiveError(ArchiveError):
    """Backward-compatible specific base name for archive errors."""


class ArchiveDateValidationError(ECCCArchiveError, ValueError):
    """Raised when an archive date is not a real ``YYYYMMDD`` date."""


class ECCCArchiveConfigurationError(ECCCArchiveError, ValueError):
    """Raised when the archive client has an unsafe configuration."""


class ECCCArchiveRequestError(ECCCArchiveError):
    """Raised when an archive resource cannot be retrieved."""


class ECCCArchiveDirectoryError(ECCCArchiveError):
    """Raised when the Datamart directory cannot be interpreted safely."""


class ECCCArchiveResponseError(ECCCArchiveError):
    """Raised when an archive product is not a FeatureCollection."""


@dataclass(frozen=True, slots=True)
class ArchiveProduct:
    """One selected Coastal Flooding Risk Index archive product."""

    filename: str
    url: str
    logical_name: str
    version: int
    issue_time: datetime
    valid_time: datetime
    office: str
    coverage: str
    lead_hours: int
    lead_minutes: int

    @property
    def label(self) -> str:
        """Return a concise, human-readable selector label."""

        issue = self.issue_time.strftime("%Y-%m-%d %H:%MZ")
        lead = f"+{self.lead_hours}h"
        if self.lead_minutes:
            lead = f"{lead} {self.lead_minutes}m"
        return f"{issue} · {self.office}/{self.coverage} · {lead}"

    def metadata(self) -> dict[str, Any]:
        """Return JSON-serializable product metadata."""

        return {
            "filename": self.filename,
            "url": self.url,
            "logical_name": self.logical_name,
            "version": self.version,
            "issue_time": _utc_text(self.issue_time),
            "valid_time": _utc_text(self.valid_time),
            "office": self.office,
            "coverage": self.coverage,
            "lead_hours": self.lead_hours,
            "lead_minutes": self.lead_minutes,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class ArchiveDocument:
    """An exact decoded ECCC file kept separately from merged features."""

    product: ArchiveProduct
    payload: FeatureCollection


@dataclass(frozen=True, slots=True)
class ArchiveFetchResult:
    """Merged data plus the unaltered per-file archive documents."""

    collection: FeatureCollection
    products: tuple[ArchiveProduct, ...]
    documents: tuple[ArchiveDocument, ...]


class _DirectoryLinkParser(HTMLParser):
    """Collect anchor hrefs without executing or interpreting page content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and isinstance(value, str):
                self.hrefs.append(value)
                return


def validate_archive_date(value: str | date) -> str:
    """Return a real calendar date in the Datamart's ``YYYYMMDD`` form.

    Retention-window policy intentionally belongs to the UI.  The transport
    client only validates that the requested partition is a concrete date.
    """

    if isinstance(value, datetime):
        raise ArchiveDateValidationError(
            "The ECCC archive date must be a date without a time."
        )
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    if not isinstance(value, str) or re.fullmatch(r"\d{8}", value) is None:
        raise ArchiveDateValidationError(
            "The ECCC archive date must use YYYYMMDD."
        )
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ArchiveDateValidationError(
            "The ECCC archive date is not a valid calendar date."
        ) from exc
    return parsed.strftime("%Y%m%d")


def build_archive_directory_url(
    archive_date: str | date,
    archive_root: str = ECCC_ARCHIVE_ROOT,
) -> str:
    """Build the official date-partitioned Datamart directory URL."""

    valid_date = validate_archive_date(archive_date)
    valid_root = _validate_archive_root(archive_root)
    path = ARCHIVE_PATH_TEMPLATE.format(archive_date=valid_date)
    return f"{valid_root}{path}"


class ECCCDatamartArchiveClient:
    """Discover and aggregate one recent ECCC Datamart archive partition."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        archive_root: str = ECCC_ARCHIVE_ROOT,
        max_files: int = MAX_ARCHIVE_FILES,
        max_features: int = MAX_TOTAL_FEATURES,
    ) -> None:
        self.archive_root = _validate_archive_root(archive_root)
        self._origin = _origin(self.archive_root)
        self.max_files = _validate_limit(
            max_files,
            maximum=MAX_ARCHIVE_FILES,
            label="Archive file",
        )
        self.max_features = _validate_limit(
            max_features,
            maximum=MAX_TOTAL_FEATURES,
            label="Feature",
        )
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)

    def list_products(
        self,
        archive_date: str | date,
    ) -> tuple[ArchiveProduct, ...]:
        """List the latest official amendment of each product for a date."""

        valid_date = validate_archive_date(archive_date)
        directory_url = build_archive_directory_url(
            valid_date,
            self.archive_root,
        )
        response = self._get(directory_url)
        hrefs = _decode_directory(response, valid_date)

        latest: dict[tuple[str, str, str, int], ArchiveProduct] = {}
        for href in hrefs:
            product = _product_from_href(
                href,
                directory_url=directory_url,
                archive_date=valid_date,
                expected_origin=self._origin,
            )
            if product is None:
                continue
            logical_key = _logical_product_key(product)
            previous = latest.get(logical_key)
            if previous is None or product.version > previous.version:
                latest[logical_key] = product

        if len(latest) > self.max_files:
            raise ECCCArchiveDirectoryError(
                "The ECCC archive directory listed more than "
                f"{self.max_files} Coastal Flooding products, so retrieval "
                "was stopped."
            )
        return tuple(sorted(latest.values(), key=lambda item: item.filename))

    def discover(
        self,
        archive_date: str | date,
    ) -> tuple[ArchiveProduct, ...]:
        """Compatibility alias for :meth:`list_products`."""

        return self.list_products(archive_date)

    def fetch_products(
        self,
        products: Sequence[ArchiveProduct],
    ) -> ArchiveFetchResult:
        """Fetch selected products and retain each exact decoded document."""

        if isinstance(products, (str, bytes, bytearray)):
            raise ECCCArchiveConfigurationError(
                "Archive products must be a sequence of discovered products."
            )
        try:
            candidates = tuple(products)
        except TypeError as exc:
            raise ECCCArchiveConfigurationError(
                "Archive products must be a sequence of discovered products."
            ) from exc
        if len(candidates) > self.max_files:
            raise ECCCArchiveConfigurationError(
                "More than the configured archive file limit was selected."
            )

        latest: dict[tuple[str, str, str, int], ArchiveProduct] = {}
        for product in candidates:
            validated = self._validate_selected_product(product)
            key = _logical_product_key(validated)
            previous = latest.get(key)
            if previous is None or validated.version > previous.version:
                latest[key] = validated
        selected = tuple(
            sorted(latest.values(), key=lambda item: item.filename)
        )

        features: list[Any] = []
        documents: list[ArchiveDocument] = []
        for product in selected:
            response = self._get(product.url)
            payload = _decode_product(response, product.filename)
            product_features = payload["features"]
            if len(features) + len(product_features) > self.max_features:
                raise ECCCArchiveResponseError(
                    "The selected ECCC archive contains more than "
                    f"{self.max_features} features. Choose a different issue "
                    "date or reduce the configured archive scope."
                )
            # Keep the decoded file untouched for the raw bundle.  The merged
            # collection receives its own deep copy so later clipping or
            # filtering cannot change the retained source document.
            documents.append(ArchiveDocument(product, payload))
            features.extend(deepcopy(product_features))

        return ArchiveFetchResult(
            collection={"type": "FeatureCollection", "features": features},
            products=selected,
            documents=tuple(documents),
        )

    def fetch_date(
        self,
        archive_date: str | date,
    ) -> ArchiveFetchResult:
        """Discover and fetch all latest products for one archive date."""

        valid_date = validate_archive_date(archive_date)
        products = self.list_products(valid_date)
        if not products:
            raise ECCCArchiveDirectoryError(
                f"The ECCC archive for {valid_date} did not list any official "
                "Coastal Flooding Risk Index GeoJSON products."
            )
        return self.fetch_products(products)

    def fetch(
        self,
        archive_date: str | date,
    ) -> FeatureCollection:
        """Compatibility helper returning only the merged collection."""

        return self.fetch_date(archive_date).collection

    def _validate_selected_product(
        self,
        product: ArchiveProduct,
    ) -> ArchiveProduct:
        if not isinstance(product, ArchiveProduct):
            raise ECCCArchiveConfigurationError(
                "Only products returned by the ECCC archive discovery can be "
                "fetched."
            )
        archive_date = product.filename[:8]
        try:
            directory_url = build_archive_directory_url(
                archive_date,
                self.archive_root,
            )
        except (ArchiveDateValidationError, ValueError) as exc:
            raise ECCCArchiveConfigurationError(
                "A selected ECCC archive product has an invalid filename."
            ) from exc
        validated = _product_from_href(
            product.url,
            directory_url=directory_url,
            archive_date=archive_date,
            expected_origin=self._origin,
        )
        if validated is None or validated != product:
            raise ECCCArchiveConfigurationError(
                "A selected ECCC archive product does not match its official "
                "Datamart URL."
            )
        return validated

    def _get(self, url: str) -> requests.Response:
        try:
            response = self.session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            logger.warning(
                "ECCC archive request timed out for %s",
                url,
                exc_info=True,
            )
            raise ECCCArchiveRequestError(
                "The ECCC archive request timed out. Please try again."
            ) from exc
        except requests.ConnectionError as exc:
            logger.warning(
                "Could not connect to the ECCC archive at %s",
                url,
                exc_info=True,
            )
            raise ECCCArchiveRequestError(
                "Could not connect to the ECCC archive. Check the network "
                "connection and try again."
            ) from exc
        except requests.RequestException as exc:
            logger.warning(
                "ECCC archive request failed for %s",
                url,
                exc_info=True,
            )
            raise ECCCArchiveRequestError(
                "The ECCC archive request could not be completed. Please try "
                "again."
            ) from exc

        status_code = response.status_code
        if 200 <= status_code < 300:
            return response
        if status_code == 404:
            message = (
                "ECCC does not have the requested Coastal Flooding archive "
                "resource (HTTP 404). It may be outside the recent retention "
                "window."
            )
        elif status_code == 429:
            message = (
                "ECCC is temporarily limiting archive requests (HTTP 429). "
                "Please wait and try again."
            )
        elif 500 <= status_code < 600:
            message = (
                "The ECCC archive is temporarily unavailable "
                f"(HTTP {status_code}). Please try again."
            )
        elif 400 <= status_code < 500:
            message = (
                f"ECCC rejected the archive request (HTTP {status_code}). "
                "Check the issue date and try again."
            )
        else:
            message = (
                "ECCC returned an unexpected archive HTTP status "
                f"({status_code}). Please try again."
            )
        logger.warning(
            "ECCC archive returned HTTP %s for %s",
            status_code,
            url,
        )
        raise ECCCArchiveRequestError(message)


# Short compatibility name used by the first archive prototype.
ECCCArchiveClient = ECCCDatamartArchiveClient


def raw_bundle_bytes(
    result: ArchiveFetchResult,
    issue_date: str | date,
) -> bytes:
    """Serialize retained source documents as a strict UTF-8 JSON bundle.

    The bundle is deliberately described as raw ECCC JSON, not as GeoJSON:
    each file's complete decoded payload is nested under ``payload`` and no
    source markers are added to feature properties or geometries.
    """

    if not isinstance(result, ArchiveFetchResult):
        raise ECCCArchiveConfigurationError(
            "A valid archive fetch result is required for raw export."
        )
    valid_date = validate_archive_date(issue_date)
    files: list[dict[str, Any]] = []
    for document in result.documents:
        if not document.product.filename.startswith(valid_date):
            raise ECCCArchiveConfigurationError(
                "The raw archive bundle contains a product from a different "
                "issue date."
            )
        files.append(
            {
                "filename": document.product.filename,
                "url": document.product.url,
                "payload": json_safe(deepcopy(document.payload)),
            }
        )
    bundle = {
        "source": (
            "Environment and Climate Change Canada Datamart — "
            "Coastal Flooding Risk Index"
        ),
        "issue_date": valid_date,
        "files": files,
    }
    return json.dumps(
        bundle,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_archive_root(archive_root: str) -> str:
    if not isinstance(archive_root, str) or not archive_root.strip():
        raise ECCCArchiveConfigurationError(
            "The ECCC archive root URL is not configured."
        )
    candidate = archive_root.strip().rstrip("/")
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ECCCArchiveConfigurationError(
            "The ECCC archive root URL has an invalid port."
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ECCCArchiveConfigurationError(
            "The ECCC archive root must be an HTTPS origin without "
            "credentials, a path, a query, or a fragment."
        )
    return candidate


def _validate_limit(value: int, *, maximum: int, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ECCCArchiveConfigurationError(
            f"{label} limit must be an integer from 1 to {maximum}."
        )
    return value


def _decode_directory(
    response: requests.Response,
    archive_date: str,
) -> Sequence[str]:
    media_type = _content_type(response)
    if media_type not in HTML_MEDIA_TYPES:
        raise ECCCArchiveDirectoryError(
            f"The ECCC archive directory for {archive_date} did not return "
            "HTML."
        )
    text = response.text
    if not isinstance(text, str):
        raise ECCCArchiveDirectoryError(
            f"The ECCC archive directory for {archive_date} was unreadable."
        )
    if len(text.encode("utf-8")) > MAX_DIRECTORY_BYTES:
        raise ECCCArchiveDirectoryError(
            f"The ECCC archive directory for {archive_date} was unexpectedly "
            "large, so retrieval was stopped."
        )

    parser = _DirectoryLinkParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        logger.warning(
            "Could not parse ECCC archive directory for %s",
            archive_date,
            exc_info=True,
        )
        raise ECCCArchiveDirectoryError(
            f"The ECCC archive directory for {archive_date} could not be "
            "parsed."
        ) from exc
    return parser.hrefs


def _product_from_href(
    href: str,
    *,
    directory_url: str,
    archive_date: str,
    expected_origin: tuple[str, str, int],
) -> ArchiveProduct | None:
    candidate = href.strip()
    if not candidate or "\\" in candidate or "%" in candidate:
        return None
    try:
        href_parts = urlsplit(candidate)
    except ValueError:
        return None
    if (
        href_parts.query
        or href_parts.fragment
        or href_parts.username is not None
        or href_parts.password is not None
    ):
        return None

    filename = href_parts.path.rsplit("/", 1)[-1]
    match = _PRODUCT_FILENAME.fullmatch(filename)
    if match is None or match.group("archive_date") != archive_date:
        return None
    duration_minutes_text = match.group("duration_minutes")
    duration_minutes = (
        int(duration_minutes_text)
        if duration_minutes_text is not None
        else 0
    )
    if duration_minutes > 59:
        return None
    try:
        issue_time = datetime.strptime(
            match.group("stamp"),
            "%Y%m%dT%H%MZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    duration_hours = int(match.group("duration_hours"))
    valid_time = issue_time + timedelta(
        hours=duration_hours,
        minutes=duration_minutes,
    )

    resolved = urljoin(directory_url, candidate)
    try:
        resolved_parts = urlsplit(resolved)
        actual_origin = _origin(resolved)
    except ValueError:
        return None
    if (
        actual_origin != expected_origin
        or actual_origin[0] != "https"
        or resolved_parts.username is not None
        or resolved_parts.password is not None
        or resolved_parts.query
        or resolved_parts.fragment
    ):
        return None

    expected_path = f"{urlsplit(directory_url).path}{filename}"
    if resolved_parts.path != expected_path:
        return None

    return ArchiveProduct(
        filename=filename,
        url=resolved,
        logical_name=match.group("logical"),
        version=int(match.group("version")),
        issue_time=issue_time,
        valid_time=valid_time,
        office=match.group("office"),
        coverage=match.group("coverage"),
        lead_hours=duration_hours,
        lead_minutes=duration_minutes,
    )


def _decode_product(
    response: requests.Response,
    filename: str,
) -> FeatureCollection:
    media_type = _content_type(response)
    if media_type not in JSON_MEDIA_TYPES:
        raise ECCCArchiveResponseError(
            f"ECCC archive product {filename} did not return a supported "
            "JSON content type."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "ECCC archive product %s contained invalid JSON",
            filename,
            exc_info=True,
        )
        raise ECCCArchiveResponseError(
            f"ECCC archive product {filename} did not contain valid JSON."
        ) from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ECCCArchiveResponseError(
            f"ECCC archive product {filename} was not a GeoJSON "
            "FeatureCollection."
        )
    features = payload.get("features")
    if not isinstance(features, list):
        raise ECCCArchiveResponseError(
            f"ECCC archive product {filename} did not contain a feature list."
        )
    return payload


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _logical_product_key(
    product: ArchiveProduct,
) -> tuple[str, str, str, int]:
    """Identify a product independently of casing and duration spelling."""

    return (
        _utc_text(product.issue_time),
        product.office.casefold(),
        product.coverage.casefold(),
        product.lead_hours * 60 + product.lead_minutes,
    )
