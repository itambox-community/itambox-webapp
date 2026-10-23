"""Signature payloads that pass the server-side PNG check of the consent flow."""

import base64

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def signature_png(label="signature"):
    """A PNG data URI; ``label`` keeps payloads (and their hashes) distinct."""
    return "data:image/png;base64," + base64.b64encode(PNG_MAGIC + label.encode("utf-8")).decode("ascii")
