"""
Shared collector exception hierarchy.

Every external-data collector raises one of two shapes, mirroring
``pissync-core``'s ingestion client. The Celery task that wraps a collector
turns :class:`CollectorRetriable` into a backed-off retry and lets
:class:`CollectorTerminal` fail the run without retrying.
"""

from __future__ import annotations


class CollectorError(RuntimeError):
    """Base for all collector failures."""


class CollectorRetriable(CollectorError):
    """Transient failure — timeout, connection error, 429, or 5xx. Retry the run."""


class CollectorTerminal(CollectorError):
    """Terminal failure — 4xx (other than 429) or an unparseable response. Do not retry."""
