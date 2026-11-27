# The app as one image: code, libraries and the built Olist database.
# The database is built here, at image build time, so it never enters the git repository.
# Secrets are never baked in: GROQ_API_KEY arrives at runtime (Cloud Run: from Secret Manager).

FROM ghcr.io/astral-sh/uv:0.9.2@sha256:6dbd7c42a9088083fa79e41431a579196a189bcee3ae68ba904ac2bf77765867 AS uv

FROM python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

WORKDIR /app

# Libraries first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY dbt ./dbt
COPY scripts ./scripts
COPY web ./web
COPY context ./context
RUN uv sync --locked --no-dev

# Download Olist, build the models and run the data tests. A failing test fails the build.
RUN .venv/bin/python scripts/load_olist.py \
 && DBT_PROFILES_DIR=dbt .venv/bin/dbt build --project-dir dbt \
 && rm -rf data/*.zip data/raw dbt/logs

# Run as an unprivileged user; the app opens the database read-only.
RUN useradd --uid 10001 --no-create-home app && chown -R app /app/dbt
USER app

ENV PATH="/app/.venv/bin:$PATH" DBT_LOG_PATH=/tmp/dbt-logs
EXPOSE 8080
# Cloud Run sets PORT; 8080 is the local default.
CMD ["sh", "-c", "exec uvicorn ttym.api:app --host 0.0.0.0 --port ${PORT:-8080} --no-server-header"]
