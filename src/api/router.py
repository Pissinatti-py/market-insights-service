from fastapi import APIRouter

from src.api.articles_router import router as articles_router
from src.api.config_router import router as config_router
from src.api.curation_router import router as curation_router
from src.api.feed_router import router as feed_router
from src.api.health_router import router as health_router
from src.api.libraries_router import router as libraries_router
from src.api.repositories_router import router as repositories_router
from src.api.tasks_router import router as tasks_router
from src.core.conf import settings

root_router = APIRouter()

# Health/status at the root (no prefix) for probes.
root_router.include_router(health_router)

# Domain routers under the API prefix (default /api).
root_router.include_router(repositories_router, prefix=settings.API_PREFIX)
root_router.include_router(libraries_router, prefix=settings.API_PREFIX)
root_router.include_router(articles_router, prefix=settings.API_PREFIX)
root_router.include_router(curation_router, prefix=settings.API_PREFIX)
root_router.include_router(feed_router, prefix=settings.API_PREFIX)
root_router.include_router(tasks_router, prefix=settings.API_PREFIX)
root_router.include_router(config_router, prefix=settings.API_PREFIX)
