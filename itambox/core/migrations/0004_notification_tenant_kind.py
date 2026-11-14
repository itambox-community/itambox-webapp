import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_cross_app_relations_late"),
        ("organization", "0002_cross_app_relations"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="organization.tenant",
                verbose_name="Tenant",
            ),
        ),
        migrations.AddField(
            model_name="notification",
            name="kind",
            field=models.CharField(
                choices=[("tenant", "Tenant"), ("personal", "Personal"), ("global", "Global"), ("legacy", "Legacy")],
                db_default="legacy",
                default="legacy",
                max_length=16,
                verbose_name="Kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind", "tenant"), ("tenant__isnull", False))
                | (models.Q(("kind", "tenant"), _negated=True) & models.Q(("tenant__isnull", True))),
                name="core_notification_kind_tenant_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.CheckConstraint(
                condition=models.Q(("user__isnull", False)) | models.Q(("kind", "legacy")),
                name="core_notification_user_required_unless_legacy",
            ),
        ),
    ]
