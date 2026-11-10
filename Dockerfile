# Build stage: install the locked dependencies into a virtual environment. --only-binary makes the
# build fail loudly if a dependency ever needs compiling, instead of silently requiring a compiler.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

RUN python -m venv /opt/venv
COPY requirements.lock ./
# Every package, transitive ones included, is pinned and checked against its hash (requirements.lock).
# Downloads go to a BuildKit cache mount: reused by later builds, never part of an image layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock

# Runtime stage: the virtual environment and the application only (no compiler, no tests), run as
# an unprivileged user that cannot modify the code.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN useradd --system --uid 10001 --user-group --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY app ./app
# Local storage (STORAGE_BACKEND=local) is the only place the app writes.
RUN mkdir -p /app/data && chown app:app /app/data

# Production unless told otherwise: the API refuses to start with the public development keys.
ENV APP_ENV=production

USER app
EXPOSE 8000

# Readiness, not just liveness: unhealthy while the database, storage or queue is unreachable.
# Python, because the slim image has no curl.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status == 200 else 1)"]

# Migrations are a separate step (the compose "migrate" services), so replicas never race to run them.
# On SIGTERM uvicorn stops accepting connections and gives open requests 30 s to finish; the app
# then drains storage writes (SHUTDOWN_DRAIN_SECONDS). Container stop grace periods must cover both.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--timeout-graceful-shutdown", "30"]
