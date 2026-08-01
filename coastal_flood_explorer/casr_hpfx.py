"""Hardened HPFX client for CaSR-Rivers per-subbasin NetCDF files.

The collaborative host publishes Apache directory listings and static NetCDF.
This client is HTTPS-only, follows no redirects, allowlists same-directory
filenames, and enforces response media-type and size ceilings - the same
spirit as ``archive.py`` / ``gdsps_datamart.py``.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

import requests

from .api import REQUEST_TIMEOUT, _configure_session, _content_type, _origin
from .archive import _DirectoryLinkParser
from .casr_common import (
    CASR_HPFX_ROOT,
    CASR_RIVERS_BASE_PATH,
    CASRConfigurationError,
    CASRDataUnavailableError,
    CASRRequestError,
    CASRResponseError,
    CASRRiversFile,
    normalize_variable,
    parse_month_token,
    parse_rivers_filename,
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
MAX_FILE_BYTES = 40_000_000
MAX_MONTHS = 800
MAX_FILES_PER_MONTH = 8_000


class CASRHpfxClient:
    """Discover and download CaSR-Rivers per-subbasin NetCDF from HPFX."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        root: str = CASR_HPFX_ROOT,
        base_path: str = CASR_RIVERS_BASE_PATH,
    ) -> None:
        self.root = _validate_root(root)
        self._origin = _origin(self.root)
        self.base_path = _validate_base_path(base_path)
        self.session = session if session is not None else requests.Session()
        _configure_session(self.session)

    def list_months(self) -> tuple[str, ...]:
        """Return available ``YYYYMM`` month directories under per_subbasin."""

        url = f"{self.root}{self.base_path}"
        hrefs = self._list_directory(url)
        months: list[str] = []
        for href in hrefs:
            name = href.rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"\d{6}", name):
                months.append(name)
            if len(months) >= MAX_MONTHS:
                break
        return tuple(sorted(months))

    def list_files(
        self,
        year_month: str,
        *,
        variable: str | None = None,
    ) -> tuple[CASRRiversFile, ...]:
        """List NetCDF files for one month, optionally filtered by variable."""

        month = parse_month_token(year_month)
        wanted = normalize_variable(variable) if variable is not None else None
        if variable is not None and wanted is None:
            raise CASRConfigurationError(
                "CaSR-Rivers variable must be RiverDischarge, "
                "RiverChannelStorage, or DeepReservoirStorage."
            )

        url = f"{self.root}{self.base_path}{month}/"
        hrefs = self._list_directory(url)
        files: list[CASRRiversFile] = []
        for href in hrefs:
            name = href.rsplit("/", 1)[-1]
            parsed = parse_rivers_filename(name)
            if parsed is None:
                continue
            if parsed.year_month != month:
                continue
            if wanted is not None and parsed.variable != wanted:
                continue
            file_url = urljoin(url, name)
            if urlsplit(file_url).path.rstrip("/").count("/") < 3:
                continue
            files.append(
                CASRRiversFile(
                    year_month=parsed.year_month,
                    subbasin_id=parsed.subbasin_id,
                    variable=parsed.variable,
                    filename=parsed.filename,
                    url=file_url,
                )
            )
            if len(files) >= MAX_FILES_PER_MONTH:
                break
        if not files:
            raise CASRDataUnavailableError(
                f"No CaSR-Rivers per-subbasin files were found for {month}."
            )
        return tuple(files)

    def download(self, file: CASRRiversFile | str) -> bytes:
        """Download one NetCDF file by descriptor or absolute HTTPS URL."""

        if isinstance(file, CASRRiversFile):
            url = file.url
        elif isinstance(file, str) and file.startswith("https://"):
            url = file
        else:
            raise CASRConfigurationError(
                "A CaSR-Rivers file descriptor or HTTPS URL is required."
            )
        self._assert_same_origin(url)
        try:
            # Per-subbasin discharge files are ~10-30 MB; allow a longer read.
            response = self.session.get(
                url,
                timeout=(5.0, 120.0),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            logger.warning("CaSR HPFX download failed", exc_info=True)
            raise CASRRequestError(
                "CaSR-Rivers data could not be downloaded from HPFX."
            ) from exc
        if response.status_code != 200:
            raise CASRRequestError(
                "CaSR-Rivers data could not be downloaded from HPFX."
            )
        media = _content_type(response)
        if media not in NETCDF_MEDIA_TYPES:
            raise CASRResponseError(
                "The CaSR-Rivers response was not a NetCDF payload."
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise CASRResponseError(
                    "The CaSR-Rivers NetCDF exceeded the download size limit."
                )
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise CASRResponseError("The CaSR-Rivers NetCDF payload was empty.")
        return data

    def _list_directory(self, url: str) -> tuple[str, ...]:
        self._assert_same_origin(url)
        try:
            response = self.session.get(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.warning("CaSR HPFX listing failed", exc_info=True)
            raise CASRRequestError(
                "CaSR-Rivers directory listing could not be retrieved."
            ) from exc
        if response.status_code != 200:
            raise CASRRequestError(
                "CaSR-Rivers directory listing could not be retrieved."
            )
        media = _content_type(response)
        if media not in HTML_MEDIA_TYPES:
            raise CASRResponseError(
                "The CaSR-Rivers directory response was not HTML."
            )
        if len(response.content) > MAX_DIRECTORY_BYTES:
            raise CASRResponseError(
                "The CaSR-Rivers directory listing exceeded the size limit."
            )
        parser = _DirectoryLinkParser()
        parser.feed(response.text)
        parser.close()
        return tuple(parser.hrefs)

    def _assert_same_origin(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise CASRConfigurationError("CaSR HPFX URLs must use HTTPS.")
        if _origin(url) != self._origin:
            raise CASRConfigurationError(
                "CaSR HPFX URLs must stay on the configured host."
            )


def _validate_root(root: str) -> str:
    if not isinstance(root, str) or not root.startswith("https://"):
        raise CASRConfigurationError("CaSR HPFX root must be an HTTPS URL.")
    parts = urlsplit(root.rstrip("/"))
    if parts.path not in {"", "/"}:
        raise CASRConfigurationError(
            "CaSR HPFX root must not include a path prefix."
        )
    return f"{parts.scheme}://{parts.netloc}"


def _validate_base_path(base_path: str) -> str:
    if not isinstance(base_path, str) or not base_path.startswith("/"):
        raise CASRConfigurationError(
            "CaSR HPFX base path must be an absolute path."
        )
    if ".." in base_path or not base_path.endswith("/"):
        raise CASRConfigurationError(
            "CaSR HPFX base path must be a trailing-slash directory path."
        )
    return base_path
