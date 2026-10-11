from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0103_stock_leaf_deleted_at"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="consumable",
            name="inventory_consumable_unique_manufacturer_name",
        ),
        migrations.AddConstraint(
            model_name="consumable",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True), ("tenant__isnull", False)),
                fields=("tenant", "manufacturer", "name"),
                name="inventory_consumable_unique_tenant_manufacturer_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="consumable",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True), ("tenant__isnull", True)),
                fields=("manufacturer", "name"),
                name="inventory_consumable_unique_global_manufacturer_name",
            ),
        ),
    ]
