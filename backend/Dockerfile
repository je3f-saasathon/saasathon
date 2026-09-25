FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# git: the SRE worker clones monitored repos and pushes fix branches.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

# --- dev: runserver, bind-mount friendly, auto-reloads on file changes ---
FROM base AS dev

ENV DJANGO_SETTINGS_MODULE=config.settings.dev

CMD ["uv", "run", "python", "manage.py", "runserver", "0.0.0.0:8000"]

# --- prod: gunicorn with uvicorn workers, no bind mount, no dev server ---
FROM base AS prod

ENV DJANGO_SETTINGS_MODULE=config.settings.prod

RUN uv run python manage.py collectstatic --noinput

CMD ["uv", "run", "gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--worker-class", "uvicorn.workers.UvicornWorker"]
