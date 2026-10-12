from django.db import IntegrityError, transaction
from django.test import TestCase

from assets.models import AssetType, AssetTypeFieldset, Category, CategoryDefaultFieldset, Manufacturer
from extras.models import CustomFieldset


class AssetTypeCompositionFoundationTests(TestCase):
    def test_managed_type_connector_identity_is_immutable_under_direct_updates(self):
        connector_identity = "sha256:" + "a" * 64
        manufacturer = Manufacturer.objects.create(name="Immutable DB Manufacturer", slug="immutable-db-manufacturer")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Immutable DB type",
            slug="immutable-db-type",
            management_kind=AssetType.MANAGEMENT_LIBRARY,
            connector_identity=connector_identity,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AssetType._base_manager.filter(pk=asset_type.pk).update(connector_identity="sha256:" + "b" * 64)
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.connector_identity, connector_identity)

    def test_composition_and_category_defaults_are_relational(self):
        manufacturer = Manufacturer.objects.create(name="Example Networks", slug="example-networks")
        first = CustomFieldset.objects.create(namespace="local", slug="product", label="Product")
        second = CustomFieldset.objects.create(namespace="local", slug="networking", label="Networking")
        category = Category.objects.create(name="Foundation Switch", slug="foundation-switch")
        CategoryDefaultFieldset.objects.create(category=category, fieldset=first, position=1)
        CategoryDefaultFieldset.objects.create(category=category, fieldset=second, position=2)
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Switch 48P",
            slug="example-networks-switch-48p",
            category=category,
            region="global",
            configuration="rev-b",
        )
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=first, position=1)
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=second, position=2)

        self.assertEqual(
            list(asset_type.fieldset_memberships.values_list("fieldset__slug", "position")),
            [("product", 1), ("networking", 2)],
        )
        self.assertEqual(
            list(category.default_fieldset_memberships.values_list("fieldset__slug", "position")),
            [("product", 1), ("networking", 2)],
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=first, position=3)

        asset_type.delete()
        asset_type.refresh_from_db()
        self.assertIsNotNone(asset_type.deleted_at)
        self.assertEqual(
            list(asset_type.fieldset_memberships.values_list("fieldset_id", "position")),
            [(first.pk, 1), (second.pk, 2)],
        )
        asset_type.restore()
        asset_type.refresh_from_db()
        self.assertIsNone(asset_type.deleted_at)
        self.assertEqual(asset_type.fieldset_memberships.count(), 2)

    def test_library_managed_type_hard_delete_is_rejected_and_preserves_memberships(self):
        manufacturer = Manufacturer.objects.create(name="Protected Manufacturer", slug="protected-manufacturer")
        fieldset = CustomFieldset.objects.create(namespace="local", slug="protected", label="Protected")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Protected model",
            slug="protected-model",
            management_kind=AssetType.MANAGEMENT_LIBRARY,
        )
        membership = AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            asset_type.delete(force_hard_delete=True)

        self.assertTrue(AssetType.all_objects.filter(pk=asset_type.pk).exists())
        self.assertTrue(AssetTypeFieldset.objects.filter(pk=membership.pk).exists())

    def test_local_asset_type_hard_delete_cascades_owned_memberships(self):
        manufacturer = Manufacturer.objects.create(name="Local Manufacturer", slug="local-manufacturer")
        fieldset = CustomFieldset.objects.create(namespace="local", slug="local", label="Local")
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer,
            model="Local model",
            slug="local-model",
        )
        membership = AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=1)
        asset_type_pk = asset_type.pk
        membership_pk = membership.pk

        asset_type.delete(force_hard_delete=True)

        self.assertFalse(AssetType.all_objects.filter(pk=asset_type_pk).exists())
        self.assertFalse(AssetTypeFieldset.objects.filter(pk=membership_pk).exists())
        self.assertTrue(CustomFieldset.objects.filter(pk=fieldset.pk).exists())
