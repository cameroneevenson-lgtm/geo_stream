"""Unit tests for the endpoint-gated GDSPS THREDDS/OPeNDAP client."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from coastal_flood_explorer.gdsps_common import (
    ETAS,
    GDSPSConfigurationError,
    GDSPSRequestError,
)
from coastal_flood_explorer import gdsps_thredds
from coastal_flood_explorer.gdsps_thredds import (
    GDSPS_THREDDS_CATALOG_URL,
    GDSPSThreddsClient,
    GDSPSThreddsDataset,
    is_configured,
)

CATALOG_URL = "https://example-thredds.gc.ca/thredds/catalog/gdsps/catalog.xml"
CAT_NS = 'xmlns="http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"'


def catalog_xml(*datasets: str, opendap: bool = True) -> str:
    service = (
        '<service name="odap" serviceType="OPENDAP" base="/thredds/dodsC/"/>'
        if opendap
        else '<service name="http" serviceType="HTTPServer" base="/thredds/fileServer/"/>'
    )
    inner = "".join(datasets)
    return (
        f'<?xml version="1.0"?><catalog {CAT_NS}>{service}{inner}</catalog>'
    )


def dataset_xml(name: str, url_path: str) -> str:
    return f'<dataset name="{name}" urlPath="{url_path}"/>'


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        status_code: int = 200,
        content_type: str = "text/xml",
    ) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


class FakeSession:
    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self.responses = deque(responses)
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}
        self.mounts: dict[str, Any] = {}

    def mount(self, prefix: str, adapter: Any) -> None:
        self.mounts[prefix] = adapter

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        result = self.responses.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def test_inert_by_default() -> None:
    assert GDSPS_THREDDS_CATALOG_URL is None
    assert is_configured() is False
    assert is_configured(None) is False
    assert is_configured("  ") is False
    assert is_configured(CATALOG_URL) is True


def test_constructing_without_endpoint_raises() -> None:
    with pytest.raises(GDSPSConfigurationError):
        GDSPSThreddsClient(None)


def test_discovers_gdsps_opendap_datasets() -> None:
    document = catalog_xml(
        dataset_xml("GDSPS ETAS", "gdsps/etas_20260722.nc"),
        dataset_xml("Some other model", "gdps/tt.nc"),
    )
    client = GDSPSThreddsClient(
        CATALOG_URL, session=FakeSession(FakeResponse(text=document))
    )
    datasets = client.discover_datasets()
    assert len(datasets) == 1
    assert datasets[0].variable == ETAS
    assert datasets[0].url.endswith("/thredds/dodsC/gdsps/etas_20260722.nc")
    assert datasets[0].url.startswith("https://example-thredds.gc.ca/")


def test_no_opendap_service_yields_nothing() -> None:
    document = catalog_xml(
        dataset_xml("GDSPS ETAS", "gdsps/etas.nc"), opendap=False
    )
    client = GDSPSThreddsClient(
        CATALOG_URL, session=FakeSession(FakeResponse(text=document))
    )
    assert client.discover_datasets() == ()


def test_open_dataset_uses_injected_opener() -> None:
    opened: list[str] = []

    def opener(url: str) -> str:
        opened.append(url)
        return f"dataset::{url}"

    client = GDSPSThreddsClient(
        CATALOG_URL, session=FakeSession(), opener=opener
    )
    dataset = GDSPSThreddsDataset(
        name="GDSPS ETAS",
        url="https://example-thredds.gc.ca/thredds/dodsC/gdsps/etas.nc",
        variable=ETAS,
    )
    result = client.open_dataset(dataset)
    assert result == f"dataset::{dataset.url}"
    assert opened == [dataset.url]


def test_open_dataset_wraps_opener_errors() -> None:
    def opener(url: str) -> str:
        raise OSError("boom")

    client = GDSPSThreddsClient(
        CATALOG_URL, session=FakeSession(), opener=opener
    )
    with pytest.raises(GDSPSRequestError):
        client.open_dataset("https://example-thredds.gc.ca/thredds/dodsC/x.nc")


def test_insecure_catalog_rejected() -> None:
    with pytest.raises(GDSPSConfigurationError):
        GDSPSThreddsClient("http://insecure.example/catalog.xml")
