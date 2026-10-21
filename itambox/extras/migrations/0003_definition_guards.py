"""PostgreSQL guards for reusable custom-field definitions.

Keeps permanent definition identities immutable and undeletable, rejects global
fields inside fieldsets and freezes field activation changes. Fresh installs get
the final trigger set directly.
"""

from django.db import migrations

GUARDS_SQL = """
CREATE OR REPLACE FUNCTION extras_customfield_activation_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.activation = 'global'
       AND EXISTS (
           SELECT 1 FROM extras_customfieldsetfield
           WHERE custom_field_id = NEW.id
       ) THEN
        RAISE EXCEPTION 'A Custom Field with memberships cannot become global'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION extras_customfield_identity_update_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.name IS DISTINCT FROM NEW.name
       OR OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Custom Field identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION extras_customfieldchoice_identity_update_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.choice_set_id IS DISTINCT FROM NEW.choice_set_id
       OR OLD.key IS DISTINCT FROM NEW.key THEN
        RAISE EXCEPTION 'Choice identity is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION extras_customfieldchoiceset_identity_update_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.slug IS DISTINCT FROM NEW.slug
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Choice Set identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION extras_customfieldset_identity_update_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.namespace IS DISTINCT FROM NEW.namespace
       OR OLD.slug IS DISTINCT FROM NEW.slug
       OR OLD.connector_identity IS DISTINCT FROM NEW.connector_identity THEN
        RAISE EXCEPTION 'Custom Fieldset identity or source link is immutable after creation'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION extras_customfieldsetfield_global_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    PERFORM 1
    FROM extras_customfield
    WHERE id = NEW.custom_field_id
    FOR NO KEY UPDATE;
    IF EXISTS (
        SELECT 1 FROM extras_customfield
        WHERE id = NEW.custom_field_id AND activation = 'global'
    ) THEN
        RAISE EXCEPTION 'Global Custom Fields cannot join Fieldsets'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION extras_permanent_definition_delete_guard()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'Reusable definition identities are permanent; deprecate the row instead'
        USING ERRCODE = 'check_violation';
    RETURN OLD;
END;
$function$;

CREATE TRIGGER extras_customfield_activation_guard BEFORE UPDATE OF activation ON extras_customfield FOR EACH ROW EXECUTE FUNCTION extras_customfield_activation_guard();
CREATE TRIGGER extras_customfield_identity_update_guard BEFORE UPDATE OF name, namespace, connector_identity ON extras_customfield FOR EACH ROW EXECUTE FUNCTION extras_customfield_identity_update_guard();
CREATE TRIGGER extras_customfield_permanent_delete_guard BEFORE DELETE ON extras_customfield FOR EACH ROW EXECUTE FUNCTION extras_permanent_definition_delete_guard();
CREATE TRIGGER extras_customfieldchoice_identity_update_guard BEFORE UPDATE OF choice_set_id, key ON extras_customfieldchoice FOR EACH ROW EXECUTE FUNCTION extras_customfieldchoice_identity_update_guard();
CREATE TRIGGER extras_customfieldchoice_permanent_delete_guard BEFORE DELETE ON extras_customfieldchoice FOR EACH ROW EXECUTE FUNCTION extras_permanent_definition_delete_guard();
CREATE TRIGGER extras_customfieldchoiceset_identity_update_guard BEFORE UPDATE OF namespace, slug, connector_identity ON extras_customfieldchoiceset FOR EACH ROW EXECUTE FUNCTION extras_customfieldchoiceset_identity_update_guard();
CREATE TRIGGER extras_customfieldchoiceset_permanent_delete_guard BEFORE DELETE ON extras_customfieldchoiceset FOR EACH ROW EXECUTE FUNCTION extras_permanent_definition_delete_guard();
CREATE TRIGGER extras_customfieldset_identity_update_guard BEFORE UPDATE OF namespace, slug, connector_identity ON extras_customfieldset FOR EACH ROW EXECUTE FUNCTION extras_customfieldset_identity_update_guard();
CREATE TRIGGER extras_customfieldset_permanent_delete_guard BEFORE DELETE ON extras_customfieldset FOR EACH ROW EXECUTE FUNCTION extras_permanent_definition_delete_guard();
CREATE TRIGGER extras_customfieldsetfield_global_guard BEFORE INSERT OR UPDATE OF custom_field_id ON extras_customfieldsetfield FOR EACH ROW EXECUTE FUNCTION extras_customfieldsetfield_global_guard();
"""

GUARDS_REVERSE_SQL = """
DROP TRIGGER IF EXISTS extras_customfield_activation_guard ON extras_customfield;
DROP TRIGGER IF EXISTS extras_customfield_identity_update_guard ON extras_customfield;
DROP TRIGGER IF EXISTS extras_customfield_permanent_delete_guard ON extras_customfield;
DROP TRIGGER IF EXISTS extras_customfieldchoice_identity_update_guard ON extras_customfieldchoice;
DROP TRIGGER IF EXISTS extras_customfieldchoice_permanent_delete_guard ON extras_customfieldchoice;
DROP TRIGGER IF EXISTS extras_customfieldchoiceset_identity_update_guard ON extras_customfieldchoiceset;
DROP TRIGGER IF EXISTS extras_customfieldchoiceset_permanent_delete_guard ON extras_customfieldchoiceset;
DROP TRIGGER IF EXISTS extras_customfieldset_identity_update_guard ON extras_customfieldset;
DROP TRIGGER IF EXISTS extras_customfieldset_permanent_delete_guard ON extras_customfieldset;
DROP TRIGGER IF EXISTS extras_customfieldsetfield_global_guard ON extras_customfieldsetfield;
DROP FUNCTION IF EXISTS extras_customfield_activation_guard();
DROP FUNCTION IF EXISTS extras_customfield_identity_update_guard();
DROP FUNCTION IF EXISTS extras_customfieldchoice_identity_update_guard();
DROP FUNCTION IF EXISTS extras_customfieldchoiceset_identity_update_guard();
DROP FUNCTION IF EXISTS extras_customfieldset_identity_update_guard();
DROP FUNCTION IF EXISTS extras_customfieldsetfield_global_guard();
DROP FUNCTION IF EXISTS extras_permanent_definition_delete_guard();
"""


class Migration(migrations.Migration):
    replaces = [
        ("extras", "0102_alter_event_action"),
        ("extras", "0103_remove_reporttemplate_advanced_mode_and_more"),
        ("extras", "0104_issue183_alert_tenant_reconciliation"),
        ("extras", "0105_reporttemplate_advanced_mode_and_more"),
        ("extras", "0106_scheduledreportscopeauthorization"),
        ("extras", "0107_scheduledreportscopeauthorization_revocation"),
        ("extras", "0108_alertlog_delivery_outcome"),
        ("extras", "0109_webhookdelivery"),
        ("extras", "0110_issue445_task_paths"),
        ("extras", "0111_webhookdelivery_target_claim"),
        ("extras", "0112_backfill_webhookdelivery_targets"),
        ("extras", "0113_upgrade_legacy_webhook_retry_schedules"),
        ("extras", "0114_asset_type_definition_library_schema"),
        ("extras", "0115_asset_type_definition_conversion"),
        ("extras", "0116_asset_type_definition_cutover"),
        ("extras", "0117_asset_type_definition_guards"),
        ("extras", "0122_journalentry_tenant_group"),
        ("extras", "0123_pause_flag_suppressed_report_schedules"),
        ("extras", "0124_scheduled_report_fire_identity_delivery_outcomes"),
        ("extras", "0125_scheduled_report_fire_records_retry_hardening"),
        ("extras", "0126_scheduledreport_last_run_archive"),
        ("extras", "0127_retire_report_designer_legacy"),
        ("extras", "0128_tenant_archive_leaf_markers"),
        ("extras", "0129_remove_type_library"),
        ("extras", "0130_definition_translations"),
    ]

    dependencies = [
        ("extras", "0002_cross_app_relations"),
    ]

    operations = [
        migrations.RunSQL(GUARDS_SQL, reverse_sql=GUARDS_REVERSE_SQL),
    ]
