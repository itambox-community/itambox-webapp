# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project layout

All Python/Django code lives in `itambox/`. Commands below assume you `cd itambox` first (or prefix paths accordingly). `manage.py` is at `itambox/manage.py`.

```
itambox/          # Django project root
  core/           # Framework layer: models (BaseModel, ChangeLoggingMixin), managers,
  |               #   mixins, auth backends, settings, background task wrappers
  itambox/        # Generic infrastructure: generic views, middleware, API base, plugins,
  |               #   registry, panels, HTMX helpers, URL root
  assets/         # Hardware asset tracking (core domain)
  inventory/      # Accessories, consumables, components + stock management
  organization/   # Tenants, tenant groups, contacts, locations, AssetHolder
  compliance/     # Custody receipts, audit campaigns
  procurement/    # Purchase orders, contracts, Asset Request fulfillment
  subscriptions/  # SaaS subscription tracking
  licenses/       # Software license seat management
  software/       # Installed software catalogue
  extras/         # Tags, custom fields, config contexts, dashboards, journal entries,
  |               #   attachments, reporting, alerting, webhooks, event rules
  users/          # User model, preferences, SCIM provisioning
  static/src/     # TypeScript + SCSS source
  static/dist/    # Compiled frontend (git-ignored, rebuild with npm)
```

### Standard app layout

Domain apps follow a consistent internal layout. Large apps split `models`, `forms`, and `views` into packages; smaller apps keep them as single modules — both are fine, match the neighbouring app rather than imposing a structure.

```text
<app>/
  models.py | models/    # ORM models (package when large — only assets/ splits today)
  forms.py  | forms/     # ModelForms + filter forms; CSV import forms in forms/import_forms.py
  views.py  | views/     # UI views subclassing itambox.views.generic.*
  tables.py              # django-tables2 table classes
  filters.py             # django-filter FilterSet classes (this repo uses filters.py, NOT filtersets.py)
  api/                   # serializers.py / views.py / urls.py — bases imported from itambox.api.*
  schema.py              # GraphQL (graphene) Query/Mutation — GraphQL-exposed apps only
  search.py              # @register_search SearchIndex classes (global search)
  services.py            # domain/service-layer logic — only where a service layer is warranted
  tasks.py               # django-q2 task functions — only where the app enqueues work
  signals.py             # signal receivers — only where needed
  choices.py             # ChoiceSet / enum-style choices
  urls.py                # pk-based routes; app_name set
  admin.py, apps.py
  tests/    | tests.py    # pytest-django tests
```

Not every app has every file: `services.py`, `schema.py`, `tasks.py`, `signals.py`, and `search.py` exist only where the feature is used. `api/` is always a package.

## Tech stack

Django + PostgreSQL (SQLite is rejected at settings load). Beyond Django itself:

- **REST API** — Django REST Framework + drf-spectacular (OpenAPI schema/sidecar).
- **GraphQL** — graphene-django (see "Architecture: GraphQL").
- **Lists & filtering** — django-tables2 (tables) + django-filter (FilterSets).
- **Forms** — django-crispy-forms + crispy-bootstrap5.
- **Frontend interactivity** — HTMX (django-htmx); TypeScript + SCSS compiled via npm into `static/dist/`.
- **Background jobs** — django-q2 (NOT Celery); worker via `manage.py qcluster`.
- **Cache / queue broker** — Valkey/Redis in prod, locmem in dev.
- **Tests** — pytest-django + model_bakery. The full suite runs on two lanes: pytest-xdist with `-n auto --dist=loadscope -m 'not serial_only'` for the parallel lane, plus a serial-only lane for tests that require serial semantics (races, migrations, global seed state — the `serial_only` marker).
- **Docs** — MkDocs.

Direct dependency policy lives in `pyproject.toml`; exact cross-platform versions live in `uv.lock`. Use uv `0.11.31` with `--locked` rather than installing ad hoc packages.

## Development commands

All commands run from `itambox/`.

### Django
```bash
# Env selection: ITAMBOX_ENV=dev|prod. Fails closed to prod when neither
# ITAMBOX_ENV nor ITAMBOX_DEBUG is set (test runs default to dev).
DJANGO_SETTINGS_MODULE=core.settings.dev uv run --locked --group dev python manage.py runserver

uv run --locked --group dev python manage.py makemigrations
uv run --locked --group dev python manage.py migrate
uv run --locked --group dev python manage.py createsuperuser
```

### Tests (pytest-django)
```bash
# Run all tests
uv run --locked --group dev pytest

# Run a single test file
uv run --locked --group dev pytest assets/tests/test_assignments.py

# Run a single test
uv run --locked --group dev pytest assets/tests/test_assignments.py::TestAssetAssignment::test_active_assignment

# Run with coverage (--cov-config is required: coverage.py reads its config from
# the current directory, and pytest runs from itambox/)
uv run --locked --group dev pytest --cov=. --cov-config=../pyproject.toml --cov-report=html

# Adversarial GraphQL suite (uses a separate test DB to avoid collisions)
uv run --locked --group dev pytest assets/tests/test_graphql_adversarial.py
```

Tests require a running PostgreSQL instance. SQLite is explicitly rejected by settings. The `conftest.py` at `itambox/` clears tenant/user contextvars after each test via an `autouse` fixture.

### Coverage gates

Coverage is measured (line **and** branch) from the complete serial suite against a database migrated from scratch, and three blocking gates read the result. Run them the way CI does, from the repository root:

```bash
make coverage        # suite + branch coverage, then run certification + global ratchet
make coverage-diff   # differential coverage of the branch against origin/main
```

| Gate | Script | Rule |
|---|---|---|
| Global ratchet | `scripts/check_coverage_baseline.py` | Line and branch rates may not fall below `scripts/coverage_baseline.json`; improvements must be recorded (`make coverage-baseline`); a decline needs `--allow-decline --reason`. Growth in excluded lines fails too. |
| Differential | `scripts/check_diff_coverage.py` | 85% of the executable lines a change touches must be covered — executed **and** not the origin of an untaken branch. An unmeasured changed production file fails closed. |
| Run certification | `scripts/check_test_report.py` | No failures, no errors, and no skipped tests (`MAX_SKIPPED_TESTS = 0`). Durations are published, never gated. |

The measurement policy is declared once in `scripts/coverage_policy.py` and mirrored in `pyproject.toml`; the gates refuse to run when the two disagree, so an `omit` entry cannot be added silently, and `# pragma: no cover` cannot be used to retire a hard-to-test branch because the excluded-line count is itself ratcheted. The combined two-lane run (parallel `-n auto -m 'not serial_only'` + `serial_only` lane) is the correctness source of truth; both lanes are certified against the serial collection manifest, so a test dropped from both lanes, or run in both, fails closed. The complete serial suite remains available as a control run via the `xdist-validation` dispatch workflow.

Full policy: [test-coverage-policy.md](https://github.com/itambox/design-docs/blob/main/development/test-coverage-policy.md). What tests for tenancy, RBAC, tokens, encryption, imports, SCIM, task context, and destructive operations must assert: [security-test-expectations.md](https://github.com/itambox/design-docs/blob/main/development/security-test-expectations.md) — coverage proves a line ran, not that the test asserted anything about the boundary it crosses.

### Frontend
```bash
# Install dependencies once
npm install

# Full build (SCSS + vendor copy + JS bundle)
npm run build:all

# JS bundle only
npm run build

# Watch mode during development
npm run watch
npm run watch:css   # CSS only
```

### Background worker
```bash
uv run --locked --group dev python manage.py qcluster   # Start django-q2 worker
```

In tests, `Q_CLUSTER['sync'] = True` is set automatically so tasks run inline.

### Lint (flake8 + flake8-bugbear)
```bash
# From the repository root (not itambox/) -- blocking gate, same command CI and
# pre-commit both run:
uv run --locked --only-group dev python scripts/check_flake8_baseline.py

# After deliberately reducing debt, regenerate with the pinned toolchain on
# canonical Python 3.12. Other interpreter versions are refused:
uv run --locked --only-group dev python scripts/check_flake8_baseline.py --write-baseline
```
Policy (`select`/`ignore`, each ignore documented with a reason) lives in `setup.cfg`
at the repo root and covers the rules not yet migrated to Ruff (`B017`, `B907`,
`B908`, `C901`); migrated rule groups move over one phase
at a time under issue #622 (current admission: see the Ruff section below). The
pinned Flake8/Bugbear toolchain is blocking; pre-existing
violations are grandfathered via `scripts/flake8_baseline.json`, a
schema-v3 identity baseline keyed by path, code, message, source statement, and
stable AST context. Its policy SHA-256 binds it to the effective Flake8 config,
targets, and exact tool versions. Canonical Python 3.12 requires exact identity
equality: increases are regressions, while reductions require regenerating the
baseline in the same reviewed cleanup so old headroom cannot hide reintroduced
debt. The gate refuses to run on any interpreter other than canonical Python
3.12; there are no interpreter- or OS-specific exceptions. `make lint` /
`pre-commit run --all-files` use the same managed policy.

### Static typing (mypy policy gate)
```bash
# From the repository root -- blocking gate; Linux/Python 3.12 is authoritative:
make typecheck

# Direct equivalent, using the full dev environment:
uv run --locked --group dev python scripts/check_typing_policy.py
```
The gate checks only modules admitted to `scripts/typing_checked_modules.json`;
it has no write mode, and the record fingerprint must be updated in the same
reviewed change as any deliberate policy or admission change. CI runs the same
gate after dependency installation and pre-commit invokes it with the full dev
group. A Windows run is useful for local feedback but is explicitly
non-authoritative because Linux has the complete native dependency surface.

### Format and import order (Ruff)
```bash
# From the repository root -- idempotent; import sort runs before formatting:
make format

# Non-mutating check -- the same rule set enforced by CI and pre-commit:
make format-check
```
Ruff is the canonical formatter and import sorter, pinned to an exact version
in `pyproject.toml`/`uv.lock` so contributors, pre-commit, and CI produce
identical output. Configuration lives in `pyproject.toml` `[tool.ruff]`: line
length 120 and `target-version = "py312"` to match the Flake8 policy above,
plus repository-appropriate excludes (migrations, `itambox/static/dist`,
and `itambox/static/docs`). `[tool.ruff.lint] select` carries import ordering
plus the rule groups already migrated from Flake8 (`B007`, `B009`, `B010`,
`B028`, `B905`, `E402`, `E501`, `E741`, `F401`, `F403`, `F405`, `F541`); `E501` replaced the retired `B950`
via the 132-character `[tool.ruff.lint.pycodestyle]` setting. Flake8 remains
the blocking identity-baseline gate for the rules not yet migrated (see the
Flake8 section above); each phase moves one bounded rule group onto Ruff only
after a parity comparison shows nothing became weaker (issue #622).
Local-import classification is enforced separately by the gate below; this
formatting pass does not move any import between scopes.

### Lint: local imports (AST policy gate)
```bash
# From the repository root -- blocking gate, same command CI and pre-commit run:
uv run --locked --only-group dev python scripts/check_local_imports.py

# After hoisting or annotating debt, regenerate on canonical Python 3.12:
uv run --locked --only-group dev python scripts/check_local_imports.py --write-baseline
```
Function-body imports must be annotated with a categorised reason (see
"Inline-import policy" below) or recorded in `scripts/local_import_baseline.json`,
a schema-v1 identity baseline keyed by path, enclosing scope path, and the
normalised import statement -- never by line number. A SHA-256 policy
fingerprint binds the baseline to the effective policy (categories, marker
grammar, targets, exclusions). New unannotated imports are regressions;
hoisted or annotated ones make the baseline stale and must be regenerated in
the same reviewed change. A comment carrying the `inline import:` marker
without a recognised category always fails and can never be baselined. The gate
refuses to run outside canonical Python 3.12. `itambox/` and `scripts/` are
scanned; migrations, vendored trees, and test modules are excluded.

### Lint: escape hatches (AST policy gate)
```bash
# From the repository root -- blocking gate, same command CI and pre-commit run:
uv run --locked --only-group dev python scripts/check_escape_hatches.py

# After paying down debt or annotating occurrences, regenerate on canonical Python 3.12:
uv run --locked --only-group dev python scripts/check_escape_hatches.py --write-baseline
```
A new occurrence of a pattern that bypasses an invariant needs an in-place
reason or the gate fails (see "Escape-hatch policy" below). Existing
occurrences are frozen in `scripts/escape_hatch_baseline.json`, a schema-v1
identity baseline keyed by rule, path, enclosing scope path, and the normalised
expression -- never by line number -- bound to the effective rules by a SHA-256
policy fingerprint. New identities are regressions and `--write-baseline`
refuses them; removed or annotated ones make the baseline stale and must be
regenerated in the same reviewed change, so paid-down debt never becomes
headroom. The gate refuses to run outside canonical Python 3.12.

### Lint: architecture boundaries (AST policy gate)
```bash
# From the repository root -- blocking gate, same command CI and pre-commit run:
uv run --locked --only-group dev python scripts/check_architecture.py

# Why are these two modules coupled?
uv run --locked --only-group dev python scripts/check_architecture.py --explain core.managers organization.access

# After removing a cycle or a cross-layer edge, regenerate on canonical Python 3.12:
uv run --locked --only-group dev python scripts/check_architecture.py --write-baseline
```
Every first-party module is classified into one layer and the policy declares
which direction a dependency may run (see "Architecture: layers and dependency
direction" below). The gate builds the first-party import graph twice -- once
from module-top imports, once including function-body imports -- and both graphs
block, so moving an import into a function changes which rule fails and nothing
else. `if TYPE_CHECKING:` imports are in neither graph. Accepted debt is frozen
in `scripts/architecture_baseline.json` (schema v2) with an owning `area:*` label
and a machine-readable `disposition`: `debt` rows also name the open tracking
issue that will remove them — recorded in the reviewed
`scripts/architecture_issue_states.json` snapshot, refreshed with `python
scripts/check_architecture.py --refresh-issue-states` — and a removal direction
of at least 40 characters; `accepted` rows carry a stable rationale and no
removal promise. The offline gate never reaches the network; CI refreshes the
snapshot live and rejects any reference that is a pull request rather than a
real, open issue. Cycles and cycle claims are always `debt`. A SHA-256
fingerprint binds the baseline to the effective policy.
A model importing a form, a table, or a view (`R-M1`) has no baseline
representation at any severity and cannot be written even by
`--write-baseline`. New identities are never absorbed: hand-review the row in
first. `--report-only` is a triage inventory, prints `REPORT ONLY -- NOT A PASS`,
and must never be wired into CI. The gate refuses to run outside canonical
Python 3.12 and scans `itambox/` only. Full policy:
[architecture-policy.md](https://github.com/itambox/design-docs/blob/main/development/architecture-policy.md); the
layer definitions and the matrix:
[adr-0001-architecture-boundaries-and-layering.md](https://github.com/itambox/design-docs/blob/main/development/adr-0001-architecture-boundaries-and-layering.md).

### Published 1.0 contract (AST policy gate)
```bash
# From the repository root -- blocking gate; CI reaches it through the
# scripts/tests gate-suite discovery rather than a dedicated workflow step:
uv run --locked --only-group dev python scripts/check_contract_policy.py

# What is the gate comparing the inventory against?
uv run --locked --only-group dev python scripts/check_contract_policy.py --list

# The behavioural suite CI actually runs:
uv run --locked --only-group dev python -m unittest scripts.tests.test_contract_policy
```
The 1.x compatibility promise
([compatibility-policy.md](https://github.com/itambox/design-docs/blob/main/development/compatibility-policy.md)) and
the bounded enumeration it applies to
([external-contract-inventory.md](https://github.com/itambox/design-docs/blob/main/development/external-contract-inventory.md))
are checked against source rather than kept in step by hand. The gate derives
persisted choice values, `ITAMBOX_*` settings reads, capability declarations and
their limitation text, custom permission codenames, the webhook envelope and its
signature header, SCIM routes, UI URL namespaces, and root entry routes with
`ast`, then compares them against the anchored tables in the inventory. It
imports no Django and touches no database. There is deliberately **no write
mode**: publication is a reviewed edit, so when the gate fails the fix is to
restore the surface or to edit the document. A closed-for-1.x enum records its
frozen values in `scripts/contract_policy.py` as well as in the document, and
each capability's published exclusions summary is pinned to the exact declared
limitation text it was written against, so changing either takes coordinated
reviewed edits. Rule identifiers (`C-ENUM1` ... `C-DOC3`) and what each blocks
are tabulated in the policy document.

### Docs (MkDocs)
```bash
# Build docs to static/docs/ (run from itambox/)
uv run --locked --only-group docs mkdocs build --strict

# Live-reload preview
uv run --locked --only-group docs mkdocs serve
```

### API schema
```bash
# Regenerate schema.yaml after model/serializer changes
uv run --locked --group dev python manage.py spectacular --file schema.yaml
```

## Architecture: tenant scoping

Every request is scoped to one active tenant, stored in a `contextvars.ContextVar` (`core/managers.py`). **`TenantMiddleware`** resolves the tenant from session/query-param and sets it via `set_current_tenant()` / `set_current_membership()`. This propagates automatically through ORM queries.

Manager hierarchy for tenant-aware models:
- `SoftDeleteManager` â€” default manager; filters `deleted_at__isnull=True`
- `TenantScopingManager` â€” also filters to the current tenant's objects
- `AllObjectsManager` â€” unfiltered; use only for admin/recycle-bin operations
- `TenantScopingSoftDeleteManager` â€” combines both

Soft-delete models must use `UniqueConstraint(..., condition=Q(deleted_at__isnull=True))` rather than `unique=True` on name/slug fields (active rows only must be unique). Documented exception: `Asset.asset_tag` stays unique across soft-deleted rows, so a printed tag is never reissued while the record can still be restored.

### Form scoping conventions

Tenant scoping on forms is declared **explicitly**; the codebase performs no runtime replacement of Django form internals. The former global monkey patches were removed in the #584 series, and `core/tests/test_no_form_patches.py` is an AST gate that fails closed if a production module reintroduces a runtime replacement (assignment, `setattr`/`del` on form classes, module-attribute swaps).

- **Model forms** inherit `TenantScopedFormMixin` (`core/forms/scoping.py`): it narrows choice querysets to the request scope (single-tenant, group and all-accessible) and keeps requiredness parity with the former patch behavior.
- **Helpers**: `apply_tenant_scoped_choices()` (e.g. on `BaseForm.__init__`), `apply_tom_select()` to emit the `data-tom-select` frontend contract, and `scope_tenant_field()` for narrowing a single field.
- **Field classes**: `TenantScopedModelChoiceField` / `TenantScopedModelMultipleChoiceField` re-declare `queryset` as a read-time property (subclass delegation to Django's own property — never a patch).
- **Filter surfaces** use `TenantScopedFilterSetMixin` so group and all-accessible scopes narrow exactly like the single-tenant path in `BaseFilterSet.__init__`.
- **Admin**: `TenantScopedAdminFormMixin` for `ModelAdmin` form classes (superuser console; UI consistency, not isolation).
- **New forms declare their scoping explicitly**; the WP0 characterization suite is the parity contract for any change in this area.

## Architecture: change logging

`ChangeLoggingMixin` (`core/models.py`) records an `ObjectChange` on every `save()` and `delete()`. It relies on two contextvars from `itambox/middleware.py`:
- `_request_id` â€” a UUID set per HTTP request by `CurrentUserMiddleware`
- `_current_user` â€” the authenticated user

**If either is `None`, the save is not logged.** Background tasks must use `TaskContext` (`core/tasks/context.py`) as a context manager, which sets both variables for the lifetime of the task, ensuring changes are attributed to the task's user rather than silently skipped.

Call `obj.snapshot()` before making changes to capture the pre-change state; otherwise the mixin re-fetches the row from the DB.

## Architecture: generic views

`itambox/views/generic/__init__.py` provides the reusable view base classes:
- `ObjectListView` â€” paginated, filterable, tenant-scoped, HTMX-aware list
- `ObjectDetailView` â€” detail with layout panels
- `ObjectEditView` / `ObjectDeleteView` / `ObjectCloneView`
- `ObjectBulkEditView` / `ObjectBulkDeleteView` / `ObjectImportView`

**HTMX pattern:** `BaseHTMXView` detects boosted (`hx-boost`) vs. partial requests and returns the appropriate template (`content_partial_name` for HTMX, full template otherwise). Service/action views return `204 + HX-Trigger` JSON payload on success (`closeModalEvent`, `tableRefreshRequired`, `showMessage`).

Layout panels are declared as a tuple of `Panel(slot, title)` objects on the view; the `{% render_panel %}` template tag renders them.

**URL routing convention:** URLs are pk-based; slugs are stable natural keys for import/export and filtering â€” never routing. `AutoSlugMixin` populates the slug on save; `ObjectDetailView`/`ObjectEditView` resolve edit/delete/clone URLs exclusively via `kwargs={'pk': ...}`.

### Inline-import policy

Imports live at module top. A function-body (inline) import is justified ONLY by one of four categories: `cycle` (breaks a real circular import), `app-registry` (avoids `AppRegistryNotReady` at import time), `optional-dependency` (absent in a supported environment), or `heavy-import` (defers an expensive import off a hot import path). Every other inline import — plain stdlib, plain Django, and local-app imports with no cycle — must be hoisted.

A justified inline import is annotated in place as `# inline import: <category>: <reason>` (plural `# inline imports:` covers a contiguous group), naming the modules involved:

```python
# inline import: cycle: core.managers <-> itambox.middleware at module load
from itambox.middleware import get_current_user
```

`scripts/check_local_imports.py` enforces this as a blocking, AST-based gate (see "Lint: local imports" above). The full policy — grammar, scope, ratchet semantics, and how to pay down baselined debt — is in [python-import-policy.md](https://github.com/itambox/design-docs/blob/main/development/python-import-policy.md).

### Escape-hatch policy

Three patterns step around tenant scoping, the provider seam, or the behaviour
of the code under test. A new occurrence is justified in place as
`# <rule>: <reason>` on the statement's own line(s) or in the comment block
directly above it, or it fails `scripts/check_escape_hatches.py`:

| Rule | Pattern | Where it applies |
|---|---|---|
| `unscoped` | `X._base_manager` / `X.all_objects` | production views, forms, serializers and services (path contains a `view(s)`, `form(s)`, `serializer(s)` or `service(s)` component) |
| `provider-slot` | a new `SingleProviderSlot` instance | production code |
| `source-text` | `inspect.getsource` / `ast.parse` | test modules, except `scripts/tests` |

```python
# unscoped: restore flow must see soft-deleted rows regardless of the active tenant
row = Thing._base_manager.get(pk=pk)
```

Prefer the scoped manager, a service helper, or a behavioural assertion over an
annotation. Broad `except Exception` handlers are not part of this gate: they
are ratcheted by `scripts/check_exception_policy.py`, and bare `except:` is
refused by flake8 E722.

## Architecture: layers and dependency direction

Every first-party module belongs to exactly one layer, derived from its dotted
name: `framework` (`itambox.api.*`, `itambox.middleware`, `itambox.plugins.*`),
`kernel` (`core.models`, `core.managers`, `core.mixins`, `core.choices`),
`platform-service` (`core.tasks.*`, `core.events`, `core.reports.*`),
`integration` (`core.auth.*`, `core.importers.*`, `core.integrations.*`),
`domain-model`, `domain-service`, `presentation`, and `composition` (URLconfs,
`apps.py`, `admin.py`, settings). `presentation` splits by origin: a domain
app's presentation may name its own domain, while the platform's generic
presentation (`itambox.views.*`, `core.tables.*`) is held to the framework
standard.

Four invariants follow, and `scripts/check_architecture.py` enforces them:

- Nothing imports `composition`; composition roots are wired *into*, never *from*.
- Nothing below `presentation` imports `presentation`. A model may not depend on
  a form, table, view, or presentation helper -- the one rule with no baseline
  escape at any severity.
- `framework` and `kernel` are domain-blind. They may recurse into each other --
  they are one mutually recursive substrate -- but neither may name a domain app.
- Cross-application `domain-model -> domain-model` coupling needs an entry in
  `CROSS_DOMAIN_MODEL_EDGES`; same-app model coupling is always fine.

The usual fixes when the gate blocks an import are `apps.get_model()` for a model
the substrate needs, a registry hook the platform publishes and the domain
registers with, or moving the shared helper down a layer. The gate cannot see
dynamic imports (`importlib.import_module`, `import_string`) and does not guess
at them; it reports their count in the substrate as information.

## Architecture: permissions & auth

Permissions flow: `TenantMembershipBackend` (`core/auth/__init__.py`) is the first backend. It resolves permissions from a JSON `permissions` field on `TenantRole`; it handles the `obj=` argument by extracting `obj.tenant` and checking the user's membership in that tenant. `PasswordLoginOnlyBackend` blocks all perm checks for password-auth, ensuring all authorization goes through the membership backend.

`StrictTenantPermission` (DRF) enforces object-level tenant boundary on all API detail endpoints via `DEFAULT_PERMISSION_CLASSES`.

The canonical API implementation lives in `itambox/api/`. All app-level API code (`serializers.py`, `views.py`) imports directly from `itambox.api.*`.

### Content Security Policy

`CSPMiddleware` (`itambox/middleware.py`) sets the CSP header. Inline scripts use the per-request `request.csp_nonce`; `script-src` has no `'unsafe-inline'`. Browser styles use the same nonce through `style-src`/`style-src-elem`, while `style-src-attr 'none'` blocks every inline `style=` attribute. Authored HTML/Python emitters and TS/JS DOM-style writes are checked by `scripts/check_inline_styles.py`; static rules live in authored CSS/SCSS and genuinely dynamic rules use the nonce-aware helpers in `core/html_styles.py`. The only source exceptions are documented PDF/standalone emitters in that gate.

## Business invariants

The gates above constrain code shape; none of them checks that two modules agree on a business fact. These invariants are the facts agents must keep when touching the named areas. Each one is enforced by a journey test in `itambox/tests/journeys/` (see "Journey guardrail suite" under "Testing conventions"), not by lint.

- **A custody receipt belongs to one assignment.** Check-in or reassignment ends the receipt's right to be accepted; a former holder never signs. A holder without a tenant membership can still sign their own receipt (no 500).
- **Purchase-order transitions lock the PO row.** Approve, order, receive, cancel and reopen all `select_for_update` the `PurchaseOrder` and re-read `status` under the lock. Cancel → reopen → relink yields one line per request unit.
- **Import never updates without `change_<model>`.** An `add`-only role creates rows; it cannot rewrite one by `id`. Lifecycle models (custody receipts, asset assignments) are not importable at all.
- **Lifecycle rows are written only by their services.** Assignments, custody receipts, disposals and seat assignments are created and ended by `assets.services`, `compliance.services`, and the like -- never by import, a serializer, GraphQL, or a management command writing the model directly.
- **Authorization uses `user.has_perm` (role grants), never Django `user_permissions` directly.** Permissions come from `Role`/`RoleGrant` through `TenantMembershipBackend`. Code that reads `user.user_permissions` or `groups__permissions` silently locks out every role-based user. Tests grant through roles, never superusers or `user_permissions`.
- **A deleted or deprovisioned person's obligations stay visible.** SCIM DELETE or soft-deleting a holder must not hide assets, receipts, reservations or seats they still carry; the holder and its offboarding report stay reachable. Identity is never linked by guesswork (a holder is not bound to a login by email alone when its UPN differs).
- **A reservation holds an asset against every checkout target**, not only people: a location or asset checkout during another holder's active reservation is refused.
- **Every audited mutation leaves an `ObjectChange`, and every logged delete emits an `Event`.** That includes cascaded soft-delete children and management commands (wrap them in `TaskContext`).
- **GraphQL is read-only.** No mutation operation is accepted; writes go through the service-backed REST/UI paths.

When you change behaviour in one of these areas, run `uv run --locked --group dev pytest tests/journeys` and flip the matching journey from `xfail` to passing in the same change.

## Architecture: GraphQL

GraphQL uses **graphene-django** (not Strawberry). Each exposed app declares `Query`/`Mutation` classes in `<app>/schema.py`; the root schema in `core/schema.py` combines them (currently `assets`, `inventory`, `licenses`, `software`, `subscriptions`) plus any plugin schema (a plugin opts in via a `graphql_schema` attr on its app config). The endpoint is served by `core/views/graphql.py` — a `GraphQLView` subclass wired through `TenantMiddleware`/`CurrentUserMiddleware` and token auth, with **query-complexity guards**: a depth limit plus a field/alias-count validator (`field_count_limit_validator`) that stops alias-amplification DoS (`a1: assets(...) a2: assets(...) …`). To expose a new app: add `<app>/schema.py` with `Query`/`Mutation`, then add those to the bases in `core/schema.py`. Coverage is tested by `test_graphql.py`, `test_graphql_adversarial.py`, and `test_sec_graphql.py`.

## Architecture: background tasks

Tasks live in `core/tasks/`. Each task function should be wrapped in `TaskContext(tenant_id=..., user_id=...)` to wire up tenant scoping and change-log attribution. Tasks are enqueued with django-q2's `async_task()`, dispatched via `transaction.on_commit()` to avoid running before the triggering transaction commits.

## Common tasks: add a model end-to-end

A fully-wired model touches every layer below. Skipping one leaves a half-wired feature (a model with no API, a list view with no filter, an object missing from search). Mirror an existing model in the same app rather than inventing structure.

1. **Model** — add to `<app>/models.py` (or `models/`). Inherit `BaseModel`; add `ChangeLoggingMixin` for audit history and `AutoSlugMixin` if it needs a slug. Tenant-scoped models get the tenant FK + the appropriate manager (see "Architecture: tenant scoping"); soft-delete uniqueness uses `UniqueConstraint(condition=Q(deleted_at__isnull=True))`, never `unique=True`.
2. **Migration** — `uv run --locked --group dev python manage.py makemigrations <app>` (never hand-write).
3. **Filtering** — add a `FilterSet` to `<app>/filters.py` and a filter form to `<app>/forms/` (the list view wires them as `filterset` / `filterset_form`).
4. **Form** — add a crispy `ModelForm` to `<app>/forms/`; for CSV import add a form to `<app>/forms/import_forms.py` decorated with `@register_import_form` (auto-wires to the centralized import view).
5. **Table** — add a `django_tables2` table to `<app>/tables.py`.
6. **REST API** — serializer in `<app>/api/serializers.py`, viewset in `<app>/api/views.py`, route in `<app>/api/urls.py` (bases from `itambox.api.*`); then regenerate the schema with `uv run --locked --group dev python manage.py spectacular --file schema.yaml`.
7. **UI views** — subclass the generics in `<app>/views.py` (or `views/`): `ObjectListView` / `ObjectDetailView` / `ObjectEditView` / `ObjectDeleteView` / `ObjectCloneView` / `ObjectBulkEditView` / `ObjectBulkDeleteView`; set `queryset`, `filterset`, `filterset_form`, `table`, and detail `Panel`s.
8. **URLs** — add the pk-based route set to `<app>/urls.py` (`list` / `add` / `<pk>/` / `<pk>/edit/` / `<pk>/clone/` / `<pk>/delete/` + bulk + any custom actions).
9. **Search** — register a `SearchIndex` in `<app>/search.py` with `@register_search()` if the model should appear in global search.
10. **GraphQL** (optional) — expose via `<app>/schema.py` and wire into `core/schema.py` (see "Architecture: GraphQL").
11. **Navigation** — wire the list view into the sidebar navigation so the model is reachable in the UI.
12. **Tests** — add to `<app>/tests/` using `TenantTestMixin`; mirror the existing `test_api.py` / `test_filter_forms.py` / `test_views.py` coverage.

## Settings

| Env var | Purpose | Default |
|---|---|---|
| `ITAMBOX_ENV` | `dev` or `prod` | fail-closed to `prod` when unset (dev under tests) |
| `ITAMBOX_SECRET_KEY` | Django secret key | insecure default (dev only) |
| `ITAMBOX_FIELD_ENCRYPTION_KEYS` | Comma-separated Fernet keys for field encryption (`License.product_key`, SMTP password, webhook secret); first key encrypts, all keys decrypt (rotation). **Unset derives the key from `SECRET_KEY` — insecure: rotating `SECRET_KEY` then makes encrypted fields unrecoverable.** Set a stable value in prod and back it up. | unset (derives from `SECRET_KEY`) |
| `ITAMBOX_BASE_URL` | Public base URL for QR labels & outbound links (no trailing slash) | `""` (bare-tag QR used) |
| `ITAMBOX_DEFAULT_CURRENCY` | ISO 4217 fallback for money display; `{{ value|money:obj }}` resolves tenant currency first | `EUR` |
| `ITAMBOX_DB_*` | DB connection | `itambox`/`localhost`/`5432` |
| `ITAMBOX_CACHE_BACKEND` | `locmem` or `redis` (Redis wire protocol — run Valkey, the BSD-licensed fork) | `locmem` |
| `ITAMBOX_REDIS_URL` | Valkey/Redis connection (`redis://` protocol) when cache=redis | `redis://127.0.0.1:6379/1` |
| `RATELIMIT_CACHE` | Cache alias for rate limiting. Under multi-worker prod this (and the SAML replay-protection cache, which uses the `default` alias) MUST resolve to a shared redis/Valkey backend — a per-process `locmem` cache makes login counters per-worker (effective limit × workers) and weakens SAML replay protection to per-process. See "Caching in production". | `default` |
| `ITAMBOX_TENANT_LDAP_CONFIGS` | JSON per-tenant LDAP configs | `{}` |
| `ITAMBOX_TENANT_SAML_CONFIGS` | JSON per-tenant SAML configs | `{}` |
| `ITAMBOX_TENANT_OIDC_CONFIGS` | JSON per-tenant OIDC configs | `{}` |
| `ITAMBOX_TENANT_INTUNE_CONFIGS` | JSON per-tenant Intune discovery configs | `{}` |
| `ITAMBOX_DOCS_ROOT` | Filesystem path to compiled MkDocs output | `BASE_DIR/docs` |
| `ITAMBOX_REQUIRE_MFA` | Enforce TOTP MFA (django-otp) for local-password logins by superusers/owner-admin roles. SSO/LDAP/SAML/OIDC delegate MFA to the IdP and are always exempt. | `False` (dev); `True` (prod) |
| `ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS` | JSON object with optional non-negative `accessory` and `consumable` thresholds. Enables Asset Request auto-approval and the Beta procurement seam; absent means inert. | unset |
| `ITAMBOX_REQUIRE_CUSTODY_SIGNIN` | Require digital signature on custody receipt sign-off | `True` |
| `ITAMBOX_ALLOW_GLOBAL_CUSTODY_TEMPLATES` | Allow custody templates not scoped to a tenant | `True` |
| `ITAMBOX_SERVER_EMAIL` | From-address for error emails (prod only) | `DEFAULT_FROM_EMAIL` |
| `ITAMBOX_PAGINATOR_COUNT_CAP` | Upper bound for the list-page row counter (`EnhancedPaginator`). A plain `SELECT COUNT(*)` scans the whole filtered table on every list view (slow at NetBox scale); the paginator counts only up to this many rows. At or below the cap the total is exact (small tables and tests are unaffected); above it the UI shows "<cap>+". Set `0` to disable capping (stock unbounded count). | `100000` |

Reads `.env` from `BASE_DIR` or `BASE_DIR/../` at startup (hand-rolled parser; no `python-dotenv`).

### Caching in production

Rate limiting (`RATELIMIT_CACHE`) and SAML replay protection both read through the Django cache. Under multi-worker gunicorn a per-process `locmem` cache silently breaks them: login/throttle counters become per-worker (so the effective limit is `RATELIMIT_LIMIT × workers`) and SAML assertion replay protection only dedupes within a single worker. Set `ITAMBOX_CACHE_BACKEND=redis` (+ `ITAMBOX_REDIS_URL`, pointing at Valkey/Redis) so all workers share one counter store. `core/settings/prod.py` logs a loud warning at startup when `CACHE_BACKEND=locmem` in production.

## Testing conventions

### Journey guardrail suite

`itambox/tests/journeys/` holds one module per cross-module journey (custody, procurement, identity, import, reservation, events, audit, catalogue, GraphQL). Each test asserts the **correct** behaviour end to end. While the owning defect is open the test carries `@pytest.mark.xfail(strict=True, reason="... (#NNN)")` naming the issue that fixes it; the fixing change deletes the marker. `xfail_strict = true` in `pyproject.toml` makes an unexpectedly passing test fail the run, and `scripts/check_test_report.py` counts expected failures separately so they are not treated as skipped tests.

Rules for journey tests:

- Grant permissions **through roles** (`JourneyMixin.make_member`, `TenantTestMixin.client_login_to_tenant`, `grant`). Never use superusers or Django `user_permissions` -- the catalogue permission defect hid behind exactly that.
- The suite runs in the parallel lane. A test that needs real concurrency (two threads) is `serial_only` and uses `TransactionTestCase`.
- A journey test fails for the stated reason: check that it reaches the assertion about the invariant, not an unrelated setup error, before parking it behind `xfail`.

- Use `TenantTestMixin` (`core/tests/mixins.py`) for any test that touches tenant-scoped models. It provides `setup_tenant_context()`, `set_active_tenant()`, and a `tenant_context()` context manager.
- `model_bakery` recipes are in `core/tests/baker_recipes.py`.
- Tenant/user contextvars are cleared automatically after each test by `conftest.py`.
- Security boundary tests live in `core/tests/test_tenant_security.py` and `test_security_boundaries.py` â€” run these when touching auth, middleware, or manager code.

## Plugin system

Plugins are Django apps listed in `settings.PLUGINS`. Each plugin's `__init__.py` must expose a `config` object that subclasses `itambox.plugins.PluginConfig`. `load_plugins()` (called at settings load) merges the plugin's `INSTALLED_APPS`, `MIDDLEWARE`, and config defaults, then registers it with the global `registry`. Plugin API routes mount under `/api/plugins/`; UI routes mount under `/plugins/<base_url>/`.
