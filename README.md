# market-insights-service

A standalone FastAPI microservice that aggregates software-development market
signals — trending GitHub repositories, PyPI/npm library releases, and technical
articles — stores them in PostgreSQL, and curates them with a local LLM against a
configurable technical profile. Built to mirror the **pissync** microservice
conventions (uv, async SQLAlchemy 2.0, Celery + RedBeat on Redis, single-stage uv
Docker image) so it can later drop into that network unchanged.

> **Full docs:** see [`docs/`](docs/README.md) — architecture, the collection →
> curation → review flows, API reference, data model, configuration, and ops.

## What it does

| Collector (Celery beat) | Source | Cadence |
|---|---|---|
| `collect_trending` | GitHub Search API | every 6h |
| `collect_releases` | PyPI + npm registries | daily 12:00 |
| `collect_articles` | Dev.to API + Hacker News (Algolia) + Medium tag RSS | every 12h |
| `curate_uncurated` | Ollama LLM curation | chained after each collection + daily 02:00 sweep |

Every collected row carries a unique `dedup_key`; collectors upsert with
`INSERT … ON CONFLICT DO NOTHING`, so re-runs are idempotent. Curation is also
idempotent — an item is curated at most once (`(item_type, item_id)` unique).
Articles additionally pass an age gate (`ARTICLE_MAX_AGE_DAYS`) and a
title-fingerprint collapse, so old news and cross-source duplicates never enter
the pipeline.

Consume the result at **`GET /`** (single-page review UI: ranked feed, filters,
approve/reject) or **`GET /api/feed`** (same data over JSON, default
pending + approved).

## Quick start (Docker)

```bash
cp .env.example .env        # set GITHUB_TOKEN; point OLLAMA_BASE_URL at your daemon
docker compose up --build   # api :8003, postgres, redis, celery (worker + embedded beat)
```

The API container migrates on boot. Review UI at http://localhost:8003/ —
Swagger at http://localhost:8003/docs.

Fire a collector by hand — over HTTP (no shell into the container needed):

```bash
# list what can be triggered (short name → full Celery name)
curl http://localhost:8003/api/tasks

# enqueue one now instead of waiting for beat → 202 + Celery task id
curl -X POST http://localhost:8003/api/tasks/collect_trending/trigger
# {"task": "src.tasks.github_tasks.collect_trending", "task_id": "…", "state": "queued"}

# then watch it land
curl http://localhost:8003/status
```

Triggerable: `collect_trending`, `collect_releases`, `collect_articles`,
`curate_uncurated`, `recurate_all`. Or via the Makefile if you have a shell
on the box:

```bash
make trigger t=src.tasks.github_tasks.collect_trending
```

## Quick start (local)

```bash
uv sync                     # installs into .venv (Python 3.13)
uv run uvicorn src.main:app --reload --port 8003
```

Requires a reachable Postgres + Redis (`DATABASE_URL`, `REDIS_URL`, `CELERY_*`).

## Configuration

All settings live in `src/core/conf.py` (pydantic-settings, read from `.env`).
Key vars: `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`,
`GITHUB_TOKEN`, `OLLAMA_BASE_URL`/`OLLAMA_MODEL`. The *what to
search for* (stacks, keywords, monitored libraries, source toggles) is stored in
the database and edited via `PUT /api/config/preferences` + `POST /api/config/sources`.

## Endpoints

`GET /` (review UI); `GET /health`, `GET /status`; `/api/feed` (ranked unified
feed with `status`/`item_type`/`min_score`/`tag`/`since` filters);
`/api/repositories` (list/detail/`POST search`); `/api/libraries`
(list/detail-history/add/remove); `/api/articles` (list/detail/`search`);
`/api/curation` (list/`stats`/`PUT {id}/review`/bulk review); `/api/tasks`
(list/`POST {name}/trigger`); `/api/config/preferences` + `/api/config/sources`.
Full schemas at `/docs`.

A few more examples:

```bash
# newest curated repositories, page 2
curl "http://localhost:8003/api/repositories?page=2&per_page=20"

# ad-hoc repo search (POST body, not a saved preference)
curl -X POST http://localhost:8003/api/repositories/search \
  -H 'content-type: application/json' \
  -d '{"languages": ["rust"], "keywords": ["cli"], "min_stars": 500, "max_results": 25}'

# start monitoring a library
curl -X POST http://localhost:8003/api/libraries \
  -H 'content-type: application/json' \
  -d '{"ecosystem": "pypi", "name": "httpx"}'

# approve a curated item
curl -X PUT http://localhost:8003/api/curation/42/review \
  -H 'content-type: application/json' \
  -d '{"status": "approved"}'

# change what the collectors look for
curl -X PUT http://localhost:8003/api/config/preferences \
  -H 'content-type: application/json' \
  -d '{"stacks": ["python", "rust"], "keywords": ["llm", "async"]}'
```

## Testing

```bash
uv run pytest tests/unit              # no DB — collectors + curation agent (httpx mocked)
uv run pytest tests/integration       # needs Postgres (TEST_DATABASE_URL)
uv run pytest --cov=src               # everything + coverage
```

## Tech stack

FastAPI · async SQLAlchemy 2.0 + PostgreSQL · Alembic · Celery + celery-redbeat on
Redis · httpx · Ollama (LLM) · uv · Docker · pytest + respx · ruff.
