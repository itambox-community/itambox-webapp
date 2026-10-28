# Contributing to ITAMbox

ITAMbox accepts focused changes through pull requests. This guide describes the repository's development and review gates; deeper implementation notes live in [DEVELOPMENT.md](DEVELOPMENT.md), and the layering that authorization and tenancy code sits inside is recorded in the [architecture boundaries ADR](https://github.com/itambox-community/design-docs/blob/main/development/adr-0001-architecture-boundaries-and-layering.md).

## Before you start

- Search the existing issues and pull requests before opening duplicate work.
- Discuss large features, schema changes, and compatibility breaks in an issue before implementation.
- Keep each pull request to one concern. Separate cleanup from behavioral changes unless the cleanup is required for the fix.
- Report vulnerabilities privately through [SECURITY.md](SECURITY.md), never in a public issue.
- Follow the [security scanning and suppression policy](https://github.com/itambox-community/design-docs/blob/main/development/security-scanning.md) for dependency, secret, or scanner-policy changes.

## Prerequisites

| Tool | Supported version or purpose |
|---|---|
| Python | Minimum metadata version 3.12; Python 3.12 is the currently qualified and canonical interpreter for CI, Docker, native Windows development, and lint |
| PostgreSQL | 15 or newer; CI uses PostgreSQL 16 and SQLite is not supported |
| Node.js | 26, with the npm version supplied by that release |
| Git | Current maintained release |
| Docker Compose | Optional for deployment and production smoke tests; the smoke test requires v2.24.4 or newer |
| GNU Make | Optional convenience wrapper; on Windows, use it from Git Bash or WSL |

Native Windows Python 3.12 is supported for the application and normal test suite. LDAP development requires Docker, Linux, or WSL because `python-ldap` has no supported native Windows wheel. Native Windows also uses the documented file-validation fallback instead of libmagic. See [DEVELOPMENT.md](DEVELOPMENT.md#native-windows-support).

### Python version policy

Project metadata requires Python 3.12 or newer, but Python 3.12 is the only currently qualified and canonical interpreter: production Docker images, every blocking CI workflow, native Windows development, and the Flake8 identity baseline all run on it. Python 3.11 and older are not supported, and the lint gate refuses non-canonical interpreters rather than producing results that cannot be compared to the baseline.

Newer Python minor versions (3.13 and later) satisfy the metadata minimum but are evaluated conservatively and separately before becoming supported. A new version is adopted only after, on that interpreter: every pinned dependency installs from a compatible release (including native Windows wheels), the full test suite and the deterministic gates pass in CI, the Flake8 identity baseline is re-validated against the pinned toolchain (tokenizer changes can alter reported findings), and the dependency lock is resolved and validated. Until that evaluation lands as its own reviewed change, newer interpreters are unsupported for development and production even if the application appears to run on them.

## Set up a development checkout

Fork the repository if you do not have branch access, then clone your fork. Organization contributors can clone the main repository directly.

```bash
git clone https://github.com/itambox-community/itambox-webapp.git
cd itambox-webapp
python -m pip install --user "uv==0.13.5"
uv --version
```

Synchronize the canonical development environment and install the Git hooks:

```bash
uv lock --check
uv sync --locked --group dev
uv run --locked --group dev pre-commit install
```

uv owns the repository-root `.venv`. `pyproject.toml` is the sole source of
direct dependencies and `uv.lock` is the exact environment contract. Do not use
`pip install .` or `pip install -e .`; ITAMbox deliberately does not build a
Python package.

For a dependency change, edit only `pyproject.toml`, regenerate `uv.lock` with
uv `0.13.5`, and review both files together. Use `uv lock --upgrade-package
<name>` for a targeted update or `uv lock --upgrade` for a deliberate full
refresh; normal setup and CI must use `--locked` and must never rewrite the lock.

Copy the environment template, select development mode, and point it at a PostgreSQL database whose user can create test databases and install the `btree_gist` extension.

```bash
cp .env.example .env
# PowerShell: Copy-Item .env.example .env
```

Set `ITAMBOX_ENV=dev` and the `ITAMBOX_DB_*` values in `.env`. If you need a disposable local database and already have Docker, this is sufficient for development and tests:

```bash
docker run --name itambox-postgres-dev --rm -d \
  -e POSTGRES_DB=itambox \
  -e POSTGRES_USER=itambox \
  -e POSTGRES_PASSWORD=itambox \
  -p 5432:5432 \
  postgres:16
```

This throwaway local-development database deliberately keeps the floating tag;
production Compose and release images follow the
[release image policy](itambox/docs/operations/release-image-policy.md).

Build the frontend and initialize the application:

```bash
cd itambox
npm ci
npm run build:all
uv run --locked --group dev python manage.py migrate
uv run --locked --group dev python manage.py seed_data --skip-drop
uv run --locked --group dev python manage.py runserver
```

The seed creates and updates public demo users, organizations, and assets. Use it only against a disposable development database. `--skip-drop` prevents the command from clearing existing records; running `seed_data` without that flag resets domain data. The application is available at <http://127.0.0.1:8000>.

## Branches and commits

Start from an up-to-date `main` branch:

```bash
git switch main
git pull --ff-only
git switch -c fix/short-description
```

Use a branch prefix that describes the work:

- `feat/` for user-visible features
- `fix/` for defects
- `docs/` for documentation
- `test/` for test-only work
- `ci/` for automation
- `chore/` for maintenance

Use the repository's established Conventional Commit style, for example:

```text
fix(assets): preserve custody history on check-in
docs: document the production backup sequence
```

Use `!` or a `BREAKING CHANGE:` footer when a change breaks an API, route, configuration contract, or migration path.

## Implementation expectations

- Follow the neighboring Django app instead of introducing a new local pattern. Standard app layout and cross-layer wiring are documented in [DEVELOPMENT.md](DEVELOPMENT.md).
- Generate migrations with `makemigrations`; do not hand-write them unless the migration cannot be expressed safely by Django and the pull request explains why.
- Preserve tenant scoping and object-level permissions in UI, REST, GraphQL, background jobs, imports, and bulk actions. Cross-tenant access must use explicit `RoleGrant` and `RoleGrantScope` records, written through the single service entry point described in [view patterns](https://github.com/itambox-community/design-docs/blob/main/development/view-patterns.md).
- Use scoped managers and established tenant-aware service boundaries rather than unscoped model queries. Add regression tests for fixes and tests for new behavior; tenant-aware tests should use `TenantTestMixin`.
- Build shared API behavior on `itambox.api`, keep generic object detail/edit/delete routes primary-key based, and retain slugs only where an integration contract explicitly requires them.
- Follow the existing HTMX partial/modal/toast conventions, and propagate `TaskContext` through django-q2 jobs so tenant and actor attribution is preserved.
- Regenerate the OpenAPI schema locally after serializer or endpoint changes and review the diff. The generated schema is currently ignored, so do not force-add it unless the release policy changes.
- Rebuild frontend assets after TypeScript, SCSS, or vendor changes.
- Update user or operator documentation and the current unreleased section of [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

## Run the checks

Run targeted tests while developing, then run the relevant full gates before opening a pull request. The full suite runs on two lanes: `pytest -n auto -m 'not serial_only'` for the parallel lane plus the `serial_only` lane. Plain `pytest -n auto` without the marker selection stays banned — the marker split is enforced by the workflow and by `conftest.py`. A test that races, mutates global/seed state, exercises migrations, or is timing-sensitive must carry the `serial_only` marker.

From the repository root:

```bash
uv run --locked --group dev pre-commit run --all-files
make format-check
uv run --locked --only-group dev python scripts/check_flake8_baseline.py
uv run --locked --only-group dev python scripts/check_local_imports.py
uv run --locked --only-group dev python scripts/check_escape_hatches.py
uv run --locked --only-group dev python scripts/check_architecture.py
make typecheck
```

Ruff is the canonical formatter and import sorter and enforces E501 at 132
characters plus the migrated B007, B009, B010, B028, B905, E741, and F541 rules
(`make format` applies formatting/import sorting; `make format-check` and CI
enforce the non-mutating checks). Flake8 above retains the not-yet-migrated
rules in its blocking baseline; B950 is replaced by Ruff E501 with exact
file/line parity. Phase 3 moved E741 after a same-tree comparison showed Ruff
was stricter (8 findings versus Flake8's 7); all eight were fixed. B907/B908
remain Flake8-only because Ruff 0.15.20 has no
equivalent selectors.

The canonical rule selections are in `pyproject.toml` and `setup.cfg`; CI and
pre-commit enforce them. The phased migration is tracked in
[issue #622](https://github.com/itambox-community/itambox-webapp/issues/622).

The fourth command is the import-placement gate. Imports belong at module top; a
function-body import needs an explicit `# inline import: <category>: <reason>`
annotation, and everything not yet triaged is frozen in
`scripts/local_import_baseline.json` so new untriaged imports fail review. Read
the [Python import policy](https://github.com/itambox-community/design-docs/blob/main/development/python-import-policy.md)
before adding one or paying down baselined debt.

The last command is the architecture boundary gate. It builds the first-party
import graph twice — once from module-top imports and once including
function-body imports — and blocks on import cycles and on edges that cross a
layer boundary the policy forbids. Accepted debt is recorded in
`scripts/architecture_baseline.json` with an owning area label and a
machine-readable `disposition`: `debt` rows also name the open tracking issue
that will remove them — recorded in the reviewed
`scripts/architecture_issue_states.json` snapshot (refresh with `python
scripts/check_architecture.py --refresh-issue-states`) — and a stated removal
direction, while `accepted` rows carry a stable rationale and no removal
promise, so the baseline is a work list rather than a suppression file. A model
that imports a form, a table, or a view is the one finding with no baseline
representation at any severity. Read the
[architecture policy](https://github.com/itambox-community/design-docs/blob/main/development/architecture-policy.md) and the
[architecture boundaries ADR](https://github.com/itambox-community/design-docs/blob/main/development/adr-0001-architecture-boundaries-and-layering.md)
before adding an entry.

From `itambox/`:

```bash
uv run --locked --group dev python manage.py makemigrations --check --dry-run
uv run --locked --group dev python manage.py check

npm ci
npm run build:all
npm run typecheck
npx eslint static/src
```

Coverage is gated, so run it the way CI does — from the repository root, which
measures the complete serial suite against a database migrated from scratch and
then checks the three quality gates:

```bash
make coverage        # suite + branch coverage + run certification + global ratchet
make coverage-diff   # differential coverage of your branch against origin/main
```

`make coverage-diff` requires 85% of the executable lines your change touched to
be covered, counting a line as covered only when it was executed and is not the
origin of an untaken branch. A changed production file the run never measured is
a failure, not a pass. Read the
[test coverage policy](https://github.com/itambox-community/design-docs/blob/main/development/test-coverage-policy.md) before
changing a baseline or adding an exemption, and the
[security test expectations](https://github.com/itambox-community/design-docs/blob/main/development/security-test-expectations.md)
before writing tests for tenancy, RBAC, tokens, encryption, imports, SCIM, task
context, or destructive operations — coverage proves a line ran, not that the
test asserted anything about the boundary it crosses.

Documentation dependencies are isolated in their own locked group:

```bash
uv sync --locked --only-group docs
cd itambox
uv run --locked --only-group docs mkdocs build --strict
```

Changes to authentication, authorization, tenant scoping, GraphQL, or background-task attribution should also run the relevant adversarial and boundary suites, including:

```bash
uv run --locked --group dev pytest assets/tests/test_graphql_adversarial.py
uv run --locked --group dev pytest core/tests/test_tenant_security.py core/tests/test_security_boundaries.py
```

For UI flows, run the Playwright suite. Its preflight requires the repository-root `.venv`, an available migrated database, an active superuser, `E2E_USERNAME` and `E2E_PASSWORD`, and an installed Playwright browser:

```bash
cd itambox
uv run --locked --group dev python manage.py createsuperuser
cd tests/e2e
npm ci
npx playwright install chromium
export E2E_USERNAME='<superuser username>'
export E2E_PASSWORD='<superuser password>'
cd ../../..
make e2e
```

For Dockerfiles, Compose configuration, entrypoints, health checks, worker startup, caching, or production settings, run the isolated production smoke test from the repository root:

```bash
./scripts/docker-smoke-test.sh
```

The smoke test requires Docker Compose v2.24.4 or newer and tears down its own temporary stack on exit.

## Open the pull request

A reviewable pull request includes:

1. A concise explanation of the problem and why the chosen fix is appropriate.
2. A linked issue when one exists.
3. The exact automated and manual verification performed.
4. Screenshots or a short recording for visible interface changes.
5. Migration, deployment, rollback, and compatibility notes when applicable.
6. Documentation and changelog updates for user-visible behavior.

Keep generated files in sync with their sources. Do not include unrelated formatting changes, local environment files, credentials, database dumps, build output, or editor state.

## Merging

`main` is protected by a ruleset that requires the 13 status checks (`lint`, `template-lint`, `stylelint`,
`inline-style`, `docs`, `frontend`, `test (3.12)`, `Migration & seed qualification`, `Coverage gates`,
`policy`, `dependencies`, `secrets`, `E2E / Gate`), a clean CodeQL code-scanning result, and the merge
queue. Every merge lands through the queue:

- Once the required checks are green on a pull request's head, a maintainer enqueues the pull request;
  direct merges into `main` are refused.
- The queue re-qualifies each entry against the current tip of `main` in a `merge_group` run before
  merging (squash), so what lands has been qualified against the state it lands into. Entries that no
  longer qualify are removed from the queue instead of landing on stale qualification.
- No manual **Update branch** is needed for freshness on the queue path; rebase locally only to resolve
  real conflicts.
- Queue configuration: all-green batching, up to three entries building and five merging per batch, a
  minimum of one entry merged after a five minute wait, and a 60 minute check-response timeout.
- The strict up-to-date requirement stays enabled as an additional guard.

Merging is done by a maintainer via the queue once every required check is green on the current head.

By submitting a contribution, you agree that it is licensed under the repository's [Apache License 2.0](LICENSE), as described by the license's contribution terms.
