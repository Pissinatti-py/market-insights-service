COMPOSE = docker compose

.PHONY: lock up down logs migrate makemigrations downgrade shell format lint test cov trigger

lock:
	uv lock

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api

migrate:
	$(COMPOSE) exec api alembic upgrade head

makemigrations:
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

downgrade:
	$(COMPOSE) exec api alembic downgrade -1

shell:
	$(COMPOSE) exec api ipython

# Manually fire a collector/curation task, e.g. `make trigger t=src.tasks.github_tasks.collect_trending`
trigger:
	$(COMPOSE) exec api celery -A src.core.celery.celery_app call $(t)

format:
	uv run ruff check --fix src/ tests/
	uv run ruff format src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

test:
	uv run pytest -ra

cov:
	uv run pytest --cov=src --cov-report=term-missing --cov-report=html
