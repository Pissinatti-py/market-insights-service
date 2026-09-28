"""
Beat-schedule registry — periodic collectors + curation, driven by celery-redbeat.
"""

from __future__ import annotations

from celery.schedules import crontab

beat_schedule: dict = {
    # Trending repositories — every 6 hours.
    "collect-github": {
        "task": "src.tasks.github_tasks.collect_trending",
        "schedule": crontab(minute=0, hour="*/6"),
    },
    # Library releases (PyPI/npm) — daily at noon.
    "collect-packages": {
        "task": "src.tasks.packages_tasks.collect_releases",
        "schedule": crontab(minute=0, hour=12),
    },
    # Articles (Dev.to/Medium/RSS) — every 12 hours.
    "collect-articles": {
        "task": "src.tasks.articles_tasks.collect_articles",
        "schedule": crontab(minute=0, hour="*/12"),
    },
    # AI curation over uncurated items — daily at 02:00.
    "curate": {
        "task": "src.tasks.curation_tasks.curate_uncurated",
        "schedule": crontab(minute=0, hour=2),
    },
    # Re-score the feed against the latest review verdicts (no LLM) — every 30 minutes.
    "rerank": {
        "task": "src.tasks.curation_tasks.rerank_all",
        "schedule": crontab(minute="*/30"),
    },
}
