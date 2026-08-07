FROM python:3.13-slim

# No build toolchain: every dependency resolves to a cp313 manylinux wheel
# (psycopg2-binary and asyncpg bundle libpq), so nothing compiles from source.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# uv installs into the system Python (not a .venv) so the dev bind-mount doesn't
# shadow it and uvicorn/pytest/alembic resolve without `uv run`.
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./

# --frozen: install exactly uv.lock. Run `uv lock` once before the first build.
RUN uv sync --frozen

COPY . .

RUN chmod +x scripts/*.sh

EXPOSE 8003

ENV PYTHONPATH=/app

# No ENTRYPOINT in the image — the standalone compose opts into the migrate-on-boot
# bootstrap via scripts/docker-entrypoint.sh, keeping the image itself a plain
# uvicorn CMD so it can be embedded in another stack unchanged.
# No --reload here either: that is a dev-loop flag, and docker-compose.yml adds it
# back via its own `command:` for local work.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8003"]
