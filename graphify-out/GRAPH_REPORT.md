# Graph Report - .  (2026-07-02)

## Corpus Check
- Corpus is ~15,410 words - fits in a single context window. You may not need a graph.

## Summary
- 472 nodes · 1075 edges · 38 communities (34 shown, 4 thin omitted)
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 300 edges (avg confidence: 0.5)
- Token cost: 60,282 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_DB Base & Model Mixins|DB Base & Model Mixins]]
- [[_COMMUNITY_Articles API|Articles API]]
- [[_COMMUNITY_Docs & Deployment Stack|Docs & Deployment Stack]]
- [[_COMMUNITY_Base Manager Layer|Base Manager Layer]]
- [[_COMMUNITY_Config & Preferences API|Config & Preferences API]]
- [[_COMMUNITY_Curation Review API|Curation Review API]]
- [[_COMMUNITY_Libraries API|Libraries API]]
- [[_COMMUNITY_Health API & DB Session|Health API & DB Session]]
- [[_COMMUNITY_Repositories API|Repositories API]]
- [[_COMMUNITY_GitHub Collector|GitHub Collector]]
- [[_COMMUNITY_Ollama Curation Agent|Ollama Curation Agent]]
- [[_COMMUNITY_Collector Error Handling|Collector Error Handling]]
- [[_COMMUNITY_Package Release Collector|Package Release Collector]]
- [[_COMMUNITY_Upsert & Dedup|Upsert & Dedup]]
- [[_COMMUNITY_GitHub Collector Tests|GitHub Collector Tests]]
- [[_COMMUNITY_Logging Service|Logging Service]]
- [[_COMMUNITY_Package Tasks & Ecosystems|Package Tasks & Ecosystems]]
- [[_COMMUNITY_Curation Agent Tests|Curation Agent Tests]]
- [[_COMMUNITY_Settings & Config|Settings & Config]]
- [[_COMMUNITY_Alembic Migrations|Alembic Migrations]]
- [[_COMMUNITY_Celery App|Celery App]]
- [[_COMMUNITY_Celery Beat Schedules|Celery Beat Schedules]]
- [[_COMMUNITY_Docker Entrypoint|Docker Entrypoint]]

## God Nodes (most connected - your core abstractions)
1. `CollectorTerminal` - 33 edges
2. `BaseManager` - 33 edges
3. `CollectorRetriable` - 30 edges
4. `Base` - 29 edges
5. `Page` - 29 edges
6. `CurationStatus` - 25 edges
7. `Curation` - 25 edges
8. `Repository` - 25 edges
9. `LibraryRelease` - 21 edges
10. `TimestampMixin` - 19 edges

## Surprising Connections (you probably didn't know these)
- `AbstractEventLoop` --uses--> `Base`  [INFERRED]
  tests/integration/conftest.py → src/db/base.py
- `AsyncClient` --uses--> `Base`  [INFERRED]
  tests/integration/conftest.py → src/db/base.py
- `CI Pipeline` --semantically_similar_to--> `Operations`  [INFERRED] [semantically similar]
  .github/workflows/ci.yml → docs/operations.md
- `Response` --uses--> `CollectorRetriable`  [INFERRED]
  tests/unit/test_curation_agent.py → src/core/exceptions.py
- `Response` --uses--> `CollectorTerminal`  [INFERRED]
  tests/unit/test_curation_agent.py → src/core/exceptions.py

## Import Cycles
- 1-file cycle: `src/main.py -> src/main.py`
- 1-file cycle: `src/services/collectors/github.py -> src/services/collectors/github.py`
- 1-file cycle: `src/core/celery/schedules.py -> src/core/celery/schedules.py`
- 1-file cycle: `src/services/collectors/articles.py -> src/services/collectors/articles.py`

## Hyperedges (group relationships)
- **Profile-driven Collection Pipeline (beat schedules collectors; each reads the preferences singleton, honours enabled_sources, and upserts via dedup_key)** — docs_architecture_celery_beat, docs_flows_collect_trending, docs_flows_collect_releases, docs_flows_collect_articles, docs_flows_collect_linkedin, docs_data_model_mi_preferences, docs_architecture_bulk_upsert_dedup [EXTRACTED 1.00]
- **Curation and Human Review Flow (curate_uncurated feeds uncurated items through the curation agent to Ollama, writes pending mi__curation rows, humans approve/reject via the API)** — docs_flows_curate_uncurated, docs_architecture_curation_agent, docs_architecture_ollama, docs_data_model_mi_curation, docs_flows_human_review_loop, docs_endpoints_curation_api [EXTRACTED 1.00]
- **Five-container Compose Stack (api, celery_worker, celery_beat, postgres, redis; Ollama is the one external dependency outside compose)** — docs_architecture_fastapi_api, docs_architecture_celery_worker, docs_architecture_celery_beat, docs_architecture_postgres, docs_architecture_redis [EXTRACTED 1.00]

## Communities (38 total, 4 thin omitted)

### Community 0 - "DB Base & Model Mixins"
Cohesion: 0.08
Nodes (39): Base, CurationItemType, Base, Base class for all models., Adds ``deleted_at`` for non-destructive deletes (audit trail).      A row with `, Adds ``created_at`` / ``updated_at`` timestamp columns., SoftDeleteMixin, TimestampMixin (+31 more)

### Community 1 - "Articles API"
Cohesion: 0.08
Nodes (38): get_article(), list_articles(), List collected articles, newest first., # NOTE: declared before /{article_id} so "search" is not captured as an id., Search articles by topic/author/title (case-insensitive substring)., Get one article by id., search_articles(), ArticleRead (+30 more)

### Community 2 - "Docs & Deployment Stack"
Cohesion: 0.09
Nodes (45): Docker Compose Stack, Architecture, bulk_upsert_dedup (INSERT ON CONFLICT DO NOTHING), Celery Beat / RedBeat Scheduler, Celery Worker (market_insights_celery_worker), Curation Agent (curation_agent.py - only LLM call site), Dual DB Sessions (async API / sync Celery), FastAPI API Container (market_insights_api) (+37 more)

### Community 3 - "Base Manager Layer"
Cohesion: 0.11
Nodes (22): Any, ColumnElement, CreateSchemaType, LoaderOption, BaseManager, PaginatedResult, Generic async CRUD manager — a trimmed copy of pissync-core's BaseManager.  This, Get a single instance by a field value.          :param db: Database session. (+14 more)

### Community 4 - "Config & Preferences API"
Cohesion: 0.12
Nodes (30): configure_sources(), get_preferences(), Get the user's technical profile (creates an empty one on first read)., Update the technical profile (partial — only provided fields change)., Enable/disable individual collectors (e.g. ``{"github": true, "articles": false}, update_preferences(), _Empty, get_or_create_sync() (+22 more)

### Community 5 - "Curation Review API"
Cohesion: 0.16
Nodes (29): curation_stats(), list_curation(), List curation results, most important first, optionally filtered by status., # NOTE: before /{curation_id} so "stats" is not parsed as an id., Aggregate counts of curation rows by review status., Manually approve/reject a curation result., review_curation(), BaseModel (+21 more)

### Community 6 - "Libraries API"
Cohesion: 0.20
Nodes (25): add_library(), library_release_history(), list_libraries(), List monitored libraries., Get a library's release history, newest first., Add a library to monitoring (idempotent on ``ecosystem:name``)., Stop monitoring a library (soft delete — release history is preserved)., remove_library() (+17 more)

### Community 7 - "Health API & DB Session"
Cohesion: 0.08
Nodes (20): AbstractEventLoop, health(), Liveness probe — the process is up.      :return: A static OK payload.     :rtyp, Readiness probe — the database is reachable.      :return: ``{"status": "ok"|"de, status(), get_db_async_session(), FastAPI dependency yielding an async session, closed when the request ends., FastAPI (+12 more)

### Community 8 - "Repositories API"
Cohesion: 0.17
Nodes (20): get_repository(), list_repositories(), List stored trending repositories, most relevant first., Get one repository by id., Run a live GitHub search with explicit filters (does not persist).      Lets a c, search_repositories(), Repository data access. (Yes, the name is a pun the codebase has to live with.), RepositoryRepository (+12 more)

### Community 9 - "GitHub Collector"
Cohesion: 0.20
Nodes (15): build_query(), compute_relevance(), compute_stars_per_week(), fetch_trending(), _parse_dt(), parse_repo(), GitHub trending-repository collector.  Hits the GitHub Search API over plain htt, Map one GitHub Search API item to a validated :class:`RepositoryCreate`.      :p (+7 more)

### Community 10 - "Ollama Curation Agent"
Cohesion: 0.20
Nodes (14): base_confidence(), _call_ollama(), _coerce(), curate(), LLM curation agent.  Talks to a local **Ollama** daemon over plain HTTP (`/api/c, Validate the raw model output through :class:`CurationCreate`.      :param raw:, The baseline confidence stamped on LLM-derived curation rows., Build the per-item user prompt embedding the profile and the output contract. (+6 more)

### Community 11 - "Collector Error Handling"
Cohesion: 0.16
Nodes (13): fetch_devto(), Fetch recent Dev.to articles for the given tags.      :param tags: Topic tags (o, _raise_for_status(), Map a GitHub HTTP status onto the collector exception contract., CollectorError, CollectorRetriable, Shared collector exception hierarchy.  Every external-data collector raises one, Base for all collector failures. (+5 more)

### Community 12 - "Package Release Collector"
Cohesion: 0.22
Nodes (12): fetch_latest(), is_major_bump(), LatestRelease, _parse_npm(), _parse_pypi(), Library-release collector for PyPI and npm.  For each monitored library, fetch t, Extract the latest version + description from an npm registry payload., The latest version of a package, as read from its registry. (+4 more)

### Community 13 - "Upsert & Dedup"
Cohesion: 0.20
Nodes (10): bulk_upsert_dedup(), Idempotent bulk upsert keyed on ``dedup_key``.  The collector pattern from ``pis, Insert ``rows`` into ``model``, skipping any whose ``dedup_key`` already exists., Idempotency of the dedup upsert — re-running a collector inserts nothing new., _row(), test_upsert_is_idempotent_on_dedup_key(), Base, Session (+2 more)

### Community 14 - "GitHub Collector Tests"
Cohesion: 0.24
Nodes (3): _item(), test_fetch_trending_ranks_by_relevance(), test_parse_repo_maps_fields()

### Community 15 - "Logging Service"
Cohesion: 0.29
Nodes (6): Logger, ColoredFormatter, get_logger(), Custom formatter to add colors based on log level., Format a log record, wrapping it in the ANSI color for its level.          :para, Get a logger with the specified name, configured with a colored formatter.

### Community 16 - "Package Tasks & Ecosystems"
Cohesion: 0.32
Nodes (7): PackageEcosystem, Where a monitored library lives., collect_releases(), Library-release collector Celery task.  Monitoring set = the ``mi__libraries`` r, Ensure a ``mi__libraries`` row exists for each ``ecosystem:name`` in the profile, Check every monitored library for a new release.      :return: ``{"checked": int, _seed_from_preferences()

### Community 17 - "Curation Agent Tests"
Cohesion: 0.43
Nodes (5): Response, _ollama_response(), test_curate_rejects_non_json(), test_curate_rejects_out_of_range_score(), test_curate_validates_good_output()

### Community 19 - "Settings & Config"
Cohesion: 0.40
Nodes (3): BaseSettings, Parse ``ALLOWED_ORIGINS`` into a list of origins for CORS.          Accepts a JS, Settings

## Knowledge Gaps
- **9 isolated node(s):** `docker-entrypoint.sh script`, `CreateSchemaType`, `LoaderOption`, `UpdateSchemaType`, `AsyncSession` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `BaseManager` connect `Base Manager Layer` to `Articles API`, `Config & Preferences API`, `Curation Review API`, `Libraries API`, `Repositories API`?**
  _High betweenness centrality (0.133) - this node is a cross-community bridge._
- **Why does `Base` connect `DB Base & Model Mixins` to `Articles API`, `Config & Preferences API`, `Curation Review API`, `Libraries API`, `Health API & DB Session`, `Upsert & Dedup`, `Package Tasks & Ecosystems`, `Alembic Migrations`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Why does `CollectorTerminal` connect `Ollama Curation Agent` to `DB Base & Model Mixins`, `Articles API`, `GitHub Collector`, `Collector Error Handling`, `Package Release Collector`, `GitHub Collector Tests`, `Package Tasks & Ecosystems`, `Curation Agent Tests`, `Package Collector Tests`?**
  _High betweenness centrality (0.084) - this node is a cross-community bridge._
- **Are the 11 inferred relationships involving `CollectorTerminal` (e.g. with `ArticleSource` and `LatestRelease`) actually correct?**
  _`CollectorTerminal` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 14 inferred relationships involving `BaseManager` (e.g. with `ArticleRepository` and `_Empty`) actually correct?**
  _`BaseManager` has 14 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `CollectorRetriable` (e.g. with `ArticleSource` and `LatestRelease`) actually correct?**
  _`CollectorRetriable` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `Base` (e.g. with `AbstractEventLoop` and `Article`) actually correct?**
  _`Base` has 17 INFERRED edges - model-reasoned connections that need verification._