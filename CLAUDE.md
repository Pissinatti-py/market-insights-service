# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`market-insights-service` is a standalone FastAPI microservice that collects software-development
market signals (trending GitHub repos, PyPI/npm releases, technical articles), stores them in
PostgreSQL, and curates them with a local LLM (Ollama) against a configurable technical profile.
It deliberately mirrors **pissync-core** conventions (uv, async SQLAlchemy 2.0, Celery + RedBeat,
single-stage uv Docker image) so it can drop into that network unchanged.

Extended docs live in `docs/` (architecture, flows, data-model, endpoints, configuration, operations).

## Commands

Run via the Makefile (Docker) or `uv` directly (local). Python 3.13, package manager is **uv** (not pip).

```bash
# Docker (recommended) — api :8003, postgres, redis, celery (worker + embedded beat)
make up                 # docker compose up --build  (api migrates on boot)
make down / make logs
make migrate            # alembic upgrade head (inside api container)
make makemigrations m="msg"   # alembic autogenerate
make downgrade          # alembic downgrade -1
make trigger t=src.tasks.github_tasks.collect_trending   # fire a task by full name

# Local (needs reachable Postgres + Redis)
uv sync
uv run uvicorn src.main:app --reload --port 8003

# Lint / format — ruff only (line-length 120, double quotes)
make lint               # ruff check + ruff format --check
make format             # ruff check --fix + ruff format

# Tests
make test               # uv run pytest -ra
uv run pytest tests/unit          # no DB — collectors + agents, httpx mocked via respx
uv run pytest tests/integration   # needs Postgres (TEST_DATABASE_URL)
uv run pytest --cov=src           # coverage
uv run pytest tests/unit/test_github_collector.py::test_name   # single test
```

Trigger a task over HTTP without a shell: `POST /api/tasks/{short_name}/trigger`; list runs at `GET /status`.
The review UI (single static page over the API) is served at `GET /` from `src/static/index.html`.

## Architecture

**Two DB engines, one models set** (`src/db/session.py`). The **async** engine drives the FastAPI
request path (`get_db_async_session` dependency). The **sync** engine drives every Celery task and
Alembic — collectors/curation always open `with SyncSession() as session:`. `DATABASE_URL` is the
asyncpg URL; the sync URL is derived by stripping `+asyncpg`.

**Request path (FastAPI).** `src/api/router.py` mounts all routers under `API_PREFIX` (`/api`).
Routers → `db/managers/*` → models. Managers subclass `BaseManager` (`db/managers/base_manager.py`),
a generic async CRUD (`create/get/get_multi/paginate/update/soft_delete`) parameterized by the model.
Prefer adding query methods to the relevant manager over inlining SQL in a router. Pydantic schemas
live in `src/schemas/`; list endpoints envelope results with `Page.from_result(...)`.

**Collection path (Celery).** `src/tasks/*` are the thin task wrappers; the real work lives in
`src/services/collectors/*` (github, packages, articles — pure functions returning validated schema
rows, all HTTP via httpx). Each task: read the `Preference` row → fetch → `bulk_upsert_dedup(...)` →
**chain `curate_uncurated` when anything was inserted**. Articles additionally pass an age gate
(`ARTICLE_MAX_AGE_DAYS`) and a `title_fingerprint` near-dup collapse before the upsert.

**Idempotency is structural.** Every collected row carries a unique `dedup_key`; `db/upsert.py`
does `INSERT … ON CONFLICT (dedup_key) DO NOTHING` and returns only the newly-inserted rows, so
re-runs are no-ops and curation only chains over new items. Curation is likewise once-per-item via a
`UniqueConstraint(item_type, item_id)` on `mi__curation`.

**Review decisions feed back into scoring.** Each curation run loads the most recently
approved/rejected rows (`curation_manager.recent_decisions_sync`) once and passes them to
`curate()` as few-shot examples of the user's taste — the reviewed row's own summary/tags/score
*is* the example, which is why this needed no schema change. Two invariants: the block is dropped
unless **both** sides have ≥2 examples (one-sided feedback just ratchets scores up), and an item
never gets its own decision back (`recurate_all` would otherwise score approved items against
themselves and fake `GET /api/curation/calibration`, the metric that says whether any of this
works). Keyword suggestions (`GET /api/config/keyword-suggestions`) stay read-only on purpose —
the loop is self-reinforcing, so it must not also rewrite what the collectors search for.

**Curation is a two-stage LLM pipeline** (`src/services/agents/`). `enrichment.build_context()`
fetches the full article body for articles (`tools/article_reader`, best-effort — a failure drops
the block, never breaks the run); repos/releases carry their evidence in the rendered item text.
Then `curation_agent.curate()` sends item + profile + context to Ollama's `/api/chat` (JSON mode,
schema-constrained) and validates the output through `CurationCreate`. **The curation prompt lives
only in `curation_agent.py`** — never inline it into a task. Retriable failures (Ollama down/5xx)
raise `CollectorRetriable` and the task retries; bad output raises `CollectorTerminal` and the item
is **dead-lettered** as a `status=failed` curation row (never re-selected; `recurate_all` is the
retry path). The batch (`CURATION_BATCH_SIZE`, default 50) is drained round-robin across the three
item types, newest first.

**Task-run bookkeeping is automatic.** `src/core/celery/task_runs.py` connects a `task_postrun`
signal that writes one `mi__task_runs` row per finished task (success or failure) — new tasks are
covered with zero extra code, and recording never breaks the task (errors are swallowed).

**Live updates for the review UI.** `src/core/events.py` publishes a JSON event on the `mi:events`
Redis channel from a `Curation` `after_insert` listener (per item, so the page moves while a batch is
still draining) and from the task-run recorder; `GET /api/events` relays the channel as server-sent
events and `index.html` listens with `EventSource`. Publishing is fire-and-forget — Redis down only
loses the notification, never the task.

**Config split.** Infra settings (`DATABASE_URL`, `OLLAMA_*`, `GITHUB_TOKEN`, …)
are env-driven via pydantic-settings in `src/core/conf.py`. The *what to search for* (stacks,
keywords, monitored libraries, per-source toggles) is a DB row, edited via
`PUT /api/config/preferences` + `POST /api/config/sources` and read by collectors through
`preference_manager.get_or_create_sync()`.

**Celery config** (`src/core/celery/celery_app.py`): JSON-only bus (no pickle), `acks_late`,
`prefetch=1`, RedBeat (Redis-backed) scheduler so the periodic schedule survives restarts. The beat
schedule is in `src/core/celery/schedules.py`.

## Conventions

- **All tables are prefixed `mi__`** (`mi__repositories`, `mi__curation`, …) — namespaced so the
  service can share a database in the pissync network.
- **Task triggering is whitelisted.** `tasks_router.TRIGGERABLE_TASKS` maps a safe short name → full
  Celery name; the task name is never taken from the request verbatim. Add new triggerable tasks
  there and keep it in sync with the beat schedule.
- **Migrations are Alembic**, not `Base.metadata.create_all`. `src/main.py` imports `src.models` for
  the side effect of registering mappers; keep new models importable from that package.
  A revision must describe a **fixed point in time** — never write one against `Base.metadata`, which
  replays whatever the models look like when it runs and collides with later revisions on a fresh DB.
  After any model change run `tests/integration/test_migrations.py`: it applies the chain to an empty
  database and fails if autogenerate still sees a diff.
- **`order_by` from a request is a `Literal`**, never a free string — `BaseManager._order_columns`
  raises on an unknown field so a typo can't silently degrade to insertion order.
