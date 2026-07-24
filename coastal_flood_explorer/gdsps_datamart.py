"""Hardened MSC Datamart client for GDSPS NetCDF files.

The Datamart serves GDSPS output as static NetCDF under a date/cycle directory
tree.  This client crawls the live directory listing (reusing the archive's
anchor-only HTML parser), pattern-matches GDSPS NetCDF filenames, and downloads
selected files.  It is the guaranteed numerical path when WCS has no matching
coverage.  Exact filenames are discovered, never assumed: non-matching entries
are skipped the way the Coastal Flooding archive skips unrelated products.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import requests

from .api import (
    REQUEST_TIMEOUT,
    _configure_session,
    _content_type,
    _origin,
)
from .archive import _DirectoryLinkParser
from .gdsps_common import (
    GDSPS_DATAMART_PATH,
    GDSPS_DATAMART_ROOT,
    GDSPSConfigurationError,
    GDSPSDatamartFile,
    GDSPSDiscoveryError,
    GDSPSRequestError,
    GDSPSResponseError,
    GDSPSRun,
    normalize_variable,
)

logger = logging.getLogger(__name__)

HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
NETCDF_MEDIA_TYPES = frozenset(
    {
        "application/x-netcdf",
        "application/netcdf",
        "image/netcdf",
        "application/octet-stream",
    }
)
MAX_DIRECTORY_BYTES = 4_000_000
MAX_DIRECTORIES = 128
MAX_FILES = 2_000
MAX_FILE_BYTES = 128_000_000

# Best-effort MSC NetCDF naming: an issue stamp, the GDSPS model token, the
# variable, and a PT###H lead. The middle (grid/level tokens) is flexible so a
# minor naming change does not silently drop everything; unmatched files are
# simply skipped.
_NETCDF_FILENAME = re.compile(
    r"^(?P<stamp>\d{8}T\d{2}Z)_.*?GDSPS.*?_"
    r"(?P<variable>ETAS|SSH)_.*?"
    r"PT(?P<lead>\d{2,3})H\.nc$",
    re.IGNORECASE,
)


class GDSPSDatamartClient:
    """Discover and download GDSPS NetCDF files from the MSC Datamart."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        root: str = GDSPS_DATAMART_ROOT,
        base_path: str = GDSPS_DATAMART_PATH,
        max_directories: int = MAX_DIRECTORIES,
        max_files: int = MAX_FILES,
    ) -> None:
        self.root = _validate_root(root)
        self._origin = _origin(self.root)
        self.base_path = _validate_base_path(base_path)
        self.max_directories = _validate_limit(
            max_directories, maximum=MAX_DIRECTORIES, label="Directory"
        )
        self.max_files = _validate_limit(
            max_files, maximum=MAX_FILES, label="File"
        )
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)

    def discover_files(
        self,
        *,
        variable: str | None = None,
    ) -> tuple[GDSPSDatamartFile, ...]:
        """Crawl the GDSPS Datamart tree for matching NetCDF files.

        The crawl is breadth-first and bounded by ``max_directories`` and
        ``max_files``; only same-origin subdirectories under the base path are
        followed, and non-NetCDF or non-GDSPS entries are skipped.
        """

        wanted = normalize_variable(variable) if variable is not None else None
        if variable is not None and wanted is None:
            raise GDSPSConfigurationError(
                "The GDSPS variable filter must be ETAS or SSH."
            )

        start_url = f"{self.root}{self.base_path}"
        queue: deque[str] = deque([start_url])
        visited: set[str] = set()
        files: dict[str, GDSPSDatamartFile] = {}
        directories_seen = 0

        while queue and directories_seen < self.max_directories:
            directory_url = queue.popleft()
            if directory_url in visited:
                continue
            visited.add(directory_url)
            directories_seen += 1

            hrefs = self._list_directory(directory_url)
            for href in hrefs:
                resolved = _safe_child_url(
                    href,
                    directory_url=directory_url,
                    expected_origin=self._origin,
                    base_path=self.base_path,
                )
                if resolved is None:
                    continue
                if resolved.endswith("/"):
                    if resolved not in visited:
                        queue.append(resolved)
                    continue
                parsed_file = _file_from_url(resolved, self.root)
                if parsed_file is None:
                    continue
                if wanted is not None and parsed_file.variable != wanted:
                    continue
                files[parsed_file.url] = parsed_file
                if len(files) >= self.max_files:
                    return _sorted_files(files.values())
        return _sorted_files(files.values())

    def list_runs(self, *, variable: str | None = None) -> tuple[GDSPSRun, ...]:
        """Return the distinct model runs discovered on the Datamart."""

        runs = {
            file.run.stamp: file.run
            for file in self.discover_files(variable=variable)
        }
        return tuple(
            sorted(runs.values(), key=lambda run: run.issue_time, reverse=True)
        )

    def fetch_file(self, file: GDSPSDatamartFile) -> bytes:
        """Download one discovered NetCDF file's bytes."""

        if not isinstance(file, GDSPSDatamartFile):
            raise GDSPSConfigurationError(
                "A discovered GDSPS Datamart file is required for download."
            )
        response = self._get(file.url)
        media_type = _content_type(response)
        if media_type is not None and media_type in HTML_MEDIA_TYPES:
            raise GDSPSResponseError(
                f"The Datamart returned HTML instead of NetCDF for "
                f"{file.filename}."
            )
        content = response.content
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise GDSPSResponseError(
                f"The Datamart returned no data for {file.filename}."
            )
        if len(content) > MAX_FILE_BYTES:
            raise GDSPSResponseError(
                f"The Datamart file {file.filename} was unexpectedly large, so "
                "retrieval was stopped."
            )
        if media_type is not None and media_type not in NETCDF_MEDIA_TYPES:
            raise GDSPSResponseError(
                f"The Datamart returned an unsupported content type for "
                f"{file.filename}."
            )
        return bytes(content)

    def _list_directory(self, url: str) -> list[str]:
        response = self._get(url)
        media_type = _content_type(response)
        if media_type not in HTML_MEDIA_TYPES:
            raise GDSPSDiscoveryError(
                "The GDSPS Datamart directory did not return HTML."
            )
        text = response.text
        if not isinstance(text, str):
            raise GDSPSDiscoveryError(
                "The GDSPS Datamart directory was unreadable."
            )
        if len(text.encode("utf-8")) > MAX_DIRECTORY_BYTES:
            raise GDSPSDiscoveryError(
                "A GDSPS Datamart directory was unexpectedly large, so "
                "discovery was stopped."
            )
        parser = _DirectoryLinkParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception as exc:  # noqa: BLE001 - malformed listing is fatal.
            logger.warning("Could not parse GDSPS Datamart directory", exc_info=True)
            raise GDSPSDiscoveryError(
                "A GDSPS Datamart directory could not be parsed."
            ) from exc
        return parser.hrefs

    def _get(self, url: str) -> requests.Response:
        try:
            response = self.session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            logger.warning("GDSPS Datamart request timed out for %s", url, exc_info=True)
            raise GDSPSRequestError(
                "The GDSPS Datamart request timed out. Please try again."
            ) from exc
        except requests.ConnectionError as exc:
            logger.warning("Could not connect to GDSPS Datamart at %s", url, exc_info=True)
            raise GDSPSRequestError(
                "Could not connect to the GDSPS Datamart. Check the network "
                "connection and try again."
            ) from exc
        except requests.RequestException as exc:
            logger.warning("GDSPS Datamart request failed for %s", url, exc_info=True)
            raise GDSPSRequestError(
                "The GDSPS Datamart request could not be completed. Please try "
                "again."
            ) from exc

        if 200 <= response.status_code < 300:
            return response
        raise GDSPSRequestError(_status_message(response.status_code))


def _file_from_url(url: str, root: str) -> GDSPSDatamartFile | None:
    filename = urlsplit(url).path.rsplit("/", 1)[-1]
    match = _NETCDF_FILENAME.fullmatch(filename)
    if match is None:
        return None
    try:
        issue_time = datetime.strptime(match.group("stamp"), "%Y%m%dT%HZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    variable = normalize_variable(match.group("variable"))
    if variable is None:
        return None
    lead_hours = int(match.group("lead"))
    valid_time = issue_time + timedelta(hours=lead_hours)
    run = GDSPSRun(issue_time=issue_time, cycle=f"{issue_time.hour:02d}")
    return GDSPSDatamartFile(
        filename=filename,
        url=url,
        variable=variable,
        run=run,
        lead_hours=lead_hours,
        valid_time=valid_time,
    )


def _safe_child_url(
    href: str,
    *,
    directory_url: str,
    expected_origin: tuple[str, str, int],
    base_path: str,
) -> str | None:
    candidate = href.strip()
    if not candidate or candidate.startswith(("?", "#", "..")) or "\\" in candidate:
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
        or not resolved_parts.path.startswith(base_path)
    ):
        return None
    return resolved


def _sorted_files(
    files,
) -> tuple[GDSPSDatamartFile, ...]:
    return tuple(
        sorted(
            files,
            key=lambda file: (
                file.run.issue_time,
                file.variable,
                file.lead_hours,
                file.filename,
            ),
        )
    )


def _validate_root(root: str) -> str:
    if not isinstance(root, str) or not root.strip():
        raise GDSPSConfigurationError("The GDSPS Datamart root is not configured.")
    candidate = root.strip().rstrip("/")
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GDSPSConfigurationError(
            "The GDSPS Datamart root has an invalid port."
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
        raise GDSPSConfigurationError(
            "The GDSPS Datamart root must be an HTTPS origin without "
            "credentials, a path, a query, or a fragment."
        )
    return candidate


def _validate_base_path(base_path: str) -> str:
    if not isinstance(base_path, str) or not base_path.startswith("/"):
        raise GDSPSConfigurationError(
            "The GDSPS Datamart base path must start with '/'."
        )
    normalized = base_path if base_path.endswith("/") else f"{base_path}/"
    if "//" in normalized[1:] or ".." in normalized:
        raise GDSPSConfigurationError("The GDSPS Datamart base path is invalid.")
    return normalized


def _validate_limit(value: int, *, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise GDSPSConfigurationError(
            f"{label} limit must be an integer from 1 to {maximum}."
        )
    return value


def _status_message(status_code: int) -> str:
    if status_code == 404:
        return (
            "The GDSPS Datamart does not have the requested resource "
            "(HTTP 404). It may be outside the retention window."
        )
    if status_code == 429:
        return (
            "The GDSPS Datamart is temporarily limiting requests (HTTP 429). "
            "Please wait and try again."
        )
    if 500 <= status_code < 600:
        return (
            f"The GDSPS Datamart is temporarily unavailable (HTTP "
            f"{status_code}). Please try again."
        )
    if 400 <= status_code < 500:
        return (
            f"The GDSPS Datamart rejected the request (HTTP {status_code}). "
            "Please try again."
        )
    return (
        f"The GDSPS Datamart returned an unexpected HTTP status "
        f"({status_code}). Please try again."
    )
