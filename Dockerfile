FROM node:26.13.0-bookworm-slim@sha256:add5c17c14752c54d2f3d519ec46a3815684d41c33f198bcb09fb998f7dcbf68 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY app ./app
RUN uv sync --frozen --no-dev --no-editable \
    && chmod -R go-w /app/.venv

FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY --from=frontend /app/static ./app/static
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
# Ownership and mode are set here, not taken from the build context: the code stays root-owned
# and read-only for the runtime user, whatever the host's umask was. UID 10001 owns nothing on a
# volume, so the account's name is free to change without touching an existing install.
RUN chmod -R go-w /app/app /app/migrations /app/alembic.ini /app/scripts \
    && chmod +x /app/scripts/*.sh \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin healthos

USER healthos

HEALTHCHECK --interval=10s --timeout=6s --start-period=20s --retries=12 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('APP_PORT', '8000') + '/health/live', timeout=5).read()"

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${APP_HOST:-127.0.0.1} --port ${APP_PORT:-8000} --no-access-log --no-proxy-headers --timeout-graceful-shutdown 5"]
