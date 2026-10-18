# Installation

This guide covers the source-built Docker Compose production stack. For a disposable evaluation with development settings and demo data, use "Evaluate from source" in the repository-root `README.md` instead.

ITAMbox ships as source and, starting with `v1.0.0-beta.2`, as an official container image published from each tagged release. Pin every deployment to a tagged release or a reviewed commit; do not deploy a moving branch without reviewing its migrations and changelog.

## Official container image

Each tagged release publishes the qualified build to the GitHub Container Registry:

```bash
docker pull ghcr.io/itambox-community/itambox-webapp:1.0.0-beta.4
```

The image is published as a multi-platform index for **linux/amd64** and **linux/arm64**; `docker pull` selects the variant matching the host platform, and `docker pull --platform linux/arm64` overrides that choice. Verify that the tag lists both platforms before pinning a digest:

```bash
docker buildx imagetools inspect ghcr.io/itambox-community/itambox-webapp:1.0.0-beta.4
```

The image is the exact build the release workflow scanned, archived, and SBOM-validated, and its registry digest carries build-provenance and SPDX-SBOM attestations:

```bash
gh attestation verify "oci://ghcr.io/itambox-community/itambox-webapp@<digest>" \
  --repo itambox-community/itambox-webapp \
  --signer-workflow itambox-community/itambox-webapp/.github/workflows/release.yml
```

The remaining sections build the stack from source, which stays the supported path for auditing or customizing the build.

The build bases and bundled PostgreSQL and Valkey services use reviewed
tag-and-digest pins. For the update procedure or managed-service override, see
the [release image policy](release-image-policy.md).

## Prerequisites

- Docker Engine 24 or newer with Docker Compose v2.24.4 or newer
- A DNS name and TLS-capable reverse proxy or ingress
- Durable storage for PostgreSQL and uploaded media
- A secrets manager or encrypted backup location

## 1. Clone and pin the source

```bash
git clone https://github.com/itambox-community/itambox-webapp.git
cd itambox-webapp
DEPLOY_REVISION='full-reviewed-commit-sha'
git checkout --detach "$DEPLOY_REVISION"
cp .env.example .env
```

## 2. Configure production values

The Compose file forces `ITAMBOX_ENV=prod` for the application and worker. Edit `.env` before building.

### 2.1 Critical production variables

These must be reviewed before the application can be operated safely in production. The classification distinguishes **startup-blocking** (the app refuses to start), **strongly recommended with a fallback** (the app starts, but on a development-grade default you must not keep), and **required for external access**.

| Variable | Classification |
|---|---|
| `ITAMBOX_SECRET_KEY` | Required — startup-blocking. Production enforces the full Django deployment-check contract: at least 50 characters, at least 5 distinct characters, and no `django-insecure-` prefix. |
| `ITAMBOX_FIELD_ENCRYPTION_KEYS` | Strongly recommended — fallback-supported when unset or blank. When unset/blank, the key is derived from `ITAMBOX_SECRET_KEY` (development convenience; rotating the secret then makes encrypted fields unreadable). An explicitly supplied **non-blank** value that is not a valid keyring **is startup-blocking**. |
| `ITAMBOX_API_TOKEN_PEPPERS` | Strongly recommended — fallback-supported when unset or blank. When unset/blank, token hashing falls back to a `SECRET_KEY`-derived pepper (loudly warned). An explicitly supplied **non-blank** value that is not a valid mapping **is startup-blocking** — malformed configuration never silently falls back. |
| `ITAMBOX_DB_NAME`, `ITAMBOX_DB_USER`, `ITAMBOX_DB_PASSWORD` | Required — PostgreSQL credentials. The production application requires an explicitly configured `ITAMBOX_DB_PASSWORD` (startup-blocking), and the bundled Compose stack fails closed before any container starts without one. |
| `ITAMBOX_ALLOWED_HOSTS` | Required for external access — unrecognized hosts receive HTTP 400. |
| `ITAMBOX_CSRF_TRUSTED_ORIGINS` | Required for cross-origin POST/PUT/PATCH/DELETE (scheme-qualified external origins). |
| `ITAMBOX_EMAIL_*` | Working SMTP settings for resets, alerts, and reports. Failure degrades notification delivery, not startup. |

Generate independent values rather than reusing one secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The last value can be placed in a pepper mapping such as `{"1":"<generated-value>"}`. Verify the JSON syntax before admitting users: an explicitly supplied value that is not a valid mapping aborts production startup instead of silently falling back to a `SECRET_KEY`-derived pepper.

!!! warning "Upgrade legacy field-encryption passphrases before deployment"
    Older revisions accepted arbitrary strings in `ITAMBOX_FIELD_ENCRYPTION_KEYS` and silently derived a Fernet key from each string. Current revisions fail closed unless every configured entry is already a valid Fernet key. Before upgrading an installation that used a passphrase, convert each existing entry to the exact key the older revision derived:

    ```bash
    read -rsp 'Legacy field-encryption passphrase: ' LEGACY_FIELD_KEY && printf '\n'
    LEGACY_FIELD_KEY="$LEGACY_FIELD_KEY" python -c "import base64, hashlib, os; print(base64.urlsafe_b64encode(hashlib.sha256(os.environ['LEGACY_FIELD_KEY'].encode()).digest()).decode())"
    unset LEGACY_FIELD_KEY
    ```

    Store the printed value as the corresponding entry in `ITAMBOX_FIELD_ENCRYPTION_KEYS`, then discard terminal scrollback according to your secrets-handling policy. Do **not** generate a replacement key for this migration: changing the key would make existing encrypted SMTP passwords, license keys, and webhook secrets unreadable. For a later controlled rotation, keep the converted old key in the comma-separated keyring until all values have been re-encrypted.

!!! danger "Upgrade a short legacy SECRET_KEY without losing data"
    Production now enforces Django's full deployment-check contract on `ITAMBOX_SECRET_KEY` (at least 50 characters, at least 5 distinct characters, no `django-insecure-` prefix). An existing deployment that predates the contract may run a shorter random key. **Do not rotate that key blindly**: while `ITAMBOX_FIELD_ENCRYPTION_KEYS` is unset, the field-encryption key is derived from `SECRET_KEY`, and while `ITAMBOX_API_TOKEN_PEPPERS` is unset, API tokens are hashed under a `SECRET_KEY`-derived pepper. Replacing the key destroys every encrypted field and invalidates every fallback-hashed token.

    To migrate data-preservingly:

    1. Pin the Fernet key derived from the current `SECRET_KEY` into `ITAMBOX_FIELD_ENCRYPTION_KEYS` first — the same one-liner as the legacy-passphrase conversion above, run with the current key in the environment:
       ```bash
       read -rsp 'Current ITAMBOX_SECRET_KEY: ' LEGACY_SECRET_KEY && printf '\n'
       LEGACY_FIELD_KEY="$LEGACY_SECRET_KEY" python -c "import base64, hashlib, os; print(base64.urlsafe_b64encode(hashlib.sha256(os.environ['LEGACY_FIELD_KEY'].encode()).digest()).decode())"
       unset LEGACY_SECRET_KEY
       ```
    2. Accept the cutover: rotate `ITAMBOX_SECRET_KEY` to a compliant value and configure a dedicated `ITAMBOX_API_TOKEN_PEPPERS` mapping in the same change. From this moment every token hashed under the old fallback pepper is invalid — that is expected and unavoidable (there is no automatic re-hash).
    3. Only **after** the cutover, issue replacement API tokens under the dedicated mapping, update consumers, and delete the old-scheme tokens. (Tokens created before the cutover would themselves be fallback-hashed and die with it, so replacements must come after.)
    4. Keep the pinned `ITAMBOX_FIELD_ENCRYPTION_KEYS` keyring forever (or rotate it separately and deliberately afterwards — never together with `SECRET_KEY`).

!!! danger "Back up the complete secret set"
    Back up `.env`, especially `ITAMBOX_SECRET_KEY`, `ITAMBOX_FIELD_ENCRYPTION_KEYS`, and `ITAMBOX_API_TOKEN_PEPPERS`, with the database and media. Losing the field-encryption keyring makes encrypted SMTP passwords, license keys, and webhook secrets unreadable. Losing token peppers invalidates existing API tokens.

### 2.2 Environment variable reference

The tables below document every variable available in `.env.example`, organized by category. Variables marked **prod-critical** must be reviewed or set before deploying to production.

---

#### 2.2.1 Environment selection

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_ENV` | Recommended | `prod` (when unset) | `dev` or `prod`. The settings loader fails closed: if neither this nor `ITAMBOX_DEBUG` is set, the app runs in production mode. Set to `dev` for local development. |
| `ITAMBOX_DEBUG` ⚠️ | Recommended | `False` (in prod) | Enable Django debug mode. **Never** set to `True` in production — exposes stack traces, settings, and environment variables to end users. |

---

#### 2.2.2 Public base URL

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_BASE_URL` | Recommended (prod) | *(empty)* | Fully qualified base URL used to build absolute URLs in QR code labels, email links, and outbound references. Must not have a trailing slash. In development, leave blank — the app uses the `itambox:<tag>` URI scheme by default. Example: `https://itam.example.com` |

---

#### 2.2.3 Core / security ⚠️ prod-critical

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_SECRET_KEY` ⚠️ | **Required** (prod) | *(insecure dev fallback)* | Django secret key used for cryptographic signing (sessions, CSRF tokens, password reset tokens). Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. Production startup rejects keys shorter than 50 characters, keys with fewer than 5 distinct characters, and any key starting with `django-insecure-`, with a secret-free diagnostic naming the failed rule. |
| `ITAMBOX_FIELD_ENCRYPTION_KEYS` ⚠️ | **Strongly recommended** (prod) | *(derived from SECRET_KEY)* | Valid Fernet key(s) that encrypt sensitive stored values (license keys, SMTP passwords, webhook secrets). Comma-separated; the **first** key encrypts, **all** keys decrypt. Append the old key when rotating. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Arbitrary passphrases are rejected; follow the legacy-passphrase upgrade procedure above before deploying over an older installation. When unset **or blank**, the key is derived from `ITAMBOX_SECRET_KEY` as a dev convenience — then rotating `SECRET_KEY` makes every encrypted field **permanently unreadable**. An explicitly supplied **non-blank** value that is not a valid keyring (including whitespace/separators only) **aborts production startup** naming the failing key index. Set a dedicated value and back it up with every database dump. |
| `ITAMBOX_API_TOKEN_PEPPERS` ⚠️ | **Strongly recommended** (prod) | *(derived from SECRET_KEY)* | Server-side peppers used to HMAC-hash API tokens at rest. Must be a JSON object whose numeric keys are rotation IDs and values are dedicated secrets of ≥50 characters. The highest ID peppers new tokens; older IDs keep existing tokens valid. Example: `{"1":"replace-with-a-random-secret-of-at-least-50-characters"}`. When **unset or blank**, falls back to a `SECRET_KEY`-derived pepper with a loud production warning — acceptable for development only; tokens hashed under the fallback stop validating once a dedicated mapping is configured (re-issue them **after** the cutover). An explicitly supplied **non-blank** value that is not a valid mapping **aborts production startup**; malformed configuration never silently falls back. |
| `ITAMBOX_ALLOWED_HOSTS` ⚠️ | **Required** (prod) | `localhost,127.0.0.1` | Comma-separated host/domain names the site may serve. Django's `ALLOWED_HOSTS` — requests with unrecognized `Host` headers receive HTTP 400. Always include `127.0.0.1` so the container health probe works. |
| `ITAMBOX_CSRF_TRUSTED_ORIGINS` ⚠️ | **Required** (prod) | *(empty)* | Origins trusted for cross-origin POST/PUT/PATCH/DELETE. Scheme-qualified (e.g. `https://itam.example.com`). Must match the external URL users access the site through. |

---

#### 2.2.4 HTTPS hardening

These are only applied when `ITAMBOX_ENV=prod` (or `ITAMBOX_DEBUG=False`). Leave SSL redirect on unless TLS is terminated upstream and you handle redirects there.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_SECURE_SSL_REDIRECT` | Optional | `True` | Redirect all HTTP requests to HTTPS. Set to `False` if TLS is terminated at a reverse proxy that already handles the redirect. |
| `ITAMBOX_HSTS_SECONDS` | Optional | `31536000` (1 year) | `Strict-Transport-Security` max-age in seconds. Browsers will refuse plain HTTP connections for this duration after the first HTTPS visit. |
| `ITAMBOX_HSTS_INCLUDE_SUBDOMAINS` | Optional | `True` | Apply HSTS policy to all subdomains, not just the apex domain. |
| `ITAMBOX_HSTS_PRELOAD` | Optional | `True` | Opt into browser HSTS preload lists. Only enable if you are committed to HTTPS permanently and can meet [hstspreload.org](https://hstspreload.org) requirements. |

---

#### 2.2.5 Logging

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_LOG_LEVEL` | Optional | `INFO` | Application log level. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. Use `WARNING` in production to reduce noise; `DEBUG` for troubleshooting. |

---

#### 2.2.6 Database ⚠️ prod-critical

PostgreSQL 15+ is required. The `btree_gist` extension must be available (see [section 3](#3-build-and-initialize)).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_DB_ENGINE` | Optional | `django.db.backends.postgresql` | Django database backend. Only PostgreSQL is supported in production. |
| `ITAMBOX_DB_NAME` ⚠️ | **Required** (prod) | `itambox` | PostgreSQL database name. |
| `ITAMBOX_DB_USER` ⚠️ | **Required** (prod) | `itambox` | PostgreSQL user with full DDL/DML privileges on the database. |
| `ITAMBOX_DB_PASSWORD` ⚠️ | **Required** (prod) | *(none — explicitly configured)* | Password for the database user. Production startup fails without an explicitly configured, non-empty value (the old `itambox` default is a development-only convenience of the dev settings). The bundled Compose stack interpolates fail-closed: `compose config`/`compose up` abort before any container starts when `ITAMBOX_DB_PASSWORD` is unset **or blank** (both states are covered by the smoke suite). For the bundled stack, set it **in `.env`** — the template ships a blank `ITAMBOX_DB_PASSWORD=` line that would otherwise shadow a shell-exported value in the app/worker containers. Externally managed PostgreSQL may deliberately use any value — "explicitly configured" is the contract, not a string comparison. |
| `ITAMBOX_DB_HOST` ⚠️ | **Required** (prod) | `localhost` | PostgreSQL host. Use the Compose service name (`db`) when using the bundled PostgreSQL container. |
| `ITAMBOX_DB_PORT` | Optional | `5432` | PostgreSQL port. |
| `ITAMBOX_DB_CONN_MAX_AGE` | Optional | `300` | Persistent connection lifetime in seconds. `0` closes connections after each request (not recommended for production). |
| `ITAMBOX_DB_SSLMODE` | Recommended | `require` when unset; `.env.example` ships `prefer` | PostgreSQL SSL mode. Valid values: `prefer`, `require`, `verify-full`. Use `require` or `verify-full` for managed/remote PostgreSQL; `prefer` (the shipped template default) is only suitable for a trusted private database network. |

---

#### 2.2.7 Cache / rate limiting

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_CACHE_BACKEND` | Recommended (prod) | `locmem` | Cache backend. `locmem` (per-process in-memory, not shared across workers) or `redis`. Use a Redis-protocol server in production (Valkey recommended — BSD-licensed fork of Redis) so throttles and rate-limit counters are shared across gunicorn workers. |
| `ITAMBOX_REDIS_URL` | Conditional | `redis://127.0.0.1:6379/1` | Redis connection URL. Required when `ITAMBOX_CACHE_BACKEND=redis`. |
| `ITAMBOX_CACHE_TIMEOUT` | Optional | `300` | Default cache TTL in seconds. |
| `RATELIMIT_CACHE` | Optional | `default` | Cache alias used for the authentication rate limiter. Keep this set to the defined `default` alias; configure a shared Redis/Valkey backend with `ITAMBOX_CACHE_BACKEND=redis` and `ITAMBOX_REDIS_URL`. |

---

#### 2.2.8 Email ⚠️ prod-critical

In development, the app uses a local SMTP catcher automatically. In production, configure working SMTP settings for password resets, alerts, notifications, and reports.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_EMAIL_BACKEND` | Optional | `django.core.mail.backends.smtp.EmailBackend` | Django email backend. Use `django.core.mail.backends.console.EmailBackend` for development debugging. |
| `ITAMBOX_EMAIL_HOST` ⚠️ | **Required** (prod) | `localhost` | SMTP server hostname. |
| `ITAMBOX_EMAIL_PORT` | Optional | `587` | SMTP server port. Common values: `587` (STARTTLS), `465` (SSL), `25` (unencrypted — avoid). |
| `ITAMBOX_EMAIL_HOST_USER` ⚠️ | **Required** (prod) | *(empty)* | SMTP authentication username. |
| `ITAMBOX_EMAIL_HOST_PASSWORD` ⚠️ | **Required** (prod) | *(empty)* | SMTP authentication password. This value is stored encrypted at rest via `ITAMBOX_FIELD_ENCRYPTION_KEYS` if configured in the admin UI; the environment variable itself should still be protected. |
| `ITAMBOX_EMAIL_USE_TLS` | Optional | `True` | Use STARTTLS (port 587). Mutually exclusive with `EMAIL_USE_SSL`. |
| `ITAMBOX_EMAIL_USE_SSL` | Optional | `False` | Use implicit SSL (port 465). Mutually exclusive with `EMAIL_USE_TLS`. |
| `ITAMBOX_DEFAULT_FROM_EMAIL` | Optional | `ITAMbox <no-reply@example.com>` | Default sender address for all outgoing emails. Override to a real address in production. |
| `ITAMBOX_SERVER_EMAIL` | Optional | *(same as DEFAULT_FROM_EMAIL)* | From-address for server error emails (500 errors) sent to `ADMINS`. |
| `ITAMBOX_EMAIL_TIMEOUT` | Optional | `10` | Outbound SMTP timeout in seconds. Bounds web requests and background tasks against a dead mail server. Django's own default would be no timeout at all — keep this set. |

---

#### 2.2.9 Application behavior

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_DEFAULT_CURRENCY` | Optional | `EUR` | ISO 4217 currency code used as the fallback for money display. Each tenant can override its own currency. |
| `ITAMBOX_PAGINATOR_COUNT_CAP` | Optional | `100000` | Upper bound for list-view row counters. Below the cap, exact counts are shown; above, displays `100000+`. Set to `0` for unbounded `COUNT(*)` on every list view — slow at large scale. |
| `ITAMBOX_SESSION_COOKIE_AGE` | Optional | `28800` (8 h) in prod; `1209600` (2 weeks) in dev | Interactive session lifetime in seconds. Production overrides the Django default to 8 hours; after this duration of inactivity, users must re-authenticate. |
| `ITAMBOX_DOCS_ROOT` | Optional | `BASE_DIR/docs` | Accepted setting for the documentation root. It is currently read into `DOCS_ROOT` only; the in-app help links resolve against the compiled MkDocs output under `BASE_DIR/static/docs`, so changing this variable does not relocate the served help pages. |
| `ITAMBOX_REQUIRE_MFA` | Optional | `True` (in prod) | Enforce TOTP multi-factor authentication for local-password logins by superusers and owner-admin roles. SSO/LDAP/SAML/OIDC logins always delegate MFA to the identity provider regardless of this setting. Set to `False` to disable for local accounts. |
| `ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS` | Optional | *(unset)* | Opt-in auto-approval for low-risk asset requests. JSON object with the supported keys `accessory` and/or `consumable`; each value is a non-negative integer quantity threshold. Example: `{"accessory": 3, "consumable": 5}`. When unset, no request is auto-approved and requests remain pending for manual approval. Invalid JSON, unknown keys, or non-integer/bool thresholds abort startup with an error; the deprecated `REQUISITION_AUTO_APPROVAL_THRESHOLDS` name is still accepted through 1.x with a startup deprecation warning and is removed in ITAMbox 2.0. |

---

#### 2.2.10 Compliance

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_REQUIRE_CUSTODY_SIGNIN` | Optional | `True` | Require a digital signature on custody receipt sign-off. When enabled, users must provide an electronic signature before custody transfers are accepted. |
| `ITAMBOX_ALLOW_GLOBAL_CUSTODY_TEMPLATES` | Optional | `True` | Allow custody templates that are not scoped to a specific tenant. When `False`, templates must be assigned to individual tenants. |

---

#### 2.2.11 Data retention

Set to `0` to keep records forever. See [data-retention.md](data-retention.md) for the complete retention policy and pruning schedule.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_CHANGELOG_RETENTION_DAYS` | Optional | `365` | Days to retain object change history (`ObjectChange` records). Pruned daily by the `prune_changelog` scheduled task. Per-tenant overrides and legal holds are set via `Tenant.changelog_retention_days`. |
| `ITAMBOX_ALERTLOG_RETENTION_DAYS` | Optional | `180` | Days to retain alert log entries. |
| `ITAMBOX_NOTIFICATION_RETENTION_DAYS` | Optional | `90` | Days to retain notification history. |
| `ITAMBOX_EVENT_RETENTION_DAYS` | Optional | `0` (retain indefinitely) | Days to retain event-stream rows (`extras.Event`). The event stream is **not** pruned by default; pruning starts only when a positive value is configured. |
| `ITAMBOX_QTASK_FAILED_RETENTION_DAYS` | Optional | `90` | Days to retain failed background task records. |
| `ITAMBOX_HEALTH_SCHEDULE_GRACE_SECONDS` | Optional | `900` | Seconds a registered schedule may be overdue before `operational_health` flags it. See [Operational health](operational-health.md). |
| `ITAMBOX_HEALTH_QUEUE_BACKLOG_MAX` | Optional | `1000` | Queued task count above which (together with the age threshold) the queue is reported as backed up. See [Operational health](operational-health.md). |
| `ITAMBOX_HEALTH_QUEUE_AGE_SECONDS` | Optional | `1800` | Age in seconds of the oldest pending queued task above which (together with the backlog threshold) the queue is reported as backed up. See [Operational health](operational-health.md). |
| `ITAMBOX_HEALTH_JOB_STUCK_SECONDS` | Optional | `3600` | Seconds a first-party `Job` may stay pending or running before it is reported as stuck. See [Operational health](operational-health.md). |
| `ITAMBOX_HEALTH_FAILURES_24H_MAX` | Optional | `10` | Failed background tasks in the last 24 hours above which failures are reported as degraded. See [Operational health](operational-health.md). |

---

#### 2.2.12 Static / media paths

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_STATIC_ROOT` | Optional | *(Django-derived)* | Filesystem path for collected static files. Override to serve static assets from a dedicated volume or CDN origin. The default Compose stack uses WhiteNoise from the application image; an override is only needed for external static serving. |
| `ITAMBOX_MEDIA_ROOT` | Optional | *(Django-derived)* | Filesystem path for user-uploaded media (attachments, label images, QR code exports). Must be on durable, backed-up storage. |

---

#### 2.2.13 Plugins

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_PLUGINS` | Optional | *(empty)* | Comma-separated importable plugin package names. Each package must be installed in the Python environment. Example: `example_plugin`. See `itambox/docs/plugins/` for the plugin development guide. |
| `ITAMBOX_PLUGINS_CONFIG` | Optional | `{}` | JSON object keyed by plugin package name; validated against each plugin's `settings_schema`. Malformed JSON or a non-object value stops startup. A single value (for example a secret) can instead be injected as `ITAMBOX_PLUGIN_<NAME>_<KEY>` (package name and key upper-cased, every character outside `A-Z0-9` replaced by `_`), which wins over the JSON value. See [plugin configuration](../plugins/api_reference.md#configuration). |

---

#### 2.2.14 CORS

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_CORS_ALLOW_ALL_ORIGINS` | Optional | `False` | Allow cross-origin requests from any origin. **Do not enable in production** — it bypasses the browser same-origin policy entirely. |
| `ITAMBOX_CORS_ALLOWED_ORIGINS` | Optional | *(empty)* | Comma-separated list of origins permitted for cross-origin API requests. Example: `https://portal.example.com,https://dashboard.example.com`. Required if a separate frontend or external service calls the ITAMbox API from a browser. |

---

#### 2.2.15 Multi-tenant SSO (LDAP / SAML / OIDC)

Each variable accepts a JSON object keyed by tenant slug. These configure per-tenant identity provider integration. See also the [SCIM provisioning guide](../integration/scim.md).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_TENANT_LDAP_CONFIGS` | Optional | *(empty)* | Per-tenant LDAP directory configuration. JSON object keyed by tenant slug. Each value is an object with keys: `SERVER_URI`, `BIND_DN`, `BIND_PASSWORD`, `USER_SEARCH_BASE`, `USER_SEARCH_FILTER`, and optional `REQUIRE_GROUP`. See `.env.example` for the full example. |
| `ITAMBOX_TENANT_SAML_CONFIGS` | Optional | *(empty)* | Per-tenant SAML 2.0 identity provider configuration. JSON object keyed by tenant slug. Uses `djangosaml2` / `pysaml2` configuration format. Each entry requires `entityid` and `metadata` (remote URL or inline XML). |
| `ITAMBOX_TENANT_OIDC_CONFIGS` | Optional | *(empty)* | Per-tenant OpenID Connect provider configuration. JSON object keyed by tenant slug. Each value includes the standard `mozilla-django-oidc` client, secret, authorization, token, userinfo and issuer settings, plus a JWKS endpoint or static IdP signing key for RS/ES tokens. |

!!! note "SSO configuration format"
    Each SSO variable is a single-line JSON string. Escape inner quotes or use a `.env`-compatible quoting strategy. Example for LDAP:
    ```
    ITAMBOX_TENANT_LDAP_CONFIGS='{"tenant-alpha": {"SERVER_URI": "ldap://ldap.alpha-corp.com", ...}}'
    ```

---

#### 2.2.16 Intune discovery

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ITAMBOX_TENANT_INTUNE_CONFIGS` | Optional | *(empty)* | Per-tenant Microsoft Intune device discovery configuration. JSON object keyed by tenant slug. Each value requires `azure_tenant_id`, `client_id`, and `client_secret` for Microsoft Graph API access. Optional keys use the parser's lowercase contract (including `create_missing`, `default_status`, and `sync_software`). See [discovery-sync.md](../integration/discovery-sync.md) for the full configuration and discovery workflow. |

---

## 3. Build and initialize

```bash
docker compose build
docker compose run --rm app python manage.py migrate
docker compose run --rm app python manage.py createsuperuser
```

The image builds frontend assets and runs `collectstatic`; WhiteNoise serves those assets from the application image. A separate static-file volume or web-server mapping is not required by the included stack.

Do not run `python manage.py seed_data` against production. Its demo dataset
contains public credentials and, without `--skip-drop`, clears domain data. For
an isolated development/demo run only, `ITAMBOX_SEED_PASSWORD` overrides the
password assigned to seeded demo users. Set any local value outside version
control and never reuse it in production. `--skip-drop` avoids the data-clear
path but does not make demo seed credentials suitable for production.

!!! warning "PostgreSQL `btree_gist` extension required"
    Migration `assets.0051` creates the `btree_gist` extension for reservation overlap constraints. The bundled PostgreSQL role can create it. For a managed or least-privilege external database, create the extension as an administrator before running migrations:

    ```sql
    CREATE EXTENSION IF NOT EXISTS btree_gist;
    ```

## 4. Terminate TLS at a reverse proxy

Production settings redirect HTTP to HTTPS and set secure-only session and CSRF cookies. Before starting the long-running services, route the external HTTPS host to the app's HTTP upstream on port 8000, preserve the `Host` header, and set `X-Forwarded-Proto: https`. Do not present `http://localhost:8000` as the user-facing URL.

Restrict the published port to the trusted proxy or firewall boundary; `docker-compose.yml` publishes port 8000 but does not provide TLS itself.

## 5. Verify

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 app worker
curl -I https://itam.example.com/health/
```

Replace the example host with the configured external URL, then sign in with the superuser created during initialization.

Next, establish a tested [backup and restore](backup-restore.md) process before loading production data. Follow the [upgrade guide](upgrades.md) for later source revisions.
