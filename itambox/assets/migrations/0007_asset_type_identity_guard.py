"""PostgreSQL guard that keeps Asset Type connector identity immutable and core types undeletable."""

from django.db import migrations

ASSET_TYPE_GUARD_SQL = """
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
DROP FUNCTION IF EXISTS assettype_specification_identity_guard();
"""


class Migration(migrations.Migration):
    replaces = [
        ("assets", "0107_asset_type_specification_guards"),
        ("assets", "0118_assetdisposal_cancellation_reason_and_more"),
        ("assets", "0119_warranty_supplier"),
        ("assets", "0120_repair_episode"),
        ("assets", "0121_supplier_scoping_and_commercial_fields"),
        ("assets", "0122_repair_maintenance_anchor"),
        ("assets", "0123_asset_archive_leaf_markers"),
        ("assets", "0124_tenant_archive_leaf_markers"),
        ("assets", "0125_unique_open_loan_per_maintenance"),
        ("assets", "0126_remove_type_library_identity"),
        ("assets", "0127_core_vocabulary_german_translations"),
    ]

    dependencies = [
        ("assets", "0006_core_vocabulary_german_translations"),
    ]

    operations = [
        migrations.RunSQL(ASSET_TYPE_GUARD_SQL, reverse_sql=ASSET_TYPE_GUARD_REVERSE_SQL),
    ]
