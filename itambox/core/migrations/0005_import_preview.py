import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_notification_tenant_kind"),
        ("organization", "0002_cross_app_relations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportPreview",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("app_label", models.CharField(max_length=100)),
                ("model_name", models.CharField(max_length=100)),
                ("payload", models.JSONField(blank=True, default=list, verbose_name="Rows")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="organization.tenant",
                        verbose_name="Tenant",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="import_previews",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Import Preview",
                "verbose_name_plural": "Import Previews",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="importpreview",
            index=models.Index(
                fields=["user", "tenant", "consumed_at", "created_at"],
                name="core_import_user_id_4e2c6f_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="importpreview",
            index=models.Index(fields=["user", "created_at"], name="core_import_user_id_9a1b3d_idx"),
        ),
    ]
