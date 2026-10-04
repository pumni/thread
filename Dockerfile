FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM python:3.14.7-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    PATH="/app/.venv/bin:$PATH" \
    THREADS_PLATFORM_HTTP_HOST=0.0.0.0 \
    THREADS_PLATFORM_HTTP_PORT=8000 \
    THREADS_PLATFORM_TLS_CERTFILE=/var/lib/threads/controller-tls-serving/leaf-fullchain.pem \
    THREADS_PLATFORM_TLS_KEYFILE=/var/lib/threads/controller-tls-serving/leaf-key.pem

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /var/lib/threads/controller-tls-admin /var/lib/threads/controller-tls-serving \
    && chown -R app:app /var/lib/threads \
    && chmod 0700 /var/lib/threads/controller-tls-admin /var/lib/threads/controller-tls-serving

WORKDIR /app

COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app migrations ./migrations

USER app

EXPOSE 8000

CMD ["sh", "-c", "test -r \"${THREADS_PLATFORM_TLS_CERTFILE}\" && test -r \"${THREADS_PLATFORM_TLS_KEYFILE}\" && exec uvicorn threads_platform.app:app --host \"${THREADS_PLATFORM_HTTP_HOST:-0.0.0.0}\" --port \"${THREADS_PLATFORM_HTTP_PORT:-8000}\" --ssl-certfile \"${THREADS_PLATFORM_TLS_CERTFILE}\" --ssl-keyfile \"${THREADS_PLATFORM_TLS_KEYFILE}\" --no-proxy-headers --no-access-log"]
