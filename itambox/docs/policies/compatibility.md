# Compatibility policy for 1.x

This page states what a 1.x release may change in a surface you already depend
on, how a removal is announced, and which guarantees no maturity grade can trade
away. It also contains the deprecation rules.

!!! note "Pre-release status"
    Until `v1.0.0` is published, this page describes the contract 1.0 will ship
    under. It is not a retroactive promise about the alpha and beta prereleases;
    their changes are recorded in the [changelog](https://github.com/itambox-community/itambox-webapp/blob/main/CHANGELOG.md).
    This policy takes effect with `v1.0.0`.

## What the promise covers

The promise applies to these surfaces and to nothing else:

- the REST, GraphQL, and SCIM interfaces, including the checked-in OpenAPI document;
- report identifiers and column keys;
- the webhook and domain-event envelope;
- contract-bearing `ITAMBOX_*` settings (see the
  [installation guide](../operations/installation.md));
- UI URL namespaces and the primary-key route shape;
- permission codenames stored in role definitions;
- persisted choice values;
- the grade and activation mode of each capability.

An internal Python name, a template, a table column, a form field, a CSS class,
or a management-command flag carries no compatibility promise. The plugin
interface is graded Experimental. Individual UI route names are not frozen.

## Capability grades

Every covered surface belongs to a capability, and the capability's grade decides
what is promised. The current grade of each capability is listed in
[Capability maturity](../operations/capability-maturity.md); `manage.py capabilities`
reports it for your deployment.

| Grade | Activation | What you can rely on |
|---|---|---|
| **Stable** | Always on; cannot be switched off | Additive-only for the whole of 1.x. Removal needs two minor releases of notice. |
| **Beta** (enabled) | On by default, labelled Beta | May change in a minor release. Your recorded data is preserved. Removal needs one minor release of notice and a data export. |
| **Beta** (opt-in) | Off until you enable it | As Beta enabled. Inert on a fresh deployment. The setting or field that turns it on is held to the Stable standard. |
| **Experimental** | Off until you enable it | May change in any release, including a patch. No notice. Security fixes only. |

### Stable

- A minor release may add a field, a filter, an optional request parameter, or a
  value to an enum documented as open. It does not change the meaning of an
  existing one.
- Existing names, types, closed enum values, primary-key URLs, documented state
  machines, and permission codenames do not change within 1.x. Renaming is a
  removal plus an addition and follows the removal rule below. That includes
  OpenAPI operation IDs and component names.
- A Stable surface is never removed in a patch release.
- A Stable surface is not regraded to Beta or Experimental. The promise only
  moves toward stronger.
- Every release that changes a Stable persisted surface is rehearsed against a
  restore-first rollback, as described in the [upgrade policy](upgrade.md).

### Beta

- A change may reshape how data is entered or displayed. It does not silently
  discard rows you recorded.
- A breaking change lands only in a minor release, never in a patch.
- Removal comes with an export: the removing release can still get your data out
  in a documented format.
- A Beta capability you have not enabled reports as inactive and does nothing.

### Experimental

- Reached only through an explicit configuration opt-in. Nothing Experimental is
  on by default.
- Functional defects are accepted as feedback, not as regressions.
- Pin the exact revision you validated. No compatible range is claimed.

## Guarantees no grade waives

These apply identically to every grade, and a change that lowers one is handled
as a security change:

1. tenant isolation;
2. authorization, including object-level authorization;
3. preservation of data you recorded;
4. secret handling: no credential, key, or token reaches a log, a task payload,
   or a rendered page;
5. asynchronous correctness: idempotency, finite retry, and an observable
   outcome;
6. a record of who changed what, and when;
7. rollback and export.

Tenant isolation has no exception. Tenant B cannot read, write, list, filter,
export, or reference tenant A's data through any surface, at any grade, for any
account type, including superusers. A view that is merely absent from navigation
is not exempt either.

Permission codenames are stored as plain `app_label.codename` strings in role
definitions. Renaming one invalidates stored grants, which is why codenames are a
Stable surface.

A change made by a scheduled task with no bound user is displayed as a system
action, never as a human one.

## API and wire versioning

- **`/api/` is generation 1 of the API for the whole of 1.x.** The unversioned
  prefix is the name under which generation 1 is promised. There is no version
  segment and no negotiation header. A breaking generation would be mounted
  separately as `/api/v2/`; the existing tree would not be renamed.
- **The checked-in OpenAPI document is the normative contract** for REST and
  SCIM. Paths, methods, parameter names, response codes, security schemes, error
  envelopes, pagination parameters, operation IDs, component names, and enum
  values are all compatibility surfaces. Generate clients from it and pin the
  schema revision you validated against.
- **The webhook envelope carries its own integer `schema_version`**, independent
  of the product version. For wire version 1 the top-level field set is frozen;
  changing it requires a new `schema_version`. Delivery is at-least-once, so
  deduplicate by `event_id`. Reject a `schema_version` you do not support.
- **Enums are open or closed.** A consumer must tolerate an unknown value in an
  open enum. A closed enum is frozen for 1.x and you may switch on it
  exhaustively.

## Configuration renames

A contract-bearing `ITAMBOX_*` setting is not renamed silently. The new name
wins, the old name keeps working for the rest of 1.x, and a startup warning
tells you when the old name supplies the value. Dropping that fallback is a
removal and follows the notice rules below.

Retained for the whole of 1.x and removed no earlier than 2.0:

- the `auto_renewal` alias of `vendor_contract_auto_renews` on subscriptions, in
  the UI model, REST API, and GraphQL API;
- the unprefixed `REQUISITION_AUTO_APPROVAL_THRESHOLDS` setting, a deprecated
  fallback for `ITAMBOX_REQUISITION_AUTO_APPROVAL_THRESHOLDS`;
- integer-keyed SCIM detail routes (`Users/<id>`, `Groups/<id>`), which resolve
  legacy integer IDs alongside the opaque SCIM resource IDs.

## Deprecation

### Who announces and where

The maintainers announce a deprecation. It takes effect only when published in
the release that starts the notice period:

1. in the **Deprecated** section of that release in the
   [changelog](https://github.com/itambox-community/itambox-webapp/blob/main/CHANGELOG.md);
2. in the **release notes** of the GitHub Release;
3. as a **runtime signal** where a running deployment reads the surface: a
   startup warning for a deprecated `ITAMBOX_*` setting, a deprecation marker in
   the OpenAPI document for an API field or parameter, a label in the product
   for a UI surface.

A comment in an issue or discussion is not an announcement. The notice stays in
the changelog until the surface is removed, and the removing release repeats it
with the replacement and the migration step.

### Notice per grade

| Grade | Notice before removal | Removed in | Data export |
|---|---|---|---|
| **Stable** | Two minor releases | A minor release, never a patch | Not provided by this policy |
| **Beta** | One minor release | A minor release, never a patch | The removing release can still export the data in a documented format |
| **Experimental** | None | Any release, including a patch | Not provided |

For Stable, a surface announced as deprecated in `1.4.0` keeps working through
`1.5.0` and `1.6.0` and may be removed in `1.7.0`, not earlier.

The notice period is counted in releases, not calendar time. A deprecated
surface stays functional for the stated number of releases whatever their dates.
A patch release does not remove a Stable or Beta surface.

### What a notice contains

Every notice states:

1. the surface, identified exactly: endpoint and field, setting name, capability,
   route, enum value, report column key, or permission codename;
2. its grade at the time of the notice;
3. the release that announces it and the earliest release that may remove it;
4. the replacement, or that there is none;
5. what you change, and how to check it worked;
6. the data consequence: kept, translated by a migration, or discarded on
   removal, and how to export it where export is promised;
7. the runtime signal, if any.

A notice that omits the surface, the earliest removal release, or the data
consequence is incomplete and does not start the notice period.

### What you do

- Read the **Deprecated** and **Removed** entries of every release between your
  deployment and the target, as part of the [upgrade policy](upgrade.md).
- Treat a startup deprecation warning as a task with a deadline. The notice names
  the earliest removal release.
- Keep an export of Beta data you cannot afford to lose.
- Pin the exact revision of any Experimental surface you use.

### Security exception

A change required to close a vulnerability or to restore one of the guarantees
above can alter or remove a surface in any grade without the notice period. The
changelog states what changed and why.

### What this does not promise

- No calendar-time window and no fixed release cadence.
- No deprecation cycle for anything outside the covered surfaces.
- No retention of a deprecated surface beyond its earliest removal release,
  except where a retention above names a longer period.
- No extension of a notice period on request.

## Related

- [Support policy](support.md)
- [Upgrade policy](upgrade.md)
- [Capability maturity](../operations/capability-maturity.md)
- [Security policy](https://github.com/itambox-community/itambox-webapp/blob/main/SECURITY.md)
