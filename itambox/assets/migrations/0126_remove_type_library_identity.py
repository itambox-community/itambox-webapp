"""Remove the in-app Type Library identity from Asset Types (issue #768).

The Library link, its definition key and the Library-only lifecycle columns go
with the Type Library engine. The identity guard installed by 0107 watches the
Library columns, and dropping a column cascades to a trigger that names it, so
the guard is first recreated on the retained connector identity: connector
provenance stays immutable and core/library-managed Asset Types stay protected
against hard deletion.
"""

from django.db import migrations

ASSET_TYPE_GUARD_SQL = """
DROP TRIGGER IF EXISTS assettype_specification_identity_guard ON assets_assettype;

CREATE OR REPLACE FUNCTION assettype_specification_identity_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Asset Type specification identity is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' AND OLD.management_kind IN ('core', 'library') THEN
        RAISE EXCEPTION 'Core and library Asset Type identities cannot be hard-deleted'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER assettype_specification_identity_guard
BEFORE UPDATE OF connector_identity OR DELETE
ON assets_assettype
FOR EACH ROW
EXECUTE FUNCTION assettype_specification_identity_guard();
"""

ASSET_TYPE_GUARD_REVERSE_SQL = """
DROP TRIGGER IF EXISTS assettype_specification_identity_guard ON assets_assettype;

CREATE OR REPLACE FUNCTION assettype_specification_identity_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND (OLD.library_id IS DISTINCT FROM NEW.library_id
            OR OLD.library_definition_key IS DISTINCT FROM NEW.library_definition_key
            OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity) THEN
        RAISE EXCEPTION 'Asset Type specification identity is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' AND OLD.management_kind IN ('core', 'library') THEN
        RAISE EXCEPTION 'Core and library Asset Type identities cannot be hard-deleted'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER assettype_specification_identity_guard
BEFORE UPDATE OF library_id, library_definition_key, connector_identity OR DELETE
ON assets_assettype
FOR EACH ROW
EXECUTE FUNCTION assettype_specification_identity_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0125_unique_open_loan_per_maintenance"),
        ("users", "0100_issue88_shard_62_users_relations"),
    ]

    operations = [
        # Reverse order matters: on rollback the columns come back before the
        # original guard that names them is reinstalled.
        migrations.RunSQL(ASSET_TYPE_GUARD_SQL, reverse_sql=ASSET_TYPE_GUARD_REVERSE_SQL),
        migrations.RemoveConstraint(model_name="assettype", name="assettype_library_identity_uq"),
        migrations.RemoveConstraint(model_name="assettype", name="assettype_library_identity_ck"),
        migrations.RemoveConstraint(model_name="assettype", name="assettype_library_mgmt_ck"),
        migrations.RemoveField(model_name="assettype", name="deprecated_at"),
        migrations.RemoveField(model_name="assettype", name="library"),
        migrations.RemoveField(model_name="assettype", name="library_definition_key"),
        migrations.RemoveField(model_name="assettype", name="lifecycle"),
        migrations.RemoveField(model_name="assettype", name="replaced_by"),
    ]
