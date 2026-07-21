# Raw OTLP normalizer. Build context is the repository root.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY platform/pyproject.toml platform/uv.lock ./
RUN uv sync --extra ingest --extra storage --no-dev --no-install-project

COPY platform/ ./
COPY config/ ./config/
RUN uv sync --extra ingest --extra storage --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    SENTINEL_CONFIG_DIR=/app/config

RUN useradd --system --uid 10001 sentinel && chown -R sentinel:sentinel /app
USER sentinel

CMD ["python", "-m", "ingest"]
