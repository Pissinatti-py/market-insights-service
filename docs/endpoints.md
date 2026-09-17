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
| GET | `/api/curation/calibration` | Does the score match your verdicts? `{reviewed, approved_mean_score, rejected_mean_score, separation, bands[]}` over reviewed, scored rows only. `separation` = approved mean − rejected mean; `null` until both sides exist. Per-band `approval_rate` is `null` for an empty band (not `0.0`). |
| PUT | `/api/curation/{curation_id}/review` | Approve/reject. Body: `{status, reviewed_by}`. Stamps `reviewed_at`. `404` if missing. |

> `/stats` and `/calibration` are declared before `/{curation_id}` for the same reason as above.

## Events — `/api/events`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/events` | Server-sent events (`text/event-stream`). One `data:` line per change — `{"kind": "curation", item_type, status}` for every curated item and `{"kind": "task", name, state, result}` for every finished task — relayed from the `mi:events` Redis channel; a `: ping` comment every 15 s keeps the connection alive. The review UI listens with `EventSource` and refreshes its counters/feed on each event. |

## Feed — `/api/feed`

| Method | Path | Notes |
|---|---|---|
| GET | `/api/feed` | Unified curated feed across repos, releases, and articles — curation joined with a render of its item (`title`, `url`), most important first. `Page<FeedItem>`. |
| GET | `/api/feed/digest` | The last `days` (default 7) of the same feed, best first — `format=markdown` renders it as a document. |

Filters: `status` (default **pending + approved**; `rejected`/`failed` only when
asked for explicitly), `item_type`, `min_score` (0–1), `tag` (exact lowercase
match), `since` (curated on/after). Orphan curations (item hard-deleted) are
silently skipped.

Ordering: `order_by` — one of `-importance_score` (default), `-reviewed_at`,
`-created_at`; anything else is `422`. `?status=approved&order_by=-reviewed_at`
is the "recently approved" list the review UI renders under the feed.

### Digest

`GET /api/feed/digest?days=7&min_score=&limit=20&format=json|markdown`

The period summary: pending + approved curations from the last `days` (1–90,
measured on curation time), ranked by importance, capped at `limit` (1–100).
`format=json` returns the same `Page<FeedItem>` as `/api/feed`, so clients need no
extra model; `format=markdown` returns `text/markdown` grouped under
`## Repositories / ## Releases / ## Articles` — paste-ready for a newsletter or a
weekly note.

```bash
curl "localhost:8003/api/feed/digest?days=7&min_score=0.6&format=markdown"
```

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
| GET | `/api/config/keyword-suggestions` | Candidate keywords mined from approved items' tags, minus what the profile already has, ranked by `net` (approvals − rejections, kept only when positive). `?limit=` default 15. Read-only — apply one with `PUT /api/config/preferences`. |

Field routing (also documented in the OpenAPI schema): `stacks` → GitHub language
filters · `keywords` → GitHub search + article tags · `areas` → article tags ·
`monitored_libraries` → `ecosystem:name` release polling.

Suggestions are never auto-applied: the scoring loop already learns from your reviews,
and letting it also rewrite the collectors' search terms would let the feed narrow with
nothing in the way.

These edits change what the next collector run searches for and which collectors
run — see [flows.md](flows.md#steering-future-runs).
