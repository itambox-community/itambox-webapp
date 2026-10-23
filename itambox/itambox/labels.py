"""Printable labels: which models carry one, what it says, and what its code encodes.

This module is domain-blind. A domain app opts a model in from its
``AppConfig.ready`` with :meth:`LabelRegistry.register`, describing the
human-readable fields a label may show. Everything else -- the code a label
encodes, how a scanned code is turned back into an object, the print route,
the toolbar and detail buttons -- is derived from that one registration, so
every labelled model behaves the same way.

The encoded reference is globally unique on purpose. A tenant-scoped natural
key (an asset tag) can repeat across the tenants an MSP operator reaches, so
a label must never rely on it to identify the object:

* with ``ITAMBOX_BASE_URL`` set, the absolute detail URL -- a phone camera
  opens it directly, and ITAMbox's scanners resolve it;
* otherwise ``itambox://<app_label>.<model_name>/<pk>``, which ITAMbox's
  scanners resolve.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import Resolver404, resolve, reverse

LABEL_SCHEME = "itambox://"


@dataclass(frozen=True)
class LabelField:
    """One human-readable value a label may print (``key`` is the template name)."""

    key: str
    label: str
    value: Callable[[object], object]

    def read(self, obj) -> str:
        value = self.value(obj)
        if value is None:
            return ""
        name = getattr(value, "name", None)
        return str(name if name is not None and not isinstance(value, str) else value)


@dataclass(frozen=True)
class LabelSpec:
    """How a model is labelled.

    ``fields`` are exposed to templates as ``obj.<key>``; the built-in card
    prints the title and the first two non-empty fields. ``barcode_value``
    opts the model into one-dimensional barcodes (Code 128 / Code 39), which
    cannot carry a URL: it returns the short identifier to encode. Models
    without it are QR-only. ``template_alias`` additionally exposes the field
    mapping under that name (``asset``), so templates written for the former
    asset-only engine keep rendering. ``legacy_scheme`` maps the pre-1.0
    ``itambox://<legacy_scheme>/<pk>`` form to this model.
    """

    fields: tuple[LabelField, ...]
    title: Callable[[object], object] = str
    barcode_value: Callable[[object], object] | None = None
    template_alias: str | None = None
    legacy_scheme: str | None = None
    keys: frozenset[str] = field(init=False)

    def __post_init__(self):
        keys = [label_field.key for label_field in self.fields]
        if len(keys) != len(set(keys)):
            raise ImproperlyConfigured("label fields must have unique keys")
        object.__setattr__(self, "keys", frozenset(keys))

    @property
    def supports_linear_barcodes(self) -> bool:
        return self.barcode_value is not None

    def values(self, obj) -> dict[str, str]:
        return {label_field.key: label_field.read(obj) for label_field in self.fields}


class LabelRegistry:
    """The labelled models. Populated from domain ``AppConfig.ready`` hooks."""

    def __init__(self):
        self._specs: dict[type, LabelSpec] = {}
        self._legacy: dict[str, type] = {}

    def register(self, model, spec: LabelSpec) -> None:
        if model in self._specs:
            # ``AppConfig.ready()`` runs again in tests, in management commands and
            # in every subprocess that calls ``django.setup()``, and the spec a
            # domain app builds there is a fresh object each time. The first
            # registration wins, so a repeated ready() neither raises nor
            # duplicates.
            return
        if not hasattr(model.objects, "for_scope"):
            # A label discloses the object's identity; only tenant-scoped
            # models, resolvable through the request scope, may print one.
            raise ImproperlyConfigured(f"{model._meta.label} must use a tenant-scoping manager to carry a label")
        if spec.legacy_scheme:
            existing = self._legacy.get(spec.legacy_scheme)
            if existing is not None and existing is not model:
                raise ImproperlyConfigured(f"legacy label scheme {spec.legacy_scheme!r} is already taken")
            self._legacy[spec.legacy_scheme] = model
        self._specs[model] = spec

    def unregister(self, model) -> None:
        spec = self._specs.pop(model, None)
        if spec is not None and spec.legacy_scheme:
            self._legacy.pop(spec.legacy_scheme, None)

    def get(self, model) -> LabelSpec | None:
        return self._specs.get(getattr(model, "_meta", None) and model._meta.concrete_model)

    def is_labelled(self, model) -> bool:
        return self.get(model) is not None

    def models(self) -> list[type]:
        return sorted(self._specs, key=lambda model: model._meta.label_lower)

    def by_label(self, label: str):
        """The labelled model for ``app_label.model_name`` (case-insensitive), or ``None``."""
        try:
            model = apps.get_model(label)
        except (LookupError, ValueError):
            return None
        return model if self.is_labelled(model) else None

    def legacy_model(self, scheme: str):
        return self._legacy.get(scheme.lower())


label_registry = LabelRegistry()


def label_reference(obj) -> str:
    """The globally unique code a label encodes for ``obj``."""
    base_url = (getattr(settings, "ITAMBOX_BASE_URL", "") or "").rstrip("/")
    if base_url:
        return base_url + obj.get_absolute_url()
    return f"{LABEL_SCHEME}{obj._meta.app_label}.{obj._meta.model_name}/{obj.pk}"


def _clean(code: str) -> str:
    raw = (code or "").strip().replace("﻿", "").replace("​", "").replace("：", ":")
    return raw.strip("\"' ")


def _parse_pk(text: str) -> int | None:
    text = text.strip("/ \\\"'")
    return int(text) if text.isdigit() else None


def _parse_scheme(raw: str):
    body = raw[len(LABEL_SCHEME) :]
    head, _sep, tail = body.partition("/")
    head = head.lower()
    pk = _parse_pk(tail)
    if pk is None:
        return None
    model = label_registry.by_label(head) if "." in head else label_registry.legacy_model(head)
    return (model, pk) if model is not None else None


def _detail_route_model(path: str):
    # inline import: app-registry: itambox.utils loads ContentType, and this module is
    # imported by AppConfig modules before the app registry is ready.
    from itambox.utils import get_model_viewname

    try:
        match = resolve(path)
    except Resolver404:
        return None
    pk = match.kwargs.get("pk")
    if not isinstance(pk, int):
        return None
    for model in label_registry.models():
        if match.view_name == get_model_viewname(model, "detail"):
            return model, pk
    return None


def _parse_url(raw: str):
    parts = urlsplit(raw)
    path = parts.path or "/"
    base_url = (getattr(settings, "ITAMBOX_BASE_URL", "") or "").rstrip("/")
    if base_url:
        base = urlsplit(base_url)
        if parts.netloc.lower() == base.netloc.lower() and base.path and path.startswith(base.path + "/"):
            path = path[len(base.path) :]
    return _detail_route_model(path)


def parse_label_reference(code: str):
    """Return ``(model, pk)`` for a code printed by :func:`label_reference`, else ``None``.

    Accepts ``itambox://<app>.<model>/<pk>``, the pre-1.0 legacy scheme a
    model registered (``itambox://asset/<pk>``), and an http(s) URL whose path
    resolves to a labelled model's detail route (an ``ITAMBOX_BASE_URL``
    sub-path is honoured). Bare tags, serials and EANs are not references and
    return ``None``; callers fall back to their own lookups.
    """
    raw = _clean(code)
    lowered = raw.lower()
    if lowered.startswith(LABEL_SCHEME):
        return _parse_scheme(raw)
    if lowered.startswith(("http://", "https://")):
        return _parse_url(raw)
    return None


def label_print_url(model, pk=None) -> str | None:
    """The print route for ``model`` (pre-selecting one object), or ``None`` without a label."""
    if not label_registry.is_labelled(model):
        return None
    meta = model._meta
    url = reverse("label_print", kwargs={"app_label": meta.app_label, "model_name": meta.model_name})
    return f"{url}?pk={pk}" if pk is not None else url


def user_label_print_url(user, model, obj=None) -> str | None:
    """:func:`label_print_url` when ``user`` may view ``model`` (or ``obj``), else ``None``."""
    if user is None or not user.is_authenticated or not label_registry.is_labelled(model):
        return None
    perm = f"{model._meta.app_label}.view_{model._meta.model_name}"
    if not (user.has_perm(perm, obj=obj) if obj is not None else user.has_perm(perm)):
        return None
    return label_print_url(model, obj.pk if obj is not None else None)
