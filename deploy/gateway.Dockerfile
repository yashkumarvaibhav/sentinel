# API gateway. Build context is the repository root.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Bytecode precompilation is left off deliberately: this host hands containers
# a million-descriptor rlimit, and the compile step exhausts descriptors trying
# to fan out across it.
ENV UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing platform code does not re-resolve the world.
COPY platform/pyproject.toml platform/uv.lock ./
RUN uv sync --extra api --extra storage --no-dev --no-install-project

COPY platform/ ./
COPY config/ ./config/
COPY docs/reports/latest-score-proof.json ./reports/latest-score-proof.json
RUN uv sync --extra api --extra storage --no-dev

# Stamped at build time so /api/version can name the commit that is running.
ARG SENTINEL_VERSION=0.1.0
ARG SENTINEL_GIT_SHA=unknown
ARG SENTINEL_BUILT_AT=unknown
ENV SENTINEL_VERSION=${SENTINEL_VERSION} \
    SENTINEL_GIT_SHA=${SENTINEL_GIT_SHA} \
    SENTINEL_BUILT_AT=${SENTINEL_BUILT_AT} \
    PATH="/app/.venv/bin:${PATH}"

RUN useradd --system --uid 10001 sentinel && chown -R sentinel:sentinel /app
USER sentinel

EXPOSE 8040
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8040", "--no-server-header"]
