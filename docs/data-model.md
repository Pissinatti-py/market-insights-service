# Data model

All tables use the `mi__` prefix and a UUID primary key. Conventions
(`src/db/mixins.py`):

- **TimestampMixin** — `created_at` / `updated_at`.
- **SoftDeleteMixin** — `deleted_at`; "deleting" sets the timestamp, and every
  list/detail query filters `deleted_at IS NULL`. Snapshot/release/curation
  tables are append-only and do **not** carry it.
- **`dedup_key`** — a unique string per row that makes collector upserts
  idempotent (`INSERT … ON CONFLICT (dedup_key) DO NOTHING`).

```
mi__preferences (singleton)
      │ steers every collector + the curation agent
      ▼
mi__repositories ─1:N─ mi__repository_snapshots     (metric history)
mi__libraries    ─1:N─ mi__library_releases         (release history)
mi__articles
      │
      ▼  (item_type, item_id)  — polymorphic, no FK
mi__curation
```

## mi__preferences — the technical profile (singleton)

One row, fixed id `00000000-…-0001`. Read by every collector to build queries and
by the curation agent to score relevance. Edited via `/api/config/*`.

| Column | Type | Meaning |
|---|---|---|
| `stacks` | JSONB list | Languages/stacks → GitHub `languages`, curation context. |
| `areas` | JSONB list | Domains of interest → article tags, curation context. |
| `keywords` | JSONB list | Free-text terms → GitHub keywords + article tags. |
| `monitored_libraries` | JSONB list | `"ecosystem:name"` strings seeded into `mi__libraries`. |
| `enabled_sources` | JSONB dict | Per-collector on/off, e.g. `{"github": true, "articles": false}`. Missing key ⇒ enabled. |

## mi__repositories + mi__repository_snapshots

`Repository` — a trending GitHub repo, stored once (`dedup_key = owner/name`).

Key columns: `url`, `owner`, `name`, `description`, `stars`, `forks`,
`stars_per_week`, `relevance_score` (indexed — list default sort),
`languages`/`topics` (JSONB), `repo_created_at`, `collected_at`.

`RepositorySnapshot` — append-only point-in-time reading (`stars`,
`stars_per_week`, `relevance_score`, `captured_at`), one per inserted repo per
collector run, cascade-deleted with the repo. Drives star-growth/relevance charts.

## mi__libraries + mi__library_releases

`Library` — a monitored package (`dedup_key = ecosystem:name`, ecosystem ∈
`pypi|npm`). Tracks `current_version` and `is_monitored` (soft-deletable).

`LibraryRelease` — one recorded release (`dedup_key = ecosystem:name:version`):
`previous_version`, `new_version`, `is_major` (major bump / likely breaking),
`release_notes`, `released_at`. Cascade-deleted with the library.

## mi__articles

A collected article/post, stored once (`dedup_key` = hash of `source + url`).
`source` ∈ `devto|medium|rss|linkedin|hackernews`. Columns: `title`, `author`, `url`,
`content` (excerpt/summary — full bodies are not fetched), `likes`, `comments`,
`published_at`. Soft-deletable.

## mi__curation

An LLM analysis of one collected item. **Polymorphic**: `(item_type, item_id)`
points into one of the three source tables — there is deliberately no DB-level FK.

| Column | Type | Meaning |
|---|---|---|
| `item_type` | enum | `repository \| library_release \| article`. |
| `item_id` | UUID | The analysed row's id (in the matching table). |
| `summary` | text | One–two sentence LLM summary. |
| `tags` | JSONB list | Short lowercase technology/topic tags. |
| `importance_score` | Numeric(4,3) | 0–1 relevance to the profile (indexed — list default sort). |
| `status` | enum | `pending \| approved \| rejected` (manual review). |
| `reviewed_by` | text | Who reviewed it. |
| `model` | text | Ollama model that produced it. |
| `confidence` | Numeric(4,3) | Baseline `0.85` for LLM rows; a "needs review" filter can target anything below. |
| `raw_llm_output` | JSONB | The unmodified model JSON, kept for debugging. |

**`UNIQUE(item_type, item_id)`** — an item is curated at most once; this is what
makes `curate_uncurated` idempotent even under a concurrent double-run.

## mi__task_runs

One row per completed Celery task execution, written automatically by the
`task_postrun` signal (`src/core/celery/task_runs.py`) — success or failure, no
per-task code. `/status` surfaces the latest row per `task_name`. Columns:
`task_name`, `state` (Celery's state string, e.g. `SUCCESS|FAILURE|RETRY`),
`result` (JSONB, the task's return dict), `error`, `finished_at`. Append-only.
