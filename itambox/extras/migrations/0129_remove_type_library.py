"""Remove the in-app Type Library engine's persisted state (issue #768).

The three Library models, the Library links on reusable definitions and their
coherence constraints go with the engine; ``management_kind`` and the connector
identity stay. Ordering is deliberate:

1. The definition identity guards installed by 0117 name ``library_id`` in
   their trigger column lists, and dropping that column would cascade to the
   triggers. They are recreated first on the retained identity columns.
2. The Library-only guards and archive constraints are dropped before their
   tables, so a rollback restores them only after the tables exist again.
3. The Library permissions are removed from the permission table and from the
   literal ``app_label.codename`` grants persisted in role JSON, so upgraded
   installations neither offer nor store a permission for a removed surface.
   Only roles that carry one of these strings are rewritten.
"""

from django.db import migrations

RETIRED_MODELS = ("specificationlibrary", "specificationlibraryrelease", "specificationlibrarylegacyprovenance")
RETIRED_PERMISSIONS = frozenset(
    [f"extras.{action}_{model}" for model in RETIRED_MODELS for action in ("view", "add", "change", "delete")]
    + ["extras.manage_specification_library"]
)

DEFINITION_IDENTITY_GUARDS_SQL = """
DROP TRIGGER IF EXISTS extras_customfield_identity_update_guard ON extras_customfield;
CREATE OR REPLACE FUNCTION extras_customfield_identity_update_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.name IS DISTINCT FROM NEW.name
       OR OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Custom Field identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER extras_customfield_identity_update_guard
BEFORE UPDATE OF name, namespace, connector_identity ON extras_customfield FOR EACH ROW
EXECUTE FUNCTION extras_customfield_identity_update_guard();

DROP TRIGGER IF EXISTS extras_customfieldset_identity_update_guard ON extras_customfieldset;
CREATE OR REPLACE FUNCTION extras_customfieldset_identity_update_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.slug IS DISTINCT FROM NEW.slug
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Custom Fieldset identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER extras_customfieldset_identity_update_guard
BEFORE UPDATE OF namespace, slug, connector_identity ON extras_customfieldset FOR EACH ROW
EXECUTE FUNCTION extras_customfieldset_identity_update_guard();

DROP TRIGGER IF EXISTS extras_customfieldchoiceset_identity_update_guard ON extras_customfieldchoiceset;
CREATE OR REPLACE FUNCTION extras_customfieldchoiceset_identity_update_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.slug IS DISTINCT FROM NEW.slug
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Choice Set identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER extras_customfieldchoiceset_identity_update_guard
BEFORE UPDATE OF namespace, slug, connector_identity ON extras_customfieldchoiceset FOR EACH ROW
EXECUTE FUNCTION extras_customfieldchoiceset_identity_update_guard();
"""

DEFINITION_IDENTITY_GUARDS_REVERSE_SQL = """
DROP TRIGGER IF EXISTS extras_customfield_identity_update_guard ON extras_customfield;
CREATE OR REPLACE FUNCTION extras_customfield_identity_update_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.name IS DISTINCT FROM NEW.name
       OR OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.library_id IS DISTINCT FROM NEW.library_id
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Custom Field identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER extras_customfield_identity_update_guard
BEFORE UPDATE OF name, namespace, library_id, connector_identity ON extras_customfield FOR EACH ROW
EXECUTE FUNCTION extras_customfield_identity_update_guard();

DROP TRIGGER IF EXISTS extras_customfieldset_identity_update_guard ON extras_customfieldset;
CREATE OR REPLACE FUNCTION extras_customfieldset_identity_update_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.slug IS DISTINCT FROM NEW.slug
       OR OLD.library_id IS DISTINCT FROM NEW.library_id
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Custom Fieldset identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER extras_customfieldset_identity_update_guard
BEFORE UPDATE OF namespace, slug, library_id, connector_identity ON extras_customfieldset FOR EACH ROW
EXECUTE FUNCTION extras_customfieldset_identity_update_guard();

DROP TRIGGER IF EXISTS extras_customfieldchoiceset_identity_update_guard ON extras_customfieldchoiceset;
CREATE OR REPLACE FUNCTION extras_customfieldchoiceset_identity_update_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.slug IS DISTINCT FROM NEW.slug
       OR OLD.library_id IS DISTINCT FROM NEW.library_id
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Choice Set identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER extras_customfieldchoiceset_identity_update_guard
BEFORE UPDATE OF namespace, slug, library_id, connector_identity ON extras_customfieldchoiceset FOR EACH ROW
EXECUTE FUNCTION extras_customfieldchoiceset_identity_update_guard();
"""

LIBRARY_GUARDS_DROP_SQL = """
ALTER TABLE extras_specificationlibrarylegacyprovenance
    DROP CONSTRAINT IF EXISTS specificationlibrarylegacyprovenance_disposition_valid,
    DROP CONSTRAINT IF EXISTS specificationlibrarylegacyprovenance_owner_kind_valid;
ALTER TABLE extras_specificationlibraryrelease
    DROP CONSTRAINT IF EXISTS specificationlibraryrelease_document_object,
    DROP CONSTRAINT IF EXISTS specificationlibraryrelease_digest_format;
DROP TRIGGER IF EXISTS legacy_provenance_archive_guard ON extras_specificationlibrarylegacyprovenance;
DROP FUNCTION IF EXISTS legacy_provenance_archive_guard();
DROP TRIGGER IF EXISTS specificationlibraryrelease_immutable_guard ON extras_specificationlibraryrelease;
DROP FUNCTION IF EXISTS specificationlibraryrelease_immutable_guard();
DROP TRIGGER IF EXISTS specificationlibrary_identity_delete_guard ON extras_specificationlibrary;
DROP FUNCTION IF EXISTS specificationlibrary_identity_delete_guard();
"""

LIBRARY_GUARDS_RESTORE_SQL = """
CREATE OR REPLACE FUNCTION specificationlibrary_identity_delete_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Specification Library identities are permanent'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.namespace IS DISTINCT FROM NEW.namespace THEN
        RAISE EXCEPTION 'Specification Library namespace is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.accepted_release_id IS DISTINCT FROM NEW.accepted_release_id THEN
        IF COALESCE(current_setting('itambox.specification_library_reconcile', true), 'off') <> 'on' THEN
            RAISE EXCEPTION 'Accepted Release changes require the library reconciliation path'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.accepted_release_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
               FROM extras_specificationlibraryrelease
               WHERE id = NEW.accepted_release_id
                 AND library_id = NEW.id
           ) THEN
            RAISE EXCEPTION 'Accepted Release must belong to the same Specification Library'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER specificationlibrary_identity_delete_guard
BEFORE UPDATE OF namespace, accepted_release_id OR DELETE
ON extras_specificationlibrary
FOR EACH ROW
EXECUTE FUNCTION specificationlibrary_identity_delete_guard();

CREATE OR REPLACE FUNCTION specificationlibraryrelease_immutable_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Specification Library Release rows are immutable and retained'
        USING ERRCODE = 'check_violation';
    RETURN OLD;
END;
$$;
CREATE TRIGGER specificationlibraryrelease_immutable_guard
BEFORE UPDATE OR DELETE ON extras_specificationlibraryrelease
FOR EACH ROW
EXECUTE FUNCTION specificationlibraryrelease_immutable_guard();

CREATE OR REPLACE FUNCTION legacy_provenance_archive_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Legacy provenance rows are immutable transition evidence'
        USING ERRCODE = 'check_violation';
    RETURN OLD;
END;
$$;
CREATE TRIGGER legacy_provenance_archive_guard
BEFORE UPDATE OR DELETE ON extras_specificationlibrarylegacyprovenance
FOR EACH ROW
EXECUTE FUNCTION legacy_provenance_archive_guard();

ALTER TABLE extras_specificationlibraryrelease
    ADD CONSTRAINT specificationlibraryrelease_digest_format
        CHECK (semantic_digest ~ '^sha256:[0-9a-f]{64}$'),
    ADD CONSTRAINT specificationlibraryrelease_document_object
        CHECK (jsonb_typeof(source_document) = 'object');
ALTER TABLE extras_specificationlibrarylegacyprovenance
    ADD CONSTRAINT specificationlibrarylegacyprovenance_owner_kind_valid
        CHECK (owner_kind IN ('library', 'asset_type', 'custom_field', 'custom_fieldset', 'choice_set', 'choice')),
    ADD CONSTRAINT specificationlibrarylegacyprovenance_disposition_valid
        CHECK (disposition IN ('uninitialized', 'unreconciled'));
"""


def retire_library_permissions(apps, schema_editor):
    Role = apps.get_model("organization", "Role")
    Permission = apps.get_model("auth", "Permission")
    for role in Role._base_manager.all().only("pk", "permissions").iterator():
        permissions = list(role.permissions or [])
        retained = [permission for permission in permissions if permission not in RETIRED_PERMISSIONS]
        if retained == permissions:
            continue
        role.permissions = retained
        role.save(update_fields=["permissions"])
    Permission.objects.filter(content_type__app_label="extras", content_type__model__in=RETIRED_MODELS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0126_remove_type_library_identity"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("extras", "0128_tenant_archive_leaf_markers"),
        ("organization", "0104_tenant_archive_leaf_markers"),
        ("users", "0100_issue88_shard_62_users_relations"),
    ]

    operations = [
        migrations.RunSQL(DEFINITION_IDENTITY_GUARDS_SQL, reverse_sql=DEFINITION_IDENTITY_GUARDS_REVERSE_SQL),
        migrations.RunSQL(LIBRARY_GUARDS_DROP_SQL, reverse_sql=LIBRARY_GUARDS_RESTORE_SQL),
        # Retired grants cannot be reconstructed; post_migrate recreates the permission rows on rollback.
        migrations.RunPython(retire_library_permissions, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="customfield", name="customfield_library_management_coherence"),
        migrations.RemoveConstraint(
            model_name="customfieldchoiceset", name="customfieldchoiceset_library_management_coherence"
        ),
        migrations.RemoveConstraint(model_name="customfieldset", name="customfieldset_library_management_coherence"),
        migrations.RemoveField(model_name="customfield", name="library"),
        migrations.RemoveField(model_name="customfieldchoiceset", name="library"),
        migrations.RemoveField(model_name="customfieldset", name="library"),
        migrations.RemoveField(model_name="specificationlibrary", name="accepted_release"),
        migrations.DeleteModel(name="SpecificationLibraryLegacyProvenance"),
        migrations.DeleteModel(name="SpecificationLibraryRelease"),
        migrations.DeleteModel(name="SpecificationLibrary"),
    ]
