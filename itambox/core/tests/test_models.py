from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase

from assets.models import Asset, AssetRole, Manufacturer
from core.models import Notification
from core.tasks.context import TaskContext
from extras.models import Bookmark, JournalEntry
from inventory.models import Accessory
from itambox.utils import serialize_object
from software.models import Software

User = get_user_model()


class CoreModelsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="password123", is_superuser=True)
        self.manufacturer = Manufacturer.objects.create(name="Microsoft", slug="microsoft")
        self.software = Software.objects.create(name="Windows 11", manufacturer=self.manufacturer)

    def test_serialize_object_exclude_fields(self):
        """Test that serialize_object respects exclude_fields parameter."""
        data_all = serialize_object(self.software)
        self.assertIn("name", data_all)
        self.assertIn("manufacturer", data_all)

        data_excluded = serialize_object(self.software, exclude_fields={"name"})
        self.assertNotIn("name", data_excluded)
        self.assertIn("manufacturer", data_excluded)

    def test_serialize_object_with_fk(self):
        data = serialize_object(self.software)
        self.assertEqual(data["name"], "Windows 11")
        self.assertEqual(data["manufacturer"], self.manufacturer.pk)
        self.assertIn("description", data)

    def test_notification_creation(self):
        notif = Notification.objects.create(
            user=self.user,
            message="This is a test notification",
        )
        self.assertFalse(notif.is_read)

    def test_bookmark_creation(self):
        maker_ct = ContentType.objects.get_for_model(self.manufacturer)
        bookmark = Bookmark.objects.create(
            user=self.user,
            model=maker_ct,
            object_id=self.manufacturer.pk,
        )
        self.assertEqual(bookmark.user, self.user)

    def test_journal_entry_creation(self):
        maker_ct = ContentType.objects.get_for_model(self.manufacturer)
        entry = JournalEntry.objects.create(
            comment="Test journal note",
            user=self.user,
            model=maker_ct,
            object_id=self.manufacturer.pk,
        )
        self.assertIsNotNone(entry.created_at)

    def test_soft_delete_accessory(self):
        acc = Accessory.objects.create(name="Test Accessory", manufacturer=self.manufacturer)
        acc.delete()
        self.assertIsNotNone(acc.deleted_at)
        self.assertEqual(Accessory.objects.filter(pk=acc.pk).count(), 0)

    def test_soft_delete_does_not_cascade_to_children(self):
        """A soft delete only tombstones the row itself; children are retired by the archive service (#619)."""
        from software.models import InstalledSoftware

        role = AssetRole.objects.create(name="Desktop", slug="desktop")
        asset = Asset.objects.create(name="Test Desktop", asset_tag="TAG-CSD", asset_role=role)

        installed_sw = InstalledSoftware.objects.create(asset=asset, software=self.software, version_detected="1.0.0")

        self.assertEqual(InstalledSoftware.objects.filter(pk=installed_sw.pk).count(), 1)

        with TaskContext(user_id=self.user.pk, operation="soft_delete_test"):
            asset.delete()

        self.assertIsNotNone(asset.deleted_at)
        self.assertEqual(InstalledSoftware.objects.filter(pk=installed_sw.pk).count(), 1)

    def test_custom_validator_integration(self):
        """Test settings-based validation on a model and verify it raises friendly field-level validation errors."""
        from django.test import override_settings

        custom_validators = {"assets.manufacturer": {"name": {"min_length": 5, "pattern": "^[A-Z].*$"}}}

        with override_settings(CUSTOM_VALIDATORS=custom_validators):
            mfr_invalid_len = Manufacturer(name="Abc", slug="abc")
            with self.assertRaises(ValidationError) as ctx:
                mfr_invalid_len.full_clean()
            self.assertIn("name", ctx.exception.error_dict)

            mfr_invalid_pattern = Manufacturer(name="lhp-corp", slug="lhp-corp")
            with self.assertRaises(ValidationError) as ctx:
                mfr_invalid_pattern.full_clean()
            self.assertIn("name", ctx.exception.error_dict)

            mfr_valid = Manufacturer(name="Lenovo", slug="lenovo-valid")
            mfr_valid.full_clean()
