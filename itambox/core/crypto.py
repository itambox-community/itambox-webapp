import base64
import hashlib
import hmac
import logging
import os

from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.config_contract import ConfigState, parse_field_encryption_keys

logger = logging.getLogger(__name__)


def _configured_keys_str():
    """The configured keyring value: the environment is the single source of truth.

    ``core.settings.base`` derives the tri-state from the same environment, so
    enforcement (production settings import), key resolution (here), and the
    tagged check surface always agree. The value is deliberately never read
    from a Django setting attribute.
    """
    return os.environ.get("ITAMBOX_FIELD_ENCRYPTION_KEYS")


def get_fernet():
    """
    Get a Fernet or MultiFernet instance.

    Uses the comma-separated ITAMBOX_FIELD_ENCRYPTION_KEYS keyring parsed by
    ``core.config_contract.parse_field_encryption_keys`` — the same validation
    production settings enforce at import. Unset/blank falls back to SECRET_KEY
    hashing (development convenience, loudly warned in production). Explicitly
    malformed key material fails closed with a secret-free diagnostic naming
    only the failing key index.
    """
    result = parse_field_encryption_keys(_configured_keys_str())

    if result.state == ConfigState.VALID:
        fernet_instances = [Fernet(key) for key in result.keys]
        if len(fernet_instances) > 1:
            return MultiFernet(fernet_instances)
        return fernet_instances[0]

    if result.state == ConfigState.MALFORMED:
        raise ImproperlyConfigured(f"ITAMBOX_FIELD_ENCRYPTION_KEYS is malformed: {result.error}.")

    key_bytes = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def derive_purpose_keys(purpose: str) -> list[tuple[str, bytes]]:
    """Derive one 32-byte key per keyring entry for a single named purpose.

    Each ITAMBOX_FIELD_ENCRYPTION_KEYS entry (or, when the keyring is unset,
    the same SECRET_KEY-derived material get_fernet() falls back to) is fed
    through HKDF-SHA256 with ``purpose`` as the context, so a derived key is
    never the encryption key itself and two purposes never share a key. The
    first entry is the current key; the rest exist so material written under
    a rotated-out key can still be checked. Each key travels with a short,
    non-secret identifier derived from it.
    """
    result = parse_field_encryption_keys(_configured_keys_str())
    if result.state == ConfigState.MALFORMED:
        raise ImproperlyConfigured(f"ITAMBOX_FIELD_ENCRYPTION_KEYS is malformed: {result.error}.")
    if result.state == ConfigState.VALID:
        materials = [base64.urlsafe_b64decode(key) for key in result.keys]
    else:
        materials = [hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()]

    derived = []
    for material in materials:
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=f"itambox:{purpose}".encode(),
        ).derive(material)
        key_id = hmac.new(key, b"itambox-key-id", hashlib.sha256).hexdigest()[:16]
        derived.append((key_id, key))
    return derived


def is_using_derived_encryption_key() -> bool:
    """
    True exactly when the field-encryption keyring is unset/blank, meaning
    get_fernet() falls back to deriving the key from SECRET_KEY.

    Mirrors the parsing in get_fernet() via the same helper: explicitly
    malformed material is NOT an accidental fallback — production settings
    refuse to import it and get_fernet() raises. When this returns True,
    rotating SECRET_KEY makes all encrypted fields unrecoverable.
    """
    result = parse_field_encryption_keys(_configured_keys_str())
    return result.state is ConfigState.UNSET


def encrypt_string(plain_text: str) -> str:
    """
    Encrypt a plaintext string and prepend the 'enc$' prefix sentinel.
    If the string is empty, returns an empty string.
    """
    if not plain_text:
        return ""

    fernet = get_fernet()
    encrypted_bytes = fernet.encrypt(plain_text.encode("utf-8"))
    return f"enc${encrypted_bytes.decode('ascii')}"


def decrypt_string(cipher_text: str) -> str:
    """
    Decrypt a cipher string starting with the 'enc$' prefix sentinel.
    Raises ValueError if a non-encrypted string is passed.

    Raises ValueError when decryption fails.
    """
    if not cipher_text:
        return ""

    if not cipher_text.startswith("enc$"):
        raise ValueError("Provided value is not encrypted (missing 'enc$' prefix).")

    fernet = get_fernet()
    try:
        encrypted_part = cipher_text[4:]
        decrypted_bytes = fernet.decrypt(encrypted_part.encode("ascii"))
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to decrypt string: {e}", exc_info=True)
        raise ValueError(f"Decryption failed: {str(e)}") from e
