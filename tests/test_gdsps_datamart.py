"""Unit tests for the GDSPS MSC Datamart client."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

import pytest

from coastal_flood_explorer.gdsps_common import (
    ETAS,
    SSH,
    GDSPSConfigurationError,
    GDSPSDatamartFile,
    GDSPSRequestError,
    GDSPSResponseError,
    GDSPSRun,
)
from coastal_flood_explorer.gdsps_datamart import GDSPSDatamartClient

ROOT = "https://dd.weather.gc.ca"
BASE = "/model_gdsps/"


def directory_html(*hrefs: str) -> str:
    links = "".join(f'<a href="{href}">link</a>' for href in hrefs)
    return f"<!doctype html><html><body>{links}</body></html>"


def etas_name(stamp: str = "20260722T00Z", lead: str = "003") -> str:
    return f"{stamp}_MSC_GDSPS_ETAS_LatLon0.083_PT{lead}H.nc"


def ssh_name(stamp: str = "20260722T00Z", lead: str = "003") -> str:
    return f"{stamp}_MSC_GDSPS_SSH_LatLon0.083_PT{lead}H.nc"


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        content: bytes | None = None,
        status_code: int = 200,
        content_type: str = "text/html",
    ) -> None:
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class FakeSession:
    def __init__(self) -> None:
        self.routes: dict[str, FakeResponse | BaseException] = {}
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}
        self.mounts: dict[str, Any] = {}

    def route(self, url: str, response: FakeResponse | BaseException) -> None:
        self.routes[url] = response

    def mount(self, prefix: str, adapter: Any) -> None:
        self.mounts[prefix] = adapter

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        result = self.routes.get(url)
        if result is None:
            raise AssertionError(f"Unexpected mocked request for {url}")
        if isinstance(result, BaseException):
            raise result
        return result


def test_crawls_tree_and_parses_files() -> None:
    session = FakeSession()
    session.route(
        f"{ROOT}{BASE}",
        FakeResponse(text=directory_html("00/", "12/", "../", "readme.txt")),
    )
    session.route(
        f"{ROOT}{BASE}00/",
        FakeResponse(text=directory_html(etas_name(), ssh_name(), "notes.txt")),
    )
    session.route(
        f"{ROOT}{BASE}12/",
        FakeResponse(text=directory_html(etas_name("20260722T12Z"))),
    )
    client = GDSPSDatamartClient(session=session)
    files = client.discover_files()

    names = [file.filename for file in files]
    assert etas_name() in names
    assert ssh_name() in names
    assert etas_name("20260722T12Z") in names
    # 'readme.txt'/'notes.txt' and the '../' parent link are ignored.
    assert "readme.txt" not in names
    etas_file = next(f for f in files if f.filename == etas_name())
    assert etas_file.variable == ETAS
    assert etas_file.lead_hours == 3
    assert etas_file.run.cycle == "00"
    assert etas_file.valid_time == datetime(
        2026, 7, 22, 3, tzinfo=timezone.utc
    )


def test_variable_filter_limits_results() -> None:
    session = FakeSession()
    session.route(
        f"{ROOT}{BASE}",
        FakeResponse(text=directory_html(etas_name(), ssh_name())),
    )
    client = GDSPSDatamartClient(session=session)
    ssh_files = client.discover_files(variable="ssh")
    assert [f.variable for f in ssh_files] == [SSH]


def test_list_runs_deduplicates_and_sorts() -> None:
    session = FakeSession()
    session.route(
        f"{ROOT}{BASE}",
        FakeResponse(
            text=directory_html(
                etas_name("20260722T00Z", "003"),
                etas_name("20260722T00Z", "006"),
                etas_name("20260722T12Z", "003"),
            )
        ),
    )
    client = GDSPSDatamartClient(session=session)
    runs = client.list_runs()
    assert [run.stamp for run in runs] == ["20260722T12Z", "20260722T00Z"]


def test_cross_origin_href_is_ignored() -> None:
    session = FakeSession()
    session.route(
        f"{ROOT}{BASE}",
        FakeResponse(
            text=directory_html(
                "https://evil.example/model_gdsps/x/",
                etas_name(),
            )
        ),
    )
    client = GDSPSDatamartClient(session=session)
    files = client.discover_files()
    assert [f.filename for f in files] == [etas_name()]
    # The evil host was never requested.
    assert all("evil.example" not in url for url in session.calls)


def test_fetch_file_returns_bytes() -> None:
    session = FakeSession()
    url = f"{ROOT}{BASE}00/{etas_name()}"
    session.route(
        url,
        FakeResponse(content=b"CDF\x01data", content_type="application/x-netcdf"),
    )
    client = GDSPSDatamartClient(session=session)
    file = GDSPSDatamartFile(
        filename=etas_name(),
        url=url,
        variable=ETAS,
        run=GDSPSRun(datetime(2026, 7, 22, tzinfo=timezone.utc), "00"),
        lead_hours=3,
        valid_time=datetime(2026, 7, 22, 3, tzinfo=timezone.utc),
    )
    assert client.fetch_file(file) == b"CDF\x01data"


def test_fetch_file_rejects_html_body() -> None:
    session = FakeSession()
    url = f"{ROOT}{BASE}00/{etas_name()}"
    session.route(url, FakeResponse(text="<html>404</html>"))
    client = GDSPSDatamartClient(session=session)
    file = GDSPSDatamartFile(
        filename=etas_name(),
        url=url,
        variable=ETAS,
        run=GDSPSRun(datetime(2026, 7, 22, tzinfo=timezone.utc), "00"),
        lead_hours=3,
        valid_time=datetime(2026, 7, 22, 3, tzinfo=timezone.utc),
    )
    with pytest.raises(GDSPSResponseError):
        client.fetch_file(file)


def test_http_error_raises_request_error() -> None:
    session = FakeSession()
    session.route(f"{ROOT}{BASE}", FakeResponse(status_code=503))
    client = GDSPSDatamartClient(session=session)
    with pytest.raises(GDSPSRequestError):
        client.discover_files()


def test_insecure_root_rejected() -> None:
    with pytest.raises(GDSPSConfigurationError):
        GDSPSDatamartClient(root="http://dd.weather.gc.ca")
