# Upgrade policy for 1.x

This page states which upgrades are supported, what you do before you start, and
how a failed upgrade is undone. The step-by-step procedures are in
[Updating a deployment](../operations/upgrades.md); this policy states the rules
those procedures serve.

## Supported upgrade origin

- **Each release declares its supported origin**, and that declaration is
  authoritative. The origin belongs to the target release, not to tag order: a
  `1.0.x` patch can be published after `1.1.0` and keeps its own origin.
- **The upgrade from the declared origin is verified** on the qualification
  stack before the release is published. The
  [qualification record](../operations/upgrade-qualification.md) lists the runs.
- **For a new minor release, the current patch of the previous line is the
  natural origin.** `1.1.0` declares the latest `1.0.x`, not `1.0.0`.
- **Skipping patch releases within one line is supported only when the
  qualification checks the oldest still-supported patch against the target.**
  Until that run exists, upgrade through each declared origin.
- **A jump across minor lines is not supported** unless the target release names
  the revision it was qualified from.
- **Arbitrary revisions are not supported origins**, including development
  snapshots of `main`. The migration preflight fails closed on a database state
  it does not recognize instead of rewriting its history.
- **Prereleases are not supported origins** except where the target release
  names one; `v1.0.0` names `v1.0.0-rc.2`.

Migrations are written and rehearsed against one predecessor. A migration may
assume the data shape its predecessor left, may be irreversible, and may be
replaced by a later squash. The project tests the one pair it declares and does
not imply the rest.

## Before you upgrade

Do all of the following before changing the running deployment:

1. **Select an exact target**: a release tag, with its image digest or full Git
   SHA. Do not follow a branch or a mutable tag.
2. **Read the changelog and the [upgrade guide](../operations/upgrades.md)** for
   every release between your deployment and the target, including any section
   that describes a behavior change or a one-time data transition.
3. **Record the rollback point**: the current revision or image digest, and the
   exact `db.image` reference.
4. **Take the complete backup set and verify it can be read.** The set is the
   database dump, the media archive, and the runtime secrets and encryption keys,
   taken together, as defined in [Backup and restore](../operations/backup-restore.md).
   A database dump without the matching secrets and media is not a rollback source.
5. **Run the preflight** from the candidate against the intended database:
   `manage.py migration_baseline_preflight --format=json`. A non-zero exit is a
   stop condition. The database is not at a recognized state: restore the
   verified predecessor and investigate. Do not proceed.
6. **Rehearse on a restored copy** of production when the release changes
   migrations, storage, or encryption. The
   [recovery qualification drill](../operations/recovery-drill.md) is the
   procedure the project uses; you may follow it on your own copy.
7. **Plan a maintenance window.** The application and worker are stopped during
   migration. Migrations are not guaranteed to be compatible with the old
   application.

## Rollback

!!! warning "Rollback is restore-first"
    Do not assume a migration can be reversed. A migration may be irreversible,
    or may discard data when reversed. Once a migration has started, a code-only
    rollback is not a rollback.

To roll back a failed or unwanted upgrade:

1. Stop the application and the worker. Do not admit traffic.
2. Restore the retained predecessor image, or check out and rebuild the recorded
   predecessor revision. A rebuild is not necessarily byte-identical when base
   images or ranged dependencies have moved, which is why you record the image
   digest.
3. Restore the matching database, media, and secrets from the pre-upgrade backup
   set, into clean storage.
4. Start the predecessor and verify login, attachments, encrypted fields, and
   background processing.

Consequences you accept when you roll back:

- Data written after the backup was taken is lost. Export what you need first.
- If the target release changed security behavior, the predecessor may not be a
  security-equivalent rollback. The changelog and upgrade guide say so for the
  releases where it applies.
- Return to the predecessor only together with the restored backup, unless that
  specific migration is documented as reversible and you reverse it deliberately
  with the predecessor code in place.

If the upgrade fails before any migration ran, returning to the previous revision
may be sufficient. If you cannot tell whether a migration started, treat it as
started: a missing recorder row never proves that no schema or data change
occurred.

Each release that changes a Stable persisted surface is rehearsed against a
restore-first rollback and a repeat upgrade before it is published.

## PostgreSQL major versions

!!! danger "Changing the database image tag is not an upgrade"
    A new PostgreSQL major version does not read the old cluster's files. The
    stack can start an empty database that reports healthy while your data sits
    untouched in the old volume. Read
    [Major-version database upgrade](../operations/upgrades.md#major-version-database-upgrade-bundled-postgresql-16-to-18)
    before touching the database image.

- **Supported majors are 16 and 18**, as stated in the
  [installation guide](../operations/installation.md). The bundled stack and the
  primary test lanes run 18; a compatibility lane keeps 16 under test. Any other
  major is unsupported.
- **A PostgreSQL major-version change is a separate operation from an application
  upgrade.** Do not combine them. Complete and verify one, then start the other.
- **Move the data through PostgreSQL itself**, by logical dump and restore or by
  `pg_upgrade`, as set out in the upgrade guide.
- **Keep the old cluster untouched until the new one is verified.** It is the
  fastest rollback.
- The application requires the `btree_gist` and `pg_trgm` extensions. An
  externally managed database must provide them.
- When a release changes the supported PostgreSQL majors, its changelog says so.
  Until you perform the database operation yourself, your database stays on the
  major it already runs.

## What an upgrade is not

- Not a guarantee that every migration is reversible.
- Not a promise that a deployment more than one release behind can reach the
  target directly.
- Not a substitute for a verified backup. The project cannot recover a deployment
  that has none.

## Related

- [Support policy](support.md)
- [Compatibility policy](compatibility.md)
- [Updating a deployment](../operations/upgrades.md)
- [Recovery qualification drill](../operations/recovery-drill.md)
