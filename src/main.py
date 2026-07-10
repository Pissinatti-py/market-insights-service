from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import src.models  # noqa — registers SQLAlchemy models with the mapper at startup
from src.api.router import root_router
from src.core.conf import settings

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"


def get_application() -> FastAPI:
    """
    Create and configure the FastAPI application.

    :return: The configured application.
    :rtype: FastAPI
    """
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(root_router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """The review UI — one static page over the existing API."""
        return FileResponse(_INDEX_HTML, media_type="text/html")

    return app


app = get_application()
