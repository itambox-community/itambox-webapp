from django.db import migrations

from ._core_vocabulary import CORE_VOCABULARY


def seed_translations(apps, schema_editor):
    from assets.services.specifications.core_vocabulary_translations import seed_core_vocabulary_translations

    seed_core_vocabulary_translations(
        apps.get_model,
        using=schema_editor.connection.alias,
        vocabulary=CORE_VOCABULARY,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0126_remove_type_library_identity"),
        ("extras", "0130_definition_translations"),
        ("users", "0100_issue88_shard_62_users_relations"),
    ]

    operations = [migrations.RunPython(seed_translations, migrations.RunPython.noop)]
