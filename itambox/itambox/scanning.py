def strip_itambox_prefix(code: str) -> str:
    """Strip standard itambox:// or itambox: prefix from the scanned code, leaving the bare tag/serial."""
    if not code:
        return ""
    # Defensive cleaning: strip spaces, quotes, BOM, zero-width spaces, and normalize colons
    raw = code.strip().replace("\ufeff", "").replace("\u200b", "")
    raw = raw.replace("：", ":")
    raw = raw.strip("\"' ")

    lowered = raw.lower()
    # Object references (``itambox://<app>.<model>/<pk>`` and the legacy
    # ``itambox://asset/<pk>``) identify a row, not a tag: keep them whole so
    # the resolver can parse them.
    if lowered.startswith("itambox://asset/"):
        return raw
    head = lowered[len("itambox://") :].partition("/")[0] if lowered.startswith("itambox://") else ""
    if "." in head and lowered[len("itambox://") + len(head) :].startswith("/"):
        return raw

    if raw.lower().startswith("itambox://"):
        raw = raw[len("itambox://") :].strip("/ \\\"'")
    elif raw.lower().startswith("itambox:"):
        raw = raw[len("itambox:") :].strip("/ \\\"'")
    return raw
