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

**Collection chains into curation.** When a collector inserts at least one new
row it fires `curate_uncurated` immediately (`celery_app.send_task`), so fresh
signals are scored within minutes instead of waiting for the daily sweep. The
02:00 schedule remains as a fallback drain. Chained double-fires are harmless —
curation is idempotent (see below).

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
3. `articles.fetch_hackernews(tags, since=cutoff)` — Algolia HN search API,
   free/no-auth, filtered server-side to stories newer than `ARTICLE_MAX_AGE_DAYS`
   (terminal error → logged, run continues). Text posts link to the HN item.
4. `articles.fetch_rss(medium_feed(tag))` for each tag — Medium per-tag RSS.
   RSS parsing never raises; a dead feed just yields `[]`.
5. **Age gate**: rows published before the cutoff are dropped (undated rows pass —
   the LLM caps stale items by date instead).
6. **Near-dup collapse**: every row carries a `title_fingerprint` (normalized-title
   hash); the same story syndicated across HN/Dev.to/Medium keeps only its
   highest-engagement copy, and fingerprints already stored are skipped entirely —
   no duplicate rows, no duplicate LLM calls.
7. `bulk_upsert_dedup` on `Article` (dedup on `source + url`).

Returns `{"fetched": N, "inserted": M}`.

## 4. AI curation — `curate_uncurated`

`src/tasks/curation_tasks.py` + `src/services/agents/curation_agent.py` ·
schedule: **chained after each collection** + daily 02:00 fallback sweep

Curates a batch (`CURATION_BATCH_SIZE`, default 50) **round-robin** across the
three item types (articles, repositories, library releases), newest first — no
type can starve the others:

1. Read profile → `{stacks, areas, keywords, monitored_libraries}`.
2. Read **review feedback** once per run: the most recently approved and rejected
   curations (see "Feeding review decisions back" below).
3. Interleave rows with **no** curation row yet (`item.id NOT IN
   (SELECT item_id FROM mi__curation WHERE item_type = …)`), one per type, up to the budget.
4. Enrich: articles get their full body fetched (`article_reader`, trafilatura,
   truncated to `ARTICLE_MAX_CHARS`) — best-effort, a fetch failure just drops the block.
5. Render the item (with published/created dates and engagement) and call
   `curation_agent.curate(text, profile, context, feedback)`:
   - POSTs to Ollama `/api/chat` constrained to a `{band, summary, tags}` JSON schema (band first, so
     the pick is not anchored by the model's own summary)
     (`temperature: 0.1`, `logprobs` on). The prompt carries the band rubric, anchor
     examples, your past review decisions, and today's date (stale news → `noise`).
   - **The score is a decision, not an invented number.** The model picks one closed
     band — `noise` 0.20 · `routine` 0.55 · `relevant` 0.80 · `must_see` 0.95 — and the
     score is those anchors weighted by the model's own token probability over the four
     bands at the band position (case-merged and renormalized; Ollama's alternatives are
     taken before the grammar mask, so non-band tokens are dropped). No usable logprobs →
     the picked band's anchor. `raw_llm_output` records `band_probs` and `score_source`
     (`logprobs` | `label`).
   - Ollama down / timeout / 5xx → `CollectorRetriable` (task retries).
   - Invalid output → `CollectorTerminal`: a **dead-letter row** is written
     (`status=failed`, error in `raw_llm_output`) so the item is never re-selected;
     `recurate_all` is its retry path.
6. Persist a `Curation` row: `summary`, `tags`, `importance_score` (the weighted band score), `status=pending`,
   the `model` name, and the `raw_llm_output` (kept in JSONB for debugging).

Returns `{"curated": N, "failed": M}` (failed = dead-lettered invalid output).

### Idempotency, two ways

- Only **uncurated** items are selected, so a normal re-run has nothing to do.
- The `(item_type, item_id)` unique constraint on `mi__curation` makes even a
  concurrent double-run a no-op at the DB level.

---

## Human review loop

Curation rows land as `pending` and **already show in the feed** (default =
pending + approved) — review is optional grooming, not a gate. A reviewer:

- `GET /` — the review UI: ranked feed, filters, approve/reject in one click.
- `GET /api/curation?status=pending` — worklist, most important first.
  Optionally add `&item_type=repository|library_release|article` to narrow to one
  entity type (combines with `status`).
- `GET /api/curation/stats` — counts by status (including `failed` dead-letters).
- `PUT /api/curation/{id}/review` — set `approved` / `rejected` (+ `reviewed_by`).

Reviewing never deletes the underlying signal — a rejected row keeps its item and
its curation, it just drops out of the default feed.

## Feeding review decisions back

Approve/reject is not only bookkeeping: it is the training signal for the next run.

**Into the score.** Each curation run loads the most recently reviewed rows
(`_FEEDBACK_EXAMPLES` per side, default 6) and passes them to the agent, which renders
them into the prompt as a `REVIEW FEEDBACK` block — the LLM's own summary, tags, and
score for each item, plus the verdict you gave it. The system prompt tells the model
this outranks the static anchor examples. Two guards:

- **Both sides or nothing.** Under 2 approved *or* under 2 rejected, the block is
  dropped entirely — one-sided feedback has no contrast and just pushes every score up.
- **No self-reference.** An item never receives its own past decision as an example,
  or `recurate_all` would re-score approved items against themselves and make the
  calibration report below meaningless.

**Into the score's credibility.** `GET /api/curation/calibration` reports how the
model's scores line up with your verdicts: mean score of approved vs. rejected, the
gap between them (`separation`), and approval rate per score band. Only reviewed,
scored rows count. Separation near zero means the score is not discriminating and the
feed is effectively unranked for you; it should widen as feedback accumulates. This is
the number to check before and after a `recurate_all`.

**Into what gets collected.** `GET /api/config/keyword-suggestions` mines tags the LLM
assigned to approved items, drops the ones already in your profile, and ranks the rest
by approvals minus rejections (a tag that appears equally in both discriminates
nothing). It is deliberately read-only — see below.

## Steering future runs

Everything the collectors search for lives in the DB, edited through the API — no
redeploy needed:

- `PUT /api/config/preferences` — stacks, areas, keywords, monitored libraries.
- `POST /api/config/sources` — flip individual collectors on/off (`enabled_sources`).

Changes take effect on the next scheduled (or manually triggered) run.

Keyword suggestions stop at *suggesting* on purpose. The scoring loop is already
self-reinforcing — examples come from decisions made under a prompt that used
examples — so letting it also rewrite the collectors' search terms would let the feed
narrow with nothing in the way. Applying a suggestion stays a deliberate
`PUT /api/config/preferences`. If `separation` climbs while the feed visibly gets
samey, that is the loop over-fitting; widen the profile by hand.
