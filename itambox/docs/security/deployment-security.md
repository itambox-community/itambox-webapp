# Deployment Security

This page collects the deployment-facing security controls an operator
configures, verifies, and maintains. Feature-specific guidance lives in the
linked pages; here you find the operational overview. The standalone
[SECURITY.md](https://github.com/itambox/itambox-webapp/blob/main/SECURITY.md)
covers vulnerability reporting — **do not report vulnerabilities through
public GitHub issues**; contact [security@itambox.dev](mailto:security@itambox.dev).

## Secrets and key management

| Secret | Where it is used | If lost or compromised |
|---|---|---|
| `ITAMBOX_SECRET_KEY` | Django cryptographic signing (sessions, CSRF, password-reset tokens) | Sessions and signed values invalidate; **if `ITAMBOX_FIELD_ENCRYPTION_KEYS` is unset, all encrypted fields become permanently unreadable** (the derived key changes) |
| `ITAMBOX_FIELD_ENCRYPTION_KEYS` | Fernet keyring for license keys, SMTP passwords, webhook secrets | Encrypted fields are permanently unreadable; **no recovery path exists** |
| `ITAMBOX_API_TOKEN_PEPPERS` | Server-side HMAC peppers for API tokens at rest | Tokens hashed under a removed/unknown pepper stop validating; a leaked pepper plus a database dump enables offline token guessing |
| `ITAMBOX_DB_PASSWORD` | PostgreSQL authentication | Full database access |

Generation and production classification are documented in the
[installation guide](../operations/installation.md). Back up the complete secret set
together with the database and media — see [Backup & Restore](../operations/backup-restore.md)
for the backup contract. The keyring, peppers, and `.env` are a **unit** with
the database dump: restoring a dump without its keys yields unreadable
encrypted fields and invalidated tokens.

## Field-encryption key rotation

Rotation is supported by the keyring design: the **first** key encrypts, **all**
listed keys decrypt. The rotation command covers `License.product_key`,
`EmailSettings.smtp_password`, and `WebhookEndpoint.secret`; it does **not**
rotate `WebhookDelivery.target_secret`, an encrypted snapshot retained for
delivery retries. Keep the old key available for those rows and for backups
until they are no longer needed. The command does not currently provide a way
to re-encrypt those snapshots.

1. Take and verify a full database backup. Put the **new** key first and retain
   the old key after it: `ITAMBOX_FIELD_ENCRYPTION_KEYS=<NEW>,<OLD>`. Do not use
   `<OLD>,<NEW>`: the first key remains the encryption key.
2. Restart every application and worker process so all use the same keyring.
   Run `python manage.py rotate_encryption_keys --dry-run`, review its output,
   then run `python manage.py rotate_encryption_keys` to re-encrypt the fields
   listed above with the new primary key.
3. Verify the rotated values can be decrypted while both keys are configured.
   **Do not remove the old key merely because the command completed.** Retained
   `WebhookDelivery.target_secret` snapshots are not in the command's rotation
   inventory and still require their original key. Keep that key until those
   snapshots are no longer retained (and all backups requiring it have expired),
   or until a future supported process explicitly re-encrypts them.

Keep both keys configured through verification. Removing an old key too early
can make retained delivery snapshots or restored backups unreadable. Command
details: [Management Commands](../operations/management-commands.md).

## API-token pepper rotation

`ITAMBOX_API_TOKEN_PEPPERS` is a JSON object mapping **numeric rotation IDs**
to secrets of at least 50 characters. New tokens are hashed under the
**highest** ID; validation tries every configured pepper, so older IDs keep
existing tokens valid.

To rotate:

1. Add a new, higher rotation ID with a fresh secret, keeping every existing
   entry: `{"1": "<old>", "2": "<new>"}`.
2. Restart the application. New tokens are peppered under the highest ID;
   existing tokens continue to validate.
3. Remove old IDs only when you accept that tokens hashed under them stop
   validating — there is no automatic re-hash. Re-issue affected tokens
   first, or keep the old mapping indefinitely.

Validation falls back to a `SECRET_KEY`-derived pepper only when the setting
is unset/blank — production starts with a loud warning in that state, and
tokens hashed under the fallback stop validating once a dedicated mapping is
configured (re-issue them after the cutover). An explicitly supplied
**non-blank** value that is not a valid mapping **aborts production startup**
with a secret-free diagnostic; malformed configuration never silently falls
back (see [Installation](../operations/installation.md)). The fallback is only
appropriate for development.

## Trusted proxies and forwarded client IPs

Client-IP handling is used by the rate limiter:

| Setting | Default | Meaning |
|---|---|---|
| `ITAMBOX_RATELIMIT_USE_X_FORWARDED_FOR` | `False` | Read client IP from `X-Forwarded-For`. Enable **only** when every request reaches the app through a trusted proxy that overwrites/appends this header (for example a Cloudflare Tunnel or an ingress you control). Directly reachable deployments must keep it disabled, otherwise a spoofed header bypasses per-IP rate limits. |
| `ITAMBOX_RATELIMIT_NUM_PROXIES` | `1` | How many trusted hops are appended by your proxy chain. Must be a positive integer; startup fails otherwise. |

SSL termination and the `X-Forwarded-Proto` header are handled by the reverse
proxy — see [Installation](../operations/installation.md), section *Terminate TLS at a
reverse proxy*.

## Rate limiting

Login and invitation endpoints are rate-limited per client IP. The limit state
lives in the cache:

- With `ITAMBOX_CACHE_BACKEND=locmem` (development default) limits are
  per-process and reset on restart — acceptable for single-worker setups only.
- Keep `RATELIMIT_CACHE=default`; `default` is the configured cache alias. In
  production set `ITAMBOX_CACHE_BACKEND=redis` and configure
  `ITAMBOX_REDIS_URL` so that alias uses shared Redis/Valkey and limits hold
  across all gunicorn workers.

## SSO security expectations

- Local-password logins by superusers and owner-admin roles are subject to
  TOTP MFA enforcement in production (`ITAMBOX_REQUIRE_MFA`, default `True`).
- LDAP, SAML, and OIDC logins always delegate MFA to the identity provider;
  `ITAMBOX_REQUIRE_MFA` does not apply to them.
- SAML replay protection relies on the shared cache; with `locmem` it is
  per-process and weaker. Use the Redis/Valkey backend when your deployment
  authenticates via SAML.
- Per-tenant SSO configuration is JSON keyed by tenant slug — see
  [SSO & MFA](../usage/sso-and-mfa.md).
- API tokens are bound to a tenant and can additionally be restricted to
  allowed client IPs/CIDRs (fail closed on unparseable IPs) — see
  [API Tokens & SCIM](../usage/api-tokens-and-scim.md).

## Backup protection

A database backup contains **encrypted** values, not plaintext secrets — but
that protection is only as good as the key handling around it:

- Store field-encryption keys, token peppers, and `.env` separately from
  routine application backups, in an access-controlled secrets store.
- Restore drills must include the secret set; a restore without keys is a
  data-loss event, not a recovery.
- Keep backups long enough to cover retention-window decisions — pruning in
  the live database does not retroactively protect a backup taken before the
  prune (see [Data Retention](../operations/data-retention.md)).

## Plugin trust model

Plugins are **trusted, unsandboxed, in-process Python/Django code**: enabling
a plugin is equivalent to installing trusted application code with access to
the process, the database, and the host network. See the
[plugin guide](../plugins/getting_started.md) and the
[plugin removal and recovery runbook](../operations/plugin-runbook.md) before enabling
anything.

## Security reporting

Follow [SECURITY.md](https://github.com/itambox/itambox-webapp/blob/main/SECURITY.md):
report privately through [security@itambox.dev](mailto:security@itambox.dev)
with the subject prefix `[ITAMbox Security]`, or via GitHub Private
Vulnerability Reporting — never through public issues. The latest published
prerelease is the current supported target; reports against current source are
still welcome, and response or remediation times are not guaranteed during the
prerelease period.