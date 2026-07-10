"""
Library-release collector Celery task.

Monitoring set = the ``mi__libraries`` rows flagged ``is_monitored``, seeded from
``preferences.monitored_libraries`` (``"ecosystem:name"`` strings) so the profile
and the explicit ``POST /api/libraries`` calls both feed it. For each library we
fetch the latest version and, when it advanced, record a deduped release row and
update the stored ``current_version``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.celery.celery_app import celery_app
from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.db.managers.preference_manager import get_or_create_sync
from src.db.session import SyncSession
from src.db.upsert import bulk_upsert_dedup
from src.models.library import Library, LibraryRelease, PackageEcosystem
from src.services.collectors import packages
from src.services.logger_service import logger


def _seed_from_preferences(session, monitored: list[str]) -> None:
    """Ensure a ``mi__libraries`` row exists for each ``ecosystem:name`` in the profile."""
    rows = []
    for entry in monitored:
        if ":" not in entry:
            continue
        eco_raw, name = entry.split(":", 1)
        try:
            eco = PackageEcosystem(eco_raw.strip().lower())
        except ValueError:
            continue
        rows.append({"dedup_key": f"{eco.value}:{name.strip()}", "ecosystem": eco, "name": name.strip()})
    if rows:
        bulk_upsert_dedup(session, Library, rows)


@celery_app.task(
    bind=True,
    name="src.tasks.packages_tasks.collect_releases",
    autoretry_for=(CollectorRetriable,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def collect_releases(self) -> dict:
    """
    Check every monitored library for a new release.

    :return: ``{"checked": int, "new_releases": int}``.
    :rtype: dict
    """
    now = datetime.now(timezone.utc)
    checked = 0
    new_releases = 0

    with SyncSession() as session:
        pref = get_or_create_sync(session)
        if pref.enabled_sources.get("packages", True) is False:
            logger.info("collect_releases: packages source disabled — skipping")
            return {"checked": 0, "new_releases": 0}

        _seed_from_preferences(session, list(pref.monitored_libraries or []))

        libraries = session.query(Library).filter(Library.is_monitored.is_(True), Library.deleted_at.is_(None)).all()

        for lib in libraries:
            checked += 1
            try:
                latest = packages.fetch_latest(lib.ecosystem, lib.name)
            except CollectorTerminal as exc:
                # One bad package (404, renamed) shouldn't fail the whole run.
                logger.warning(f"collect_releases: skipping {lib.dedup_key}: {exc}")
                continue

            if latest.version == lib.current_version:
                continue

            release = {
                "dedup_key": f"{lib.ecosystem.value}:{lib.name}:{latest.version}",
                "library_id": lib.id,
                "previous_version": lib.current_version,
                "new_version": latest.version,
                "is_major": packages.is_major_bump(lib.current_version, latest.version),
                "release_notes": latest.release_notes,
                "released_at": now,
            }
            inserted = bulk_upsert_dedup(session, LibraryRelease, [release])
            new_releases += len(inserted)
            lib.current_version = latest.version
            session.commit()

    if new_releases:
        # Chain curation over the fresh rows — idempotent, so a double-fire is a no-op.
        celery_app.send_task("src.tasks.curation_tasks.curate_uncurated")

    logger.info(f"collect_releases: checked {checked}, new {new_releases}")
    return {"checked": checked, "new_releases": new_releases}
