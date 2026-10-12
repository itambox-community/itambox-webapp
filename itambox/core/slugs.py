"""Kernel-owned slug generation helper."""

from django.utils.text import slugify


def generate_unique_slug(instance, slug_source=None, slug_field="slug"):
    if getattr(instance, slug_field, None):
        return
    slug_source = slug_source if slug_source is not None else getattr(instance, "slug_source", "name")
    if isinstance(slug_source, (list, tuple)):
        values = []
        for field_name in slug_source:
            obj = instance
            for part in field_name.split("__"):
                obj = getattr(obj, part, None) if obj else None
            if obj:
                values.append(str(obj))
        source = "-".join(values)
    else:
        source = getattr(instance, slug_source, "")
    max_length = instance._meta.get_field(slug_field).max_length
    base_slug = (slugify(source) or "auto-slug")[:max_length]
    current_slug = base_slug
    counter = 1
    manager = getattr(instance.__class__, "_base_manager", instance.__class__.objects)
    while manager.filter(**{slug_field: current_slug}).exclude(pk=instance.pk).exists():
        suffix = f"-{counter}"
        current_slug = f"{base_slug[: max_length - len(suffix)]}{suffix}"
        counter += 1
    setattr(instance, slug_field, current_slug)
