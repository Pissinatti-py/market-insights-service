import httpx
import pytest
import respx

from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.models.library import PackageEcosystem
from src.services.collectors import packages


def test_is_major_bump():
    assert packages.is_major_bump("1.4.2", "2.0.0") is True
    assert packages.is_major_bump("1.4.2", "1.5.0") is False
    assert packages.is_major_bump(None, "3.0.0") is False  # newly tracked
    assert packages.is_major_bump("garbage", "2.0.0") is False


@respx.mock
def test_fetch_latest_pypi():
    respx.get("https://pypi.org/pypi/httpx/json").mock(
        return_value=httpx.Response(200, json={"info": {"version": "0.27.2", "summary": "HTTP for humans"}})
    )
    latest = packages.fetch_latest(PackageEcosystem.PYPI, "httpx")
    assert latest.version == "0.27.2"
    assert latest.release_notes == "HTTP for humans"


@respx.mock
def test_fetch_latest_npm():
    respx.get("https://registry.npmjs.org/vite").mock(
        return_value=httpx.Response(200, json={"dist-tags": {"latest": "5.4.0"}, "description": "Next-gen bundler"})
    )
    latest = packages.fetch_latest(PackageEcosystem.NPM, "vite")
    assert latest.version == "5.4.0"


@respx.mock
def test_fetch_latest_404_is_terminal():
    respx.get("https://pypi.org/pypi/nope-no-pkg/json").mock(return_value=httpx.Response(404))
    with pytest.raises(CollectorTerminal):
        packages.fetch_latest(PackageEcosystem.PYPI, "nope-no-pkg")


@respx.mock
def test_fetch_latest_5xx_is_retriable():
    respx.get("https://pypi.org/pypi/httpx/json").mock(return_value=httpx.Response(503))
    with pytest.raises(CollectorRetriable):
        packages.fetch_latest(PackageEcosystem.PYPI, "httpx")
