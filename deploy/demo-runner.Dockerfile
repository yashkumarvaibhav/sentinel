# The browser-facing gateway cannot execute scenarios. This lab-side image can,
# and contains exactly the extra client a live run needs: kubectl pinned to the
# testbed's Kubernetes minor. It still has no host Docker socket.
FROM registry.k8s.io/kubectl:v1.31.5 AS kubectl

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY platform/pyproject.toml platform/uv.lock ./
RUN uv sync --extra ingest --extra storage --extra detection --extra context \
    --no-dev --no-install-project

COPY platform/ ./
COPY config/ ./config/
COPY --from=kubectl /bin/kubectl /usr/local/bin/kubectl
RUN uv sync --extra ingest --extra storage --extra detection --extra context --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    SENTINEL_CONFIG_DIR=/app/config \
    KUBECONFIG=/tmp/sentinel-kubeconfig

RUN useradd --system --uid 10001 sentinel && chown -R sentinel:sentinel /app
USER sentinel

CMD ["python", "-m", "lab.runner", "--repo-root", "/app"]
