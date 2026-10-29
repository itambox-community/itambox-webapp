from django.urls import reverse


def resolve_generic_items(rows, toggle_url_name=None):
    """
    Resolve a list of Bookmark or ObjectWatch rows to dicts suitable for templates.
    Rows must have .user, .model (ContentType FK), .object_id, .created, .pk.
    Objects that no longer exist under the active tenant scope are silently skipped.
    """

    by_ct = {}
    for row in rows:
        by_ct.setdefault(row.model, []).append(row)

    resolved = {}
    for ct, ct_rows in by_ct.items():
        model_class = ct.model_class()
        if not model_class:
            continue
        ids = [r.object_id for r in ct_rows]
        for obj in model_class.objects.filter(pk__in=ids):
            resolved[(ct.id, obj.pk)] = obj

    items = []
    for row in rows:
        obj = resolved.get((row.model.id, row.object_id))
        if not obj:
            continue
        url = "#"
        if hasattr(obj, "get_absolute_url"):
            try:
                url = obj.get_absolute_url()
            except Exception:
                pass
        item = {
            "id": row.pk,
            "type_name": obj._meta.verbose_name.title(),
            "name": str(obj),
            "url": url,
            "created": row.created,
            "content_type_id": row.model.pk,
            "object_id": row.object_id,
        }
        if toggle_url_name:
            try:
                item["toggle_url"] = reverse(
                    toggle_url_name,
                    kwargs={
                        "content_type_id": row.model.pk,
                        "object_id": row.object_id,
                    },
                )
            except Exception:
                item["toggle_url"] = "#"
        items.append(item)
    return items
