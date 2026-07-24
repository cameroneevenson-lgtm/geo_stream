"""Unit tests for the GDSPS GeoMet WCS client."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

import pytest

from coastal_flood_explorer.gdsps_common import (
    ETAS,
    SSH,
    GDSPSConfigurationError,
    GDSPSCoverageInfo,
    GDSPSDataUnavailableError,
    GDSPSRequestError,
    GDSPSResponseError,
)
from coastal_flood_explorer.gdsps_wcs import (
    GDSPSWCSClient,
    find_coverage_for_variable,
)

WCS_NS = (
    'xmlns:wcs="http://www.opengis.net/wcs/2.0" '
    'xmlns:ows="http://www.opengis.net/ows/2.0"'
)


def coverage_summary(coverage_id: str, title: str) -> str:
    return (
        "<wcs:CoverageSummary>"
        f"<ows:Title>{title}</ows:Title>"
        f"<wcs:CoverageId>{coverage_id}</wcs:CoverageId>"
        "</wcs:CoverageSummary>"
    )


def capabilities(*summaries: str) -> str:
    inner = "".join(summaries)
    return (
        f'<?xml version="1.0"?><wcs:Capabilities {WCS_NS} version="2.0.1">'
        f"<wcs:Contents>{inner}</wcs:Contents></wcs:Capabilities>"
    )


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        content: bytes | None = None,
        status_code: int = 200,
        content_type: str = "text/xml",
    ) -> None:
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class FakeSession:
    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}
        self.mounts: dict[str, Any] = {}

    def mount(self, prefix: str, adapter: Any) -> None:
        self.mounts[prefix] = adapter

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        result = self.responses.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def test_discovers_gdsps_coverages() -> None:
    document = capabilities(
        coverage_summary("GDSPS.ETAS", "Storm surge elevation"),
        coverage_summary("GDSPS.SSH", "Total water level"),
        coverage_summary("GDPS.TT", "Air temperature"),
    )
    client = GDSPSWCSClient(session=FakeSession(FakeResponse(text=document)))
    coverages = client.discover_coverages()
    assert [c.coverage_id for c in coverages] == ["GDSPS.ETAS", "GDSPS.SSH"]
    assert coverages[0].variable == ETAS
    assert coverages[1].variable == SSH


def test_find_coverage_for_variable_falls_back_when_missing() -> None:
    coverages = (
        GDSPSCoverageInfo(coverage_id="GDSPS.ETAS", title="t", variable=ETAS),
    )
    assert find_coverage_for_variable(coverages, "etas").coverage_id == "GDSPS.ETAS"
    with pytest.raises(GDSPSDataUnavailableError):
        find_coverage_for_variable(coverages, "ssh")


def test_fetch_coverage_builds_subset_and_returns_bytes() -> None:
    netcdf = b"CDF\x01payload"
    session = FakeSession(
        FakeResponse(content=netcdf, content_type="application/x-netcdf")
    )
    client = GDSPSWCSClient(session=session)
    coverage = GDSPSCoverageInfo(
        coverage_id="GDSPS.ETAS", title="t", variable=ETAS
    )
    data = client.fetch_coverage(
        coverage,
        bbox=(-63.0, 44.0, -62.0, 45.0),
        time=datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
    )
    assert data == netcdf
    (_, kwargs) = session.calls[0]
    subsets = kwargs["params"]["subset"]
    assert "Long(-63.0,-62.0)" in subsets
    assert "Lat(44.0,45.0)" in subsets
    assert any(s.startswith("time(") for s in subsets)
    assert kwargs["params"]["request"] == "GetCoverage"
    assert kwargs["allow_redirects"] is False


def test_fetch_coverage_detects_exception_report() -> None:
    report = (
        '<?xml version="1.0"?><ows:ExceptionReport '
        'xmlns:ows="http://www.opengis.net/ows/2.0">'
        '<ows:Exception exceptionCode="NoSuchCoverage">'
        "<ows:ExceptionText>NotFound</ows:ExceptionText>"
        "</ows:Exception></ows:ExceptionReport>"
    )
    session = FakeSession(FakeResponse(text=report, content_type="text/xml"))
    client = GDSPSWCSClient(session=session)
    coverage = GDSPSCoverageInfo(
        coverage_id="GDSPS.ETAS", title="t", variable=ETAS
    )
    with pytest.raises(GDSPSDataUnavailableError):
        client.fetch_coverage(coverage, bbox=(-63.0, 44.0, -62.0, 45.0))


def test_fetch_coverage_rejects_bad_bbox() -> None:
    client = GDSPSWCSClient(session=FakeSession())
    coverage = GDSPSCoverageInfo(
        coverage_id="GDSPS.ETAS", title="t", variable=ETAS
    )
    with pytest.raises(GDSPSConfigurationError):
        client.fetch_coverage(coverage, bbox=(1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_http_error_raises_request_error() -> None:
    client = GDSPSWCSClient(session=FakeSession(FakeResponse(status_code=500)))
    with pytest.raises(GDSPSRequestError):
        client.discover_coverages()


def test_unsupported_coverage_media_type_rejected() -> None:
    session = FakeSession(
        FakeResponse(content=b"junk", content_type="text/plain")
    )
    client = GDSPSWCSClient(session=session)
    coverage = GDSPSCoverageInfo(
        coverage_id="GDSPS.ETAS", title="t", variable=ETAS
    )
    with pytest.raises(GDSPSResponseError):
        client.fetch_coverage(coverage, bbox=(-63.0, 44.0, -62.0, 45.0))
