# API reference

Base URL `http://localhost:8003`. Domain routes are under `API_PREFIX` (default
`/api`); health/status sit at the root for probes. The **review UI** is served at
`/` (single page over these same endpoints). Interactive docs: `/docs`
(Swagger), `/redoc`, raw spec at `/openapi.json`.

List endpoints return a **`Page`** envelope: `{ total, items, page, per_page,
num_pages }`. Common list params: `page` (≥1), `per_page` (1–100). All list/detail
endpoints hide soft-deleted rows (`deleted_at IS NULL`).

---

## Health — no prefix

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness. `{status, service, version}` — always OK if the process is up. |
| GET | `/status` | Readiness. Runs `SELECT 1`; `{status: ok\|degraded, database: bool, tasks}`. `tasks` maps each Celery task to its latest recorded run (`{state, finished_at}`) from `mi__task_runs`. |

## Repositories — `/api/repositories`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/repositories` | List stored repos. `order_by` is one of `-relevance_score` (default), `-stars`, `-created_at`, `name` — anything else is a `422`. Returns `Page<RepositoryRead>`. |
| GET | `/api/repositories/{repository_id}` | One repo by UUID. `404` if missing/deleted. |
| GET | `/api/repositories/{repository_id}/momentum` | Star-growth from snapshot history: snapshots in the window (oldest first) + newest-minus-oldest `stars_delta` / `velocity_delta`. `days` 1–365, default 30. |
| POST | `/api/repositories/search` | **Live** GitHub query with explicit filters; ranked results, **not persisted**. |

`POST /search` body (`RepositorySearch`): `languages[]`, `keywords[]`, `min_stars`,
`max_results`. Persisted rows carry metric history — see `RepositorySnapshot` in
[data-model.md](data-model.md).

## Libraries — `/api/libraries`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/libraries` | List monitored libraries (ordered by name). `Page<LibraryRead>`. |
| GET | `/api/libraries/{library_id}` | The library's **release history**, newest first (`list<LibraryReleaseRead>`). |
| POST | `/api/libraries` | Add to monitoring. Body: `{ecosystem: pypi\|npm, name}`. Idempotent on `ecosystem:name`; re-adding a removed one un-deletes it. `201`. |
| DELETE | `/api/libraries/{library_id}` | Stop monitoring (**soft delete** — release history is kept). `204`. |

The `collect_releases` task also seeds this set from `preferences.monitored_libraries`.

## Articles — `/api/articles`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/articles` | List collected articles, newest first. `Page<ArticleRead>`. |
| GET | `/api/articles/search` | `q` (required) case-insensitive substring over title/author/content; `limit` 1–200. Returns a plain list. |
| GET | `/api/articles/{article_id}` | One article by UUID. `404` if missing/deleted. |

> `/search` is declared before `/{article_id}` so the literal isn't parsed as a UUID.

## Curation — `/api/curation`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/curation` | List curation results, most important first. Optional `status=pending\|approved\|rejected\|failed`. `Page<CurationRead>`. |
| GET | `/api/curation/stats` | Aggregate counts: `{total, pending, approved, rejected, failed}`. |
| PUT | `/api/curation/{curation_id}/review` | Approve/reject. Body: `{status, reviewed_by}`. Stamps `reviewed_at`. `404` if missing. |

> `/stats` is declared before `/{curation_id}` for the same reason as above.

## Feed — `/api/feed`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/feed` | Unified curated feed across repos, releases, and articles — curation joined with a render of its item (`title`, `url`), most important first. `Page<FeedItem>`. |

Filters: `status` (default **pending + approved**; `rejected`/`failed` only when
asked for explicitly), `item_type`, `min_score` (0–1), `tag` (exact lowercase
match), `since` (curated on/after). Orphan curations (item hard-deleted) are
silently skipped.

Ordering: `order_by` — one of `-importance_score` (default), `-reviewed_at`,
`-created_at`; anything else is `422`. `?status=approved&order_by=-reviewed_at`
is the "recently approved" list the review UI renders under the feed.

## Tasks — `/api/tasks`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/tasks` | The triggerable tasks: short name → full Celery name. |
| POST | `/api/tasks/{task_name}/trigger` | Enqueue a periodic task **now** (HTTP face of `make trigger`). Whitelisted short names only (`collect_trending`, `collect_releases`, `collect_articles`, `curate_uncurated`, `recurate_all`); `404` otherwise. Returns `202 {task, task_id, state: queued}` — the outcome lands in `/status`. |

> `recurate_all` re-runs curation over **every** existing row with the current
> prompt/logic (also the retry path for `failed` rows) — use after prompt changes.

## Configuration — `/api/config`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/config/preferences` | Read the technical profile (creates an empty singleton on first read). |
| PUT | `/api/config/preferences` | **Partial** update (only provided fields change): `stacks`, `areas`, `keywords`, `monitored_libraries`. |
| POST | `/api/config/sources` | Set collector toggles. Body: `{enabled_sources: {github: true, articles: false, ...}}`. Keys are validated — anything outside `github`/`packages`/`articles` is a `422`. |

Field routing (also documented in the OpenAPI schema): `stacks` → GitHub language
filters · `keywords` → GitHub search + article tags · `areas` → article tags ·
`monitored_libraries` → `ecosystem:name` release polling.

These edits change what the next collector run searches for and which collectors
run — see [flows.md](flows.md#steering-future-runs).
