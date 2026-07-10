"""
Library-release collector for PyPI and npm.

For each monitored library, fetch the registry's JSON, read the latest version,
and — when it differs from what we last stored — emit a release row flagged
``is_major`` when the major version component increased (likely breaking).

Plain httpx, shared retriable/terminal contract. A 404 (package not found) is
terminal *for that package* but the task treats one bad package as a skip, not a
whole-run failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from packaging.version import InvalidVersion, Version

from src.core.conf import settings
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.models.library import PackageEcosystem

_PYPI_URL = "https://pypi.org/pypi/{name}/json"
_NPM_URL = "https://registry.npmjs.org/{name}"


@dataclass(frozen=True, slots=True)
class LatestRelease:
    """The latest version of a package, as read from its registry."""

    version: str
    release_notes: str | None = None


def is_major_bump(previous: str | None, new: str) -> bool:
    """
    Return True when ``new`` raises the major version above ``previous``.

    A missing/unparseable previous version is not treated as a major bump (we
    only just started tracking it).

    :param previous: Last known version, or None.
    :type previous: str | None
    :param new: The newly-seen version.
    :type new: str
    :return: Whether this is a major-version increase.
    :rtype: bool
    """
    if not previous:
        return False
    try:
        return Version(new).major > Version(previous).major
    except InvalidVersion:
        return False


def fetch_latest(ecosystem: PackageEcosystem, name: str) -> LatestRelease:
    """
    Fetch the latest released version of one package.

    :param ecosystem: ``pypi`` or ``npm``.
    :type ecosystem: PackageEcosystem
    :param name: Package name.
    :type name: str
    :return: The latest release.
    :rtype: LatestRelease
    :raises CollectorRetriable: Timeout, transport error, 429, or 5xx.
    :raises CollectorTerminal: 404 / other 4xx or an unparseable body.
    """
    url = (_PYPI_URL if ecosystem == PackageEcosystem.PYPI else _NPM_URL).format(name=name)
    try:
        with httpx.Client(timeout=settings.COLLECTOR_HTTP_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise CollectorRetriable(f"packages: {type(exc).__name__}: {exc}") from exc

    if resp.status_code == 429 or resp.status_code >= 500:
        raise CollectorRetriable(f"packages: status {resp.status_code} for {name}")
    if resp.status_code != 200:
        raise CollectorTerminal(f"packages: status {resp.status_code} for {name}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise CollectorTerminal(f"packages: non-JSON for {name}: {exc}") from exc

    if ecosystem == PackageEcosystem.PYPI:
        return _parse_pypi(body, name)
    return _parse_npm(body, name)


def _parse_pypi(body: dict, name: str) -> LatestRelease:
    """Extract the latest version + summary from a PyPI JSON payload."""
    info = body.get("info") or {}
    version = info.get("version")
    if not version:
        raise CollectorTerminal(f"packages: no version in PyPI payload for {name}")
    return LatestRelease(version=str(version), release_notes=info.get("summary"))


def _parse_npm(body: dict, name: str) -> LatestRelease:
    """Extract the latest version + description from an npm registry payload."""
    version = (body.get("dist-tags") or {}).get("latest")
    if not version:
        raise CollectorTerminal(f"packages: no dist-tags.latest for {name}")
    return LatestRelease(version=str(version), release_notes=body.get("description"))
