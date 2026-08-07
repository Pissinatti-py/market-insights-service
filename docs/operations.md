# Operations

Everything here is also wrapped in the `Makefile`; raw commands shown so they work
without it.

## Run the stack (Docker)

```bash
cp .env.example .env        # set GITHUB_TOKEN; point OLLAMA_BASE_URL at your daemon
docker compose up --build   # api :8003, postgres, redis, celery (worker+beat)  →  make up
```

The `api` container runs migrations on boot (`scripts/docker-entrypoint.sh` →
`alembic upgrade head`) before starting uvicorn. Review UI:
<http://localhost:8003/> · Swagger: <http://localhost:8003/docs>.
(`uv.lock` is committed — no pre-step needed.)

> **Gotcha:** compose mounts `.:/app`, which overlays the host directory on top of
> the image — so `scripts/*.sh` must be executable **on the host** or the
> entrypoint fails with `permission denied`. Fix once: `chmod +x scripts/*.sh`
> (the bit is stored in git, so it won't recur for teammates).

`make down` stops everything; `make logs` tails the api.

## Run locally (no Docker)

```bash
uv sync                                              # installs into .venv (Python 3.13)
uv run uvicorn src.main:app --reload --port 8003
```

Needs a reachable Postgres + Redis; override `DATABASE_URL`, `REDIS_URL`,
`CELERY_*` in `.env`. Run the worker (with embedded beat) yourself if you want
the collectors:

```bash
uv run celery -A src.core.celery.celery_app worker -B --loglevel=info -Q celery --scheduler redbeat.RedBeatScheduler
```

## Trigger a task by hand

Don't wait for the schedule — enqueue any collector/curation task:

```bash
curl -X POST localhost:8003/api/tasks/collect_trending/trigger   # over HTTP
# or via the CLI:
make trigger t=src.tasks.github_tasks.collect_trending
docker compose exec api celery -A src.core.celery.celery_app call src.tasks.github_tasks.collect_trending
```

Check the outcome afterwards in `curl -s localhost:8003/status` (`tasks` map).

Task names: `src.tasks.github_tasks.collect_trending`,
`src.tasks.packages_tasks.collect_releases`,
`src.tasks.articles_tasks.collect_articles`,
`src.tasks.curation_tasks.curate_uncurated`,
`src.tasks.curation_tasks.recurate_all`.

Watch the worker: `docker compose logs -f celery`.

## Migrations (Alembic)

```bash
make migrate                          # alembic upgrade head   (auto-runs on api boot)
make makemigrations m="add x"         # autogenerate a revision
make downgrade                        # alembic downgrade -1
```

Versions live in `src/migrations/versions/`; baseline is
`20260805164116_baseline_schema.py`, which creates the whole schema as explicit
`op.*` calls.

> A migration must describe a fixed point in time. Never write one against
> `Base.metadata` (`create_all`) — it would replay whatever the models look like
> *when it runs*, so later revisions collide with tables it already created and
> `alembic upgrade head` breaks on a fresh database.
> `tests/integration/test_migrations.py` guards this: it runs the chain on an
> empty DB and fails if autogenerate still finds a diff against the models. Run
> it after every model change.

## Tests

```bash
uv run pytest tests/unit         # no DB — collectors + curation agent (httpx mocked via respx)
uv run pytest tests/integration  # needs Postgres (TEST_DATABASE_URL)
make cov                         # everything + coverage (term + html)
```

## Lint / format

```bash
make lint        # ruff check + ruff format --check
make format      # ruff check --fix + ruff format
```

## Quick health checks

```bash
curl -s localhost:8003/health    # process up
curl -s localhost:8003/status    # DB reachable?  {"status":"ok","database":true}
docker compose ps                # all four containers Up / healthy
```
