import json

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Main app
    APP_NAME: str = "Market Insights Service"
    APP_VERSION: str = "0.1.0"
    API_PREFIX: str = "/api"
    DEBUG: bool = True

    # CORS
    ALLOWED_ORIGINS: str = "*"

    # Database (this service owns its own Postgres)
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "market_insights"
    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@db:5432/market_insights"

    # Redis + Celery
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # External collectors
    GITHUB_TOKEN: str = ""
    COLLECTOR_HTTP_TIMEOUT_SECONDS: float = 30.0
    GITHUB_MAX_RESULTS: int = 50

    # LLM curation — Ollama over plain httpx (no SDK). Swap OLLAMA_BASE_URL for any
    # OpenAI-compatible /api/chat endpoint to change providers.
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"
    OLLAMA_MODEL: str = "qwen3:14b-q8_0"
    OLLAMA_REQUEST_TIMEOUT_SECONDS: float = 120.0
    # Per-run safety cap: curation is chained after each collector, so runs stay
    # small; anything beyond waits for the next chained run or the daily sweep.
    CURATION_BATCH_SIZE: int = 50

    # Curation enrichment — read the full article body before scoring.
    ARTICLE_MAX_CHARS: int = 6000

    # Articles older than this never enter the pipeline (kills resurfaced old news).
    ARTICLE_MAX_AGE_DAYS: int = 14

    @property
    def cors_origins(self) -> list[str]:
        """
        Parse ``ALLOWED_ORIGINS`` into a list of origins for CORS.

        Accepts a JSON array or a comma-separated string; empty means allow all.

        :return: The configured origins, or ``["*"]`` when unset.
        :rtype: list[str]
        """
        v = self.ALLOWED_ORIGINS.strip()
        if not v:
            return ["*"]
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return [origin.strip() for origin in v.split(",") if origin.strip()]


settings = Settings()
