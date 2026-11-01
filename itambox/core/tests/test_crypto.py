import os
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase

from assets.models import Manufacturer
from core.crypto import decrypt_string, encrypt_string, get_fernet
from licenses.models import License
from software.models import Software

User = get_user_model()


class CoreCryptoTestCase(TestCase):
    def test_malformed_configured_key_fails_closed_without_disclosing_key(self):
        secret_key_material = "not-a-valid-fernet-key-secret"

        with patch.dict(
            os.environ,
            {"ITAMBOX_FIELD_ENCRYPTION_KEYS": secret_key_material},
        ):
            with self.assertRaises(ImproperlyConfigured) as raised:
                get_fernet()

        message = str(raised.exception)
        self.assertIn("index 1", message)
        self.assertNotIn(secret_key_material, message)

    def test_decrypt_string_maps_invalid_ciphertext_to_value_error(self):
        for cipher in ("enc$not-a-fernet-token", "enc$äö", "enc$" + "A" * 8):
            with self.subTest(cipher=cipher):
                with self.assertRaises(ValueError) as raised:
                    decrypt_string(cipher)
                self.assertTrue(str(raised.exception).startswith("Decryption failed"))

    def test_decrypt_string_does_not_swallow_unexpected_errors(self):
        class _BrokenFernet:
            @staticmethod
            def decrypt(_token):
                raise RuntimeError("keyring backend exploded")

        cipher = encrypt_string("x")
        with patch("core.crypto.get_fernet", return_value=_BrokenFernet()):
            with self.assertRaises(RuntimeError):
                decrypt_string(cipher)

    def test_multi_key_encryption_consolidation(self):
        """Test encryption key rotation using MultiFernet."""
        key1 = Fernet.generate_key().decode("ascii")
        key2 = Fernet.generate_key().decode("ascii")

        os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = f"{key1},{key2}"
        plain = "SuperSecretToken"
        cipher = encrypt_string(plain)

        decrypted = decrypt_string(cipher)
        self.assertEqual(decrypted, plain)

        os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = f"{key2},{key1}"
        decrypted_rotated = decrypt_string(cipher)
        self.assertEqual(decrypted_rotated, plain)

        if "ITAMBOX_FIELD_ENCRYPTION_KEYS" in os.environ:
            del os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"]

    def test_old_ciphertext_decrypts_when_new_key_is_primary_and_old_retained(self):
        """Rotation contract: old ciphertext stays readable while the old key remains in the ring."""
        old_key = Fernet.generate_key().decode("ascii")
        new_key = Fernet.generate_key().decode("ascii")
        plain = "rotation-compat-plaintext"

        os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = old_key
        try:
            legacy_cipher = encrypt_string(plain)

            # New key first, old key retained: old ciphertext decrypts, new
            # ciphertext is produced under the primary (new) key.
            os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = f"{new_key},{old_key}"
            self.assertEqual(decrypt_string(legacy_cipher), plain)

            new_cipher = encrypt_string(plain)
            # Fernet embeds a random IV and timestamp, so two ciphertexts
            # differ even under one key — the primary-key binding is proven by
            # the new-key-only decrypt step right below, not by a comparison.
            # The real proof is that new_cipher decrypts with ONLY the new key.

            # The new ciphertext is bound to the primary key alone.
            os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = new_key
            self.assertEqual(decrypt_string(new_cipher), plain)
        finally:
            if "ITAMBOX_FIELD_ENCRYPTION_KEYS" in os.environ:
                del os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"]

    def test_decryption_never_rewrites_stored_ciphertext(self):
        """Reading a value must not silently re-encrypt it (no automatic data rewrite)."""
        old_key = Fernet.generate_key().decode("ascii")
        new_key = Fernet.generate_key().decode("ascii")
        mfr = Manufacturer.objects.create(name="Microsoft", slug="microsoft")
        software = Software.objects.create(name="Office 365", version="v2026", manufacturer=mfr)

        os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = old_key
        try:
            raw_product_key = "NO-REWRITE-PRODUCT-KEY-2026"
            license_obj = License.objects.create(
                name="Office Suite", software=software, seats=10, product_key=encrypt_string(raw_product_key)
            )
            stored_before = license_obj.product_key

            os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = f"{new_key},{old_key}"
            # Decryption through the model property must not touch the row.
            self.assertEqual(license_obj.decrypted_product_key, raw_product_key)
            license_obj.refresh_from_db()
            self.assertEqual(license_obj.product_key, stored_before)
        finally:
            if "ITAMBOX_FIELD_ENCRYPTION_KEYS" in os.environ:
                del os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"]

    def test_rotate_encryption_keys_command(self):
        """Test key rotation: decrypt with the old key and re-encrypt with the new primary key."""
        mfr = Manufacturer.objects.create(name="Microsoft", slug="microsoft")
        software = Software.objects.create(name="Office 365", version="v2026", manufacturer=mfr)

        old_key = Fernet.generate_key().decode("ascii")
        new_key = Fernet.generate_key().decode("ascii")

        # Set old key as primary/only key
        os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = old_key
        raw_product_key = "MICROSOFT-OFFICE-KEY-2026"

        license_obj = License.objects.create(
            name="Office Suite", software=software, seats=10, product_key=encrypt_string(raw_product_key)
        )

        # Verify it encrypted correctly with the old key
        self.assertTrue(license_obj.product_key.startswith("enc$"))

        # Rotate key in settings (new key is primary, old key is fallback)
        os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = f"{new_key},{old_key}"

        # Call rotate_encryption_keys command
        call_command("rotate_encryption_keys")

        # Refresh from db
        license_obj.refresh_from_db()

        # Decrypted value should still be correct
        self.assertEqual(license_obj.decrypted_product_key, raw_product_key)

        # Product key in db should now be encrypted using new key (which is different from old key's ciphertext)
        # We can verify this by checking that decrypting the ciphertext with ONLY the new key succeeds!
        os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"] = new_key
        self.assertEqual(license_obj.decrypted_product_key, raw_product_key)

        # Clean up environment variables
        if "ITAMBOX_FIELD_ENCRYPTION_KEYS" in os.environ:
            del os.environ["ITAMBOX_FIELD_ENCRYPTION_KEYS"]
