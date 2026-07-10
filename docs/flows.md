# Main flows

Four periodic tasks do the real work. They share three invariants:

- **Profile-driven** — every one reads the singleton `preferences` row first.
- **Source-gated** — each honours `enabled_sources` and skips (returns zeros) when off.
- **Idempotent** — writes go through `bulk_upsert_dedup` (`INSERT … ON CONFLICT DO
  NOTHING` on `dedup_key`), so re-running a task never duplicates rows.

Retriable failures raise `CollectorRetriable` and the task auto-retries with
exponential backoff (`max_retries=3`); terminal problems raise `CollectorTerminal`
and are skipped/logged without retrying. Schedules live in
`src/core/celery/schedules.py`.

---

## 1. Trending repositories — `collect_trending`

`src/tasks/github_tasks.py` · schedule: **every 6h**

1. Compute a recency cutoff (`pushed_since` = 30 days ago).
2. Read profile; skip if `enabled_sources["github"]` is `False`.
3. `github.fetch_trending(languages=stacks, keywords, pushed_since)` queries the
   GitHub Search API and returns ranked `RepositoryCreate` rows.
4. `bulk_upsert_dedup` inserts new repos (dedup on `owner/name`).
5. For each **freshly inserted** repo, append a `RepositorySnapshot`
   (stars / stars-per-week / relevance) so growth can be charted over time.

Returns `{"fetched": N, "inserted": M}`.

> On-demand variant: `POST /api/repositories/search` runs the same GitHub query
> live with explicit filters and returns ranked results **without persisting** —
> useful for probing without waiting for the schedule.

## 2. Library releases — `collect_releases`

`src/tasks/packages_tasks.py` · schedule: **daily 12:00**

The monitoring set is the `mi__libraries` rows flagged `is_monitored`. It is fed
from two places, unified here:

- `preferences.monitored_libraries` — `"ecosystem:name"` strings, seeded into
  `mi__libraries` at the top of each run (`_seed_from_preferences`).
- Explicit `POST /api/libraries` calls.

For each monitored library:

1. `packages.fetch_latest(ecosystem, name)` hits PyPI or the npm registry.
   A single bad package (404/renamed → `CollectorTerminal`) is skipped, not fatal.
2. If the latest version differs from the stored `current_version`, write a
   deduped `LibraryRelease` (dedup on `ecosystem:name:version`), flag `is_major`
   on a major bump, and advance `current_version`.

Returns `{"checked": N, "new_releases": M}`.

## 3. Articles — `collect_articles`

`src/tasks/articles_tasks.py` · schedule: **every 12h**

1. Build the tag set from `keywords + areas` (de-duplicated).
2. `articles.fetch_devto(tags)` — Dev.to API (terminal error → logged, run continues).
3. `articles.fetch_hackernews(tags)` — Algolia HN search API, free/no-auth
   (terminal error → logged, run continues). Text posts link to the HN item.
4. `articles.fetch_rss(medium_feed(tag))` for each tag — Medium per-tag RSS.
   RSS parsing never raises; a dead feed just yields `[]`.
5. `bulk_upsert_dedup` on `Article` (dedup on `source + url`).

Returns `{"fetched": N, "inserted": M}`.

### LinkedIn — `collect_linkedin` (best-effort, off by default)

A **separate** task, scheduled only when `ENABLE_LINKEDIN` is set. `linkedin.fetch_posts()`
is a no-op stub today, and the task never raises — it can't affect the other
sources or the beat loop. LinkedIn has no stable public API and scraping violates
its ToS; the Dev.to/Medium collector already covers the "articles & insights"
requirement. Upgrade path: the official LinkedIn Marketing API.

## 4. AI curation — `curate_uncurated`

`src/tasks/curation_tasks.py` + `src/services/agents/curation_agent.py` · schedule: **daily 02:00**

Curates a batch (`CURATION_BATCH_SIZE`, default 25) across all three item types
(repositories, library releases, articles):

1. Read profile → `{stacks, areas, keywords}`.
2. For each type, select rows with **no** curation row yet (`item.id NOT IN
   (SELECT item_id FROM mi__curation WHERE item_type = …)`), up to the remaining budget.
3. Render the item to a compact text string and call `curation_agent.curate(text, profile)`:
   - POSTs to Ollama `/api/chat` in JSON mode (`format: json`, `temperature: 0.1`).
   - Ollama down / timeout / 5xx → `CollectorRetriable` (task retries).
   - Empty or non-JSON content → `CollectorTerminal` (item skipped).
   - The raw JSON is validated through `CurationCreate`; invalid output →
     `CollectorTerminal`, **no row written**. The LLM never writes unchecked data.
4. Persist a `Curation` row: `summary`, `tags`, `importance_score`, `status=pending`,
   the `model` name, a `confidence` baseline (`0.85`), and the `raw_llm_output`
   (kept in JSONB for debugging).

Returns `{"curated": N, "skipped": M}` (skipped = invalid LLM output).

### Idempotency, two ways

- Only **uncurated** items are selected, so a normal re-run has nothing to do.
- The `(item_type, item_id)` unique constraint on `mi__curation` makes even a
  concurrent double-run a no-op at the DB level.

---

## Human review loop

Curation rows land as `pending`. A reviewer:

- `GET /api/curation?status=pending` — worklist, most important first.
  Optionally add `&item_type=repository|library_release|article` to narrow to one
  entity type (combines with `status`).
- `GET /api/curation/stats` — counts by status.
- `PUT /api/curation/{id}/review` — set `approved` / `rejected` (+ `reviewed_by`).

The review status is advisory metadata on the curation row; it does not delete the
underlying signal.

## Steering future runs

Everything the collectors search for lives in the DB, edited through the API — no
redeploy needed:

- `PUT /api/config/preferences` — stacks, areas, keywords, monitored libraries.
- `POST /api/config/sources` — flip individual collectors on/off (`enabled_sources`).

Changes take effect on the next scheduled (or manually triggered) run.
