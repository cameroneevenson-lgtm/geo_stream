"""Unit tests for the CaSR HPFX client (HTTP mocked)."""

from __future__ import annotations

from typing import Any

import pytest

from coastal_flood_explorer.casr_common import (
    CASRDataUnavailableError,
    CASRRiversFile,
)
from coastal_flood_explorer.casr_hpfx import CASRHpfxClient

ROOT = "https://hpfx.example.test"
BASE = "/~scar700/rcas-casr/data/CaSR-Rivers_v2.1/per_subbasin/"


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

    def iter_content(self, chunk_size: int = 64 * 1024):
        del chunk_size
        yield self.content


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
        del kwargs
        self.calls.append(url)
        result = self.routes.get(url)
        if result is None:
            raise AssertionError(f"Unexpected mocked request for {url}")
        if isinstance(result, BaseException):
            raise result
        return result


def test_list_months_and_files() -> None:
    session = FakeSession()
    session.route(
        f"{ROOT}{BASE}",
        FakeResponse(
            text=(
                "<html><a href='198001/'>198001/</a>"
                "<a href='198002/'>198002/</a></html>"
            )
        ),
    )
    session.route(
        f"{ROOT}{BASE}198001/",
        FakeResponse(
            text=(
                "<html>"
                "<a href='198001_01AA000_MSC_CaSR-Rivers-Analysis_RiverDischarge_"
                "Sfc_LatLon0.00833_PT0H.nc'>x</a>"
                "<a href='readme.txt'>readme</a>"
                "</html>"
            )
        ),
    )
    client = CASRHpfxClient(session=session, root=ROOT, base_path=BASE)
    assert client.list_months() == ("198001", "198002")
    files = client.list_files("198001", variable="RiverDischarge")
    assert len(files) == 1
    assert files[0].subbasin_id == "01AA000"
    assert isinstance(files[0], CASRRiversFile)


def test_download_netcdf_bytes() -> None:
    payload = b"CDF\x01fake-netcdf"
    url = (
        f"{ROOT}{BASE}198001/"
        "198001_01AA000_MSC_CaSR-Rivers-Analysis_DeepReservoirStorage_"
        "Sfc_LatLon0.00833_PT0H.nc"
    )
    session = FakeSession()
    session.route(
        url,
        FakeResponse(content=payload, content_type="application/x-netcdf"),
    )
    client = CASRHpfxClient(session=session, root=ROOT, base_path=BASE)
    assert client.download(url) == payload


def test_list_files_empty_raises() -> None:
    session = FakeSession()
    session.route(
        f"{ROOT}{BASE}199901/",
        FakeResponse(text="<html></html>"),
    )
    client = CASRHpfxClient(session=session, root=ROOT, base_path=BASE)
    with pytest.raises(CASRDataUnavailableError):
        client.list_files("199901")
