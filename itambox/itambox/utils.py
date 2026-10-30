import datetime
import logging
import os
from decimal import Decimal
from uuid import UUID

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Model
from django.db.models.fields.files import FieldFile
from django.urls import NoReverseMatch, reverse
from django.utils.module_loading import import_string
from django.utils.text import slugify

from itambox.constants import DEFAULT_PAGINATE_COUNT, PAGINATE_COUNT_CHOICES

logger = logging.getLogger(__name__)


def get_model_viewname(model, action):
    app_label = model._meta.app_label
    if app_label == "components":
        app_label = "assets"
    elif app_label == "auth":
        app_label = "users"
    model_name = model._meta.model_name
    if app_label == "core":
        # core-app models (e.g. ObjectChange/changelog) are root-mounted, unnamespaced.
        act = "add" if action == "create" else action
        return f"{model_name}_{act}"
    return f"{app_label}:{model_name}_{action}"


def model_view_url(model, action, **kwargs):
    """URL of ``model``'s ``action`` view, or ``None`` when the model has no such view."""
    try:
        return reverse(get_model_viewname(model, action), kwargs=kwargs or None)
    except NoReverseMatch:
        return None


def get_paginate_count(request):
    try:
        per_page_param = request.GET.get("per_page")
        if per_page_param:
            per_page = int(per_page_param)
            if per_page in dict(PAGINATE_COUNT_CHOICES):
                return per_page
    except (ValueError, TypeError):
        logger.debug("Invalid per_page query parameter: '%s'", request.GET.get("per_page"))

    if request.user.is_authenticated:
        try:
            UserPreference = apps.get_model("users", "UserPreference")

            if not hasattr(request, "_user_preferences_cache"):
                request._user_preferences_cache = UserPreference.objects.filter(user=request.user).first()
            prefs = request._user_preferences_cache
            if prefs and prefs.data:
                user_pref_val = prefs.data.get("pagination", {}).get("per_page")
                if user_pref_val:
                    try:
                        user_pref = int(user_pref_val)
                        if user_pref in dict(PAGINATE_COUNT_CHOICES):
                            return user_pref
                    except (ValueError, TypeError):
                        logger.debug("Invalid user preference pagination value: '%s'", user_pref_val)
        # broad except: render-degrade: an unreadable preference falls back to the default page size
        except Exception as e:
            logger.debug("Error reading user pagination preferences: %s", e)

    return DEFAULT_PAGINATE_COUNT


class ChoiceSet:
    CHOICES = []

    def __iter__(self):
        yield from [(c[0], c[1]) for c in self.CHOICES]


def serialize_object(obj: Model, extra_fields=None, exclude_fields=None) -> dict:
    if not obj:
        return None

    if extra_fields is None:
        extra_fields = set()
    if exclude_fields is None:
        exclude_fields = set()

    data = {}
    m2m_fields = {f.name for f in obj._meta.many_to_many}

    for field in obj._meta.get_fields():
        field_name = field.name

        if field_name in exclude_fields:
            continue

        if not field.concrete or field.name == obj._meta.pk.name:
            if field.name not in extra_fields:
                continue

        try:
            field_value = getattr(obj, field_name)
        except AttributeError:
            continue

        if field_name in m2m_fields:
            if hasattr(field_value, "all"):
                try:
                    data[field_name] = sorted(list(field_value.values_list("pk", flat=True)))
                # broad except: boundary-isolation: an unreadable M2M relation is recorded as empty
                # rather than aborting the audited save
                except Exception:
                    data[field_name] = []
            else:
                data[field_name] = []
        elif field.is_relation:
            if field_value is not None:
                data[field_name] = field_value.pk
            else:
                data[field_name] = None
        else:
            if isinstance(field_value, (datetime.date, datetime.datetime, datetime.time)):
                data[field_name] = field_value.isoformat()
            elif isinstance(field_value, (Decimal, UUID)):
                data[field_name] = str(field_value)
            else:
                if isinstance(field_value, FieldFile):
                    data[field_name] = field_value.name if field_value else None
                else:
                    data[field_name] = field_value

    return data


def get_content_type_by_natural_key(natural_key):
    try:
        app_label, model = natural_key.lower().split(".")
        return ContentType.objects.get(app_label=app_label, model=model)
    except (ContentType.DoesNotExist, ValueError, AttributeError):
        return None


def get_table_for_model(model):
    app_label = model._meta.app_label
    model_name = model.__name__
    table_class_name = f"{model_name}Table"
    try:
        tables_module = import_string(f"{app_label}.tables")
        return getattr(tables_module, table_class_name)
    except (ImportError, AttributeError):
        logger.warning("Could not find %s in %s.tables", table_class_name, app_label)
        return None


def get_help_url(view_instance, app_label=None, model_name=None):
    """
    Resolves the local static documentation help link for a given view context.
    Checks if the compiled HTML file or its directory index exists in static/docs.
    """
    doc_path = getattr(view_instance, "document_path", None)
    if not doc_path and app_label and model_name:
        # Resolve path mismatch for components which belong to the inventory app
        resolved_app = "components" if app_label == "inventory" and model_name.startswith("component") else app_label
        # Resolve path mismatch for assetmaintenance (its app_label in model is assets, but docs are in compliance)
        if app_label == "assets" and model_name == "assetmaintenance":
            resolved_app = "compliance"
        doc_path = f"models/{resolved_app}/{model_name}"

    if not doc_path:
        return f"{settings.STATIC_URL}docs/index.html"

    static_docs_dir = os.path.join(settings.BASE_DIR, "static", "docs")

    file_target = os.path.join(static_docs_dir, f"{doc_path}.html")
    dir_target = os.path.join(static_docs_dir, doc_path, "index.html")

    if os.path.exists(file_target):
        return f"{settings.STATIC_URL}docs/{doc_path}.html"
    elif os.path.exists(dir_target):
        return f"{settings.STATIC_URL}docs/{doc_path}/index.html"
    return f"{settings.STATIC_URL}docs/index.html"


def generate_unique_slug(instance, slug_source=None, slug_field="slug"):
    """
    Helper to automatically generate a unique slug field on an instance.
    By default, it will slugify the field specified by instance.slug_source (default: 'name').
    It handles list/tuple of field names and double-underscore relation lookups (e.g. manufacturer__name).
    If there is a collision, it appends a counter to ensure uniqueness.
    """
    if getattr(instance, slug_field, None):
        return

    if slug_source is None:
        slug_source = getattr(instance, "slug_source", "name")

    if isinstance(slug_source, (list, tuple)):
        source_values = []
        for field_name in slug_source:
            if "__" in field_name:
                parts = field_name.split("__")
                obj = instance
                for part in parts:
                    obj = getattr(obj, part, None) if obj else None
                val = str(obj) if obj else ""
            else:
                val = getattr(instance, field_name, "")
            if val:
                source_values.append(str(val))
        slug_src = "-".join(source_values)
    else:
        slug_src = getattr(instance, slug_source, "")

    slug_val = slugify(slug_src) or "auto-slug"
    base_slug = slug_val
    counter = 1
    model_class = instance.__class__
    manager = getattr(model_class, "_base_manager", model_class.objects)

    current_slug = base_slug
    while manager.filter(**{slug_field: current_slug}).exclude(pk=instance.pk).exists():
        current_slug = f"{base_slug}-{counter}"
        counter += 1

    setattr(instance, slug_field, current_slug)


def get_status_color(status):
    """Map Site, Location, and Subscription status names/slugs to suiting hex colors."""
    if not status:
        return "6c757d"
    status_lower = str(status).lower()
    status_colors = {
        # Site / Location
        "planned": "0d6efd",
        "staging": "fd7e14",
        "active": "20c997",
        "decommissioning": "e83e8c",
        "retired": "6c757d",
        # Subscription
        "expired": "dc3545",
        "cancelled": "6f42c1",
        "pending": "ffc107",
        "suspended": "fd7e14",
        "renewing": "0d6efd",
        "trial": "17a2b8",
    }
    return status_colors.get(status_lower, "6c757d")
