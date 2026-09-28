# Documentation

Reference docs for **market-insights-service** — a standalone FastAPI microservice
that aggregates software-development market signals (trending GitHub repos,
PyPI/npm releases, technical articles), stores them in PostgreSQL, and curates
them with a local LLM against a configurable technical profile.

## Contents

| Doc | What's in it |
|---|---|
| [architecture.md](architecture.md) | The moving parts (api / worker / beat / db / redis / ollama) and how a signal travels end to end |
| [flows.md](flows.md) | The main flows in detail: collection → curation → manual review, plus idempotency and failure handling |
| [endpoints.md](endpoints.md) | Full REST API reference, grouped by domain |
| [data-model.md](data-model.md) | Every table, its columns, and the dedup/soft-delete conventions |
| [configuration.md](configuration.md) | Env vars (`src/core/conf.py`) vs. runtime preferences (DB) |
| [operations.md](operations.md) | Running locally, triggering tasks by hand, migrations, testing |

## The one-paragraph version

Celery **beat** fires four collectors on a schedule. Each reads the singleton
**preferences** row (the technical profile) to build its query, fetches from an
external source, and **upserts** rows keyed by a unique `dedup_key` — so re-runs
never duplicate. A nightly **curation** task feeds every not-yet-curated item to
a local **Ollama** LLM, which returns a summary + tags; a local ranker scores it
against your past approve/reject verdicts (embedding kNN), and the result is
written as a `pending` curation row. The **API** serves the stored
repos/libraries/articles and lets a human approve or reject each curation. The
profile and per-source on/off toggles are themselves edited through the API.

```
GitHub / PyPI / npm / Dev.to / Medium         Ollama (local LLM)
        │  (Celery collectors)                        │  (curation task)
        ▼                                             ▼
   PostgreSQL  ◄──────────  preferences  ──────────►  curation rows
        │                    (profile)                     │
        └──────────────────►  FastAPI REST API  ◄──────────┘
                                    │
                              clients / reviewers
```

See [architecture.md](architecture.md) for the full picture.
