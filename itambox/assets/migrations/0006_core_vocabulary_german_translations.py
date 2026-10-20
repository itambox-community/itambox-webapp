from django.db import migrations

from ._core_vocabulary import CORE_VOCABULARY


def seed_translations(apps, schema_editor):
    # inline import: app-registry: assets.services imports models, which must not load with the migration module
    from assets.services.specifications.core_vocabulary_translations import seed_core_vocabulary_translations

    seed_core_vocabulary_translations(
        apps.get_model,
        using=schema_editor.connection.alias,
        vocabulary=CORE_VOCABULARY,
    )


class Migration(migrations.Migration):
    replaces = [
        ("assets", "0100_issue88_shard_17_assets_history_4"),
        ("assets", "0100_issue88_shard_29_assets_history_5"),
        ("assets", "0100_issue88_shard_31_assets_history_6"),
        ("assets", "0100_issue88_shard_36_assets_history_7"),
        ("assets", "0100_issue88_shard_42_assets_relations"),
        ("assets", "0100_issue88_shard_43_assets_seed"),
        ("assets", "0101_seed_canonical_missing_status"),
        ("assets", "0102_asset_type_composition_schema"),
        ("assets", "0103_asset_type_specification_conversion"),
        ("assets", "0104_asset_type_core_vocabulary"),
        ("assets", "0105_asset_type_provenance_capture"),
        ("assets", "0106_asset_type_composition_cutover"),
    ]

    dependencies = [
        ("assets", "0005_core_vocabulary"),
        ("extras", "0003_definition_guards"),
    ]

    operations = [migrations.RunPython(seed_translations, migrations.RunPython.noop)]
