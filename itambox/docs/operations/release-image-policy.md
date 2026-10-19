# Release image policy

Container images used to build releases or run the production Compose stack are
pinned with a readable tag and an immutable digest:

```text
<name>:<tag>@sha256:<64-hex-digest>
```

The digest is authoritative. For multi-platform images, it must be the
multi-architecture image index digest, not a platform manifest digest. Every
release base index must contain both `linux/amd64` and `linux/arm64`, the
platforms built and boot-checked by the release rehearsal.

## Covered images

The policy check scans every Dockerfile `FROM`, every Compose `image:` field in
the production `docker-compose.yml`, and every `image:` field in the explicitly
listed `.github/workflows/*.yml` files. It rejects a missing tag or digest and
fails if a workflow file is added or removed without updating its scan list.

| Path | Current references |
|---|---|
| `Dockerfile` | `ghcr.io/astral-sh/uv:0.13.1` (existing pin), `node:26-slim`, and all three `python:3.12-slim-bookworm` stages |
| `docker-compose.yml` | Production `db` service (`postgres:18`) and `valkey` service (`valkey/valkey:8-alpine`) |
| `.github/workflows/*.yml` | CI and validation service containers, plus the already pinned E2E PostgreSQL and mock OAuth2 services |

The bundled `db` service runs PostgreSQL 18. `runner-validation.yml` additionally
carries one PostgreSQL 16 compatibility job, pinned to the previous major
version's digest, so the version existing installations run stays under test; it
runs on dispatch only and is not part of the pull-request path.

The release workflow builds both platforms, scans the release image with its
existing Trivy gate (`--fail-on any`), and promotes the exact built digest. The
weekly image-drift workflow continues to rebuild and scan `main`. The Docker
Compose smoke workflow boots the production Compose stack on pull requests
touching an image input, as well as on its existing `main` push and manual
triggers. GitHub Actions remain commit-SHA pinned under the existing policy.

## Updating image pins

Dependabot checks the repository root's `docker` ecosystem for Dockerfiles and
`docker-compose` ecosystem for Compose files weekly. Review its PR like any other
dependency update; keep the human-readable tag beside the digest. Workflow
service images are embedded in workflow YAML and are not covered by those
ecosystems, so update them in the same reviewed change when required.

Resolve and inspect each candidate tag with Docker Buildx:

```bash
docker buildx imagetools inspect node:26-slim
docker buildx imagetools inspect python:3.12-slim-bookworm
docker buildx imagetools inspect postgres:18
docker buildx imagetools inspect valkey/valkey:8-alpine
docker buildx imagetools inspect ghcr.io/navikt/mock-oauth2-server:6.0.0
```

Use the top-level `Digest: sha256:...` as the index digest and confirm that its
manifest list contains both `linux/amd64` and `linux/arm64` before editing the
pin. Do not use either platform's child-manifest digest. Update Dockerfile and
Compose references in their respective files. For a workflow-embedded image,
update its `image:` value in the matching `.github/workflows/*.yml` service or
container entry; PostgreSQL service references currently appear in `ci.yml`,
`xdist-validation.yml`, `runner-heavy-validation.yml`, `runner-validation.yml`
and `e2e.yml`, while the mock OAuth2 service is in `e2e.yml`. The PostgreSQL 16
reference in `runner-validation.yml` is the compatibility lane described above,
so it changes only when the retained major version changes.

The pull-request CI policy check enforces the pinned shape. Dockerfile and
Compose image updates also run the two-platform release rehearsal and the
production Compose smoke boot; the existing scan thresholds and workflow
behavior remain in force.

## Managed PostgreSQL or Valkey

The pinned `db` and `valkey` services are the default self-managed stack.
Operators using managed endpoints can keep `docker-compose.yml` intact and pass
a second Compose file that points the app and worker at those endpoints. For
example, save the following as `compose.managed-services.yml` and provide the
corresponding connection values in the shell or `.env` file:

```yaml
services:
  app:
    environment: &managed-services-env
      ITAMBOX_DB_HOST: ${ITAMBOX_DB_HOST:?set ITAMBOX_DB_HOST}
      ITAMBOX_DB_PORT: ${ITAMBOX_DB_PORT:-5432}
      ITAMBOX_DB_SSLMODE: ${ITAMBOX_DB_SSLMODE:-require}
      ITAMBOX_REDIS_URL: ${ITAMBOX_REDIS_URL:?set ITAMBOX_REDIS_URL}
    depends_on: !override []
  worker:
    environment: *managed-services-env
    depends_on: !override []
  db:
    profiles: [bundled-services]
  valkey:
    profiles: [bundled-services]
```

Use the override file for builds, migrations, and startup so the bundled
services stay inactive:

```bash
docker compose -f docker-compose.yml -f compose.managed-services.yml build app worker
docker compose -f docker-compose.yml -f compose.managed-services.yml run --rm app python manage.py migrate
docker compose -f docker-compose.yml -f compose.managed-services.yml up -d app worker
```

This override syntax requires Docker Compose v2.24.4 or newer. Configure the
managed database with the required PostgreSQL extensions and privileges, and
set the Redis-protocol URL supported by the managed Valkey endpoint. Protect
the connection values as production secrets.

## Recovery drill PostgreSQL digest

The approved PostgreSQL image for a recovery drill is the exact `db.image`
reference in the candidate checkout's `docker-compose.yml`. Copy the full
`postgres:18@sha256:...` value, including its readable tag, into
`POSTGRES_IMAGE`; use that same immutable image for both recovery paths. The
Compose pin is the source of the approved digest, and this policy describes how
it is maintained. See the [recovery qualification drill](recovery-drill.md).

## Deliberate non-pins

- GitHub-managed runner labels such as `runs-on: ubuntu-latest` identify the
  runner service, not a container image controlled by this repository.
- CI Node setup uses the GitHub Actions `setup-node` toolchain with
  `node-version: "26"`; it is not a container reference.
- The contributor quickstart's `postgres:16` container is disposable local
  development infrastructure. Its tag stays convenient to pull and is not
  used for production deployment or release builds.

These exceptions are outside the image policy check. The repository-root
`CONTRIBUTING.md` contains the local-development quickstart.
