FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for asyncpg/psycopg2 build wheels are avoided by using binary
# wheels; keep the image lean. pyproject declares packages=["app"], so the
# package sources must be present BEFORE `pip install .` (a pyproject-only
# layer fails package discovery).
COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

COPY . .

EXPOSE 8000

# Binds Railway's injected $PORT (8000 locally). Migrations do NOT run by
# default: on the shared production database, schema is owned by mhn-spring's
# Flyway (db/flyway/V6__davi_ai_tables.sql) — running Alembic there would
# create tables outside Flyway's bookkeeping. For a STANDALONE/test database
# (e.g. a fresh Railway Postgres), set RUN_MIGRATIONS_ON_START=true and the
# Davi tables are created via the local Alembic chain (davi_alembic_version).
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS_ON_START\" = \"true\" ]; then alembic upgrade head; fi && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
