"""Give the ``scim_id`` unique constraints their canonical names.

Databases created by earlier prereleases carry hash-suffixed ``*_scim_id_<hash>_uniq``
constraint names; a fresh install names them ``*_scim_id_key``. Renaming the
legacy ones makes both histories converge on one schema. Idempotent.
"""

from django.db import migrations

RENAME_SQL = """
DO $$
DECLARE
    legacy record;
BEGIN
    FOR legacy IN
        SELECT c.conrelid::regclass::text AS table_name, c.conname AS old_name
        FROM pg_constraint c
        WHERE c.contype = 'u'
          AND c.conrelid::regclass::text IN ('users_user', 'users_usergroup')
          AND c.conname ~ '^users_(user|usergroup)_scim_id_[0-9a-f]+_uniq$'
    LOOP
        EXECUTE format(
            'ALTER TABLE %s RENAME CONSTRAINT %I TO %I',
            legacy.table_name,
            legacy.old_name,
            legacy.table_name || '_scim_id_key'
        );
    END LOOP;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0001_baseline"),
    ]

    operations = [
        migrations.RunSQL(RENAME_SQL, reverse_sql=migrations.RunSQL.noop),
    ]
