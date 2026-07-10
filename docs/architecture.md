# Architecture

## Processes

The stack (`docker-compose.yml`) is five containers:

| Container | Image / build | Role |
|---|---|---|
| `market_insights_api` | `Dockerfile` | FastAPI (uvicorn `:8003`). Serves the REST API and applies Alembic migrations on boot (`scripts/docker-entrypoint.sh`). |
| `market_insights_celery_worker` | `Dockerfile.celery` | Runs the collectors + curation task on the `celery` queue. |
| `market_insights_celery_beat` | `Dockerfile.celery` | RedBeat scheduler — enqueues the periodic tasks. |
| `market_insights_db` | `postgres:16-alpine` | This service owns its own Postgres (host port `5434`). |
| `market_insights_redis` | `redis:7-alpine` | Celery broker + result backend **and** the RedBeat schedule store. |

One external dependency lives **outside** the compose stack: a local **Ollama**
daemon, reached over `host.docker.internal:11434` (see the `extra_hosts` entry).
The API and worker containers can talk to it; the DB/redis don't need to.

## Layers (in `src/`)

```
src/
  main.py                 FastAPI app factory (CORS, router mount)
  api/                    HTTP layer — one router per domain, thin
    router.py               mounts domain routers under API_PREFIX (/api)
    health_router.py        /health, /status  (no prefix — for probes)
    repositories_router.py  libraries_router.py  articles_router.py
    curation_router.py      config_router.py
  schemas/                Pydantic request/response models (the API contract)
  db/
    session.py              async engine (API) + SyncSession (Celery)
    managers/               repository-pattern data access (paginate, upsert, soft-delete)
    upsert.py               bulk_upsert_dedup — INSERT … ON CONFLICT DO NOTHING
    mixins.py               TimestampMixin, SoftDeleteMixin
  models/                 SQLAlchemy ORM tables (mi__* prefix)
  tasks/                  Celery task entrypoints (one module per collector + curation)
  services/
    collectors/             external fetchers: github, packages, articles, linkedin
    agents/curation_agent.py the ONLY place the LLM prompt + Ollama call live
  core/
    conf.py                 pydantic-settings (env)
    celery/                 celery_app + beat schedules
    exceptions.py           CollectorRetriable / CollectorTerminal
```

### Why two DB sessions

The **API** is async (`asyncpg`, `get_db_async_session`). **Celery tasks** run on
a synchronous engine (`SyncSession`) because the collectors are ordinary blocking
HTTP calls and Celery workers are process/threads, not an event loop. Both point
at the same Postgres; the models are shared.

## End-to-end flow

```
                          ┌─────────────── celery_beat (RedBeat) ───────────────┐
                          │  every 6h  daily 12:00  every 12h   daily 02:00      │
                          ▼            ▼            ▼            ▼
                    collect_trending  collect_releases  collect_articles  curate_uncurated
                          │            │            │            │
   reads profile ◄────────┴────────────┴────────────┘            │
   (preferences)          │                                      │
                          ▼  fetch + rank                        ▼  for each uncurated item
        GitHub / PyPI / npm / Dev.to / HN / Medium         Ollama /api/chat
                          │  bulk_upsert_dedup                   │  summary + tags + score
                          ▼  (ON CONFLICT DO NOTHING)            ▼  validate via CurationCreate
                     ┌──────────────── PostgreSQL ─────────────────┐
                     │  mi__repositories (+ snapshots)             │
                     │  mi__libraries / mi__library_releases       │
                     │  mi__articles                               │
                     │  mi__curation  (status = pending)           │
                     └──────────────────────┬──────────────────────┘
                                             ▼
                                     FastAPI REST API
                          list/search signals · review curation · edit profile
```

1. **Beat** enqueues a collector on its cadence.
2. The collector task opens a `SyncSession`, reads the **preferences** singleton,
   and bails early if that source is toggled off in `enabled_sources`.
3. It calls the matching `services/collectors/*` fetcher, then `bulk_upsert_dedup`
   writes new rows keyed by `dedup_key`. Existing rows are left untouched.
4. Nightly, **curate_uncurated** finds items with no curation row, renders each to
   text, and asks the LLM (via `curation_agent.curate`) for a summary/tags/score.
   Valid output becomes a `pending` `mi__curation` row.
5. **Clients** read everything through the API; a **reviewer** approves/rejects
   each curation, and edits the profile/toggles that steer future runs.

Details of each step — including idempotency and error handling — are in
[flows.md](flows.md).
