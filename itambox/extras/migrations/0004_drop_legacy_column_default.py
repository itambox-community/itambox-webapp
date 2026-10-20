"""Drop a database default left on a column by an older migration.

Databases created by earlier prereleases carry ``DEFAULT ''`` on
``extras_reporttemplate.template_content``; the model supplies the value, so a
fresh install never had it. Dropping it makes both histories converge on one
schema. Idempotent.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("extras", "0003_definition_guards"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE extras_reporttemplate ALTER COLUMN template_content DROP DEFAULT;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
