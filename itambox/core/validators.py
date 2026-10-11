import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


def validate_external_url(url, allow_private=False):
    """
    SSRF guard for user/tenant-configured outbound URLs (webhooks, notification
    channels). Rejects non-HTTP(S) schemes and any URL whose host resolves to a
    loopback, link-local (incl. cloud metadata 169.254.169.254), private,
    reserved, multicast or unspecified address.

    Resolution is performed here and the caller should connect to the validated
    address; re-validate at send time to limit DNS-rebinding exposure.
    Returns the list of resolved ip_address objects on success.
    """
    if not url:
        raise ValidationError(_("A URL is required."))

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValidationError(_("URL scheme must be http or https."))
    host = parts.hostname
    if not host:
        raise ValidationError(_("URL has no host."))

    # A bare IP literal is checked directly even if DNS is unavailable.
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        infos = [(None, None, None, None, (str(literal_ip), 0))]
    else:
        # Resolve every address the host maps to and reject if ANY is internal.
        try:
            infos = socket.getaddrinfo(
                host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP
            )
        except socket.gaierror:
            # FAIL CLOSED. An unresolvable host cannot be pinned to a validated
            # address, and "allow it through" would let an attacker-controlled
            # resolver answer differently at send time (DNS rebinding: benign at
            # validation, 169.254.169.254 at connect). A transient DNS outage
            # making a webhook save/send fail is the acceptable cost.
            raise ValidationError(_("URL host could not be resolved; refusing to accept it unverified.")) from None

    resolved = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        resolved.append(ip)
        if allow_private:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValidationError(_("URL host resolves to a disallowed internal address (%(ip)s).") % {"ip": str(ip)})
    return resolved


class CustomValidator:
    def validate(self, instance, request=None):
        raise NotImplementedError

    @classmethod
    def validate_object(cls, instance):
        from django.conf import settings

        model_path = f"{instance._meta.app_label}.{instance._meta.model_name}"
        rules = getattr(settings, "CUSTOM_VALIDATORS", {}).get(model_path, {})
        if rules:
            parse_json_rules(instance, rules)


def _json_length_error(field_name, value, field_rules):
    error = None
    if "min_length" in field_rules and isinstance(value, str):
        min_length = field_rules["min_length"]
        if len(value) < min_length:
            error = ValidationError(
                _("%(field)s must be at least %(min)d characters."),
                params={"field": field_name, "min": min_length},
            )

    if "max_length" in field_rules and isinstance(value, str):
        max_length = field_rules["max_length"]
        if len(value) > max_length:
            error = ValidationError(
                _("%(field)s must be at most %(max)d characters."),
                params={"field": field_name, "max": max_length},
            )
    return error


def _json_numeric_error(field_name, value, field_rules):
    error = None
    if "min" in field_rules and isinstance(value, (int, float)):
        min_val = field_rules["min"]
        if value < min_val:
            error = ValidationError(
                _("%(field)s must be at least %(min)s."),
                params={"field": field_name, "min": min_val},
            )

    if "max" in field_rules and isinstance(value, (int, float)):
        max_val = field_rules["max"]
        if value > max_val:
            error = ValidationError(
                _("%(field)s must be at most %(max)s."),
                params={"field": field_name, "max": max_val},
            )
    return error


def _json_pattern_error(field_name, value, field_rules):
    if "pattern" not in field_rules or not isinstance(value, str):
        return None
    try:
        if not re.match(field_rules["pattern"], value):
            return ValidationError(
                _("%(field)s does not match required pattern."),
                params={"field": field_name},
            )
    except re.error:
        pass
    return None


def _validate_json_field(field_name, value, field_rules):
    if field_rules.get("required", False) and not value:
        return ValidationError(
            _("%(field)s is required."),
            params={"field": field_name},
        )
    if value is None or value == "":
        return None

    error = None
    for validator in (_json_length_error, _json_numeric_error, _json_pattern_error):
        field_error = validator(field_name, value, field_rules)
        if field_error is not None:
            error = field_error
    return error


def parse_json_rules(instance, rules):
    errors = {}
    for field_name, field_rules in rules.items():
        if field_name == "__all__":
            continue
        error = _validate_json_field(field_name, getattr(instance, field_name, None), field_rules)
        if error is not None:
            errors[field_name] = error

    if errors:
        raise ValidationError(errors)


def validate_file_attachment(file):
    # 1. Size Validation (10 MB limit)
    max_size = 10 * 1024 * 1024
    if file.size > max_size:
        raise ValidationError(_("File size must not exceed 10 MB."))

    # 2. Extension Validation (blacklist dangerous extensions). Kept broad because the
    # libmagic signature check below is best-effort (it degrades to '' when python-magic /
    # libmagic is unavailable in the running image), so the extension gate must stand alone.
    ext = os.path.splitext(file.name)[1].lower()
    dangerous_extensions = {
        ".exe",
        ".dll",
        ".bat",
        ".cmd",
        ".sh",
        ".bash",
        ".php",
        ".phtml",
        ".pl",
        ".py",
        ".cgi",
        ".asp",
        ".aspx",
        ".jsp",
        ".vbs",
        ".vbe",
        ".scr",
        ".pif",
        ".app",
        ".msi",
        ".com",
        ".htm",
        ".html",
        ".xhtml",
        ".shtml",
        ".xml",
        ".svg",
        ".svgz",
        ".mhtml",
        ".mht",
        ".jar",
        ".iso",
        ".jnlp",
        ".hta",
        ".js",
        ".jse",
        ".wsf",
        ".ps1",
        ".psm1",
        ".reg",
    }
    if ext in dangerous_extensions:
        raise ValidationError(
            _("Files with extension '%(ext)s' are not allowed for security reasons."), params={"ext": ext}
        )

    # 3. Magic Mime Validation (blacklist dangerous mime types)
    try:
        initial_pos = file.tell()
        file.seek(0)
        chunk = file.read(2048)
        file.seek(initial_pos)
        # inline import: optional-dependency: python-magic is excluded on native
        # Windows; the except branch is the documented fallback.
        import magic

        mime_type = magic.from_buffer(chunk, mime=True).lower()
    except Exception:
        # libmagic unavailable: do NOT fall back to the client-supplied
        # Content-Type (attacker-controlled, defeats this check). The extension
        # blacklist above remains the gate; treat the signature as unknown.
        mime_type = ""

    dangerous_mimes = {
        "application/x-dosexec",
        "application/x-msdownload",
        "text/html",
        "application/xml",
        "text/xml",
        "image/svg+xml",
        "application/x-executable",
        "application/x-sharedlib",
        "application/x-shellscript",
        "text/x-php",
        "application/x-httpd-php",
        "text/x-python",
        "text/x-script.python",
        "text/x-perl",
        "text/x-script.perl",
    }
    if mime_type in dangerous_mimes:
        raise ValidationError(_("Uploaded file signature is not allowed for security reasons."))


# The raster image signatures an attachment may carry. Shared by the upload
# validator and the inline image proxy: content whose signature is outside this
# set is never served inline.
SAFE_IMAGE_ATTACHMENT_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/x-ms-bmp", "image/webp"}
)


def detect_image_mime(file):
    """Return the MIME type detected in the file's own bytes, or "".

    Never trusts the client-supplied filename or Content-Type: it prefers a
    libmagic signature check and falls back to Pillow when libmagic is
    unavailable (native Windows, minimal runtime images). Callers compare the
    result against ``SAFE_IMAGE_ATTACHMENT_MIMES``: libmagic reports the real
    type of whatever it is handed, so a non-raster file comes back with its own
    MIME (``image/svg+xml`` for an SVG) rather than an empty string.
    """
    initial_pos = file.tell()
    file.seek(0)
    chunk = file.read(2048)
    file.seek(initial_pos)

    try:
        # inline import: optional-dependency: python-magic is excluded on native
        # Windows; the except branch is the documented Pillow fallback.
        import magic

        mime_type = magic.from_buffer(chunk, mime=True).lower()
    except Exception:
        # libmagic unavailable: verify the bytes are a real image with Pillow
        # rather than trusting the client-supplied Content-Type (which an
        # attacker controls). Anything Pillow can't decode fails closed.
        try:
            # inline import: optional-dependency: Pillow is the documented
            # fallback when libmagic is unavailable.
            from PIL import Image

            file.seek(0)
            with Image.open(file) as img:
                img.verify()
                pil_format = (img.format or "").lower()
            file.seek(initial_pos)
            mime_type = {
                "png": "image/png",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "bmp": "image/bmp",
                "webp": "image/webp",
            }.get(pil_format, "")
        except Exception:
            file.seek(initial_pos)
            mime_type = ""
    return mime_type


def validate_image_attachment(file):
    # 1. Size Validation (5 MB limit)
    max_size = 5 * 1024 * 1024
    if file.size > max_size:
        raise ValidationError(_("Image size must not exceed 5 MB."))

    # 2. Extension Validation (whitelist safe image extensions)
    ext = os.path.splitext(file.name)[1].lower()
    allowed_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    if ext not in allowed_extensions:
        raise ValidationError(
            _("Image format '%(ext)s' is not supported. Please upload a PNG, JPG, JPEG, GIF, BMP, or WebP image."),
            params={"ext": ext},
        )

    # 3. Magic Mime Validation (verify actual file signature)
    mime_type = detect_image_mime(file)

    if mime_type not in SAFE_IMAGE_ATTACHMENT_MIMES:
        raise ValidationError(
            _("Uploaded file signature does not match a valid image format (PNG, JPG, GIF, BMP, WebP).")
        )
