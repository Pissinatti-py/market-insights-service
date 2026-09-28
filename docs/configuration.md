# Configuration

Two distinct layers:

- **Deployment config** — env vars in `.env`, read once at boot by
  `src/core/conf.py` (pydantic-settings). Wiring: DB, Redis, tokens, LLM endpoint.
- **Runtime config** — the `mi__preferences` singleton in the DB, edited live via
  `/api/config/*`. *What* to search for. No redeploy; takes effect next run.

## Environment variables (`src/core/conf.py`)

Copy `.env.example` → `.env`. Defaults shown; `extra="ignore"` so unknown keys
are dropped.

### App / API
| Var | Default | Notes |
|---|---|---|
| `APP_NAME` | `Market Insights Service` | Shown in `/health` + Swagger title. |
| `APP_VERSION` | `0.1.0` | |
| `API_PREFIX` | `/api` | Prefix for all domain routers. |
| `ALLOWED_ORIGINS` | `*` | CORS. JSON array or comma-separated; empty ⇒ allow all. |

### Database (this service owns its Postgres)
| Var | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@db:5432/market_insights` | The only one the app reads. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `postgres` / `postgres` / `market_insights` | Read by **docker-compose** to provision the container, not by the app. |

> `DATABASE_URL` is the single source of truth for the application: the API uses
> the async `asyncpg` URL and Celery derives the sync one from it. Changing the
> `POSTGRES_*` vars alone moves the container's credentials but not where the app
> connects — change both together. Host port `5434` maps to the container's
> `5432` (compose).

### Redis + Celery
| Var | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | |
| `CELERY_BROKER_URL` | `redis://redis:6379/1` | Also the RedBeat schedule store. |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/2` | |

### Collectors
| Var | Default | Notes |
|---|---|---|
| `GITHUB_TOKEN` | `""` | **Set this** — raises the GitHub Search rate limit. |
| `COLLECTOR_HTTP_TIMEOUT_SECONDS` | `30.0` | |
| `GITHUB_MAX_RESULTS` | `50` | |

### LLM curation (Ollama)
| Var | Default | Notes |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | A host-side daemon reached from the container. Point at any OpenAI-compatible `/api/chat` to swap providers. |
| `OLLAMA_MODEL` | `qwen3:14b-q8_0` | Also stamped onto each curation row. |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `120.0` | |
| `CURATION_BATCH_SIZE` | `50` | Items curated per `curate_uncurated` run (round-robin across types). A run that leaves backlog queues the next one. |
| `ARTICLE_MAX_CHARS` | `6000` | Truncation cap for full article bodies fetched at curation time. |
| `ARTICLE_MAX_AGE_DAYS` | `14` | Articles older than this never enter the pipeline. |

## Runtime preferences (DB)

Edited through the API, not env — see [endpoints.md](endpoints.md#configuration--apiconfig)
and the `mi__preferences` schema in [data-model.md](data-model.md).

- `PUT /api/config/preferences` → `stacks`, `areas`, `keywords`, `monitored_libraries`
- `POST /api/config/sources` → `enabled_sources` toggles

Example — target a Python/Rust backend profile and turn articles off:

```bash
curl -X PUT localhost:8003/api/config/preferences -H 'content-type: application/json' -d '{
  "stacks": ["python", "rust"],
  "areas": ["backend", "devops"],
  "keywords": ["fastapi", "async"],
  "monitored_libraries": ["pypi:fastapi", "npm:vue"]
}'

curl -X POST localhost:8003/api/config/sources -H 'content-type: application/json' -d '{
  "enabled_sources": {"github": true, "packages": true, "articles": false}
}'
```
