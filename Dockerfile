# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

# ---- Stage 1: build the frontend (SCSS + vendor copy + JS bundle) ----
# Keep the readable tag beside the authoritative multi-arch index digest; Dependabot maintains these pins.
FROM node:26-slim@sha256:193fe51b64e77981119c98c2002c9e32a70e2f006fb4d25068ce0558998917f0 AS frontend
WORKDIR /app
COPY itambox/package.json itambox/package-lock.json ./
RUN npm ci
COPY itambox/ ./
RUN npm run build:all


# ---- Stage 2: resolve the exact production Python environment ----
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS python-deps

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc libldap2-dev libsasl2-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project


# ---- Stage 3: build documentation from its locked dependency group ----
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS docs

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --only-group docs --no-install-project
COPY itambox/mkdocs.yml ./itambox/mkdocs.yml
COPY itambox/docs/ ./itambox/docs/
WORKDIR /app/itambox
RUN /app/.venv/bin/mkdocs build --strict


# ---- Stage 4: minimal runtime image ----
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ARG ITAMBOX_VERSION=dev
ARG ITAMBOX_REVISION=unknown
ARG ITAMBOX_SOURCE=https://github.com/itambox/itambox-webapp

LABEL org.opencontainers.image.title="ITAMbox" \
      org.opencontainers.image.description="Open-source IT asset management" \
      org.opencontainers.image.version="${ITAMBOX_VERSION}" \
      org.opencontainers.image.revision="${ITAMBOX_REVISION}" \
      org.opencontainers.image.source="${ITAMBOX_SOURCE}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ITAMBOX_ENV=prod \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Runtime tools and libraries for PostgreSQL, LDAP, SAML/xmlsec, and libmagic.
# openssl, libssl3, tzdata, and libpcre2-8-0 are named explicitly so this
# install upgrades them to the current bookworm-security builds; the slim base
# image ships the builds from its own build time, and a plain install would
# leave them in place.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates openssl libssl3 postgresql-client libldap-2.5-0 libsasl2-2 xmlsec1 tzdata libmagic1 libpcre2-8-0 \
    && rm -rf /var/lib/apt/lists/*

# collectstatic, gunicorn, qcluster, and the health check all run from the locked
# /app/.venv and never install packages. Remove the unused global pip package and
# its ensurepip bootstrap wheel from the final image only.
RUN set -eu; \
    test -x /usr/local/bin/python3.12; \
    if /usr/local/bin/python3.12 -c "import pip" 2>/dev/null; then \
        /usr/local/bin/python3.12 -m pip uninstall --yes pip; \
    fi; \
    rm -rf /usr/local/lib/python3.12/ensurepip

COPY --from=python-deps /app/.venv /app/.venv
COPY itambox/ .
# License and attribution notices ship with the distributed application so the
# bundled third-party components (Apache ECharts, zrender, d3-derived parts)
# stay attributed in every runtime image.
COPY NOTICE LICENSE ./
COPY --from=frontend /app/static/dist ./static/dist
COPY --from=docs /app/itambox/static/docs ./static/docs

# Verify the complete runtime filesystem, including the copied venv, cannot
# import or invoke pip before any application command runs.
RUN set -eu; \
    test -x /usr/local/bin/python3.12; \
    test -x /app/.venv/bin/python; \
    if /usr/local/bin/python3.12 -c "import pip" 2>/dev/null; then \
        echo "global pip module survived runtime-image hardening" >&2; \
        exit 1; \
    fi; \
    if /usr/local/bin/python3.12 -c "import ensurepip" 2>/dev/null; then \
        echo "ensurepip survived runtime-image hardening" >&2; \
        exit 1; \
    fi; \
    if /app/.venv/bin/python -c "import pip" 2>/dev/null; then \
        echo "runtime venv contains pip" >&2; \
        exit 1; \
    fi; \
    for pip_path in /app/.venv/bin/pip /app/.venv/bin/pip3 /app/.venv/bin/pip3.12; do \
        if [ -e "$pip_path" ]; then \
            echo "runtime venv contains pip entry point: $pip_path" >&2; \
            exit 1; \
        fi; \
    done; \
    for pip_command in pip pip3 pip3.12; do \
        if command -v "$pip_command" >/dev/null 2>&1; then \
            echo "runtime PATH exposes pip entry point: $pip_command" >&2; \
            exit 1; \
        fi; \
    done

# Collect static assets at build time. No database access is required, but prod
# settings reject missing secrets, so generate throwaway build-only values per
# build: a source-known literal would be readable via `docker history` AND would
# satisfy the structural contract, which must never legitimize a published key.
# These values never enter the runtime environment (RUN-scope only).
RUN ITAMBOX_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    ITAMBOX_DB_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
    python manage.py collectstatic --noinput

# Drop privileges.
RUN useradd --system --uid 1001 --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Web server. The background worker (django-q2) runs from the same image with
# `python manage.py qcluster` — see docker-compose.yml.
CMD ["gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
