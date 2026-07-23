"""HTTP client for ECCC's Coastal Flooding Risk Index collection."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BBox: TypeAlias = tuple[float, float, float, float]
FeatureCollection: TypeAlias = dict[str, Any]

ECCC_API_URL = (
    "https://api.weather.gc.ca/collections/"
    "coastal_flood_risk_index/items"
)
DEFAULT_API_URL = ECCC_API_URL
API_URL = ECCC_API_URL

USER_AGENT = (
    "geo-stream/0.1 "
    "(+https://github.com/cameroneevenson-lgtm/geo_stream)"
)
REQUEST_TIMEOUT = (5.0, 30.0)
RETRY_COUNT = 4
RETRY_BACKOFF_FACTOR = 0.5
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_PAGES = 100
PAGE_LIMIT = 10_000
MAX_TOTAL_FEATURES = 50_000
JSON_MEDIA_TYPES = frozenset({"application/json", "application/geo+json"})


class ECCCError(RuntimeError):
    """Base class for errors whose messages are safe to show to users."""


class BBoxValidationError(ECCCError, ValueError):
    """Raised when a bounding box is not valid ordered CRS84 coordinates."""


class ECCCConfigurationError(ECCCError, ValueError):
    """Raised when the API client is configured unsafely or incorrectly."""


class ECCCRequestError(ECCCError):
    """Raised when ECCC cannot successfully complete an HTTP request."""


class ECCCResponseError(ECCCError):
    """Raised when an ECCC response is not valid GeoJSON."""


class ECCCPaginationError(ECCCError):
    """Raised when pagination is unsafe, cyclic, or unreasonably long."""


def validate_bbox(bbox: Sequence[float]) -> BBox:
    """Validate and return an ordered WGS84/CRS84 bounding box.

    Coordinates are returned as ``(min_lon, min_lat, max_lon, max_lat)``.
    Antimeridian-spanning boxes are not accepted because the GeoMet request
    contract used by this application requires ordered bounds.
    """

    if isinstance(bbox, (str, bytes)):
        raise BBoxValidationError(
            "The selected region does not contain four numeric coordinates."
        )

    try:
        values = tuple(bbox)
    except TypeError as exc:
        raise BBoxValidationError(
            "The selected region does not contain four numeric coordinates."
        ) from exc

    if len(values) != 4:
        raise BBoxValidationError(
            "The selected region does not contain four numeric coordinates."
        )

    if any(isinstance(value, bool) for value in values):
        raise BBoxValidationError(
            "The selected region contains a non-numeric coordinate."
        )

    try:
        min_lon, min_lat, max_lon, max_lat = (
            float(value) for value in values
        )
    except (TypeError, ValueError) as exc:
        raise BBoxValidationError(
            "The selected region contains a non-numeric coordinate."
        ) from exc

    coordinates = (min_lon, min_lat, max_lon, max_lat)
    if not all(math.isfinite(value) for value in coordinates):
        raise BBoxValidationError(
            "The selected region contains a non-finite coordinate."
        )
    if not -180.0 <= min_lon <= 180.0 or not -180.0 <= max_lon <= 180.0:
        raise BBoxValidationError(
            "The selected region's longitudes must be between -180 and 180."
        )
    if not -90.0 <= min_lat <= 90.0 or not -90.0 <= max_lat <= 90.0:
        raise BBoxValidationError(
            "The selected region's latitudes must be between -90 and 90."
        )
    if min_lon >= max_lon or min_lat >= max_lat:
        raise BBoxValidationError(
            "The selected region must have ordered, non-zero bounds."
        )

    return coordinates


def build_retry_session() -> requests.Session:
    """Create a session configured for bounded, GET-only transient retries."""

    session = requests.Session()
    _configure_session(session)
    return session


def _configure_session(session: requests.Session) -> None:
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=RETRY_COUNT,
        connect=RETRY_COUNT,
        read=RETRY_COUNT,
        status=RETRY_COUNT,
        other=0,
        allowed_methods=frozenset({"GET"}),
        status_forcelist=RETRY_STATUS_CODES,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


class ECCCClient:
    """Fetch and aggregate Coastal Flooding Risk Index GeoJSON pages."""

    def __init__(
        self,
        api_url: str = ECCC_API_URL,
        *,
        session: requests.Session | None = None,
        max_pages: int = MAX_PAGES,
        max_features: int = MAX_TOTAL_FEATURES,
    ) -> None:
        self.api_url = _validate_api_url(api_url)
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= MAX_PAGES
        ):
            raise ECCCConfigurationError(
                f"Page limit must be an integer from 1 to {MAX_PAGES}."
            )
        self.max_pages = max_pages
        if (
            isinstance(max_features, bool)
            or not isinstance(max_features, int)
            or not 1 <= max_features <= MAX_TOTAL_FEATURES
        ):
            raise ECCCConfigurationError(
                "Feature limit must be an integer from 1 to "
                f"{MAX_TOTAL_FEATURES}."
            )
        self.max_features = max_features
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)
        self._origin = _origin(self.api_url)

    def fetch(
        self,
        bbox: Sequence[float],
        language: str = "en",
    ) -> FeatureCollection:
        """Fetch every matching page for a validated CRS84 bounding box."""

        valid_bbox = validate_bbox(bbox)
        valid_language = _validate_language(language)
        initial_params = {
            "f": "json",
            "bbox": ",".join(_format_coordinate(value) for value in valid_bbox),
            "limit": PAGE_LIMIT,
            "lang": valid_language,
        }

        request_url = self.api_url
        request_params: dict[str, str | int] | None = initial_params
        effective_url = _url_with_params(request_url, initial_params)
        visited: set[str] = set()
        features: list[Any] = []
        page_count = 0

        while True:
            canonical_url = _canonical_url(effective_url)
            if canonical_url in visited:
                raise ECCCPaginationError(
                    "ECCC returned a repeating pagination link, so retrieval "
                    "was stopped."
                )
            visited.add(canonical_url)
            page_count += 1

            response = self._get(request_url, request_params)
            payload = _decode_feature_collection(response, page_count)
            page_features = payload["features"]
            if len(features) + len(page_features) > self.max_features:
                raise ECCCResponseError(
                    "ECCC returned more than "
                    f"{self.max_features} features for one request. "
                    "Draw a smaller region and try again."
                )
            features.extend(page_features)

            next_href = _next_href(payload, page_count)
            if next_href is None:
                break
            if page_count >= self.max_pages:
                raise ECCCPaginationError(
                    f"ECCC returned more than {self.max_pages} pages, so "
                    "retrieval was stopped."
                )

            next_url = urljoin(effective_url, next_href)
            _validate_pagination_url(next_url, self._origin)
            next_canonical = _canonical_url(next_url)
            if next_canonical in visited:
                raise ECCCPaginationError(
                    "ECCC returned a repeating pagination link, so retrieval "
                    "was stopped."
                )

            request_url = next_url
            request_params = None
            effective_url = next_url

        return {"type": "FeatureCollection", "features": features}

    def _get(
        self,
        url: str,
        params: Mapping[str, str | int] | None,
    ) -> requests.Response:
        kwargs: dict[str, Any] = {
            "timeout": REQUEST_TIMEOUT,
            "allow_redirects": False,
        }
        if params is not None:
            kwargs["params"] = dict(params)

        try:
            response = self.session.get(url, **kwargs)
        except requests.Timeout as exc:
            logger.warning("ECCC request timed out for %s", url, exc_info=True)
            raise ECCCRequestError(
                "The ECCC request timed out. Please try again."
            ) from exc
        except requests.ConnectionError as exc:
            logger.warning(
                "Could not connect to ECCC at %s", url, exc_info=True
            )
            raise ECCCRequestError(
                "Could not connect to the ECCC service. Check the network "
                "connection and try again."
            ) from exc
        except requests.RequestException as exc:
            logger.warning("ECCC request failed for %s", url, exc_info=True)
            raise ECCCRequestError(
                "The ECCC request could not be completed. Please try again."
            ) from exc

        status_code = response.status_code
        if 200 <= status_code < 300:
            return response
        if status_code == 429:
            message = (
                "ECCC is temporarily limiting requests (HTTP 429). Please "
                "wait and try again."
            )
        elif 500 <= status_code < 600:
            message = (
                f"The ECCC service is temporarily unavailable (HTTP "
                f"{status_code}). Please try again."
            )
        elif 400 <= status_code < 500:
            message = (
                f"ECCC rejected the request (HTTP {status_code}). Check the "
                "selected region and try again."
            )
        else:
            message = (
                f"ECCC returned an unexpected HTTP status ({status_code}). "
                "Please try again."
            )
        logger.warning("ECCC returned HTTP %s for %s", status_code, url)
        raise ECCCRequestError(message)


def _validate_api_url(api_url: str) -> str:
    if not isinstance(api_url, str) or not api_url.strip():
        raise ECCCConfigurationError("The ECCC API URL is not configured.")
    candidate = api_url.strip()
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ECCCConfigurationError(
            "The ECCC API URL has an invalid port."
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ECCCConfigurationError(
            "The ECCC API URL must be a valid HTTPS URL without credentials."
        )
    return candidate


def _validate_language(language: str) -> str:
    if not isinstance(language, str):
        raise ECCCConfigurationError("The ECCC language must be text.")
    normalized = language.strip().lower()
    if normalized not in {"en", "fr"}:
        raise ECCCConfigurationError(
            "The ECCC language must be either 'en' or 'fr'."
        )
    return normalized


def _format_coordinate(value: float) -> str:
    # Fifteen significant digits preserve normal map-drawing precision without
    # emitting needlessly long binary floating-point tails.
    return format(value, ".15g")


def _url_with_params(
    url: str,
    params: Mapping[str, str | int],
) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, str(value)) for key, value in params.items())
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            "",
        )
    )


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    query = sorted(parse_qsl(parsed.query, keep_blank_values=True))
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = hostname if port in (None, default_port) else f"{hostname}:{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            authority,
            parsed.path or "/",
            urlencode(query),
            "",
        )
    )


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


def _validate_pagination_url(
    url: str,
    expected_origin: tuple[str, str, int],
) -> None:
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ECCCPaginationError(
            "ECCC returned an unsafe pagination link, so retrieval was "
            "stopped."
        )
    try:
        actual_origin = _origin(url)
    except ValueError as exc:
        raise ECCCPaginationError(
            "ECCC returned an invalid pagination link, so retrieval was "
            "stopped."
        ) from exc
    if actual_origin != expected_origin or actual_origin[0] != "https":
        raise ECCCPaginationError(
            "ECCC returned a pagination link outside its configured HTTPS "
            "service, so retrieval was stopped."
        )


def _content_type(response: requests.Response) -> str | None:
    headers = response.headers
    value = headers.get("Content-Type")
    if value is None:
        for key, candidate in headers.items():
            if str(key).lower() == "content-type":
                value = candidate
                break
    if not isinstance(value, str):
        return None
    return value.split(";", 1)[0].strip().lower()


def _decode_feature_collection(
    response: requests.Response,
    page_number: int,
) -> FeatureCollection:
    media_type = _content_type(response)
    if media_type not in JSON_MEDIA_TYPES:
        raise ECCCResponseError(
            f"ECCC page {page_number} did not return a supported JSON "
            "content type."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "ECCC page %s contained invalid JSON", page_number, exc_info=True
        )
        raise ECCCResponseError(
            f"ECCC page {page_number} did not contain valid JSON."
        ) from exc

    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ECCCResponseError(
            f"ECCC page {page_number} was not a GeoJSON FeatureCollection."
        )
    page_features = payload.get("features")
    if not isinstance(page_features, list):
        raise ECCCResponseError(
            f"ECCC page {page_number} did not contain a feature list."
        )
    return payload


def _next_href(payload: FeatureCollection, page_number: int) -> str | None:
    links = payload.get("links")
    if links is None:
        return None
    if not isinstance(links, list):
        raise ECCCResponseError(
            f"ECCC page {page_number} contained an invalid links list."
        )

    for link in links:
        if not isinstance(link, dict):
            raise ECCCResponseError(
                f"ECCC page {page_number} contained an invalid pagination "
                "link."
            )
        relation = link.get("rel")
        relations = relation if isinstance(relation, list) else [relation]
        if any(
            isinstance(item, str) and item.lower() == "next"
            for item in relations
        ):
            href = link.get("href")
            if not isinstance(href, str) or not href.strip():
                raise ECCCResponseError(
                    f"ECCC page {page_number} contained a next link without "
                    "a URL."
                )
            return href.strip()
    return None
